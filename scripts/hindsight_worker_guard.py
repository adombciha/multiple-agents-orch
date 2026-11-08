"""Launch a native Hindsight worker with cooperative local inference priority."""

import argparse
import importlib.util
from pathlib import Path
import signal
import threading

helper_path = (
    Path(__file__).resolve().parents[1] / "orchestrator/core/local_resources.py"
)
helper_spec = importlib.util.spec_from_file_location("local_resources", helper_path)
helper = importlib.util.module_from_spec(helper_spec)
helper_spec.loader.exec_module(helper)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resource-dir", required=True)
    parser.add_argument("--poll-interval", type=float, default=0.1)
    parser.add_argument("--drain-timeout", type=float, default=125.0)
    parser.add_argument("--idle-grace", type=float, default=30.0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("a foreground native worker command is required after --")
    stop_event = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda _signum, _frame: stop_event.set())
    return helper.supervise_worker(
        args.resource_dir,
        command,
        poll_interval=args.poll_interval,
        drain_timeout=args.drain_timeout,
        idle_grace=args.idle_grace,
        stop_event=stop_event,
        on_event=lambda message: print(message, flush=True),
    )


if __name__ == "__main__":
    raise SystemExit(main())
