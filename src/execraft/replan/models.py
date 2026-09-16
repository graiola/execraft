"""Typed contracts for versioned task-definition replanning."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


class ReplanError(RuntimeError):
    """Base class for safe replanning failures."""


class ReplanConflictError(ReplanError):
    """Raised when a candidate would corrupt durable execution history."""


class ReplanConsistencyError(ReplanError):
    """Raised when BRIEF/PLAN/graph coherence cannot be established."""


class ReplanTransactionError(ReplanError):
    """Raised when a previous replanning transaction needs recovery."""


@dataclass(frozen=True)
class PackageImpact:
    package_id: str
    classification: str
    summary: str
    replacement_id: str = ""
    changed_fields: tuple[str, ...] = ()

    def as_mapping(self) -> dict[str, Any]:
        return {
            "package_id": self.package_id,
            "classification": self.classification,
            "summary": self.summary,
            "replacement_id": self.replacement_id,
            "changed_fields": list(self.changed_fields),
        }


@dataclass(frozen=True)
class ReplanImpact:
    current_revision: int
    candidate_revision: int
    packages: tuple[PackageImpact, ...] = ()
    added_packages: tuple[str, ...] = ()
    removed_packages: tuple[str, ...] = ()
    completed_packages: tuple[str, ...] = ()
    active_packages: tuple[str, ...] = ()
    pending_packages: tuple[str, ...] = ()
    requires_clean_workspace: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def applicable(self) -> bool:
        return not self.blockers

    def as_mapping(self) -> dict[str, Any]:
        return {
            "current_revision": self.current_revision,
            "candidate_revision": self.candidate_revision,
            "applicable": self.applicable,
            "packages": [item.as_mapping() for item in self.packages],
            "added_packages": list(self.added_packages),
            "removed_packages": list(self.removed_packages),
            "completed_packages": list(self.completed_packages),
            "active_packages": list(self.active_packages),
            "pending_packages": list(self.pending_packages),
            "requires_clean_workspace": list(self.requires_clean_workspace),
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class ReplanCandidate:
    candidate_id: str
    revision: int
    path: Path
    brief_markdown: str
    plan_markdown: str
    plan_graph_yaml: str
    impact: ReplanImpact
    consistency_mode: str
    consistency_summary: str
    package_mapping: Mapping[str, str] = field(default_factory=dict)
    document_origins: Mapping[str, str] = field(default_factory=dict)
    document_sources: Mapping[str, str] = field(default_factory=dict)
    requested_change: str = ""
    generated_by: str = "operator"
    provider_id: str = ""
    accepts_live_drift: bool = False

    def as_mapping(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "revision": self.revision,
            "path": str(self.path),
            "consistency_mode": self.consistency_mode,
            "consistency_summary": self.consistency_summary,
            "package_mapping": dict(self.package_mapping),
            "document_origins": dict(self.document_origins),
            "document_sources": dict(self.document_sources),
            "requested_change": self.requested_change,
            "generated_by": self.generated_by,
            "provider_id": self.provider_id,
            "accepts_live_drift": self.accepts_live_drift,
            "impact": self.impact.as_mapping(),
        }


@dataclass(frozen=True)
class ReplanApplyResult:
    candidate_id: str
    revision: int
    revision_path: Path
    previous_definition_sha256: str
    definition_sha256: str
    state_path: Path | None
    invalidated_capsules: int
    superseded_active_packages: tuple[str, ...] = ()
    previous_task_status: str = ""
    task_status: str = ""

    def as_mapping(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "revision": self.revision,
            "revision_path": str(self.revision_path),
            "previous_definition_sha256": self.previous_definition_sha256,
            "definition_sha256": self.definition_sha256,
            "state_path": str(self.state_path) if self.state_path else "",
            "invalidated_capsules": self.invalidated_capsules,
            "superseded_active_packages": list(self.superseded_active_packages),
            "previous_task_status": self.previous_task_status,
            "task_status": self.task_status,
        }


__all__ = [
    "PackageImpact",
    "ReplanApplyResult",
    "ReplanCandidate",
    "ReplanConflictError",
    "ReplanConsistencyError",
    "ReplanError",
    "ReplanImpact",
    "ReplanTransactionError",
]
