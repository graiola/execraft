from types import SimpleNamespace

import pytest

from execraft.gui.dashboard_presenter import (
    active_assignments,
    agent_for_stage,
    agent_nodes,
    primary_execution_context,
    role_for_stage,
)
from execraft.orchestrate.models import WorkPackage, WorkPackageStage


@pytest.mark.parametrize(
    ("stage", "role"),
    [
        (WorkPackageStage.DECOMPOSE, "planner"),
        (WorkPackageStage.IMPLEMENT, "implementer"),
        (WorkPackageStage.REVIEW, "reviewer"),
        (WorkPackageStage.FIX_REVIEW, "fixer"),
        (WorkPackageStage.FINAL_REVIEW, "reviewer"),
    ],
)
def test_role_for_stage_preserves_operator_role_names(stage, role):
    assert role_for_stage(stage) == role


def test_agent_for_stage_uses_stage_specific_durable_identity():
    package = WorkPackage(
        id="WP1",
        title="Update",
        decomposition_agent_id="planner-a",
        agent_id="impl-a",
        reviewer_id="review-a",
        final_reviewer_id="final-a",
        last_fixer_id="fix-a",
    )

    expected = {
        WorkPackageStage.DECOMPOSE: "planner-a",
        WorkPackageStage.IMPLEMENT: "impl-a",
        WorkPackageStage.REVIEW: "final-a",
        WorkPackageStage.FINAL_REVIEW: "final-a",
        WorkPackageStage.FIX_REVIEW: "fix-a",
    }
    for stage, agent_id in expected.items():
        package.stage = stage
        assert agent_for_stage(package) == agent_id


def test_live_invocation_overrides_persisted_assignment_projection():
    package = WorkPackage(
        id="WP1",
        title="Update",
        stage=WorkPackageStage.IMPLEMENT,
        status="running",
        agent_id="persisted-agent",
    )

    rows = active_assignments(
        [package],
        {},
        live_invocations=[
            {
                "package_id": "WP1",
                "stage": "implement",
                "agent_id": "live-agent",
                "invocation_id": "inv-1",
                "started_at": "2026-09-08T12:00:00Z",
            }
        ],
    )

    assert len(rows) == 1
    assert rows[0]["source"] == "invocation"
    assert rows[0]["agent_id"] == "live-agent"
    assert rows[0]["invocation_id"] == "inv-1"


def test_primary_execution_context_keeps_legacy_shape_and_invocation_fields():
    result = primary_execution_context(
        [
            {
                "package_id": "WP1",
                "stage": "implement",
                "agent_id": "agent-a",
                "status": "running",
                "source": "invocation",
                "invocation_id": "inv-1",
                "started_at": "now",
                "model": "qwen",
                "ignored": "presentation-only",
            }
        ]
    )

    assert result == {
        "package_id": "WP1",
        "stage": "implement",
        "agent_id": "agent-a",
        "status": "running",
        "source": "invocation",
        "invocation_id": "inv-1",
        "started_at": "now",
        "model": "qwen",
    }


def test_agent_nodes_retains_unknown_compatibility_endpoint_without_secrets():
    registry = SimpleNamespace(endpoints=[])
    agents = [
        {
            "id": "legacy-agent",
            "endpoint": {
                "id": "legacy-node",
                "target_kind": "satellite_inference",
                "provider_family": "openai-compatible",
                "url": "http://example.invalid",
                "credential_ref": "must-not-propagate",
            },
        }
    ]

    nodes = {row["id"]: row for row in agent_nodes(agents, registry, {})}

    assert nodes["legacy-node"]["agents"] == ["legacy-agent"]
    assert nodes["legacy-node"]["target_kind"] == "satellite_inference"
    assert "credential_ref" not in nodes["legacy-node"]
