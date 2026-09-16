"""Read-model projection joining roadmap planning data to real task state."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

from execraft.orchestrate.identity import resolve_storage_identity
from execraft.project import ProjectDescriptor
from execraft.project_execution.repository import ProjectExecutionRepository
from execraft.project_execution.runtime_repository import ProjectRuntimeRepository
from execraft.workspace.task_git import TaskGitError, TaskManifest

from .models import Roadmap, RoadmapItem


ActiveTaskGetter = Callable[[], object | None]


class RoadmapProjection:
    """Create browser-facing roadmap views without mutating task state."""

    def __init__(
        self,
        *,
        control_root: Path,
        state_root: Path,
        active_task: ActiveTaskGetter,
    ) -> None:
        self.control_root = Path(control_root).expanduser().resolve()
        self.state_root = Path(state_root).expanduser().resolve()
        self._active_task = active_task

    def project(self, project: ProjectDescriptor, roadmap: Roadmap) -> dict[str, Any]:
        tasks = self._task_index(project)
        projected_items: list[dict[str, Any]] = []
        linked_active: set[str] = set()
        definition_repo = ProjectExecutionRepository(project.directory)
        project_execution = definition_repo.load_optional()
        runtime = ProjectRuntimeRepository(self.state_root, project.id).load()
        for item in roadmap.items:
            row = item.as_mapping()
            if item.kind == "task":
                task = tasks.get(item.task_id)
                if task and task["availability"] == "active":
                    linked_active.add(item.task_id)
                row["task"] = task or self._missing_task(item.task_id)
            elif item.kind in {"phase", "gate", "milestone"} and item.project_asset_id:
                row.update(
                    self._project_asset_projection(
                        item.kind, item.project_asset_id, project_execution, runtime
                    )
                )
            row["project_phase"] = self._project_phase_projection(
                item, project_execution
            )
            projected_items.append(row)
        unscheduled = [
            task
            for task in tasks.values()
            if task["availability"] == "active"
            and task["status"] != "invalid"
            and task["id"] not in linked_active
        ]
        unscheduled.sort(
            key=lambda item: (item.get("modified_at", ""), item["id"]), reverse=True
        )
        lanes = _lane_names(roadmap.items)
        return {
            "schema_version": roadmap.schema_version,
            "project_id": project.id,
            "id": roadmap.id,
            "title": roadmap.title,
            "description": roadmap.description,
            "revision": roadmap.revision,
            "project_execution_configured": project_execution is not None,
            "project_execution_revision": project_execution.revision if project_execution else 0,
            "created_at": roadmap.created_at,
            "updated_at": roadmap.updated_at,
            "items": projected_items,
            "relations": [relation.as_mapping() for relation in roadmap.relations],
            "lanes": lanes,
            "unscheduled_tasks": unscheduled,
            "statistics": {
                "items": len(roadmap.items),
                "linked_tasks": sum(1 for item in roadmap.items if item.kind == "task"),
                "planned_tasks": sum(
                    1 for item in roadmap.items if item.kind == "planned_task"
                ),
                "milestones": sum(
                    1 for item in roadmap.items if item.kind == "milestone"
                ),
                "gates": sum(1 for item in roadmap.items if item.kind == "gate"),
                "unscheduled_tasks": len(unscheduled),
            },
        }

    def task(self, project: ProjectDescriptor, task_id: str) -> dict[str, Any] | None:
        """Return one projected task row, including archived/invalid rows."""

        return self._task_index(project).get(task_id)

    def linkable_task(
        self, project: ProjectDescriptor, task_id: str
    ) -> dict[str, Any] | None:
        """Return an active, valid task that may be newly linked to a roadmap."""

        task = self.task(project, task_id)
        if not task or task["availability"] != "active" or task["status"] == "invalid":
            return None
        return task

    def _task_index(self, project: ProjectDescriptor) -> dict[str, dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        active_root = project.directory / "tasks"
        if active_root.is_dir():
            for directory in sorted(active_root.iterdir()):
                row = self._read_task(project.id, directory, availability="active")
                if row:
                    rows[row["id"]] = row
        archive_root = (
            self.control_root / "projects" / ".archive" / "tasks" / project.id
        )
        if archive_root.is_dir():
            for directory in sorted(archive_root.iterdir()):
                row = self._read_task(project.id, directory, availability="archived")
                if row and row["id"] not in rows:
                    rows[row["id"]] = row
        return rows

    def _read_task(
        self, project_id: str, directory: Path, *, availability: str
    ) -> dict[str, Any] | None:
        manifest_path = directory / "TASK.yaml"
        if not directory.is_dir() or directory.is_symlink() or not manifest_path.is_file():
            return None
        try:
            raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
            manifest = TaskManifest.from_mapping(raw)
            modified_at = datetime.fromtimestamp(
                manifest_path.stat().st_mtime, timezone.utc
            ).isoformat()
            row: dict[str, Any] = {
                "id": manifest.id,
                "title": manifest.title or manifest.id,
                "status": manifest.status or "draft",
                "repositories": [repository.id for repository in manifest.repositories],
                "availability": availability,
                "modified_at": modified_at,
                "has_plan": (directory / "PLAN.graph.yaml").is_file(),
                "current": False,
                "runtime_state": "",
                "completed_packages": 0,
                "total_packages": 0,
                "progress_percent": 0,
            }
        except (OSError, ValueError, TaskGitError, yaml.YAMLError) as exc:
            return {
                "id": directory.name,
                "title": directory.name,
                "status": "invalid",
                "repositories": [],
                "availability": availability,
                "modified_at": "",
                "has_plan": False,
                "current": False,
                "runtime_state": "unreadable",
                "completed_packages": 0,
                "total_packages": 0,
                "progress_percent": 0,
                "error": str(exc),
            }
        active = self._active_task()
        row["current"] = bool(
            active
            and getattr(active, "project_id", "") == project_id
            and getattr(active, "task_id", "") == manifest.id
        )
        if availability == "active":
            row.update(self._runtime(project_id, manifest.id))
        return row

    def _runtime(self, project_id: str, task_id: str) -> dict[str, Any]:
        try:
            identity = resolve_storage_identity(
                self.state_root,
                project_id=project_id,
                task_id=task_id,
                create=False,
            )
            state_path = identity.state_dir / "state.json"
            if not state_path.is_file():
                return {}
            raw = json.loads(state_path.read_text(encoding="utf-8"))
            if not isinstance(raw, Mapping):
                return {"runtime_state": "unreadable"}
            completed = max(0, int(raw.get("completed_packages", 0)))
            total = max(0, int(raw.get("total_packages", 0)))
            return {
                "runtime_state": str(raw.get("state", "")),
                "completed_packages": completed,
                "total_packages": total,
                "progress_percent": (
                    min(100, round((completed / total) * 100)) if total else 0
                ),
            }
        except (OSError, ValueError, TypeError):
            return {"runtime_state": "unreadable"}

    @staticmethod
    def _project_asset_projection(kind: str, asset_id: str, definition, runtime) -> dict[str, Any]:
        if definition is None:
            return {"title": asset_id, "description": "", "project_asset": {"id": asset_id, "kind": kind, "status": "missing"}}
        index = {
            "phase": definition.phase_index,
            "gate": definition.gate_index,
            "milestone": definition.milestone_index,
        }[kind]
        asset = index.get(asset_id)
        if asset is None:
            return {"title": asset_id, "description": "", "project_asset": {"id": asset_id, "kind": kind, "status": "missing"}}
        if kind == "phase":
            schedule = asset.schedule.as_mapping()
            state = ((runtime.executor.get("phase_states") or {}).get(asset_id) or {}).get("state", "planned")
            health = ((runtime.executor.get("phase_states") or {}).get(asset_id) or {}).get("health", "on_track")
        elif kind == "gate":
            schedule = asset.schedule.as_mapping()
            state = (runtime.gates.get(asset_id) or {}).get("state", "waiting")
            health = ""
        else:
            schedule = {key: value for key, value in (("start", asset.start), ("target", asset.target)) if value}
            state = "achieved" if asset_id in runtime.milestone_achievements else ("cancelled" if asset_id in runtime.cancelled_milestones else "pending")
            health = ""
        result = {
            "title": asset.title,
            "description": asset.description,
            "project_asset": {"id": asset_id, "kind": kind, "state": state},
        }
        if health:
            result["project_asset"]["health"] = health
        if schedule:
            result["schedule"] = schedule
        return result

    @staticmethod
    def _project_phase_projection(
        item: RoadmapItem, definition
    ) -> dict[str, Any]:
        """Return a view-only Phase grouping projection for one Roadmap row.

        Roadmap v2 remains the owner of lane/order only.  This metadata is
        derived from ``PROJECT_EXECUTION.yaml`` so the browser can optionally
        group the planning canvas by canonical Phase without persisting a
        second Phase assignment in Roadmap YAML.
        """

        if definition is None:
            return {
                "id": "__unassigned__",
                "title": "Unassigned",
                "order": 1_000_000,
                "kind": "unassigned",
            }

        phase_ids: list[str] = []
        if item.kind == "task":
            project_task = definition.task_index.get(item.task_id)
            if project_task is not None:
                phase_ids.append(project_task.phase)
        elif item.kind == "phase" and item.project_asset_id:
            phase_ids.append(item.project_asset_id)
        elif item.kind == "milestone" and item.project_asset_id:
            phase_ids.extend(
                phase.id
                for phase in definition.phases
                if item.project_asset_id in phase.milestones
            )
        elif item.kind == "gate" and item.project_asset_id:
            phase_ids.extend(
                phase.id
                for phase in definition.phases
                if item.project_asset_id in phase.entry_gates
                or item.project_asset_id in phase.exit_gates
            )

        unique = list(dict.fromkeys(phase_ids))
        if len(unique) > 1:
            return {
                "id": "__cross_phase__",
                "title": "Cross-phase",
                "order": 999_999,
                "kind": "cross_phase",
            }
        if unique:
            phase_id = unique[0]
            phase = definition.phase_index.get(phase_id)
            if phase is not None:
                order = next(
                    (
                        index
                        for index, candidate in enumerate(definition.phases)
                        if candidate.id == phase_id
                    ),
                    999_998,
                )
                return {
                    "id": phase.id,
                    "title": phase.title,
                    "order": order,
                    "kind": "phase",
                }

        return {
            "id": "__unassigned__",
            "title": "Unassigned",
            "order": 1_000_000,
            "kind": "unassigned",
        }

    @staticmethod
    def _missing_task(task_id: str) -> dict[str, Any]:
        return {
            "id": task_id,
            "title": task_id,
            "status": "missing",
            "repositories": [],
            "availability": "missing",
            "modified_at": "",
            "has_plan": False,
            "current": False,
            "runtime_state": "",
            "completed_packages": 0,
            "total_packages": 0,
            "progress_percent": 0,
        }


def _lane_names(items: tuple[RoadmapItem, ...]) -> list[str]:
    lanes: list[str] = []
    for item in sorted(items, key=lambda value: (value.order, value.id)):
        if item.lane not in lanes:
            lanes.append(item.lane)
    return lanes or ["General"]
