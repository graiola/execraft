"""Project-home and onboarding HTTP route dispatch."""

from __future__ import annotations

from typing import Any, Mapping

from .payload import payload_bool, payload_int_list, payload_string_list


_MISSING = object()


def _query_value(query: Mapping[str, list[str]], key: str, default: str = "") -> str:
    values = query.get(key)
    return values[0] if values else default


def dispatch_get(service: Any, path: str, query: Mapping[str, list[str]]) -> Any:
    """Dispatch one project-home read request or return ``_MISSING``."""

    if path == "/api/home":
        return service.home_snapshot()
    if path == "/api/projects":
        return {"projects": service.home_snapshot().get("projects", [])}
    if path == "/api/onboarding/templates":
        return service.template_catalog()
    if path == "/api/onboarding/sessions":
        return {"sessions": service.onboarding_sessions()}
    if path == "/api/onboarding/readiness":
        return service.project_readiness(_query_value(query, "project_id"))
    if path == "/api/onboarding/execution":
        return service.project_execution(_query_value(query, "project_id"))
    if path == "/api/onboarding/providers":
        # Compatibility endpoint for older clients. New GUI surfaces consume
        # /execution so mixed Native/OpenClaw projects are never forced through
        # the provider-only projection.
        return service.project_providers(_query_value(query, "project_id"))
    if path == "/api/onboarding/verification":
        return service.verification_snapshot(_query_value(query, "project_id"))
    if path == "/api/onboarding/task":
        return service.task_review(
            _query_value(query, "project_id"),
            _query_value(query, "task_id"),
        )
    if path == "/api/archive":
        return service.archive_catalog()
    if path == "/api/archive/item":
        return service.inspect_archive(
            _query_value(query, "kind"),
            project_id=_query_value(query, "project_id"),
            item_id=_query_value(query, "id"),
        )
    return _MISSING


def dispatch_post(service: Any, path: str, payload: Mapping[str, Any]) -> Any:
    """Dispatch one project-home mutation request or return ``_MISSING``."""

    if path == "/api/session/open":
        return service.open_task(
            str(payload.get("project_id", "")),
            str(payload.get("task_id", "")),
            acknowledged=payload_bool(payload, "acknowledged", default=False),
        )
    if path == "/api/session/home":
        return service.open_home(
            acknowledged=payload_bool(payload, "acknowledged", default=False)
        )
    if path == "/api/session/catalog":
        return service.open_catalog(
            acknowledged=payload_bool(payload, "acknowledged", default=False)
        )
    if path == "/api/session/project":
        return service.open_project(
            str(payload.get("project_id", "")),
            acknowledged=payload_bool(payload, "acknowledged", default=False),
        )
    if path == "/api/project/focus":
        return service.focus_project(str(payload.get("project_id", "")))
    if path == "/api/onboarding/inspect":
        return service.inspect_source(
            str(payload.get("source_root", "")),
            template_id=str(payload.get("template_id", "standard")),
            feature_ids=payload_string_list(payload, "features"),
            include_devcontainer=payload_bool(payload, "devcontainer", default=False),
            accept_decisions=payload_bool(payload, "accept_decisions", default=False),
        )
    if path == "/api/onboarding/project/create":
        return service.create_project_from_source(
            str(payload.get("source_root", "")),
            template_id=str(payload.get("template_id", "standard")),
            feature_ids=payload_string_list(payload, "features"),
            include_devcontainer=payload_bool(payload, "devcontainer", default=False),
            accept_decisions=payload_bool(payload, "accept_decisions", default=False),
            acknowledged=payload_bool(payload, "acknowledged", default=False),
        )
    if path == "/api/onboarding/project/register":
        return service.register_descriptor(
            str(payload.get("descriptor", "")),
            source_root=str(payload.get("source_root", "")),
            replace=payload_bool(payload, "replace", default=False),
            acknowledged=payload_bool(payload, "acknowledged", default=False),
        )
    if path == "/api/onboarding/start/preview":
        return service.preview_start(payload)
    if path == "/api/onboarding/start/apply":
        return service.apply_start(
            payload,
            acknowledged=payload_bool(payload, "acknowledged", default=False),
        )
    if path == "/api/onboarding/greenfield/preview":
        return service.preview_greenfield(payload)
    if path == "/api/onboarding/greenfield/apply":
        return service.apply_greenfield(
            payload,
            acknowledged=payload_bool(payload, "acknowledged", default=False),
        )
    if path == "/api/onboarding/verification/update":
        return service.update_verification(
            str(payload.get("project_id", "")),
            expected_sha256=str(payload.get("expected_sha256", "")),
            enabled_indexes=payload_int_list(payload, "enabled_indexes"),
            require_commands=payload_bool(payload, "require_commands", default=False),
            acknowledged=payload_bool(payload, "acknowledged", default=False),
        )
    if path == "/api/archive/archive":
        return service.archive_catalog_entry(
            str(payload.get("kind", "")),
            project_id=str(payload.get("project_id", "")),
            item_id=str(payload.get("id", "")),
            reason=str(payload.get("reason", "")),
        )
    if path == "/api/archive/reactivate":
        return service.reactivate_catalog_entry(
            str(payload.get("kind", "")),
            project_id=str(payload.get("project_id", "")),
            item_id=str(payload.get("id", "")),
        )
    if path == "/api/archive/delete":
        return service.delete_catalog_entry(
            str(payload.get("kind", "")),
            project_id=str(payload.get("project_id", "")),
            item_id=str(payload.get("id", "")),
            confirmation=str(payload.get("confirmation", "")),
            delete_branches=payload_bool(payload, "delete_branches", default=False),
        )
    return _MISSING


__all__ = ["_MISSING", "dispatch_get", "dispatch_post"]
