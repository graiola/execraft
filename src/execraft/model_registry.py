"""Canonical runtime-neutral model endpoint and route inventory.

This module owns endpoint/model/physical-target semantics. Runtime-specific
renderers (currently Native/OpenCode) are compatibility projections over this
inventory rather than the source of truth.
"""

from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import yaml

from execraft.network import is_loopback_host
from execraft.model_routes import ModelRouteConfig
from execraft.targets.config import ExecutionTargetConfig, ExecutionTargetKind


class ModelRegistryError(ValueError):
    """Raised when a model endpoint inventory is invalid or ambiguous."""


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SUPPORTED_API_FAMILIES = {"openai-compatible"}


@dataclass(frozen=True)
class ModelEndpoint:
    """One model-serving endpoint independent from any agent runtime.

    ``provider_alias`` is a runtime-facing alias used by projections such as
    OpenCode (for example ``ollama-gpu-a``). ``provider_family`` captures the
    actual provider semantics (for example ``ollama``), while ``target_id`` and
    ``target_kind`` describe where inference is physically hosted.
    """

    endpoint_id: str
    provider_family: str
    provider_alias: str
    name: str
    base_url: str
    base_url_source: str
    models: Mapping[str, Mapping[str, Any]]
    target_id: str
    target_kind: ExecutionTargetKind
    concurrency_group: str = ""
    api_family: str = "openai-compatible"
    connect_timeout_seconds: int = 5
    models_path: str = "/models"
    projection_metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def models_url(self) -> str:
        return f"{self.base_url.rstrip('/')}{self.models_path}"

    def runtime_model_reference(self, model_id: str) -> str:
        return f"{self.provider_alias}/{model_id}"

    def target_config(self) -> ExecutionTargetConfig:
        return ExecutionTargetConfig(
            id=self.target_id,
            kind=self.target_kind,
            endpoint=self.base_url,
            concurrency_group=self.concurrency_group,
        )

    def route_config(self, model_id: str) -> ModelRouteConfig:
        if model_id not in self.models:
            raise KeyError(model_id)
        model_config = self.models[model_id]
        context_window: int | None = None
        raw_limit = model_config.get("limit") if isinstance(model_config, Mapping) else None
        if isinstance(raw_limit, Mapping):
            raw_context = raw_limit.get("context")
            if isinstance(raw_context, int) and raw_context > 0:
                context_window = raw_context
        return ModelRouteConfig(
            id=f"{self.endpoint_id}:{model_id}",
            provider=self.provider_family,
            provider_alias=self.provider_alias,
            model=model_id,
            endpoint=self.base_url,
            api_family=self.api_family,
            context_window=context_window,
            default_target=self.target_id,
        )


@dataclass(frozen=True)
class ModelRouteRegistry:
    """Validated canonical inventory of model endpoints and physical targets."""

    endpoints: tuple[ModelEndpoint, ...] = ()

    def __post_init__(self) -> None:
        _ensure_unique(self.endpoints, "endpoint_id", "endpoint id")
        _ensure_unique(self.endpoints, "provider_alias", "provider alias")
        _validate_target_consistency(self.endpoints)

    @property
    def by_endpoint_id(self) -> dict[str, ModelEndpoint]:
        return {endpoint.endpoint_id: endpoint for endpoint in self.endpoints}

    @property
    def by_provider_alias(self) -> dict[str, ModelEndpoint]:
        return {endpoint.provider_alias: endpoint for endpoint in self.endpoints}

    @property
    def routes(self) -> tuple[ModelRouteConfig, ...]:
        return tuple(
            endpoint.route_config(model_id)
            for endpoint in self.endpoints
            for model_id in endpoint.models
        )

    @property
    def targets(self) -> tuple[ExecutionTargetConfig, ...]:
        seen: set[str] = set()
        result: list[ExecutionTargetConfig] = []
        for endpoint in self.endpoints:
            if endpoint.target_id in seen:
                continue
            seen.add(endpoint.target_id)
            result.append(endpoint.target_config())
        return tuple(result)

    def endpoint_for_model(self, model_reference: str) -> ModelEndpoint | None:
        provider_alias, separator, model_id = model_reference.partition("/")
        if not separator:
            return None
        endpoint = self.by_provider_alias.get(provider_alias)
        if endpoint is None or model_id not in endpoint.models:
            return endpoint
        return endpoint

    def model_id(self, model_reference: str) -> str:
        endpoint = self.endpoint_for_model(model_reference)
        if endpoint is None:
            return ""
        return model_reference.split("/", 1)[1]

    def route_for_model(self, model_reference: str) -> ModelRouteConfig | None:
        endpoint = self.endpoint_for_model(model_reference)
        if endpoint is None:
            return None
        model_id = self.model_id(model_reference)
        if not model_id or model_id not in endpoint.models:
            return None
        return endpoint.route_config(model_id)


def load_model_route_registry(
    path: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> ModelRouteRegistry:
    """Load the existing ``providers.yaml`` shape into the canonical inventory.

    Schema 1 remains supported. Newer schemas add optional runtime-neutral metadata fields
    (``provider``, ``target_id``, ``target_kind``, ``concurrency_group``, and
    ``api_family``). When absent, conservative compatibility inference is used:
    loopback URLs are local targets and non-loopback URLs are inference targets.
    """

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return ModelRouteRegistry()
    except yaml.YAMLError as exc:
        raise ModelRegistryError(f"invalid YAML in {path}: {exc}") from exc

    if not isinstance(raw, Mapping):
        raise ModelRegistryError(f"model endpoint registry must be a mapping: {path}")
    schema_version = int(raw.get("schema_version", 0))
    if schema_version != 1:
        raise ModelRegistryError(
            f"unsupported model endpoint registry schema_version: {schema_version!r}"
        )

    model_sets = _parse_model_sets(raw.get("model_sets") or {})
    endpoints_raw = raw.get("endpoints") or {}
    if not isinstance(endpoints_raw, Mapping):
        raise ModelRegistryError("model endpoint registry endpoints must be a mapping")

    environment = os.environ if environ is None else environ
    endpoints: list[ModelEndpoint] = []
    for endpoint_name, endpoint_value in endpoints_raw.items():
        endpoint = _parse_endpoint(
            str(endpoint_name).strip(), endpoint_value, model_sets=model_sets,
            environment=environment,
        )
        if endpoint is not None:
            endpoints.append(endpoint)
    return ModelRouteRegistry(tuple(endpoints))


def _parse_endpoint(
    endpoint_id: str,
    raw: object,
    *,
    model_sets: Mapping[str, Mapping[str, Mapping[str, Any]]],
    environment: Mapping[str, str],
) -> ModelEndpoint | None:
    _validate_identifier(endpoint_id, label="endpoint id")
    if not isinstance(raw, Mapping):
        raise ModelRegistryError(f"model endpoint {endpoint_id!r} must be a mapping")
    enabled = bool(raw.get("enabled", True))

    provider_alias = str(raw.get("provider_id", endpoint_id)).strip()
    _validate_identifier(provider_alias, label=f"provider_id for endpoint {endpoint_id!r}")
    provider_family = str(raw.get("provider", "")).strip().lower()
    if not provider_family:
        provider_family = _infer_provider_family(provider_alias)
    _validate_identifier(provider_family, label=f"provider for endpoint {endpoint_id!r}")

    api_family = str(raw.get("api_family", raw.get("kind", "openai-compatible"))).strip().lower()
    if api_family not in _SUPPORTED_API_FAMILIES:
        raise ModelRegistryError(
            f"unsupported endpoint kind/api_family for {endpoint_id!r}: {api_family!r}"
        )

    base_url_env = str(raw.get("base_url_env", "")).strip()
    if base_url_env and not _ENVIRONMENT_NAME.fullmatch(base_url_env):
        raise ModelRegistryError(
            f"invalid base_url_env for endpoint {endpoint_id!r}: {base_url_env!r}"
        )
    configured_url = str(raw.get("base_url", "")).strip()
    if configured_url:
        configured_url = _validate_base_url(configured_url, endpoint_id=endpoint_id)
    environment_url = str(environment.get(base_url_env, "")).strip() if base_url_env else ""
    if environment_url:
        environment_url = _validate_base_url(
            environment_url,
            endpoint_id=endpoint_id,
            source=f"environment variable {base_url_env}",
        )
    base_url = environment_url or configured_url
    base_url_source = f"environment:{base_url_env}" if environment_url else "configuration"
    if not base_url and enabled:
        suffix = f" or environment variable {base_url_env}" if base_url_env else ""
        raise ModelRegistryError(f"enabled endpoint {endpoint_id!r} requires base_url{suffix}")

    models_path = str(raw.get("models_path", "/models")).strip()
    if not models_path.startswith("/") or "?" in models_path or "#" in models_path:
        raise ModelRegistryError(
            f"models_path for endpoint {endpoint_id!r} must be an absolute URL path"
        )
    connect_timeout_seconds = int(raw.get("connect_timeout_seconds", 5))
    if not 1 <= connect_timeout_seconds <= 120:
        raise ModelRegistryError(
            f"connect_timeout_seconds for endpoint {endpoint_id!r} must be between 1 and 120"
        )

    model_set_id = str(raw.get("model_set", "")).strip()
    models: dict[str, Mapping[str, Any]] = {}
    if model_set_id:
        _validate_identifier(model_set_id, label=f"model_set for endpoint {endpoint_id!r}")
        try:
            models.update(copy.deepcopy(dict(model_sets[model_set_id])))
        except KeyError as exc:
            raise ModelRegistryError(
                f"unknown model_set {model_set_id!r} for endpoint {endpoint_id!r}"
            ) from exc
    models.update(_parse_models(raw.get("models") or {}, label=f"endpoint {endpoint_id!r}"))
    if enabled and not models:
        raise ModelRegistryError(f"enabled endpoint {endpoint_id!r} must declare at least one model")

    options_raw = raw.get("options") or {}
    if not isinstance(options_raw, Mapping):
        raise ModelRegistryError(f"options must be a mapping for endpoint {endpoint_id!r}")
    options = copy.deepcopy(dict(options_raw))
    if "baseURL" in options or "base_url" in options:
        raise ModelRegistryError(
            f"endpoint {endpoint_id!r} must configure its URL through base_url/base_url_env"
        )
    _validate_json_value(options, label=f"options for endpoint {endpoint_id!r}")
    npm = str(raw.get("npm", "@ai-sdk/openai-compatible")).strip()
    if not npm:
        raise ModelRegistryError(f"npm cannot be empty for endpoint {endpoint_id!r}")

    if not enabled:
        return None
    inferred_kind = _infer_target_kind(base_url)
    target_kind_raw = str(raw.get("target_kind", inferred_kind.value)).strip().lower()
    try:
        target_kind = ExecutionTargetKind(target_kind_raw)
    except ValueError as exc:
        raise ModelRegistryError(
            f"unsupported target_kind for endpoint {endpoint_id!r}: {target_kind_raw!r}"
        ) from exc
    if target_kind == ExecutionTargetKind.REMOTE_RUNTIME:
        raise ModelRegistryError(
            f"endpoint {endpoint_id!r} is a model endpoint and cannot use remote_runtime target_kind"
        )
    target_id = str(raw.get("target_id", endpoint_id)).strip()
    _validate_identifier(target_id, label=f"target_id for endpoint {endpoint_id!r}")
    concurrency_group = str(raw.get("concurrency_group", target_id)).strip() or target_id

    return ModelEndpoint(
        endpoint_id=endpoint_id,
        provider_family=provider_family,
        provider_alias=provider_alias,
        name=str(raw.get("name", endpoint_id)).strip() or endpoint_id,
        base_url=base_url,
        base_url_source=base_url_source,
        models=models,
        target_id=target_id,
        target_kind=target_kind,
        concurrency_group=concurrency_group,
        api_family=api_family,
        connect_timeout_seconds=connect_timeout_seconds,
        models_path=models_path,
        projection_metadata={"opencode": {"npm": npm, "options": options}},
    )


def _infer_provider_family(provider_alias: str) -> str:
    lowered = provider_alias.lower()
    if lowered == "ollama" or lowered.startswith("ollama-"):
        return "ollama"
    return lowered.split("-", 1)[0] or "custom"


def _infer_target_kind(base_url: str) -> ExecutionTargetKind:
    hostname = urlparse(base_url).hostname or ""
    if is_loopback_host(hostname):
        return ExecutionTargetKind.LOCAL
    return ExecutionTargetKind.INFERENCE_ENDPOINT


def _validate_target_consistency(endpoints: tuple[ModelEndpoint, ...]) -> None:
    seen: dict[str, tuple[ExecutionTargetKind, str, str]] = {}
    for endpoint in endpoints:
        current = (endpoint.target_kind, endpoint.base_url, endpoint.concurrency_group)
        previous = seen.setdefault(endpoint.target_id, current)
        if previous != current:
            raise ModelRegistryError(
                f"execution target {endpoint.target_id!r} has conflicting endpoint definitions"
            )


def _ensure_unique(items: tuple[ModelEndpoint, ...], attr: str, label: str) -> None:
    seen: set[str] = set()
    for item in items:
        value = str(getattr(item, attr))
        if value in seen:
            raise ModelRegistryError(f"duplicate {label} in model registry: {value}")
        seen.add(value)


def _validate_identifier(value: str, *, label: str) -> None:
    if not value or not _IDENTIFIER.fullmatch(value):
        raise ModelRegistryError(f"invalid {label}: {value!r}")


def _parse_model_sets(raw: Any) -> dict[str, dict[str, Mapping[str, Any]]]:
    if not isinstance(raw, Mapping):
        raise ModelRegistryError("model registry model_sets must be a mapping")
    result: dict[str, dict[str, Mapping[str, Any]]] = {}
    for name, models in raw.items():
        model_set_id = str(name).strip()
        _validate_identifier(model_set_id, label="model_set id")
        result[model_set_id] = _parse_models(models, label=f"model_set {model_set_id!r}")
    return result


def _parse_models(raw: Any, *, label: str) -> dict[str, Mapping[str, Any]]:
    if not isinstance(raw, Mapping):
        raise ModelRegistryError(f"models must be a mapping for {label}")
    result: dict[str, Mapping[str, Any]] = {}
    for name, value in raw.items():
        model_id = str(name).strip()
        if not model_id:
            raise ModelRegistryError(f"model ids cannot be empty for {label}")
        if not isinstance(value, Mapping):
            raise ModelRegistryError(f"model {model_id!r} for {label} must be a mapping")
        model_config = copy.deepcopy(dict(value))
        _validate_json_value(model_config, label=f"model {model_id!r} for {label}")
        result[model_id] = model_config
    return result


def _validate_base_url(value: str, *, endpoint_id: str, source: str = "configuration") -> str:
    parsed = urlparse(value)
    prefix = f"base_url {source} for endpoint {endpoint_id!r}"
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ModelRegistryError(f"{prefix} must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ModelRegistryError(f"{prefix} must not embed credentials")
    if parsed.query or parsed.fragment:
        raise ModelRegistryError(f"{prefix} must not include query or fragment")
    return value.rstrip("/")


def _validate_json_value(value: Any, *, label: str) -> None:
    try:
        json.dumps(value)
    except (TypeError, ValueError) as exc:
        raise ModelRegistryError(f"{label} must be JSON serializable") from exc
