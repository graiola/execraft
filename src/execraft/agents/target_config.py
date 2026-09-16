"""Schema-v4 execution-target parsing kept separate from agent profile parsing."""

from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import urlsplit

from execraft.agents.config_errors import AgentConfigError
from execraft.targets.config import (
    ExecutionTargetConfig,
    ExecutionTargetKind,
    RemoteRuntimeEnvironmentConfig,
)

_KEYS = frozenset(
    {
        "kind",
        "endpoint",
        "concurrency_group",
        "workspace_transport",
        "max_concurrency",
        "environment",
    }
)
_ENV_KEYS = frozenset({"toolchains", "tools", "gpu", "models", "sandbox"})
_REMOTE_ONLY = frozenset({"workspace_transport", "max_concurrency", "environment"})


def parse_execution_target(
    target_id: str, raw: Mapping[str, Any]
) -> ExecutionTargetConfig:
    unknown = sorted(str(key) for key in raw if str(key) not in _KEYS)
    if unknown:
        raise AgentConfigError(
            f"unsupported field for execution target {target_id!r}: {', '.join(unknown)}"
        )
    try:
        kind = ExecutionTargetKind(str(raw.get("kind", "")).strip().lower())
    except ValueError as exc:
        raise AgentConfigError(
            f"unsupported execution target kind for {target_id!r}: {raw.get('kind')!r}"
        ) from exc
    endpoint = str(raw.get("endpoint", "")).strip()
    if kind != ExecutionTargetKind.LOCAL and not endpoint:
        raise AgentConfigError(
            f"execution target {target_id!r} of kind {kind.value!r} requires endpoint"
        )
    concurrency_group = str(raw.get("concurrency_group", target_id)).strip() or target_id

    if kind != ExecutionTargetKind.REMOTE_RUNTIME:
        present = sorted(key for key in _REMOTE_ONLY if key in raw)
        if present:
            raise AgentConfigError(
                f"execution target {target_id!r} uses remote-runtime-only fields: "
                + ", ".join(present)
            )
        return ExecutionTargetConfig(
            id=target_id,
            kind=kind,
            endpoint=endpoint,
            concurrency_group=concurrency_group,
        )

    parsed = urlsplit(endpoint)
    if parsed.scheme.lower() not in {"ws", "wss"} or not parsed.hostname:
        raise AgentConfigError(
            f"remote runtime target {target_id!r} endpoint must be a ws:// or wss:// Gateway URL"
        )
    if parsed.username is not None or parsed.password is not None:
        raise AgentConfigError(
            f"remote runtime target {target_id!r} endpoint must not contain credentials"
        )
    if parsed.query or parsed.fragment:
        raise AgentConfigError(
            f"remote runtime target {target_id!r} endpoint must not contain query/fragment secrets; "
            "use the OpenClaw runtime credential reference"
        )
    workspace_transport = str(raw.get("workspace_transport", "shared")).strip().lower()
    if workspace_transport != "shared":
        raise AgentConfigError(
            f"remote runtime target {target_id!r} supports only workspace_transport 'shared'"
        )
    maximum = _integer(
        raw.get("max_concurrency", 1),
        label=f"remote runtime target {target_id!r} max_concurrency",
    )
    if maximum < 1:
        raise AgentConfigError(
            f"remote runtime target {target_id!r} max_concurrency must be >= 1"
        )
    environment = _environment(raw.get("environment"), target_id=target_id)
    return ExecutionTargetConfig(
        id=target_id,
        kind=kind,
        endpoint=endpoint,
        concurrency_group=concurrency_group,
        workspace_transport=workspace_transport,
        max_concurrency=maximum,
        environment=environment,
    )


def _environment(
    raw: object, *, target_id: str
) -> RemoteRuntimeEnvironmentConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise AgentConfigError(
            f"remote runtime target {target_id!r} environment must be a mapping"
        )
    unknown = sorted(str(key) for key in raw if str(key) not in _ENV_KEYS)
    if unknown:
        raise AgentConfigError(
            f"remote runtime target {target_id!r} environment has unsupported fields: "
            + ", ".join(unknown)
        )
    sandbox = raw.get("sandbox")
    if sandbox is not None and not isinstance(sandbox, bool):
        raise AgentConfigError(
            f"remote runtime target {target_id!r} environment sandbox must be boolean"
        )
    return RemoteRuntimeEnvironmentConfig(
        toolchains=_strings(raw.get("toolchains", []), label="toolchains", target_id=target_id),
        tools=_strings(raw.get("tools", []), label="tools", target_id=target_id),
        gpu=_strings(raw.get("gpu", []), label="gpu", target_id=target_id),
        models=_strings(raw.get("models", []), label="models", target_id=target_id),
        sandbox=sandbox,
    )


def _strings(raw: object, *, label: str, target_id: str) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise AgentConfigError(
            f"remote runtime target {target_id!r} environment {label} must be a list"
        )
    values: list[str] = []
    for item in raw:
        value=str(item).strip()
        if not value:
            raise AgentConfigError(
                f"remote runtime target {target_id!r} environment {label} "
                "contains an empty value"
            )
        if value not in values:
            values.append(value)
    return tuple(values)


def _integer(raw: object, *, label: str) -> int:
    if isinstance(raw, bool):
        raise AgentConfigError(f"{label} must be an integer")
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise AgentConfigError(f"{label} must be an integer") from exc
