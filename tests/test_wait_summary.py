"""Regression tests for provider-wait versus operator-boundary reconciliation."""

from execraft.orchestrate.models import (
    AcceptanceCriterion,
    PlanGraph,
    TaskExecutionState,
    TaskExecutionStateRecord,
    WorkPackage,
    WorkPackageStage,
)
from execraft.orchestrate.wait_summary import reconcile_wait_summary


def _graph(*, completed: bool = False) -> PlanGraph:
    package = WorkPackage(
        id="WP21",
        title="Work Package",
        requirements=["R1"],
        acceptance_criteria=[AcceptanceCriterion(id="A1", description="Done")],
    )
    if completed:
        package.stage = WorkPackageStage.COMPLETED
        package.status = "completed"
    return PlanGraph([package])


def test_active_operator_pause_survives_agent_wait_housekeeping() -> None:
    waiting = {
        "kind": "pause_for_repository_sync",
        "package_id": "WP21",
        "stage": "completed",
    }
    result = reconcile_wait_summary(
        agent_waits={"WP21": waiting},
        current_waiting=waiting,
        project_state=TaskExecutionState.OPERATOR_PAUSED,
        plan_graph=_graph(completed=True),
        poll_max_seconds=300.0,
    )

    assert result.agent_waits == {}
    assert result.waiting == waiting


def test_stale_operator_pause_summary_is_removed_outside_paused_state() -> None:
    waiting = {
        "kind": "pause_for_repository_sync",
        "package_id": "WP21",
        "stage": "completed",
    }
    result = reconcile_wait_summary(
        agent_waits={"WP21": waiting},
        current_waiting=waiting,
        project_state=TaskExecutionState.WAITING_FOR_AGENT,
        plan_graph=_graph(completed=True),
        poll_max_seconds=300.0,
    )

    assert result.agent_waits == {}
    assert result.waiting == {}


def test_state_migration_does_not_turn_operator_pause_into_agent_wait() -> None:
    record = TaskExecutionStateRecord(
        project_id="pause",
        state=TaskExecutionState.OPERATOR_PAUSED,
        waiting={
            "kind": "pause_before_start",
            "package_id": "WP21",
            "stage": "prepare",
        },
    )

    restored = TaskExecutionStateRecord.from_mapping(record.as_mapping())

    assert restored.waiting == record.waiting
    assert restored.agent_waits == {}


def test_state_migration_keeps_legacy_waiting_for_agent_compatibility() -> None:
    record = TaskExecutionStateRecord(
        project_id="agent-wait",
        state=TaskExecutionState.WAITING_FOR_AGENT,
        waiting={
            "package_id": "WP21",
            "stage": "implement",
            "next_check_at": "2026-08-24T16:30:00+00:00",
        },
    )

    restored = TaskExecutionStateRecord.from_mapping(record.as_mapping())

    assert restored.agent_waits == {"WP21": record.waiting}
