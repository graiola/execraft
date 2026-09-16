"""OpenCode compatibility projection over the runtime-neutral model registry.

Endpoint/model/physical-target ownership belongs to :mod:`execraft.model_registry`.
This module intentionally retains the historical public constructors and render
helpers so existing projects, tests, and extensions do not need a flag-day
migration.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse
from execraft.network import is_loopback_host

from execraft.model_registry import (
    ModelEndpoint,
    ModelRegistryError,
    ModelRouteRegistry,
    load_model_route_registry,
)
from execraft.targets.config import ExecutionTargetKind


class OpenCodeRegistryError(ValueError):
    """Raised when the OpenCode compatibility projection cannot be built."""


@dataclass(frozen=True)
class OpenCodeEndpoint:
    """Legacy OpenCode endpoint view backed by normalized endpoint semantics."""

    endpoint_id: str
    provider_id: str
    name: str
    base_url: str
    base_url_source: str
    npm: str
    models: Mapping[str, Mapping[str, Any]]
    options: Mapping[str, Any]
    connect_timeout_seconds: int = 5
    models_path: str = "/models"
    kind: str = "openai-compatible"
    provider_family: str = ""
    target_id: str = ""
    target_kind: ExecutionTargetKind | str = ""
    concurrency_group: str = ""

    @property
    def models_url(self) -> str:
        return f"{self.base_url.rstrip('/')}{self.models_path}"

    def full_model_id(self, model_id: str) -> str:
        return f"{self.provider_id}/{model_id}"

    def as_opencode_provider(self) -> dict[str, Any]:
        options = copy.deepcopy(dict(self.options))
        options["baseURL"] = self.base_url
        return {
            "npm": self.npm,
            "name": self.name,
            "options": options,
            "models": copy.deepcopy(dict(self.models)),
        }

    def as_model_endpoint(self) -> ModelEndpoint:
        provider_family = self.provider_family or _provider_family(self.provider_id)
        target_id = self.target_id or self.endpoint_id
        target_kind = _target_kind(self.target_kind, self.base_url)
        concurrency_group = self.concurrency_group or target_id
        return ModelEndpoint(
            endpoint_id=self.endpoint_id,
            provider_family=provider_family,
            provider_alias=self.provider_id,
            name=self.name,
            base_url=self.base_url,
            base_url_source=self.base_url_source,
            models=self.models,
            target_id=target_id,
            target_kind=target_kind,
            concurrency_group=concurrency_group,
            api_family=self.kind,
            connect_timeout_seconds=self.connect_timeout_seconds,
            models_path=self.models_path,
            projection_metadata={
                "opencode": {
                    "npm": self.npm,
                    "options": copy.deepcopy(dict(self.options)),
                }
            },
        )

    @classmethod
    def from_model_endpoint(cls, endpoint: ModelEndpoint) -> "OpenCodeEndpoint":
        metadata = endpoint.projection_metadata.get("opencode", {})
        if not isinstance(metadata, Mapping):
            metadata = {}
        return cls(
            endpoint_id=endpoint.endpoint_id,
            provider_id=endpoint.provider_alias,
            name=endpoint.name,
            base_url=endpoint.base_url,
            base_url_source=endpoint.base_url_source,
            npm=str(metadata.get("npm", "@ai-sdk/openai-compatible")),
            models=endpoint.models,
            options=copy.deepcopy(dict(metadata.get("options", {}))),
            connect_timeout_seconds=endpoint.connect_timeout_seconds,
            models_path=endpoint.models_path,
            kind=endpoint.api_family,
            provider_family=endpoint.provider_family,
            target_id=endpoint.target_id,
            target_kind=endpoint.target_kind,
            concurrency_group=endpoint.concurrency_group,
        )


@dataclass(frozen=True)
class OpenCodeProviderRegistry:
    """Compatibility view whose canonical representation is ``model_registry``."""

    endpoints: tuple[OpenCodeEndpoint, ...] = ()
    _model_registry: ModelRouteRegistry = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        try:
            registry = ModelRouteRegistry(
                tuple(endpoint.as_model_endpoint() for endpoint in self.endpoints)
            )
        except ModelRegistryError as exc:
            raise OpenCodeRegistryError(_compat_error(str(exc))) from exc
        object.__setattr__(self, "_model_registry", registry)

    @classmethod
    def from_model_registry(cls, registry: ModelRouteRegistry) -> "OpenCodeProviderRegistry":
        instance = cls(tuple(OpenCodeEndpoint.from_model_endpoint(item) for item in registry.endpoints))
        object.__setattr__(instance, "_model_registry", registry)
        return instance

    @property
    def model_registry(self) -> ModelRouteRegistry:
        return self._model_registry

    @property
    def by_provider_id(self) -> dict[str, OpenCodeEndpoint]:
        return {endpoint.provider_id: endpoint for endpoint in self.endpoints}

    @property
    def by_endpoint_id(self) -> dict[str, OpenCodeEndpoint]:
        return {endpoint.endpoint_id: endpoint for endpoint in self.endpoints}

    def endpoint_for_model(self, model: str) -> OpenCodeEndpoint | None:
        generic = self._model_registry.endpoint_for_model(model)
        if generic is None:
            return None
        return self.by_endpoint_id[generic.endpoint_id]

    def model_id(self, model: str) -> str:
        return self._model_registry.model_id(model)

    def as_opencode_providers(self) -> dict[str, Any]:
        return {endpoint.provider_id: endpoint.as_opencode_provider() for endpoint in self.endpoints}

    def minimal_opencode_config(self, provider_id: str) -> dict[str, Any]:
        endpoint = self.by_provider_id.get(provider_id)
        if endpoint is None:
            raise OpenCodeRegistryError(
                f"OpenCode endpoint is not declared for provider {provider_id!r}"
            )
        return {
            "$schema": "https://opencode.ai/config.json",
            "permission": {"*": "deny", "external_directory": "deny"},
            "provider": {provider_id: endpoint.as_opencode_provider()},
        }


def load_opencode_provider_registry(
    path: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> OpenCodeProviderRegistry:
    """Load legacy ``providers.yaml`` through the canonical generic registry."""
    try:
        registry = load_model_route_registry(path, environ=environ)
    except ModelRegistryError as exc:
        raise OpenCodeRegistryError(_compat_error(str(exc))) from exc
    return OpenCodeProviderRegistry.from_model_registry(registry)


def _provider_family(provider_id: str) -> str:
    lowered = provider_id.lower()
    if lowered == "ollama" or lowered.startswith("ollama-"):
        return "ollama"
    return lowered.split("-", 1)[0] or "custom"


def _target_kind(value: ExecutionTargetKind | str, base_url: str) -> ExecutionTargetKind:
    if value:
        try:
            return value if isinstance(value, ExecutionTargetKind) else ExecutionTargetKind(str(value))
        except ValueError as exc:
            raise OpenCodeRegistryError(f"unsupported target_kind: {value!r}") from exc
    hostname = urlparse(base_url).hostname or ""
    if is_loopback_host(hostname):
        return ExecutionTargetKind.LOCAL
    return ExecutionTargetKind.INFERENCE_ENDPOINT


def _compat_error(message: str) -> str:
    if message.startswith("duplicate provider alias in model registry:"):
        value = message.split(":", 1)[1].strip()
        return f"duplicate OpenCode provider_id in endpoint registry: {value}"
    if message.startswith("model endpoint registry must be a mapping"):
        return message.replace("model endpoint registry", "OpenCode provider registry", 1)
    if message.startswith("model endpoint registry endpoints"):
        return message.replace("model endpoint registry", "OpenCode registry", 1)
    if message.startswith("unsupported model endpoint registry schema_version"):
        return message.replace("model endpoint registry", "OpenCode provider registry", 1)
    return message
