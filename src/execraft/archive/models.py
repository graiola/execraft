"""Data contracts for task completion archive operations."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from execraft.workspace.task_git import TaskGitError


@dataclass(frozen=True)
class ArchiveCheck:
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
class ArchivePreflightReport:
    task_id: str
    project: str
    checks: list[ArchiveCheck] = field(default_factory=list)

    @property
    def errors(self) -> list[ArchiveCheck]:
        return [check for check in self.checks if not check.ok and check.severity == "error"]

    @property
    def warnings(self) -> list[ArchiveCheck]:
        return [check for check in self.checks if not check.ok and check.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def require_ok(self) -> None:
        if self.ok:
            return
        detail = "; ".join(check.message for check in self.errors)
        raise TaskGitError(f"task archive preflight failed: {detail}")

    def as_mapping(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "project": self.project,
            "ok": self.ok,
            "checks": [check.as_mapping() for check in self.checks],
        }


@dataclass(frozen=True)
class ArchiveResult:
    project: str
    task_id: str
    archive_id: str
    archive_path: Path
    manifest_path: Path
    manifest_sha256: str
    created: bool
