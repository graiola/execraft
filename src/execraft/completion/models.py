"""Typed contracts for unified task completion."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class TaskCompletionPolicy:
    """Policy controlling post-orchestration archival and workspace retirement.

    Automatic completion is enabled by default for isolated Execraft task workspaces.
    The destructive part always remains behind the archive/ownership/Git safety
    checks. Task branches and the live dossier are intentionally retained.
    """

    automatic: bool = True
    require_archive: bool = True
    verify_archive_before_cleanup: bool = True
    stop_runtime: bool = True
    remove_worktrees: bool = True
    remove_workspace_shell: bool = True
    retain_task_branches: bool = True
    retain_live_dossier: bool = True
    retain_workspace_tombstone: bool = True

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "TaskCompletionPolicy":
        if not raw:
            return cls()
        if not isinstance(raw, Mapping):
            raise ValueError("task completion policy must be a mapping")

        defaults = cls()

        def boolean(key: str) -> bool:
            default = getattr(defaults, key)
            value = raw.get(key, default)
            if not isinstance(value, bool):
                raise ValueError(f"task completion policy {key} must be a boolean")
            return value

        policy = cls(
            automatic=boolean("automatic"),
            require_archive=boolean("require_archive"),
            verify_archive_before_cleanup=boolean("verify_archive_before_cleanup"),
            stop_runtime=boolean("stop_runtime"),
            remove_worktrees=boolean("remove_worktrees"),
            remove_workspace_shell=boolean("remove_workspace_shell"),
            retain_task_branches=boolean("retain_task_branches"),
            retain_live_dossier=boolean("retain_live_dossier"),
            retain_workspace_tombstone=boolean("retain_workspace_tombstone"),
        )
        if not policy.require_archive and policy.remove_worktrees:
            raise ValueError(
                "task completion policy remove_worktrees requires require_archive=true"
            )
        if policy.remove_worktrees and not policy.stop_runtime:
            raise ValueError(
                "task completion policy remove_worktrees requires stop_runtime=true"
            )
        if policy.remove_worktrees and not policy.verify_archive_before_cleanup:
            raise ValueError(
                "task completion policy remove_worktrees requires "
                "verify_archive_before_cleanup=true"
            )
        if policy.remove_workspace_shell and not policy.remove_worktrees:
            raise ValueError(
                "task completion policy remove_workspace_shell requires remove_worktrees=true"
            )
        # Completion deliberately retains these durable recovery/traceability surfaces.
        if not policy.retain_task_branches:
            raise ValueError("completion does not delete task branches; retain_task_branches must be true")
        if not policy.retain_live_dossier:
            raise ValueError("completion retains the live dossier; retain_live_dossier must be true")
        if not policy.retain_workspace_tombstone:
            raise ValueError(
                "completion retains the workspace registry tombstone; "
                "retain_workspace_tombstone must be true"
            )
        return policy

    def as_mapping(self) -> dict[str, Any]:
        return {
            "automatic": self.automatic,
            "require_archive": self.require_archive,
            "verify_archive_before_cleanup": self.verify_archive_before_cleanup,
            "stop_runtime": self.stop_runtime,
            "remove_worktrees": self.remove_worktrees,
            "remove_workspace_shell": self.remove_workspace_shell,
            "retain_task_branches": self.retain_task_branches,
            "retain_live_dossier": self.retain_live_dossier,
            "retain_workspace_tombstone": self.retain_workspace_tombstone,
        }


@dataclass(frozen=True)
class TaskCompletionResult:
    """Durable result of one idempotent completion transaction."""

    project: str
    task_id: str
    status: str
    phase: str
    report_path: Path
    archive_path: Path | None = None
    archive_id: str = ""
    workspace_status: str = ""
    runtime_stopped: bool = False
    removed_worktrees: tuple[str, ...] = ()
    shell_removed: bool = False
    actions: tuple[str, ...] = ()
    resumed: bool = False

    @property
    def completed(self) -> bool:
        return self.status == "completed"

    def as_mapping(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "task_id": self.task_id,
            "status": self.status,
            "phase": self.phase,
            "report_path": str(self.report_path),
            "archive_path": str(self.archive_path) if self.archive_path else "",
            "archive_id": self.archive_id,
            "workspace_status": self.workspace_status,
            "runtime_stopped": self.runtime_stopped,
            "removed_worktrees": list(self.removed_worktrees),
            "shell_removed": self.shell_removed,
            "actions": list(self.actions),
            "resumed": self.resumed,
        }


class TaskCompletionError(RuntimeError):
    """Raised when completion cannot safely finish in the current invocation."""

    def __init__(self, message: str, *, report_path: Path | None = None) -> None:
        super().__init__(message)
        self.report_path = report_path
