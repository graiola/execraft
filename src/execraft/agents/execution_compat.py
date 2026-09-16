"""Compatibility translation between legacy provider config and schema v4.

This module is deliberately separate from the schema-v4 parser: legacy
provider semantics are migration input/output, not part of the normalized
runtime/model/target domain.
"""

from __future__ import annotations

from typing import Sequence

from execraft.agents.config_errors import AgentConfigError
from execraft.agents.execution_config import ExecutionArchitectureConfig
from execraft.agents.legacy_config import AgentProviderConfig
from execraft.agents.profile import AgentExecutionPolicy, AgentProfileConfig
from execraft.model_routes import ModelRouteConfig
from execraft.runtime_config import RuntimeConfig, RuntimeKind


def _legacy_model_parts(config: AgentProviderConfig) -> tuple[str, str]:
    if not config.model:
        return "", ""
    if "/" in config.model:
        return tuple(config.model.split("/", 1))  # type: ignore[return-value]
    if config.adapter in {"claude", "claude-code"}:
        return "anthropic", config.model
    if config.adapter == "codex":
        return "openai", config.model
    if config.adapter in {"antigravity", "antigravity-cli"}:
        return "antigravity", config.model
    return config.adapter, config.model


def _profile_from_legacy(
    config: AgentProviderConfig,
) -> tuple[AgentProfileConfig, RuntimeConfig, ModelRouteConfig | None]:
    runtime_id = f"legacy-runtime:{config.provider_id}"
    runtime = RuntimeConfig(
        id=runtime_id,
        kind=RuntimeKind.NATIVE,
        adapter=config.adapter,
        binary=config.binary,
    )
    provider, model = _legacy_model_parts(config)
    route: ModelRouteConfig | None = None
    route_id = ""
    if config.model:
        route_id = f"legacy-model:{config.provider_id}"
        route = ModelRouteConfig(id=route_id, provider=provider, model=model)
    profile = AgentProfileConfig(
        id=config.provider_id,
        name=config.name,
        enabled=config.enabled,
        aliases=config.aliases,
        capabilities=config.capabilities,
        runtime_id=runtime_id,
        model_route_id=route_id,
        priority=config.priority,
        capability_weight=config.capability_weight,
        capability_weights=config.capability_weights,
        max_complexity=config.max_complexity,
        max_complexity_by_capability=config.max_complexity_by_capability,
        concurrency_group=config.concurrency_group,
        policy=AgentExecutionPolicy(
            timeout_seconds=config.timeout_seconds,
            inactivity_timeout_seconds=config.inactivity_timeout_seconds,
            output_silence_timeout_seconds=config.output_silence_timeout_seconds,
            first_output_timeout_seconds=config.first_output_timeout_seconds,
            max_output_bytes=config.max_output_bytes,
            max_internal_retry_delay_seconds=config.max_internal_retry_delay_seconds,
            live_sessions=config.live_sessions,
            auto_approve=config.auto_approve,
            sandbox=config.sandbox,
            permission_mode=config.permission_mode,
            agent_by_capability=config.agent_by_capability,
            format_repair_agent=config.format_repair_agent,
            effort=config.effort,
            effort_by_capability=config.effort_by_capability,
            dangerously_skip_permissions=config.dangerously_skip_permissions,
            sandbox_enabled=config.sandbox_enabled,
            policy_paths=config.policy_paths,
        ),
    )
    return profile, runtime, route


def normalize_legacy_agent_configs(
    configs: Sequence[AgentProviderConfig],
    *,
    source_schema_version: int,
) -> ExecutionArchitectureConfig:
    """Translate validated v1-v3 provider declarations into the v4 domain model.

    Execution targets are intentionally left unresolved by this compatibility projection. Legacy provider
    files do not contain enough information to distinguish a local model from an
    inference endpoint reliably; canonical model routing resolves that from endpoint
    registry instead of encoding a misleading guess here.
    """

    agents: list[AgentProfileConfig] = []
    runtimes: list[RuntimeConfig] = []
    routes: list[ModelRouteConfig] = []
    for config in configs:
        profile, runtime, route = _profile_from_legacy(config)
        agents.append(profile)
        runtimes.append(runtime)
        if route is not None:
            routes.append(route)
    return ExecutionArchitectureConfig(
        agents=tuple(agents),
        runtimes=tuple(runtimes),
        model_routes=tuple(routes),
        targets=(),
        source_schema_version=source_schema_version,
    )


def project_native_profile_legacy_config(
    config: ExecutionArchitectureConfig,
    profile: AgentProfileConfig,
) -> AgentProviderConfig:
    """Project one Native profile without requiring every profile to be Native.

    Orchestration uses this narrow compatibility seam so mixed Native and
    OpenClaw schema-v4 configurations can coexist.  The public
    ``project_native_legacy_configs`` function retains its historical fail-closed
    behavior for callers expecting a provider-only configuration list.
    """

    runtime = config.runtime(profile.runtime_id)
    if runtime.kind != RuntimeKind.NATIVE:
        raise AgentConfigError(
            f"agent {profile.id!r} selects runtime {runtime.id!r} of kind "
            f"{runtime.kind.value!r}; a Native compatibility projection is not available"
        )
    route = config.model_route(profile.model_route_id) if profile.model_route_id else None
    model = route.reference_for_native_adapter(runtime.adapter) if route else ""
    if runtime.adapter == "opencode" and not model:
        raise AgentConfigError(
            f"agent {profile.id!r} using Native OpenCode requires model_route"
        )
    policy = profile.policy
    target_id = profile.target_id or (route.default_target if route else "")
    target = config.target(target_id) if target_id else None
    return AgentProviderConfig(
        name=profile.name,
        adapter=runtime.adapter,
        enabled=profile.enabled,
        provider_id=profile.id,
        binary=runtime.binary,
        capabilities=profile.capabilities,
        model=model,
        timeout_seconds=policy.timeout_seconds,
        inactivity_timeout_seconds=policy.inactivity_timeout_seconds,
        output_silence_timeout_seconds=policy.output_silence_timeout_seconds,
        first_output_timeout_seconds=policy.first_output_timeout_seconds,
        max_output_bytes=policy.max_output_bytes,
        max_internal_retry_delay_seconds=policy.max_internal_retry_delay_seconds,
        live_sessions=policy.live_sessions,
        priority=profile.priority,
        auto_approve=policy.auto_approve,
        sandbox=policy.sandbox,
        permission_mode=policy.permission_mode,
        aliases=profile.aliases,
        agent_by_capability=policy.agent_by_capability,
        format_repair_agent=policy.format_repair_agent,
        capability_weight=profile.capability_weight,
        capability_weights=profile.capability_weights,
        max_complexity=profile.max_complexity,
        max_complexity_by_capability=profile.max_complexity_by_capability,
        effort=policy.effort,
        effort_by_capability=policy.effort_by_capability,
        dangerously_skip_permissions=policy.dangerously_skip_permissions,
        sandbox_enabled=policy.sandbox_enabled,
        policy_paths=policy.policy_paths,
        concurrency_group=profile.concurrency_group,
        candidate_id=profile.candidate_id,
        runtime_id=runtime.id,
        runtime_backend=runtime.adapter,
        model_route_id=route.id if route else "",
        model_provider_semantic=route.provider if route else "",
        target_id=target_id,
        target_kind=target.kind.value if target else "",
        target_concurrency_group=target.concurrency_group if target else "",
    )


def project_native_legacy_configs(
    config: ExecutionArchitectureConfig,
) -> list[AgentProviderConfig]:
    """Project schema-v4 Native profiles onto the existing adapter contract.

    This public compatibility API remains provider-only and therefore continues
    to reject mixed/non-Native topologies.  Runtime-aware registration uses
    ``project_native_profile_legacy_config`` profile-by-profile instead.
    """

    projected: list[tuple[int, AgentProviderConfig]] = []
    for index, profile in enumerate(config.agents):
        runtime = config.runtime(profile.runtime_id)
        if runtime.kind != RuntimeKind.NATIVE:
            raise AgentConfigError(
                f"agent {profile.id!r} selects runtime {runtime.id!r} of kind "
                f"{runtime.kind.value!r}; this runtime cannot be projected onto "
                "the legacy provider-only API"
            )
        projected.append((index, project_native_profile_legacy_config(config, profile)))
    projected.sort(key=lambda item: (-item[1].priority, item[0]))
    return [item[1] for item in projected]
