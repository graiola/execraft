"""Optimistically concurrent CRUD service for one Project Execution graph."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import TypeVar

from .models import (
    ExecutionMode,
    MilestoneRequirements,
    ProjectExecutionDefinition,
    ProjectExecutionError,
    ProjectExecutionNotFoundError,
    ProjectGate,
    ProjectMilestone,
    ProjectPhase,
    ProjectTask,
    TaskRequirements,
)
from .policy import ProjectExecutionPolicy
from .repository import ProjectExecutionRepository
from .runtime_repository import ProjectRuntimeRepository
from .validation import validate_definition

T = TypeVar("T")


class ProjectExecutionService:
    """Own all coordinated writes to ``PROJECT_EXECUTION.yaml``.

    Each operation mutates the complete graph and validates it before the
    repository performs an optimistic-revision write. Runtime history is
    optionally consulted to prevent deleting identities needed for audit and
    immutable Milestone baselines.
    """

    def __init__(
        self,
        repository: ProjectExecutionRepository,
        *,
        task_exists: Callable[[str], bool] | None = None,
        runtime_repository: ProjectRuntimeRepository | None = None,
    ) -> None:
        self.repository = repository
        self.task_exists = task_exists
        self.runtime_repository = runtime_repository

    def initialize(
        self,
        project_id: str,
        *,
        mode: str = "assisted",
    ) -> ProjectExecutionDefinition:
        return self.repository.create(
            ProjectExecutionDefinition(project=project_id, mode=mode)
        )

    def get(self) -> ProjectExecutionDefinition:
        return self.repository.load()

    def set_mode(
        self,
        mode: str | ExecutionMode,
        *,
        expected_revision: int,
    ) -> ProjectExecutionDefinition:
        """Change Project execution mode through the normal revision contract."""

        try:
            normalized = ExecutionMode(mode)
        except ValueError as exc:
            raise ProjectExecutionError(
                "Project execution mode must be observe, assisted, or automatic"
            ) from exc
        definition = self.get()
        return self._save(
            replace(definition, mode=normalized),
            expected_revision,
        )

    def set_policy(
        self,
        policy: ProjectExecutionPolicy,
        *,
        expected_revision: int,
    ) -> ProjectExecutionDefinition:
        """Replace Automatic execution policy through optimistic concurrency."""

        if not isinstance(policy, ProjectExecutionPolicy):
            raise ProjectExecutionError("Project Execution policy is invalid")
        definition = self.get()
        return self._save(
            replace(definition, policy=policy),
            expected_revision,
        )

    def upsert_phase(
        self,
        phase: ProjectPhase,
        *,
        expected_revision: int,
    ) -> ProjectExecutionDefinition:
        definition = self.get()
        return self._save(
            replace(
                definition,
                phases=self._upsert(definition.phases, phase),
            ),
            expected_revision,
        )

    def upsert_gate(
        self,
        gate: ProjectGate,
        *,
        expected_revision: int,
    ) -> ProjectExecutionDefinition:
        definition = self.get()
        return self._save(
            replace(
                definition,
                gates=self._upsert(definition.gates, gate),
            ),
            expected_revision,
        )

    def upsert_milestone(
        self,
        milestone: ProjectMilestone,
        *,
        expected_revision: int,
    ) -> ProjectExecutionDefinition:
        definition = self.get()
        return self._save(
            replace(
                definition,
                milestones=self._upsert(definition.milestones, milestone),
            ),
            expected_revision,
        )

    def assign_task(
        self,
        task: ProjectTask,
        *,
        expected_revision: int,
    ) -> ProjectExecutionDefinition:
        """Create/update Project metadata for a canonical Task and its Phase."""

        definition = self.get()
        tasks = self._upsert(
            definition.tasks,
            task,
            key="task_id",
        )
        phases = tuple(
            replace(
                phase,
                tasks=tuple(
                    item.task_id for item in tasks if item.phase == phase.id
                ),
            )
            for phase in definition.phases
        )
        return self._save(
            replace(definition, tasks=tasks, phases=phases),
            expected_revision,
        )

    def remove_task(
        self,
        task_id: str,
        *,
        expected_revision: int,
    ) -> ProjectExecutionDefinition:
        """Remove only the Project reference; never delete the canonical Task."""

        definition = self.get()
        if any(task_id in item.requires.tasks for item in definition.tasks):
            raise ProjectExecutionError(
                f"cannot unlink Task {task_id}: another Project Task requires it"
            )
        if any(task_id in item.requires.tasks for item in definition.milestones):
            raise ProjectExecutionError(
                f"cannot unlink Task {task_id}: a Milestone requires it"
            )
        tasks = self._delete(definition.tasks, task_id, key="task_id")
        phases = tuple(
            replace(
                phase,
                tasks=tuple(member for member in phase.tasks if member != task_id),
            )
            for phase in definition.phases
        )
        return self._save(
            replace(definition, tasks=tasks, phases=phases),
            expected_revision,
        )

    def set_task_prerequisites(
        self,
        task_id: str,
        requires: TaskRequirements,
        *,
        expected_revision: int,
    ) -> ProjectExecutionDefinition:
        definition = self.get()
        current = definition.task_index.get(task_id)
        if current is None:
            raise ProjectExecutionNotFoundError(
                f"project Task reference not found: {task_id}"
            )
        return self.assign_task(
            replace(current, requires=requires),
            expected_revision=expected_revision,
        )

    def set_phase_boundaries(
        self,
        phase_id: str,
        *,
        entry_gates: tuple[str, ...],
        exit_gates: tuple[str, ...],
        expected_revision: int,
    ) -> ProjectExecutionDefinition:
        definition = self.get()
        phase = definition.phase_index.get(phase_id)
        if phase is None:
            raise ProjectExecutionNotFoundError(f"Phase not found: {phase_id}")
        return self.upsert_phase(
            replace(
                phase,
                entry_gates=entry_gates,
                exit_gates=exit_gates,
            ),
            expected_revision=expected_revision,
        )

    def set_milestone_requirements(
        self,
        milestone_id: str,
        requires: MilestoneRequirements,
        *,
        expected_revision: int,
    ) -> ProjectExecutionDefinition:
        definition = self.get()
        milestone = definition.milestone_index.get(milestone_id)
        if milestone is None:
            raise ProjectExecutionNotFoundError(
                f"Milestone not found: {milestone_id}"
            )
        return self.upsert_milestone(
            replace(milestone, requires=requires),
            expected_revision=expected_revision,
        )

    def delete_phase(
        self,
        phase_id: str,
        *,
        expected_revision: int,
    ) -> ProjectExecutionDefinition:
        definition = self.get()
        if any(task.phase == phase_id for task in definition.tasks):
            raise ProjectExecutionError(
                f"cannot delete Phase {phase_id}: Tasks still reference it"
            )
        return self._save(
            replace(
                definition,
                phases=self._delete(definition.phases, phase_id),
            ),
            expected_revision,
        )

    def delete_gate(
        self,
        gate_id: str,
        *,
        expected_revision: int,
    ) -> ProjectExecutionDefinition:
        definition = self.get()
        references = self._gate_references(definition, gate_id)
        if references:
            raise ProjectExecutionError(
                f"cannot delete Gate {gate_id}: referenced by "
                + ", ".join(references)
            )
        self._assert_gate_history_removable(gate_id)
        return self._save(
            replace(
                definition,
                gates=self._delete(definition.gates, gate_id),
            ),
            expected_revision,
        )

    def delete_milestone(
        self,
        milestone_id: str,
        *,
        expected_revision: int,
    ) -> ProjectExecutionDefinition:
        definition = self.get()
        references = [
            f"Phase {phase.id}"
            for phase in definition.phases
            if milestone_id in phase.milestones
        ]
        references.extend(
            f"Milestone {milestone.id}"
            for milestone in definition.milestones
            if milestone_id in milestone.requires.milestones
        )
        if references:
            raise ProjectExecutionError(
                f"cannot delete Milestone {milestone_id}: referenced by "
                + ", ".join(references)
            )
        self._assert_milestone_history_removable(milestone_id)
        return self._save(
            replace(
                definition,
                milestones=self._delete(definition.milestones, milestone_id),
            ),
            expected_revision,
        )

    def _save(
        self,
        definition: ProjectExecutionDefinition,
        expected_revision: int,
    ) -> ProjectExecutionDefinition:
        validate_definition(definition, task_exists=self.task_exists)
        return self.repository.save(
            definition,
            expected_revision=expected_revision,
        )

    @staticmethod
    def _gate_references(
        definition: ProjectExecutionDefinition,
        gate_id: str,
    ) -> list[str]:
        references = [
            f"Phase {phase.id}"
            for phase in definition.phases
            if gate_id in (*phase.entry_gates, *phase.exit_gates)
        ]
        references.extend(
            f"Task {task.task_id}"
            for task in definition.tasks
            if gate_id in task.requires.gates
        )
        references.extend(
            f"Gate {gate.id}"
            for gate in definition.gates
            if any(criterion.gate_id == gate_id for criterion in gate.criteria)
        )
        references.extend(
            f"Milestone {milestone.id}"
            for milestone in definition.milestones
            if gate_id in milestone.requires.gates
        )
        return references

    def _assert_gate_history_removable(self, gate_id: str) -> None:
        runtime = self._runtime()
        if runtime is None:
            return
        row = runtime.gates.get(gate_id)
        if row and self._gate_has_history(row):
            raise ProjectExecutionError(
                f"cannot delete Gate {gate_id}: runtime evaluation/decision history exists"
            )
        for milestone_id, achievement in runtime.milestone_achievements.items():
            gates = achievement.get("gates") or {}
            if isinstance(gates, dict) and gate_id in gates:
                raise ProjectExecutionError(
                    f"cannot delete Gate {gate_id}: Milestone {milestone_id} "
                    "baseline references it"
                )

    def _assert_milestone_history_removable(self, milestone_id: str) -> None:
        runtime = self._runtime()
        if runtime is None:
            return
        if milestone_id in runtime.milestone_achievements:
            raise ProjectExecutionError(
                f"cannot delete Milestone {milestone_id}: immutable achievement exists"
            )

    def _runtime(self):
        if self.runtime_repository is None:
            return None
        return self.runtime_repository.load()

    @staticmethod
    def _gate_has_history(row: dict[str, object]) -> bool:
        return bool(
            row.get("evaluation")
            or row.get("decisions")
            or row.get("waiver")
            or row.get("input_fingerprint")
        )

    @staticmethod
    def _upsert(
        values: Sequence[T],
        item: T,
        *,
        key: str = "id",
    ) -> tuple[T, ...]:
        result = list(values)
        wanted = getattr(item, key)
        for index, current in enumerate(result):
            if getattr(current, key) == wanted:
                result[index] = item
                return tuple(result)
        result.append(item)
        return tuple(result)

    @staticmethod
    def _delete(
        values: Sequence[T],
        item_id: str,
        *,
        key: str = "id",
    ) -> tuple[T, ...]:
        result = tuple(value for value in values if getattr(value, key) != item_id)
        if len(result) == len(values):
            raise ProjectExecutionNotFoundError(
                f"project asset not found: {item_id}"
            )
        return result
