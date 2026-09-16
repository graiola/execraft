"""Deterministic, restart-safe Roadmap v1 -> v2 migration.

Roadmap v1 stored Phase/Gate/Milestone business data inline.  Roadmap v2 stores
only references to canonical Project Execution assets.  Migration is explicit:
callers can preview the exact identity mapping, and application records a
prepared marker before it mutates either durable domain.

A prepared marker is intentionally resumable.  If the process dies after one
or more canonical assets are created but before the Roadmap is rewritten, a
subsequent apply validates the original Roadmap revision/digest, reuses the
same planned canonical IDs, verifies any already-created assets, and completes
without duplicating them.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from execraft.persistence.atomic import atomic_write_json
from execraft.project import ProjectDescriptor
from execraft.project_execution.models import (
    GateCriterion,
    ProjectExecutionDefinition,
    ProjectExecutionError,
    ProjectGate,
    ProjectMilestone,
    ProjectPhase,
    ProjectSchedule,
)
from execraft.project_execution.repository import ProjectExecutionRepository
from execraft.project_execution.service import ProjectExecutionService

from .models import (
    Roadmap,
    RoadmapConflictError,
    RoadmapError,
    RoadmapItem,
    validate_roadmap_id,
)
from .repository import RoadmapRepository

_PROJECT_ASSET_KINDS = frozenset({"phase", "gate", "milestone"})
_MIGRATION_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class RoadmapMigrationItem:
    """One v1 Roadmap item and its deterministic canonical identity."""

    item_id: str
    kind: str
    project_asset_id: str
    action: str = "create"
    note: str = ""

    def as_mapping(self) -> dict[str, str]:
        return {
            key: value
            for key, value in (
                ("item_id", self.item_id),
                ("kind", self.kind),
                ("project_asset_id", self.project_asset_id),
                ("action", self.action),
                ("note", self.note),
            )
            if value
        }

    @classmethod
    def from_mapping(cls, raw: object) -> "RoadmapMigrationItem":
        if not isinstance(raw, Mapping):
            raise RoadmapError("Roadmap migration item must be a mapping")
        return cls(
            item_id=validate_roadmap_id(raw.get("item_id", ""), label="migration item id"),
            kind=str(raw.get("kind", "")).strip().lower(),
            project_asset_id=validate_roadmap_id(
                raw.get("project_asset_id", ""), label="migration project asset id"
            ),
            action=str(raw.get("action", "create")).strip().lower(),
            note=str(raw.get("note", "")).strip(),
        )


@dataclass(frozen=True)
class RoadmapMigrationPreview:
    """Dry-run contract for one Roadmap migration."""

    roadmap_id: str
    source_revision: int
    source_digest: str
    project_execution_revision: int
    items: tuple[RoadmapMigrationItem, ...]
    conflicts: tuple[str, ...] = ()
    resumed: bool = False

    @property
    def can_apply(self) -> bool:
        return not self.conflicts

    def as_mapping(self) -> dict[str, Any]:
        return {
            "roadmap_id": self.roadmap_id,
            "source_revision": self.source_revision,
            "source_digest": self.source_digest,
            "project_execution_revision": self.project_execution_revision,
            "items": [item.as_mapping() for item in self.items],
            "conflicts": list(self.conflicts),
            "can_apply": self.can_apply,
            "resumed": self.resumed,
        }


@dataclass(frozen=True)
class _PreparedMigration:
    roadmap_id: str
    source_revision: int
    source_digest: str
    items: tuple[RoadmapMigrationItem, ...]


class RoadmapV1Migrator:
    """Promote v1 project assets into ``PROJECT_EXECUTION.yaml`` safely."""

    def __init__(
        self,
        *,
        project: ProjectDescriptor,
        roadmaps: RoadmapRepository,
    ) -> None:
        self.project = project
        self.roadmaps = roadmaps
        self.executions = ProjectExecutionRepository(project.directory)
        self.marker_dir = project.directory / ".migrations"

    def preview(self, roadmap_id: str) -> RoadmapMigrationPreview:
        roadmap = self.roadmaps.load(roadmap_id)
        definition = self.executions.load_optional()
        execution_revision = definition.revision if definition is not None else 0

        if roadmap.schema_version == 2:
            return RoadmapMigrationPreview(
                roadmap_id=roadmap.id,
                source_revision=roadmap.revision,
                source_digest=self.roadmaps.sha256(roadmap.id),
                project_execution_revision=execution_revision,
                items=(),
            )

        prepared = self._load_prepared(roadmap.id)
        if prepared is not None:
            conflicts = self._prepared_conflicts(roadmap, prepared)
            return RoadmapMigrationPreview(
                roadmap_id=roadmap.id,
                source_revision=prepared.source_revision,
                source_digest=prepared.source_digest,
                project_execution_revision=execution_revision,
                items=prepared.items,
                conflicts=tuple(conflicts),
                resumed=True,
            )

        allocation = self._global_identity_allocation(definition)
        planned: list[RoadmapMigrationItem] = []
        conflicts: list[str] = []
        for item in roadmap.items:
            if item.kind not in _PROJECT_ASSET_KINDS:
                continue
            asset_id = allocation.get((roadmap.id, item.id))
            if not asset_id:
                conflicts.append(
                    f"cannot allocate canonical ID for {item.kind} {item.id}"
                )
                continue
            note = ""
            if item.kind == "gate":
                note = (
                    "legacy planning Gate promoted to a human-approval Gate so "
                    "migration cannot silently authorize execution"
                )
            planned.append(
                RoadmapMigrationItem(
                    item_id=item.id,
                    kind=item.kind,
                    project_asset_id=asset_id,
                    note=note,
                )
            )

        return RoadmapMigrationPreview(
            roadmap_id=roadmap.id,
            source_revision=roadmap.revision,
            source_digest=self.roadmaps.sha256(roadmap.id),
            project_execution_revision=execution_revision,
            items=tuple(planned),
            conflicts=tuple(conflicts),
        )

    def apply(self, roadmap_id: str, *, expected_revision: int) -> Roadmap:
        """Apply or resume one migration.

        The Roadmap revision remains the operator-facing optimistic-concurrency
        token.  A prepared marker also binds the operation to the exact source
        file digest so out-of-band edits cannot be silently migrated.
        """

        roadmap = self.roadmaps.load(roadmap_id)
        if roadmap.schema_version == 2:
            return roadmap
        if roadmap.revision != expected_revision:
            raise RoadmapConflictError(
                "roadmap revision conflict: "
                f"expected {expected_revision}, current {roadmap.revision}; "
                "refresh before migrating"
            )

        preview = self.preview(roadmap_id)
        if preview.conflicts:
            raise RoadmapError(
                "Roadmap migration has conflicts: " + "; ".join(preview.conflicts)
            )
        if preview.source_revision != expected_revision:
            raise RoadmapConflictError(
                "prepared Roadmap migration targets a different source revision"
            )

        if not preview.resumed:
            self._write_prepared(preview)

        definition = self.executions.load_optional()
        if definition is None:
            definition = self.executions.create(
                ProjectExecutionDefinition(project=self.project.id)
            )
        service = ProjectExecutionService(self.executions)
        mapping = {item.item_id: item for item in preview.items}

        for item in roadmap.items:
            plan = mapping.get(item.id)
            if plan is None:
                continue
            self._ensure_canonical_asset(service, item, plan)

        migrated = replace(
            roadmap,
            schema_version=2,
            items=tuple(self._migrated_item(item, mapping) for item in roadmap.items),
        )
        saved = self.roadmaps.save(migrated, expected_revision=expected_revision)
        self._write_complete(preview, saved)
        return saved

    def _global_identity_allocation(
        self,
        definition: ProjectExecutionDefinition | None,
    ) -> dict[tuple[str, str], str]:
        """Allocate canonical IDs independent of migration execution order."""

        existing_ids = {
            asset.id
            for collection in (
                definition.phases if definition else (),
                definition.gates if definition else (),
                definition.milestones if definition else (),
            )
            for asset in collection
        }
        roadmaps = self.roadmaps.list()
        all_project_rows = [
            (roadmap.id, item.id)
            for roadmap in roadmaps
            for item in roadmap.items
            if item.kind in _PROJECT_ASSET_KINDS
        ]
        legacy_rows = [
            (roadmap.id, item.id)
            for roadmap in roadmaps
            if roadmap.schema_version == 1
            for item in roadmap.items
            if item.kind in _PROJECT_ASSET_KINDS
        ]
        id_counts: dict[str, int] = {}
        for _, item_id in all_project_rows:
            id_counts[item_id] = id_counts.get(item_id, 0) + 1

        used = set(existing_ids)
        result: dict[tuple[str, str], str] = {}
        for roadmap_id, item_id in sorted(legacy_rows):
            if id_counts[item_id] == 1 and item_id not in existing_ids:
                candidate = item_id
            else:
                candidate = self._namespaced_asset_id(roadmap_id, item_id)
            candidate = self._deduplicate_candidate(
                candidate,
                roadmap_id=roadmap_id,
                item_id=item_id,
                used=used,
            )
            used.add(candidate)
            result[(roadmap_id, item_id)] = candidate
        return result

    @staticmethod
    def _namespaced_asset_id(roadmap_id: str, item_id: str) -> str:
        raw = f"{roadmap_id}-{item_id}"
        if len(raw) <= 96:
            return validate_roadmap_id(raw, label="migrated project asset id")
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
        prefix = raw[: 96 - len(digest) - 1].rstrip("-_")
        return validate_roadmap_id(
            f"{prefix}-{digest}", label="migrated project asset id"
        )

    @staticmethod
    def _deduplicate_candidate(
        candidate: str,
        *,
        roadmap_id: str,
        item_id: str,
        used: set[str],
    ) -> str:
        if candidate not in used:
            return candidate
        seed = f"{roadmap_id}\0{item_id}"
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        for width in range(8, 33, 4):
            suffix = digest[:width]
            prefix = candidate[: 96 - width - 1].rstrip("-_")
            resolved = validate_roadmap_id(
                f"{prefix}-{suffix}", label="migrated project asset id"
            )
            if resolved not in used:
                return resolved
        raise RoadmapError(
            f"cannot deterministically allocate canonical ID for {roadmap_id}/{item_id}"
        )

    def _ensure_canonical_asset(
        self,
        service: ProjectExecutionService,
        item: RoadmapItem,
        plan: RoadmapMigrationItem,
    ) -> None:
        desired = self._asset_from_v1_item(item, plan.project_asset_id)
        current = service.get()
        existing = self._asset_by_id(current, plan.project_asset_id)
        if existing is not None:
            if existing != desired:
                raise RoadmapError(
                    "prepared Roadmap migration conflicts with existing canonical "
                    f"{item.kind} {plan.project_asset_id}"
                )
            return

        if item.kind == "phase":
            service.upsert_phase(desired, expected_revision=current.revision)
        elif item.kind == "gate":
            service.upsert_gate(desired, expected_revision=current.revision)
        elif item.kind == "milestone":
            service.upsert_milestone(desired, expected_revision=current.revision)
        else:  # pragma: no cover - guarded by preview/mapping
            raise RoadmapError(f"unsupported migration item kind: {item.kind}")

    @staticmethod
    def _asset_from_v1_item(
        item: RoadmapItem,
        asset_id: str,
    ) -> ProjectPhase | ProjectGate | ProjectMilestone:
        schedule = ProjectSchedule(item.schedule.start, item.schedule.target)
        if item.kind == "phase":
            return ProjectPhase(
                id=asset_id,
                title=item.title,
                description=item.description,
                schedule=schedule,
            )
        if item.kind == "gate":
            return ProjectGate(
                id=asset_id,
                title=item.title,
                description=item.description,
                schedule=schedule,
                criteria=(GateCriterion("human_approval"),),
            )
        if item.kind == "milestone":
            return ProjectMilestone(
                id=asset_id,
                title=item.title,
                description=item.description,
                start=schedule.start,
                target=schedule.target,
            )
        raise RoadmapError(f"unsupported migration item kind: {item.kind}")

    @staticmethod
    def _asset_by_id(
        definition: ProjectExecutionDefinition,
        asset_id: str,
    ) -> ProjectPhase | ProjectGate | ProjectMilestone | None:
        return (
            definition.phase_index.get(asset_id)
            or definition.gate_index.get(asset_id)
            or definition.milestone_index.get(asset_id)
        )

    @staticmethod
    def _migrated_item(
        item: RoadmapItem,
        mapping: Mapping[str, RoadmapMigrationItem],
    ) -> RoadmapItem:
        plan = mapping.get(item.id)
        if plan is None:
            return item
        return RoadmapItem(
            id=item.id,
            kind=item.kind,
            project_asset_id=plan.project_asset_id,
            lane=item.lane,
            order=item.order,
        )

    def _marker_path(self, roadmap_id: str) -> Path:
        return self.marker_dir / f"roadmap-v1-{validate_roadmap_id(roadmap_id)}.json"

    def _load_prepared(self, roadmap_id: str) -> _PreparedMigration | None:
        marker = self._marker_path(roadmap_id)
        if not marker.is_file() or marker.is_symlink():
            return None
        try:
            import json

            raw = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RoadmapError(f"cannot read Roadmap migration marker: {exc}") from exc
        if not isinstance(raw, Mapping):
            raise RoadmapError("Roadmap migration marker must contain a mapping")
        if int(raw.get("schema_version", 0)) != _MIGRATION_SCHEMA_VERSION:
            raise RoadmapError("unsupported Roadmap migration marker schema")
        if raw.get("status") != "prepared":
            return None
        items_raw = raw.get("items") or []
        if not isinstance(items_raw, list):
            raise RoadmapError("Roadmap migration marker items must be a list")
        return _PreparedMigration(
            roadmap_id=validate_roadmap_id(raw.get("roadmap_id", "")),
            source_revision=int(raw.get("source_revision", 0)),
            source_digest=str(raw.get("source_digest", "")),
            items=tuple(RoadmapMigrationItem.from_mapping(item) for item in items_raw),
        )

    def _prepared_conflicts(
        self,
        roadmap: Roadmap,
        prepared: _PreparedMigration,
    ) -> list[str]:
        conflicts: list[str] = []
        if prepared.roadmap_id != roadmap.id:
            conflicts.append("migration marker belongs to a different Roadmap")
        if prepared.source_revision != roadmap.revision:
            conflicts.append(
                "Roadmap revision changed after migration was prepared "
                f"({prepared.source_revision} -> {roadmap.revision})"
            )
        current_digest = self.roadmaps.sha256(roadmap.id)
        if prepared.source_digest != current_digest:
            conflicts.append("Roadmap content changed after migration was prepared")
        source_items = {
            (item.id, item.kind)
            for item in roadmap.items
            if item.kind in _PROJECT_ASSET_KINDS
        }
        marker_items = {(item.item_id, item.kind) for item in prepared.items}
        if source_items != marker_items:
            conflicts.append("Roadmap project assets no longer match the migration marker")
        return conflicts

    def _write_prepared(self, preview: RoadmapMigrationPreview) -> None:
        self.marker_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            self._marker_path(preview.roadmap_id),
            {
                "schema_version": _MIGRATION_SCHEMA_VERSION,
                "status": "prepared",
                "roadmap_id": preview.roadmap_id,
                "source_revision": preview.source_revision,
                "source_digest": preview.source_digest,
                "items": [item.as_mapping() for item in preview.items],
            },
            mode=0o600,
        )

    def _write_complete(
        self,
        preview: RoadmapMigrationPreview,
        saved: Roadmap,
    ) -> None:
        atomic_write_json(
            self._marker_path(preview.roadmap_id),
            {
                "schema_version": _MIGRATION_SCHEMA_VERSION,
                "status": "complete",
                "roadmap_id": preview.roadmap_id,
                "source_revision": preview.source_revision,
                "source_digest": preview.source_digest,
                "target_revision": saved.revision,
                "items": [item.as_mapping() for item in preview.items],
            },
            mode=0o600,
        )


__all__ = [
    "RoadmapMigrationItem",
    "RoadmapMigrationPreview",
    "RoadmapV1Migrator",
]
