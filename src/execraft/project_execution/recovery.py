"""Deterministic reconciliation of interrupted Project Execution intents."""
from __future__ import annotations

from datetime import datetime, timezone

from .events import ProjectEventJournal
from .runtime_repository import ProjectExecutionRuntimeState
from .task_port import TaskExecutionPort, TaskOutcome

_RESOLVED_INTENT_STATES = {"resolved", "failed", "superseded"}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def reconcile_start_intents(
    runtime: ProjectExecutionRuntimeState,
    task_port: TaskExecutionPort,
    journal: ProjectEventJournal | None = None,
) -> bool:
    """Resolve start intents using observable Task state only.

    If the Task still appears not-started, an interrupted intent becomes
    ``uncertain`` and is never replayed automatically. This deliberately favors
    duplicate-start safety over liveness; an operator can explicitly retry an
    uncertain intent after inspecting the Task domain.
    """

    changed_intents: list[str] = []
    for intent_id, intent in runtime.intents.items():
        if intent.get("kind") != "start_task":
            continue
        if intent.get("status") in _RESOLVED_INTENT_STATES:
            continue

        task_id = str(intent.get("task_id", ""))
        outcome = task_port.outcome(task_id)
        if outcome != TaskOutcome.NOT_STARTED:
            intent.update(
                {
                    "status": "resolved",
                    "resolved_at": _now(),
                    "observed_outcome": outcome.value,
                }
            )
            changed_intents.append(intent_id)
            continue

        if intent.get("status") != "uncertain":
            intent.update(
                {
                    "status": "uncertain",
                    "reconciled_at": _now(),
                    "reason": (
                        "Task still appears not started; interrupted intent "
                        "was not replayed automatically"
                    ),
                }
            )
            changed_intents.append(intent_id)

    if changed_intents and journal is not None:
        journal.append(
            "project_execution_reconciled",
            {"intent_ids": sorted(changed_intents)},
        )
    return bool(changed_intents)
