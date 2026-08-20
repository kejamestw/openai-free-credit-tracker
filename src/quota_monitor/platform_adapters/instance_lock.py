"""Cross-platform file-based single-instance lock with stale recovery."""

from __future__ import annotations

import errno
import json
import os
import secrets
import stat
import time
from collections.abc import Callable
from pathlib import Path


def process_is_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        if exc.errno == errno.ESRCH or getattr(exc, "winerror", None) == 87:
            return False
        return True
    return True


class FileInstanceLock:
    """An owner-token lock that can recover files left by dead processes."""

    available = True

    def __init__(
        self,
        path: Path,
        *,
        pid: int | None = None,
        clock: Callable[[], float] | None = None,
        process_alive: Callable[[int], bool] | None = None,
        invalid_lock_grace_seconds: float = 10.0,
    ) -> None:
        lock_path = Path(path)
        if not lock_path.is_absolute():
            raise ValueError("lock path must be absolute")
        if invalid_lock_grace_seconds < 0:
            raise ValueError("invalid_lock_grace_seconds cannot be negative")
        self._path = lock_path
        self._pid = os.getpid() if pid is None else pid
        if self._pid <= 0:
            raise ValueError("pid must be positive")
        self._clock = clock or time.time
        self._process_alive = process_alive or process_is_alive
        self._invalid_lock_grace_seconds = invalid_lock_grace_seconds
        self._owner_token: str | None = None
        self._stale_recovered = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def acquired(self) -> bool:
        return self._owner_token is not None

    @property
    def stale_recovered(self) -> bool:
        return self._stale_recovered

    @staticmethod
    def _signature(metadata: os.stat_result) -> tuple[int, int, int, int]:
        return (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mtime_ns,
            metadata.st_size,
        )

    def _read_existing(self) -> tuple[dict[str, object] | None, os.stat_result | None]:
        try:
            metadata = self._path.lstat()
        except FileNotFoundError:
            return None, None
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            return None, metadata
        try:
            raw = self._path.read_text(encoding="utf-8")
            if len(raw) > 4096:
                return None, metadata
            payload = json.loads(raw)
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None, metadata
        if not isinstance(payload, dict):
            return None, metadata
        return payload, metadata

    def _is_stale(
        self,
        payload: dict[str, object] | None,
        metadata: os.stat_result,
    ) -> bool:
        if payload is not None:
            pid = payload.get("pid")
            token = payload.get("owner_token")
            created_at = payload.get("created_at")
            if (
                isinstance(pid, int)
                and not isinstance(pid, bool)
                and pid > 0
                and isinstance(token, str)
                and len(token) == 32
                and isinstance(created_at, (int, float))
                and not isinstance(created_at, bool)
            ):
                return not self._process_alive(pid)
        age = max(0.0, self._clock() - metadata.st_mtime)
        return age >= self._invalid_lock_grace_seconds

    def _remove_if_unchanged(self, metadata: os.stat_result) -> bool:
        try:
            current = self._path.lstat()
        except FileNotFoundError:
            return True
        if stat.S_ISLNK(current.st_mode) or self._signature(current) != self._signature(metadata):
            return False
        try:
            self._path.unlink()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return True

    def _create(self) -> bool:
        token = secrets.token_hex(16)
        payload = json.dumps(
            {
                "pid": self._pid,
                "created_at": self._clock(),
                "owner_token": token,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            descriptor = os.open(self._path, flags, 0o600)
        except FileExistsError:
            return False
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            try:
                self._path.unlink()
            except OSError:
                pass
            raise
        self._owner_token = token
        return True

    def acquire(self) -> bool:
        if self.acquired:
            return True
        if not self._path.parent.is_dir():
            raise FileNotFoundError("lock directory does not exist")
        self._stale_recovered = False
        for _attempt in range(3):
            if self._create():
                return True
            payload, metadata = self._read_existing()
            if metadata is None:
                continue
            if not self._is_stale(payload, metadata):
                return False
            if not self._remove_if_unchanged(metadata):
                return False
            self._stale_recovered = True
        return False

    def release(self) -> None:
        token = self._owner_token
        self._owner_token = None
        if token is None:
            return
        payload, metadata = self._read_existing()
        if metadata is None or payload is None or payload.get("owner_token") != token:
            return
        self._remove_if_unchanged(metadata)

    def __enter__(self) -> "FileInstanceLock":
        if not self.acquire():
            raise RuntimeError("another instance owns the lock")
        return self

    def __exit__(self, *_: object) -> None:
        self.release()
