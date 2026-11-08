"""Coordinate foreground inference with a cooperating local background worker."""

from contextlib import contextmanager
import math
from pathlib import Path
import subprocess
import threading
import time


class LocalResourceBusy(RuntimeError):
    """The local resource was not available within the caller's wait budget."""


def _interval(value: float, name: str, *, allow_zero: bool = False) -> float:
    value = float(value)
    if not math.isfinite(value) or value < 0 or (not allow_zero and value == 0):
        raise ValueError(
            f"{name} must be finite and {'nonnegative' if allow_zero else 'positive'}"
        )
    return value


def _try_lock(file) -> bool:
    import fcntl

    try:
        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock(file) -> None:
    import fcntl

    fcntl.flock(file, fcntl.LOCK_UN)


@contextmanager
def foreground_slot(directory, *, timeout=60.0, poll_interval=0.1, on_wait=None):
    """Hold foreground intent until the protected inference has completed."""
    timeout = _interval(timeout, "timeout", allow_zero=True)
    poll_interval = _interval(poll_interval, "poll_interval")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    waiting = False
    with (directory / "intent.lock").open("a+b") as intent:
        with (directory / "resource.lock").open("a+b") as resource:
            for file in (intent, resource):
                while not _try_lock(file):
                    if not waiting:
                        waiting = True
                        if on_wait is not None:
                            on_wait()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise LocalResourceBusy(
                            f"Local inference resource remained busy for {timeout:g}s: {directory}"
                        )
                    time.sleep(min(poll_interval, remaining))
            yield


def supervise_worker(
    directory,
    command,
    *,
    poll_interval=0.1,
    drain_timeout=125.0,
    idle_grace=30.0,
    stop_event=None,
    on_event=None,
):
    """Run a foreground-aware worker; drain overruns keep the resource locked.

    The command must stay in the foreground and preserve inherited descriptors.
    The worker inherits the resource lock so a supervisor crash cannot release it.
    """
    poll_interval = _interval(poll_interval, "poll_interval")
    drain_timeout = _interval(drain_timeout, "drain_timeout", allow_zero=True)
    idle_grace = _interval(idle_grace, "idle_grace", allow_zero=True)
    if isinstance(command, (str, bytes)) or not command:
        raise ValueError("command must be a nonempty argv sequence")
    stop_event = stop_event if stop_event is not None else threading.Event()
    emit = on_event if on_event is not None else lambda _message: None
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    resume_after = 0.0
    with (directory / "intent.lock").open("a+b") as intent:
        with (directory / "resource.lock").open("a+b") as resource:
            while not stop_event.is_set():
                if not _try_lock(intent):
                    resume_after = time.monotonic() + idle_grace
                    stop_event.wait(poll_interval)
                    continue
                worker = None
                try:
                    if time.monotonic() >= resume_after and _try_lock(resource):
                        try:
                            worker = subprocess.Popen(
                                list(command), pass_fds=(resource.fileno(),)
                            )
                        except BaseException:
                            _unlock(resource)
                            raise
                finally:
                    _unlock(intent)
                if worker is None:
                    stop_event.wait(poll_interval)
                    continue

                preempted = False
                stopping_at = None
                overrun_reported = False
                try:
                    emit(f"worker_started pid={worker.pid}")
                    while worker.poll() is None:
                        foreground_waiting = not _try_lock(intent)
                        if not foreground_waiting:
                            _unlock(intent)
                        if stopping_at is None and (
                            stop_event.is_set() or foreground_waiting
                        ):
                            preempted = foreground_waiting
                            stopping_at = time.monotonic()
                            try:
                                worker.terminate()
                            except ProcessLookupError:
                                pass
                            emit(f"worker_draining pid={worker.pid}")
                        if (
                            stopping_at is not None
                            and not overrun_reported
                            and time.monotonic() - stopping_at >= drain_timeout
                        ):
                            overrun_reported = True
                            emit(
                                f"worker_drain_overrun pid={worker.pid}; resource remains locked"
                            )
                        time.sleep(poll_interval)
                finally:
                    if worker.poll() is None:
                        if stopping_at is None:
                            try:
                                worker.terminate()
                            except ProcessLookupError:
                                pass
                        # Never unlock an inherited flock while its worker is alive.
                        worker.wait()
                    _unlock(resource)
                emit(f"worker_stopped pid={worker.pid} returncode={worker.returncode}")
                if not preempted:
                    return worker.returncode
                resume_after = time.monotonic() + idle_grace
    return 0
