"""Project-level roadmap application service.

Roadmap mutations are deliberately planning-only.  No method in this service
starts, pauses, blocks, or otherwise changes task orchestration.  Linking a task
only creates a reference to its canonical dossier.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from execraft.project import load_registered_project, validate_project_id
from execraft.project_execution.models import (
    GateCriterion,
    ProjectExecutionDefinition,
    ProjectGate,
    ProjectMilestone,
    ProjectPhase,
    ProjectSchedule,
)
from execraft.project_execution.errors import ProjectExecutionConflictError
from execraft.project_execution.repository import ProjectExecutionRepository
from execraft.project_execution.validation import validate_definition

from .coordination import RoadmapCanonicalCoordinator, RoadmapCoordinationConflictError
from .migration import RoadmapV1Migrator
from .models import (
    Roadmap,
    RoadmapConflictError,
    RoadmapError,
    RoadmapItem,
    RoadmapNotFoundError,
    RoadmapRelation,
    RoadmapSchedule,
    validate_roadmap_id,
    validate_task_reference_id,
)
from .projection import RoadmapProjection
from .repository import RoadmapRepository


ActiveTaskGetter = Callable[[], object | None]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(value: str, *, fallback: str = "roadmap") -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", str(value).strip().lower()).strip("-")
    if not normalized:
        normalized = fallback
    if normalized[0].isdigit():
        normalized = f"{fallback}-{normalized}"
    return normalized[:72]


class RoadmapService:
    """Coordinate roadmap persistence, task references, and read projections."""

    def __init__(
        self,
        *,
        control_root: Path,
        state_root: Path,
        active_task: ActiveTaskGetter,
    ) -> None:
        self.control_root = Path(control_root).expanduser().resolve()
        self.state_root = Path(state_root).expanduser().resolve()
        self.projection = RoadmapProjection(
            control_root=self.control_root,
            state_root=self.state_root,
            active_task=active_task,
        )

    def list(self, project_id: str) -> dict[str, Any]:
        project = self._project(project_id)
        repository = self._repository(project)
        coordination = self._coordination_status(project, repository)
        roadmaps = repository.list()
        return {
            "project_id": project.id,
            "roadmaps": [self._summary(roadmap) for roadmap in roadmaps],
            "coordination": coordination,
        }

    def get(self, project_id: str, roadmap_id: str) -> dict[str, Any]:
        project = self._project(project_id)
        repository = self._repository(project)
        coordination = self._coordination_status(project, repository)
        roadmap = repository.load(roadmap_id)
        result = self.projection.project(project, roadmap)
        result["sha256"] = repository.sha256(roadmap.id)
        result["coordination"] = coordination
        return result

    def create(
        self,
        project_id: str,
        *,
        title: str,
        description: str = "",
        roadmap_id: str = "",
    ) -> dict[str, Any]:
        project = self._project(project_id)
        repository = self._repository(project)
        requested = roadmap_id.strip() or _slug(title)
        candidate = validate_roadmap_id(requested)
        if not roadmap_id.strip():
            candidate = self._available_id(repository, candidate)
        now = _utc_now()
        roadmap = Roadmap(
            id=candidate,
            title=title,
            description=description,
            revision=1,
            created_at=now,
            updated_at=now,
        )
        repository.create(roadmap)
        return self.get(project.id, roadmap.id)

    def migration_preview(self, project_id: str, roadmap_id: str) -> dict[str, Any]:
        """Return the deterministic v1 -> v2 promotion plan without mutating state."""

        project = self._project(project_id)
        repository = self._repository(project)
        self._assert_coordination_clear(project, repository)
        return RoadmapV1Migrator(project=project, roadmaps=repository).preview(roadmap_id).as_mapping()

    def migrate_v1(
        self,
        project_id: str,
        roadmap_id: str,
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Promote one legacy Roadmap to schema v2 and return its projection."""

        project = self._project(project_id)
        repository = self._repository(project)
        self._assert_coordination_clear(project, repository)
        RoadmapV1Migrator(project=project, roadmaps=repository).apply(
            roadmap_id, expected_revision=expected_revision
        )
        return self.get(project.id, roadmap_id)

    def update_metadata(
        self,
        project_id: str,
        roadmap_id: str,
        *,
        expected_revision: int,
        title: str,
        description: str = "",
    ) -> dict[str, Any]:
        project, repository, roadmap = self._load(project_id, roadmap_id)
        updated = replace(
            roadmap,
            title=title,
            description=description,
            updated_at=_utc_now(),
        )
        repository.save(updated, expected_revision=expected_revision)
        return self.get(project.id, roadmap.id)

    def upsert_item(
        self,
        project_id: str,
        roadmap_id: str,
        *,
        expected_revision: int,
        expected_project_execution_revision: int = 0,
        raw_item: Mapping[str, Any],
    ) -> dict[str, Any]:
        project, repository, roadmap = self._load(project_id, roadmap_id)
        self._assert_roadmap_revision(roadmap, expected_revision)
        data = dict(raw_item)
        if not str(data.get("id", "")).strip():
            kind = str(data.get("kind", "planned_task")).strip().lower()
            prefix = {
                "task": "task",
                "planned_task": "planned",
                "milestone": "milestone",
                "phase": "phase",
                "gate": "gate",
            }.get(kind, "item")
            data["id"] = f"{prefix}-{uuid.uuid4().hex[:10]}"
        kind = str(data.get("kind", "planned_task")).strip().lower()
        current_execution: ProjectExecutionDefinition | None = None
        desired_execution: ProjectExecutionDefinition | None = None
        if kind in {"phase", "gate", "milestone"}:
            data, current_execution, desired_execution = self._prepare_project_asset_item(
                project,
                data,
                expected_project_execution_revision=expected_project_execution_revision,
            )
        item = RoadmapItem.from_mapping(data)
        if item.kind == "task" and self.projection.linkable_task(project, item.task_id) is None:
            raise RoadmapNotFoundError(
                f"active valid task not found: {item.task_id}"
            )
        items = list(roadmap.items)
        existing_index = next(
            (index for index, current in enumerate(items) if current.id == item.id), None
        )
        if existing_index is None:
            if item.order == 0 and items:
                item = replace(item, order=max(current.order for current in items) + 10)
            items.append(item)
        else:
            items[existing_index] = item
        updated = replace(
            roadmap,
            items=tuple(items),
            updated_at=_utc_now(),
        )
        if desired_execution is not None and current_execution is not None:
            self._coordinator().coordinate(
                project=project,
                roadmaps=repository,
                current_roadmap=roadmap,
                desired_roadmap=updated,
                current_project_execution=current_execution,
                desired_project_execution=desired_execution,
                operation=f"upsert_{kind}",
            )
        else:
            repository.save(updated, expected_revision=expected_revision)
        return self.get(project.id, roadmap.id)


    def move_item(
        self,
        project_id: str,
        roadmap_id: str,
        *,
        expected_revision: int,
        expected_project_execution_revision: int = 0,
        item_id: str,
        lane: str,
        start: str = "",
        target: str = "",
        target_item_id: str = "",
        placement: str = "before",
    ) -> dict[str, Any]:
        """Move one item atomically in time and vertical roadmap order.

        The GUI sends semantic placement intent rather than attempting to
        maintain sparse order values itself.  Re-normalising the compact item
        sequence here keeps ordering deterministic across concurrent clients
        and makes cross-lane drag/drop a single optimistic mutation.
        """

        project, repository, roadmap = self._load(project_id, roadmap_id)
        self._assert_roadmap_revision(roadmap, expected_revision)
        safe_id = validate_roadmap_id(item_id, label="roadmap item id")
        destination_lane = str(lane or "").strip() or "General"
        if len(destination_lane) > 160:
            raise RoadmapError("roadmap lane exceeds 160 characters")
        normalized_placement = str(placement or "before").strip().lower()
        if normalized_placement not in {"before", "after", "end"}:
            raise RoadmapError("roadmap move placement must be before, after, or end")

        ordered = sorted(roadmap.items, key=lambda item: (item.order, item.id))
        moving = next((item for item in ordered if item.id == safe_id), None)
        if moving is None:
            raise RoadmapNotFoundError(f"roadmap item not found: {safe_id}")
        ordered = [item for item in ordered if item.id != safe_id]

        target_id = str(target_item_id or "").strip()
        insert_at = len(ordered)
        if target_id and normalized_placement != "end":
            safe_target = validate_roadmap_id(target_id, label="target roadmap item id")
            if safe_target == safe_id:
                safe_target = ""
            if safe_target:
                target_index = next(
                    (index for index, item in enumerate(ordered) if item.id == safe_target),
                    None,
                )
                if target_index is None:
                    raise RoadmapNotFoundError(
                        f"target roadmap item not found: {safe_target}"
                    )
                destination_lane = ordered[target_index].lane
                insert_at = target_index + (1 if normalized_placement == "after" else 0)
        elif normalized_placement == "end":
            matching = [
                index for index, item in enumerate(ordered) if item.lane == destination_lane
            ]
            insert_at = (matching[-1] + 1) if matching else len(ordered)

        current_execution: ProjectExecutionDefinition | None = None
        desired_execution: ProjectExecutionDefinition | None = None
        if moving.kind in {"phase", "gate", "milestone"}:
            if start or target:
                execution_repository = ProjectExecutionRepository(project.directory)
                current_execution = execution_repository.load()
                if (
                    expected_project_execution_revision
                    and current_execution.revision
                    != int(expected_project_execution_revision)
                ):
                    raise ProjectExecutionConflictError(
                        "Project Execution changed since the Roadmap projection was loaded; "
                        "refresh before moving canonical project assets "
                        f"(expected revision {expected_project_execution_revision}, "
                        f"current {current_execution.revision})"
                    )
                desired_execution = self._definition_with_project_asset_schedule(
                    current_execution,
                    moving.kind,
                    moving.project_asset_id,
                    start=start,
                    target=target,
                )
            moved = replace(moving, lane=destination_lane)
        else:
            moved = replace(
                moving,
                lane=destination_lane,
                schedule=RoadmapSchedule(start=start, target=target),
            )
        ordered.insert(insert_at, moved)
        renumbered = tuple(
            replace(item, order=(index + 1) * 10) for index, item in enumerate(ordered)
        )
        updated = replace(roadmap, items=renumbered, updated_at=_utc_now())
        if desired_execution is not None and current_execution is not None:
            self._coordinator().coordinate(
                project=project,
                roadmaps=repository,
                current_roadmap=roadmap,
                desired_roadmap=updated,
                current_project_execution=current_execution,
                desired_project_execution=desired_execution,
                operation=f"move_{moving.kind}",
            )
        else:
            repository.save(updated, expected_revision=expected_revision)
        return self.get(project.id, roadmap.id)

    def move_lane(
        self,
        project_id: str,
        roadmap_id: str,
        *,
        expected_revision: int,
        lane: str,
        target_lane: str,
        placement: str = "before",
    ) -> dict[str, Any]:
        """Reorder one complete roadmap lane atomically.

        Lane order is intentionally derived from item order rather than stored
        as a second source of truth.  Moving a lane therefore rebuilds the
        compact item sequence while preserving every item's lane, schedule,
        identity, and the relative order of items inside each lane.
        """

        project, repository, roadmap = self._load(project_id, roadmap_id)
        source_lane = str(lane or "").strip() or "General"
        destination_lane = str(target_lane or "").strip() or "General"
        for value in (source_lane, destination_lane):
            if len(value) > 160:
                raise RoadmapError("roadmap lane exceeds 160 characters")
        normalized_placement = str(placement or "before").strip().lower()
        if normalized_placement not in {"before", "after"}:
            raise RoadmapError("roadmap lane placement must be before or after")
        if source_lane == destination_lane:
            return self.get(project.id, roadmap.id)

        ordered = sorted(roadmap.items, key=lambda item: (item.order, item.id))
        groups: dict[str, list[RoadmapItem]] = {}
        lane_order: list[str] = []
        for item in ordered:
            if item.lane not in groups:
                groups[item.lane] = []
                lane_order.append(item.lane)
            groups[item.lane].append(item)

        if source_lane not in groups:
            raise RoadmapNotFoundError(f"roadmap lane not found: {source_lane}")
        if destination_lane not in groups:
            raise RoadmapNotFoundError(f"target roadmap lane not found: {destination_lane}")

        lane_order.remove(source_lane)
        target_index = lane_order.index(destination_lane)
        insert_at = target_index + (1 if normalized_placement == "after" else 0)
        lane_order.insert(insert_at, source_lane)

        flattened = [item for lane_name in lane_order for item in groups[lane_name]]
        renumbered = tuple(
            replace(item, order=(index + 1) * 10)
            for index, item in enumerate(flattened)
        )
        updated = replace(roadmap, items=renumbered, updated_at=_utc_now())
        repository.save(updated, expected_revision=expected_revision)
        return self.get(project.id, roadmap.id)

    def delete_item(
        self,
        project_id: str,
        roadmap_id: str,
        *,
        item_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        project, repository, roadmap = self._load(project_id, roadmap_id)
        safe_id = validate_roadmap_id(item_id, label="roadmap item id")
        if not any(item.id == safe_id for item in roadmap.items):
            raise RoadmapNotFoundError(f"roadmap item not found: {safe_id}")
        items = tuple(item for item in roadmap.items if item.id != safe_id)
        relations = tuple(
            relation
            for relation in roadmap.relations
            if relation.source != safe_id and relation.target != safe_id
        )
        updated = replace(
            roadmap,
            items=items,
            relations=relations,
            updated_at=_utc_now(),
        )
        repository.save(updated, expected_revision=expected_revision)
        return self.get(project.id, roadmap.id)

    def link_task(
        self,
        project_id: str,
        roadmap_id: str,
        *,
        expected_revision: int,
        task_id: str,
        item_id: str = "",
        lane: str = "",
        start: str = "",
        target: str = "",
        order: int = 0,
    ) -> dict[str, Any]:
        """Link an existing task or convert one planned item in place."""

        project, repository, roadmap = self._load(project_id, roadmap_id)
        safe_task_id = validate_task_reference_id(task_id)
        if self.projection.linkable_task(project, safe_task_id) is None:
            raise RoadmapNotFoundError(f"active valid task not found: {safe_task_id}")
        existing_task = next(
            (
                item
                for item in roadmap.items
                if item.kind == "task" and item.task_id == safe_task_id
            ),
            None,
        )
        if existing_task and existing_task.id != item_id:
            raise RoadmapConflictError(
                f"task {safe_task_id!r} is already linked in this roadmap"
            )
        items = list(roadmap.items)
        if item_id:
            safe_item_id = validate_roadmap_id(item_id, label="roadmap item id")
            index = next(
                (idx for idx, current in enumerate(items) if current.id == safe_item_id),
                None,
            )
            if index is None:
                raise RoadmapNotFoundError(f"roadmap item not found: {safe_item_id}")
            current = items[index]
            if current.kind not in {"planned_task", "task"}:
                raise RoadmapError(
                    "only planned_task items may be converted to task links"
                )
            if current.kind == "task" and current.task_id != safe_task_id:
                raise RoadmapConflictError(
                    "an existing task roadmap item cannot be relinked to a different task"
                )
            schedule = (
                RoadmapSchedule(start=start, target=target)
                if start or target
                else current.schedule
            )
            linked = RoadmapItem(
                id=current.id,
                kind="task",
                task_id=safe_task_id,
                lane=lane.strip() or current.lane,
                order=order if order else current.order,
                schedule=schedule,
            )
            items[index] = linked
        else:
            linked = RoadmapItem(
                id=f"task-{uuid.uuid4().hex[:10]}",
                kind="task",
                task_id=safe_task_id,
                lane=lane.strip() or "General",
                order=order or (max((item.order for item in items), default=0) + 10),
                schedule=RoadmapSchedule(start=start, target=target),
            )
            items.append(linked)
        updated = replace(roadmap, items=tuple(items), updated_at=_utc_now())
        repository.save(updated, expected_revision=expected_revision)
        return self.get(project.id, roadmap.id)

    def upsert_relation(
        self,
        project_id: str,
        roadmap_id: str,
        *,
        expected_revision: int,
        source: str,
        target: str,
        kind: str = "blocks",
    ) -> dict[str, Any]:
        project, repository, roadmap = self._load(project_id, roadmap_id)
        relation = RoadmapRelation(source=source, target=target, kind=kind)
        known = {item.id for item in roadmap.items}
        if relation.source not in known or relation.target not in known:
            raise RoadmapNotFoundError("roadmap relation endpoints must exist")
        relations = list(roadmap.relations)
        if relation.key not in {current.key for current in relations}:
            relations.append(relation)
        updated = replace(
            roadmap,
            relations=tuple(relations),
            updated_at=_utc_now(),
        )
        repository.save(updated, expected_revision=expected_revision)
        return self.get(project.id, roadmap.id)

    def delete_relation(
        self,
        project_id: str,
        roadmap_id: str,
        *,
        expected_revision: int,
        source: str,
        target: str,
        kind: str = "blocks",
    ) -> dict[str, Any]:
        project, repository, roadmap = self._load(project_id, roadmap_id)
        relation = RoadmapRelation(source=source, target=target, kind=kind)
        relations = tuple(
            current for current in roadmap.relations if current.key != relation.key
        )
        if len(relations) == len(roadmap.relations):
            raise RoadmapNotFoundError("roadmap relation not found")
        updated = replace(roadmap, relations=relations, updated_at=_utc_now())
        repository.save(updated, expected_revision=expected_revision)
        return self.get(project.id, roadmap.id)

    def delete(
        self,
        project_id: str,
        roadmap_id: str,
        *,
        expected_revision: int,
        acknowledged: bool,
    ) -> dict[str, Any]:
        if not acknowledged:
            raise RoadmapError("deleting a roadmap requires explicit acknowledgement")
        project = self._project(project_id)
        repository = self._repository(project)
        self._coordinator().reconcile(project=project, roadmaps=repository)
        removed = repository.delete(roadmap_id, expected_revision=expected_revision)
        return {
            "project_id": project.id,
            "id": removed.id,
            "deleted": True,
            "tasks_deleted": 0,
        }

    def task_references(self, project_id: str, task_id: str) -> list[dict[str, str]]:
        """Return task references used to guard permanent task deletion."""

        project = self._project(project_id)
        safe_task_id = validate_task_reference_id(task_id)
        references: list[dict[str, str]] = []
        repository = self._repository(project)
        self._coordinator().reconcile(project=project, roadmaps=repository)
        for roadmap in repository.list():
            for item in roadmap.items:
                if item.kind == "task" and item.task_id == safe_task_id:
                    references.append(
                        {
                            "roadmap_id": roadmap.id,
                            "roadmap_title": roadmap.title,
                            "item_id": item.id,
                        }
                    )
        return references

    def _prepare_project_asset_item(
        self,
        project,
        data: dict[str, Any],
        *,
        expected_project_execution_revision: int = 0,
    ) -> tuple[
        dict[str, Any],
        ProjectExecutionDefinition,
        ProjectExecutionDefinition | None,
    ]:
        """Return a v2 reference plus an optional unsaved canonical definition.

        The old compatibility bridge wrote Project Execution immediately and
        only then attempted to save the Roadmap.  Returning the desired graph
        instead lets :class:`RoadmapCanonicalCoordinator` durably coordinate
        the legitimate two-document mutation.
        """

        kind = str(data.get("kind", "")).strip().lower()
        asset_id = str(data.get("project_asset_id", "")).strip()
        repository = ProjectExecutionRepository(project.directory)
        if not repository.exists():
            # Compatibility for non-GUI callers.  R3's GUI asks explicitly
            # before initialization, but an empty canonical definition is safe
            # to create before the cross-domain asset/link transaction.
            repository.create(ProjectExecutionDefinition(project=project.id))
        definition = repository.load()
        if (
            expected_project_execution_revision
            and definition.revision != int(expected_project_execution_revision)
        ):
            raise ProjectExecutionConflictError(
                "Project Execution changed since the Roadmap projection was loaded; "
                "refresh before editing canonical project assets "
                f"(expected revision {expected_project_execution_revision}, "
                f"current {definition.revision})"
            )

        desired: ProjectExecutionDefinition | None = None
        if asset_id:
            index = {
                "phase": definition.phase_index,
                "gate": definition.gate_index,
                "milestone": definition.milestone_index,
            }[kind]
            if asset_id not in index:
                raise RoadmapNotFoundError(
                    f"canonical Project {kind.title()} not found: {asset_id}"
                )
        else:
            asset_id = validate_roadmap_id(
                str(data.get("id", "")), label=f"{kind} project asset id"
            )
            title = str(data.get("title", "")).strip()
            description = str(data.get("description", "")).strip()
            schedule = RoadmapSchedule.from_mapping(data.get("schedule"))
            if kind == "phase":
                asset = ProjectPhase(
                    asset_id,
                    title,
                    description=description,
                    schedule=ProjectSchedule(schedule.start, schedule.target),
                )
            elif kind == "gate":
                asset = ProjectGate(
                    asset_id,
                    title,
                    description=description,
                    schedule=ProjectSchedule(schedule.start, schedule.target),
                    criteria=(GateCriterion("human_approval"),),
                )
            else:
                asset = ProjectMilestone(
                    asset_id,
                    title,
                    description=description,
                    start=schedule.start,
                    target=schedule.target,
                )
            desired = self._definition_with_asset(definition, kind, asset)

        return (
            {
                "id": str(data.get("id", "")),
                "kind": kind,
                "project_asset_id": asset_id,
                "lane": str(data.get("lane", "General")),
                "order": data.get("order", 0),
            },
            definition,
            desired,
        )

    @staticmethod
    def _definition_with_asset(
        definition: ProjectExecutionDefinition,
        kind: str,
        asset: ProjectPhase | ProjectGate | ProjectMilestone,
    ) -> ProjectExecutionDefinition:
        attribute = {"phase": "phases", "gate": "gates", "milestone": "milestones"}[kind]
        current = list(getattr(definition, attribute))
        index = next((i for i, value in enumerate(current) if value.id == asset.id), None)
        if index is None:
            current.append(asset)
        else:
            current[index] = asset
        desired = replace(definition, **{attribute: tuple(current)})
        validate_definition(desired)
        return desired

    @classmethod
    def _definition_with_project_asset_schedule(
        cls,
        definition: ProjectExecutionDefinition,
        kind: str,
        asset_id: str,
        *,
        start: str,
        target: str,
    ) -> ProjectExecutionDefinition:
        schedule = ProjectSchedule(start, target)
        if kind == "phase":
            asset = definition.phase_index[asset_id]
            replacement = replace(asset, schedule=schedule)
        elif kind == "gate":
            asset = definition.gate_index[asset_id]
            replacement = replace(asset, schedule=schedule)
        else:
            asset = definition.milestone_index[asset_id]
            replacement = replace(asset, start=schedule.start, target=schedule.target)
        return cls._definition_with_asset(definition, kind, replacement)

    def _coordinator(self) -> RoadmapCanonicalCoordinator:
        return RoadmapCanonicalCoordinator(state_root=self.state_root)


    def _coordination_status(self, project, repository: RoadmapRepository) -> dict[str, Any]:
        return self._coordinator().reconcile_if_safe(
            project=project, roadmaps=repository
        ).as_mapping()

    def _assert_coordination_clear(self, project, repository: RoadmapRepository) -> None:
        status = self._coordination_status(project, repository)
        if status.get("pending"):
            raise RoadmapCoordinationConflictError(
                "canonical Roadmap coordination requires operator resolution before mutation: "
                f"{status.get('message', 'pending coordination conflict')}"
            )

    @staticmethod
    def _assert_roadmap_revision(roadmap: Roadmap, expected_revision: int) -> None:
        """Fail before cross-domain effects when the Roadmap is already stale."""

        if roadmap.revision != int(expected_revision):
            raise RoadmapConflictError(
                "roadmap changed since it was loaded; refresh before saving "
                f"(expected revision {expected_revision}, current {roadmap.revision})"
            )

    def _load(self, project_id: str, roadmap_id: str):
        project = self._project(project_id)
        repository = self._repository(project)
        self._assert_coordination_clear(project, repository)
        return project, repository, repository.load(roadmap_id)

    def _project(self, project_id: str):
        return load_registered_project(
            self.control_root, validate_project_id(str(project_id).strip())
        )

    def _repository(self, project) -> RoadmapRepository:
        return RoadmapRepository(project, state_root=self.state_root)

    @staticmethod
    def _summary(roadmap: Roadmap) -> dict[str, Any]:
        return {
            "id": roadmap.id,
            "title": roadmap.title,
            "description": roadmap.description,
            "revision": roadmap.revision,
            "updated_at": roadmap.updated_at,
            "item_count": len(roadmap.items),
            "task_count": sum(1 for item in roadmap.items if item.kind == "task"),
        }

    @staticmethod
    def _available_id(repository: RoadmapRepository, base: str) -> str:
        existing = {roadmap.id for roadmap in repository.list()}
        if base not in existing:
            return base
        for suffix in range(2, 10_000):
            candidate = f"{base}-{suffix}"
            if candidate not in existing:
                return candidate
        raise RoadmapConflictError("could not allocate a unique roadmap id")
