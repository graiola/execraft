"""Pure reconciliation for the shared project wait-summary slot.

``TaskExecutionStateRecord.waiting`` is a presentation/driver summary used by both
provider availability waits and explicit operator pause boundaries.  Durable
provider waits live separately in ``agent_waits``.  This module keeps those two
concepts separate so housekeeping cannot erase an operator-owned boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .agent_wait import seconds_until
from .directives import PAUSE_BEFORE_START, PAUSE_FOR_REPOSITORY_SYNC
from .models import OrchestrateError, PlanGraph, TaskExecutionState, WorkPackageStage


_OPERATOR_PAUSE_KINDS = frozenset(
    {PAUSE_BEFORE_START, PAUSE_FOR_REPOSITORY_SYNC, "pause_after_completion"}
)


@dataclass(frozen=True)
class WaitSummary:
    """Reconciled durable provider waits and the active summary record."""

    agent_waits: dict[str, dict[str, Any]]
    waiting: dict[str, Any]


def is_agent_wait_summary(waiting: Mapping[str, Any]) -> bool:
    """Return whether a top-level waiting record is provider-scheduler data."""

    if not waiting or waiting.get("kind"):
        return False
    return bool(
        waiting.get("package_id")
        and (
            waiting.get("capability")
            or waiting.get("next_check_at")
            or waiting.get("candidates")
            or waiting.get("attempts")
        )
    )


def _live_agent_waits(
    agent_waits: Mapping[str, Mapping[str, Any]],
    plan_graph: PlanGraph,
) -> dict[str, dict[str, Any]]:
    """Drop scheduler records that cannot represent live provider waits."""

    live: dict[str, dict[str, Any]] = {}
    for package_id, raw in agent_waits.items():
        waiting = dict(raw or {})
        try:
            package = plan_graph.package_by_id(package_id)
        except OrchestrateError:
            continue
        if package.stage == WorkPackageStage.COMPLETED or waiting.get("kind"):
            continue
        live[package_id] = waiting
    return live


def _next_agent_wait(
    waits: Mapping[str, Mapping[str, Any]],
    *,
    poll_max_seconds: float,
) -> dict[str, Any]:
    def key(item: Mapping[str, Any]) -> tuple[float, str]:
        delay = seconds_until(str(item.get("next_check_at", "")))
        return (
            delay if delay is not None else poll_max_seconds,
            str(item.get("package_id", "")),
        )

    return dict(min(waits.values(), key=key))


def reconcile_wait_summary(
    *,
    agent_waits: Mapping[str, Mapping[str, Any]],
    current_waiting: Mapping[str, Any],
    project_state: TaskExecutionState,
    plan_graph: PlanGraph,
    poll_max_seconds: float,
) -> WaitSummary:
    """Prune stale provider waits without clobbering operator/human boundaries."""

    live = _live_agent_waits(agent_waits, plan_graph)
    current = dict(current_waiting or {})
    current_is_agent = is_agent_wait_summary(current)

    if not live:
        operator_pause = current.get("kind") in _OPERATOR_PAUSE_KINDS
        if (
            current_is_agent
            or project_state == TaskExecutionState.WAITING_FOR_AGENT
            or (operator_pause and project_state != TaskExecutionState.OPERATOR_PAUSED)
        ):
            current = {}
        return WaitSummary(agent_waits=live, waiting=current)

    # Historical provider records may coexist with an explicit operator/human
    # boundary.  Only provider-owned/empty summaries are replaceable here.
    if (
        not current
        or current_is_agent
        or project_state == TaskExecutionState.WAITING_FOR_AGENT
    ):
        current = _next_agent_wait(live, poll_max_seconds=poll_max_seconds)
    return WaitSummary(agent_waits=live, waiting=current)
