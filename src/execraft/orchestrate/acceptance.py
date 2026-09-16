"""Deterministic collection of durable acceptance evidence.

Acceptance evidence is intentionally reconstructed only from control-plane
records that already passed their own checks: completed aggregate children,
verification command results, and an approved review result.  Model prose and
criterion naming conventions are not treated as proof.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .journal import JournalEntry
from .models import WorkPackage
from .verification_outcome import (
    PASSED_WITH_ACCEPTED_BASELINE,
    verification_acceptance_phrase,
)


@dataclass(frozen=True)
class AcceptanceEvidenceBundle:
    """Validated sources used to satisfy package-level exit criteria."""

    criterion_ids: tuple[str, ...]
    verification_profile: str
    verification_commands: tuple[dict[str, Any], ...]
    verification_sequences: tuple[int, ...]
    verification_status: str
    accepted_baseline_matches: tuple[dict[str, Any], ...]
    review_sequence: int
    reviewer_id: str
    review_stage: str
    review_artifact: dict[str, Any]
    child_evidence: tuple[dict[str, Any], ...] = ()
    legacy_observations: tuple[str, ...] = ()

    def as_mapping(self) -> dict[str, Any]:
        return {
            "criteria": list(self.criterion_ids),
            "verification_profile": self.verification_profile,
            "verification_commands": [dict(item) for item in self.verification_commands],
            "verification_sequences": list(self.verification_sequences),
            "verification_status": self.verification_status,
            "accepted_baseline_matches": [
                dict(item) for item in self.accepted_baseline_matches
            ],
            "child_evidence": [dict(item) for item in self.child_evidence],
            "review": {
                "sequence": self.review_sequence,
                "agent_id": self.reviewer_id,
                "stage": self.review_stage,
                "artifact": dict(self.review_artifact),
                "verdict": "approved",
                "legacy_observations": list(self.legacy_observations),
            },
        }

    def evidence_text(self) -> str:
        commands = "; ".join(
            _command_label(item) for item in self.verification_commands
        )
        artifact_path = str(self.review_artifact.get("path", "")).strip()
        artifact_digest = str(self.review_artifact.get("sha256", "")).strip()
        reviewer = self.reviewer_id or "independent reviewer"
        review_ref = reviewer
        if artifact_path:
            review_ref += f" artifact={artifact_path}"
        if artifact_digest:
            review_ref += f" sha256={artifact_digest}"
        children = (
            f" {len(self.child_evidence)} aggregate child(ren) completed;"
            if self.child_evidence
            else ""
        )
        verification = verification_acceptance_phrase(
            {
                "status": self.verification_status,
                "accepted_baseline_matches": list(self.accepted_baseline_matches),
            }
        )
        return (
            f"Automatically collected from verification profile "
            f"'{self.verification_profile}':{children} {len(self.verification_commands)} "
            f"command(s) evaluated ({commands}); {verification}; "
            f"final review approved by {review_ref}."
        )



@dataclass(frozen=True)
class _VerificationEvidence:
    entries: tuple[JournalEntry, ...]
    status: str
    accepted_baseline_matches: tuple[dict[str, Any], ...] = ()


def _collect_verification_evidence(
    entries: list[JournalEntry],
    *,
    after_sequence: int,
    before_sequence: int,
) -> _VerificationEvidence | None:
    verification_event = _latest_accepted_verification_event(
        entries, after_sequence=after_sequence, before_sequence=before_sequence
    )
    terminal_events = [
        entry
        for entry in entries
        if after_sequence < entry.sequence < before_sequence
        and entry.event_type in _VERIFICATION_TERMINAL_EVENTS
    ]
    if verification_event is not None:
        previous_terminal_sequence = max(
            (
                entry.sequence
                for entry in entries
                if after_sequence < entry.sequence < verification_event.sequence
                and entry.event_type in _VERIFICATION_TERMINAL_EVENTS
            ),
            default=after_sequence,
        )
        command_entries = tuple(
            entry
            for entry in entries
            if previous_terminal_sequence < entry.sequence < verification_event.sequence
            and entry.event_type == "verification_command_run"
        )
        if not command_entries:
            return None
        raw_matches = verification_event.payload.get("accepted_baseline_matches") or []
        return _VerificationEvidence(
            entries=command_entries,
            status=str(
                verification_event.payload.get("effective_status", "passed")
            ).strip().lower(),
            accepted_baseline_matches=tuple(
                dict(item) for item in raw_matches if isinstance(item, dict)
            ),
        )

    if terminal_events:
        return None

    # Legacy journals predate terminal verification events. Preserve their
    # original fail-closed contract: only an all-raw-passing command set can
    # be reconstructed as accepted evidence.
    command_entries = tuple(
        entry
        for entry in entries
        if after_sequence < entry.sequence < before_sequence
        and entry.event_type == "verification_command_run"
    )
    if not command_entries or not all(
        str(entry.payload.get("status", "")).strip().lower() == "passed"
        for entry in command_entries
    ):
        return None
    return _VerificationEvidence(entries=command_entries, status="passed")

def collect_acceptance_evidence(
    package: WorkPackage,
    entries: Iterable[JournalEntry],
    *,
    require_verification: bool,
    child_evidence: Iterable[dict[str, Any]] = (),
) -> AcceptanceEvidenceBundle | None:
    """Return validated evidence for missing criteria.

    The latest approved review must follow a complete passing verification set,
    and that verification set must follow the most recent implementation/fix
    result.  This ordering prevents stale tests or an earlier review cycle from
    satisfying a later mutation.

    Aggregate detection uses package metadata or graph-derived child evidence,
    not criterion naming conventions.
    """

    missing = [
        criterion
        for criterion in package.acceptance_criteria
        if not criterion.verified or not criterion.evidence.strip()
    ]
    if not missing:
        return None

    children = tuple(dict(item) for item in child_evidence)

    # Only auto-collect for aggregate packages.  The orchestrator supplies child
    # assessments for graph-defined aggregates that predate explicit shard_ids.
    if not _is_aggregate_package(package, entries, child_evidence=children):
        return None

    expected_children = set(package.shard_ids)
    evidenced_children = {
        str(item.get("package_id", "")).strip() for item in children
    }
    if (
        not children
        or (expected_children and evidenced_children != expected_children)
        or any(
            item.get("completed") is not True
            or item.get("commit_satisfied") is not True
            for item in children
        )
    ):
        return None

    package_entries = [
        entry
        for entry in entries
        if str(entry.payload.get("package_id", "")) == package.id
    ]
    review = next(
        (
            entry
            for entry in reversed(package_entries)
            if entry.event_type == "review_result"
            and str(entry.payload.get("verdict", "")).lower() == "approved"
        ),
        None,
    )
    if review is None:
        return None

    mutation_sequence = max(
        (
            entry.sequence
            for entry in package_entries
            if entry.sequence < review.sequence
            and entry.event_type == "agent_result_persisted"
            and str(entry.payload.get("capability", "")) in {"implement", "fix_review"}
        ),
        default=0,
    )
    verification = _collect_verification_evidence(
        package_entries,
        after_sequence=mutation_sequence,
        before_sequence=review.sequence,
    )
    if require_verification and verification is None:
        return None
    verification_entries = verification.entries if verification is not None else ()

    review_artifact_entry = next(
        (
            entry
            for entry in reversed(package_entries)
            if entry.sequence < review.sequence
            and entry.event_type == "agent_result_persisted"
            and str(entry.payload.get("capability", "")) == "review"
        ),
        None,
    )
    if review_artifact_entry is None:
        return None

    artifact = review_artifact_entry.payload.get("artifact")
    if not isinstance(artifact, dict) or not str(artifact.get("path", "")).strip():
        return None

    findings = tuple(
        str(item).strip()
        for item in (review.payload.get("findings") or [])
        if str(item).strip()
    )
    commands = tuple(
        {
            "repository_id": str(entry.payload.get("repository_id", "")),
            "command": str(entry.payload.get("command", "")),
            "status": str(entry.payload.get("status", "")),
            "returncode": int(entry.payload.get("returncode", 0)),
            "stdout_fingerprint": str(entry.payload.get("stdout_fingerprint", "")),
        }
        for entry in verification_entries
    )
    return AcceptanceEvidenceBundle(
        criterion_ids=tuple(item.id for item in missing),
        verification_profile=package.verification_profile,
        verification_commands=commands,
        verification_sequences=tuple(entry.sequence for entry in verification_entries),
        verification_status=(verification.status if verification is not None else "passed"),
        accepted_baseline_matches=(
            verification.accepted_baseline_matches if verification is not None else ()
        ),
        review_sequence=review.sequence,
        reviewer_id=str(review_artifact_entry.payload.get("agent_id", "")),
        review_stage=str(review_artifact_entry.payload.get("stage", "review")),
        review_artifact=dict(artifact),
        child_evidence=children,
        legacy_observations=findings,
    )



_VERIFICATION_TERMINAL_EVENTS = frozenset(
    {
        "verification_passed",
        "verification_passed_with_accepted_baseline",
        "verification_failed",
    }
)


def _latest_accepted_verification_event(
    entries: Iterable[JournalEntry],
    *,
    after_sequence: int,
    before_sequence: int,
) -> JournalEntry | None:
    """Return the latest effective verification success in one review window.

    Raw command failures are intentionally not interpreted here.  The
    orchestrator's terminal verification event is the durable control-plane
    decision that all commands were either successful or covered by an exact
    accepted baseline.  Restricting command evidence to the terminal event's
    own run also avoids rejecting a later successful retry because an earlier
    attempt failed in the same mutation/review window.
    """

    terminal = [
        entry
        for entry in entries
        if after_sequence < entry.sequence < before_sequence
        and entry.event_type in _VERIFICATION_TERMINAL_EVENTS
    ]
    if not terminal:
        return None
    latest = terminal[-1]
    if latest.event_type not in {
        "verification_passed",
        "verification_passed_with_accepted_baseline",
    }:
        return None
    if (
        latest.event_type == "verification_passed_with_accepted_baseline"
        and str(latest.payload.get("effective_status", "")).strip().lower()
        not in {"", PASSED_WITH_ACCEPTED_BASELINE}
    ):
        return None
    return latest

def _is_aggregate_package(
    package: WorkPackage,
    entries: Iterable[JournalEntry],
    *,
    child_evidence: Iterable[dict[str, Any]] = (),
) -> bool:
    """Determine aggregate status from durable package or graph-derived state."""
    del entries  # Retained for API compatibility with existing callers.
    return bool(
        package.execution_mode == "aggregate"
        or package.shard_ids
        or tuple(child_evidence)
    )


def _command_label(item: dict[str, Any]) -> str:
    repository = str(item.get("repository_id", "")).strip()
    command = str(item.get("command", "")).strip() or "unknown command"
    return f"{repository}:{command}" if repository else command
