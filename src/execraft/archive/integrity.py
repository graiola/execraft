"""Atomic file operations and integrity primitives for task archives."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml

from execraft.persistence import atomic_write_text, sha256_file
from execraft.workspace.task_git import TaskGitError


def _copy_tree_without_symlinks(
    source: Path,
    destination: Path,
    *,
    ignored_names: set[str] | None = None,
    ignored_suffixes: set[str] | None = None,
) -> None:
    ignored_names = ignored_names or set()
    ignored_suffixes = ignored_suffixes or set()
    destination.mkdir(parents=True, exist_ok=False)
    for item in sorted(source.rglob("*")):
        relative = item.relative_to(source)
        if item.name in ignored_names or any(item.name.endswith(suffix) for suffix in ignored_suffixes):
            continue
        if item.is_symlink():
            raise TaskGitError(f"archive source contains a symbolic link: {item}")
        target = destination / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)



def _protect_tree(root: Path) -> None:
    """Keep completion evidence private while retaining owner maintenance access."""

    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink():
            raise TaskGitError(f"archive tree contains a symbolic link: {path}")
        path.chmod(0o700 if path.is_dir() else 0o600)
    root.chmod(0o700)

def _tree_digests(root: Path, *, excluded: set[str] | None = None) -> dict[str, str]:
    excluded = excluded or set()
    result: dict[str, str] = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        if relative in excluded:
            continue
        result[relative] = sha256_file(path)
    return result


def _write_sha256sums(root: Path) -> None:
    lines: list[str] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        if relative == "SHA256SUMS":
            continue
        lines.append(f"{sha256_file(path)}  {relative}")
    atomic_write_text(root / "SHA256SUMS", "\n".join(lines) + "\n")


def _safe_archive_member(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise TaskGitError(f"archive checksum path escapes archive root: {relative!r}") from exc
    return candidate



def _write_json(path: Path, data: Any) -> None:
    atomic_write_text(
        path,
        json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False) + "\n",
    )


def _write_yaml(path: Path, data: Mapping[str, Any]) -> None:
    atomic_write_text(path, yaml.safe_dump(dict(data), sort_keys=False, width=1000))


def _archive_id(timestamp: str) -> str:
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
