"""Compatibility resolution from configured providers to execution identity."""

from __future__ import annotations

from execraft.execution_identity import ExecutionIdentity
from execraft.model_registry import ModelRouteRegistry

from .legacy_config import AgentProviderConfig


def _native_model_parts(config: AgentProviderConfig) -> tuple[str, str]:
    """Return semantic provider/model when no canonical route is available."""

    if not config.model:
        return str(getattr(config, "model_provider_semantic", "") or ""), ""
    semantic = str(getattr(config, "model_provider_semantic", "") or "").strip()
    if "/" in config.model:
        alias, model = config.model.split("/", 1)
        return semantic or alias, model
    if semantic:
        return semantic, config.model
    if config.adapter in {"claude", "claude-code"}:
        return "anthropic", config.model
    if config.adapter == "codex":
        return "openai", config.model
    if config.adapter in {"antigravity", "antigravity-cli"}:
        return "antigravity", config.model
    return config.adapter, config.model


def execution_identity_for_provider(
    config: AgentProviderConfig,
    *,
    model_registry: ModelRouteRegistry | None = None,
) -> ExecutionIdentity:
    """Resolve a compatibility provider into the normalized execution identity.

    Schema-v4 projections carry authoritative IDs directly.  For v1-v3
    For OpenCode configurations, the canonical model registry enriches the legacy
    provider with the real route and physical target instead of inventing a
    synthetic placement.  Other legacy providers retain conservative local
    compatibility metadata until a canonical route is configured.
    """

    candidate_id = str(getattr(config, "candidate_id", "") or config.provider_id).strip()
    runtime_id = str(getattr(config, "runtime_id", "") or "native").strip()
    runtime_backend = str(getattr(config, "runtime_backend", "") or config.adapter).strip()
    model_route_id = str(getattr(config, "model_route_id", "") or "").strip()
    target_id = str(getattr(config, "target_id", "") or "").strip()
    target_kind = str(getattr(config, "target_kind", "") or "").strip()
    target_group = str(getattr(config, "target_concurrency_group", "") or "").strip()
    model_provider, model = _native_model_parts(config)

    route = model_registry.route_for_model(config.model) if model_registry and config.model else None
    endpoint = model_registry.endpoint_for_model(config.model) if model_registry and config.model else None
    if route is not None:
        model_route_id = model_route_id or route.id
        model_provider = route.provider
        model = route.model
        target_id = target_id or route.default_target
    if endpoint is not None:
        target_id = target_id or endpoint.target_id
        target_kind = target_kind or endpoint.target_kind.value
        target_group = endpoint.concurrency_group or target_group

    profile_group = str(config.concurrency_group or "").strip()
    concurrency_group = target_group or profile_group or candidate_id
    return ExecutionIdentity(
        candidate_id=candidate_id,
        runtime_id=runtime_id,
        runtime_backend=runtime_backend,
        model_route_id=model_route_id,
        model_provider=model_provider,
        model=model,
        target_id=target_id,
        target_kind=target_kind,
        concurrency_group=concurrency_group,
        legacy_provider_id=config.provider_id,
    )
