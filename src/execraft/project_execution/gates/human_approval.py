"""Evidence-bound human approval criterion evaluator."""

from .base import CriterionResult, GateEvaluationContext
from ..models import GateCriterion


class HumanApprovalEvaluator:
    criterion_type = "human_approval"

    def evaluate(
        self,
        criterion: GateCriterion,
        context: GateEvaluationContext,
    ) -> CriterionResult:
        del criterion  # The v1 human criterion has no additional parameters.
        row = context.runtime.gates.get(context.gate_id, {})
        decisions = row.get("decisions", [])
        matching = [
            decision
            for decision in decisions
            if isinstance(decision, dict)
            and decision.get("input_fingerprint") == context.evidence_fingerprint
        ]
        latest = matching[-1] if matching else {}
        decision = str(latest.get("decision", ""))
        evidence = {
            "decision": decision or "pending",
            "actor": latest.get("actor", ""),
            "input_fingerprint": context.evidence_fingerprint,
        }
        if decision == "approved":
            return CriterionResult(True, evidence)
        if decision == "rejected":
            return CriterionResult(False, evidence, "human approval rejected")
        return CriterionResult(
            False,
            evidence,
            "human approval required",
            awaiting_decision=True,
        )
