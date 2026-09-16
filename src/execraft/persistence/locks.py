"""File locks with timeouts, ownership validation, and lock-order checks."""

from __future__ import annotations

import os
import stat
import threading
import time
from enum import IntEnum
from pathlib import Path
from typing import IO

try:  # POSIX is required for inter-process correctness.
    import fcntl
except ImportError:  # pragma: no cover - exercised by monkeypatch tests
    fcntl = None  # type: ignore[assignment]


class LockBusyError(TimeoutError):
    """Raised when a lock cannot be acquired before its deadline."""


class LockHierarchyError(RuntimeError):
    """Raised before an acquisition that could invert the global lock order."""


class LockOwnershipError(RuntimeError):
    """Raised for unsafe lock files or release by a non-owning thread."""


class LockUnavailableError(RuntimeError):
    """Raised when the platform cannot provide process-safe file locking."""


class LockLevel(IntEnum):
    # Project Execution is the outermost execution owner.  It may safely invoke
    # a Task action that acquires DRIVER without same-level recursion.
    PROJECT_EXECUTOR = 5
    DRIVER = 10
    ORCHESTRATOR = 20
    LIFECYCLE = 30
    REPOSITORY_REF = 40
    # Cross-domain project-definition coordination may invoke independent
    # durable RECORD repositories while keeping their domain locks separate.
    PROJECT_COORDINATOR = 45
    RECORD = 50


_state = threading.local()
_registry_guard = threading.Lock()
_thread_locks: dict[Path, threading.Lock] = {}


def _held() -> list[tuple[Path, LockLevel]]:
    locks = getattr(_state, "locks", None)
    if locks is None:
        locks = []
        _state.locks = locks
    return locks


def _thread_lock(path: Path) -> threading.Lock:
    with _registry_guard:
        return _thread_locks.setdefault(path, threading.Lock())


class FileLock:
    """Exclusive/shared advisory lock acquired in global outer-to-inner order."""

    def __init__(
        self,
        path: Path,
        *,
        level: LockLevel,
        timeout: float | None = None,
        exclusive: bool = True,
        poll_interval: float = 0.05,
    ) -> None:
        if timeout is not None and timeout < 0:
            raise ValueError("lock timeout cannot be negative")
        if poll_interval <= 0:
            raise ValueError("lock poll interval must be positive")
        self.path = Path(path).expanduser().absolute()
        self.level = LockLevel(level)
        self.timeout = timeout
        self.exclusive = exclusive
        self.poll_interval = poll_interval
        self._handle: IO[str] | None = None
        self._owner_thread: int | None = None
        self._local_lock: threading.Lock | None = None

    def acquire(self) -> "FileLock":
        if self._owner_thread is not None:
            raise LockOwnershipError(f"lock instance is already held: {self.path}")
        if fcntl is None:
            raise LockUnavailableError("process-safe file locking is unavailable")
        held = _held()
        if (self.path, self.level) in held:
            raise LockBusyError(f"lock is already held by the current thread: {self.path}")
        if held and self.level <= held[-1][1]:
            raise LockHierarchyError(
                f"lock order violation: {self.level.name} after {held[-1][1].name}"
            )
        deadline = None if self.timeout is None else time.monotonic() + self.timeout
        local_lock = _thread_lock(self.path)
        if not _acquire_thread_lock(local_lock, deadline):
            raise LockBusyError(f"timed out acquiring lock: {self.path}")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._validate_existing_lock_file()
            flags = os.O_RDWR | os.O_CREAT
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(self.path, flags, 0o600)
            handle = os.fdopen(descriptor, "a+", encoding="utf-8")
            try:
                self._validate_open_lock_file(handle)
                operation = fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH
                while True:
                    try:
                        fcntl.flock(handle.fileno(), operation | fcntl.LOCK_NB)
                        break
                    except BlockingIOError as exc:
                        if deadline is not None and time.monotonic() >= deadline:
                            raise LockBusyError(
                                f"timed out acquiring lock: {self.path}"
                            ) from exc
                        time.sleep(_remaining_sleep(deadline, self.poll_interval))
            except Exception:
                handle.close()
                raise
        except Exception:
            local_lock.release()
            raise
        self._handle = handle
        self._local_lock = local_lock
        self._owner_thread = threading.get_ident()
        held.append((self.path, self.level))
        return self

    def release(self) -> None:
        if self._owner_thread != threading.get_ident() or self._handle is None:
            raise LockOwnershipError(f"current thread does not own lock: {self.path}")
        held = _held()
        if not held or held[-1] != (self.path, self.level):
            raise LockHierarchyError(f"locks must be released in reverse order: {self.path}")
        held.pop()
        handle = self._handle
        local_lock = self._local_lock
        self._handle = None
        self._local_lock = None
        self._owner_thread = None
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[union-attr]
        finally:
            handle.close()
            assert local_lock is not None
            local_lock.release()

    def _validate_existing_lock_file(self) -> None:
        try:
            metadata = self.path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise LockOwnershipError(f"lock path is not a regular file: {self.path}")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise LockOwnershipError(f"lock file is owned by another user: {self.path}")

    def _validate_open_lock_file(self, handle: IO[str]) -> None:
        opened = os.fstat(handle.fileno())
        current = self.path.stat()
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise LockOwnershipError(f"lock file changed while opening: {self.path}")
        if not stat.S_ISREG(opened.st_mode):
            raise LockOwnershipError(f"lock path is not a regular file: {self.path}")
        if hasattr(os, "getuid") and opened.st_uid != os.getuid():
            raise LockOwnershipError(f"lock file is owned by another user: {self.path}")

    def __enter__(self) -> "FileLock":
        return self.acquire()

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.release()



def file_lock_is_held(path: Path) -> bool:
    """Return whether an existing lock file is held by another lock owner.

    The probe never creates a missing lock file and releases any lock it acquires
    immediately. It centralizes the platform/safety behavior used by read-only
    status checks without introducing a second lock implementation.
    """

    if fcntl is None:
        raise LockUnavailableError("process-safe file locking is unavailable")
    candidate = Path(path).expanduser().absolute()
    try:
        metadata = candidate.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise LockOwnershipError(f"lock path is not a regular file: {candidate}")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise LockOwnershipError(f"lock file is owned by another user: {candidate}")

    flags = os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(candidate, flags)
    handle = os.fdopen(descriptor, "a+", encoding="utf-8")
    try:
        opened = os.fstat(handle.fileno())
        current = candidate.stat()
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise LockOwnershipError(f"lock file changed while opening: {candidate}")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
    finally:
        handle.close()

def try_file_lock(
    path: Path, *, level: LockLevel, exclusive: bool = True
) -> FileLock:
    """Acquire immediately or raise :class:`LockBusyError`."""

    return FileLock(path, level=level, timeout=0.0, exclusive=exclusive).acquire()


def _acquire_thread_lock(lock: threading.Lock, deadline: float | None) -> bool:
    if deadline is None:
        lock.acquire()
        return True
    return lock.acquire(timeout=max(0.0, deadline - time.monotonic()))


def _remaining_sleep(deadline: float | None, interval: float) -> float:
    if deadline is None:
        return interval
    return max(0.0, min(interval, deadline - time.monotonic()))
