"""Crash-safe and conflict-checked roadmap YAML repository."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
import yaml

from execraft.persistence.atomic import atomic_write_yaml, fsync_directory
from execraft.persistence.locks import FileLock, LockLevel
from execraft.project import ProjectDescriptor

from .models import (
    Roadmap,
    RoadmapConflictError,
    RoadmapError,
    RoadmapNotFoundError,
    validate_roadmap_id,
)


class RoadmapRepository:
    """Persist roadmaps under ``<project>/roadmaps`` with one project-level lock."""

    def __init__(
        self,
        project: ProjectDescriptor,
        *,
        state_root: Path,
        lock_timeout: float = 5.0,
    ) -> None:
        self.project = project
        self.root = project.directory / "roadmaps"
        self.lock_path = (
            Path(state_root).expanduser().resolve()
            / "roadmap-locks"
            / f"{project.id}.lock"
        )
        self.lock_timeout = lock_timeout

    def path(self, roadmap_id: str) -> Path:
        return self.root / f"{validate_roadmap_id(roadmap_id)}.yaml"

    def list(self) -> list[Roadmap]:
        if not self.root.is_dir():
            return []
        documents: list[Roadmap] = []
        for path in sorted(self.root.glob("*.yaml")):
            if path.is_symlink() or not path.is_file():
                continue
            documents.append(self._load_path(path))
        documents.sort(
            key=lambda item: (item.updated_at, item.title, item.id), reverse=True
        )
        return documents

    def load(self, roadmap_id: str) -> Roadmap:
        path = self.path(roadmap_id)
        if not path.is_file() or path.is_symlink():
            raise RoadmapNotFoundError(f"roadmap not found: {roadmap_id}")
        return self._load_path(path)

    def create(self, roadmap: Roadmap) -> Roadmap:
        path = self.path(roadmap.id)
        with self._lock():
            if path.exists():
                raise RoadmapConflictError(f"roadmap already exists: {roadmap.id}")
            self._write(path, roadmap)
        return roadmap

    def save(self, roadmap: Roadmap, *, expected_revision: int) -> Roadmap:
        path = self.path(roadmap.id)
        with self._lock():
            current = self._load_required(path, roadmap.id)
            if current.revision != int(expected_revision):
                raise RoadmapConflictError(
                    "roadmap changed since it was loaded; refresh before saving "
                    f"(expected revision {expected_revision}, current {current.revision})"
                )
            updated = replace(roadmap, revision=current.revision + 1)
            self._write(path, updated)
        return updated

    def delete(self, roadmap_id: str, *, expected_revision: int) -> Roadmap:
        path = self.path(roadmap_id)
        with self._lock():
            current = self._load_required(path, roadmap_id)
            if current.revision != int(expected_revision):
                raise RoadmapConflictError(
                    "roadmap changed since it was loaded; refresh before deleting"
                )
            path.unlink()
            fsync_directory(path.parent)
        return current

    def sha256(self, roadmap_id: str) -> str:
        path = self.path(roadmap_id)
        if not path.is_file():
            return ""
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _load_required(self, path: Path, roadmap_id: str) -> Roadmap:
        if not path.is_file() or path.is_symlink():
            raise RoadmapNotFoundError(f"roadmap not found: {roadmap_id}")
        return self._load_path(path)

    @staticmethod
    def _load_path(path: Path) -> Roadmap:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise RoadmapError(f"cannot load roadmap {path.name}: {exc}") from exc
        roadmap = Roadmap.from_mapping(raw)
        if path.stem != roadmap.id:
            raise RoadmapError(
                f"roadmap id {roadmap.id!r} does not match filename {path.name!r}"
            )
        return roadmap

    @staticmethod
    def _write(path: Path, roadmap: Roadmap) -> None:
        atomic_write_yaml(path, roadmap.as_mapping(), sort_keys=False)

    def _lock(self) -> FileLock:
        # ROADMAP state is a normal durable record and does not nest any lower
        # level repository/lifecycle locks.
        return FileLock(
            self.lock_path,
            level=LockLevel.RECORD,
            timeout=self.lock_timeout,
        )
