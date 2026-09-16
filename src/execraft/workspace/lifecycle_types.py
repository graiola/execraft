"""Typed contracts shared by workspace lifecycle components."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from execraft.workspace.task_git import TaskGitError


@dataclass(frozen=True)
class LifecycleCheck:
    """One deterministic workspace lifecycle safety assertion."""

    id: str
    ok: bool
    message: str
    severity: str = "error"

    def as_mapping(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ok": self.ok,
            "message": self.message,
            "severity": self.severity,
        }


@dataclass
class LifecycleReport:
    """Preflight evidence for a stop or destroy operation."""

    task_id: str
    operation: str
    checks: list[LifecycleCheck] = field(default_factory=list)

    @property
    def errors(self) -> list[LifecycleCheck]:
        return [
            check for check in self.checks if not check.ok and check.severity in {"error", "fatal"}
        ]

    @property
    def fatal_errors(self) -> list[LifecycleCheck]:
        return [check for check in self.checks if not check.ok and check.severity == "fatal"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def require_ok(self) -> None:
        if self.ok:
            return
        detail = "; ".join(check.message for check in self.errors)
        raise TaskGitError(f"workspace {self.operation} preflight failed: {detail}")

    def require_integrity(self) -> None:
        """Reject ownership/routing corruption even in force recovery mode."""

        if not self.fatal_errors:
            return
        detail = "; ".join(check.message for check in self.fatal_errors)
        raise TaskGitError(f"workspace lifecycle integrity check failed: {detail}")

    def as_mapping(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "operation": self.operation,
            "ok": self.ok,
            "checks": [check.as_mapping() for check in self.checks],
        }


@dataclass(frozen=True)
class RuntimeStopResult:
    """Result of an exact, label-scoped runtime shutdown."""

    actions: tuple[str, ...] = ()
    stopped_containers: tuple[str, ...] = ()
    removed_networks: tuple[str, ...] = ()
    skipped: bool = False


@dataclass(frozen=True)
class WorkspaceLifecycleResult:
    """Result of a runtime stop or destructive workspace retirement."""

    report: LifecycleReport
    actions: tuple[str, ...]
    runtime: RuntimeStopResult
    removed_worktrees: tuple[str, ...] = ()
    shell_removed: bool = False
