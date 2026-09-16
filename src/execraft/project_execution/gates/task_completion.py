"""Task completion criterion evaluator."""

from .base import CriterionResult, GateEvaluationContext
from ..models import GateCriterion
from ..task_port import TaskOutcome


class TaskCompletionEvaluator:
    criterion_type = "task_completion"

    def evaluate(
        self,
        criterion: GateCriterion,
        context: GateEvaluationContext,
    ) -> CriterionResult:
        outcome = context.task_port.outcome(criterion.task_id)
        satisfied = outcome == TaskOutcome.COMPLETED
        return CriterionResult(
            satisfied=satisfied,
            evidence={"task_id": criterion.task_id, "outcome": outcome.value},
            reason=(
                ""
                if satisfied
                else f"Task {criterion.task_id} outcome is {outcome.value}"
            ),
        )
