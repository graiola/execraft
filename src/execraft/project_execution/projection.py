"""Read projection shared by Project Execution CLI and GUI surfaces."""
from __future__ import annotations

from typing import Any

from .engine import ProjectExecutionSnapshot
from .milestones import MilestoneRequirementEvaluator
from .models import ProjectExecutionDefinition
from .runtime_repository import ProjectExecutionRuntimeState
from .task_port import TaskExecutionPort, TaskOutcome


class ProjectExecutionProjection:
    """Project canonical definition + runtime observations into transport data."""

    def __init__(self) -> None:
        self._milestones = MilestoneRequirementEvaluator()

    def project(
        self,
        definition: ProjectExecutionDefinition,
        runtime: ProjectExecutionRuntimeState,
        snapshot: ProjectExecutionSnapshot,
        task_port: TaskExecutionPort,
    ) -> dict[str, Any]:
        return {
            "schema_version": definition.schema_version,
            "project_id": definition.project,
            "definition_revision": definition.revision,
            "definition_digest": snapshot.definition_digest,
            "mode": definition.mode.value,
            "policy": definition.policy.as_mapping(),
            "automatic": dict(runtime.executor.get("automatic", {}))
            if isinstance(runtime.executor.get("automatic", {}), dict)
            else {},
            "held": runtime.held,
            "hold_reason": runtime.hold_reason,
            "phases": [
                self._phase(phase, snapshot) for phase in definition.phases
            ],
            "gates": [self._gate(gate, runtime) for gate in definition.gates],
            "milestones": [
                self._milestone(milestone, runtime, task_port)
                for milestone in definition.milestones
            ],
            "tasks": [
                self._task(project_task, snapshot, task_port)
                for project_task in definition.tasks
            ],
            "ready_tasks": [
                task_id
                for task_id, result in snapshot.eligibility.items()
                if result.eligible
            ],
            "blocked_tasks": self._blocked_tasks(snapshot, task_port),
        }

    @staticmethod
    def _phase(phase, snapshot: ProjectExecutionSnapshot) -> dict[str, Any]:
        projected = snapshot.phases[phase.id]
        return {
            **phase.as_mapping(),
            "state": projected.state.value,
            "health": projected.health.value,
            "health_reasons": list(projected.reasons),
        }

    @staticmethod
    def _gate(gate, runtime: ProjectExecutionRuntimeState) -> dict[str, Any]:
        row = runtime.gates.get(gate.id, {})
        return {
            **gate.as_mapping(),
            "state": row.get("state", "waiting"),
            "evaluation": row.get("evaluation", {}),
            "input_fingerprint": row.get("input_fingerprint", ""),
            "decision_history": row.get("decisions", []),
            "waiver": row.get("waiver", {}),
        }

    def _milestone(
        self,
        milestone,
        runtime: ProjectExecutionRuntimeState,
        task_port: TaskExecutionPort,
    ) -> dict[str, Any]:
        projection = self._milestones.evaluate(
            milestone,
            runtime=runtime,
            task_port=task_port,
        )
        achievement = runtime.milestone_achievements.get(milestone.id)
        return {
            **milestone.as_mapping(),
            "state": projection.state.value,
            "health": projection.health.value,
            "missing_requirements": list(projection.missing),
            "achievement": achievement or {},
        }

    @staticmethod
    def _task(project_task, snapshot, task_port: TaskExecutionPort) -> dict[str, Any]:
        task_id = project_task.task_id
        summary = task_port.describe(task_id)
        eligibility = snapshot.eligibility[task_id]
        if not summary.exists:
            availability = "missing"
        elif summary.active:
            availability = "active"
        else:
            availability = "inactive"
        return {
            "task_id": task_id,
            "phase": project_task.phase,
            "required": project_task.required,
            "requires": project_task.requires.as_mapping(),
            "title": summary.title,
            "availability": availability,
            "execution_state": summary.execution_state,
            "outcome": summary.outcome.value,
            "executable": summary.executable,
            "eligibility": eligibility.as_mapping(),
        }

    @staticmethod
    def _blocked_tasks(
        snapshot: ProjectExecutionSnapshot,
        task_port: TaskExecutionPort,
    ) -> list[dict[str, Any]]:
        blocked: list[dict[str, Any]] = []
        for task_id, result in snapshot.eligibility.items():
            if result.eligible or task_port.outcome(task_id) != TaskOutcome.NOT_STARTED:
                continue
            blocked.append(
                {
                    "task_id": task_id,
                    "reasons": [reason.as_mapping() for reason in result.reasons],
                }
            )
        return blocked
