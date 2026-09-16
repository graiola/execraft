"""Contract tests for shared atomic persistence and lock hierarchy."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest
import yaml

from execraft.persistence import (
    FileLock,
    LockBusyError,
    LockHierarchyError,
    LockLevel,
    LockOwnershipError,
    LockUnavailableError,
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
    atomic_write_yaml,
    try_file_lock,
)
from execraft.persistence import atomic as atomic_module
from execraft.persistence import locks as locks_module


def test_atomic_writers_publish_content_and_preserve_permissions(tmp_path: Path) -> None:
    text_path = tmp_path / "state.txt"
    text_path.write_text("old", encoding="utf-8")
    text_path.chmod(0o640)

    atomic_write_text(text_path, "new")
    atomic_write_bytes(tmp_path / "state.bin", b"bytes")
    atomic_write_json(tmp_path / "state.json", {"ready": True})
    atomic_write_yaml(tmp_path / "state.yaml", {"phase": "complete"})

    assert text_path.read_text(encoding="utf-8") == "new"
    assert text_path.stat().st_mode & 0o777 == 0o640
    assert (tmp_path / "state.bin").read_bytes() == b"bytes"
    assert json.loads((tmp_path / "state.json").read_text()) == {"ready": True}
    assert yaml.safe_load((tmp_path / "state.yaml").read_text()) == {
        "phase": "complete"
    }


def test_atomic_write_preserves_previous_on_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.json"
    path.write_text("previous", encoding="utf-8")

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("simulated crash boundary")

    monkeypatch.setattr(atomic_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated crash"):
        atomic_write_text(path, "next")

    assert path.read_text(encoding="utf-8") == "previous"
    assert not list(tmp_path.glob(".state.json.*.tmp"))


def test_atomic_write_rejects_symlink_destination(tmp_path: Path) -> None:
    owned = tmp_path / "owned"
    owned.write_text("safe", encoding="utf-8")
    link = tmp_path / "state"
    link.symlink_to(owned)

    with pytest.raises(OSError, match="symlink"):
        atomic_write_text(link, "unsafe")
    assert owned.read_text(encoding="utf-8") == "safe"


def test_lock_timeout_does_not_transfer_ownership(tmp_path: Path) -> None:
    path = tmp_path / "state.lock"
    entered = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with FileLock(path, level=LockLevel.RECORD):
            entered.set()
            release.wait(timeout=5)

    thread = threading.Thread(target=holder)
    thread.start()
    assert entered.wait(timeout=5)
    try:
        with pytest.raises(LockBusyError, match="timed out"):
            try_file_lock(path, level=LockLevel.RECORD)
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive()


def test_lock_hierarchy_allows_forward_order_and_rejects_inversion(
    tmp_path: Path,
) -> None:
    with FileLock(tmp_path / "outer.lock", level=LockLevel.ORCHESTRATOR):
        with FileLock(tmp_path / "inner.lock", level=LockLevel.RECORD):
            pass
        with pytest.raises(LockHierarchyError, match="order violation"):
            FileLock(tmp_path / "driver.lock", level=LockLevel.DRIVER).acquire()


def test_lock_release_is_owned_by_acquiring_thread(tmp_path: Path) -> None:
    lock = FileLock(tmp_path / "owned.lock", level=LockLevel.RECORD).acquire()
    errors: list[Exception] = []
    thread = threading.Thread(
        target=lambda: _capture_release_error(lock, errors),
    )
    thread.start()
    thread.join(timeout=5)
    assert isinstance(errors[0], LockOwnershipError)
    lock.release()


def _capture_release_error(lock: FileLock, errors: list[Exception]) -> None:
    try:
        lock.release()
    except Exception as exc:
        errors.append(exc)


def test_locking_fails_closed_without_fcntl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(locks_module, "fcntl", None)
    with pytest.raises(LockUnavailableError, match="unavailable"):
        FileLock(tmp_path / "state.lock", level=LockLevel.RECORD).acquire()


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX ownership semantics")
def test_lock_rejects_foreign_owner_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "foreign.lock"
    path.touch()
    real_lstat = Path.lstat

    def foreign_lstat(candidate: Path):
        metadata = real_lstat(candidate)
        values = list(metadata)
        values[4] = os.getuid() + 1
        return os.stat_result(values)

    monkeypatch.setattr(Path, "lstat", foreign_lstat)
    with pytest.raises(LockOwnershipError, match="another user"):
        FileLock(path, level=LockLevel.RECORD).acquire()


def test_project_coordinator_lock_may_nest_record_but_not_reverse(tmp_path: Path) -> None:
    """Cross-domain coordination is outside independent durable records."""

    from execraft.persistence.locks import FileLock, LockHierarchyError, LockLevel

    with FileLock(tmp_path / "coordinator.lock", level=LockLevel.PROJECT_COORDINATOR):
        with FileLock(tmp_path / "record.lock", level=LockLevel.RECORD):
            pass

    with FileLock(tmp_path / "record-outer.lock", level=LockLevel.RECORD):
        with pytest.raises(LockHierarchyError):
            FileLock(
                tmp_path / "coordinator-inner.lock",
                level=LockLevel.PROJECT_COORDINATOR,
            ).acquire()
