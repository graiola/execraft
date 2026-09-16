"""Collision-safe host-local storage identity for orchestration tasks.

Historically Execraft keyed state only by ``task_id``. That is convenient and is
preserved for the first owner of a legacy path, but two registered projects may
legitimately reuse the same task ID. A tiny owner marker lets the first project
keep the compatible location while any conflicting project is routed to a
stable namespaced key.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import uuid

from execraft.persistence import fsync_directory


_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9._-]+")
_OWNER_FILE = ".execraft-project-id"


def _component(value: str) -> str:
    text = _SAFE_COMPONENT.sub("-", str(value).strip()).strip("-.") or "unnamed"
    return text[:80]


def namespaced_storage_key(project_id: str, task_id: str) -> str:
    """Return a stable bounded key for a project/task pair."""

    project = _component(project_id)
    task = _component(task_id)
    digest = hashlib.sha256(f"{project_id}\0{task_id}".encode("utf-8")).hexdigest()[:10]
    return f"{project}--{task}--{digest}"


@dataclass(frozen=True)
class OrchestrationStorageIdentity:
    project_id: str
    task_id: str
    storage_key: str
    state_dir: Path
    journal_path: Path
    legacy_compatible: bool


def resolve_storage_identity(
    state_root: Path,
    *,
    project_id: str,
    task_id: str,
    create: bool = True,
) -> OrchestrationStorageIdentity:
    """Resolve one collision-safe state and journal location.

    With no project ID, the historical task-only path is returned. With a
    project ID, the task-only path is claimed by an atomic owner marker when it
    is unowned. If another project already owns that path, a deterministic
    namespaced location is selected instead.
    """

    root = Path(state_root).expanduser().resolve()
    task = str(task_id).strip()
    project = str(project_id).strip()
    if not task:
        raise ValueError("task_id cannot be empty")

    if not project:
        key = task
        state_dir = root / "projects" / key
        if create:
            state_dir.mkdir(parents=True, exist_ok=True)
        return OrchestrationStorageIdentity(
            project_id="",
            task_id=task,
            storage_key=key,
            state_dir=state_dir,
            journal_path=root / "journals" / f"{key}.json",
            legacy_compatible=True,
        )

    legacy_dir = root / "projects" / task
    marker = legacy_dir / _OWNER_FILE
    if create:
        legacy_dir.mkdir(parents=True, exist_ok=True)
    owner = _read_owner(marker)
    if owner is None and create:
        owner = _claim_owner(marker, project)

    if owner in {None, project}:
        key = task
        state_dir = legacy_dir
        legacy = True
    else:
        key = namespaced_storage_key(project, task)
        state_dir = root / "projects" / key
        legacy = False
        if create:
            state_dir.mkdir(parents=True, exist_ok=True)
            _claim_owner(state_dir / _OWNER_FILE, project)

    return OrchestrationStorageIdentity(
        project_id=project,
        task_id=task,
        storage_key=key,
        state_dir=state_dir,
        journal_path=root / "journals" / f"{key}.json",
        legacy_compatible=legacy,
    )


def _read_owner(path: Path) -> str | None:
    try:
        owner = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    if not owner:
        raise ValueError(f"corrupt empty orchestration owner marker: {path}")
    return owner


def _claim_owner(path: Path, project_id: str) -> str:
    """Atomically publish a fully written claim and return the winning owner."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            os.write(descriptor, (project_id + "\n").encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            # link(2) is an atomic create-if-absent operation. Unlike opening
            # the final marker with O_EXCL, readers can never observe a winning
            # marker before its owner text has been fully written and synced.
            os.link(temporary, path)
        except FileExistsError:
            owner = _read_owner(path)
            assert owner is not None
            return owner
        try:
            fsync_directory(path.parent)
        except OSError:  # pragma: no cover - unusual filesystems
            pass
        return project_id
    finally:
        temporary.unlink(missing_ok=True)
