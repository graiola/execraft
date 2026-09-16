"""Configuration parsing for unified task completion."""

from __future__ import annotations

from typing import Any, Mapping

from .models import TaskCompletionPolicy


def task_completion_policy_from_scheduling(
    scheduling: Mapping[str, Any] | None,
) -> TaskCompletionPolicy:
    """Read ``scheduling.task_completion`` with strict types and safe invariants."""

    if not scheduling:
        return TaskCompletionPolicy()
    if not isinstance(scheduling, Mapping):
        raise ValueError("agent scheduling policy must be a mapping")
    raw = scheduling.get("task_completion") or {}
    if not isinstance(raw, Mapping):
        raise ValueError("scheduling.task_completion must be a mapping")
    return TaskCompletionPolicy.from_mapping(raw)
