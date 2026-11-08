"""Exercise real process locks without contacting a model or Docker."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from orchestrator.core.local_resources import LocalResourceBusy, foreground_slot

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX flock coordination")
ROOT = Path(__file__).resolve().parent
WORKER = """
import json, os, signal, sys, time
path, delay = sys.argv[1], float(sys.argv[2])
def record(event):
    with open(path, 'a') as file:
        file.write(json.dumps({'event': event, 'pid': os.getpid(), 'pgid': os.getpgrp()}) + '\\n')
def stop(signum, frame):
    record('draining')
    time.sleep(delay)
    record('stopped')
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
record('ready')
while True:
    time.sleep(0.01)
"""
FOREGROUND = """
from pathlib import Path
import sys, time
from orchestrator.core.local_resources import foreground_slot
directory, marker, release = map(Path, sys.argv[1:])
with foreground_slot(directory, timeout=5, poll_interval=0.01):
    marker.write_text('entered')
    while not release.exists():
        time.sleep(0.01)
"""


def wait_until(check, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.01)
    raise AssertionError("Timed out waiting for subprocess state")


def events(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture
def processes(tmp_path):
    children = []

    def start(argv, **kwargs):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(ROOT))
        child = subprocess.Popen(argv, cwd=ROOT, env=env, **kwargs)
        children.append(child)
        return child

    yield start
    for child in children:
        if child.poll() is None:
            child.terminate()
    for child in children:
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=3)
    for log in tmp_path.glob("*.jsonl"):
        for event in events(log):
            try:
                os.kill(event["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass


def start_guard(start, directory, log, delay=0.1, output=None, idle_grace=0):
    return start(
        [
            sys.executable,
            str(ROOT / "scripts/hindsight_worker_guard.py"),
            "--resource-dir",
            str(directory),
            "--poll-interval",
            "0.01",
            "--drain-timeout",
            "0.05",
            "--idle-grace",
            str(idle_grace),
            "--",
            sys.executable,
            "-u",
            "-c",
            WORKER,
            str(log),
            str(delay),
        ],
        stdout=output if output is not None else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


def test_foreground_requests_exclude_each_other_and_release_after_error(
    tmp_path, processes
):
    directory = tmp_path / "locks"
    first = tmp_path / "first"
    second = tmp_path / "second"
    release_first = tmp_path / "release-first"
    release_second = tmp_path / "release-second"
    one = processes(
        [
            sys.executable,
            "-c",
            FOREGROUND,
            str(directory),
            str(first),
            str(release_first),
        ]
    )
    wait_until(first.exists)
    two = processes(
        [
            sys.executable,
            "-c",
            FOREGROUND,
            str(directory),
            str(second),
            str(release_second),
        ]
    )
    callbacks = []
    with pytest.raises(LocalResourceBusy):
        with foreground_slot(
            directory,
            timeout=0.05,
            poll_interval=0.01,
            on_wait=lambda: callbacks.append(1),
        ):
            pytest.fail("The first foreground process still holds the resource")
    assert callbacks == [1]
    assert not second.exists()
    release_first.touch()
    assert one.wait(timeout=3) == 0
    wait_until(second.exists)
    release_second.touch()
    assert two.wait(timeout=3) == 0
    with pytest.raises(ValueError, match="caller failed"):
        with foreground_slot(directory, timeout=0):
            raise ValueError("caller failed")
    with foreground_slot(directory, timeout=0):
        pass


def test_worker_drains_before_foreground_and_resumes_afterwards(tmp_path, processes):
    directory = tmp_path / "locks"
    log = tmp_path / "worker.jsonl"
    guard = start_guard(processes, directory, log)
    wait_until(lambda: any(item["event"] == "ready" for item in events(log)))
    assert events(log)[0]["pgid"] == guard.pid
    with foreground_slot(directory, timeout=3, poll_interval=0.01):
        assert [item["event"] for item in events(log)] == [
            "ready",
            "draining",
            "stopped",
        ]
        time.sleep(0.05)
        assert len(events(log)) == 3
    wait_until(lambda: sum(item["event"] == "ready" for item in events(log)) == 2)
    guard.terminate()
    assert guard.wait(timeout=3) == 0
    with foreground_slot(directory, timeout=0):
        pass


def test_drain_overrun_does_not_release_live_worker_lock(tmp_path, processes):
    directory = tmp_path / "locks"
    log = tmp_path / "worker.jsonl"
    output = tmp_path / "supervisor.log"
    with output.open("w") as stream:
        guard = start_guard(processes, directory, log, delay=0.5, output=stream)
        wait_until(lambda: any(item["event"] == "ready" for item in events(log)))
        with pytest.raises(LocalResourceBusy):
            with foreground_slot(directory, timeout=0.15, poll_interval=0.01):
                pytest.fail("The worker has not drained")
        assert "worker_drain_overrun" in output.read_text()
        assert "stopped" not in [item["event"] for item in events(log)]
        with foreground_slot(directory, timeout=3, poll_interval=0.01):
            assert "stopped" in [item["event"] for item in events(log)]
        guard.terminate()
        assert guard.wait(timeout=3) == 0


def test_supervisor_crash_preserves_worker_lock_until_worker_exits(tmp_path, processes):
    directory = tmp_path / "locks"
    log = tmp_path / "worker.jsonl"
    guard = start_guard(processes, directory, log, delay=0)
    wait_until(lambda: any(item["event"] == "ready" for item in events(log)))
    worker_pid = events(log)[0]["pid"]
    guard.kill()
    guard.wait(timeout=3)
    with pytest.raises(LocalResourceBusy):
        with foreground_slot(directory, timeout=0.05, poll_interval=0.01):
            pytest.fail("The orphan worker must retain its inherited resource lock")
    os.kill(worker_pid, signal.SIGTERM)
    with foreground_slot(directory, timeout=3, poll_interval=0.01):
        assert events(log)[-1]["event"] == "stopped"
    replacement = start_guard(processes, directory, log, delay=0)
    wait_until(lambda: sum(item["event"] == "ready" for item in events(log)) == 2)
    replacement.terminate()
    assert replacement.wait(timeout=3) == 0


def test_foreground_burst_keeps_worker_stopped_until_idle(tmp_path, processes):
    directory = tmp_path / "locks"
    log = tmp_path / "worker.jsonl"
    guard = start_guard(processes, directory, log, delay=0, idle_grace=0.2)
    wait_until(lambda: any(item["event"] == "ready" for item in events(log)))
    for _ in range(3):
        with foreground_slot(directory, timeout=3, poll_interval=0.01):
            assert [item["event"] for item in events(log)] == [
                "ready",
                "draining",
                "stopped",
            ]
            time.sleep(0.05)
        time.sleep(0.05)
    wait_until(lambda: sum(item["event"] == "ready" for item in events(log)) == 2)
    guard.terminate()
    assert guard.wait(timeout=3) == 0


def test_supervisor_error_during_drain_does_not_send_second_term(tmp_path, processes):
    directory = tmp_path / "locks"
    log = tmp_path / "worker.jsonl"
    source = """
import sys
from orchestrator.core.local_resources import supervise_worker
def event(message):
    if message.startswith('worker_draining'):
        raise RuntimeError('event callback failed')
supervise_worker(sys.argv[1], sys.argv[2:], poll_interval=0.01, on_event=event)
"""
    guard = processes(
        [
            sys.executable,
            "-c",
            source,
            str(directory),
            sys.executable,
            "-u",
            "-c",
            WORKER,
            str(log),
            "0.15",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    wait_until(lambda: any(item["event"] == "ready" for item in events(log)))
    with foreground_slot(directory, timeout=3, poll_interval=0.01):
        assert [item["event"] for item in events(log)] == [
            "ready",
            "draining",
            "stopped",
        ]
    assert guard.wait(timeout=3) != 0


@pytest.mark.parametrize("timeout", [-1, float("inf"), float("nan")])
def test_invalid_wait_budget_is_rejected(tmp_path, timeout):
    with pytest.raises(ValueError, match="timeout must be finite"):
        with foreground_slot(tmp_path, timeout=timeout):
            pass
