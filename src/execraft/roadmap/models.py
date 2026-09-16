"""Validated roadmap persistence contracts.

The schema is deliberately small.  Linked task metadata is never duplicated in
roadmap YAML; it is projected at read time from the task dossier/runtime state.
This keeps task execution authoritative while still allowing roadmap-specific
schedule, lane, ordering, and relationship metadata.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any, Mapping, Sequence

from execraft.workspace.task_git import TaskGitError, validate_task_id


ROADMAP_SCHEMA_VERSION = 2
ROADMAP_SUPPORTED_SCHEMA_VERSIONS = frozenset({1, 2})
ROADMAP_ITEM_KINDS = frozenset({"task", "planned_task", "milestone", "phase", "gate"})
ROADMAP_RELATION_KINDS = frozenset({"blocks", "related"})


class RoadmapError(RuntimeError):
    """Base error for invalid or unsafe roadmap operations."""


class RoadmapNotFoundError(RoadmapError):
    """Raised when a requested roadmap does not exist."""


class RoadmapConflictError(RoadmapError):
    """Raised when optimistic concurrency detects a stale browser edit."""


def validate_roadmap_id(value: object, *, label: str = "roadmap id") -> str:
    """Validate identifiers before they are used as file or relation keys."""

    text = str(value or "").strip()
    if not text or len(text) > 96 or not text.replace("-", "_").isidentifier():
        raise RoadmapError(f"invalid {label}: {text!r}")
    return text


def validate_task_reference_id(value: object) -> str:
    """Validate task references with the canonical task-ID contract."""

    try:
        return validate_task_id(str(value or "").strip())
    except TaskGitError as exc:
        raise RoadmapError(str(exc)) from exc


def _text(value: object, *, label: str, required: bool = False, limit: int = 20_000) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise RoadmapError(f"{label} cannot be empty")
    if len(text) > limit:
        raise RoadmapError(f"{label} exceeds {limit} characters")
    return text


def _iso_date(value: object, *, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise RoadmapError(f"{label} must be an ISO date (YYYY-MM-DD)") from exc
    return parsed.isoformat()


@dataclass(frozen=True)
class RoadmapSchedule:
    """Optional exact planning window for one roadmap item."""

    start: str = ""
    target: str = ""

    def __post_init__(self) -> None:
        start = _iso_date(self.start, label="schedule start")
        target = _iso_date(self.target, label="schedule target")
        if start and target and start > target:
            raise RoadmapError("schedule start cannot be after target")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "target", target)

    @property
    def scheduled(self) -> bool:
        return bool(self.start or self.target)

    @property
    def duration_days(self) -> int:
        """Return the inclusive planning duration without persisting a second source of truth."""

        if not self.scheduled:
            return 0
        start = date.fromisoformat(self.start or self.target)
        target = date.fromisoformat(self.target or self.start)
        return (target - start).days + 1

    def as_mapping(self) -> dict[str, str]:
        payload: dict[str, str] = {}
        if self.start:
            payload["start"] = self.start
        if self.target:
            payload["target"] = self.target
        return payload

    @classmethod
    def from_mapping(cls, raw: object) -> "RoadmapSchedule":
        if raw in (None, ""):
            return cls()
        if not isinstance(raw, Mapping):
            raise RoadmapError("roadmap item schedule must be a mapping")
        return cls(start=str(raw.get("start", "")), target=str(raw.get("target", "")))


@dataclass(frozen=True)
class RoadmapItem:
    """One stable roadmap node.

    ``task`` nodes contain only the canonical task ID plus roadmap-local
    metadata.  Their display title, lifecycle status, repositories, and runtime
    progress are intentionally resolved dynamically.
    """

    id: str
    kind: str
    title: str = ""
    description: str = ""
    task_id: str = ""
    project_asset_id: str = ""
    lane: str = "General"
    order: int = 0
    schedule: RoadmapSchedule = field(default_factory=RoadmapSchedule)

    def __post_init__(self) -> None:
        item_id = validate_roadmap_id(self.id, label="roadmap item id")
        kind = str(self.kind or "").strip().lower()
        if kind not in ROADMAP_ITEM_KINDS:
            raise RoadmapError(f"unsupported roadmap item kind: {kind!r}")
        title = _text(self.title, label="roadmap item title", limit=500)
        description = _text(
            self.description, label="roadmap item description", limit=20_000
        )
        task_id = str(self.task_id or "").strip()
        project_asset_id = str(self.project_asset_id or "").strip()
        lane = _text(self.lane, label="roadmap lane", limit=160) or "General"
        if not isinstance(self.order, int) or isinstance(self.order, bool):
            raise RoadmapError("roadmap item order must be an integer")
        order = self.order
        if abs(order) > 1_000_000_000:
            raise RoadmapError("roadmap item order is outside the supported range")
        if kind == "task":
            task_id = validate_task_reference_id(task_id)
            if project_asset_id:
                raise RoadmapError("task roadmap items cannot carry project_asset_id")
            # Linked task identity is canonical. Do not persist duplicate title
            # or description that could silently diverge from TASK.yaml.
            title = ""
            description = ""
        elif kind == "planned_task":
            if task_id or project_asset_id:
                raise RoadmapError("planned_task roadmap items cannot carry canonical references")
            title = _text(title, label="roadmap item title", required=True, limit=500)
        else:
            if task_id:
                raise RoadmapError(f"{kind} roadmap items cannot carry task_id")
            if project_asset_id:
                project_asset_id = validate_roadmap_id(
                    project_asset_id, label=f"{kind} project asset id"
                )
                title = ""
                description = ""
            else:
                # Legacy Roadmap v1 representation. Roadmap.__post_init__ is
                # responsible for ensuring this shape is never written as v2.
                title = _text(title, label="roadmap item title", required=True, limit=500)
        object.__setattr__(self, "id", item_id)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "task_id", task_id)
        object.__setattr__(self, "project_asset_id", project_asset_id)
        object.__setattr__(self, "lane", lane)
        object.__setattr__(self, "order", order)
        if not isinstance(self.schedule, RoadmapSchedule):
            raise RoadmapError("roadmap item schedule is invalid")

    def as_mapping(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "lane": self.lane,
            "order": self.order,
        }
        if self.kind == "task":
            payload["task_id"] = self.task_id
        elif self.kind == "planned_task":
            payload["title"] = self.title
            if self.description:
                payload["description"] = self.description
        elif self.project_asset_id:
            payload["project_asset_id"] = self.project_asset_id
        else:
            # Legacy v1 business data; Roadmap v2 validation forbids this shape.
            payload["title"] = self.title
            if self.description:
                payload["description"] = self.description
        schedule = self.schedule.as_mapping()
        if schedule:
            payload["schedule"] = schedule
        return payload

    @classmethod
    def from_mapping(cls, raw: object) -> "RoadmapItem":
        if not isinstance(raw, Mapping):
            raise RoadmapError("each roadmap item must be a mapping")
        return cls(
            id=str(raw.get("id", "")),
            kind=str(raw.get("kind", "")),
            title=str(raw.get("title", "")),
            description=str(raw.get("description", "")),
            task_id=str(raw.get("task_id", "")),
            project_asset_id=str(raw.get("project_asset_id", "")),
            lane=str(raw.get("lane", "General")),
            order=raw.get("order", 0),  # type: ignore[arg-type]
            schedule=RoadmapSchedule.from_mapping(raw.get("schedule")),
        )


@dataclass(frozen=True)
class RoadmapRelation:
    """Planning-only relation between two roadmap items."""

    source: str
    target: str
    kind: str = "blocks"

    def __post_init__(self) -> None:
        source = validate_roadmap_id(self.source, label="relation source")
        target = validate_roadmap_id(self.target, label="relation target")
        kind = str(self.kind or "").strip().lower()
        if source == target:
            raise RoadmapError("roadmap relation cannot reference itself")
        if kind not in ROADMAP_RELATION_KINDS:
            raise RoadmapError(f"unsupported roadmap relation kind: {kind!r}")
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "kind", kind)

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.source, self.target, self.kind)

    def as_mapping(self) -> dict[str, str]:
        return {"from": self.source, "to": self.target, "kind": self.kind}

    @classmethod
    def from_mapping(cls, raw: object) -> "RoadmapRelation":
        if not isinstance(raw, Mapping):
            raise RoadmapError("each roadmap relation must be a mapping")
        return cls(
            source=str(raw.get("from", "")),
            target=str(raw.get("to", "")),
            kind=str(raw.get("kind", "blocks")),
        )


@dataclass(frozen=True)
class Roadmap:
    """Versioned roadmap document persisted below one project descriptor."""

    id: str
    title: str
    description: str = ""
    revision: int = 1
    created_at: str = ""
    updated_at: str = ""
    items: tuple[RoadmapItem, ...] = ()
    relations: tuple[RoadmapRelation, ...] = ()
    schema_version: int = ROADMAP_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if int(self.schema_version) not in ROADMAP_SUPPORTED_SCHEMA_VERSIONS:
            raise RoadmapError(
                f"unsupported roadmap schema_version: {self.schema_version!r}"
            )
        roadmap_id = validate_roadmap_id(self.id)
        title = _text(self.title, label="roadmap title", required=True, limit=500)
        description = _text(
            self.description, label="roadmap description", limit=20_000
        )
        try:
            revision = int(self.revision)
        except (TypeError, ValueError) as exc:
            raise RoadmapError("roadmap revision must be an integer") from exc
        if revision < 1:
            raise RoadmapError("roadmap revision must be at least 1")
        item_ids = [item.id for item in self.items]
        if len(set(item_ids)) != len(item_ids):
            raise RoadmapError("roadmap item IDs must be unique")
        linked_tasks = [item.task_id for item in self.items if item.kind == "task"]
        if len(set(linked_tasks)) != len(linked_tasks):
            raise RoadmapError("a task may be linked only once within a roadmap")
        project_kinds = {"phase", "gate", "milestone"}
        for item in self.items:
            if item.kind not in project_kinds:
                continue
            if int(self.schema_version) == 1:
                if item.project_asset_id:
                    raise RoadmapError("Roadmap v1 project items cannot carry project_asset_id")
                if not item.title:
                    raise RoadmapError("Roadmap v1 project items require local title data")
            else:
                if not item.project_asset_id:
                    raise RoadmapError(
                        "Roadmap v2 Phase/Gate/Milestone items require project_asset_id"
                    )
                if item.title or item.description or item.schedule.scheduled:
                    raise RoadmapError(
                        "Roadmap v2 project assets may persist only project_asset_id, lane, and order"
                    )
        known = set(item_ids)
        relation_keys: set[tuple[str, str, str]] = set()
        for relation in self.relations:
            if relation.source not in known or relation.target not in known:
                raise RoadmapError(
                    "roadmap relation references an item that is not in the roadmap"
                )
            if relation.key in relation_keys:
                raise RoadmapError("duplicate roadmap relation")
            relation_keys.add(relation.key)
        _validate_block_graph(self.items, self.relations)
        object.__setattr__(self, "id", roadmap_id)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "revision", revision)

    def item(self, item_id: str) -> RoadmapItem:
        safe_id = validate_roadmap_id(item_id, label="roadmap item id")
        for item in self.items:
            if item.id == safe_id:
                return item
        raise RoadmapNotFoundError(f"roadmap item not found: {safe_id}")

    def as_mapping(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "id": self.id,
            "title": self.title,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "items": [item.as_mapping() for item in self.items],
        }
        if self.description:
            payload["description"] = self.description
        if self.relations:
            payload["relations"] = [relation.as_mapping() for relation in self.relations]
        return payload

    @classmethod
    def from_mapping(cls, raw: object) -> "Roadmap":
        if not isinstance(raw, Mapping):
            raise RoadmapError("roadmap YAML must contain a mapping")
        items_raw = raw.get("items") or []
        relations_raw = raw.get("relations") or []
        if not isinstance(items_raw, Sequence) or isinstance(items_raw, (str, bytes)):
            raise RoadmapError("roadmap items must be a list")
        if not isinstance(relations_raw, Sequence) or isinstance(
            relations_raw, (str, bytes)
        ):
            raise RoadmapError("roadmap relations must be a list")
        return cls(
            schema_version=int(raw.get("schema_version", 0)),
            id=str(raw.get("id", "")),
            title=str(raw.get("title", "")),
            description=str(raw.get("description", "")),
            revision=int(raw.get("revision", 1)),
            created_at=str(raw.get("created_at", "")),
            updated_at=str(raw.get("updated_at", "")),
            items=tuple(RoadmapItem.from_mapping(item) for item in items_raw),
            relations=tuple(
                RoadmapRelation.from_mapping(relation) for relation in relations_raw
            ),
        )


def _validate_block_graph(
    items: Sequence[RoadmapItem], relations: Sequence[RoadmapRelation]
) -> None:
    """Reject cycles in ``blocks`` relations while allowing symmetric related links."""

    adjacency = {item.id: [] for item in items}
    for relation in relations:
        if relation.kind == "blocks":
            adjacency[relation.source].append(relation.target)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visited:
            return
        if node in visiting:
            raise RoadmapError("blocks relations must form an acyclic graph")
        visiting.add(node)
        for target in adjacency[node]:
            visit(target)
        visiting.remove(node)
        visited.add(node)

    for item in adjacency:
        visit(item)
