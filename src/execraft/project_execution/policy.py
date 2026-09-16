"""Bounded policy model for opt-in Automatic Project execution."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from .errors import ProjectExecutionError


class TaskFailureBehavior(str, Enum):
    """Policy for new Project Task starts after a required Task fails.

    The Project executor never pauses or cancels already-running Tasks as a
    side effect of this policy.  ``HOLD`` controls only Project Execution state.
    """

    STOP_NEW = "stop_new"
    HOLD = "hold"
    CONTINUE = "continue"


@dataclass(frozen=True)
class ProjectExecutionPolicy:
    """Task/Phase concurrency and failure policy for Automatic mode.

    Limits are intentionally project-level.  Work Package and agent concurrency
    remain private to Task Execution.
    """

    maximum_parallel_tasks: int = 1
    maximum_parallel_tasks_per_phase: int = 1
    maximum_active_phases: int = 1
    task_failure_behavior: TaskFailureBehavior = TaskFailureBehavior.STOP_NEW

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "maximum_parallel_tasks",
            self._bounded_integer(
                self.maximum_parallel_tasks,
                label="maximum_parallel_tasks",
            ),
        )
        object.__setattr__(
            self,
            "maximum_parallel_tasks_per_phase",
            self._bounded_integer(
                self.maximum_parallel_tasks_per_phase,
                label="maximum_parallel_tasks_per_phase",
            ),
        )
        object.__setattr__(
            self,
            "maximum_active_phases",
            self._bounded_integer(
                self.maximum_active_phases,
                label="maximum_active_phases",
            ),
        )
        try:
            behavior = TaskFailureBehavior(self.task_failure_behavior)
        except ValueError as exc:
            raise ProjectExecutionError(
                "task_failure_behavior must be stop_new, hold, or continue"
            ) from exc
        object.__setattr__(self, "task_failure_behavior", behavior)

    @staticmethod
    def _bounded_integer(value: object, *, label: str) -> int:
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 1 <= value <= 128
        ):
            raise ProjectExecutionError(f"{label} must be an integer from 1 to 128")
        return value

    def as_mapping(self) -> dict[str, Any]:
        """Serialize only canonical P12 policy vocabulary."""

        return {
            "maximum_parallel_tasks": self.maximum_parallel_tasks,
            "maximum_parallel_tasks_per_phase": self.maximum_parallel_tasks_per_phase,
            "maximum_active_phases": self.maximum_active_phases,
            "task_failure_behavior": self.task_failure_behavior.value,
        }

    @classmethod
    def from_mapping(cls, raw: object) -> "ProjectExecutionPolicy":
        """Parse canonical policy plus the bounded P10/P11 compatibility flag."""

        if raw in (None, ""):
            return cls()
        if not isinstance(raw, Mapping):
            raise ProjectExecutionError("policy must be a mapping")

        allowed = {
            "maximum_parallel_tasks",
            "maximum_parallel_tasks_per_phase",
            "maximum_active_phases",
            "task_failure_behavior",
            # P10/P11 compatibility reader. New serializers never emit it.
            "stop_on_task_failure",
        }
        unknown = set(raw) - allowed
        if unknown:
            names = ", ".join(sorted(map(str, unknown)))
            raise ProjectExecutionError(
                f"unsupported Project Execution policy field(s): {names}"
            )

        behavior = cls._failure_behavior(raw)
        return cls(
            maximum_parallel_tasks=raw.get("maximum_parallel_tasks", 1),
            maximum_parallel_tasks_per_phase=raw.get(
                "maximum_parallel_tasks_per_phase",
                raw.get("maximum_parallel_tasks", 1),
            ),
            maximum_active_phases=raw.get("maximum_active_phases", 1),
            task_failure_behavior=behavior,
        )

    @staticmethod
    def _failure_behavior(raw: Mapping[str, Any]) -> object:
        behavior = raw.get("task_failure_behavior")
        legacy_stop = raw.get("stop_on_task_failure")
        if legacy_stop is not None and not isinstance(legacy_stop, bool):
            raise ProjectExecutionError("stop_on_task_failure must be a boolean")

        if behavior is not None and legacy_stop is not None:
            expected = (
                TaskFailureBehavior.STOP_NEW.value
                if legacy_stop
                else TaskFailureBehavior.CONTINUE.value
            )
            if behavior != expected:
                raise ProjectExecutionError(
                    "task_failure_behavior conflicts with legacy "
                    "stop_on_task_failure"
                )
        if behavior is not None:
            return behavior
        if legacy_stop is None:
            return TaskFailureBehavior.STOP_NEW.value
        return (
            TaskFailureBehavior.STOP_NEW.value
            if legacy_stop
            else TaskFailureBehavior.CONTINUE.value
        )


__all__ = ["ProjectExecutionPolicy", "TaskFailureBehavior"]
