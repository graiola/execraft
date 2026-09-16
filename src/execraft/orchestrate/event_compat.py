"""Bounded read compatibility for historical Task Execution event names.

New Task Execution journals emit canonical Work Package/Check terminology.
Historical journals are immutable and may contain the pre-refactor ``milestone``
and ``gate`` names, so read-side projections normalize only the event *type*.
The original journal entry is never rewritten.
"""

from __future__ import annotations

from typing import Mapping

HISTORICAL_TASK_EVENT_ALIASES: dict[str, str] = {
    "milestone_directive_applied": "work_package_directive_applied",
    "milestone_directive_rejected": "work_package_directive_rejected",
    "milestone_pause_before_start_reached": "work_package_pause_before_start_reached",
    "milestone_pause_before_start_acknowledged": "work_package_pause_before_start_acknowledged",
    "repository_scope_gate_reconciled": "repository_scope_check_reconciled",
    "repository_sync_commit_gate_superseded": "repository_sync_commit_check_superseded",
    "repository_sync_commit_gate_recovery": "repository_sync_commit_check_recovery",
}


def canonical_task_event_type(event_type: object) -> str:
    """Return the canonical event type for new and historical task journals."""

    value = str(event_type or "").strip()
    return HISTORICAL_TASK_EVENT_ALIASES.get(value, value)


def task_event_work_package_id(payload: Mapping[str, object]) -> str:
    """Return the Work Package identity from canonical or historical Task payloads.

    ``milestone_id`` existed before Work Packages received their canonical name.
    Keep that spelling confined to this read-side compatibility seam so new
    Task-domain code cannot accidentally reintroduce it.
    """

    return str(
        payload.get("package_id")
        or payload.get("work_package_id")
        or payload.get("milestone_id")
        or ""
    ).strip()
