from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on native Windows
    fcntl = None


class RunLockBusy(RuntimeError):

    def __init__(self, holder_info: dict):
        self.holder_info = holder_info
        super().__init__("Another orchestrator command is already running.")


class RunLock:

    def __init__(self, ai_dir: Path, command: str, lock_name: str = "run.lock"):
        self.path = ai_dir / lock_name
        self.command = command
        self._file = None
        self._fd = None
        self._fallback = fcntl is None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.release()

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        holder = {
            "pid": os.getpid(),
            "command": self.command,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        if not self._fallback:
            self._file = self.path.open("a+", encoding="utf-8")
            try:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                self._file.seek(0)
                current = self._read_holder(self._file.read())
                self._file.close()
                self._file = None
                raise RunLockBusy(current or {"command": "unknown"}) from error
            self._file.seek(0)
            self._file.truncate()
            self._file.write(json.dumps(holder))
            self._file.flush()
            os.fsync(self._file.fileno())
            return

        self._acquire_exclusive(holder)

    def _acquire_exclusive(self, holder: dict):
        for attempt in range(2):
            try:
                self._fd = os.open(
                    self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                )
                os.write(self._fd, json.dumps(holder).encode("utf-8"))
                os.fsync(self._fd)
                return
            except FileExistsError as error:
                current = self.read_holder(self.path.parent)
                if current is not None or attempt:
                    raise RunLockBusy(current or {"command": "unknown"}) from error
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass

    def release(self):
        if self._file is not None:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
            self._file = None
        if self._fd is not None:
            try:
                self.path.unlink(missing_ok=True)
            finally:
                os.close(self._fd)
                self._fd = None

    @staticmethod
    def _read_holder(content: str) -> dict | None:
        try:
            value = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _pid_is_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return True
        return True

    @classmethod
    def read_holder(cls, ai_dir: Path) -> dict | None:
        path = ai_dir / "run.lock"
        if not path.exists():
            return None
        if fcntl is not None:
            try:
                handle = path.open("a+", encoding="utf-8")
            except OSError:
                return {"command": "unknown"}
            try:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    handle.seek(0)
                    return cls._read_holder(handle.read()) or {"command": "unknown"}
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    return None
            finally:
                handle.close()

        try:
            holder = cls._read_holder(path.read_text(encoding="utf-8"))
        except OSError:
            return {"command": "unknown"}
        if holder is None:
            return {"command": "unknown"}
        pid = holder.get("pid")
        return holder if not isinstance(pid, int) or cls._pid_is_alive(pid) else None
