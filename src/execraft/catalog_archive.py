"""Reversible archive for inactive project and task catalog entries.

This module deliberately complements, rather than replaces, the immutable
completion archives in :mod:`execraft.archive`.  Completion archives are evidence
bundles created after strict orchestration preflight.  Catalog archives are a
lightweight usability feature: they retire inactive dossiers from the normal
project/task selectors while preserving their files for inspection and safe
reactivation.
"""

from __future__ import annotations

import hashlib
import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator, Mapping

import yaml

from execraft.control_plane import xdg_state_home
from execraft.persistence.atomic import atomic_write_yaml
from execraft.orchestrate.identity import resolve_storage_identity
from execraft.project import (
    ProjectRegistration,
    list_project_catalog,
    load_project_registration,
    project_directory,
    register_project_descriptor,
    remove_project_registration,
)

try:  # pragma: no cover - Windows uses the process-local lock below.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


_ARCHIVE_SCHEMA_VERSION = 1
_METADATA_NAME = ".archive.yaml"
_HISTORY_DIRECTORY = ".execraft-archive-history"
_PREVIEW_NAMES = (
    "TASK.yaml",
    "DEFINITION.yaml",
    "project.yaml",
    "BRIEF.md",
    "PLAN.md",
    "PLAN.graph.yaml",
    "HANDOFF.md",
    "REVIEW.md",
    "COMPLETION.yaml",
    "README.md",
)


class CatalogArchiveError(RuntimeError):
    """Raised when a catalog archive operation is invalid or unsafe."""


@dataclass(frozen=True)
class CatalogArchiveEntry:
    """One active or archived project/task descriptor."""

    kind: str
    item_id: str
    project_id: str
    title: str
    status: str
    archived: bool
    archived_at: str = ""
    reason: str = ""
    file_count: int = 0
    total_bytes: int = 0
    task_count: int = 0

    def as_mapping(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.item_id,
            "project_id": self.project_id,
            "title": self.title,
            "status": self.status,
            "archived": self.archived,
            "archived_at": self.archived_at,
            "reason": self.reason,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "task_count": self.task_count,
        }


class CatalogArchiveManager:
    """Move inactive catalog entries into a reversible archive.

    The archive is stored below ``projects/.archive`` so active project
    discovery remains unchanged.  Every archived directory contains a bounded
    metadata document with the original location and SHA-256 digests for all
    regular files.  Reactivation verifies that inventory before moving the
    directory back into the active catalog.
    """

    _thread_lock = threading.RLock()

    def __init__(self, root: Path, state_root: Path | None = None) -> None:
        self.root = root.expanduser().resolve()
        self.projects_root = self.root / "projects"
        self.archive_root = self.projects_root / ".archive"
        self.archived_tasks_root = self.archive_root / "tasks"
        self.archived_projects_root = self.archive_root / "projects"
        self.state_root = (
            state_root.expanduser().resolve()
            if state_root is not None
            else xdg_state_home()
        )
        self.lock_path = self.archive_root / ".catalog.lock"

    def catalog(self, *, current_project: str = "", current_task: str = "") -> dict[str, Any]:
        """Return active and archived catalog rows for the dashboard."""

        active_projects = self._active_projects(current_project=current_project)
        active_tasks: list[CatalogArchiveEntry] = []
        for project in active_projects:
            active_tasks.extend(
                self._active_tasks(
                    project.project_id,
                    current_task=current_task if project.project_id == current_project else "",
                )
            )
        archived_projects = self._archived_entries("project")
        archived_tasks = self._archived_entries("task")
        return {
            "active_projects": [entry.as_mapping() for entry in active_projects],
            "active_tasks": [entry.as_mapping() for entry in active_tasks],
            "archived_projects": [entry.as_mapping() for entry in archived_projects],
            "archived_tasks": [entry.as_mapping() for entry in archived_tasks],
            "archive_root": str(self.archive_root),
        }

    def archive_task(self, project_id: str, task_id: str, *, reason: str = "") -> dict[str, Any]:
        """Retire one active task dossier from the normal project catalog."""

        project_id = _safe_identifier(project_id, label="project id")
        task_id = _safe_identifier(task_id, label="task id")
        source = project_directory(self.root, project_id) / "tasks" / task_id
        target = self.archived_tasks_root / project_id / task_id
        with self._exclusive_lock():
            if not source.is_dir() or source.is_symlink():
                raise CatalogArchiveError(f"active task dossier not found: {source}")
            if not (source / "TASK.yaml").is_file():
                raise CatalogArchiveError(f"task dossier has no TASK.yaml: {source}")
            if target.exists():
                raise CatalogArchiveError(
                    f"archived task already exists: {project_id}/{task_id}"
                )
            manifest = _read_yaml_mapping(source / "TASK.yaml")
            manifest_id = str(manifest.get("id", "")).strip()
            if manifest_id and manifest_id != task_id:
                raise CatalogArchiveError(
                    f"TASK.yaml id {manifest_id!r} does not match directory {task_id!r}"
                )
            metadata = self._metadata(
                kind="task",
                item_id=task_id,
                project_id=project_id,
                source=source,
                reason=reason,
            )
            self._move_into_archive(source, target, metadata)
            return self.inspect("task", project_id=project_id, item_id=task_id)

    def archive_project(self, project_id: str, *, reason: str = "") -> dict[str, Any]:
        """Retire one project descriptor and all of its active task dossiers."""

        project_id = _safe_identifier(project_id, label="project id")
        source = project_directory(self.root, project_id)
        target = self.archived_projects_root / project_id
        with self._exclusive_lock():
            try:
                source.relative_to(self.projects_root.resolve())
            except ValueError as exc:
                raise CatalogArchiveError(
                    "registered external projects cannot be archived as a whole; "
                    "archive their tasks individually or relocate the descriptor "
                    "under the control-plane home"
                ) from exc
            if (
                not source.is_dir()
                or source.is_symlink()
                or not (source / "project.yaml").is_file()
            ):
                raise CatalogArchiveError(f"active project not found: {source}")
            if target.exists():
                raise CatalogArchiveError(f"archived project already exists: {project_id}")
            registration = load_project_registration(project_id)
            metadata = self._metadata(
                kind="project",
                item_id=project_id,
                project_id=project_id,
                source=source,
                reason=reason,
                registration=registration,
            )
            self._move_into_archive(source, target, metadata)
            if registration is not None:
                try:
                    remove_project_registration(project_id)
                except Exception:
                    # A project must never remain registered to the descriptor
                    # that has just moved into the archive.  Roll the directory
                    # move back before surfacing the configuration failure.
                    target.rename(source)
                    (source / _METADATA_NAME).unlink(missing_ok=True)
                    raise
            return self.inspect("project", project_id=project_id, item_id=project_id)

    def reactivate_task(self, project_id: str, task_id: str) -> dict[str, Any]:
        """Verify and restore one archived task dossier to its project."""

        project_id = _safe_identifier(project_id, label="project id")
        task_id = _safe_identifier(task_id, label="task id")
        source = self.archived_tasks_root / project_id / task_id
        destination = project_directory(self.root, project_id) / "tasks" / task_id
        with self._exclusive_lock():
            project = project_directory(self.root, project_id)
            if not (project / "project.yaml").is_file():
                raise CatalogArchiveError(
                    f"reactivate project {project_id!r} before restoring its tasks"
                )
            self._reactivate(source, destination, expected_kind="task")
            return self._active_task_entry(project_id, task_id).as_mapping()

    def reactivate_project(self, project_id: str) -> dict[str, Any]:
        """Verify and restore one archived project descriptor."""

        project_id = _safe_identifier(project_id, label="project id")
        source = self.archived_projects_root / project_id
        destination = self.projects_root / project_id
        with self._exclusive_lock():
            self._reactivate(
                source,
                destination,
                expected_kind="project",
                after_move=self._restore_project_registration,
            )
            return self._active_project_entry(project_id).as_mapping()

    def inspect(self, kind: str, *, project_id: str, item_id: str) -> dict[str, Any]:
        """Return bounded metadata and human-readable previews for an archive."""

        kind = _safe_kind(kind)
        project_id = _safe_identifier(project_id, label="project id")
        item_id = _safe_identifier(item_id, label=f"{kind} id")
        path = self._archive_path(kind, project_id=project_id, item_id=item_id)
        metadata = self._load_metadata(path, expected_kind=kind)
        verification = self.verify(kind, project_id=project_id, item_id=item_id)
        previews: list[dict[str, Any]] = []
        preview_budget = 160_000
        for name in _PREVIEW_NAMES:
            candidate = path / name
            if not candidate.is_file() or candidate.is_symlink() or preview_budget <= 0:
                continue
            data = candidate.read_bytes()[: min(48_000, preview_budget)]
            previews.append(
                {
                    "name": name,
                    "content": data.decode("utf-8", errors="replace"),
                    "truncated": candidate.stat().st_size > len(data),
                }
            )
            preview_budget -= len(data)
        if kind == "project":
            # Surface task manifests without dumping every project file.
            tasks_root = path / "tasks"
            for task_dir in sorted(tasks_root.iterdir()) if tasks_root.is_dir() else []:
                candidate = task_dir / "TASK.yaml"
                if not candidate.is_file() or preview_budget <= 0:
                    continue
                data = candidate.read_bytes()[: min(24_000, preview_budget)]
                previews.append(
                    {
                        "name": f"tasks/{task_dir.name}/TASK.yaml",
                        "content": data.decode("utf-8", errors="replace"),
                        "truncated": candidate.stat().st_size > len(data),
                    }
                )
                preview_budget -= len(data)
        return {
            "entry": self._entry_from_archive(path, metadata).as_mapping(),
            "metadata": metadata,
            "verification": verification,
            "previews": previews,
            "state": self._state_summary(project_id, item_id) if kind == "task" else {},
            "path": str(path),
        }

    def verify(self, kind: str, *, project_id: str, item_id: str) -> dict[str, Any]:
        """Verify the file inventory stored with an archived item."""

        kind = _safe_kind(kind)
        project_id = _safe_identifier(project_id, label="project id")
        item_id = _safe_identifier(item_id, label=f"{kind} id")
        path = self._archive_path(kind, project_id=project_id, item_id=item_id)
        metadata = self._load_metadata(path, expected_kind=kind)
        expected = {
            str(item.get("path", "")): item
            for item in metadata.get("files", [])
            if isinstance(item, Mapping)
        }
        actual = {item["path"]: item for item in _file_inventory(path, exclude_metadata=True)}
        missing = sorted(set(expected) - set(actual))
        unexpected = sorted(set(actual) - set(expected))
        changed = sorted(
            relative
            for relative in set(expected) & set(actual)
            if str(expected[relative].get("sha256", "")) != actual[relative]["sha256"]
            or int(expected[relative].get("size", -1)) != actual[relative]["size"]
        )
        return {
            "ok": not (missing or unexpected or changed),
            "missing": missing,
            "unexpected": unexpected,
            "changed": changed,
            "file_count": len(actual),
            "total_bytes": sum(int(item["size"]) for item in actual.values()),
        }

    def _move_into_archive(
        self, source: Path, target: Path, metadata: Mapping[str, Any]
    ) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        metadata_path = source / _METADATA_NAME
        if metadata_path.exists():
            raise CatalogArchiveError(
                f"source already contains reserved archive metadata: {metadata_path}"
            )
        _atomic_write_yaml(metadata_path, metadata)
        try:
            source.rename(target)
        except Exception:
            metadata_path.unlink(missing_ok=True)
            raise

    def _reactivate(
        self,
        source: Path,
        destination: Path,
        *,
        expected_kind: str,
        after_move: Callable[[Path, Mapping[str, Any]], None] | None = None,
    ) -> None:
        if not source.is_dir():
            raise CatalogArchiveError(f"archive not found: {source}")
        if destination.exists():
            raise CatalogArchiveError(f"active destination already exists: {destination}")
        metadata = self._load_metadata(source, expected_kind=expected_kind)
        verification = self.verify(
            expected_kind,
            project_id=str(metadata.get("project_id", "")),
            item_id=str(metadata.get("id", "")),
        )
        if not verification["ok"]:
            details = []
            for key in ("missing", "unexpected", "changed"):
                if verification[key]:
                    details.append(f"{key}: {', '.join(verification[key][:8])}")
            raise CatalogArchiveError(
                "archive integrity verification failed; " + "; ".join(details)
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.rename(destination)
        if after_move is not None:
            try:
                after_move(destination, metadata)
            except Exception:
                # Keep the original archive metadata and inventory intact when
                # restoring associated host-local state fails.
                destination.rename(source)
                raise
        metadata_path = destination / _METADATA_NAME
        history_dir = destination / _HISTORY_DIRECTORY
        history_dir.mkdir(parents=True, exist_ok=True)
        archive_id = str(metadata.get("archive_id", "archive"))
        history_path = history_dir / f"{archive_id}.yaml"
        metadata = dict(metadata)
        metadata["reactivated_at"] = _utc_now()
        _atomic_write_yaml(history_path, metadata)
        metadata_path.unlink(missing_ok=True)

    def _metadata(
        self,
        *,
        kind: str,
        item_id: str,
        project_id: str,
        source: Path,
        reason: str,
        registration: ProjectRegistration | None = None,
    ) -> dict[str, Any]:
        files = _file_inventory(source, exclude_metadata=True)
        archived_at = _utc_now()
        archive_id = hashlib.sha256(
            f"{kind}\0{project_id}\0{item_id}\0{archived_at}".encode("utf-8")
        ).hexdigest()[:16]
        metadata: dict[str, Any] = {
            "schema_version": _ARCHIVE_SCHEMA_VERSION,
            "archive_id": archive_id,
            "kind": kind,
            "id": item_id,
            "project_id": project_id,
            "archived_at": archived_at,
            "reason": str(reason).strip(),
            "original_path": _display_original_path(source, self.root),
            "files": files,
            "file_count": len(files),
            "total_bytes": sum(int(item["size"]) for item in files),
        }
        if registration is not None:
            metadata["registration"] = {
                "source_root": (
                    str(registration.source_root)
                    if registration.source_root is not None
                    else ""
                )
            }
        return metadata

    @staticmethod
    def _restore_project_registration(
        destination: Path, metadata: Mapping[str, Any]
    ) -> None:
        """Restore a registration captured with a whole-project archive."""

        registration = metadata.get("registration")
        if not isinstance(registration, Mapping):
            return
        source_value = str(registration.get("source_root", "")).strip()
        source_root = Path(source_value) if source_value else None
        register_project_descriptor(
            destination / "project.yaml",
            source_root=source_root,
            replace=True,
        )

    def _archive_path(self, kind: str, *, project_id: str, item_id: str) -> Path:
        if kind == "project":
            if project_id != item_id:
                raise CatalogArchiveError("project archive id must match project_id")
            return self.archived_projects_root / item_id
        return self.archived_tasks_root / project_id / item_id

    def _load_metadata(self, path: Path, *, expected_kind: str) -> dict[str, Any]:
        if not path.is_dir() or path.is_symlink():
            raise CatalogArchiveError(f"archive not found: {path}")
        metadata_path = path / _METADATA_NAME
        metadata = _read_yaml_mapping(metadata_path)
        if int(metadata.get("schema_version", 0)) != _ARCHIVE_SCHEMA_VERSION:
            raise CatalogArchiveError(
                f"unsupported catalog archive schema in {metadata_path}"
            )
        if str(metadata.get("kind", "")) != expected_kind:
            raise CatalogArchiveError(
                f"archive kind mismatch: expected {expected_kind!r}"
            )
        item_id = _safe_identifier(str(metadata.get("id", "")), label=f"{expected_kind} id")
        project_id = _safe_identifier(
            str(metadata.get("project_id", "")), label="project id"
        )
        if expected_kind == "project":
            identity_matches = (
                path.parent == self.archived_projects_root
                and path.name == item_id
                and project_id == item_id
            )
        else:
            identity_matches = (
                path.parent.parent == self.archived_tasks_root
                and path.parent.name == project_id
                and path.name == item_id
            )
        if not identity_matches:
            raise CatalogArchiveError(
                f"archive metadata identity does not match its catalog path: {path}"
            )
        return dict(metadata)

    def _active_projects(self, *, current_project: str) -> list[CatalogArchiveEntry]:
        rows: list[CatalogArchiveEntry] = []
        project_ids = {
            entry.project.id
            for entry in list_project_catalog(self.root, skip_invalid=True)
        }
        if self.projects_root.is_dir():
            project_ids.update(
                directory.name
                for directory in self.projects_root.iterdir()
                if directory.is_dir()
                and not directory.name.startswith(".")
                and (directory / "project.yaml").is_file()
            )
        for project_id in sorted(project_ids):
            try:
                entry = self._active_project_entry(project_id)
            except (CatalogArchiveError, OSError, yaml.YAMLError):
                continue
            status = "current" if project_id == current_project else "active"
            rows.append(CatalogArchiveEntry(**{**entry.__dict__, "status": status}))
        return rows

    def _active_project_entry(self, project_id: str) -> CatalogArchiveEntry:
        path = project_directory(self.root, project_id)
        manifest = _read_yaml_mapping(path / "project.yaml")
        tasks_root = path / "tasks"
        task_count = sum(
            1
            for child in tasks_root.iterdir()
            if child.is_dir() and (child / "TASK.yaml").is_file()
        ) if tasks_root.is_dir() else 0
        return CatalogArchiveEntry(
            kind="project",
            item_id=project_id,
            project_id=project_id,
            title=str(manifest.get("description", "")).strip() or project_id,
            status="active",
            archived=False,
            task_count=task_count,
        )

    def _active_tasks(self, project_id: str, *, current_task: str) -> list[CatalogArchiveEntry]:
        tasks_root = project_directory(self.root, project_id) / "tasks"
        rows: list[CatalogArchiveEntry] = []
        for directory in sorted(tasks_root.iterdir()) if tasks_root.is_dir() else []:
            if not directory.is_dir() or not (directory / "TASK.yaml").is_file():
                continue
            try:
                entry = self._active_task_entry(project_id, directory.name)
            except (CatalogArchiveError, OSError, yaml.YAMLError):
                continue
            status = "current" if directory.name == current_task else entry.status
            rows.append(CatalogArchiveEntry(**{**entry.__dict__, "status": status}))
        return rows

    def _active_task_entry(self, project_id: str, task_id: str) -> CatalogArchiveEntry:
        path = project_directory(self.root, project_id) / "tasks" / task_id
        manifest = _read_yaml_mapping(path / "TASK.yaml")
        return CatalogArchiveEntry(
            kind="task",
            item_id=task_id,
            project_id=project_id,
            title=str(manifest.get("title", "")).strip() or task_id,
            status=str(manifest.get("status", "active")).strip() or "active",
            archived=False,
        )

    def _archived_entries(self, kind: str) -> list[CatalogArchiveEntry]:
        rows: list[CatalogArchiveEntry] = []
        if kind == "project":
            directories = (
                sorted(self.archived_projects_root.iterdir())
                if self.archived_projects_root.is_dir()
                else []
            )
        else:
            directories = []
            if self.archived_tasks_root.is_dir():
                for project_dir in sorted(self.archived_tasks_root.iterdir()):
                    if project_dir.is_dir():
                        directories.extend(sorted(project_dir.iterdir()))
        for directory in directories:
            if not directory.is_dir() or not (directory / _METADATA_NAME).is_file():
                continue
            try:
                metadata = self._load_metadata(directory, expected_kind=kind)
                rows.append(self._entry_from_archive(directory, metadata))
            except (CatalogArchiveError, OSError, yaml.YAMLError):
                continue
        rows.sort(key=lambda item: (item.archived_at, item.project_id, item.item_id), reverse=True)
        return rows

    def _entry_from_archive(
        self, path: Path, metadata: Mapping[str, Any]
    ) -> CatalogArchiveEntry:
        kind = str(metadata.get("kind", ""))
        project_id = str(metadata.get("project_id", ""))
        item_id = str(metadata.get("id", ""))
        title = item_id
        status = "archived"
        task_count = 0
        manifest_name = "project.yaml" if kind == "project" else "TASK.yaml"
        try:
            manifest = _read_yaml_mapping(path / manifest_name)
        except (CatalogArchiveError, OSError, yaml.YAMLError):
            manifest = {}
        if kind == "project":
            title = str(manifest.get("description", "")).strip() or item_id
            tasks_root = path / "tasks"
            task_count = sum(
                1
                for child in tasks_root.iterdir()
                if child.is_dir() and (child / "TASK.yaml").is_file()
            ) if tasks_root.is_dir() else 0
        else:
            title = str(manifest.get("title", "")).strip() or item_id
            manifest_status = str(manifest.get("status", "")).strip()
            status = f"archived · {manifest_status}" if manifest_status else "archived"
        return CatalogArchiveEntry(
            kind=kind,
            item_id=item_id,
            project_id=project_id,
            title=title,
            status=status,
            archived=True,
            archived_at=str(metadata.get("archived_at", "")),
            reason=str(metadata.get("reason", "")),
            file_count=int(metadata.get("file_count", 0)),
            total_bytes=int(metadata.get("total_bytes", 0)),
            task_count=task_count,
        )

    def _state_summary(self, project_id: str, task_id: str) -> dict[str, Any]:
        try:
            identity = resolve_storage_identity(
                self.state_root,
                project_id=project_id,
                task_id=task_id,
                create=False,
            )
        except ValueError:
            return {}
        state_path = identity.state_dir / "state.json"
        if not state_path.is_file():
            return {}
        try:
            raw = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"path": str(state_path), "error": "state file is unreadable"}
        packages = (raw.get("plan_graph") or {}).get("work_packages") or []
        completed = sum(
            1 for item in packages if isinstance(item, Mapping) and item.get("stage") == "completed"
        )
        return {
            "path": str(state_path),
            "state": str(raw.get("state", "")),
            "completed_packages": completed,
            "total_packages": len(packages),
            "last_transition_at": str(raw.get("last_transition_at", "")),
        }

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        self.archive_root.mkdir(parents=True, exist_ok=True)
        with self._thread_lock:
            with self.lock_path.open("a+") as handle:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if fcntl is not None:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _safe_kind(kind: str) -> str:
    value = str(kind).strip().lower()
    if value not in {"project", "task"}:
        raise CatalogArchiveError(f"unsupported archive kind: {kind!r}")
    return value


def _display_original_path(source: Path, control_root: Path) -> str:
    """Persist a stable original path for both managed and external dossiers."""

    try:
        return source.resolve().relative_to(control_root.resolve()).as_posix()
    except ValueError:
        return str(source.resolve())


def _safe_identifier(value: str, *, label: str) -> str:
    text = str(value).strip()
    if not text or not text.replace("-", "_").isidentifier():
        raise CatalogArchiveError(f"invalid {label}: {text!r}")
    return text


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise CatalogArchiveError(f"required archive file not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, Mapping):
        raise CatalogArchiveError(f"YAML document must contain a mapping: {path}")
    return dict(raw)


def _file_inventory(root: Path, *, exclude_metadata: bool) -> list[dict[str, Any]]:
    inventory: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part == _HISTORY_DIRECTORY for part in relative.parts):
            continue
        if exclude_metadata and relative.as_posix() == _METADATA_NAME:
            continue
        if path.is_symlink():
            raise CatalogArchiveError(f"catalog archives do not accept symlinks: {path}")
        if not path.is_file():
            continue
        safe_relative = PurePosixPath(relative.as_posix())
        if safe_relative.is_absolute() or ".." in safe_relative.parts:
            raise CatalogArchiveError(f"unsafe archive member: {relative}")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        inventory.append(
            {
                "path": safe_relative.as_posix(),
                "size": path.stat().st_size,
                "sha256": digest.hexdigest(),
            }
        )
    return inventory


def _atomic_write_yaml(path: Path, payload: Mapping[str, Any]) -> None:
    atomic_write_yaml(path, payload, sort_keys=False)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
