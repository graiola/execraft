"""Task artifact criterion evaluator."""

from .base import CriterionResult, GateEvaluationContext
from ..models import GateCriterion


class TaskArtifactEvaluator:
    criterion_type = "task_artifact"

    def evaluate(
        self,
        criterion: GateCriterion,
        context: GateEvaluationContext,
    ) -> CriterionResult:
        evidence = context.task_port.evidence(criterion.task_id)
        present = criterion.artifact_id in evidence.artifacts
        return CriterionResult(
            satisfied=present,
            evidence={
                "task_id": criterion.task_id,
                "artifact_id": criterion.artifact_id,
                "artifacts": list(evidence.artifacts),
            },
            reason=(
                ""
                if present
                else f"Task {criterion.task_id} has not produced artifact "
                f"{criterion.artifact_id}"
            ),
        )
