"""ALL-only ProjectGate evaluation with evidence-bound human decisions."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .base import (
    CriterionResult,
    GateEvaluationContext,
    ProjectGateEvaluator,
    canonical_fingerprint,
)
from .gate_reference import ProjectGateReferenceEvaluator
from .human_approval import HumanApprovalEvaluator
from .task_artifact import TaskArtifactEvaluator
from .task_completion import TaskCompletionEvaluator
from .task_verification import TaskVerificationEvaluator
from ..events import ProjectEventJournal
from ..models import (
    GateState,
    ProjectExecutionDefinition,
    ProjectExecutionError,
    ProjectGate,
)
from ..runtime_repository import ProjectExecutionRuntimeState
from ..task_port import TaskExecutionPort


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class GateEvaluationService:
    """Evaluate typed Gate criteria and preserve evidence-bound decisions."""

    def __init__(
        self,
        evaluators: tuple[ProjectGateEvaluator, ...] | None = None,
    ) -> None:
        defaults: tuple[ProjectGateEvaluator, ...] = (
            TaskCompletionEvaluator(),
            TaskVerificationEvaluator(),
            TaskArtifactEvaluator(),
            ProjectGateReferenceEvaluator(),
            HumanApprovalEvaluator(),
        )
        selected = evaluators or defaults
        self._evaluators = {evaluator.criterion_type: evaluator for evaluator in selected}

    def evaluate(
        self,
        gate: ProjectGate,
        *,
        definition: ProjectExecutionDefinition,
        runtime: ProjectExecutionRuntimeState,
        task_port: TaskExecutionPort,
        journal: ProjectEventJournal | None = None,
    ) -> dict[str, Any]:
        row = runtime.gates.setdefault(
            gate.id,
            {
                "state": GateState.WAITING.value,
                "evaluation_revision": 0,
                "decisions": [],
            },
        )
        if gate.id in runtime.cancelled_gates:
            row["state"] = GateState.CANCELLED.value
            return row

        automatic_results, fingerprint = self._automatic_evidence(
            gate,
            definition=definition,
            runtime=runtime,
            task_port=task_port,
        )
        row["input_fingerprint"] = fingerprint

        waiver = row.get("waiver") or {}
        if (
            isinstance(waiver, dict)
            and waiver.get("input_fingerprint") == fingerprint
        ):
            row["state"] = GateState.WAIVED.value
            return row

        if any(not result.satisfied for result in automatic_results):
            reasons = [
                result.reason
                for result in automatic_results
                if not result.satisfied and result.reason
            ]
            changed = self._store_evaluation(
                row,
                GateState.FAILED,
                fingerprint,
                automatic_results,
                reasons,
            )
            if changed and journal is not None:
                journal.append(
                    "project_gate_failed",
                    {
                        "gate_id": gate.id,
                        "input_fingerprint": fingerprint,
                        "reasons": reasons,
                    },
                )
            return row

        human_criterion = next(
            (criterion for criterion in gate.criteria if criterion.type == "human_approval"),
            None,
        )
        if human_criterion is None:
            changed = self._store_evaluation(
                row,
                GateState.PASSED,
                fingerprint,
                automatic_results,
                [],
            )
            if changed and journal is not None:
                journal.append(
                    "project_gate_passed",
                    {
                        "gate_id": gate.id,
                        "input_fingerprint": fingerprint,
                        "evaluation_revision": row["evaluation_revision"],
                    },
                )
            return row

        evaluator = self._evaluator("human_approval")
        human_result = evaluator.evaluate(
            human_criterion,
            GateEvaluationContext(
                definition=definition,
                runtime=runtime,
                task_port=task_port,
                gate_id=gate.id,
                evidence_fingerprint=fingerprint,
            ),
        )
        results = [*automatic_results, human_result]
        if human_result.awaiting_decision:
            state = GateState.AWAITING_DECISION
            event_type = "project_gate_decision_required"
        elif human_result.satisfied:
            state = GateState.PASSED
            event_type = "project_gate_passed"
        else:
            state = GateState.FAILED
            event_type = "project_gate_failed"

        reasons = [human_result.reason] if human_result.reason else []
        changed = self._store_evaluation(row, state, fingerprint, results, reasons)
        if changed and journal is not None:
            payload: dict[str, Any] = {
                "gate_id": gate.id,
                "input_fingerprint": fingerprint,
            }
            if reasons:
                payload["reasons"] = reasons
            if state == GateState.PASSED:
                payload["evaluation_revision"] = row["evaluation_revision"]
            journal.append(event_type, payload)
        return row

    def decide(
        self,
        gate_id: str,
        *,
        runtime: ProjectExecutionRuntimeState,
        actor: str,
        decision: str,
        reason: str = "",
        journal: ProjectEventJournal | None = None,
    ) -> dict[str, Any]:
        """Record an approval/rejection for the Gate's exact current evidence."""

        row = runtime.gates.get(gate_id)
        if not row:
            raise ProjectExecutionError(f"Gate {gate_id!r} has not been evaluated")
        if row.get("state") != GateState.AWAITING_DECISION.value:
            raise ProjectExecutionError(
                f"Gate {gate_id!r} is not awaiting a human decision"
            )

        fingerprint = str(row.get("input_fingerprint", ""))
        if not fingerprint:
            raise ProjectExecutionError(
                f"Gate {gate_id!r} has no current evidence fingerprint"
            )
        normalized = decision.strip().lower()
        if normalized not in {"approved", "rejected"}:
            raise ProjectExecutionError("Gate decision must be approved or rejected")
        if not actor.strip():
            raise ProjectExecutionError("Gate decision actor cannot be empty")

        record = {
            "actor": actor.strip(),
            "decision": normalized,
            "reason": reason.strip(),
            "timestamp": _now(),
            "input_fingerprint": fingerprint,
        }
        decisions = row.setdefault("decisions", [])
        if not isinstance(decisions, list):
            raise ProjectExecutionError("Gate decision history is invalid")
        decisions.append(record)

        if journal is not None:
            journal.append(
                (
                    "project_gate_approved"
                    if normalized == "approved"
                    else "project_gate_rejected"
                ),
                {"gate_id": gate_id, **record},
            )
        return record

    def waive(
        self,
        gate_id: str,
        *,
        runtime: ProjectExecutionRuntimeState,
        actor: str,
        reason: str,
        journal: ProjectEventJournal | None = None,
    ) -> dict[str, Any]:
        """Waive a Gate for its exact current evidence fingerprint."""

        row = runtime.gates.get(gate_id)
        if not row or not row.get("input_fingerprint"):
            raise ProjectExecutionError(
                f"Gate {gate_id!r} must be evaluated before waiver"
            )
        if gate_id in runtime.cancelled_gates:
            raise ProjectExecutionError(f"Gate {gate_id!r} is cancelled")
        if not actor.strip() or not reason.strip():
            raise ProjectExecutionError("Gate waiver requires actor and reason")

        waiver = {
            "actor": actor.strip(),
            "reason": reason.strip(),
            "timestamp": _now(),
            "input_fingerprint": str(row["input_fingerprint"]),
        }
        row["waiver"] = waiver
        row["state"] = GateState.WAIVED.value
        if journal is not None:
            journal.append("project_gate_waived", {"gate_id": gate_id, **waiver})
        return waiver

    def _automatic_evidence(
        self,
        gate: ProjectGate,
        *,
        definition: ProjectExecutionDefinition,
        runtime: ProjectExecutionRuntimeState,
        task_port: TaskExecutionPort,
    ) -> tuple[list[CriterionResult], str]:
        results: list[CriterionResult] = []
        evidence_rows: list[dict[str, Any]] = []
        for criterion in gate.criteria:
            if criterion.type == "human_approval":
                continue
            result = self._evaluator(criterion.type).evaluate(
                criterion,
                GateEvaluationContext(
                    definition=definition,
                    runtime=runtime,
                    task_port=task_port,
                    gate_id=gate.id,
                ),
            )
            results.append(result)
            evidence_rows.append(
                {
                    "criterion": criterion.as_mapping(),
                    "evidence": dict(result.evidence),
                }
            )

        # Include the complete criterion definition (including human approval)
        # so changing the control contract invalidates prior human decisions.
        fingerprint = canonical_fingerprint(
            {
                "gate_id": gate.id,
                "criteria": [criterion.as_mapping() for criterion in gate.criteria],
                "automatic_evidence": evidence_rows,
            }
        )
        return results, fingerprint

    def _evaluator(self, criterion_type: str) -> ProjectGateEvaluator:
        evaluator = self._evaluators.get(criterion_type)
        if evaluator is None:
            raise ProjectExecutionError(
                f"no evaluator registered for {criterion_type}"
            )
        return evaluator

    @staticmethod
    def _store_evaluation(
        row: dict[str, Any],
        state: GateState,
        fingerprint: str,
        results: list[CriterionResult],
        reasons: list[str],
    ) -> bool:
        evidence = [dict(result.evidence) for result in results]
        previous = row.get("evaluation") or {}
        if not isinstance(previous, dict):
            previous = {}
        unchanged = (
            row.get("state") == state.value
            and previous.get("input_fingerprint") == fingerprint
            and previous.get("evidence") == evidence
            and previous.get("reasons") == reasons
        )
        if unchanged:
            return False

        revision = int(row.get("evaluation_revision", 0)) + 1
        row["evaluation_revision"] = revision
        row["state"] = state.value
        row["evaluation"] = {
            "revision": revision,
            "outcome": state.value,
            "evaluated_at": _now(),
            "input_fingerprint": fingerprint,
            "evidence": evidence,
            "reasons": reasons,
        }
        return True
