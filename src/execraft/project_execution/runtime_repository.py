"""Durable Project Execution runtime state, separate from its definition."""

from __future__ import annotations

import json
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from execraft.persistence.atomic import atomic_write_json
from execraft.persistence.locks import FileLock, LockLevel

from .models import ExecutionMode, ProjectExecutionError

RUNTIME_SCHEMA_VERSION = 1


def _mapping_dict(raw: object) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, Mapping):
        return {}
    return {
        str(key): dict(value)
        for key, value in raw.items()
        if isinstance(value, Mapping)
    }


def _string_list(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(value) for value in raw]


@dataclass
class ProjectExecutionRuntimeState:
    """Mutable durable observations and decisions for one Project Execution."""

    project_id: str
    definition_revision: int = 0
    definition_digest: str = ""
    mode: str = ExecutionMode.ASSISTED.value
    held: bool = False
    hold_reason: str = ""
    gates: dict[str, dict[str, Any]] = field(default_factory=dict)
    milestone_achievements: dict[str, dict[str, Any]] = field(default_factory=dict)
    cancelled_phases: list[str] = field(default_factory=list)
    cancelled_gates: list[str] = field(default_factory=list)
    cancelled_milestones: list[str] = field(default_factory=list)
    observed_tasks: dict[str, dict[str, Any]] = field(default_factory=dict)
    intents: dict[str, dict[str, Any]] = field(default_factory=dict)
    executor: dict[str, Any] = field(default_factory=dict)
    schema_version: int = RUNTIME_SCHEMA_VERSION

    def as_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "definition_revision": self.definition_revision,
            "definition_digest": self.definition_digest,
            "mode": self.mode,
            "held": self.held,
            "hold_reason": self.hold_reason,
            "gates": self.gates,
            "milestone_achievements": self.milestone_achievements,
            "cancelled_phases": self.cancelled_phases,
            "cancelled_gates": self.cancelled_gates,
            "cancelled_milestones": self.cancelled_milestones,
            "observed_tasks": self.observed_tasks,
            "intents": self.intents,
            "executor": self.executor,
        }

    @classmethod
    def from_mapping(cls, raw: object) -> "ProjectExecutionRuntimeState":
        if not isinstance(raw, Mapping):
            raise ProjectExecutionError("project runtime state must be a mapping")
        if int(raw.get("schema_version", 0)) != RUNTIME_SCHEMA_VERSION:
            raise ProjectExecutionError(
                "unsupported project runtime schema_version: "
                f"{raw.get('schema_version')!r}"
            )
        executor = raw.get("executor") or {}
        if not isinstance(executor, Mapping):
            raise ProjectExecutionError("project runtime executor must be a mapping")
        return cls(
            project_id=str(raw.get("project_id", "")),
            definition_revision=int(raw.get("definition_revision", 0)),
            definition_digest=str(raw.get("definition_digest", "")),
            mode=str(raw.get("mode", ExecutionMode.ASSISTED.value)),
            held=bool(raw.get("held", False)),
            hold_reason=str(raw.get("hold_reason", "")),
            gates=_mapping_dict(raw.get("gates")),
            milestone_achievements=_mapping_dict(raw.get("milestone_achievements")),
            cancelled_phases=_string_list(raw.get("cancelled_phases")),
            cancelled_gates=_string_list(raw.get("cancelled_gates")),
            cancelled_milestones=_string_list(raw.get("cancelled_milestones")),
            observed_tasks=_mapping_dict(raw.get("observed_tasks")),
            intents=_mapping_dict(raw.get("intents")),
            executor=dict(executor),
        )


class ProjectRuntimeRepository:
    """Atomic JSON repository for one project's runtime projection."""

    def __init__(
        self,
        state_root: Path,
        project_id: str,
        *,
        lock_timeout: float = 5.0,
    ) -> None:
        self.directory = (
            Path(state_root).expanduser().resolve() / "project-execution" / project_id
        )
        self.path = self.directory / "state.json"
        self.lock_path = self.directory / "state.lock"
        self.lock_timeout = lock_timeout

    def load(self) -> ProjectExecutionRuntimeState:
        with self._lock(exclusive=False):
            return self._load_unlocked()

    def save(self, state: ProjectExecutionRuntimeState) -> None:
        if state.project_id != self.directory.name:
            raise ProjectExecutionError(
                "Project Execution runtime project identity cannot change"
            )
        with self._lock():
            atomic_write_json(self.path, state.as_mapping(), mode=0o600)

    def _load_unlocked(self) -> ProjectExecutionRuntimeState:
        if not self.path.exists():
            return ProjectExecutionRuntimeState(project_id=self.directory.name)
        metadata = self.path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ProjectExecutionError(
                f"project runtime path is not a regular file: {self.path}"
            )
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            state = ProjectExecutionRuntimeState.from_mapping(raw)
        except (OSError, ValueError, TypeError) as exc:
            raise ProjectExecutionError(
                f"cannot read Project Execution runtime state: {exc}"
            ) from exc
        if state.project_id != self.directory.name:
            raise ProjectExecutionError(
                "Project Execution runtime project identity does not match storage path"
            )
        return state

    def _lock(self, *, exclusive: bool = True) -> FileLock:
        return FileLock(
            self.lock_path,
            level=LockLevel.RECORD,
            exclusive=exclusive,
            timeout=self.lock_timeout,
        )
