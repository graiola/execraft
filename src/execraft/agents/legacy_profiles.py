"""Schema-v3 provider profile inheritance and fragment validation."""

from __future__ import annotations

from typing import Any, Mapping

from execraft.agents.adapter_names import SUPPORTED_NATIVE_ADAPTERS
from execraft.agents.config_errors import AgentConfigError
from execraft.agents.effort import normalize_effort
from execraft.orchestrate.scheduler import AgentCapability


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge mappings; scalar and list values replace defaults."""

    result = {str(key): value for key, value in base.items()}
    for key_value, value in override.items():
        key = str(key_value)
        previous = result.get(key)
        if isinstance(previous, Mapping) and isinstance(value, Mapping):
            result[key] = _deep_merge(previous, value)
        else:
            result[key] = value
    return result


def _normalized_named_mappings(raw: object, *, label: str) -> dict[str, Mapping[str, Any]]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise AgentConfigError(f"agents.yaml {label} must be a mapping")
    result: dict[str, Mapping[str, Any]] = {}
    for raw_name, value in raw.items():
        name = str(raw_name).strip()
        if not name or not name.replace("-", "_").isidentifier():
            raise AgentConfigError(f"invalid agent {label[:-1]} name: {name!r}")
        if name in result:
            raise AgentConfigError(f"duplicate agent {label[:-1]} name: {name}")
        if not isinstance(value, Mapping):
            raise AgentConfigError(f"agent {label[:-1]} {name!r} must be a mapping")
        result[name] = value
    return result


def _extends_value(mapping: Mapping[str, Any], *, label: str) -> str:
    value = mapping.get("extends", "")
    if not isinstance(value, str):
        raise AgentConfigError(f"{label} extends must be a string")
    return value.strip()


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
        raise AgentConfigError(f"{key} must be an integer for provider {provider_name!r}")
    try:
        value = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise AgentConfigError(f"{key} must be an integer for provider {provider_name!r}") from exc
    if minimum is not None and value < minimum:
        qualifier = "positive" if minimum == 1 and maximum is None else f">= {minimum}"
        raise AgentConfigError(f"{key} must be {qualifier} for provider {provider_name!r}")
    if maximum is not None and value > maximum:
        if minimum is not None:
            raise AgentConfigError(
                f"{key} must be between {minimum} and {maximum} for provider {provider_name!r}"
            )
        raise AgentConfigError(f"{key} must be <= {maximum} for provider {provider_name!r}")
    return value


def _declared_capabilities(
    value: Mapping[str, Any], *, label: str
) -> set[AgentCapability]:
    declared: set[AgentCapability] = set()
    for raw_capability in value.get("capabilities", []):
        try:
            declared.add(AgentCapability(str(raw_capability)))
        except ValueError as exc:
            raise AgentConfigError(
                f"unsupported agent capability for {label}: {raw_capability!r}"
            ) from exc
    return declared


def _validate_bounded_capability_mappings(
    value: Mapping[str, Any],
    *,
    label: str,
    declared: set[AgentCapability],
) -> None:
    for key, (minimum, maximum) in {
        "capability_weights": (1, 100),
        "max_complexity_by_capability": (0, 100),
    }.items():
        raw_mapping = value.get(key, {})
        if not isinstance(raw_mapping, Mapping):
            raise AgentConfigError(f"{key} must be a mapping for {label}")
        for raw_capability, raw_number in raw_mapping.items():
            try:
                capability = AgentCapability(str(raw_capability))
            except ValueError as exc:
                raise AgentConfigError(
                    f"unsupported {key} key for {label}: {raw_capability!r}"
                ) from exc
            if declared and capability not in declared:
                raise AgentConfigError(
                    f"{key} for {label} configures {capability.value!r}, "
                    "but that capability is not declared"
                )
            if isinstance(raw_number, bool):
                raise AgentConfigError(
                    f"{key} for {label}/{capability.value} must be an integer"
                )
            try:
                number = int(raw_number)
            except (TypeError, ValueError) as exc:
                raise AgentConfigError(
                    f"{key} for {label}/{capability.value} must be an integer"
                ) from exc
            if number < minimum or number > maximum:
                raise AgentConfigError(
                    f"{key} for {label}/{capability.value} must be between "
                    f"{minimum} and {maximum}"
                )


def _validate_agent_and_effort_mappings(
    value: Mapping[str, Any],
    *,
    label: str,
    declared: set[AgentCapability],
) -> None:
    raw_agents = value.get("agent_by_capability", {})
    if not isinstance(raw_agents, Mapping):
        raise AgentConfigError(f"agent_by_capability must be a mapping for {label}")
    for raw_capability, raw_agent in raw_agents.items():
        try:
            capability = AgentCapability(str(raw_capability))
        except ValueError as exc:
            raise AgentConfigError(
                f"unsupported agent_by_capability key for {label}: {raw_capability!r}"
            ) from exc
        if declared and capability not in declared:
            raise AgentConfigError(
                f"agent_by_capability for {label} configures {capability.value!r}, "
                "but that capability is not declared"
            )
        agent_name = str(raw_agent).strip()
        if not agent_name or not agent_name.replace("-", "_").isidentifier():
            raise AgentConfigError(
                f"invalid agent_by_capability target for {label}: {agent_name!r}"
            )

    try:
        normalize_effort(value.get("effort", ""))
    except ValueError as exc:
        raise AgentConfigError(f"invalid effort for {label}: {exc}") from exc
    raw_efforts = value.get("effort_by_capability", {})
    if not isinstance(raw_efforts, Mapping):
        raise AgentConfigError(f"effort_by_capability must be a mapping for {label}")
    for raw_capability, raw_level in raw_efforts.items():
        try:
            capability = AgentCapability(str(raw_capability))
        except ValueError as exc:
            raise AgentConfigError(
                f"unsupported effort_by_capability key for {label}: {raw_capability!r}"
            ) from exc
        if declared and capability not in declared:
            raise AgentConfigError(
                f"effort_by_capability for {label} configures {capability.value!r}, "
                "but that capability is not declared"
            )
        try:
            normalize_effort(raw_level)
        except ValueError as exc:
            raise AgentConfigError(
                f"invalid effort for {label}/{capability.value}: {exc}"
            ) from exc


def _validate_profile_fragment(profile_id: str, value: Mapping[str, Any]) -> None:
    """Validate fields that can be checked before a profile becomes concrete."""

    label = f"profile {profile_id!r}"
    normalized_adapter = ""
    adapter = value.get("adapter")
    if adapter is not None:
        normalized_adapter = str(adapter).strip().lower()
        if normalized_adapter not in SUPPORTED_NATIVE_ADAPTERS:
            raise AgentConfigError(
                f"unsupported agent adapter {normalized_adapter!r} for {label}"
            )
    for key in (
        "enabled",
        "live_sessions",
        "auto_approve",
        "dangerously_skip_permissions",
        "sandbox_enabled",
    ):
        if key in value and not isinstance(value[key], bool):
            raise AgentConfigError(f"{key} must be a boolean for {label}")

    integer_bounds = {
        "timeout_seconds": (1, None),
        "inactivity_timeout_seconds": (0, None),
        "output_silence_timeout_seconds": (0, None),
        "first_output_timeout_seconds": (0, None),
        "max_output_bytes": (1024, None),
        "max_internal_retry_delay_seconds": (0, None),
        "priority": (None, None),
        "capability_weight": (1, 100),
        "max_complexity": (0, 100),
    }
    parsed_integers: dict[str, int] = {}
    for key, (minimum, maximum) in integer_bounds.items():
        if key in value:
            parsed_integers[key] = _integer_value(
                value,
                key,
                default=0,
                provider_name=label,
                minimum=minimum,
                maximum=maximum,
            )
    timeout = parsed_integers.get("timeout_seconds")
    for key in ("output_silence_timeout_seconds", "first_output_timeout_seconds"):
        bounded = parsed_integers.get(key)
        if bounded and timeout is not None and bounded > timeout:
            raise AgentConfigError(f"{key} cannot exceed timeout_seconds for {label}")

    for key in ("aliases", "capabilities", "policy_paths"):
        if key in value and not isinstance(value[key], list):
            raise AgentConfigError(f"{key} must be a list for {label}")
    for key in ("aliases", "policy_paths"):
        for item in value.get(key, []):
            if not str(item).strip():
                raise AgentConfigError(f"{key} cannot contain empty entries for {label}")

    declared = _declared_capabilities(value, label=label)
    model = value.get("model")
    if normalized_adapter == "opencode" and model is not None:
        provider, separator, model_id = str(model).strip().partition("/")
        if not separator or not provider or not model_id:
            raise AgentConfigError(
                f"OpenCode model for {label} must use provider/model format"
            )
    _validate_bounded_capability_mappings(value, label=label, declared=declared)
    _validate_agent_and_effort_mappings(value, label=label, declared=declared)


def resolve_provider_mappings(
    raw_config: Mapping[str, Any], *, schema_version: int
) -> list[tuple[str, Mapping[str, Any]]]:
    """Resolve reusable provider profiles with cycle/reference validation."""

    profiles = _normalized_named_mappings(raw_config.get("profiles"), label="profiles")
    providers = _normalized_named_mappings(raw_config.get("providers"), label="providers")
    if profiles and schema_version < 3:
        raise AgentConfigError("agents.yaml profiles require schema_version 3")

    resolved: dict[str, dict[str, Any]] = {}
    resolving: list[str] = []

    def resolve(profile_id: str) -> dict[str, Any]:
        if profile_id in resolved:
            return dict(resolved[profile_id])
        if profile_id in resolving:
            cycle = " -> ".join(resolving + [profile_id])
            raise AgentConfigError(f"agent profile inheritance cycle: {cycle}")
        raw_profile = profiles.get(profile_id)
        if raw_profile is None:
            raise AgentConfigError(f"unknown agent profile: {profile_id}")
        resolving.append(profile_id)
        parent_id = _extends_value(raw_profile, label=f"agent profile {profile_id!r}")
        parent = resolve(parent_id) if parent_id else {}
        own = {key: value for key, value in raw_profile.items() if str(key) != "extends"}
        merged = _deep_merge(parent, own)
        resolving.pop()
        resolved[profile_id] = merged
        return dict(merged)

    for profile_id in profiles:
        _validate_profile_fragment(profile_id, resolve(profile_id))

    declarations: list[tuple[str, Mapping[str, Any]]] = []
    for name, raw_provider in providers.items():
        profile_id = _extends_value(raw_provider, label=f"agent provider {name!r}")
        defaults = resolve(profile_id) if profile_id else {}
        own = {key: value for key, value in raw_provider.items() if str(key) != "extends"}
        declarations.append((name, _deep_merge(defaults, own)))
    return declarations
