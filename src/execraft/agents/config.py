"""Public agent/execution configuration facade.

Schema v1-v3 remain fully supported through the legacy provider parser. Schema
v4 introduces runtime/model-route/execution-target separation while projecting
Native profiles back to ``AgentProviderConfig`` so the current execution engine
continues to behave identically during the staged migration.
"""

from __future__ import annotations

from typing import Any, Mapping

from execraft.agents.config_errors import AgentConfigError
from execraft.agents.execution_compat import (
    normalize_legacy_agent_configs,
    project_native_legacy_configs,
    project_native_profile_legacy_config,
)
from execraft.agents.execution_config import (
    ExecutionArchitectureConfig,
    parse_v4_execution_config,
)
from execraft.agents.legacy_config import AgentProviderConfig, parse_legacy_agent_configs
from execraft.runtime_config import RuntimeKind


_SUPPORTED_SCHEMA_VERSIONS = frozenset({1, 2, 3, 4})


def agent_config_schema_version(raw_config: Mapping[str, Any]) -> int:
    """Return and validate the public ``agents.yaml`` schema version."""

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


def parse_execution_config(
    raw_config: Mapping[str, Any],
    *,
    read_only: bool = False,
    include_disabled: bool = False,
) -> ExecutionArchitectureConfig:
    """Parse any supported schema into the runtime-neutral v4 domain model."""

    version = agent_config_schema_version(raw_config)
    if version == 4:
        return parse_v4_execution_config(
            raw_config,
            read_only=read_only,
            include_disabled=include_disabled,
        )
    legacy = parse_legacy_agent_configs(
        raw_config,
        read_only=read_only,
        include_disabled=include_disabled,
    )
    return normalize_legacy_agent_configs(legacy, source_schema_version=version)


def parse_agent_configs(
    raw_config: Mapping[str, Any],
    *,
    read_only: bool = False,
    include_disabled: bool = False,
) -> list[AgentProviderConfig]:
    """Parse agents for the current Native/provider-shaped execution engine.

    For schemas v1-v3 this is the historical parser. Schema v4 is parsed through
    the normalized model and then projected onto the same legacy adapter
    contract. OpenClaw profiles execute through the runtime-aware registration
    path; this provider-only compatibility API intentionally cannot project them.
    """

    version = agent_config_schema_version(raw_config)
    if version < 4:
        return parse_legacy_agent_configs(
            raw_config,
            read_only=read_only,
            include_disabled=include_disabled,
        )
    normalized = parse_v4_execution_config(
        raw_config,
        read_only=read_only,
        include_disabled=include_disabled,
    )
    return project_native_legacy_configs(normalized)


__all__ = [
    "AgentConfigError",
    "AgentProviderConfig",
    "ExecutionArchitectureConfig",
    "agent_config_schema_version",
    "parse_agent_configs",
    "parse_execution_config",
]


def project_native_agent_configs(
    mapping: Mapping[str, Any],
) -> tuple[list[AgentProviderConfig], str]:
    """Project the Native profiles of a possibly mixed-runtime configuration.

    ``parse_agent_configs`` fails closed on any non-Native runtime, which would
    make every ``execraft agents`` subcommand unusable for a project that
    configures one. Projecting profile-by-profile keeps the command working.

    Returns the projected Native configs and, when some candidates could not be
    described in the Native provider vocabulary, an operator note naming them --
    so they are never silently missing from the listing.
    """

    execution = parse_execution_config(mapping, include_disabled=True)
    configs: list[AgentProviderConfig] = []
    other: list[str] = []
    for profile in execution.agents:
        runtime = execution.runtime(profile.runtime_id)
        if runtime.kind == RuntimeKind.NATIVE:
            configs.append(project_native_profile_legacy_config(execution, profile))
        else:
            other.append(f"{profile.candidate_id} [{runtime.kind.value}]")
    if not other:
        return configs, ""
    return configs, (
        "note: not described here (non-Native runtime): "
        + ", ".join(other)
        + "\n      inspect these with 'execraft openclaw doctor'."
    )
