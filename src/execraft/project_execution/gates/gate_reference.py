"""ProjectGate reference criterion evaluator."""

from .base import CriterionResult, GateEvaluationContext
from ..models import GateCriterion, GateState


class ProjectGateReferenceEvaluator:
    criterion_type = "project_gate"

    def evaluate(
        self,
        criterion: GateCriterion,
        context: GateEvaluationContext,
    ) -> CriterionResult:
        row = context.runtime.gates.get(criterion.gate_id, {})
        state = str(row.get("state", GateState.WAITING.value))
        current = str(row.get("input_fingerprint", ""))
        satisfied = self._satisfied(row, state=state, current=current)
        return CriterionResult(
            satisfied=satisfied,
            evidence={
                "gate_id": criterion.gate_id,
                "state": state,
                "evaluation_revision": row.get("evaluation_revision", 0),
                "input_fingerprint": current,
            },
            reason=(
                ""
                if satisfied
                else f"Gate {criterion.gate_id} is not currently satisfied"
            ),
        )

    @staticmethod
    def _satisfied(row, *, state: str, current: str) -> bool:
        if not current:
            return False
        if state == GateState.WAIVED.value:
            waiver = row.get("waiver") or {}
            return (
                isinstance(waiver, dict)
                and waiver.get("input_fingerprint") == current
            )
        if state != GateState.PASSED.value:
            return False
        evaluation = row.get("evaluation") or {}
        return (
            isinstance(evaluation, dict)
            and evaluation.get("input_fingerprint") == current
        )
