"""Deterministic recovery playbooks for known orchestration deadlocks.

The autonomous Supervisor is intentionally reserved for incidents that require
reasoning across ambiguous project state.  Some escalations already contain a
complete, machine-actionable recovery plan.  Re-running a broad Supervisor
prompt for those cases wastes provider budget and can create a second failure
mode (invalid structured output, cooldown, or provider wait) before the real
repair even begins.

This module models bounded, deterministic playbooks that only change durable
orchestrator state.  They never edit source files themselves and never bypass
verification, independent review, acceptance evidence, or commit readiness checks.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


class RecoveryPlaybookConfigError(ValueError):
    """Raised when a deterministic recovery policy is invalid."""


def _boolean(mapping: Mapping[str, Any], key: str, default: bool) -> bool:
    value = mapping.get(key, default)
    if not isinstance(value, bool):
        raise RecoveryPlaybookConfigError(
            f"recovery_playbooks.review_exhausted.{key} must be a boolean"
        )
    return value


def _bounded_int(
    mapping: Mapping[str, Any],
    key: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = mapping.get(key, default)
    if isinstance(value, bool):
        raise RecoveryPlaybookConfigError(
            f"recovery_playbooks.review_exhausted.{key} must be an integer"
        )
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RecoveryPlaybookConfigError(
            f"recovery_playbooks.review_exhausted.{key} must be an integer"
        ) from exc
    if not minimum <= parsed <= maximum:
        raise RecoveryPlaybookConfigError(
            f"recovery_playbooks.review_exhausted.{key} must be between "
            f"{minimum} and {maximum}"
        )
    return parsed


@dataclass(frozen=True)
class ReviewExhaustedPlaybookPolicy:
    """Policy for reopening an exhausted review/fix loop without supervision.

    The playbook queues a narrowly-scoped ``fix_review`` stage using the exact
    final-review findings already persisted on the package.  It deliberately
    prefers a provider other than the configured Supervisor so a malformed
    supervision response cannot repeatedly consume the same provider budget.
    """

    enabled: bool = True
    max_rescue_cycles: int = 2
    max_findings: int = 32
    prefer_non_supervisor_fixer: bool = True
    allow_supervisor_fallback: bool = False

    @classmethod
    def from_mapping(cls, raw: object) -> "ReviewExhaustedPlaybookPolicy":
        if raw is None:
            return cls()
        if not isinstance(raw, Mapping):
            raise RecoveryPlaybookConfigError(
                "recovery_playbooks.review_exhausted must be a mapping"
            )
        return cls(
            enabled=_boolean(raw, "enabled", True),
            max_rescue_cycles=_bounded_int(
                raw, "max_rescue_cycles", 2, minimum=1, maximum=8
            ),
            max_findings=_bounded_int(
                raw, "max_findings", 32, minimum=1, maximum=256
            ),
            prefer_non_supervisor_fixer=_boolean(
                raw, "prefer_non_supervisor_fixer", True
            ),
            allow_supervisor_fallback=_boolean(
                raw, "allow_supervisor_fallback", False
            ),
        )

    def as_mapping(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "max_rescue_cycles": self.max_rescue_cycles,
            "max_findings": self.max_findings,
            "prefer_non_supervisor_fixer": self.prefer_non_supervisor_fixer,
            "allow_supervisor_fallback": self.allow_supervisor_fallback,
        }


@dataclass(frozen=True)
class RecoveryPlaybookPolicy:
    """Project-level deterministic incident-recovery policy."""

    enabled: bool = False
    review_exhausted: ReviewExhaustedPlaybookPolicy = (
        ReviewExhaustedPlaybookPolicy()
    )

    @classmethod
    def from_mapping(cls, raw: object) -> "RecoveryPlaybookPolicy":
        if raw is None:
            return cls()
        if not isinstance(raw, Mapping):
            raise RecoveryPlaybookConfigError(
                "scheduling.recovery_playbooks must be a mapping"
            )
        enabled = raw.get("enabled", False)
        if not isinstance(enabled, bool):
            raise RecoveryPlaybookConfigError(
                "recovery_playbooks.enabled must be a boolean"
            )
        return cls(
            enabled=enabled,
            review_exhausted=ReviewExhaustedPlaybookPolicy.from_mapping(
                raw.get("review_exhausted")
            ),
        )

    def as_mapping(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "review_exhausted": self.review_exhausted.as_mapping(),
        }


def recovery_playbook_policy_from_scheduling(
    scheduling: object,
) -> RecoveryPlaybookPolicy:
    """Parse ``scheduling.recovery_playbooks`` with strict validation."""

    if scheduling is None:
        return RecoveryPlaybookPolicy()
    if not isinstance(scheduling, Mapping):
        raise RecoveryPlaybookConfigError("agent scheduling policy must be a mapping")
    return RecoveryPlaybookPolicy.from_mapping(scheduling.get("recovery_playbooks"))


def normalize_findings(findings: Iterable[object], *, limit: int) -> tuple[str, ...]:
    """Return stable, non-empty, bounded findings preserving reviewer order."""

    normalized: list[str] = []
    for item in findings:
        text = str(item).strip()
        if not text or text in normalized:
            continue
        normalized.append(text[:12000])
        if len(normalized) >= limit:
            break
    return tuple(normalized)


def findings_fingerprint(findings: Iterable[object]) -> str:
    """Return a stable fingerprint for one exact review finding set."""

    normalized = [str(item).strip() for item in findings if str(item).strip()]
    payload = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def indexed_findings(findings: Iterable[str]) -> dict[str, str]:
    """Assign durable IDs used by the rescue fixer's strict result contract."""

    return {
        f"RF-{index:03d}": str(finding)
        for index, finding in enumerate(findings, start=1)
    }
