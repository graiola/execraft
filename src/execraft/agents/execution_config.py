"""Normalized schema-v4 agent execution configuration.

This module is the compatibility boundary between the provider-shaped schema
accepted by Execraft and the runtime/model/target architecture. It deliberately
contains no runtime implementation imports:
configuration can be validated even when OpenClaw is not installed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from execraft.agents.adapter_names import SUPPORTED_NATIVE_ADAPTERS
from execraft.agents.effort import normalize_effort
from execraft.agents.config_errors import AgentConfigError
from execraft.agents.target_config import parse_execution_target
from execraft.runtime.registry import is_registered_runtime_kind
from execraft.agents.profile import AgentExecutionPolicy, AgentProfileConfig
from execraft.agents.openclaw_optimization_config import parse_openclaw_optimization
from execraft.model_routes import ModelRouteConfig
from execraft.orchestrate.scheduler import AgentCapability
from execraft.runtime_config import (
    OpenClawMode,
    OpenClawRuntimeOptions,
    OpenClawVersionPolicy,
    RuntimeConfig,
    RuntimeKind,
)
from execraft.routing_compat import evaluate_route_compatibility
from execraft.targets.config import ExecutionTargetConfig, ExecutionTargetKind


_EXECUTION_TOPOLOGY_KEYS = frozenset(
    {"schema_version", "runtimes", "execution_targets", "model_routes", "agents"}
)
# ``agents.yaml`` historically co-locates orchestration policy with provider
# declarations. Schema v4 owns only execution topology, but those independent
# sections remain valid in the same file so migration does not silently reset
# scheduler, supervisor, or commit behavior. The execution parser validates
# topology and intentionally leaves these sections to their existing owners.
_COLOCATED_ORCHESTRATION_KEYS = frozenset(
    {"scheduling", "supervisor", "commit", "subagents"}
)
_TOP_LEVEL_KEYS = _EXECUTION_TOPOLOGY_KEYS | _COLOCATED_ORCHESTRATION_KEYS
_WRITE_CAPABILITIES = frozenset(
    {AgentCapability.IMPLEMENT, AgentCapability.FIX_REVIEW, AgentCapability.SUPERVISE}
)
_RUNTIME_KEYS = frozenset(
    {
        "kind",
        "adapter",
        "binary",
        "mode",
        "gateway",
        "executable",
        "auth_ref",
        "auth_kind",
        "state_dir",
        "config_path",
        "request_timeout_seconds",
        "handshake_timeout_seconds",
        "startup_timeout_seconds",
        "reconnect_attempts",
        "restart_attempts",
        "version_policy",
        "optimization",
    }
)
_ROUTE_KEYS = frozenset(
    {
        "provider",
        "provider_alias",
        "model",
        "endpoint",
        "credential_ref",
        "api_family",
        "context_window",
        "capabilities",
        "default_target",
    }
)
_AGENT_KEYS = frozenset(
    {
        "name",
        "runtime",
        "model_route",
        "target",
        "enabled",
        "aliases",
        "capabilities",
        "priority",
        "capability_weight",
        "capability_weights",
        "max_complexity",
        "max_complexity_by_capability",
        "concurrency_group",
        "skills",
        "policy",
    }
)
_POLICY_KEYS = frozenset(
    {
        "timeout_seconds",
        "inactivity_timeout_seconds",
        "output_silence_timeout_seconds",
        "first_output_timeout_seconds",
        "max_output_bytes",
        "max_internal_retry_delay_seconds",
        "live_sessions",
        "auto_approve",
        "sandbox",
        "permission_mode",
        "agent_by_capability",
        "format_repair_agent",
        "effort",
        "effort_by_capability",
        "dangerously_skip_permissions",
        "sandbox_enabled",
        "policy_paths",
    }
)


@dataclass(frozen=True)
class ExecutionArchitectureConfig:
    """Normalized execution topology independent from legacy provider objects."""

    agents: tuple[AgentProfileConfig, ...]
    runtimes: tuple[RuntimeConfig, ...]
    model_routes: tuple[ModelRouteConfig, ...]
    targets: tuple[ExecutionTargetConfig, ...]
    source_schema_version: int
    schema_version: int = 4

    def runtime(self, runtime_id: str) -> RuntimeConfig:
        for runtime in self.runtimes:
            if runtime.id == runtime_id:
                return runtime
        raise KeyError(runtime_id)

    def model_route(self, route_id: str) -> ModelRouteConfig:
        for route in self.model_routes:
            if route.id == route_id:
                return route
        raise KeyError(route_id)

    def target(self, target_id: str) -> ExecutionTargetConfig:
        for target in self.targets:
            if target.id == target_id:
                return target
        raise KeyError(target_id)

    def agent(self, candidate_id: str) -> AgentProfileConfig:
        for profile in self.agents:
            if profile.id == candidate_id or candidate_id in profile.aliases:
                return profile
        raise KeyError(candidate_id)

    def as_mapping(self) -> dict[str, object]:
        """Render deterministic schema-v4-shaped data for diagnostics/migration."""

        return {
            "schema_version": self.schema_version,
            "runtimes": {item.id: item.as_mapping() for item in self.runtimes},
            "execution_targets": {
                item.id: item.as_mapping() for item in self.targets
            },
            "model_routes": {
                item.id: item.as_mapping() for item in self.model_routes
            },
            "agents": {item.id: item.as_mapping() for item in self.agents},
        }


def _reject_unknown_fields(
    mapping: Mapping[str, Any], *, allowed: frozenset[str], label: str
) -> None:
    """Reject misspelled v4 fields instead of silently changing semantics."""

    unknown = sorted(str(key) for key in mapping if str(key) not in allowed)
    if unknown:
        raise AgentConfigError(
            f"unsupported field for {label}: {', '.join(unknown)}"
        )


def _valid_id(raw: object, *, label: str) -> str:
    value = str(raw).strip()
    if not value or not value.replace("-", "_").isidentifier():
        raise AgentConfigError(f"invalid {label}: {value!r}")
    return value


def _named_mappings(raw: object, *, label: str) -> list[tuple[str, Mapping[str, Any]]]:
    if raw is None:
        return []
    if not isinstance(raw, Mapping):
        raise AgentConfigError(f"agents.yaml {label} must be a mapping")
    result: list[tuple[str, Mapping[str, Any]]] = []
    for raw_id, raw_value in raw.items():
        item_id = _valid_id(raw_id, label=f"{label[:-1]} ID")
        if not isinstance(raw_value, Mapping):
            raise AgentConfigError(f"{label[:-1]} {item_id!r} must be a mapping")
        result.append((item_id, raw_value))
    return result


def _bool(mapping: Mapping[str, Any], key: str, *, default: bool, label: str) -> bool:
    value = mapping.get(key, default)
    if not isinstance(value, bool):
        raise AgentConfigError(f"{key} must be a boolean for {label}")
    return value


def _int(
    mapping: Mapping[str, Any],
    key: str,
    *,
    default: int,
    label: str,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    raw = mapping.get(key, default)
    if isinstance(raw, bool):
        raise AgentConfigError(f"{key} must be an integer for {label}")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise AgentConfigError(f"{key} must be an integer for {label}") from exc
    if minimum is not None and value < minimum:
        raise AgentConfigError(f"{key} must be >= {minimum} for {label}")
    if maximum is not None and value > maximum:
        raise AgentConfigError(f"{key} must be <= {maximum} for {label}")
    return value


def _string_list(mapping: Mapping[str, Any], key: str, *, label: str) -> tuple[str, ...]:
    raw = mapping.get(key, [])
    if not isinstance(raw, list):
        raise AgentConfigError(f"{key} must be a list for {label}")
    values: list[str] = []
    for item in raw:
        value = str(item).strip()
        if not value:
            raise AgentConfigError(f"{key} cannot contain empty entries for {label}")
        if value not in values:
            values.append(value)
    return tuple(values)


def _capabilities(
    mapping: Mapping[str, Any], *, label: str, read_only: bool
) -> tuple[frozenset[AgentCapability], frozenset[AgentCapability]]:
    raw = mapping.get("capabilities", [])
    if not isinstance(raw, list):
        raise AgentConfigError(f"capabilities must be a list for {label}")
    declared: set[AgentCapability] = set()
    for item in raw:
        try:
            declared.add(AgentCapability(str(item)))
        except ValueError as exc:
            raise AgentConfigError(
                f"unsupported agent capability for {label}: {item!r}"
            ) from exc
    usable = declared - _WRITE_CAPABILITIES if read_only else declared
    if not usable:
        raise AgentConfigError(f"{label} has no usable capabilities")
    return frozenset(declared), frozenset(usable)


def _capability_int_mapping(
    mapping: Mapping[str, Any],
    key: str,
    *,
    label: str,
    declared: frozenset[AgentCapability],
    usable: frozenset[AgentCapability],
    minimum: int,
    maximum: int,
) -> tuple[tuple[AgentCapability, int], ...]:
    raw = mapping.get(key, {})
    if not isinstance(raw, Mapping):
        raise AgentConfigError(f"{key} must be a mapping for {label}")
    result: list[tuple[AgentCapability, int]] = []
    for raw_capability, raw_value in raw.items():
        try:
            capability = AgentCapability(str(raw_capability))
        except ValueError as exc:
            raise AgentConfigError(
                f"unsupported {key} key for {label}: {raw_capability!r}"
            ) from exc
        if capability not in declared:
            raise AgentConfigError(
                f"{key} for {label} configures {capability.value!r}, "
                "but that capability is not declared"
            )
        value = _int(
            {key: raw_value}, key, default=minimum, label=label,
            minimum=minimum, maximum=maximum,
        )
        if capability in usable:
            result.append((capability, value))
    return tuple(result)


def _agent_mapping(
    policy: Mapping[str, Any],
    *,
    label: str,
    declared: frozenset[AgentCapability],
    usable: frozenset[AgentCapability],
) -> tuple[tuple[AgentCapability, str], ...]:
    raw = policy.get("agent_by_capability", {})
    if not isinstance(raw, Mapping):
        raise AgentConfigError(f"agent_by_capability must be a mapping for {label}")
    result: list[tuple[AgentCapability, str]] = []
    for raw_capability, raw_agent in raw.items():
        try:
            capability = AgentCapability(str(raw_capability))
        except ValueError as exc:
            raise AgentConfigError(
                f"unsupported agent_by_capability key for {label}: {raw_capability!r}"
            ) from exc
        if capability not in declared:
            raise AgentConfigError(
                f"agent_by_capability for {label} configures {capability.value!r}, "
                "but that capability is not declared"
            )
        agent = _valid_id(raw_agent, label=f"runtime-native agent for {label}")
        if capability in usable:
            result.append((capability, agent))
    return tuple(result)


def _effort_mapping(
    policy: Mapping[str, Any],
    *,
    label: str,
    declared: frozenset[AgentCapability],
    usable: frozenset[AgentCapability],
) -> tuple[tuple[AgentCapability, str], ...]:
    raw = policy.get("effort_by_capability", {})
    if not isinstance(raw, Mapping):
        raise AgentConfigError(f"effort_by_capability must be a mapping for {label}")
    result: list[tuple[AgentCapability, str]] = []
    for raw_capability, raw_effort in raw.items():
        try:
            capability = AgentCapability(str(raw_capability))
        except ValueError as exc:
            raise AgentConfigError(
                f"unsupported effort_by_capability key for {label}: {raw_capability!r}"
            ) from exc
        if capability not in declared:
            raise AgentConfigError(
                f"effort_by_capability for {label} configures {capability.value!r}, "
                "but that capability is not declared"
            )
        try:
            effort = normalize_effort(raw_effort)
        except ValueError as exc:
            raise AgentConfigError(
                f"invalid effort for {label}/{capability.value}: {exc}"
            ) from exc
        if capability in usable:
            result.append((capability, effort))
    return tuple(result)


def _kind_label(kind: Any) -> str:
    """Render a runtime kind identically for enum and extension kinds."""

    return str(getattr(kind, "value", kind))


def _parse_runtime(runtime_id: str, raw: Mapping[str, Any]) -> RuntimeConfig:
    _reject_unknown_fields(raw, allowed=_RUNTIME_KEYS, label=f"runtime {runtime_id!r}")
    raw_kind = str(raw.get("kind", "")).strip().lower()
    try:
        kind: RuntimeKind | str = RuntimeKind(raw_kind)
    except ValueError as exc:
        # A runtime kind contributed by an extension is configurable once
        # its builder is registered, so adding a runtime needs registration and
        # configuration rather than an edit to this enum. Built-in kinds keep
        # their enum identity, and because RuntimeKind subclasses str every
        # downstream ``kind == RuntimeKind.NATIVE`` comparison is unaffected.
        if not is_registered_runtime_kind(raw_kind):
            raise AgentConfigError(
                f"unsupported runtime kind for {runtime_id!r}: {raw.get('kind')!r}"
            ) from exc
        kind = raw_kind
    adapter = str(raw.get("adapter", "")).strip().lower()
    binary = str(raw.get("binary", "")).strip()
    if kind == RuntimeKind.NATIVE:
        if adapter not in SUPPORTED_NATIVE_ADAPTERS:
            raise AgentConfigError(
                f"native runtime {runtime_id!r} requires a supported adapter"
            )
        if not binary:
            if adapter in {"claude", "claude-code"}:
                binary = "claude"
            elif adapter in {"antigravity", "antigravity-cli"}:
                binary = "agy"
            else:
                binary = adapter
    elif adapter or binary:
        raise AgentConfigError(
            f"{_kind_label(kind)} runtime {runtime_id!r} cannot declare "
            "native adapter/binary fields"
        )
    if kind != RuntimeKind.OPENCLAW:
        # Native and extension runtimes both own no OpenClaw policy. Rejecting
        # those fields here keeps Gateway settings from silently attaching to a
        # runtime that will never read them.
        openclaw = None
        openclaw_only = _RUNTIME_KEYS - {"kind", "adapter", "binary"}
        unexpected = sorted(key for key in openclaw_only if key in raw)
        if unexpected:
            raise AgentConfigError(
                f"{_kind_label(kind)} runtime {runtime_id!r} cannot declare OpenClaw fields: "
                + ", ".join(unexpected)
            )
    else:
        try:
            mode = OpenClawMode(str(raw.get("mode", "managed")).strip().lower())
            version_policy = OpenClawVersionPolicy(
                str(raw.get("version_policy", "pinned-compatible")).strip().lower()
            )
        except ValueError as exc:
            raise AgentConfigError(
                f"invalid OpenClaw runtime policy for {runtime_id!r}: {exc}"
            ) from exc
        gateway = str(raw.get("gateway", "ws://127.0.0.1:18789")).strip()
        if not gateway.startswith(("ws://", "wss://")):
            raise AgentConfigError(
                f"OpenClaw runtime {runtime_id!r} gateway must use ws:// or wss://"
            )
        auth_kind = str(raw.get("auth_kind", "token")).strip().lower() or "token"
        auth_ref = str(raw.get("auth_ref", "")).strip()
        try:
            openclaw = OpenClawRuntimeOptions(
                mode=mode,
                gateway=gateway,
                executable=str(raw.get("executable", "openclaw")).strip() or "openclaw",
                auth_ref=auth_ref,
                auth_kind=auth_kind,
                state_dir=str(raw.get("state_dir", "")).strip(),
                config_path=str(raw.get("config_path", "")).strip(),
                request_timeout_seconds=_int(
                    raw, "request_timeout_seconds", default=30,
                    label=f"OpenClaw runtime {runtime_id!r}", minimum=1
                ),
                handshake_timeout_seconds=_int(
                    raw, "handshake_timeout_seconds", default=15,
                    label=f"OpenClaw runtime {runtime_id!r}", minimum=1
                ),
                startup_timeout_seconds=_int(
                    raw, "startup_timeout_seconds", default=60,
                    label=f"OpenClaw runtime {runtime_id!r}", minimum=1
                ),
                reconnect_attempts=_int(
                    raw, "reconnect_attempts", default=3,
                    label=f"OpenClaw runtime {runtime_id!r}", minimum=0
                ),
                restart_attempts=_int(
                    raw, "restart_attempts", default=1,
                    label=f"OpenClaw runtime {runtime_id!r}", minimum=0
                ),
                version_policy=version_policy,
                optimization=parse_openclaw_optimization(
                    raw.get("optimization"), runtime_id=runtime_id
                ),
            )
        except ValueError as exc:
            raise AgentConfigError(str(exc)) from exc
    return RuntimeConfig(
        id=runtime_id, kind=kind, adapter=adapter, binary=binary, openclaw=openclaw
    )


def _parse_target(target_id: str, raw: Mapping[str, Any]) -> ExecutionTargetConfig:
    return parse_execution_target(target_id, raw)

def _parse_route(route_id: str, raw: Mapping[str, Any]) -> ModelRouteConfig:
    _reject_unknown_fields(raw, allowed=_ROUTE_KEYS, label=f"model route {route_id!r}")
    provider = str(raw.get("provider", "")).strip()
    model = str(raw.get("model", "")).strip()
    if not provider:
        raise AgentConfigError(f"model route {route_id!r} requires provider")
    if not model:
        raise AgentConfigError(f"model route {route_id!r} requires model")
    raw_capabilities = raw.get("capabilities", [])
    if not isinstance(raw_capabilities, list):
        raise AgentConfigError(f"capabilities must be a list for model route {route_id!r}")
    capabilities = frozenset(
        value for value in (str(item).strip() for item in raw_capabilities) if value
    )
    context_window: int | None = None
    if "context_window" in raw:
        context_window = _int(
            raw, "context_window", default=0, label=f"model route {route_id!r}", minimum=1
        )
    return ModelRouteConfig(
        id=route_id,
        provider=provider,
        provider_alias=str(raw.get("provider_alias", "")).strip(),
        model=model,
        endpoint=str(raw.get("endpoint", "")).strip(),
        credential_ref=str(raw.get("credential_ref", "")).strip(),
        api_family=str(raw.get("api_family", "")).strip(),
        context_window=context_window,
        capabilities=capabilities,
        default_target=str(raw.get("default_target", "")).strip(),
    )


def _parse_policy(
    raw: object,
    *,
    label: str,
    runtime: RuntimeConfig,
    declared: frozenset[AgentCapability],
    usable: frozenset[AgentCapability],
) -> AgentExecutionPolicy:
    if raw is None:
        policy: Mapping[str, Any] = {}
    elif isinstance(raw, Mapping):
        policy = raw
    else:
        raise AgentConfigError(f"policy must be a mapping for {label}")
    unknown = sorted(str(key) for key in policy if str(key) not in _POLICY_KEYS)
    if unknown:
        raise AgentConfigError(
            f"unsupported policy field for {label}: {', '.join(unknown)}"
        )

    timeout = _int(policy, "timeout_seconds", default=1800, label=label, minimum=1)
    output_silence = _int(
        policy, "output_silence_timeout_seconds", default=0, label=label, minimum=0
    )
    first_output = _int(
        policy, "first_output_timeout_seconds", default=0, label=label, minimum=0
    )
    if output_silence and output_silence > timeout:
        raise AgentConfigError(
            f"output_silence_timeout_seconds cannot exceed timeout_seconds for {label}"
        )
    if first_output and first_output > timeout:
        raise AgentConfigError(
            f"first_output_timeout_seconds cannot exceed timeout_seconds for {label}"
        )
    agent_mapping = _agent_mapping(
        policy, label=label, declared=declared, usable=usable
    )
    format_repair_agent = str(policy.get("format_repair_agent", "")).strip()
    if format_repair_agent:
        _valid_id(format_repair_agent, label=f"format repair agent for {label}")
    if (agent_mapping or format_repair_agent) and not (
        runtime.kind == RuntimeKind.NATIVE and runtime.adapter == "opencode"
    ):
        raise AgentConfigError(
            f"agent_by_capability/format_repair_agent are supported only by "
            f"Native OpenCode runtime profiles: {label}"
        )
    try:
        effort = normalize_effort(policy.get("effort", ""))
    except ValueError as exc:
        raise AgentConfigError(f"invalid effort for {label}: {exc}") from exc

    return AgentExecutionPolicy(
        timeout_seconds=timeout,
        inactivity_timeout_seconds=_int(
            policy, "inactivity_timeout_seconds", default=900, label=label, minimum=0
        ),
        output_silence_timeout_seconds=output_silence,
        first_output_timeout_seconds=first_output,
        max_output_bytes=_int(
            policy, "max_output_bytes", default=64 * 1024 * 1024,
            label=label, minimum=1024,
        ),
        max_internal_retry_delay_seconds=_int(
            policy, "max_internal_retry_delay_seconds", default=120,
            label=label, minimum=0,
        ),
        live_sessions=_bool(policy, "live_sessions", default=True, label=label),
        auto_approve=_bool(policy, "auto_approve", default=False, label=label),
        sandbox=str(policy.get("sandbox", "workspace-write")),
        permission_mode=str(policy.get("permission_mode", "acceptEdits")),
        agent_by_capability=agent_mapping,
        format_repair_agent=format_repair_agent,
        effort=effort,
        effort_by_capability=_effort_mapping(
            policy, label=label, declared=declared, usable=usable
        ),
        dangerously_skip_permissions=_bool(
            policy, "dangerously_skip_permissions", default=False, label=label
        ),
        sandbox_enabled=_bool(policy, "sandbox_enabled", default=False, label=label),
        policy_paths=_string_list(policy, "policy_paths", label=label),
    )


def _parse_agent(
    agent_id: str,
    raw: Mapping[str, Any],
    *,
    runtimes: Mapping[str, RuntimeConfig],
    routes: Mapping[str, ModelRouteConfig],
    targets: Mapping[str, ExecutionTargetConfig],
    read_only: bool,
    seen_ids: set[str],
) -> AgentProfileConfig:
    label = f"agent {agent_id!r}"
    _reject_unknown_fields(raw, allowed=_AGENT_KEYS, label=label)
    runtime_id = str(raw.get("runtime", "")).strip()
    if runtime_id not in runtimes:
        raise AgentConfigError(f"{label} references unknown runtime {runtime_id!r}")
    runtime = runtimes[runtime_id]
    model_route_id = str(raw.get("model_route", "")).strip()
    if model_route_id and model_route_id not in routes:
        raise AgentConfigError(
            f"{label} references unknown model route {model_route_id!r}"
        )
    if runtime.kind == RuntimeKind.NATIVE and runtime.adapter == "opencode" and not model_route_id:
        raise AgentConfigError(f"{label} using Native OpenCode requires model_route")
    target_id = str(raw.get("target", "")).strip()
    if target_id and target_id not in targets:
        raise AgentConfigError(f"{label} references unknown execution target {target_id!r}")
    if not target_id and model_route_id:
        target_id = routes[model_route_id].default_target
    if model_route_id:
        target = targets.get(target_id) if target_id else None
        compatibility = evaluate_route_compatibility(
            runtime, routes[model_route_id], target
        )
        if not compatibility.compatible:
            raise AgentConfigError(
                f"{label} has incompatible runtime/model route/target: "
                f"{compatibility.reason}"
            )

    aliases = _string_list(raw, "aliases", label=label)
    for alias in (agent_id, *aliases):
        if alias in seen_ids:
            raise AgentConfigError(f"duplicate agent ID or alias: {alias}")
    seen_ids.update((agent_id, *aliases))

    declared, usable = _capabilities(raw, label=label, read_only=read_only)
    policy = _parse_policy(
        raw.get("policy"),
        label=label,
        runtime=runtime,
        declared=declared,
        usable=usable,
    )
    capability_weight = _int(
        raw, "capability_weight", default=50, label=label, minimum=1, maximum=100
    )
    max_complexity = _int(
        raw, "max_complexity", default=100, label=label, minimum=0, maximum=100
    )
    concurrency_group = str(raw.get("concurrency_group", "")).strip()
    if not concurrency_group and target_id:
        concurrency_group = targets[target_id].concurrency_group
    if not concurrency_group:
        concurrency_group = agent_id

    return AgentProfileConfig(
        id=agent_id,
        name=str(raw.get("name", agent_id)).strip() or agent_id,
        enabled=_bool(raw, "enabled", default=True, label=label),
        aliases=aliases,
        capabilities=usable,
        runtime_id=runtime_id,
        model_route_id=model_route_id,
        target_id=target_id,
        priority=_int(raw, "priority", default=0, label=label),
        capability_weight=capability_weight,
        capability_weights=_capability_int_mapping(
            raw, "capability_weights", label=label, declared=declared,
            usable=usable, minimum=1, maximum=100,
        ),
        max_complexity=max_complexity,
        max_complexity_by_capability=_capability_int_mapping(
            raw, "max_complexity_by_capability", label=label, declared=declared,
            usable=usable, minimum=0, maximum=100,
        ),
        concurrency_group=concurrency_group,
        skill_set=_string_list(raw, "skills", label=label),
        policy=policy,
    )


def parse_v4_execution_config(
    raw_config: Mapping[str, Any],
    *,
    read_only: bool = False,
    include_disabled: bool = False,
) -> ExecutionArchitectureConfig:
    """Parse schema v4 without importing or starting any concrete runtime."""

    _reject_unknown_fields(
        raw_config, allowed=_TOP_LEVEL_KEYS, label="agents.yaml schema v4"
    )
    runtimes = tuple(
        _parse_runtime(item_id, raw)
        for item_id, raw in _named_mappings(raw_config.get("runtimes"), label="runtimes")
    )
    targets = tuple(
        _parse_target(item_id, raw)
        for item_id, raw in _named_mappings(
            raw_config.get("execution_targets"), label="execution_targets"
        )
    )
    routes = tuple(
        _parse_route(item_id, raw)
        for item_id, raw in _named_mappings(
            raw_config.get("model_routes"), label="model_routes"
        )
    )
    runtime_by_id = {item.id: item for item in runtimes}
    target_by_id = {item.id: item for item in targets}
    route_by_id = {item.id: item for item in routes}
    for route in routes:
        if route.default_target and route.default_target not in target_by_id:
            raise AgentConfigError(
                f"model route {route.id!r} references unknown default target "
                f"{route.default_target!r}"
            )

    seen_ids: set[str] = set()
    parsed_agents = [
        _parse_agent(
            item_id,
            raw,
            runtimes=runtime_by_id,
            routes=route_by_id,
            targets=target_by_id,
            read_only=read_only,
            seen_ids=seen_ids,
        )
        for item_id, raw in _named_mappings(raw_config.get("agents"), label="agents")
    ]
    agents = tuple(
        item for item in parsed_agents if item.enabled or include_disabled
    )
    return ExecutionArchitectureConfig(
        agents=agents,
        runtimes=runtimes,
        model_routes=routes,
        targets=targets,
        source_schema_version=4,
    )
