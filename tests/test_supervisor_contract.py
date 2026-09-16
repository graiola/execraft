from __future__ import annotations

import pytest

from execraft.orchestrate.supervisor import SupervisorDecisionKind
from execraft.orchestrate.supervisor_contract import compile_supervisor_decision


def _compile(payload, *, candidates=()):
    return compile_supervisor_decision(payload, candidate_paths=candidates)


def test_minimal_decision_accepts_unknown_advisory_fields():
    compiled = _compile(
        {
            "decision": "recover_and_continue",
            "summary": "The repository state is coherent.",
            "novel_future_field": {"anything": "is advisory"},
        }
    )

    assert compiled.decision.decision == SupervisorDecisionKind.RESOLVED
    assert compiled.decision.summary == "The repository state is coherent."


def test_duplicate_acceptance_evidence_is_deduplicated_or_merged():
    compiled = _compile(
        {
            "decision": "resolved",
            "summary": "Evidence checked.",
            "acceptance_evidence": [
                {"criterion_id": "conflicts_resolved", "evidence": "tree A"},
                {"criterion_id": "conflicts_resolved", "evidence": "tree A"},
                {"criterion_id": "conflicts_resolved", "evidence": "parent B"},
            ],
        }
    )

    evidence = compiled.decision.acceptance_evidence["conflicts_resolved"]
    assert "tree A" in evidence
    assert "parent B" in evidence
    assert any("duplicate acceptance criterion" in warning for warning in compiled.warnings)


def test_repository_slash_path_is_repaired_against_incident_candidates():
    candidate = "worker_b:__pycache__/test_edge.cpython-310.pyc"
    compiled = _compile(
        {
            "decision": "resolved",
            "summary": "Generated cache was removed.",
            "discard_paths": [
                "worker_b/__pycache__/test_edge.cpython-310.pyc"
            ],
        },
        candidates=(candidate,),
    )

    assert compiled.decision.discard_paths == (candidate,)
    assert compiled.decision.retain_paths == ()


def test_invalid_optional_path_is_dropped_without_losing_decision():
    compiled = _compile(
        {
            "decision": "resolved",
            "summary": "Continue with the validated workspace.",
            "discard_paths": ["not/a/known/repository/path"],
        },
        candidates=("repo:src/real.cpp",),
    )

    assert compiled.decision.decision == SupervisorDecisionKind.RESOLVED
    assert compiled.decision.discard_paths == ()
    assert compiled.decision.retain_paths == ("repo:src/real.cpp",)
    assert any("ignored" in warning for warning in compiled.warnings)
    assert any("inferred as retained" in warning for warning in compiled.warnings)


def test_overlapping_path_lists_are_normalized_with_retain_precedence():
    compiled = _compile(
        {
            "decision": "resolved",
            "summary": "Keep the repaired file.",
            "retain_paths": ["repo:file.txt"],
            "discard_paths": ["repo:file.txt"],
        },
        candidates=("repo:file.txt",),
    )

    assert compiled.decision.retain_paths == ("repo:file.txt",)
    assert compiled.decision.discard_paths == ()
    assert any("treated as retained" in warning for warning in compiled.warnings)


def test_freeform_supervisor_decision_is_first_class():
    compiled = _compile(
        {
            "final_message": """
DECISION: recover and continue
CLASSIFICATION: state_inconsistency
RESUME_STAGE: regression_verify
SUMMARY: Merge metadata was repaired and the validated tree is unchanged.

- Rerun verification for changed repositories.
- Preserve orchestrator ownership of the merge commit.
"""
        }
    )

    assert compiled.source == "freeform"
    assert compiled.decision.decision == SupervisorDecisionKind.RESOLVED
    assert compiled.decision.resume_stage.value == "regression_verify"
    assert compiled.decision.actions_taken == (
        "Rerun verification for changed repositories.",
        "Preserve orchestrator ownership of the merge commit.",
    )


def test_high_impact_replan_becomes_operator_approval_not_schema_failure():
    compiled = _compile(
        {
            "decision": "replan",
            "summary": "The remaining task graph should be replaced.",
            "instructions": ["Split the remaining work into two packages."],
        }
    )

    assert compiled.decision.decision == SupervisorDecisionKind.ASK_HUMAN
    assert compiled.decision.human_question is not None
    assert compiled.decision.human_question.options[0].id == "approve_direction"
    assert all(option.weight is None for option in compiled.decision.human_question.options)
    assert any("high-impact" in warning for warning in compiled.warnings)


def test_malformed_human_weights_are_removed_so_auto_selection_fails_closed():
    compiled = _compile(
        {
            "decision": "ask_human",
            "summary": "Operator choice is required.",
            "human_question": {
                "question": "Choose recovery.",
                "recommended_option": "a",
                "options": [
                    {"id": "a", "label": "A", "weight": 90, "risk": "routine"},
                    {"id": "b", "label": "B", "weight": 90, "risk": "routine"},
                ],
            },
        }
    )

    assert compiled.decision.human_question is not None
    assert all(option.weight is None for option in compiled.decision.human_question.options)


def test_unknown_classification_is_advisory_and_normalizes_to_unknown():
    compiled = _compile(
        {
            "decision": "resolved",
            "classification": "brand_new_incident_class",
            "summary": "Recovered.",
        }
    )

    assert compiled.decision.classification.value == "unknown"
    assert any("normalized to 'unknown'" in warning for warning in compiled.warnings)


def test_unrecognizable_prose_is_semantically_invalid():
    with pytest.raises(ValueError, match="no usable semantic decision"):
        _compile({"final_message": "I inspected several files and have no conclusion."})


def test_ask_human_survives_advisory_discard_paths_with_empty_candidate_set():
    """Path hints must not downgrade a valid ask_human to a free-form block.

    Scope cleanup routinely empties the incident candidate set before the
    decision is compiled. That used to let advisory discard_paths through
    normalization and trip the strict `resolved`-only rule in
    SupervisorDecision.from_mapping, whose ValueError is indistinguishable
    from a parse failure -- so the decision silently became `blocked`.
    """
    compiled = _compile(
        {
            "decision": "ask_human",
            "classification": "test_failure",
            "summary": "One environmental discovery timeout remains.",
            "discard_paths": ["worker_b:__pycache__"],
            "human_question": {
                "prompt": "How should we clear the timeout?",
                "options": [
                    {"id": "rerun", "label": "Re-run verification."},
                    {"id": "cancel", "label": "Replan the package."},
                ],
            },
        },
        candidates=(),
    )

    assert compiled.source == "embedded_json" or compiled.source == "mapping"
    assert compiled.decision.decision == SupervisorDecisionKind.ASK_HUMAN
    assert compiled.decision.discard_paths == ()
    assert compiled.decision.human_question is not None
    assert any("not 'resolved'" in warning for warning in compiled.warnings)
