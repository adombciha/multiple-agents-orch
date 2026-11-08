"""Keep the stock Hindsight API alive while a guarded native worker runs."""

import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request


def _wait_worker_group(group, timeout):
    deadline = time.monotonic() + timeout
    while True:
        try:
            while os.waitpid(-group, os.WNOHANG)[0]:
                pass
        except ChildProcessError:
            pass
        try:
            os.killpg(group, 0)
        except ProcessLookupError:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def _drain_orphans(group):
    # An orphan may already be draining; its native exit backstop is 120 seconds.
    if not _wait_worker_group(group, 125):
        try:
            os.killpg(group, signal.SIGTERM)
        except ProcessLookupError:
            return
        while not _wait_worker_group(group, 125):
            print(
                "Waiting for orphan Hindsight worker before stopping its database",
                flush=True,
            )


def main():
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    os.environ["HINDSIGHT_API_WORKER_ENABLED"] = "false"
    api = subprocess.Popen(["/app/start-all.sh"])
    guard = None
    exit_code = 0
    try:
        port = os.environ.get("HINDSIGHT_API_PORT", "8888")
        deadline = time.monotonic() + float(
            os.environ.get("HINDSIGHT_API_STARTUP_WAIT_SECONDS", "300")
        )
        print(
            "Waiting for Hindsight API readiness before starting background worker",
            flush=True,
        )
        while not stop.is_set() and api.poll() is None:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health", timeout=2
                ) as response:
                    if response.status == 200:
                        break
            except (OSError, urllib.error.URLError):
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("Hindsight API readiness timed out")
            stop.wait(0.5)
        if stop.is_set():
            return 0
        if api.poll() is not None:
            raise RuntimeError("Hindsight API exited before readiness")
        guard = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).with_name("hindsight_worker_guard.py")),
                "--resource-dir",
                "/opt/hindsight-coordination",
                "--idle-grace",
                "30",
                "--drain-timeout",
                "125",
                "--",
                "/app/api/.venv/bin/hindsight-worker",
                "--worker-id",
                "hindsight-local-background",
                "--http-host",
                "127.0.0.1",
                "--http-port",
                "8889",
            ],
            start_new_session=True,
        )
        while not stop.wait(0.2):
            if api.poll() is not None or guard.poll() is not None:
                exit_code = 1
                break
    except Exception as error:
        print(
            f"Hindsight local entrypoint failed: {type(error).__name__}",
            file=sys.stderr,
        )
        exit_code = 1
    finally:
        # The API owns pg0, so drain the worker before stopping its database.
        if guard is not None and guard.poll() is None:
            guard.terminate()
            guard.wait()
        if guard is not None:
            _drain_orphans(guard.pid)
        if api.poll() is None:
            api.terminate()
            api.wait()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
