"""Atomic single-file publication with durable rename semantics."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Mapping

import yaml


def atomic_write_bytes(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    """Publish ``content`` atomically and fsync both file and parent directory.

    Existing file permissions are retained. Symlink targets are rejected so a
    caller cannot accidentally publish outside the path it validated.
    """

    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    existing_mode = _existing_mode(destination)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent)
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, existing_mode if existing_mode is not None else mode)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        _existing_mode(destination)  # Recheck immediately before publication.
        os.replace(temporary, destination)
        fsync_directory(destination.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def atomic_write_text(
    path: Path, content: str, *, encoding: str = "utf-8", mode: int = 0o600
) -> None:
    atomic_write_bytes(path, content.encode(encoding), mode=mode)


def atomic_write_json(
    path: Path,
    payload: Any,
    *,
    indent: int | None = 2,
    ensure_ascii: bool = False,
    trailing_newline: bool = False,
    mode: int = 0o600,
) -> None:
    content = json.dumps(payload, indent=indent, ensure_ascii=ensure_ascii)
    if trailing_newline:
        content += "\n"
    atomic_write_text(path, content, mode=mode)


def atomic_write_yaml(
    path: Path,
    payload: Mapping[str, Any],
    *,
    sort_keys: bool = False,
    width: int = 1000,
    mode: int = 0o600,
) -> None:
    atomic_write_text(
        path,
        yaml.safe_dump(dict(payload), sort_keys=sort_keys, width=width),
        mode=mode,
    )


def _existing_mode(path: Path) -> int | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(metadata.st_mode):
        raise OSError(f"refusing atomic write through symlink: {path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise OSError(f"atomic write target is not a regular file: {path}")
    return stat.S_IMODE(metadata.st_mode)


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
