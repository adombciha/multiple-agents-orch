"""Checks for the reversible Hindsight deployment and database shutdown order."""

import importlib.util
from pathlib import Path
import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest


def load_script(name):
    path = Path(__file__).parent / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_payload_keeps_private_settings_and_limits_local_worker():
    module = load_script("deploy_hindsight_local")
    original = {
        "State": {"Running": False},
        "Image": "sha256:pinned",
        "Config": {
            "Image": "old:tag",
            "Env": [
                "PRIVATE_KEY=secret",
                "HINDSIGHT_API_RETAIN_LLM_PROVIDER=cloud",
                "HINDSIGHT_API_LLM_PROVIDER=ollama",
                "HINDSIGHT_API_LLM_BASE_URL=http://172.17.144.1:11434/v1",
                "HINDSIGHT_API_LLM_MODEL=gemma4:e2b",
            ],
            "User": "hindsight",
            "Cmd": ["/app/start-all.sh"],
        },
        "HostConfig": {
            "Binds": ["hindsight-data:/home/hindsight/.pg0"],
            "PortBindings": {"8888/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8888"}]},
            "NetworkMode": "bridge",
            "Memory": 1024,
            "RestartPolicy": {"Name": "unless-stopped"},
        },
    }
    result = module.replacement_payload(
        original, Path(__file__).parent, Path("/tmp/shared-locks")
    )
    environment = dict(value.split("=", 1) for value in result["Env"])
    assert result["Image"] == "sha256:pinned"
    assert result["User"] == "hindsight"
    assert result["HostConfig"]["Binds"] == original["HostConfig"]["Binds"]
    assert result["HostConfig"]["Memory"] == 1024
    assert (
        result["HostConfig"]["PortBindings"] == original["HostConfig"]["PortBindings"]
    )
    assert environment["PRIVATE_KEY"] == "secret"
    assert environment["HINDSIGHT_API_WORKER_ENABLED"] == "false"
    assert environment["HINDSIGHT_API_WORKER_CONSOLIDATION_RESERVED_SLOTS"] == "0"
    assert environment["HINDSIGHT_API_RETAIN_LLM_PROVIDER"] == "ollama"
    assert environment["HINDSIGHT_API_WORKER_MAX_SLOTS"] == "1"
    assert "Mounts" not in original["HostConfig"]


def test_deployment_restores_original_name_when_start_fails(monkeypatch, tmp_path):
    module = load_script("deploy_hindsight_local")
    calls = []
    original = {
        "Image": "sha256:pinned",
        "State": {"Running": False},
        "Config": {
            "Env": [
                "HINDSIGHT_API_LLM_PROVIDER=ollama",
                "HINDSIGHT_API_LLM_MODEL=local-model",
                "HINDSIGHT_API_LLM_BASE_URL=http://127.0.0.1:11434/v1",
            ]
        },
        "HostConfig": {"NetworkMode": "bridge"},
    }

    def docker(method, path, body=None):
        calls.append((method, path))
        if path == "/containers/hindsight/json":
            return original
        if path == "/containers/create?name=hindsight":
            return {"Id": "replacement"}
        if path == "/containers/replacement/start":
            raise RuntimeError("start failed")
        if path == "/containers/replacement/json":
            return {"State": {"Running": False}}

    monkeypatch.setattr(module, "docker", docker)
    monkeypatch.setattr(
        "sys.argv",
        ["deploy_hindsight_local", "--apply", "--resource-dir", str(tmp_path)],
    )
    with pytest.raises(RuntimeError, match="start failed"):
        module.main()
    assert calls[-2] == ("DELETE", "/containers/replacement")
    assert calls[-1][0] == "POST"
    assert calls[-1][1].endswith("/rename?name=hindsight")


def test_entrypoint_drains_guard_before_stopping_api(monkeypatch):
    module = load_script("hindsight_local_entrypoint")
    events = []

    class Process:

        def __init__(self, name):
            self.name = name
            self.pid = 12345

        def poll(self):
            return None

        def terminate(self):
            events.append(f"terminate {self.name}")

        def wait(self):
            events.append(f"wait {self.name}")

    def start(command, **kwargs):
        return Process("api" if command[0] == "/app/start-all.sh" else "guard")

    class Event:

        def is_set(self):
            return False

        def wait(self, _):
            return True

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    monkeypatch.setattr(module.threading, "Event", Event)
    monkeypatch.setattr(module.signal, "signal", lambda *_: None)
    monkeypatch.setattr(module.subprocess, "Popen", start)
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *_, **__: Response())
    monkeypatch.setattr(module, "os", SimpleNamespace(environ={}))
    monkeypatch.setattr(
        module, "_drain_orphans", lambda group: events.append("drain orphans")
    )
    assert module.main() == 0
    assert events == [
        "terminate guard",
        "wait guard",
        "drain orphans",
        "terminate api",
        "wait api",
    ]


def test_orphan_drain_waits_for_native_backstop_before_sending_one_term(monkeypatch):
    module = load_script("hindsight_local_entrypoint")
    events = []
    checks = iter([False, False, True])

    def wait(group, timeout):
        events.append(("wait", group, timeout))
        return next(checks)

    monkeypatch.setattr(module, "_wait_worker_group", wait)
    monkeypatch.setattr(
        module.os, "killpg", lambda *args: events.append(("signal", *args))
    )
    module._drain_orphans(12345)
    assert events == [
        ("wait", 12345, 125),
        ("signal", 12345, module.signal.SIGTERM),
        ("wait", 12345, 125),
        ("wait", 12345, 125),
    ]


@pytest.mark.skipif(
    sys.platform != "linux", reason="Linux process groups and orphan adoption"
)
def test_killed_guard_keeps_api_alive_until_already_draining_worker_exits(tmp_path):
    worker = """
import pathlib, signal, sys, time
root = pathlib.Path(sys.argv[1])
def drain(*_):
    path = root / 'term-count'
    count = int(path.read_text()) + 1 if path.exists() else 1
    path.write_text(str(count))
    time.sleep(0.5)
    sys.exit(0)
signal.signal(signal.SIGTERM, drain)
(root / 'ready').touch()
time.sleep(30)
"""
    guard = f"""
import pathlib, subprocess, sys, time
worker = subprocess.Popen([sys.executable, '-c', {worker!r}, sys.argv[1]])
pathlib.Path(sys.argv[1], 'worker-pid').write_text(str(worker.pid))
time.sleep(30)
"""
    harness = textwrap.dedent(f"""
        import ctypes, importlib.util, os, pathlib, signal, subprocess, sys, threading, time
        ctypes.CDLL(None).prctl(36, 1, 0, 0, 0)
        root = pathlib.Path({str(tmp_path)!r})
        spec = importlib.util.spec_from_file_location('entry', {str(Path(__file__).parent / 'scripts/hindsight_local_entrypoint.py')!r})
        entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entry)
        real_popen = subprocess.Popen
        processes = {{}}
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *_): pass
        entry.urllib.request.urlopen = lambda *args, **kwargs: Response()
        def launch(command, **kwargs):
            name = 'api' if command[0] == '/app/start-all.sh' else 'guard'
            code = 'import time; time.sleep(30)' if name == 'api' else {guard!r}
            process = real_popen([sys.executable, '-c', code, str(root)], **kwargs)
            processes[name] = process
            return process
        entry.subprocess.Popen = launch
        def crash_guard():
            while not (root / 'ready').exists() or 'guard' not in processes:
                time.sleep(0.01)
            worker_pid = int((root / 'worker-pid').read_text())
            os.kill(worker_pid, signal.SIGTERM)
            while not (root / 'term-count').exists():
                time.sleep(0.01)
            processes['guard'].kill()
            time.sleep(0.15)
            if processes['api'].poll() is None:
                (root / 'api-alive-during-drain').touch()
        thread = threading.Thread(target=crash_guard, daemon=True)
        thread.start()
        assert entry.main() == 1
        thread.join(timeout=2)
        assert (root / 'api-alive-during-drain').exists()
        assert (root / 'term-count').read_text() == '1'
    """)
    result = subprocess.run(
        [sys.executable, "-c", harness], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
