"""GUI facade methods for project Roadmaps.

Separated from the control-center coordinator so Roadmap HTTP adaptation does
not make the browser-context service a monolith. The mixin assumes a configured
``self.roadmaps`` :class:`execraft.roadmap.RoadmapService`.
"""

from __future__ import annotations

from typing import Any, Mapping

from execraft.gui.errors import GuiError
from execraft.project import ProjectError
from execraft.project_execution.errors import ProjectExecutionError
from execraft.roadmap import RoadmapError


class RoadmapGuiFacade:
    """Translate Roadmap/domain failures into stable GUI errors."""

    def roadmap_list(self, project_id: str) -> dict[str, Any]:
        try:
            return self.roadmaps.list(project_id)
        except (RoadmapError, ProjectExecutionError, ProjectError, OSError, ValueError) as exc:
            raise GuiError(str(exc)) from exc
    def roadmap_get(self, project_id: str, roadmap_id: str) -> dict[str, Any]:
        try:
            return self.roadmaps.get(project_id, roadmap_id)
        except (RoadmapError, ProjectExecutionError, ProjectError, OSError, ValueError) as exc:
            raise GuiError(str(exc)) from exc
    def roadmap_migration_preview(self, project_id: str, roadmap_id: str) -> dict[str, Any]:
        try:
            return self.roadmaps.migration_preview(project_id, roadmap_id)
        except (RoadmapError, ProjectExecutionError, ProjectError, OSError, ValueError) as exc:
            raise GuiError(str(exc)) from exc

    def roadmap_migrate_v1(
        self,
        project_id: str,
        roadmap_id: str,
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        try:
            return self.roadmaps.migrate_v1(
                project_id, roadmap_id, expected_revision=expected_revision
            )
        except (RoadmapError, ProjectExecutionError, ProjectError, OSError, ValueError) as exc:
            raise GuiError(str(exc)) from exc

    def roadmap_create(
        self, project_id: str, *, title: str, description: str = "", roadmap_id: str = ""
    ) -> dict[str, Any]:
        try:
            return self.roadmaps.create(
                project_id, title=title, description=description, roadmap_id=roadmap_id
            )
        except (RoadmapError, ProjectExecutionError, ProjectError, OSError, ValueError) as exc:
            raise GuiError(str(exc)) from exc

    def roadmap_update_metadata(
        self,
        project_id: str,
        roadmap_id: str,
        *,
        expected_revision: int,
        title: str,
        description: str = "",
    ) -> dict[str, Any]:
        try:
            return self.roadmaps.update_metadata(
                project_id,
                roadmap_id,
                expected_revision=expected_revision,
                title=title,
                description=description,
            )
        except (RoadmapError, ProjectExecutionError, ProjectError, OSError, ValueError) as exc:
            raise GuiError(str(exc)) from exc

    def roadmap_upsert_item(
        self,
        project_id: str,
        roadmap_id: str,
        *,
        expected_revision: int,
        expected_project_execution_revision: int = 0,
        item: Mapping[str, Any],
    ) -> dict[str, Any]:
        try:
            return self.roadmaps.upsert_item(
                project_id,
                roadmap_id,
                expected_revision=expected_revision,
                expected_project_execution_revision=expected_project_execution_revision,
                raw_item=item,
            )
        except (RoadmapError, ProjectExecutionError, ProjectError, OSError, ValueError) as exc:
            raise GuiError(str(exc)) from exc

    def roadmap_move_item(
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
        try:
            return self.roadmaps.move_item(
                project_id,
                roadmap_id,
                expected_revision=expected_revision,
                expected_project_execution_revision=expected_project_execution_revision,
                item_id=item_id,
                lane=lane,
                start=start,
                target=target,
                target_item_id=target_item_id,
                placement=placement,
            )
        except (RoadmapError, ProjectExecutionError, ProjectError, OSError, ValueError) as exc:
            raise GuiError(str(exc)) from exc

    def roadmap_move_lane(
        self,
        project_id: str,
        roadmap_id: str,
        *,
        expected_revision: int,
        lane: str,
        target_lane: str,
        placement: str = "before",
    ) -> dict[str, Any]:
        try:
            return self.roadmaps.move_lane(
                project_id,
                roadmap_id,
                expected_revision=expected_revision,
                lane=lane,
                target_lane=target_lane,
                placement=placement,
            )
        except (RoadmapError, ProjectExecutionError, ProjectError, OSError, ValueError) as exc:
            raise GuiError(str(exc)) from exc

    def roadmap_delete_item(
        self,
        project_id: str,
        roadmap_id: str,
        *,
        item_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        try:
            return self.roadmaps.delete_item(
                project_id,
                roadmap_id,
                item_id=item_id,
                expected_revision=expected_revision,
            )
        except (RoadmapError, ProjectExecutionError, ProjectError, OSError, ValueError) as exc:
            raise GuiError(str(exc)) from exc

    def roadmap_link_task(
        self,
        project_id: str,
        roadmap_id: str,
        *,
        expected_revision: int,
        task_id: str,
        item_id: str = "",
        lane: str = "General",
        start: str = "",
        target: str = "",
        order: int = 0,
    ) -> dict[str, Any]:
        try:
            return self.roadmaps.link_task(
                project_id,
                roadmap_id,
                expected_revision=expected_revision,
                task_id=task_id,
                item_id=item_id,
                lane=lane,
                start=start,
                target=target,
                order=order,
            )
        except (RoadmapError, ProjectExecutionError, ProjectError, OSError, ValueError) as exc:
            raise GuiError(str(exc)) from exc

    def roadmap_upsert_relation(
        self,
        project_id: str,
        roadmap_id: str,
        *,
        expected_revision: int,
        source: str,
        target: str,
        kind: str = "blocks",
    ) -> dict[str, Any]:
        try:
            return self.roadmaps.upsert_relation(
                project_id,
                roadmap_id,
                expected_revision=expected_revision,
                source=source,
                target=target,
                kind=kind,
            )
        except (RoadmapError, ProjectExecutionError, ProjectError, OSError, ValueError) as exc:
            raise GuiError(str(exc)) from exc

    def roadmap_delete_relation(
        self,
        project_id: str,
        roadmap_id: str,
        *,
        expected_revision: int,
        source: str,
        target: str,
        kind: str = "blocks",
    ) -> dict[str, Any]:
        try:
            return self.roadmaps.delete_relation(
                project_id,
                roadmap_id,
                expected_revision=expected_revision,
                source=source,
                target=target,
                kind=kind,
            )
        except (RoadmapError, ProjectExecutionError, ProjectError, OSError, ValueError) as exc:
            raise GuiError(str(exc)) from exc

    def roadmap_delete(
        self,
        project_id: str,
        roadmap_id: str,
        *,
        expected_revision: int,
        acknowledged: bool,
    ) -> dict[str, Any]:
        try:
            return self.roadmaps.delete(
                project_id,
                roadmap_id,
                expected_revision=expected_revision,
                acknowledged=acknowledged,
            )
        except (RoadmapError, ProjectExecutionError, ProjectError, OSError, ValueError) as exc:
            raise GuiError(str(exc)) from exc
