"""Runtime-neutral execution health projection for the dashboard.

The dashboard historically rendered ``AgentProviderConfig`` rows, which made
OpenClaw profiles disappear because they cannot be projected onto the Native
provider compatibility API.  This module renders the normalized execution
model instead and accepts the two Native-only maintenance callbacks explicitly.

Keeping this projection out of ``gui.server`` prevents runtime/model/target
presentation rules from leaking into the HTTP/application coordinator.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

from execraft.agents.execution_compat import project_native_profile_legacy_config
from execraft.runtime.config_migration import normalize_execution_for_operator
from execraft.runtime.product_support import profile_product_support
from execraft.runtime_config import RuntimeKind


PromotionFields = Callable[[Any], Mapping[str, Any]]
ActionState = Callable[[str], Mapping[str, Any]]


def _value(item: Any, default: str = "") -> str:
    value = getattr(item, "value", item)
    return str(value if value is not None else default)


def _endpoint_row(*, route: Any, target: Any, target_id: str, endpoint: Any, probe: Mapping[str, Any]) -> dict[str, Any]:
    """Return one browser-safe physical/model endpoint projection."""

    return {
        "id": endpoint.endpoint_id if endpoint else (target_id or "local"),
        "url": endpoint.base_url if endpoint else (target.endpoint if target else ""),
        "source": endpoint.base_url_source if endpoint else "execution topology",
        "target_id": target_id or "local",
        "target_kind": endpoint.target_kind.value if endpoint else _value(getattr(target, "kind", "local"), "local"),
        "provider_family": endpoint.provider_family if endpoint else (route.provider if route else ""),
        "provider_alias": endpoint.provider_id if endpoint else (route.provider_alias if route else ""),
        "api_family": endpoint.kind if endpoint else (route.api_family if route else ""),
        "concurrency_group": (
            endpoint.concurrency_group
            if endpoint
            else (target.concurrency_group if target else "")
        ),
        **dict(probe),
    }


def build_execution_health_rows(
    *,
    agents_path: Path,
    registry: Any,
    endpoint_status: Mapping[str, Mapping[str, Any]],
    assignments: list[dict[str, Any]],
    health_store: Any,
    promotion_fields: PromotionFields,
    action_state: ActionState,
) -> list[dict[str, Any]]:
    """Render all execution profiles from the normalized topology.

    Native provider maintenance metadata remains available through explicit
    callbacks, but the inventory itself is runtime-neutral.  OpenClaw profiles
    therefore appear in System health without pretending to be Native providers.
    """

    raw = yaml.safe_load(agents_path.read_text(encoding="utf-8")) or {}
    execution, _warnings = normalize_execution_for_operator(
        raw,
        model_registry=registry.model_registry,
    )
    assigned_by_agent: dict[str, list[dict[str, Any]]] = {}
    for assignment in assignments:
        agent_id = str(assignment.get("agent_id", ""))
        if agent_id:
            assigned_by_agent.setdefault(agent_id, []).append(assignment)

    rows: list[dict[str, Any]] = []
    for profile in execution.agents:
        runtime = execution.runtime(profile.runtime_id)
        route = execution.model_route(profile.model_route_id) if profile.model_route_id else None
        target_id = profile.target_id or (route.default_target if route else "")
        target = execution.target(target_id) if target_id else None
        health = health_store.get(profile.candidate_id)
        support = profile_product_support(execution, profile)

        endpoint = None
        if route is not None:
            endpoint = registry.endpoint_for_model(
                route.reference_for_native_adapter("opencode")
            )
        probe = endpoint_status.get(endpoint.endpoint_id, {}) if endpoint else {}

        capabilities = sorted(profile.capabilities, key=lambda item: item.value)
        row: dict[str, Any] = {
            "id": profile.candidate_id,
            "name": profile.name or profile.candidate_id,
            "adapter": runtime.adapter or runtime.kind.value,
            "runtime_id": runtime.id,
            "runtime_kind": runtime.kind.value,
            "enabled": profile.enabled,
            "support": support.as_mapping(),
            "model": route.model if route else "",
            "model_route_id": profile.model_route_id,
            "target_id": target_id,
            "capabilities": [item.value for item in capabilities],
            "priority": profile.priority,
            "weight": profile.capability_weight,
            "weights": {
                item.value: profile.weight_for_capability(item)
                for item in capabilities
            },
            "max_complexity": {
                item.value: profile.max_complexity_for(item)
                for item in capabilities
            },
            "effective_max_complexity": {
                item.value: profile.max_complexity_for(item)
                for item in capabilities
            },
            "promotions": [],
            "concurrency_group": profile.concurrency_group,
            "timeouts": {
                "total_seconds": profile.policy.timeout_seconds,
                "inactivity_seconds": profile.policy.inactivity_timeout_seconds,
                "first_output_seconds": profile.policy.first_output_timeout_seconds,
                "output_silence_seconds": profile.policy.output_silence_timeout_seconds,
                "max_output_bytes": profile.policy.max_output_bytes,
            },
            "health": {
                "status": health.status,
                "available": health.is_available,
                "reason": health.reason,
                "unavailable_until": health.unavailable_until,
                "failures": health.consecutive_failures,
            },
            "endpoint": _endpoint_row(
                route=route,
                target=target,
                target_id=target_id,
                endpoint=endpoint,
                probe=probe,
            ),
            "assignments": assigned_by_agent.get(profile.candidate_id, []),
            "action": {},
            "native_maintenance": runtime.kind == RuntimeKind.NATIVE,
        }

        if runtime.kind == RuntimeKind.NATIVE:
            legacy = project_native_profile_legacy_config(execution, profile)
            row.update(promotion_fields(legacy))
            row["action"] = dict(action_state(profile.candidate_id))
        rows.append(row)
    return rows
