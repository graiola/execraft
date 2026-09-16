"""Deterministic projection of Execraft execution topology into OpenClaw config.

:class:`~execraft.agents.execution_config.ExecutionArchitectureConfig` remains
source of truth.  The generated OpenClaw JSON is a disposable runtime artifact:
model/provider/agent entries can always be reconstructed from Execraft state.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from execraft.agents.execution_config import ExecutionArchitectureConfig
from execraft.agents.profile import AgentProfileConfig
from execraft.model_routes import ModelRouteConfig
from execraft.network import is_loopback_host
from execraft.runtime_config import OpenClawMode, RuntimeConfig, RuntimeKind
from execraft.targets.config import ExecutionTargetConfig, ExecutionTargetKind

from .openclaw_security import openclaw_agent_security_entries, openclaw_security_fragment


class OpenClawProjectionError(ValueError):
    """Raised when canonical Execraft routing cannot be represented safely."""


_ENV_REF = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PROVIDER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_BUILTIN_CLOUD_PROVIDERS = frozenset({"anthropic", "openai"})
_API_FAMILY_MAP = {
    "openai-compatible": "openai-completions",
    "openai-completions": "openai-completions",
    "openai-responses": "openai-responses",
    "anthropic-messages": "anthropic-messages",
}


@dataclass(frozen=True)
class OpenClawConfigProjection:
    """One deterministic, secret-safe OpenClaw configuration projection."""

    runtime_id: str
    config: Mapping[str, Any]
    providers: tuple[str, ...]
    models: tuple[str, ...]
    agents: tuple[str, ...]
    credential_refs: tuple[str, ...]

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()

    def to_bytes(self) -> bytes:
        """Serialize in the shape OpenClaw reads, not Execraft's internal one."""

        return json.dumps(
            to_gateway_config(self.config),
            indent=2,
            sort_keys=True,
            separators=(",", ": "),
        ).encode("utf-8") + b"\n"

    def as_mapping(self) -> dict[str, Any]:
        return {
            "runtime_id": self.runtime_id,
            "sha256": self.sha256,
            "providers": list(self.providers),
            "models": list(self.models),
            "agents": list(self.agents),
            "credential_refs": list(self.credential_refs),
            "config": self.config,
        }


def project_openclaw_config(
    execution: ExecutionArchitectureConfig,
    runtime_id: str,
    *,
    agent_workspaces: Mapping[str, Path | str] | None = None,
) -> OpenClawConfigProjection:
    """Project one configured OpenClaw runtime from canonical Execraft topology.

    Only enabled profiles selecting ``runtime_id`` are materialized. ``agent_workspaces``
    are Execraft-owned lazy-skill sources and are projected through
    ``skills.load.extraDirs``; they are never used as the agent working directory.
    The actual task workspace is bound immediately before execution.
    """

    runtime = _openclaw_runtime(execution, runtime_id)
    profiles = tuple(
        profile
        for profile in execution.agents
        if profile.enabled and profile.runtime_id == runtime_id
    )

    providers: dict[str, dict[str, Any]] = {}
    agent_entries: dict[str, dict[str, Any]] = {}
    models: set[str] = set()
    credential_refs: set[str] = set()

    for profile in sorted(profiles, key=lambda item: item.id):
        if not profile.model_route_id:
            raise OpenClawProjectionError(
                f"OpenClaw agent {profile.id!r} requires a model_route"
            )
        route = execution.model_route(profile.model_route_id)
        target = _selected_target(execution, profile, route)
        provider_id, provider_config, model_ref, credential_ref = _project_route(
            runtime, route, target
        )
        if provider_config:
            _merge_provider(providers, provider_id, provider_config, route_id=route.id)
        base_entry: dict[str, Any] = {"model": model_ref}
        if profile.skill_set:
            base_entry["skills"] = list(profile.skill_set)
        for agent_id, security_policy in openclaw_agent_security_entries(profile):
            if agent_id in agent_entries:
                raise OpenClawProjectionError(
                    f"OpenClaw agent id collision for {agent_id!r}; "
                    "profile ids must not collide with generated read-only ids"
                )
            agent_entry = dict(base_entry)
            agent_entry.update(openclaw_security_fragment(security_policy))
            agent_entries[agent_id] = agent_entry
        models.add(model_ref)
        if credential_ref:
            credential_refs.add(credential_ref)

    config: dict[str, Any] = {}
    if runtime.openclaw is not None and runtime.openclaw.mode == OpenClawMode.MANAGED:
        config["gateway"] = {"bind": "loopback", "mode": "local"}
    if providers:
        config["models"] = {
            "mode": "merge",
            "providers": {key: providers[key] for key in sorted(providers)},
        }
    if agent_entries:
        agents_config: dict[str, Any] = {
            "entries": {key: agent_entries[key] for key in sorted(agent_entries)}
        }
        defaults = _optimization_defaults(runtime)
        defaults["skipBootstrap"] = True
        agents_config["defaults"] = defaults
        config["agents"] = agents_config
    if agent_workspaces:
        skill_dirs = sorted(
            {
                str((Path(workspace).expanduser().resolve() / "skills").resolve())
                for workspace in agent_workspaces.values()
            }
        )
        if skill_dirs:
            config["skills"] = {"load": {"extraDirs": skill_dirs, "watch": True}}

    return OpenClawConfigProjection(
        runtime_id=runtime_id,
        config=config,
        providers=tuple(sorted(providers)),
        models=tuple(sorted(models)),
        agents=tuple(sorted(agent_entries)),
        credential_refs=tuple(sorted(credential_refs)),
    )


def _optimization_defaults(runtime: RuntimeConfig) -> dict[str, Any]:
    """Project public OpenClaw cache/pruning/compaction settings."""

    options = runtime.openclaw
    if options is None:
        return {}
    policy = options.optimization
    defaults: dict[str, Any] = {
        "params": {"cacheRetention": policy.cache_retention},
    }
    # OpenClaw has no switch that turns compaction off -- it is intrinsic to the
    # session runtime -- so ``proactive_compaction`` selects whether Execraft
    # imposes its own policy or leaves the Gateway's defaults in place. There is
    # no projection that disables compaction, and claiming one would be false.
    if policy.proactive_compaction:
        defaults["compaction"] = {
            "mode": policy.compaction_mode,
            "timeoutSeconds": policy.compaction_timeout_seconds,
        }
    if policy.context_pruning_mode != "off":
        defaults["contextPruning"] = {
            "mode": policy.context_pruning_mode,
            "ttl": policy.context_pruning_ttl,
        }
    else:
        defaults["contextPruning"] = {"mode": "off"}
    return defaults


def to_gateway_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Render Execraft's internal projection into OpenClaw's on-disk config shape.

    Execraft keeps agents id-keyed because every projection step -- subagents,
    security policy, skill workspaces -- addresses agents by id. OpenClaw's
    configuration schema instead expects ``agents.list``: an array whose entries
    each carry their own ``id``. Converting once, here at the boundary, keeps
    the convenient internal representation without writing a shape the Gateway
    rejects at startup.

    Validated against OpenClaw 2026.7.1-2, whose config validator rejects
    ``agents.entries`` with ``agents: Invalid input``.
    """

    rendered = deepcopy(dict(config))
    agents = rendered.get("agents")
    if not isinstance(agents, Mapping):
        return rendered
    agents_out = dict(agents)
    entries = agents_out.pop("entries", None)
    if isinstance(entries, Mapping):
        agents_out["list"] = [
            {"id": key, **deepcopy(dict(entries[key]))} for key in sorted(entries)
        ]
    rendered["agents"] = agents_out
    return rendered


def materialize_openclaw_config(
    projection: OpenClawConfigProjection,
    path: Path,
) -> Path:
    """Atomically write a private generated config outside product ownership."""

    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(projection.to_bytes())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination


def _openclaw_runtime(
    execution: ExecutionArchitectureConfig, runtime_id: str
) -> RuntimeConfig:
    try:
        runtime = execution.runtime(runtime_id)
    except KeyError as exc:
        raise OpenClawProjectionError(f"unknown runtime {runtime_id!r}") from exc
    if runtime.kind != RuntimeKind.OPENCLAW or runtime.openclaw is None:
        raise OpenClawProjectionError(
            f"runtime {runtime_id!r} is not an OpenClaw runtime"
        )
    return runtime


def _selected_target(
    execution: ExecutionArchitectureConfig,
    profile: AgentProfileConfig,
    route: ModelRouteConfig,
) -> ExecutionTargetConfig | None:
    target_id = profile.target_id or route.default_target
    if not target_id:
        if route.endpoint:
            raise OpenClawProjectionError(
                f"model route {route.id!r} declares endpoint without an execution target"
            )
        return None
    try:
        target = execution.target(target_id)
    except KeyError as exc:
        raise OpenClawProjectionError(
            f"model route {route.id!r} references unknown target {target_id!r}"
        ) from exc
    if target.kind == ExecutionTargetKind.REMOTE_RUNTIME:
        runtime = _openclaw_runtime(execution, profile.runtime_id)
        options = runtime.openclaw
        if options is None or options.mode != OpenClawMode.EXTERNAL:
            raise OpenClawProjectionError(
                f"remote_runtime target {target.id!r} requires external OpenClaw runtime"
            )
        if target.workspace_transport != "shared":
            raise OpenClawProjectionError(
                f"remote_runtime target {target.id!r} requires shared workspace transport"
            )
        return target
    if route.endpoint and target.endpoint:
        if _normalized_http_url(route.endpoint) != _normalized_http_url(target.endpoint):
            raise OpenClawProjectionError(
                f"model route {route.id!r} endpoint does not match target {target.id!r}"
            )
    return target


def _project_route(
    runtime: RuntimeConfig,
    route: ModelRouteConfig,
    target: ExecutionTargetConfig | None,
) -> tuple[str, dict[str, Any], str, str]:
    provider_family = route.provider.strip().lower()
    provider_id = (route.provider_alias or route.provider).strip()
    if not provider_id or not _PROVIDER_ID.fullmatch(provider_id):
        raise OpenClawProjectionError(
            f"model route {route.id!r} has invalid OpenClaw provider id {provider_id!r}"
        )
    endpoint = route.endpoint or (
        target.endpoint
        if target is not None and target.kind != ExecutionTargetKind.REMOTE_RUNTIME
        else ""
    )
    _validate_runtime_target_location(runtime, route, target, endpoint)

    model_config: dict[str, Any] = {"id": route.model, "name": route.model}
    if route.context_window is not None:
        model_config["contextWindow"] = route.context_window

    provider_config: dict[str, Any] = {}
    credential_ref = ""
    if route.credential_ref:
        credential_ref = _credential_name(route.credential_ref, route_id=route.id)
        provider_config["apiKey"] = _env_secret_ref(credential_ref)

    if provider_family == "ollama":
        if not endpoint:
            raise OpenClawProjectionError(
                f"Ollama model route {route.id!r} requires an endpoint/target"
            )
        ollama_url = _ollama_native_url(endpoint)
        provider_config.update(
            {
                "api": "ollama",
                "baseUrl": ollama_url,
                "models": [model_config],
            }
        )
        if not route.credential_ref:
            if _is_local_or_private_http_url(ollama_url):
                provider_config["apiKey"] = "ollama-local"
            else:
                raise OpenClawProjectionError(
                    f"public Ollama route {route.id!r} requires credential_ref"
                )
    elif endpoint:
        api = _API_FAMILY_MAP.get(route.api_family or "openai-compatible")
        if api is None:
            raise OpenClawProjectionError(
                f"model route {route.id!r} uses unsupported OpenClaw api_family "
                f"{route.api_family!r}"
            )
        provider_config.update(
            {"api": api, "baseUrl": endpoint.rstrip("/"), "models": [model_config]}
        )
        if not route.credential_ref:
            if _is_local_or_private_http_url(endpoint):
                provider_config["apiKey"] = "execraft-local"
            else:
                raise OpenClawProjectionError(
                    f"public custom route {route.id!r} requires credential_ref"
                )
    else:
        if provider_family not in _BUILTIN_CLOUD_PROVIDERS:
            raise OpenClawProjectionError(
                f"model route {route.id!r} provider {route.provider!r} needs an endpoint "
                "or an explicitly supported built-in OpenClaw provider"
            )
        # Built-in providers own their request adapter/base URL. We only project
        # credential references and explicit model metadata that Execraft knows.
        if route.context_window is not None:
            provider_config["models"] = [model_config]

    model_ref = f"{provider_id}/{route.model}"
    return provider_id, provider_config, model_ref, credential_ref


def _merge_provider(
    providers: dict[str, dict[str, Any]],
    provider_id: str,
    incoming: dict[str, Any],
    *,
    route_id: str,
) -> None:
    existing = providers.get(provider_id)
    if existing is None:
        providers[provider_id] = incoming
        return

    existing_models = list(existing.get("models", []))
    incoming_models = list(incoming.get("models", []))
    existing_base = {key: value for key, value in existing.items() if key != "models"}
    incoming_base = {key: value for key, value in incoming.items() if key != "models"}
    if existing_base != incoming_base:
        raise OpenClawProjectionError(
            f"OpenClaw provider id {provider_id!r} has conflicting configuration; "
            f"give route {route_id!r} a distinct provider_alias"
        )
    by_id = {str(item.get("id")): item for item in existing_models if isinstance(item, Mapping)}
    for item in incoming_models:
        model_id = str(item.get("id")) if isinstance(item, Mapping) else ""
        previous = by_id.get(model_id)
        if previous is not None and previous != item:
            raise OpenClawProjectionError(
                f"OpenClaw provider {provider_id!r} has conflicting model {model_id!r}"
            )
        if previous is None:
            existing_models.append(item)
            by_id[model_id] = item
    if existing_models:
        existing["models"] = sorted(existing_models, key=lambda item: str(item.get("id", "")))


def _validate_runtime_target_location(
    runtime: RuntimeConfig,
    route: ModelRouteConfig,
    target: ExecutionTargetConfig | None,
    endpoint: str,
) -> None:
    options = runtime.openclaw
    if options is None or target is None or not endpoint:
        return
    if target.kind != ExecutionTargetKind.LOCAL:
        return
    gateway_host = (urlsplit(options.gateway).hostname or "").strip().lower()
    endpoint_host = (urlsplit(endpoint).hostname or "").strip().lower()
    if is_loopback_host(endpoint_host) and not is_loopback_host(gateway_host):
        raise OpenClawProjectionError(
            f"local route {route.id!r} points to loopback but OpenClaw runtime "
            f"{runtime.id!r} is remote; remote runtime placement is not supported here"
        )


def _credential_name(value: str, *, route_id: str) -> str:
    name = value[4:] if value.startswith("env:") else value
    name = name.strip()
    if not _ENV_REF.fullmatch(name):
        raise OpenClawProjectionError(
            f"model route {route_id!r} credential_ref must be an environment reference"
        )
    return name


def _env_secret_ref(name: str) -> dict[str, str]:
    return {"source": "env", "provider": "default", "id": name}


def _ollama_native_url(value: str) -> str:
    parsed = urlsplit(value.rstrip("/"))
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    if path not in {"", "/"}:
        raise OpenClawProjectionError(
            f"Ollama endpoint {value!r} must be a host URL or end in /v1"
        )
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")


def _normalized_http_url(value: str) -> str:
    parsed = urlsplit(value.rstrip("/"))
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), "", ""))


def _is_local_or_private_http_url(value: str) -> bool:
    host = (urlsplit(value).hostname or "").strip().lower().rstrip(".")
    if is_loopback_host(host) or host.endswith(".local") or "." not in host:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local
