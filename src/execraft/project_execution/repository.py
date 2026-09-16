"""Crash-safe canonical ``PROJECT_EXECUTION.yaml`` repository."""

from __future__ import annotations

import stat
from dataclasses import replace
from pathlib import Path

import yaml

from execraft.persistence.atomic import atomic_write_yaml
from execraft.persistence.locks import FileLock, LockLevel

from .models import (
    ProjectExecutionConflictError,
    ProjectExecutionDefinition,
    ProjectExecutionError,
    ProjectExecutionNotFoundError,
)
from .validation import validate_definition


class ProjectExecutionRepository:
    """Persist one complete Project Execution graph under a project descriptor."""

    def __init__(self, project_directory: Path, *, lock_timeout: float = 5.0) -> None:
        self.project_directory = Path(project_directory).expanduser().resolve()
        self.path = self.project_directory / "PROJECT_EXECUTION.yaml"
        self.lock_path = self.project_directory / ".PROJECT_EXECUTION.lock"
        self.lock_timeout = lock_timeout

    def exists(self) -> bool:
        return self.path.is_file() and not self.path.is_symlink()

    def load(self) -> ProjectExecutionDefinition:
        with self._lock(exclusive=False):
            return self._load_unlocked()

    def load_optional(self) -> ProjectExecutionDefinition | None:
        if not self.path.exists():
            return None
        return self.load()

    def create(
        self,
        definition: ProjectExecutionDefinition,
    ) -> ProjectExecutionDefinition:
        validate_definition(definition)
        with self._lock():
            if self.path.exists():
                raise ProjectExecutionConflictError(
                    "PROJECT_EXECUTION.yaml already exists"
                )
            created = replace(definition, revision=1)
            atomic_write_yaml(self.path, created.as_mapping(), mode=0o600)
            return created

    def save(
        self,
        definition: ProjectExecutionDefinition,
        *,
        expected_revision: int,
    ) -> ProjectExecutionDefinition:
        validate_definition(definition)
        with self._lock():
            current = self._load_unlocked()
            if current.revision != expected_revision:
                raise ProjectExecutionConflictError(
                    "Project Execution revision conflict: "
                    f"expected {expected_revision}, current {current.revision}"
                )
            if definition.project != current.project:
                raise ProjectExecutionError(
                    "Project Execution project identity cannot change"
                )
            saved = replace(definition, revision=current.revision + 1)
            atomic_write_yaml(self.path, saved.as_mapping(), mode=0o600)
            return saved

    def _load_unlocked(self) -> ProjectExecutionDefinition:
        try:
            metadata = self.path.lstat()
        except FileNotFoundError as exc:
            raise ProjectExecutionNotFoundError(
                f"Project Execution definition not found: {self.path}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ProjectExecutionError(
                f"Project Execution path is not a regular file: {self.path}"
            )
        try:
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise ProjectExecutionError(
                f"cannot read Project Execution definition: {exc}"
            ) from exc
        return ProjectExecutionDefinition.from_mapping(raw)

    def _lock(self, *, exclusive: bool = True) -> FileLock:
        return FileLock(
            self.lock_path,
            level=LockLevel.RECORD,
            exclusive=exclusive,
            timeout=self.lock_timeout,
        )
