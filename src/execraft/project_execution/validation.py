"""Cross-object semantic validation for Project Execution definitions."""

from __future__ import annotations

from collections.abc import Callable

from .graph import assert_acyclic
from .models import ProjectExecutionDefinition, ProjectExecutionError

TaskExists = Callable[[str], bool]


def validate_definition(
    definition: ProjectExecutionDefinition,
    *,
    task_exists: TaskExists | None = None,
) -> None:
    """Validate references, ownership, and dependency semantics as one graph."""

    phases = definition.phase_index
    gates = definition.gate_index
    milestones = definition.milestone_index
    tasks = definition.task_index

    _validate_tasks(definition, phases, gates, tasks, task_exists)
    _validate_phases(definition, phases, gates, milestones, tasks)
    _validate_gates(definition, gates, tasks)
    _validate_phase_entry_boundaries(definition, gates)
    _validate_milestones(definition, gates, milestones, tasks)
    _validate_dependency_cycles(definition)


def _validate_tasks(definition, phases, gates, tasks, task_exists: TaskExists | None) -> None:
    for task in definition.tasks:
        if task.phase not in phases:
            raise ProjectExecutionError(
                f"task {task.task_id!r} references missing phase {task.phase!r}"
            )
        if task_exists is not None and not task_exists(task.task_id):
            raise ProjectExecutionError(f"missing canonical Task reference: {task.task_id}")

        for predecessor in task.requires.tasks:
            if predecessor not in tasks:
                raise ProjectExecutionError(
                    f"task {task.task_id!r} references missing prerequisite Task "
                    f"{predecessor!r}"
                )
            if predecessor == task.task_id:
                raise ProjectExecutionError(f"task {task.task_id!r} cannot require itself")

        for gate_id in task.requires.gates:
            if gate_id not in gates:
                raise ProjectExecutionError(
                    f"task {task.task_id!r} references missing Gate {gate_id!r}"
                )


def _validate_phases(definition, phases, gates, milestones, tasks) -> None:
    for phase in definition.phases:
        for task_id in phase.tasks:
            if task_id not in tasks:
                raise ProjectExecutionError(
                    f"phase {phase.id!r} references missing Task {task_id!r}"
                )
            if tasks[task_id].phase != phase.id:
                raise ProjectExecutionError(
                    f"Task {task_id!r} Phase ownership disagrees with phase {phase.id!r}"
                )

        canonical_members = {
            task.task_id for task in definition.tasks if task.phase == phase.id
        }
        if canonical_members != set(phase.tasks):
            raise ProjectExecutionError(
                f"phase {phase.id!r} task membership must exactly match canonical "
                "task Phase ownership"
            )

        for gate_id in (*phase.entry_gates, *phase.exit_gates):
            if gate_id not in gates:
                raise ProjectExecutionError(
                    f"phase {phase.id!r} references missing Gate {gate_id!r}"
                )
        for milestone_id in phase.milestones:
            if milestone_id not in milestones:
                raise ProjectExecutionError(
                    f"phase {phase.id!r} references missing Milestone {milestone_id!r}"
                )


def _validate_gates(definition, gates, tasks) -> None:
    for gate in definition.gates:
        for criterion in gate.criteria:
            if criterion.task_id and criterion.task_id not in tasks:
                raise ProjectExecutionError(
                    f"Gate {gate.id!r} references missing Task {criterion.task_id!r}"
                )
            if not criterion.gate_id:
                continue
            if criterion.gate_id not in gates:
                raise ProjectExecutionError(
                    f"Gate {gate.id!r} references missing Gate {criterion.gate_id!r}"
                )
            if criterion.gate_id == gate.id:
                raise ProjectExecutionError(f"Gate {gate.id!r} cannot reference itself")


def _validate_phase_entry_boundaries(definition, gates) -> None:
    """Prevent a Phase from waiting on work that can only run inside itself."""

    for phase in definition.phases:
        phase_tasks = set(phase.tasks)
        for gate_id in phase.entry_gates:
            referenced_tasks = {
                criterion.task_id
                for criterion in gates[gate_id].criteria
                if criterion.task_id
            }
            overlap = phase_tasks & referenced_tasks
            if overlap:
                raise ProjectExecutionError(
                    f"phase {phase.id!r} entry Gate {gate_id!r} depends on "
                    f"contained Task(s): {', '.join(sorted(overlap))}"
                )


def _validate_milestones(definition, gates, milestones, tasks) -> None:
    for milestone in definition.milestones:
        for task_id in milestone.requires.tasks:
            if task_id not in tasks:
                raise ProjectExecutionError(
                    f"Milestone {milestone.id!r} references missing Task {task_id!r}"
                )
        for gate_id in milestone.requires.gates:
            if gate_id not in gates:
                raise ProjectExecutionError(
                    f"Milestone {milestone.id!r} references missing Gate {gate_id!r}"
                )
        for prerequisite in milestone.requires.milestones:
            if prerequisite not in milestones:
                raise ProjectExecutionError(
                    f"Milestone {milestone.id!r} references missing Milestone "
                    f"{prerequisite!r}"
                )
            if prerequisite == milestone.id:
                raise ProjectExecutionError(
                    f"Milestone {milestone.id!r} cannot require itself"
                )


def _validate_dependency_cycles(definition: ProjectExecutionDefinition) -> None:
    assert_acyclic(
        {task.task_id: task.requires.tasks for task in definition.tasks},
        label="Task dependency graph",
    )
    assert_acyclic(
        {
            gate.id: tuple(
                criterion.gate_id for criterion in gate.criteria if criterion.gate_id
            )
            for gate in definition.gates
        },
        label="Gate dependency graph",
    )
    assert_acyclic(
        {
            milestone.id: milestone.requires.milestones
            for milestone in definition.milestones
        },
        label="Milestone dependency graph",
    )
