"""Architecture regression tests for Project Execution domain boundaries."""

from execraft.project_execution.models import ProjectMilestone
from tools.check_architecture import (
    _check_project_delivery_boundary,
    _check_project_execution_boundary,
)


def test_project_execution_does_not_import_task_orchestration_internals():
    assert _check_project_execution_boundary() == []


def test_project_delivery_does_not_import_provider_or_task_runtime_implementations():
    assert _check_project_delivery_boundary() == []


def test_project_milestone_remains_provider_independent():
    fields = set(ProjectMilestone.__dataclass_fields__)
    assert fields == {
        "id",
        "title",
        "description",
        "start",
        "target",
        "requires",
        "delivery_policy",
    }
    assert not fields & {
        "provider",
        "provider_id",
        "delivery_target",
        "registry",
        "deployment",
    }
