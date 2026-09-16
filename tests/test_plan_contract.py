"""Declarative PLAN.graph compatibility and safety contracts."""

from __future__ import annotations

import pytest

from execraft.plan_contract import (
    DeclarativePlanGraphError,
    canonicalize_legacy_plan_graph_mapping,
    validate_declarative_plan_graph_mapping,
)


def test_legacy_projection_removes_only_runtime_owned_fields() -> None:
    raw = {
        "schema_version": 1,
        "work_packages": [
            {
                "id": "M00",
                "title": "Historical milestone",
                "dependencies": [],
                "requirements": ["Preserve behavior"],
                "acceptance_criteria": [
                    {
                        "id": "done",
                        "description": "Done",
                        "verified": True,
                        "evidence": "historical evidence",
                    }
                ],
                "affected_repositories": ["core"],
                "stage": "completed",
                "status": "completed",
                "generated_by": "legacy-planner",
                "risk": "low",
                "priority": 10,
                "verification_profile": "focused",
            }
        ],
    }

    result = canonicalize_legacy_plan_graph_mapping(raw)
    package = result.graph["work_packages"][0]

    assert result.changed is True
    assert result.legacy_completed_package_ids == ("M00",)
    assert "stage" not in package
    assert "status" not in package
    assert "generated_by" not in package
    assert package["acceptance_criteria"] == [
        {"id": "done", "description": "Done"}
    ]
    # Compatibility projection must not mutate the historical source object.
    assert raw["work_packages"][0]["stage"] == "completed"
    validate_declarative_plan_graph_mapping(result.graph)


def test_legacy_projection_preserves_unknown_fields_for_strict_rejection() -> None:
    raw = {
        "work_packages": [
            {
                "id": "WP01",
                "stage": "prepare",
                "future_semantics": {"must_not_be_silently_dropped": True},
            }
        ]
    }

    projected = canonicalize_legacy_plan_graph_mapping(raw).graph

    assert "stage" not in projected["work_packages"][0]
    assert "future_semantics" in projected["work_packages"][0]
    with pytest.raises(
        DeclarativePlanGraphError,
        match="non-declarative/runtime fields: future_semantics",
    ):
        validate_declarative_plan_graph_mapping(projected)
