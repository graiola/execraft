"""Project roadmap HTTP-independent route dispatch."""

from __future__ import annotations

from typing import Any, Mapping

from execraft.gui.errors import GuiError

from .onboarding import _MISSING, _query_value
from .payload import payload_bool, payload_int


def _item_mapping(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Decode one roadmap item without permissive scalar coercion."""

    value = payload.get("item")
    if not isinstance(value, Mapping):
        raise GuiError("item must be a JSON object")
    for key in ("id", "kind", "title", "description", "task_id", "project_asset_id", "lane"):
        if key in value and not isinstance(value[key], str):
            raise GuiError(f"item.{key} must be a JSON string")
    order = value.get("order", 0)
    if not isinstance(order, int) or isinstance(order, bool):
        raise GuiError("item.order must be a JSON integer")
    schedule = value.get("schedule")
    if schedule is not None:
        if not isinstance(schedule, Mapping):
            raise GuiError("item.schedule must be a JSON object")
        for key in ("start", "target"):
            if key in schedule and not isinstance(schedule[key], str):
                raise GuiError(f"item.schedule.{key} must be a JSON string")
    return value



def _payload_string(
    payload: Mapping[str, Any], key: str, *, default: str = ""
) -> str:
    """Decode one JSON string without silently coercing numbers/booleans."""

    value = payload.get(key, default)
    if not isinstance(value, str):
        raise GuiError(f"{key} must be a JSON string")
    return value

def dispatch_get(service: Any, path: str, query: Mapping[str, list[str]]) -> Any:
    """Dispatch one roadmap read request or return ``_MISSING``."""

    if path == "/api/roadmaps":
        return service.roadmap_list(_query_value(query, "project_id"))
    if path == "/api/roadmap":
        return service.roadmap_get(
            _query_value(query, "project_id"),
            _query_value(query, "roadmap_id"),
        )
    if path == "/api/roadmap/migration/preview":
        return service.roadmap_migration_preview(
            _query_value(query, "project_id"),
            _query_value(query, "roadmap_id"),
        )
    return _MISSING


def dispatch_post(service: Any, path: str, payload: Mapping[str, Any]) -> Any:
    """Dispatch one roadmap mutation request or return ``_MISSING``."""

    project_id = _payload_string(payload, "project_id")
    roadmap_id = _payload_string(payload, "roadmap_id")
    if path == "/api/roadmap/migrate-v1":
        return service.roadmap_migrate_v1(
            project_id,
            roadmap_id,
            expected_revision=payload_int(payload, "expected_revision", default=0),
        )
    if path == "/api/roadmap/create":
        return service.roadmap_create(
            project_id,
            title=_payload_string(payload, "title"),
            description=_payload_string(payload, "description"),
            roadmap_id=roadmap_id,
        )
    if path == "/api/roadmap/metadata/update":
        return service.roadmap_update_metadata(
            project_id,
            roadmap_id,
            expected_revision=payload_int(payload, "expected_revision", default=0),
            title=_payload_string(payload, "title"),
            description=_payload_string(payload, "description"),
        )
    if path == "/api/roadmap/item/upsert":
        return service.roadmap_upsert_item(
            project_id,
            roadmap_id,
            expected_revision=payload_int(payload, "expected_revision", default=0),
            expected_project_execution_revision=payload_int(
                payload, "expected_project_execution_revision", default=0
            ),
            item=_item_mapping(payload),
        )
    if path == "/api/roadmap/item/move":
        return service.roadmap_move_item(
            project_id,
            roadmap_id,
            expected_revision=payload_int(payload, "expected_revision", default=0),
            expected_project_execution_revision=payload_int(
                payload, "expected_project_execution_revision", default=0
            ),
            item_id=_payload_string(payload, "item_id"),
            lane=_payload_string(payload, "lane"),
            start=_payload_string(payload, "start"),
            target=_payload_string(payload, "target"),
            target_item_id=_payload_string(payload, "target_item_id"),
            placement=_payload_string(payload, "placement", default="before"),
        )
    if path == "/api/roadmap/lane/move":
        return service.roadmap_move_lane(
            project_id,
            roadmap_id,
            expected_revision=payload_int(payload, "expected_revision", default=0),
            lane=_payload_string(payload, "lane"),
            target_lane=_payload_string(payload, "target_lane"),
            placement=_payload_string(payload, "placement", default="before"),
        )
    if path == "/api/roadmap/item/delete":
        return service.roadmap_delete_item(
            project_id,
            roadmap_id,
            item_id=_payload_string(payload, "item_id"),
            expected_revision=payload_int(payload, "expected_revision", default=0),
        )
    if path == "/api/roadmap/item/link-task":
        return service.roadmap_link_task(
            project_id,
            roadmap_id,
            expected_revision=payload_int(payload, "expected_revision", default=0),
            task_id=_payload_string(payload, "task_id"),
            item_id=_payload_string(payload, "item_id"),
            lane=_payload_string(payload, "lane"),
            start=_payload_string(payload, "start"),
            target=_payload_string(payload, "target"),
            order=payload_int(payload, "order", default=0),
        )
    if path == "/api/roadmap/relation/upsert":
        return service.roadmap_upsert_relation(
            project_id,
            roadmap_id,
            expected_revision=payload_int(payload, "expected_revision", default=0),
            source=_payload_string(payload, "from"),
            target=_payload_string(payload, "to"),
            kind=_payload_string(payload, "kind", default="blocks"),
        )
    if path == "/api/roadmap/relation/delete":
        return service.roadmap_delete_relation(
            project_id,
            roadmap_id,
            expected_revision=payload_int(payload, "expected_revision", default=0),
            source=_payload_string(payload, "from"),
            target=_payload_string(payload, "to"),
            kind=_payload_string(payload, "kind", default="blocks"),
        )
    if path == "/api/roadmap/delete":
        return service.roadmap_delete(
            project_id,
            roadmap_id,
            expected_revision=payload_int(payload, "expected_revision", default=0),
            acknowledged=payload_bool(payload, "acknowledged", default=False),
        )
    return _MISSING


__all__ = ["dispatch_get", "dispatch_post"]
