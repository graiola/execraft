"""Task verification criterion evaluator."""

from .base import CriterionResult, GateEvaluationContext
from ..models import GateCriterion


class TaskVerificationEvaluator:
    criterion_type = "task_verification"

    def evaluate(
        self,
        criterion: GateCriterion,
        context: GateEvaluationContext,
    ) -> CriterionResult:
        evidence = context.task_port.evidence(criterion.task_id)
        actual = evidence.verification.outcome
        expected = criterion.outcome or "passed"
        satisfied = actual == expected
        return CriterionResult(
            satisfied=satisfied,
            evidence={
                "task_id": criterion.task_id,
                "verification": {
                    "outcome": actual,
                    "passed": evidence.verification.passed,
                    "failed": evidence.verification.failed,
                    "total": evidence.verification.total,
                },
            },
            reason=(
                ""
                if satisfied
                else f"Task {criterion.task_id} verification is {actual}, "
                f"expected {expected}"
            ),
        )
