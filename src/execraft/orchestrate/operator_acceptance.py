"""Auditable operator acceptance for late, explicitly acknowledged risk checks.

This is intentionally distinct from verification-baseline acceptance. A
baseline says a known verification failure is acceptable while its exact
fingerprint remains stable. Operator risk acceptance instead disposes one
specific HUMAN_REQUIRED escalation so a package can finish even though a
late review/acceptance requirement remains unverified.

The mechanism is deliberately narrow: only final review, full verification,
or ready-to-commit acceptance-evidence checks are eligible. Repository scope,
provider failures, invalid agent output, and commit/repository-sync failures
cannot be bypassed through this path.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from .models import OrchestrateError, TaskExecutionState, WorkPackage, WorkPackageStage, utc_now
from .supervisor import IncidentStatus


ELIGIBLE_OPERATOR_ACCEPTANCE_STAGES = frozenset(
    {
        WorkPackageStage.FINAL_REVIEW.value,
        WorkPackageStage.FULL_VERIFY.value,
        WorkPackageStage.READY_TO_COMMIT.value,
    }
)

_BLOCKED_REASON_MARKERS = (
    "review/fix cycle budget exhausted",
    "external acceptance action required",
    "acceptance evidence missing",
)

_FORBIDDEN_MARKERS = (
    "repository_sync:",
    "repository-sync",
    "write_scope",
    "repository scope",
    "clean-start check",
    "invalid structured agent output",
    "no configured available agent",
    "could not be completed by any available agent",
    "commit transaction",
)


def _normalized_text(value: object) -> str:
    return " ".join(str(value or "").split()).casefold()


def operator_acceptance_eligibility(
    action: Mapping[str, Any] | None,
    package: WorkPackage | None,
) -> tuple[bool, str]:
    """Return whether the active escalation may be explicitly accepted.

    Eligibility is intentionally conservative. A reviewer/external acceptance
    check may be waived by an informed operator, but framework integrity checks
    and incomplete implementation/provider failures must still be repaired.
    """

    if not action or package is None:
        return False, "no active package-scoped human action is available"
    if package.stage == WorkPackageStage.COMPLETED:
        return False, "the package is already completed"

    stage = _normalized_text(action.get("stage") or package.stage.value)
    if stage not in ELIGIBLE_OPERATOR_ACCEPTANCE_STAGES:
        return False, "only final review, full verification, or acceptance-evidence checks can be accepted"
    if package.stage.value not in ELIGIBLE_OPERATOR_ACCEPTANCE_STAGES:
        return False, "the persisted package stage is not a late review/acceptance check"

    package_id = str(action.get("package_id", "")).strip()
    if package_id and package_id != package.id:
        return False, "the active human action belongs to a different package"

    evidence = action.get("evidence") or []
    if isinstance(evidence, (str, bytes)):
        evidence_text = str(evidence)
    else:
        evidence_text = " ".join(str(item) for item in evidence)
    combined = _normalized_text(
        " ".join(
            [
                str(action.get("reason", "")),
                str(action.get("recommended_decision", "")),
                evidence_text,
            ]
        )
    )
    if any(marker in combined for marker in _FORBIDDEN_MARKERS):
        return False, "this check protects repository/provider/framework integrity and cannot be waived"

    if not any(marker in combined for marker in _BLOCKED_REASON_MARKERS):
        # Late-stage human-decision holds may come from older journals whose reason was
        # generic. Require durable review findings as the fallback signal.
        if not package.review_findings:
            return False, "the active check is not a recognized late review/acceptance blocker"

    return True, ""


def accepted_criterion_ids(package: WorkPackage) -> frozenset[str]:
    """Return criterion IDs covered by the package's active risk acceptance."""

    record = package.operator_risk_acceptance or {}
    if str(record.get("status", "")).strip() != "operator_accepted":
        return frozenset()
    if str(record.get("package_id", "")).strip() != package.id:
        return frozenset()
    values = record.get("accepted_criterion_ids") or []
    if not isinstance(values, list):
        return frozenset()
    return frozenset(str(item).strip() for item in values if str(item).strip())


def operator_acceptance_preview(
    action: Mapping[str, Any], package: WorkPackage
) -> dict[str, Any]:
    """Build the stable GUI/CLI preview for one eligible active escalation."""

    eligible, reason = operator_acceptance_eligibility(action, package)
    criteria = [
        {
            "id": criterion.id,
            "description": criterion.description,
            "verified": bool(criterion.verified),
            "evidence": criterion.evidence,
        }
        for criterion in package.acceptance_criteria
        if not criterion.verified or not criterion.evidence.strip()
    ]
    evidence = action.get("evidence") or []
    if isinstance(evidence, (str, bytes)):
        evidence = [str(evidence)]
    else:
        evidence = [str(item) for item in evidence if str(item).strip()]
    return {
        "available": eligible,
        "unavailable_reason": reason,
        "package_id": package.id,
        "package_title": package.title,
        "stage": str(action.get("stage") or package.stage.value),
        "sequence": action.get("sequence"),
        "blocked_requirement": str(action.get("reason", "")).strip(),
        "impact": str(action.get("impact", "")).strip(),
        "recommended_decision": str(action.get("recommended_decision", "")).strip(),
        "evidence": evidence,
        "unverified_criteria": criteria,
        "review_findings": list(package.review_findings),
        "artifact": dict(action.get("artifact") or {}),
        "disclaimer": (
            "Accepting records an operator-approved deferral. It does not mark "
            "verification, review findings, or acceptance criteria as passed."
        ),
    }



def operator_risk_acceptance_report(host: Any, package_id: str = "") -> dict[str, Any]:
    """Return a bounded preview for explicitly accepting the active late human-decision hold."""

    action = host.human_required_report()
    if action is None:
        return {
            "available": False,
            "unavailable_reason": "the project is not waiting for operator action",
            "package_id": str(package_id).strip(),
        }
    active_package_id = str(action.get("package_id", "")).strip()
    requested = str(package_id).strip()
    if requested and requested != active_package_id:
        return {
            "available": False,
            "unavailable_reason": "the requested package is not the active human-required package",
            "package_id": requested,
            "sequence": action.get("sequence"),
        }
    if not active_package_id:
        return {
            "available": False,
            "unavailable_reason": "the active human action is not package-scoped",
            "package_id": requested,
            "sequence": action.get("sequence"),
        }
    try:
        package = host._state_record.plan_graph.package_by_id(active_package_id)
    except OrchestrateError:
        return {
            "available": False,
            "unavailable_reason": "the active package no longer exists in the plan graph",
            "package_id": active_package_id,
            "sequence": action.get("sequence"),
        }
    return operator_acceptance_preview(action, package)


def accept_operator_risk(
    host: Any,
    package_id: str,
    *,
    reason: str,
    expected_sequence: int | None = None,
) -> dict[str, Any]:
    """Disposition one exact late review/acceptance check and resume safely.

    The package remains scheduler-visible and all unverified facts remain
    unverified. The durable acceptance record is the sole authorization for
    the READY_TO_COMMIT evidence check to treat those criteria as dispositioned.
    """

    package_id = str(package_id).strip()
    decision_reason = str(reason).strip()
    if not package_id:
        raise OrchestrateError("operator risk acceptance requires a package ID")
    if not decision_reason:
        raise OrchestrateError("operator risk acceptance requires a non-empty reason")
    if len(decision_reason) > 4000:
        raise OrchestrateError("operator risk acceptance reason exceeds 4000 characters")

    with host.exclusive_driver_lock():
        with host._exclusive_run_lock():
            if host.state != TaskExecutionState.HUMAN_REQUIRED:
                raise OrchestrateError(
                    "operator risk acceptance is valid only while the project is human_required"
                )
            action = host.human_required_report()
            if action is None:
                raise OrchestrateError("no active human-required action is available")
            sequence = action.get("sequence")
            if expected_sequence is not None and sequence != expected_sequence:
                raise OrchestrateError(
                    "the human-required action changed after it was previewed; refresh before accepting"
                )
            active_package_id = str(action.get("package_id", "")).strip()
            if active_package_id != package_id:
                raise OrchestrateError(
                    f"active human-required package is {active_package_id!r}, not {package_id!r}"
                )
            package = host._state_record.plan_graph.package_by_id(package_id)
            eligible, unavailable_reason = operator_acceptance_eligibility(action, package)
            if not eligible:
                raise OrchestrateError(
                    "this human-decision hold cannot be operator-accepted: " + unavailable_reason
                )

            accepted_ids = [
                item.id
                for item in package.acceptance_criteria
                if not item.verified or not item.evidence.strip()
            ]
            decision_id = f"operator-risk-{uuid.uuid4().hex[:16]}"
            record = {
                "schema_version": 1,
                "decision_id": decision_id,
                "status": "operator_accepted",
                "package_id": package.id,
                "accepted_by": "operator",
                "accepted_at": utc_now(),
                "reason": decision_reason,
                "human_action_sequence": sequence,
                "source_stage": package.stage.value,
                "blocked_requirement": str(action.get("reason", "")).strip(),
                "accepted_criterion_ids": accepted_ids,
                "review_findings": list(package.review_findings),
                "evidence": list(action.get("evidence") or []),
                "artifact": dict(action.get("artifact") or {}),
                "disposition": "deferred_unverified_acceptance",
            }
            package.operator_risk_acceptance = record
            # ``status`` is scheduler lifecycle state; the risk decision lives
            # in its dedicated record and must not make READY_TO_COMMIT invisible.
            package.status = "pending"
            for criterion in package.acceptance_criteria:
                if criterion.id not in accepted_ids or criterion.evidence.strip():
                    continue
                criterion.evidence = (
                    f"Operator-accepted deferral {decision_id}; criterion remains "
                    f"unverified. Reason: {decision_reason}"
                )

            if package.stage != WorkPackageStage.READY_TO_COMMIT:
                host._advance_package_stage(package, WorkPackageStage.READY_TO_COMMIT)
            else:
                host.save_state(reason="operator_risk_acceptance")

            incident = host._scope_recovery_coordinator.supervisor_recovery_incident()
            if incident is not None and incident.package_id == package.id:
                incident.pending_delegations = []
                incident.pending_delegation_index = 0
                incident.human_question = {}
                incident.human_answer = {}
                incident.summary = (
                    "Superseded by an explicit package-scoped operator risk acceptance."
                )
                incident.actions_taken.append(
                    f"operator accepted deferred risk via {decision_id}"
                )
                incident.touch(status=IncidentStatus.RESOLVED)
                host._supervisor_incidents.save(incident)

            host._journal.append("operator_risk_accepted", record)
            host._emit_progress(
                "operator_risk_accepted",
                package_id=package.id,
                decision_id=decision_id,
                accepted_criteria=accepted_ids,
                source_stage=record["source_stage"],
            )
            host.transition_to(TaskExecutionState.RUNNING)
            return {
                **record,
                "next_stage": package.stage.value,
                "project_state": host.state.value,
            }


class OperatorAcceptanceFlow:
    """Thin orchestrator mixin exposing the operator-acceptance service."""

    def operator_risk_acceptance_report(self, package_id: str = "") -> dict[str, Any]:
        return operator_risk_acceptance_report(self, package_id)

    def accept_operator_risk(
        self, package_id: str, *, reason: str, expected_sequence: int | None = None
    ) -> dict[str, Any]:
        return accept_operator_risk(
            self, package_id, reason=reason, expected_sequence=expected_sequence
        )
