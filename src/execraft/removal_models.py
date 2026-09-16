"""Data models for irreversible Execraft catalog removal."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class PermanentRemovalError(RuntimeError):
    """Raised when permanent removal is unsafe, ambiguous, or incomplete."""


@dataclass(frozen=True)
class BranchRemoval:
    """One task branch that Execraft may remove from a source repository."""

    repository_id: str
    source_path: Path
    branch: str

    def as_mapping(self) -> dict[str, str]:
        return {
            "repository_id": self.repository_id,
            "source_path": str(self.source_path),
            "branch": self.branch,
        }


@dataclass
class RemovalPlan:
    """Validated set of Execraft-owned resources targeted by one removal."""

    kind: str
    project_id: str
    item_id: str
    paths: list[Path] = field(default_factory=list)
    branch_removals: list[BranchRemoval] = field(default_factory=list)
    preserved: list[str] = field(default_factory=list)

    def as_mapping(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "project_id": self.project_id,
            "id": self.item_id,
            "paths": [str(path) for path in self.paths],
            "branch_removals": [item.as_mapping() for item in self.branch_removals],
            "preserved": list(self.preserved),
        }


@dataclass(frozen=True)
class RemovalResult:
    """Result of an irreversible catalog removal."""

    kind: str
    project_id: str
    item_id: str
    removed_paths: tuple[str, ...]
    removed_branches: tuple[str, ...] = ()
    preserved: tuple[str, ...] = ()
    dry_run: bool = False

    def as_mapping(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "project_id": self.project_id,
            "id": self.item_id,
            "removed_paths": list(self.removed_paths),
            "removed_branches": list(self.removed_branches),
            "preserved": list(self.preserved),
            "dry_run": self.dry_run,
        }
