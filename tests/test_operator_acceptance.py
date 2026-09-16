from __future__ import annotations

import json

import pytest

from execraft.orchestrate import (
    AcceptanceCriterion,
    OrchestrateError,
    OrchestrationConfig,
    ProjectOrchestrator,
    TaskExecutionState,
    WorkPackage,
    WorkPackageStage,
    normalize_work_packages,
)
from execraft.orchestrate.models import WorkPackage as WorkPackageModel


def _human_required_orchestrator(tmp_path):
    orch = ProjectOrchestrator(
        "operator-acceptance-task",
        config=OrchestrationConfig(state_dir=tmp_path / "state"),
        workspace_root=tmp_path,
    )
    graph, report = normalize_work_packages(
        [
            WorkPackage(
                id="WP24",
                title="Simulation acceptance vertical slices",
                requirements=["Exercise representative integration slices"],
                acceptance_criteria=[
                    AcceptanceCriterion(
                        id="wp24_exit_criteria",
                        description="All documented WP24 exit criteria are satisfied with durable evidence",
                    )
                ],
            )
        ]
    )
    orch.initialize_graph(graph, report)
    package = orch._state_record.plan_graph.package_by_id("WP24")
    package.stage = WorkPackageStage.FINAL_REVIEW
    package.status = "human_required"
    package.review_findings = [
        "WP24-FR-003 [critical] real Gazebo/PX4 acceptance evidence is missing"
    ]
    orch._state_record.state = TaskExecutionState.HUMAN_REQUIRED
    orch.save_state()
    entry = orch._journal.append(
        "human_intervention_required",
        {
            "package_id": "WP24",
            "stage": "final_review",
            "blocked_requirement": "review/fix cycle budget exhausted",
            "evidence": [
                "real Gazebo/PX4 acceptance was not executed in the current environment"
            ],
            "impact": "WP24 cannot satisfy its real-simulation exit criterion",
            "recommended_decision": "run the external acceptance campaign or explicitly disposition the risk",
        },
    )
    return orch, package, entry.sequence


def test_operator_risk_acceptance_advances_late_check_without_faking_verification(tmp_path, monkeypatch):
    orch, package, sequence = _human_required_orchestrator(tmp_path)

    preview = orch.operator_risk_acceptance_report("WP24")
    assert preview["available"] is True
    assert preview["sequence"] == sequence
    assert preview["unverified_criteria"][0]["id"] == "wp24_exit_criteria"

    result = orch.accept_operator_risk(
        "WP24",
        reason="Real Gazebo/PX4 validation is deferred to the host acceptance campaign.",
        expected_sequence=sequence,
    )

    assert result["project_state"] == "running"
    assert result["next_stage"] == "ready_to_commit"
    assert package.stage == WorkPackageStage.READY_TO_COMMIT
    assert package.status == "pending"
    assert package.operator_risk_acceptance["status"] == "operator_accepted"
    assert package.operator_risk_acceptance["accepted_criterion_ids"] == [
        "wp24_exit_criteria"
    ]
    criterion = package.acceptance_criteria[0]
    assert criterion.verified is False
    assert "criterion remains unverified" in criterion.evidence
    assert package.review_findings

    # The package-scoped disposition satisfies only the framework's blocking
    # decision. It does not mutate the criterion into a passed verification.
    orch._validate_acceptance_evidence(package)

    # Normal orchestration can now select the package again and finalize it.
    # This unit fixture intentionally has no repository manifest, so replace only
    # the commit transaction boundary; scheduling/stage dispatch remain real.
    monkeypatch.setattr(
        orch,
        "_finalize_standard_package",
        lambda current: orch._mark_package_completed(current, transaction_id=None),
    )
    orch.run_pipeline()
    assert package.stage == WorkPackageStage.COMPLETED
    assert orch.state == TaskExecutionState.COMPLETED
    assert criterion.verified is False

    events = orch._journal.read()
    accepted = [item for item in events if item.event_type == "operator_risk_accepted"]
    assert accepted
    assert accepted[-1].payload["human_action_sequence"] == sequence


def test_operator_risk_acceptance_rejects_stale_action_sequence(tmp_path):
    orch, _, sequence = _human_required_orchestrator(tmp_path)

    with pytest.raises(OrchestrateError, match="changed after it was previewed"):
        orch.accept_operator_risk(
            "WP24",
            reason="Defer host simulation validation.",
            expected_sequence=sequence + 1,
        )


def test_operator_risk_acceptance_cannot_bypass_non_late_package_stage(tmp_path):
    orch, package, _ = _human_required_orchestrator(tmp_path)
    package.stage = WorkPackageStage.IMPLEMENT
    orch.save_state()

    preview = orch.operator_risk_acceptance_report("WP24")

    assert preview["available"] is False
    assert "persisted package stage" in preview["unavailable_reason"]


def test_operator_risk_acceptance_round_trips_in_package_state():
    package = WorkPackage(
        id="WP24",
        title="Acceptance",
        operator_risk_acceptance={
            "status": "operator_accepted",
            "package_id": "WP24",
            "decision_id": "operator-risk-1234",
            "accepted_criterion_ids": ["exit"],
        },
    )

    restored = WorkPackageModel.from_mapping(json.loads(json.dumps(package.as_mapping())))

    assert restored.operator_risk_acceptance == package.operator_risk_acceptance
