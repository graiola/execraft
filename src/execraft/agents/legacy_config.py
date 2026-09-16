"""Legacy schema-v1/v2/v3 agent-provider configuration.

The YAML key identifies one logical agent instance. ``adapter`` selects the
transport implementation, allowing multiple independently tracked instances of
one adapter (for example OpenCode Zen Free and OpenCode Go) without teaching the
orchestrator provider-specific names.

Schema version 3 adds reusable, recursively merged provider profiles. Profiles
reduce repeated satellite/model policy while every concrete provider keeps a
unique identity, model and concurrency group.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from execraft.agents.adapter_names import SUPPORTED_NATIVE_ADAPTERS, default_native_binary
from execraft.agents.config_errors import AgentConfigError
from execraft.agents.effort import normalize_effort
from execraft.agents.legacy_profiles import resolve_provider_mappings
from execraft.orchestrate.scheduler import AgentCapability


_SUPPORTED_SCHEMA_VERSIONS = {1, 2, 3}
_WRITE_CAPABILITIES = {
    AgentCapability.IMPLEMENT,
    AgentCapability.FIX_REVIEW,
    AgentCapability.SUPERVISE,
}


def _schema_version(raw_config: Mapping[str, Any]) -> int:
    raw_value = raw_config.get("schema_version", 1)
    if isinstance(raw_value, bool):
        raise AgentConfigError("agents.yaml schema_version must be an integer")
    try:
        version = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise AgentConfigError("agents.yaml schema_version must be an integer") from exc
    if version not in _SUPPORTED_SCHEMA_VERSIONS:
        raise AgentConfigError(f"unsupported agents.yaml schema_version: {version!r}")
    return version

def _boolean_value(
    mapping: Mapping[str, Any], key: str, *, default: bool, provider_name: str
) -> bool:
    value = mapping.get(key, default)
    if not isinstance(value, bool):
        raise AgentConfigError(
            f"{key} must be a boolean for provider {provider_name!r}"
        )
    return value


def _integer_value(
    mapping: Mapping[str, Any],
    key: str,
    *,
    default: int,
    provider_name: str,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    raw_value = mapping.get(key, default)
    if isinstance(raw_value, bool):
        raise AgentConfigError(
            f"{key} must be an integer for provider {provider_name!r}"
        )
    try:
        value = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise AgentConfigError(
            f"{key} must be an integer for provider {provider_name!r}"
        ) from exc
    if minimum is not None and value < minimum:
        qualifier = "positive" if minimum == 1 and maximum is None else f">= {minimum}"
        raise AgentConfigError(
            f"{key} must be {qualifier} for provider {provider_name!r}"
        )
    if maximum is not None and value > maximum:
        if minimum is not None:
            raise AgentConfigError(
                f"{key} must be between {minimum} and {maximum} "
                f"for provider {provider_name!r}"
            )
        raise AgentConfigError(
            f"{key} must be <= {maximum} for provider {provider_name!r}"
        )
    return value


def _aliases(
    value: Mapping[str, Any],
    *,
    name: str,
    provider_id: str,
    seen_agent_ids: set[str],
) -> tuple[str, ...]:
    raw_aliases = value.get("aliases", [])
    if not isinstance(raw_aliases, list):
        raise AgentConfigError(f"agent aliases must be a list for provider {name!r}")
    aliases: list[str] = []
    for raw_alias in raw_aliases:
        alias = str(raw_alias).strip()
        if not alias:
            raise AgentConfigError(
                f"agent aliases cannot be empty for provider {name!r}"
            )
        if alias == provider_id or alias in seen_agent_ids or alias in aliases:
            raise AgentConfigError(f"duplicate agent ID or alias: {alias}")
        aliases.append(alias)
    return tuple(aliases)


def _capabilities(
    value: Mapping[str, Any], *, name: str, read_only: bool
) -> tuple[set[AgentCapability], frozenset[AgentCapability]]:
    raw_capabilities = value.get("capabilities", [])
    if not isinstance(raw_capabilities, list):
        raise AgentConfigError(
            f"agent capabilities must be a list for provider {name!r}"
        )
    declared: set[AgentCapability] = set()
    for item in raw_capabilities:
        try:
            declared.add(AgentCapability(str(item)))
        except ValueError as exc:
            raise AgentConfigError(
                f"unsupported agent capability for {name!r}: {item!r}"
            ) from exc
    usable = declared - _WRITE_CAPABILITIES if read_only else set(declared)
    if not usable:
        raise AgentConfigError(f"agent provider {name!r} has no usable capabilities")
    return declared, frozenset(usable)


def _model(value: Mapping[str, Any], *, name: str, adapter: str) -> str:
    model = str(value.get("model", "")).strip()
    if adapter != "opencode":
        return model
    if not model:
        raise AgentConfigError(
            f"OpenCode provider {name!r} must declare an explicit model"
        )
    provider, separator, model_id = model.partition("/")
    if not separator or not provider or not model_id:
        raise AgentConfigError(
            f"OpenCode model for {name!r} must use provider/model format"
        )
    return model


def _capability_integer_mapping(
    value: Mapping[str, Any],
    *,
    key: str,
    name: str,
    declared: set[AgentCapability],
    usable: frozenset[AgentCapability],
    minimum: int,
    maximum: int,
    item_label: str,
) -> tuple[tuple[AgentCapability, int], ...]:
    raw_mapping = value.get(key, {})
    if not isinstance(raw_mapping, Mapping):
        raise AgentConfigError(f"{key} must be a mapping for provider {name!r}")
    result: list[tuple[AgentCapability, int]] = []
    for raw_capability, raw_number in raw_mapping.items():
        try:
            capability = AgentCapability(str(raw_capability))
        except ValueError as exc:
            raise AgentConfigError(
                f"unsupported {key} key for {name!r}: {raw_capability!r}"
            ) from exc
        if capability not in declared:
            raise AgentConfigError(
                f"{key} for {name!r} configures {capability.value!r}, "
                "but that capability is not declared"
            )
        if isinstance(raw_number, bool):
            raise AgentConfigError(
                f"{item_label} for {name!r}/{capability.value} must be an integer"
            )
        try:
            number = int(raw_number)
        except (TypeError, ValueError) as exc:
            raise AgentConfigError(
                f"{item_label} for {name!r}/{capability.value} must be an integer"
            ) from exc
        if not minimum <= number <= maximum:
            raise AgentConfigError(
                f"{item_label} for {name!r}/{capability.value} must be between "
                f"{minimum} and {maximum}"
            )
        if capability in usable:
            result.append((capability, number))
    return tuple(result)


def _provider_effort(value: Mapping[str, Any], *, name: str) -> str:
    try:
        return normalize_effort(value.get("effort", ""))
    except ValueError as exc:
        raise AgentConfigError(f"invalid effort for provider {name!r}: {exc}") from exc


def _effort_by_capability(
    value: Mapping[str, Any],
    *,
    name: str,
    declared: set[AgentCapability],
    usable: frozenset[AgentCapability],
) -> tuple[tuple[AgentCapability, str], ...]:
    raw_mapping = value.get("effort_by_capability", {})
    if not isinstance(raw_mapping, Mapping):
        raise AgentConfigError(
            f"effort_by_capability must be a mapping for provider {name!r}"
        )
    result: list[tuple[AgentCapability, str]] = []
    for raw_capability, raw_level in raw_mapping.items():
        try:
            capability = AgentCapability(str(raw_capability))
        except ValueError as exc:
            raise AgentConfigError(
                f"unsupported effort_by_capability key for {name!r}: {raw_capability!r}"
            ) from exc
        if capability not in declared:
            raise AgentConfigError(
                f"effort_by_capability for {name!r} configures {capability.value!r}, "
                "but that capability is not declared"
            )
        try:
            level = normalize_effort(raw_level)
        except ValueError as exc:
            raise AgentConfigError(
                f"invalid effort for {name!r}/{capability.value}: {exc}"
            ) from exc
        if not level:
            raise AgentConfigError(
                f"effort for {name!r}/{capability.value} cannot be empty; omit the "
                "key to inherit the provider default"
            )
        if capability in usable:
            result.append((capability, level))
    return tuple(result)


def _agent_by_capability(
    value: Mapping[str, Any],
    *,
    name: str,
    adapter: str,
    declared: set[AgentCapability],
    usable: frozenset[AgentCapability],
) -> tuple[tuple[AgentCapability, str], ...]:
    raw_mapping = value.get("agent_by_capability", {})
    if not isinstance(raw_mapping, Mapping):
        raise AgentConfigError(
            f"agent_by_capability must be a mapping for provider {name!r}"
        )
    result: list[tuple[AgentCapability, str]] = []
    for raw_capability, raw_agent in raw_mapping.items():
        try:
            capability = AgentCapability(str(raw_capability))
        except ValueError as exc:
            raise AgentConfigError(
                f"unsupported agent_by_capability key for {name!r}: "
                f"{raw_capability!r}"
            ) from exc
        if capability not in declared:
            raise AgentConfigError(
                f"agent_by_capability for {name!r} configures "
                f"{capability.value!r}, but that capability is not declared"
            )
        agent_name = str(raw_agent).strip()
        if not agent_name:
            raise AgentConfigError(
                f"agent_by_capability target cannot be empty for {name!r}"
            )
        if not agent_name.replace("-", "_").isidentifier():
            raise AgentConfigError(
                f"invalid provider-native agent name for {name!r}: {agent_name!r}"
            )
        if capability in usable:
            result.append((capability, agent_name))
    if result and adapter != "opencode":
        raise AgentConfigError(
            f"agent_by_capability is supported only by OpenCode providers: {name!r}"
        )
    return tuple(result)


def _format_repair_agent(
    value: Mapping[str, Any], *, name: str, adapter: str
) -> str:
    agent_name = str(value.get("format_repair_agent", "")).strip()
    if not agent_name:
        return ""
    if adapter != "opencode":
        raise AgentConfigError(
            f"format_repair_agent is supported only by OpenCode providers: {name!r}"
        )
    if not agent_name.replace("-", "_").isidentifier():
        raise AgentConfigError(
            f"invalid format repair agent name for {name!r}: {agent_name!r}"
        )
    return agent_name


def _policy_paths(value: Mapping[str, Any], *, name: str) -> tuple[str, ...]:
    raw_paths = value.get("policy_paths", [])
    if not isinstance(raw_paths, list):
        raise AgentConfigError(f"policy_paths must be a list for provider {name!r}")
    paths = tuple(str(item).strip() for item in raw_paths)
    if any(not item for item in paths):
        raise AgentConfigError(
            f"policy_paths cannot contain empty entries for provider {name!r}"
        )
    return paths


@dataclass(frozen=True)
class AgentProviderConfig:
    name: str
    adapter: str
    enabled: bool
    provider_id: str
    binary: str
    capabilities: frozenset[AgentCapability]
    model: str = ""
    timeout_seconds: int = 1800
    inactivity_timeout_seconds: int = 900
    output_silence_timeout_seconds: int = 0
    first_output_timeout_seconds: int = 0
    max_output_bytes: int = 64 * 1024 * 1024
    max_internal_retry_delay_seconds: int = 120
    live_sessions: bool = True
    priority: int = 0
    auto_approve: bool = False
    sandbox: str = "workspace-write"
    permission_mode: str = "acceptEdits"
    aliases: tuple[str, ...] = ()
    agent_by_capability: tuple[tuple[AgentCapability, str], ...] = ()
    format_repair_agent: str = ""
    capability_weight: int = 50
    capability_weights: tuple[tuple[AgentCapability, int], ...] = ()
    max_complexity: int = 100
    max_complexity_by_capability: tuple[tuple[AgentCapability, int], ...] = ()
    effort: str = ""
    effort_by_capability: tuple[tuple[AgentCapability, str], ...] = ()
    dangerously_skip_permissions: bool = False
    sandbox_enabled: bool = False
    policy_paths: tuple[str, ...] = ()
    concurrency_group: str = ""
    # Compatibility metadata. Legacy schemas may leave these empty; the
    # canonical model/target registry enriches them at runtime construction.
    candidate_id: str = field(default="", compare=False, repr=False)
    runtime_id: str = field(default="", compare=False, repr=False)
    runtime_backend: str = field(default="", compare=False, repr=False)
    model_route_id: str = field(default="", compare=False, repr=False)
    model_provider_semantic: str = field(default="", compare=False, repr=False)
    target_id: str = field(default="", compare=False, repr=False)
    target_kind: str = field(default="", compare=False, repr=False)
    target_concurrency_group: str = field(default="", compare=False, repr=False)

    def agent_for_capability(self, capability: AgentCapability) -> str:
        """Return the configured provider-native agent for *capability*."""
        return dict(self.agent_by_capability).get(capability, "")

    def weight_for_capability(self, capability: AgentCapability) -> int:
        """Return the scheduler strength score for one capability."""
        return dict(self.capability_weights).get(capability, self.capability_weight)

    def max_complexity_for(self, capability: AgentCapability) -> int:
        """Return the absolute task-complexity ceiling for one capability."""
        return dict(self.max_complexity_by_capability).get(
            capability, self.max_complexity
        )

    def effort_for_capability(self, capability: AgentCapability) -> str:
        """Return the reasoning-effort hint for one capability.

        An empty result means "leave the provider default alone", which is not
        the same as ``low`` — the adapter omits the flag entirely.
        """
        return dict(self.effort_by_capability).get(capability, self.effort)

    @property
    def model_provider(self) -> str:
        if "/" not in self.model:
            return ""
        return self.model.split("/", 1)[0]


def _runtime_limits(value: Mapping[str, Any], *, name: str) -> dict[str, int]:
    timeout = _integer_value(
        value, "timeout_seconds", default=1800, provider_name=name, minimum=1
    )
    output_silence = _integer_value(
        value,
        "output_silence_timeout_seconds",
        default=0,
        provider_name=name,
        minimum=0,
    )
    first_output = _integer_value(
        value,
        "first_output_timeout_seconds",
        default=0,
        provider_name=name,
        minimum=0,
    )
    if output_silence and output_silence > timeout:
        raise AgentConfigError(
            "output_silence_timeout_seconds cannot exceed timeout_seconds "
            f"for provider {name!r}"
        )
    if first_output and first_output > timeout:
        raise AgentConfigError(
            "first_output_timeout_seconds cannot exceed timeout_seconds "
            f"for provider {name!r}"
        )
    return {
        "timeout_seconds": timeout,
        "inactivity_timeout_seconds": _integer_value(
            value,
            "inactivity_timeout_seconds",
            default=900,
            provider_name=name,
            minimum=0,
        ),
        "output_silence_timeout_seconds": output_silence,
        "first_output_timeout_seconds": first_output,
        "max_output_bytes": _integer_value(
            value,
            "max_output_bytes",
            default=64 * 1024 * 1024,
            provider_name=name,
            minimum=1024,
        ),
        "max_internal_retry_delay_seconds": _integer_value(
            value,
            "max_internal_retry_delay_seconds",
            default=120,
            provider_name=name,
            minimum=0,
        ),
    }


def _scheduler_limits(
    value: Mapping[str, Any],
    *,
    name: str,
    declared: set[AgentCapability],
    usable: frozenset[AgentCapability],
) -> dict[str, Any]:
    return {
        "capability_weight": _integer_value(
            value,
            "capability_weight",
            default=50,
            provider_name=name,
            minimum=1,
            maximum=100,
        ),
        "capability_weights": _capability_integer_mapping(
            value,
            key="capability_weights",
            name=name,
            declared=declared,
            usable=usable,
            minimum=1,
            maximum=100,
            item_label="capability weight",
        ),
        "max_complexity": _integer_value(
            value,
            "max_complexity",
            default=100,
            provider_name=name,
            minimum=0,
            maximum=100,
        ),
        "max_complexity_by_capability": _capability_integer_mapping(
            value,
            key="max_complexity_by_capability",
            name=name,
            declared=declared,
            usable=usable,
            minimum=0,
            maximum=100,
            item_label="maximum complexity",
        ),
    }


def _parse_provider(
    name: str,
    value: Mapping[str, Any],
    *,
    read_only: bool,
    seen_provider_ids: set[str],
    seen_agent_ids: set[str],
) -> AgentProviderConfig:
    enabled = _boolean_value(value, "enabled", default=False, provider_name=name)
    adapter = str(value.get("adapter", name)).strip().lower()
    if adapter not in SUPPORTED_NATIVE_ADAPTERS:
        raise AgentConfigError(
            f"unsupported agent adapter {adapter!r} for provider {name!r}"
        )
    provider_id = str(value.get("provider_id", name)).strip()
    if not provider_id:
        raise AgentConfigError(f"provider_id cannot be empty for {name!r}")
    if provider_id in seen_provider_ids:
        raise AgentConfigError(f"duplicate agent provider_id: {provider_id}")
    if provider_id in seen_agent_ids:
        raise AgentConfigError(f"agent ID or alias is already declared: {provider_id}")
    aliases = _aliases(
        value, name=name, provider_id=provider_id, seen_agent_ids=seen_agent_ids
    )
    declared, usable = _capabilities(value, name=name, read_only=read_only)
    runtime_limits = _runtime_limits(value, name=name)
    scheduler_limits = _scheduler_limits(
        value, name=name, declared=declared, usable=usable
    )
    default_binary = default_native_binary(adapter)
    return AgentProviderConfig(
        name=name,
        adapter=adapter,
        enabled=enabled,
        provider_id=provider_id,
        binary=str(value.get("binary", default_binary)).strip() or default_binary,
        capabilities=usable,
        model=_model(value, name=name, adapter=adapter),
        **runtime_limits,
        live_sessions=_boolean_value(
            value, "live_sessions", default=True, provider_name=name
        ),
        priority=_integer_value(value, "priority", default=0, provider_name=name),
        auto_approve=_boolean_value(
            value, "auto_approve", default=False, provider_name=name
        ),
        sandbox=str(value.get("sandbox", "workspace-write")),
        permission_mode=str(value.get("permission_mode", "acceptEdits")),
        aliases=aliases,
        agent_by_capability=_agent_by_capability(
            value, name=name, adapter=adapter, declared=declared, usable=usable
        ),
        format_repair_agent=_format_repair_agent(value, name=name, adapter=adapter),
        **scheduler_limits,
        effort=_provider_effort(value, name=name),
        effort_by_capability=_effort_by_capability(
            value, name=name, declared=declared, usable=usable
        ),
        dangerously_skip_permissions=_boolean_value(
            value,
            "dangerously_skip_permissions",
            default=False,
            provider_name=name,
        ),
        sandbox_enabled=_boolean_value(
            value, "sandbox_enabled", default=False, provider_name=name
        ),
        policy_paths=_policy_paths(value, name=name),
        concurrency_group=(
            str(value.get("concurrency_group", provider_id)).strip() or provider_id
        ),
    )


def parse_legacy_agent_configs(
    raw_config: Mapping[str, Any],
    *,
    read_only: bool = False,
    include_disabled: bool = False,
) -> list[AgentProviderConfig]:
    """Parse legacy project providers in deterministic priority order.

    All declarations are structurally validated, including disabled providers.
    Runtime callers receive enabled providers only; administrative callers may
    set ``include_disabled`` to inspect or clear stored health for configured
    providers that are currently disabled.
    """

    declarations = resolve_provider_mappings(
        raw_config, schema_version=_schema_version(raw_config)
    )
    configs: list[tuple[int, AgentProviderConfig]] = []
    seen_provider_ids: set[str] = set()
    seen_agent_ids: set[str] = set()
    for declaration_index, (name, value) in enumerate(declarations):
        config = _parse_provider(
            name,
            value,
            read_only=read_only,
            seen_provider_ids=seen_provider_ids,
            seen_agent_ids=seen_agent_ids,
        )
        if config.enabled or include_disabled:
            configs.append((declaration_index, config))
        seen_provider_ids.add(config.provider_id)
        seen_agent_ids.add(config.provider_id)
        seen_agent_ids.update(config.aliases)

    # Higher priority wins; declaration order is the deterministic tie-breaker.
    configs.sort(key=lambda item: (-item[1].priority, item[0]))
    return [item[1] for item in configs]
