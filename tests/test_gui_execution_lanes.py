from __future__ import annotations

import json

from execraft.gui.execution_lanes import build_execution_lane_views, execution_lane_mappings


ROLES = [
    {"id": "decompose", "label": "Decompose", "capability": "decompose"},
    {"id": "implement", "label": "Implement", "capability": "implement"},
    {"id": "review", "label": "Review", "capability": "review"},
    {"id": "fix_review", "label": "Fix review", "capability": "fix_review"},
    {"id": "final_review", "label": "Final review", "capability": "review"},
]


def _profile(
    profile_id: str,
    capability: str,
    *,
    health: str = "healthy",
    available: bool = True,
    enabled: bool = True,
    supported: bool = True,
) -> dict[str, object]:
    return {
        "id": profile_id,
        "runtime_id": "openclaw-local",
        "runtime_kind": "openclaw",
        "model_route_id": "qwen3-coder",
        "target_id": "gpu-1",
        "model": "Qwen3 Coder 30B",
        "capabilities": [capability],
        "priority": 100,
        "enabled": enabled,
        "support": {"supported": supported, "status": "supported" if supported else "experimental_disabled"},
        "health": {"status": health, "available": available},
        "assignments": [],
    }


def test_equivalent_openclaw_role_profiles_group_into_one_lane() -> None:
    rows = [
        _profile("openclaw-gpu-coder", "implement"),
        _profile("openclaw-gpu-review", "review"),
        _profile("openclaw-gpu-fix-review", "fix_review"),
    ]
    lanes = build_execution_lane_views(rows, ROLES)
    assert len(lanes) == 1
    lane = lanes[0]
    assert lane.runtime_id == "openclaw-local"
    assert lane.model_route_id == "qwen3-coder"
    assert lane.target_id == "gpu-1"
    assert lane.roles == ("implement", "review", "fix_review", "final_review")
    assert set(lane.profile_ids) == {
        "openclaw-gpu-coder",
        "openclaw-gpu-review",
        "openclaw-gpu-fix-review",
    }
    assert lane.availability == "ready"


def test_lane_identity_is_stable_across_profile_order_and_role_split() -> None:
    rows = [
        _profile("coder", "implement"),
        _profile("reviewer", "review"),
    ]
    first = build_execution_lane_views(rows, ROLES)[0]
    second = build_execution_lane_views(list(reversed(rows)), ROLES)[0]
    relabelled = [dict(item, model="Renamed display model") for item in rows]
    third = build_execution_lane_views(relabelled, ROLES)[0]
    assert first.id == second.id == third.id
    assert first.id.startswith("lane-")


def test_lane_health_uses_worst_status_without_hiding_usable_profiles() -> None:
    rows = [
        _profile("coder", "implement"),
        _profile("reviewer", "review", health="cooldown", available=False),
    ]
    lane = build_execution_lane_views(rows, ROLES)[0]
    assert lane.health == "cooldown"
    assert lane.availability == "degraded"
    assert lane.diagnostics_summary == "1/2 health-reported profiles currently available."


def test_lane_is_unavailable_when_no_supported_profile_is_healthy() -> None:
    rows = [
        _profile("coder", "implement", health="blocked", available=False),
        _profile("reviewer", "review", health="failed", available=False),
    ]
    lane = build_execution_lane_views(rows, ROLES)[0]
    assert lane.health == "failed"
    assert lane.availability == "unavailable"


def test_all_disabled_and_all_unsupported_have_explicit_availability() -> None:
    disabled = build_execution_lane_views(
        [_profile("coder", "implement", enabled=False)], ROLES
    )[0]
    assert (disabled.health, disabled.availability) == ("disabled", "disabled")

    unsupported = build_execution_lane_views(
        [_profile("coder", "implement", supported=False)], ROLES
    )[0]
    assert (unsupported.health, unsupported.availability) == (
        "unsupported",
        "unsupported",
    )


def test_unknown_health_is_not_misreported_as_unavailable() -> None:
    row = _profile("coder", "implement")
    row.pop("health")
    lane = build_execution_lane_views([row], ROLES)[0]
    assert lane.health == "unknown"
    assert lane.availability == "unknown"


def test_active_assignments_are_folded_into_lane_projection() -> None:
    row = _profile("coder", "implement")
    row["assignments"] = [
        {"package_id": "WP17", "stage": "implement", "status": "running", "agent_id": "coder"}
    ]
    lane = build_execution_lane_views([row], ROLES)[0]
    assert lane.active_assignments[0].package_id == "WP17"
    assert lane.active_assignments[0].role == "implement"
    assert lane.active_assignments[0].agent_id == "coder"


def test_lane_mapping_is_browser_safe_and_has_no_credential_material() -> None:
    row = _profile("coder", "implement")
    row["credential_ref"] = "must-not-leak"
    row["auth_token"] = "also-must-not-leak"
    payload = execution_lane_mappings([row], ROLES)
    encoded = json.dumps(payload)
    assert "must-not-leak" not in encoded
    assert "credential_ref" not in encoded
    assert "auth_token" not in encoded
