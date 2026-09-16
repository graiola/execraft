"""GUI application service for canonical Project Execution.

This module is intentionally HTTP-independent.  It composes the Project
Execution definition/runtime repositories, the TaskExecutionPort adapter and
project-domain engine, while keeping browser payload parsing in ``gui.routes``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from execraft.gui.errors import GuiError
from execraft.project import ProjectError, load_registered_project, validate_project_id
from execraft.roadmap.coordination import RoadmapCanonicalCoordinator, RoadmapCoordinationStore
from execraft.roadmap.models import RoadmapError
from execraft.roadmap.repository import RoadmapRepository
from execraft.project_execution.engine import ProjectExecutionEngine
from execraft.project_execution.events import ProjectEventJournal
from execraft.project_execution.models import (
    ExecutionMode,
    ProjectExecutionDefinition,
    ProjectExecutionError,
    ProjectGate,
    ProjectMilestone,
    ProjectPhase,
    ProjectSchedule,
    ProjectTask,
)
from execraft.project_execution.policy import ProjectExecutionPolicy
from execraft.project_execution.projection import ProjectExecutionProjection
from execraft.project_execution.repository import ProjectExecutionRepository
from execraft.project_execution.runtime_repository import ProjectRuntimeRepository
from execraft.project_execution.service import ProjectExecutionService
from execraft.project_execution.task_port import TaskStartResult
from execraft.project_execution.task_projection import FilesystemTaskExecutionPort

ProjectTaskStarter = Callable[[str, str], TaskStartResult]


@dataclass(frozen=True)
class _ProjectExecutionContext:
    project_id: str
    coordination: Mapping[str, Any]
    definition_repository: ProjectExecutionRepository
    runtime_repository: ProjectRuntimeRepository
    service: ProjectExecutionService
    engine: ProjectExecutionEngine
    task_port: FilesystemTaskExecutionPort
    journal: ProjectEventJournal


class ProjectExecutionGuiService:
    """Project-scoped GUI facade over Project Execution.

    The facade owns no Task execution state.  Assisted Task starts are delegated
    through ``FilesystemTaskExecutionPort`` to an injected canonical Task-level
    starter supplied by the surrounding ControlCenterService.
    """

    def __init__(
        self,
        *,
        control_root: Path,
        state_root: Path,
        task_starter: ProjectTaskStarter | None = None,
    ) -> None:
        self.control_root = Path(control_root).expanduser().resolve()
        self.state_root = Path(state_root).expanduser().resolve()
        self.task_starter = task_starter
        self.projection = ProjectExecutionProjection()

    def snapshot(self, project_id: str) -> dict[str, Any]:
        """Return one reconciled Project Execution workspace projection."""

        context = self._context(project_id)
        if not context.definition_repository.exists():
            snapshot = self._unconfigured_snapshot(context.project_id)
            snapshot["coordination"] = dict(context.coordination)
            return snapshot
        snapshot = context.engine.reconcile()
        definition = context.definition_repository.load()
        runtime = context.runtime_repository.load()
        projected = self.projection.project(
            definition,
            runtime,
            snapshot,
            context.task_port,
        )
        projected.update(
            {
                "configured": True,
                "attention": self._attention(projected),
                "journal_tail": list(context.journal.read()[-50:]),
                "coordination": dict(context.coordination),
            }
        )
        return projected


    def coordination_status(self, project_id: str) -> dict[str, Any]:
        """Return typed coordination state without performing recovery writes."""

        safe_project_id = validate_project_id(str(project_id).strip())
        try:
            project = load_registered_project(self.control_root, safe_project_id)
            roadmaps = RoadmapRepository(project, state_root=self.state_root)
            return RoadmapCanonicalCoordinator(state_root=self.state_root).status(
                project=project,
                roadmaps=roadmaps,
            ).as_mapping()
        except (ProjectError, ProjectExecutionError, RoadmapError, OSError, ValueError) as exc:
            raise GuiError(str(exc)) from exc

    def coordination_history(self, project_id: str, *, limit: int = 20) -> dict[str, Any]:
        """Return recent terminal coordination records without mutating project state."""

        safe_project_id = validate_project_id(str(project_id).strip())
        bounded = max(1, min(int(limit), 100))
        try:
            # Resolve project identity up front so journal inspection follows the
            # same registered-project boundary as all other GUI operations.
            load_registered_project(self.control_root, safe_project_id)
            entries = RoadmapCoordinationStore(
                self.state_root, safe_project_id
            ).read_journal(limit=bounded)
            return {
                "project_id": safe_project_id,
                "limit": bounded,
                "entries": entries,
            }
        except (ProjectError, ProjectExecutionError, RoadmapError, OSError, ValueError) as exc:
            raise GuiError(str(exc)) from exc

    def resolve_coordination(self, project_id: str, *, action: str) -> dict[str, Any]:
        """Apply one safe persisted-intent resolution and return a fresh workspace."""

        safe_project_id = validate_project_id(str(project_id).strip())
        try:
            project = load_registered_project(self.control_root, safe_project_id)
            roadmaps = RoadmapRepository(project, state_root=self.state_root)
            RoadmapCanonicalCoordinator(state_root=self.state_root).resolve(
                project=project,
                roadmaps=roadmaps,
                action=action,
            )
            return self.snapshot(safe_project_id)
        except (ProjectError, ProjectExecutionError, RoadmapError, OSError, ValueError) as exc:
            raise GuiError(str(exc)) from exc

    def initialize(self, project_id: str, *, mode: str = "assisted") -> dict[str, Any]:
        context = self._context(project_id)
        self._assert_coordination_clear(context)
        if context.definition_repository.exists():
            raise GuiError("Project Execution is already initialized")
        if mode not in {item.value for item in ExecutionMode}:
            raise GuiError("Project Execution mode must be observe, assisted, or automatic")
        context.service.initialize(context.project_id, mode=mode)
        return self.snapshot(context.project_id)

    def set_mode(
        self,
        project_id: str,
        *,
        mode: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        if mode not in {item.value for item in ExecutionMode}:
            raise GuiError("Project Execution mode must be observe, assisted, or automatic")
        context = self._configured_context(project_id)
        context.service.set_mode(mode, expected_revision=expected_revision)
        return self.snapshot(context.project_id)

    def set_policy(
        self,
        project_id: str,
        policy: Mapping[str, Any],
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Replace bounded Automatic execution policy."""

        context = self._configured_context(project_id)
        context.service.set_policy(
            ProjectExecutionPolicy.from_mapping(policy),
            expected_revision=expected_revision,
        )
        return self.snapshot(context.project_id)

    def automatic_cycle(self, project_id: str) -> dict[str, Any]:
        """Run one explicit side-effecting Automatic Project cycle."""

        context = self._configured_context(project_id)
        cycle = context.engine.automatic_cycle()
        return {
            "cycle": cycle.as_mapping(),
            "project_execution": self.snapshot(context.project_id),
        }

    def pause(self, project_id: str, *, reason: str = "") -> dict[str, Any]:
        context = self._configured_context(project_id)
        context.engine.pause(reason)
        return self.snapshot(context.project_id)

    def resume(self, project_id: str) -> dict[str, Any]:
        context = self._configured_context(project_id)
        context.engine.resume()
        return self.snapshot(context.project_id)

    def start_task(
        self,
        project_id: str,
        task_id: str,
        *,
        retry_uncertain: bool = False,
    ) -> dict[str, Any]:
        context = self._configured_context(project_id)
        result = context.engine.start_task(task_id, retry_uncertain=retry_uncertain)
        return {
            "result": {
                "accepted": result.accepted,
                "already_started": result.already_started,
                "message": result.message,
            },
            "project_execution": self.snapshot(context.project_id),
        }

    def decide_gate(
        self,
        project_id: str,
        gate_id: str,
        *,
        actor: str,
        decision: str,
        reason: str = "",
    ) -> dict[str, Any]:
        context = self._configured_context(project_id)
        record = context.engine.decide_gate(
            gate_id,
            actor=actor,
            decision=decision,
            reason=reason,
        )
        return {
            "decision": record,
            "project_execution": self.snapshot(context.project_id),
        }

    def waive_gate(
        self,
        project_id: str,
        gate_id: str,
        *,
        actor: str,
        reason: str,
    ) -> dict[str, Any]:
        context = self._configured_context(project_id)
        waiver = context.engine.waive_gate(gate_id, actor=actor, reason=reason)
        return {
            "waiver": waiver,
            "project_execution": self.snapshot(context.project_id),
        }

    def upsert_phase(
        self,
        project_id: str,
        phase: Mapping[str, Any],
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        context = self._configured_context(project_id)
        context.service.upsert_phase(
            ProjectPhase.from_mapping(phase),
            expected_revision=expected_revision,
        )
        return self.snapshot(context.project_id)

    def update_phase_metadata(
        self,
        project_id: str,
        phase_id: str,
        metadata: Mapping[str, Any],
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        context = self._configured_context(project_id)
        definition = context.service.get()
        phase = definition.phase_index.get(phase_id)
        if phase is None:
            raise GuiError(f"Project Phase not found: {phase_id}")
        updated = replace(
            phase,
            title=str(metadata.get("title", phase.title)),
            description=str(metadata.get("description", phase.description)),
            schedule=ProjectSchedule.from_mapping(metadata.get("schedule", phase.schedule.as_mapping())),
        )
        context.service.upsert_phase(updated, expected_revision=expected_revision)
        return self.snapshot(context.project_id)

    def delete_phase(
        self,
        project_id: str,
        phase_id: str,
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        context = self._configured_context(project_id)
        context.service.delete_phase(phase_id, expected_revision=expected_revision)
        return self.snapshot(context.project_id)

    def upsert_gate(
        self,
        project_id: str,
        gate: Mapping[str, Any],
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        context = self._configured_context(project_id)
        context.service.upsert_gate(
            ProjectGate.from_mapping(gate),
            expected_revision=expected_revision,
        )
        return self.snapshot(context.project_id)

    def update_gate_metadata(
        self,
        project_id: str,
        gate_id: str,
        metadata: Mapping[str, Any],
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        context = self._configured_context(project_id)
        definition = context.service.get()
        gate = definition.gate_index.get(gate_id)
        if gate is None:
            raise GuiError(f"Project Gate not found: {gate_id}")
        updated = replace(
            gate,
            title=str(metadata.get("title", gate.title)),
            description=str(metadata.get("description", gate.description)),
            schedule=ProjectSchedule.from_mapping(metadata.get("schedule", gate.schedule.as_mapping())),
        )
        context.service.upsert_gate(updated, expected_revision=expected_revision)
        return self.snapshot(context.project_id)

    def delete_gate(
        self,
        project_id: str,
        gate_id: str,
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        context = self._configured_context(project_id)
        context.service.delete_gate(gate_id, expected_revision=expected_revision)
        return self.snapshot(context.project_id)

    def upsert_milestone(
        self,
        project_id: str,
        milestone: Mapping[str, Any],
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        context = self._configured_context(project_id)
        context.service.upsert_milestone(
            ProjectMilestone.from_mapping(milestone),
            expected_revision=expected_revision,
        )
        return self.snapshot(context.project_id)

    def update_milestone_metadata(
        self,
        project_id: str,
        milestone_id: str,
        metadata: Mapping[str, Any],
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        context = self._configured_context(project_id)
        definition = context.service.get()
        milestone = definition.milestone_index.get(milestone_id)
        if milestone is None:
            raise GuiError(f"Project Milestone not found: {milestone_id}")
        schedule = ProjectSchedule.from_mapping(
            metadata.get(
                "schedule",
                {key: value for key, value in (("start", milestone.start), ("target", milestone.target)) if value},
            )
        )
        updated = replace(
            milestone,
            title=str(metadata.get("title", milestone.title)),
            description=str(metadata.get("description", milestone.description)),
            start=schedule.start,
            target=schedule.target,
        )
        context.service.upsert_milestone(updated, expected_revision=expected_revision)
        return self.snapshot(context.project_id)

    def delete_milestone(
        self,
        project_id: str,
        milestone_id: str,
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        context = self._configured_context(project_id)
        context.service.delete_milestone(
            milestone_id,
            expected_revision=expected_revision,
        )
        return self.snapshot(context.project_id)

    def assign_task(
        self,
        project_id: str,
        task_id: str,
        metadata: Mapping[str, Any],
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        context = self._configured_context(project_id)
        context.service.assign_task(
            ProjectTask.from_mapping(task_id, metadata),
            expected_revision=expected_revision,
        )
        return self.snapshot(context.project_id)

    def remove_task(
        self,
        project_id: str,
        task_id: str,
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        context = self._configured_context(project_id)
        context.service.remove_task(task_id, expected_revision=expected_revision)
        return self.snapshot(context.project_id)

    def _context(self, project_id: str) -> _ProjectExecutionContext:
        safe_project_id = validate_project_id(str(project_id).strip())
        try:
            project = load_registered_project(self.control_root, safe_project_id)
            # Read coordination state before exposing Project Execution. Safe
            # partial writes are finished automatically; genuinely divergent
            # documents remain readable and are surfaced for operator review.
            roadmaps = RoadmapRepository(project, state_root=self.state_root)
            coordinator = RoadmapCanonicalCoordinator(state_root=self.state_root)
            coordination = coordinator.reconcile_if_safe(
                project=project, roadmaps=roadmaps
            )
            definitions = ProjectExecutionRepository(project.directory)
            runtime = ProjectRuntimeRepository(self.state_root, safe_project_id)
            journal = ProjectEventJournal(self.state_root, safe_project_id)
            task_port = FilesystemTaskExecutionPort(
                project=project,
                state_root=self.state_root,
                start_action=(
                    (lambda task_id: self.task_starter(safe_project_id, task_id))
                    if self.task_starter is not None
                    else None
                ),
            )
            service = ProjectExecutionService(
                definitions,
                task_exists=lambda task_id: task_port.describe(task_id).exists,
                runtime_repository=runtime,
            )
            engine = ProjectExecutionEngine(
                definition_repository=definitions,
                runtime_repository=runtime,
                task_port=task_port,
                journal=journal,
            )
            return _ProjectExecutionContext(
                project_id=safe_project_id,
                coordination=coordination.as_mapping(),
                definition_repository=definitions,
                runtime_repository=runtime,
                service=service,
                engine=engine,
                task_port=task_port,
                journal=journal,
            )
        except (ProjectError, ProjectExecutionError, RoadmapError, OSError, ValueError) as exc:
            raise GuiError(str(exc)) from exc

    def _configured_context(self, project_id: str) -> _ProjectExecutionContext:
        context = self._context(project_id)
        if not context.definition_repository.exists():
            raise GuiError("Project Execution is not initialized")
        self._assert_coordination_clear(context)
        return context

    @staticmethod
    def _assert_coordination_clear(context: _ProjectExecutionContext) -> None:
        coordination = context.coordination
        if not coordination.get("pending"):
            return
        raise GuiError(
            "canonical Roadmap coordination requires operator resolution before "
            "Project Execution mutation: "
            + str(coordination.get("message", "pending coordination conflict"))
        )

    @staticmethod
    def _unconfigured_snapshot(project_id: str) -> dict[str, Any]:
        return {
            "configured": False,
            "schema_version": 1,
            "project_id": project_id,
            "definition_revision": 0,
            "definition_digest": "",
            "mode": ExecutionMode.ASSISTED.value,
            "policy": ProjectExecutionPolicy().as_mapping(),
            "automatic": {},
            "held": False,
            "hold_reason": "",
            "phases": [],
            "gates": [],
            "milestones": [],
            "tasks": [],
            "ready_tasks": [],
            "blocked_tasks": [],
            "attention": [],
            "journal_tail": [],
        }

    @staticmethod
    def _attention(projected: Mapping[str, Any]) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        if projected.get("held"):
            rows.append(
                {
                    "kind": "project_hold",
                    "id": str(projected.get("project_id", "")),
                    "message": str(projected.get("hold_reason", "Project execution held")),
                }
            )
        for gate in projected.get("gates", []):
            state = str(gate.get("state", ""))
            if state in {"failed", "awaiting_decision"}:
                rows.append(
                    {
                        "kind": f"gate_{state}",
                        "id": str(gate.get("id", "")),
                        "message": str(gate.get("title", gate.get("id", ""))),
                    }
                )
        for task in projected.get("tasks", []):
            if str(task.get("outcome", "")) == "failed":
                rows.append(
                    {
                        "kind": "task_failed",
                        "id": str(task.get("task_id", "")),
                        "message": str(task.get("title", task.get("task_id", ""))),
                    }
                )
        for phase in projected.get("phases", []):
            if str(phase.get("health", "")) in {"blocked", "late"}:
                rows.append(
                    {
                        "kind": f"phase_{phase.get('health', '')}",
                        "id": str(phase.get("id", "")),
                        "message": str(phase.get("title", phase.get("id", ""))),
                    }
                )
        for milestone in projected.get("milestones", []):
            if str(milestone.get("health", "")) == "late":
                rows.append(
                    {
                        "kind": "milestone_late",
                        "id": str(milestone.get("id", "")),
                        "message": str(milestone.get("title", milestone.get("id", ""))),
                    }
                )
        return rows


__all__ = ["ProjectExecutionGuiService", "ProjectTaskStarter"]
