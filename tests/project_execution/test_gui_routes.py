from __future__ import annotations

from types import SimpleNamespace

import pytest

from execraft.gui.errors import GuiError
from execraft.gui.routes.project_execution import dispatch_get, dispatch_post


class _Workspace:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def pause(self, *args, **kwargs):
        self.calls.append(("pause", args, kwargs))
        return {"held": True}

    def update_gate_metadata(self, *args, **kwargs):
        self.calls.append(("gate_metadata", args, kwargs))
        return {"definition_revision": 3}

    def coordination_status(self, *args, **kwargs):
        self.calls.append(("coordination_status", args, kwargs))
        return {"pending": True, "divergent": True, "safe_actions": []}

    def coordination_history(self, *args, **kwargs):
        self.calls.append(("coordination_history", args, kwargs))
        return {"project_id": "sample", "limit": kwargs.get("limit", 20), "entries": []}

    def resolve_coordination(self, *args, **kwargs):
        self.calls.append(("coordination_resolve", args, kwargs))
        return {"configured": True, "coordination": {"pending": False}}


def test_project_execution_control_mutation_requires_acknowledgement() -> None:
    workspace = _Workspace()
    service = SimpleNamespace(project_execution_workspace=workspace)

    with pytest.raises(GuiError, match="requires explicit acknowledgement"):
        dispatch_post(
            service,
            "/api/project-execution/pause",
            {"project_id": "sample", "reason": "operator hold"},
        )

    assert workspace.calls == []


def test_project_execution_metadata_route_is_typed_and_revisioned() -> None:
    workspace = _Workspace()
    service = SimpleNamespace(project_execution_workspace=workspace)

    result = dispatch_post(
        service,
        "/api/project-execution/gate/metadata",
        {
            "project_id": "sample",
            "gate_id": "ready",
            "expected_revision": 2,
            "metadata": {"title": "Ready", "schedule": {"target": "2026-10-20"}},
        },
    )

    assert result == {"definition_revision": 3}
    assert workspace.calls == [
        (
            "gate_metadata",
            ("sample", "ready", {"title": "Ready", "schedule": {"target": "2026-10-20"}}),
            {"expected_revision": 2},
        )
    ]


def test_project_execution_metadata_route_rejects_untyped_payload() -> None:
    workspace = _Workspace()
    service = SimpleNamespace(project_execution_workspace=workspace)

    with pytest.raises(GuiError, match="metadata must be a JSON object"):
        dispatch_post(
            service,
            "/api/project-execution/gate/metadata",
            {
                "project_id": "sample",
                "gate_id": "ready",
                "expected_revision": 2,
                "metadata": "not-an-object",
            },
        )


def test_project_execution_automatic_policy_and_cycle_require_explicit_control() -> None:
    class Workspace(_Workspace):
        def set_policy(self, *args, **kwargs):
            self.calls.append(("policy", args, kwargs))
            return {"definition_revision": 4}

        def automatic_cycle(self, *args, **kwargs):
            self.calls.append(("cycle", args, kwargs))
            return {"cycle": {"started_tasks": ["build"]}}

    workspace = Workspace()
    service = SimpleNamespace(project_execution_workspace=workspace)
    policy = {
        "maximum_parallel_tasks": 2,
        "maximum_parallel_tasks_per_phase": 1,
        "maximum_active_phases": 2,
        "task_failure_behavior": "hold",
    }

    with pytest.raises(GuiError, match="requires explicit acknowledgement"):
        dispatch_post(
            service,
            "/api/project-execution/automatic/cycle",
            {"project_id": "sample"},
        )

    result = dispatch_post(
        service,
        "/api/project-execution/policy",
        {
            "project_id": "sample",
            "policy": policy,
            "expected_revision": 3,
            "acknowledged": True,
        },
    )
    assert result == {"definition_revision": 4}
    assert workspace.calls[-1] == (
        "policy",
        ("sample", policy),
        {"expected_revision": 3},
    )

    cycle = dispatch_post(
        service,
        "/api/project-execution/automatic/cycle",
        {"project_id": "sample", "acknowledged": True},
    )
    assert cycle == {"cycle": {"started_tasks": ["build"]}}
    assert workspace.calls[-1] == ("cycle", ("sample",), {})


def test_project_execution_task_assignment_accepts_only_project_metadata() -> None:
    class Workspace(_Workspace):
        def assign_task(self, *args, **kwargs):
            self.calls.append(("assign", args, kwargs))
            return {"definition_revision": 7}

    workspace = Workspace()
    service = SimpleNamespace(project_execution_workspace=workspace)
    metadata = {
        "phase": "foundation",
        "required": True,
        "requires": {"tasks": ["prepare"], "gates": ["approval"]},
    }
    result = dispatch_post(
        service,
        "/api/project-execution/task/assign",
        {
            "project_id": "sample",
            "task_id": "build",
            "metadata": metadata,
            "expected_revision": 6,
        },
    )
    assert result == {"definition_revision": 7}
    assert workspace.calls[-1] == (
        "assign",
        ("sample", "build", metadata),
        {"expected_revision": 6},
    )

    with pytest.raises(GuiError, match="unsupported project Task metadata"):
        dispatch_post(
            service,
            "/api/project-execution/task/assign",
            {
                "project_id": "sample",
                "task_id": "build",
                "metadata": {"phase": "foundation", "title": "duplicate authority"},
                "expected_revision": 6,
            },
        )


def test_project_execution_coordination_status_is_read_only_and_typed() -> None:
    workspace = _Workspace()
    service = SimpleNamespace(project_execution_workspace=workspace)

    result = dispatch_get(
        service,
        "/api/project-execution/coordination",
        {"project_id": ["sample"]},
    )

    assert result == {"pending": True, "divergent": True, "safe_actions": []}
    assert workspace.calls == [("coordination_status", ("sample",), {})]



def test_project_execution_coordination_history_is_bounded_read_only() -> None:
    workspace = _Workspace()
    service = SimpleNamespace(project_execution_workspace=workspace)

    result = dispatch_get(
        service,
        "/api/project-execution/coordination/history",
        {"project_id": ["sample"], "limit": ["12"]},
    )
    assert result == {"project_id": "sample", "limit": 12, "entries": []}
    assert workspace.calls == [("coordination_history", ("sample",), {"limit": 12})]

    with pytest.raises(GuiError, match="between 1 and 100"):
        dispatch_get(
            service,
            "/api/project-execution/coordination/history",
            {"project_id": ["sample"], "limit": ["0"]},
        )

def test_project_execution_coordination_resolution_requires_acknowledgement() -> None:
    workspace = _Workspace()
    service = SimpleNamespace(project_execution_workspace=workspace)

    with pytest.raises(GuiError, match="requires explicit acknowledgement"):
        dispatch_post(
            service,
            "/api/project-execution/coordination/resolve",
            {"project_id": "sample", "action": "abort"},
        )

    result = dispatch_post(
        service,
        "/api/project-execution/coordination/resolve",
        {"project_id": "sample", "action": "abort", "acknowledged": True},
    )
    assert result == {"configured": True, "coordination": {"pending": False}}
    assert workspace.calls == [("coordination_resolve", ("sample",), {"action": "abort"})]
