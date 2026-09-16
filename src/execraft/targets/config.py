"""Physical execution/inference target configuration."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ExecutionTargetKind(str, Enum):
    """Keep inference placement distinct from complete runtime placement."""

    LOCAL = "local"
    INFERENCE_ENDPOINT = "inference_endpoint"
    REMOTE_RUNTIME = "remote_runtime"


@dataclass(frozen=True)
class RemoteRuntimeEnvironmentConfig:
    """Declarative remote-node capability inventory; never discovered via host shell."""

    toolchains: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    gpu: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    sandbox: bool | None = None

    def as_mapping(self) -> dict[str, object]:
        result: dict[str, object] = {}
        if self.toolchains:
            result["toolchains"] = list(self.toolchains)
        if self.tools:
            result["tools"] = list(self.tools)
        if self.gpu:
            result["gpu"] = list(self.gpu)
        if self.models:
            result["models"] = list(self.models)
        if self.sandbox is not None:
            result["sandbox"] = self.sandbox
        return result


@dataclass(frozen=True)
class ExecutionTargetConfig:
    """Where inference or a complete external runtime is physically available."""

    id: str
    kind: ExecutionTargetKind
    endpoint: str = ""
    concurrency_group: str = ""
    workspace_transport: str = ""
    max_concurrency: int = 0
    environment: RemoteRuntimeEnvironmentConfig | None = None

    def as_mapping(self) -> dict[str, object]:
        result: dict[str, object] = {"kind": self.kind.value}
        if self.endpoint:
            result["endpoint"] = self.endpoint
        if self.concurrency_group:
            result["concurrency_group"] = self.concurrency_group
        if self.workspace_transport:
            result["workspace_transport"] = self.workspace_transport
        if self.max_concurrency:
            result["max_concurrency"] = self.max_concurrency
        if self.environment is not None:
            environment = self.environment.as_mapping()
            if environment:
                result["environment"] = environment
        return result
