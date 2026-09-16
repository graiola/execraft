"""HTTP-independent runtime topology/selection route group."""

from __future__ import annotations

from typing import Any, Mapping

from execraft.gui.runtime_topology import (
    apply_execution_setup,
    apply_openclaw_install,
    apply_openclaw_setup,
    apply_runtime_migration,
    diagnose_runtime_layers,
    preview_execution_setup,
    preview_openclaw_install,
    preview_openclaw_setup,
    preview_runtime_migration,
    runtime_setup_snapshot,
    runtime_topology_snapshot,
)

from .onboarding import _MISSING, _query_value
from .payload import payload_bool
from ..errors import GuiError


def dispatch_get(service: Any, path: str, query: Mapping[str, list[str]]) -> Any:
    if path == "/api/runtime/topology":
        return runtime_topology_snapshot(service, _query_value(query, "project_id"))
    if path == "/api/runtime/setup":
        return runtime_setup_snapshot(service, _query_value(query, "project_id"))
    return _MISSING


def dispatch_post(service: Any, path: str, payload: Mapping[str, Any]) -> Any:
    if path == "/api/runtime/execution/setup/preview":
        return preview_execution_setup(service, payload)
    if path == "/api/runtime/execution/setup/apply":
        if not payload_bool(payload, "acknowledged", default=False):
            raise GuiError("execution configuration requires explicit acknowledgement")
        return apply_execution_setup(service, payload)
    if path == "/api/runtime/migration/preview":
        return preview_runtime_migration(service, str(payload.get("project_id", "")))
    if path == "/api/runtime/migration/apply":
        if not payload_bool(payload, "acknowledged", default=False):
            raise GuiError("execution migration requires explicit acknowledgement")
        return apply_runtime_migration(
            service,
            project_id=str(payload.get("project_id", "")),
            expected_sha256=str(payload.get("expected_sha256", "")),
        )
    if path == "/api/runtime/openclaw/setup/preview":
        return preview_openclaw_setup(service, payload)
    if path == "/api/runtime/openclaw/setup/apply":
        if not payload_bool(payload, "acknowledged", default=False):
            raise GuiError("OpenClaw configuration requires explicit acknowledgement")
        return apply_openclaw_setup(service, payload)
    if path == "/api/runtime/openclaw/install/preview":
        return preview_openclaw_install(
            service,
            prefix=str(payload.get("prefix", "~/.local")),
            version=str(payload.get("version", "")),
        )
    if path == "/api/runtime/openclaw/install/apply":
        if not payload_bool(payload, "acknowledged", default=False):
            raise GuiError("OpenClaw installation requires explicit acknowledgement")
        return apply_openclaw_install(
            service,
            prefix=str(payload.get("prefix", "~/.local")),
            version=str(payload.get("version", "")),
        )
    if path == "/api/runtime/diagnostics":
        return diagnose_runtime_layers(
            service,
            project_id=str(payload.get("project_id", "")),
            runtime_id=str(payload.get("runtime_id", "")),
            model_route_id=str(payload.get("model_route_id", "")),
            target_id=str(payload.get("target_id", "")),
        )
    if path == "/api/runtime/selection/preview":
        return service.preview_runtime_selection(
            package_id=str(payload.get("package_id", "")),
            role=str(payload.get("role", "implement")),
            mode=str(payload.get("mode", "automatic")),
            runtime_id=str(payload.get("runtime_id", "")),
            model_route_id=str(payload.get("model_route_id", "")),
            target_id=str(payload.get("target_id", "")),
            apply_to_shards=payload_bool(payload, "apply_to_shards", default=False),
        )
    if path == "/api/runtime/selection/apply":
        return service.apply_runtime_selection(
            package_id=str(payload.get("package_id", "")),
            role=str(payload.get("role", "implement")),
            mode=str(payload.get("mode", "automatic")),
            runtime_id=str(payload.get("runtime_id", "")),
            model_route_id=str(payload.get("model_route_id", "")),
            target_id=str(payload.get("target_id", "")),
            apply_to_shards=payload_bool(payload, "apply_to_shards", default=False),
            cancel_and_switch=payload_bool(payload, "cancel_and_switch", default=False),
        )
    return _MISSING
