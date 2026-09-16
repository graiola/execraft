"""Read-only projections from canonical Execraft domains into export models."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml

from execraft.orchestrate.identity import resolve_storage_identity
from execraft.orchestrate.models import TaskExecutionStateRecord, WorkPackageStage
from execraft.orchestrate.normalizer import load_plan_graph_file
from execraft.project import load_registered_project, validate_project_id
from execraft.project_execution.milestones import MilestoneRequirementEvaluator
from execraft.project_execution.repository import ProjectExecutionRepository
from execraft.project_execution.runtime_repository import ProjectRuntimeRepository
from execraft.project_execution.task_projection import FilesystemTaskExecutionPort
from execraft.roadmap import RoadmapService
from execraft.workspace.task_git import TaskManifest

from .models import (
    ExportError,
    ProjectReportPresentation,
    RoadmapEdgePresentation,
    RoadmapNodePresentation,
    RoadmapPresentation,
    TaskReportPresentation,
    WorkPackagePresentation,
)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _state_label(value: object) -> str:
    return str(value or "").strip().replace("_", " ").title()


class ExportProjectionBuilder:
    """Build deterministic presentation models without mutating domain state."""

    def __init__(self, *, control_root: Path, state_root: Path) -> None:
        self.control_root = Path(control_root).expanduser().resolve()
        self.state_root = Path(state_root).expanduser().resolve()
        self.roadmaps = RoadmapService(
            control_root=self.control_root,
            state_root=self.state_root,
            active_task=lambda: None,
        )
        self._milestones = MilestoneRequirementEvaluator()

    def roadmap(self, project_id: str, roadmap_id: str) -> RoadmapPresentation:
        raw = self.roadmaps.get(validate_project_id(project_id), roadmap_id)
        nodes = tuple(self._roadmap_node(item) for item in raw.get("items", []))
        edges = tuple(
            RoadmapEdgePresentation(
                source=str(row.get("from", "")),
                target=str(row.get("to", "")),
                kind=str(row.get("kind", "blocks")),
            )
            for row in raw.get("relations", [])
            if isinstance(row, Mapping)
        )
        lanes = tuple(str(value) for value in raw.get("lanes", []) if str(value).strip())
        if not lanes:
            lanes = tuple(dict.fromkeys(node.lane for node in nodes)) or ("General",)
        statistics = {
            str(key): int(value)
            for key, value in (raw.get("statistics") or {}).items()
            if isinstance(value, int) and not isinstance(value, bool)
        }
        return RoadmapPresentation(
            project_id=str(raw.get("project_id", project_id)),
            roadmap_id=str(raw.get("id", roadmap_id)),
            title=str(raw.get("title", roadmap_id)),
            description=str(raw.get("description", "")),
            revision=int(raw.get("revision", 0)),
            generated_at=_now(),
            nodes=nodes,
            edges=edges,
            lanes=lanes,
            statistics=statistics,
        )

    def project_report(self, project_id: str) -> ProjectReportPresentation:
        project = load_registered_project(self.control_root, validate_project_id(project_id))
        definition = ProjectExecutionRepository(project.directory).load_optional()
        if definition is None:
            raise ExportError(f"Project Execution is not configured for {project.id}")
        runtime = ProjectRuntimeRepository(self.state_root, project.id).load()
        task_port = FilesystemTaskExecutionPort(project=project, state_root=self.state_root)
        phase_runtime = runtime.executor.get("phase_states") or {}
        phases: list[Mapping[str, Any]] = []
        for phase in definition.phases:
            state = phase_runtime.get(phase.id, {}) if isinstance(phase_runtime, Mapping) else {}
            phases.append({
                **phase.as_mapping(),
                "state": str(state.get("state", "planned")),
                "health": str(state.get("health", "on_track")),
            })

        gates: list[Mapping[str, Any]] = []
        for gate in definition.gates:
            observed = runtime.gates.get(gate.id, {})
            gates.append({
                **gate.as_mapping(),
                "state": str(observed.get("state", "waiting")),
                "input_fingerprint": str(observed.get("input_fingerprint", "")),
            })

        milestones: list[Mapping[str, Any]] = []
        for milestone in definition.milestones:
            projection = self._milestones.evaluate(
                milestone,
                runtime=runtime,
                task_port=task_port,
            )
            milestones.append({
                **milestone.as_mapping(),
                "state": projection.state.value,
                "health": projection.health.value,
                "missing_requirements": list(projection.missing),
                "achievement": runtime.milestone_achievements.get(milestone.id, {}),
            })

        tasks: list[Mapping[str, Any]] = []
        for project_task in definition.tasks:
            summary = task_port.describe(project_task.task_id)
            evidence = task_port.evidence(project_task.task_id)
            tasks.append({
                "task_id": project_task.task_id,
                "title": summary.title or project_task.task_id,
                "phase": project_task.phase,
                "required": project_task.required,
                "state": summary.execution_state,
                "outcome": summary.outcome.value,
                "verification": evidence.verification.outcome,
                "repositories": dict(evidence.repository_revisions),
            })

        return ProjectReportPresentation(
            project_id=project.id,
            title=project.id,
            description=project.description,
            generated_at=_now(),
            mode=definition.mode.value,
            held=runtime.held,
            phases=tuple(phases),
            gates=tuple(gates),
            milestones=tuple(milestones),
            tasks=tuple(tasks),
            statistics={
                "phases": len(phases),
                "gates": len(gates),
                "milestones": len(milestones),
                "tasks": len(tasks),
                "completed_tasks": sum(row["outcome"] == "completed" for row in tasks),
                "passed_gates": sum(row["state"] == "passed" for row in gates),
                "achieved_milestones": sum(row["state"] == "achieved" for row in milestones),
            },
        )

    def task_report(self, project_id: str, task_id: str) -> TaskReportPresentation:
        project = load_registered_project(self.control_root, validate_project_id(project_id))
        dossier = project.directory / "tasks" / task_id
        manifest_path = dossier / "TASK.yaml"
        if not manifest_path.is_file() or manifest_path.is_symlink():
            raise ExportError(f"Task not found: {task_id}")
        try:
            raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
            manifest = TaskManifest.from_mapping(raw)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            raise ExportError(f"cannot read Task {task_id}: {exc}") from exc

        plan, report = load_plan_graph_file(dossier / "PLAN.graph.yaml")
        if report.errors:
            raise ExportError("Task PLAN cannot be exported: " + "; ".join(report.errors))
        state = self._task_state(project.id, task_id)
        runtime_packages = {
            package.id: package for package in (state.plan_graph.work_packages if state else [])
        }
        work_packages: list[WorkPackagePresentation] = []
        for declared in plan.work_packages:
            package = runtime_packages.get(declared.id, declared)
            acceptance_total = len(package.acceptance_criteria)
            acceptance_verified = sum(item.verified for item in package.acceptance_criteria)
            work_packages.append(
                WorkPackagePresentation(
                    id=package.id,
                    title=package.title or package.id,
                    stage=package.stage.value,
                    status=package.status,
                    risk=package.risk,
                    priority=package.priority,
                    progress_label=self._work_package_progress(package.stage, package.status),
                    dependencies=tuple(package.dependencies),
                    repositories=tuple(package.affected_repositories),
                    acceptance_total=acceptance_total,
                    acceptance_verified=acceptance_verified,
                )
            )
        total = state.total_packages if state else len(work_packages)
        completed = state.completed_packages if state else sum(
            package.stage == WorkPackageStage.COMPLETED for package in plan.work_packages
        )
        progress = min(100, round((completed / total) * 100)) if total else 0
        repositories = tuple(repository.id for repository in manifest.repositories)
        return TaskReportPresentation(
            project_id=project.id,
            task_id=manifest.id,
            title=manifest.title or manifest.id,
            status=manifest.status,
            runtime_state=state.state.value if state else "not_started",
            progress_percent=progress,
            generated_at=_now(),
            repositories=repositories,
            work_packages=tuple(work_packages),
            summary={
                "completed_packages": completed,
                "total_packages": total,
                "has_plan": bool(work_packages),
                "error_message": state.error_message if state else "",
            },
        )

    def _task_state(self, project_id: str, task_id: str) -> TaskExecutionStateRecord | None:
        try:
            identity = resolve_storage_identity(
                self.state_root,
                project_id=project_id,
                task_id=task_id,
                create=False,
            )
            path = identity.state_dir / "state.json"
            if not path.is_file() or path.is_symlink():
                return None
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return None
            return TaskExecutionStateRecord.from_mapping(raw)
        except (OSError, ValueError, TypeError):
            return None

    @staticmethod
    def _roadmap_node(item: object) -> RoadmapNodePresentation:
        if not isinstance(item, Mapping):
            raise ExportError("roadmap projection contains an invalid item")
        kind = str(item.get("kind", "planned_task"))
        schedule = item.get("schedule") if isinstance(item.get("schedule"), Mapping) else {}
        task = item.get("task") if isinstance(item.get("task"), Mapping) else {}
        asset = item.get("project_asset") if isinstance(item.get("project_asset"), Mapping) else {}
        title = str(
            task.get("title")
            or item.get("title")
            or asset.get("title")
            or item.get("project_asset_id")
            or item.get("task_id")
            or item.get("id")
            or "Untitled"
        )
        if kind == "task":
            state = str(task.get("runtime_state") or task.get("status") or "")
            health = ""
            progress = int(task.get("progress_percent", 0) or 0)
            subtitle = str(task.get("status", ""))
            current = bool(task.get("current", False))
        else:
            state = str(asset.get("state") or item.get("state") or "")
            health = str(asset.get("health") or item.get("health") or "")
            progress = 100 if state in {"complete", "passed", "achieved"} else 0
            subtitle = str(item.get("description", ""))
            current = False
        return RoadmapNodePresentation(
            id=str(item.get("id", "")),
            kind=kind,
            title=title,
            subtitle=subtitle,
            lane=str(item.get("lane", "General")) or "General",
            order=int(item.get("order", 0) or 0),
            state=state,
            health=health,
            progress_percent=max(0, min(100, progress)),
            start=str(schedule.get("start", "")),
            target=str(schedule.get("target", "")),
            current=current,
        )

    @staticmethod
    def _work_package_progress(stage: WorkPackageStage, status: str) -> str:
        if stage == WorkPackageStage.COMPLETED or status == "completed":
            return "Complete"
        return _state_label(stage.value)
