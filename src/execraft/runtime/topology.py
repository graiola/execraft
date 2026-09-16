"""Runtime/model/target topology read model for operator surfaces.

The orchestration scheduler owns execution decisions.  This module only renders
normalized configuration into a stable, JSON-safe read model shared by CLI,
onboarding and GUI code.  It deliberately contains no OpenClaw lifecycle logic
and never resolves credential values.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from execraft.runtime.product_support import (
    profile_product_support,
    runtime_product_support,
    target_product_support,
)


class RuntimeTopologyError(ValueError):
    """Raised when an operator selection cannot be represented unambiguously."""


def _enum_value(value: object) -> str:
    raw = getattr(value, "value", value)
    return "" if raw is None else str(raw)


def _capability_values(values: object) -> list[str]:
    if values is None:
        return []
    return [_enum_value(item) for item in values]


def _openclaw_summary(runtime: Any) -> dict[str, Any] | None:
    openclaw = getattr(runtime, "openclaw", None)
    if openclaw is None:
        return None
    return {
        "mode": _enum_value(getattr(openclaw, "mode", "managed")),
        "gateway": str(getattr(openclaw, "gateway", "")),
        "version_policy": _enum_value(getattr(openclaw, "version_policy", "")),
        # A SecretRef is configuration metadata, but operator surfaces intentionally
        # avoid echoing even reference strings in broad topology inventories.
        "authentication_configured": bool(str(getattr(openclaw, "auth_ref", "")).strip()),
    }


def _runtime_row(runtime: Any, health: Mapping[str, Any] | None) -> dict[str, Any]:
    support = runtime_product_support(runtime)
    row = {
        "id": str(runtime.id),
        "kind": _enum_value(runtime.kind),
        "adapter": str(getattr(runtime, "adapter", "")),
        "binary": str(getattr(runtime, "binary", "")),
        "support": support.as_mapping(),
    }
    openclaw = _openclaw_summary(runtime)
    if openclaw is not None:
        row["openclaw"] = openclaw
    if health is not None:
        row["health"] = dict(health)
    return row


def _route_row(route: Any) -> dict[str, Any]:
    return {
        "id": str(route.id),
        "provider": str(getattr(route, "provider", "")),
        "provider_alias": str(getattr(route, "provider_alias", "")),
        "model": str(getattr(route, "model", "")),
        "api_family": str(getattr(route, "api_family", "")),
        "endpoint": str(getattr(route, "endpoint", "")),
        "context_window": getattr(route, "context_window", None),
        "default_target": str(getattr(route, "default_target", "")),
        "capabilities": _capability_values(getattr(route, "capabilities", ())),
        "credential_configured": bool(str(getattr(route, "credential_ref", "")).strip()),
    }


def _target_row(target: Any, health: Mapping[str, Any] | None) -> dict[str, Any]:
    support = target_product_support(target)
    row = {
        "id": str(target.id),
        "kind": _enum_value(target.kind),
        "endpoint": str(getattr(target, "endpoint", "")),
        "concurrency_group": str(getattr(target, "concurrency_group", "")),
        "workspace_transport": str(getattr(target, "workspace_transport", "")),
        "max_concurrency": int(getattr(target, "max_concurrency", 0) or 0),
        "environment": (
            target.environment.as_mapping()
            if getattr(target, "environment", None) is not None
            else {}
        ),
        "support": support.as_mapping(),
    }
    if health is not None:
        row["health"] = dict(health)
    return row


def _profile_row(config: Any, profile: Any) -> dict[str, Any]:
    support = profile_product_support(config, profile)
    runtime = config.runtime(profile.runtime_id)
    route = config.model_route(profile.model_route_id) if profile.model_route_id else None
    target_id = str(getattr(profile, "target_id", "")) or (
        str(getattr(route, "default_target", "")) if route else ""
    )
    target = config.target(target_id) if target_id else None
    return {
        "id": str(profile.id),
        "name": str(getattr(profile, "name", profile.id)),
        "enabled": bool(getattr(profile, "enabled", True)),
        "capabilities": _capability_values(getattr(profile, "capabilities", ())),
        "priority": int(getattr(profile, "priority", 0)),
        "runtime_id": str(profile.runtime_id),
        "runtime_kind": _enum_value(runtime.kind),
        "model_route_id": str(getattr(profile, "model_route_id", "")),
        "model_provider": str(getattr(route, "provider", "")) if route else "",
        "model": str(getattr(route, "model", "")) if route else "",
        "target_id": target_id,
        "target_kind": _enum_value(target.kind) if target else "",
        "support": support.as_mapping(),
        "effective_tuple": {
            "profile": str(profile.id),
            "runtime": str(profile.runtime_id),
            "model_route": str(getattr(profile, "model_route_id", "")),
            "target": target_id,
        },
    }


def build_runtime_topology(
    config: Any,
    *,
    runtime_health: Mapping[str, Mapping[str, Any]] | None = None,
    target_health: Mapping[str, Mapping[str, Any]] | None = None,
    security_posture: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return the normalized execution topology for CLI/onboarding/GUI consumers.

    Health/security data is injected by callers because probing runtimes or model
    endpoints is an operational action and must not happen merely because a UI
    asks for configuration inventory.
    """

    runtime_health = runtime_health or {}
    target_health = target_health or {}
    security_posture = security_posture or {}
    runtimes = []
    for runtime in config.runtimes:
        row = _runtime_row(runtime, runtime_health.get(runtime.id))
        if runtime.id in security_posture:
            row["security"] = dict(security_posture[runtime.id])
        runtimes.append(row)
    return {
        "schema_version": int(getattr(config, "schema_version", 4)),
        "source_schema_version": int(getattr(config, "source_schema_version", 4)),
        "runtimes": runtimes,
        "model_routes": [_route_row(item) for item in config.model_routes],
        "execution_targets": [
            _target_row(item, target_health.get(item.id)) for item in config.targets
        ],
        "profiles": [_profile_row(config, item) for item in config.agents],
        "switching": {
            "hot_migration_supported": False,
            "active_invocation": "cancel_then_restart_or_apply_after_current_attempt",
            "idle_or_between_attempts": "next_invocation",
        },
    }


@dataclass(frozen=True)
class SimpleRouteSelection:
    runtime_id: str
    model_route_id: str
    target_id: str
    inferred_target: bool

    def as_mapping(self) -> dict[str, Any]:
        return {
            "runtime_id": self.runtime_id,
            "model_route_id": self.model_route_id,
            "target_id": self.target_id,
            "inferred_target": self.inferred_target,
        }


def resolve_simple_selection(
    config: Any,
    *,
    runtime_id: str,
    model_route_id: str,
) -> SimpleRouteSelection:
    """Resolve the runtime+model UX to an unambiguous target.

    An explicit model-route default wins.  Otherwise only one local target may be
    inferred.  Ambiguity is intentionally surfaced so the GUI can switch to its
    advanced target selector rather than guessing a physical execution location.
    """

    config.runtime(runtime_id)
    route = config.model_route(model_route_id)
    default_target = str(getattr(route, "default_target", ""))
    if default_target:
        config.target(default_target)
        return SimpleRouteSelection(runtime_id, model_route_id, default_target, False)

    local_targets = [
        item
        for item in config.targets
        if _enum_value(item.kind) == "local"
        and (
            not str(getattr(route, "endpoint", ""))
            or not str(getattr(item, "endpoint", ""))
            or str(getattr(route, "endpoint", "")).rstrip("/")
            == str(getattr(item, "endpoint", "")).rstrip("/")
        )
    ]
    if len(local_targets) == 1:
        return SimpleRouteSelection(runtime_id, model_route_id, local_targets[0].id, True)
    if not local_targets:
        # Cloud/direct routes may legitimately have no physical target.
        return SimpleRouteSelection(runtime_id, model_route_id, "", False)
    raise RuntimeTopologyError(
        f"model route {model_route_id!r} has {len(local_targets)} compatible local targets; "
        "select an execution target in advanced mode"
    )
