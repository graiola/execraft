"""Deterministic Project Phase lifecycle and schedule-health projections."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .eligibility import EligibilityResult, gate_satisfied
from .models import PhaseHealth, PhaseState, ProjectExecutionDefinition, ProjectPhase
from .runtime_repository import ProjectExecutionRuntimeState
from .task_port import TaskExecutionPort, TaskOutcome


@dataclass(frozen=True)
class PhaseProjection:
    state: PhaseState
    health: PhaseHealth
    reasons: tuple[str, ...] = ()


def project_phase(
    phase: ProjectPhase,
    *,
    definition: ProjectExecutionDefinition,
    runtime: ProjectExecutionRuntimeState,
    task_port: TaskExecutionPort,
    eligibility: dict[str, EligibilityResult] | None = None,
    today: date | None = None,
) -> PhaseProjection:
    """Project one Phase entirely from canonical Task/Gate/Milestone state."""

    if phase.id in runtime.cancelled_phases:
        return PhaseProjection(PhaseState.CANCELLED, PhaseHealth.ON_TRACK)

    state = _phase_state(phase, definition, runtime, task_port)
    health, reasons = _phase_health(
        phase,
        state=state,
        definition=definition,
        task_port=task_port,
        eligibility=eligibility,
        today=today or date.today(),
    )
    return PhaseProjection(state=state, health=health, reasons=tuple(reasons))


def _phase_state(
    phase: ProjectPhase,
    definition: ProjectExecutionDefinition,
    runtime: ProjectExecutionRuntimeState,
    task_port: TaskExecutionPort,
) -> PhaseState:
    if any(not gate_satisfied(runtime, gate_id) for gate_id in phase.entry_gates):
        return PhaseState.PLANNED

    required_tasks = [
        definition.task_index[task_id]
        for task_id in phase.tasks
        if definition.task_index[task_id].required
    ]
    tasks_complete = all(
        task_port.outcome(task.task_id) == TaskOutcome.COMPLETED
        for task in required_tasks
    )
    exits_satisfied = all(
        gate_satisfied(runtime, gate_id) for gate_id in phase.exit_gates
    )
    milestones_achieved = all(
        milestone_id in runtime.milestone_achievements
        for milestone_id in phase.milestones
    )
    if tasks_complete and exits_satisfied and milestones_achieved:
        return PhaseState.COMPLETE

    started_outcomes = {
        TaskOutcome.RUNNING,
        TaskOutcome.COMPLETED,
        TaskOutcome.FAILED,
        TaskOutcome.CANCELLED,
    }
    if any(task_port.outcome(task_id) in started_outcomes for task_id in phase.tasks):
        return PhaseState.ACTIVE
    return PhaseState.READY


def _phase_health(
    phase: ProjectPhase,
    *,
    state: PhaseState,
    definition: ProjectExecutionDefinition,
    task_port: TaskExecutionPort,
    eligibility: dict[str, EligibilityResult] | None,
    today: date,
) -> tuple[PhaseHealth, list[str]]:
    if state in {PhaseState.COMPLETE, PhaseState.CANCELLED}:
        return PhaseHealth.ON_TRACK, []

    required_task_ids = [
        task_id
        for task_id in phase.tasks
        if definition.task_index[task_id].required
    ]
    failed = [
        task_id
        for task_id in required_task_ids
        if task_port.outcome(task_id) == TaskOutcome.FAILED
    ]
    not_started = [
        task_id
        for task_id in required_task_ids
        if task_port.outcome(task_id) == TaskOutcome.NOT_STARTED
    ]
    blocked = [
        task_id
        for task_id in not_started
        if eligibility is not None
        and not eligibility.get(task_id, EligibilityResult(False)).eligible
    ]
    if failed or (not_started and len(blocked) == len(not_started)):
        return PhaseHealth.BLOCKED, ["required execution is blocked"]

    if not phase.schedule.target:
        return PhaseHealth.ON_TRACK, []
    target = date.fromisoformat(phase.schedule.target)
    if today > target:
        return PhaseHealth.LATE, [f"target {target.isoformat()} has passed"]
    if target - today <= timedelta(days=7):
        return PhaseHealth.AT_RISK, ["target is within seven days"]
    return PhaseHealth.ON_TRACK, []
