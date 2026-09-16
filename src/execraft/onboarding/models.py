"""Immutable domain models for project and task onboarding.

The onboarding layer represents inspection, readiness, and filesystem changes as
plain data before applying side effects.  CLI, GUI, and future API frontends can
therefore render the same decisions without reimplementing bootstrap logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping


class FindingSeverity(str, Enum):
    """Severity and operator-interaction class for an onboarding finding."""

    ERROR = "error"
    DECISION_REQUIRED = "decision_required"
    WARNING = "warning"
    INFO = "info"


class ReadinessStatus(str, Enum):
    """State of one independently actionable readiness dimension."""

    READY = "ready"
    BLOCKED = "blocked"
    WARNING = "warning"
    DISABLED = "disabled"


@dataclass(frozen=True)
class Evidence:
    """One observed value with provenance and confidence.

    Confidence is normalized to ``[0.0, 1.0]``.  Evidence never implies that a
    value has been approved; it only records why the discovery engine proposed
    it.
    """

    id: str
    subject: str
    field: str
    value: Any
    source: str
    confidence: float
    rationale: str = ""
    location: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("evidence id cannot be empty")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("evidence confidence must be between 0.0 and 1.0")

    def as_mapping(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "subject": self.subject,
            "field": self.field,
            "value": self.value,
            "source": self.source,
            "confidence": self.confidence,
            "rationale": self.rationale,
        }
        if self.location:
            result["location"] = self.location
        if self.metadata:
            result["metadata"] = dict(self.metadata)
        return result


@dataclass(frozen=True)
class Finding:
    """A discovery or validation outcome requiring awareness or action."""

    code: str
    severity: FindingSeverity
    message: str
    subject: str = ""
    remediation: str = ""
    evidence_ids: tuple[str, ...] = ()

    @property
    def blocks_apply(self) -> bool:
        return self.severity is FindingSeverity.ERROR

    @property
    def requires_decision(self) -> bool:
        return self.severity is FindingSeverity.DECISION_REQUIRED

    def as_mapping(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
        }
        if self.subject:
            result["subject"] = self.subject
        if self.remediation:
            result["remediation"] = self.remediation
        if self.evidence_ids:
            result["evidence_ids"] = list(self.evidence_ids)
        return result


@dataclass(frozen=True)
class PlannedFile:
    """A deterministic file-system effect in a creation plan."""

    path: str
    size_bytes: int
    sha256: str
    action: str = "create"

    def as_mapping(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "action": self.action,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class CreationPlan:
    """Immutable preview of a project or task creation transaction."""

    kind: str
    identifier: str
    target: Path
    files: tuple[PlannedFile, ...]
    evidence: tuple[Evidence, ...] = ()
    findings: tuple[Finding, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    decisions_accepted: bool = False

    @property
    def decision_findings(self) -> tuple[Finding, ...]:
        return tuple(item for item in self.findings if item.requires_decision)

    @property
    def pending_decisions(self) -> tuple[Finding, ...]:
        return () if self.decisions_accepted else self.decision_findings

    @property
    def can_apply(self) -> bool:
        has_errors = any(item.blocks_apply for item in self.findings)
        return not has_errors and not self.pending_decisions

    @property
    def total_bytes(self) -> int:
        return sum(item.size_bytes for item in self.files)

    def as_mapping(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "identifier": self.identifier,
            "target": str(self.target),
            "can_apply": self.can_apply,
            "decisions_accepted": self.decisions_accepted,
            "pending_decisions": [item.as_mapping() for item in self.pending_decisions],
            "total_bytes": self.total_bytes,
            "files": [item.as_mapping() for item in self.files],
            "findings": [item.as_mapping() for item in self.findings],
            "evidence": [item.as_mapping() for item in self.evidence],
            "metadata": dict(self.metadata),
        }

    def render_text(self) -> str:
        lines = [
            f"{self.kind.capitalize()} creation plan: {self.identifier}",
            f"Target: {self.target}",
            f"Files: {len(self.files)} ({self.total_bytes} bytes)",
        ]
        for item in self.files:
            lines.append(
                f"  {item.action:<6} {item.path} "
                f"({item.size_bytes} bytes, sha256:{item.sha256[:12]})"
            )
        if self.findings:
            lines.append("Findings:")
            for finding in self.findings:
                lines.append(
                    f"  [{finding.severity.value}] {finding.code}: {finding.message}"
                )
        if self.decision_findings:
            state = "accepted" if self.decisions_accepted else "pending"
            lines.append(f"Operator decisions: {len(self.decision_findings)} ({state})")
        lines.append(f"Applicable: {'yes' if self.can_apply else 'no'}")
        return "\n".join(lines)


@dataclass(frozen=True)
class ReadinessCheck:
    """Readiness state for one independently repairable subsystem."""

    id: str
    status: ReadinessStatus
    summary: str
    details: tuple[str, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    required: bool = True

    @property
    def blocks(self) -> bool:
        return self.required and self.status is ReadinessStatus.BLOCKED

    def as_mapping(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status.value,
            "summary": self.summary,
            "required": self.required,
            "details": list(self.details),
            "evidence": [item.as_mapping() for item in self.evidence],
        }


@dataclass(frozen=True)
class ReadinessReport:
    """Multi-dimensional project readiness report."""

    project_id: str
    checks: tuple[ReadinessCheck, ...]

    @property
    def ready(self) -> bool:
        return not any(check.blocks for check in self.checks)

    @property
    def blocked_checks(self) -> tuple[ReadinessCheck, ...]:
        return tuple(check for check in self.checks if check.blocks)

    def as_mapping(self) -> dict[str, Any]:
        return {
            "project": self.project_id,
            "ready": self.ready,
            "checks": [check.as_mapping() for check in self.checks],
        }

    def render_text(self) -> str:
        lines = [f"Readiness for project {self.project_id}:"]
        for check in self.checks:
            lines.append(f"  {check.id:<16} {check.status.value.upper():<8} {check.summary}")
            for detail in check.details:
                lines.append(f"    - {detail}")
        lines.append(f"Overall: {'READY' if self.ready else 'BLOCKED'}")
        return "\n".join(lines)
