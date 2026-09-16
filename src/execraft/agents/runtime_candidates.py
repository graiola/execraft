"""Construct runtime-aware scheduler candidates from normalized execution config.

This is the runtime-aware replacement for provider-only registration. It intentionally
keeps concrete runtime construction out of the orchestrator and leaves the
legacy ``parse_agent_configs`` API unchanged for compatibility callers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from execraft.execution_identity import ExecutionIdentity
from execraft.model_registry import ModelRouteRegistry
from execraft.runtime.native import build_native_runtime
from execraft.runtime.openclaw_agent import OpenClawAgentRuntime, OpenClawRuntimeHost
from execraft.runtime.openclaw_projection import project_openclaw_config
from execraft.runtime.openclaw_service import OpenClawGatewayService
from execraft.runtime.openclaw_skills import openclaw_profile_workspace
from execraft.runtime.registry import (
    RuntimeBuildContext,
    register_runtime_builder,
    runtime_builder,
)
from execraft.runtime.product_support import ensure_supported_execution
from execraft.runtime.subagent_policy import SubagentStrategy
from execraft.runtime_config import OpenClawMode, RuntimeKind

from .execution_compat import project_native_profile_legacy_config
from .execution_config import ExecutionArchitectureConfig


def build_runtime_candidates(
    execution: ExecutionArchitectureConfig,
    *,
    workdir: Path,
    state_root: Path,
    read_only: bool,
    opencode_config_path: Path | None = None,
    model_registry: ModelRouteRegistry | None = None,
    subagent_strategy: SubagentStrategy | None = None,
) -> list[Any]:
    """Build enabled candidates for every registered runtime kind.

    Runtime selection is registration-driven: each kind supplies a builder
    through ``execraft.runtime.registry`` rather than being an arm of a hard-coded
    branch here, so a new runtime needs configuration and registration only.
    """

    strategy = subagent_strategy or SubagentStrategy()
    ensure_supported_execution(execution, subagent_strategy=strategy)
    candidates: list[tuple[int, Any]] = []
    shared: dict[str, Any] = {}
    for index, profile in enumerate(execution.agents):
        if not profile.enabled:
            continue
        runtime = execution.runtime(profile.runtime_id)
        builder = runtime_builder(runtime.kind)
        candidate = builder(
            RuntimeBuildContext(
                execution=execution,
                profile=profile,
                runtime=runtime,
                workdir=workdir,
                state_root=state_root,
                read_only=read_only,
                opencode_config_path=opencode_config_path,
                model_registry=model_registry,
                subagent_strategy=strategy,
                shared=shared,
            )
        )
        candidates.append((index, candidate))
    candidates.sort(key=lambda item: (-_priority(item[1], execution), item[0]))
    return [candidate for _, candidate in candidates]


def _build_native_candidate(ctx: RuntimeBuildContext) -> Any:
    provider = project_native_profile_legacy_config(ctx.execution, ctx.profile)
    return build_native_runtime(
        provider,
        workdir=ctx.workdir,
        read_only=ctx.read_only,
        opencode_config_path=ctx.opencode_config_path,
        model_registry=ctx.model_registry,
    )


def _build_openclaw_candidate(ctx: RuntimeBuildContext) -> Any:
    """Build the promoted local/inference OpenClaw candidate.

    The supported product deliberately keeps sub-agent delegation and remote full-runtime
    placement out of normal candidate construction. Their isolated historical
    modules remain available for evidence and future experimentation.
    """

    execution = ctx.execution
    profile = ctx.profile
    runtime = ctx.runtime
    state_root = ctx.state_root
    openclaw_hosts: dict[str, OpenClawRuntimeHost] = ctx.shared.setdefault(
        "openclaw_hosts", {}
    )
    openclaw_workspaces: dict[str, dict[str, Path]] = ctx.shared.setdefault(
        "openclaw_workspaces", {}
    )
    identity = _openclaw_identity(execution, profile.id)
    managed_workspaces = openclaw_workspaces.get(runtime.id)
    if managed_workspaces is None:
        managed_workspaces = _managed_openclaw_workspaces(
            execution, runtime.id, state_root=state_root
        )
        openclaw_workspaces[runtime.id] = managed_workspaces

    host = openclaw_hosts.get(runtime.id)
    if host is None:
        projection = project_openclaw_config(
            execution, runtime.id, agent_workspaces=managed_workspaces or None
        )
        service = OpenClawGatewayService(
            runtime,
            state_root=state_root,
            config_payload=projection.config,
            credential_env_refs=projection.credential_refs,
        )
        host = OpenClawRuntimeHost(service)
        openclaw_hosts[runtime.id] = host

    candidate = OpenClawAgentRuntime(
        profile=profile,
        runtime=runtime,
        identity=identity,
        host=host,
        skill_workspace=managed_workspaces.get(profile.id),
    )
    _attach_profile_scheduler_policy(candidate, profile)
    return candidate


register_runtime_builder(RuntimeKind.NATIVE, _build_native_candidate)
register_runtime_builder(RuntimeKind.OPENCLAW, _build_openclaw_candidate)


def _openclaw_identity(
    execution: ExecutionArchitectureConfig, profile_id: str
) -> ExecutionIdentity:
    profile = execution.agent(profile_id)
    runtime = execution.runtime(profile.runtime_id)
    route = execution.model_route(profile.model_route_id) if profile.model_route_id else None
    target_id = profile.target_id or (route.default_target if route else "")
    target = execution.target(target_id) if target_id else None
    concurrency_group = (
        (target.concurrency_group if target else "")
        or profile.concurrency_group
        or profile.candidate_id
    )
    return ExecutionIdentity(
        candidate_id=profile.candidate_id,
        runtime_id=runtime.id,
        runtime_backend="gateway",
        model_route_id=route.id if route else "",
        model_provider=route.provider if route else "",
        model=route.model if route else "",
        target_id=target_id,
        target_kind=target.kind.value if target else "",
        concurrency_group=concurrency_group,
        legacy_provider_id=profile.provider_id,
    )


def _attach_profile_scheduler_policy(candidate: Any, profile: Any) -> None:
    setattr(candidate, "_execraft_capability_weight", profile.capability_weight)
    setattr(candidate, "_execraft_capability_weights", dict(profile.capability_weights))
    setattr(candidate, "_execraft_max_complexity", profile.max_complexity)
    setattr(
        candidate,
        "_execraft_max_complexity_by_capability",
        dict(profile.max_complexity_by_capability),
    )
    setattr(
        candidate,
        "_execraft_concurrency_group",
        candidate.execution_identity.concurrency_group,
    )


def _managed_openclaw_workspaces(
    execution: ExecutionArchitectureConfig, runtime_id: str, *, state_root: Path
) -> dict[str, Path]:
    runtime = execution.runtime(runtime_id)
    if runtime.openclaw is None or runtime.openclaw.mode != OpenClawMode.MANAGED:
        return {}
    return {
        profile.id: openclaw_profile_workspace(
            state_root, runtime_id=runtime_id, candidate_id=profile.id
        )
        for profile in execution.agents
        if profile.enabled and profile.runtime_id == runtime_id
    }


def _priority(candidate: Any, execution: ExecutionArchitectureConfig) -> int:
    return execution.agent(candidate.candidate_id).priority
