"""Pure planning primitives for opt-in Automatic Project execution.

The planner consumes Project-level Task eligibility and coarse Task outcomes.
It never calls ``TaskExecutionPort.start`` and therefore has no side effects.
The engine owns durable intent creation and Task actions after accepting a plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .eligibility import EligibilityResult
from .graph import topological_order
from .models import PhaseState, ProjectExecutionDefinition
from .phases import PhaseProjection
from .runtime_repository import ProjectExecutionRuntimeState
from .task_port import TaskOutcome

_RESOLVED_INTENT_STATES = {"resolved", "failed", "superseded"}


@dataclass(frozen=True)
class AutomaticCapacity:
    """Concurrency accounting used by one automatic scheduling cycle."""

    global_limit: int
    occupied_global: int
    available_global: int
    active_phase_limit: int
    active_phases: tuple[str, ...]
    per_phase_limit: int
    occupied_by_phase: dict[str, int]

    def as_mapping(self) -> dict[str, Any]:
        return {
            "global_limit": self.global_limit,
            "occupied_global": self.occupied_global,
            "available_global": self.available_global,
            "active_phase_limit": self.active_phase_limit,
            "active_phases": list(self.active_phases),
            "per_phase_limit": self.per_phase_limit,
            "occupied_by_phase": dict(self.occupied_by_phase),
        }


@dataclass(frozen=True)
class AutomaticStartPlan:
    """Deterministic side-effect-free selection for one engine cycle."""

    task_ids: tuple[str, ...]
    capacity: AutomaticCapacity
    policy_blocked: dict[str, str]
    reserved_tasks: tuple[str, ...]

    def as_mapping(self) -> dict[str, Any]:
        return {
            "task_ids": list(self.task_ids),
            "capacity": self.capacity.as_mapping(),
            "policy_blocked": dict(self.policy_blocked),
            "reserved_tasks": list(self.reserved_tasks),
        }


class AutomaticExecutionPlanner:
    """Select eligible Tasks while enforcing project/Phase concurrency limits."""

    def plan(
        self,
        *,
        definition: ProjectExecutionDefinition,
        runtime: ProjectExecutionRuntimeState,
        eligibility: dict[str, EligibilityResult],
        phases: dict[str, PhaseProjection],
    ) -> AutomaticStartPlan:
        policy = definition.policy
        reserved = self._reserved_tasks(runtime)
        running = {
            task.task_id
            for task in definition.tasks
            if self._observed_outcome(runtime, task.task_id) == TaskOutcome.RUNNING
        }
        occupied = running | set(reserved)
        phase_by_task = {
            task.task_id: task.phase
            for task in definition.tasks
        }
        occupied_by_phase = self._counts_by_phase(occupied, phase_by_task)
        active_phases = {
            phase_id
            for phase_id, projection in phases.items()
            if projection.state == PhaseState.ACTIVE
        }
        active_phases.update(
            phase_by_task[task_id]
            for task_id in reserved
            if task_id in phase_by_task
        )

        global_available = max(0, policy.maximum_parallel_tasks - len(occupied))
        candidates: list[str] = []
        blocked: dict[str, str] = {}
        planned_by_phase = dict(occupied_by_phase)
        planned_phases = set(active_phases)

        for task_id in self._task_order(definition):
            if global_available <= 0:
                break
            result = eligibility.get(task_id)
            if result is None or not result.eligible:
                continue
            if task_id in occupied:
                blocked[task_id] = "Task has an unresolved or running start"
                continue

            phase_id = phase_by_task[task_id]
            phase_state = phases[phase_id].state
            if phase_state not in {PhaseState.READY, PhaseState.ACTIVE}:
                blocked[task_id] = (
                    f"Phase {phase_id} is {phase_state.value} and cannot start new Tasks"
                )
                continue

            if phase_id not in planned_phases:
                if len(planned_phases) >= policy.maximum_active_phases:
                    blocked[task_id] = (
                        "maximum_active_phases would be exceeded"
                    )
                    continue
                planned_phases.add(phase_id)

            phase_occupied = planned_by_phase.get(phase_id, 0)
            if phase_occupied >= policy.maximum_parallel_tasks_per_phase:
                blocked[task_id] = (
                    "maximum_parallel_tasks_per_phase would be exceeded"
                )
                continue

            candidates.append(task_id)
            planned_by_phase[phase_id] = phase_occupied + 1
            global_available -= 1

        capacity = AutomaticCapacity(
            global_limit=policy.maximum_parallel_tasks,
            occupied_global=len(occupied),
            available_global=max(0, policy.maximum_parallel_tasks - len(occupied)),
            active_phase_limit=policy.maximum_active_phases,
            active_phases=tuple(sorted(active_phases)),
            per_phase_limit=policy.maximum_parallel_tasks_per_phase,
            occupied_by_phase={
                key: occupied_by_phase[key]
                for key in sorted(occupied_by_phase)
            },
        )
        return AutomaticStartPlan(
            task_ids=tuple(candidates),
            capacity=capacity,
            policy_blocked=blocked,
            reserved_tasks=reserved,
        )

    @staticmethod
    def _task_order(definition: ProjectExecutionDefinition) -> tuple[str, ...]:
        """Use dependency-first definition order for reproducible scheduling."""

        return topological_order(
            {
                task.task_id: task.requires.tasks
                for task in definition.tasks
            },
            label="Task dependency graph",
        )

    @staticmethod
    def _reserved_tasks(
        runtime: ProjectExecutionRuntimeState,
    ) -> tuple[str, ...]:
        """Reserve ambiguous start intents until Task observation resolves them."""

        reserved = {
            str(intent.get("task_id", ""))
            for intent in runtime.intents.values()
            if intent.get("kind") == "start_task"
            and intent.get("status") not in _RESOLVED_INTENT_STATES
            and str(intent.get("task_id", ""))
        }
        # A newly observed RUNNING Task is accounted by ``running`` rather than
        # twice through the unresolved-intent reservation set.
        return tuple(
            sorted(
                task_id
                for task_id in reserved
                if AutomaticExecutionPlanner._observed_outcome(runtime, task_id)
                == TaskOutcome.NOT_STARTED
            )
        )


    @staticmethod
    def _observed_outcome(
        runtime: ProjectExecutionRuntimeState,
        task_id: str,
    ) -> TaskOutcome:
        """Read the engine's fresh Task observation without another filesystem read."""

        value = str((runtime.observed_tasks.get(task_id) or {}).get("outcome", ""))
        try:
            return TaskOutcome(value)
        except ValueError:
            return TaskOutcome.UNKNOWN

    @staticmethod
    def _counts_by_phase(
        task_ids: set[str],
        phase_by_task: dict[str, str],
    ) -> dict[str, int]:
        counts: dict[str, int] = {}
        for task_id in task_ids:
            phase_id = phase_by_task.get(task_id)
            if phase_id is None:
                continue
            counts[phase_id] = counts.get(phase_id, 0) + 1
        return counts


__all__ = [
    "AutomaticCapacity",
    "AutomaticExecutionPlanner",
    "AutomaticStartPlan",
]
