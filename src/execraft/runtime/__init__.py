"""Agent runtime substitution surface.

The neutral contracts are importable without loading any concrete runtime.
Concrete runtimes are resolved lazily to keep the contract package safe for
scheduler/configuration imports and to avoid package-initialization cycles.
"""

from __future__ import annotations

from .contracts import (
    AgentRuntime,
    RuntimeCapabilities,
    RuntimeExecutionRequest,
    RuntimeExecutionResult,
    RuntimeSessionRef,
)

__all__ = [
    "AgentRuntime",
    "NativeAgentRuntime",
    "RuntimeCapabilities",
    "RuntimeExecutionRequest",
    "RuntimeExecutionResult",
    "RuntimeSessionRef",
    "build_native_runtime",
    "OpenClawGatewayClient",
    "OpenClawGatewayService",
    "OpenClawDiagnostic",
    "OpenClawConfigProjection",
    "OpenClawModelDiscovery",
    "discover_projected_models",
    "project_openclaw_config",
]


def __getattr__(name: str):
    if name in {"NativeAgentRuntime", "build_native_runtime"}:
        from .native import NativeAgentRuntime, build_native_runtime

        return {
            "NativeAgentRuntime": NativeAgentRuntime,
            "build_native_runtime": build_native_runtime,
        }[name]
    if name in {"OpenClawModelDiscovery", "discover_projected_models"}:
        from .openclaw_discovery import OpenClawModelDiscovery, discover_projected_models

        return {
            "OpenClawModelDiscovery": OpenClawModelDiscovery,
            "discover_projected_models": discover_projected_models,
        }[name]
    if name in {"OpenClawConfigProjection", "project_openclaw_config"}:
        from .openclaw_projection import OpenClawConfigProjection, project_openclaw_config

        return {
            "OpenClawConfigProjection": OpenClawConfigProjection,
            "project_openclaw_config": project_openclaw_config,
        }[name]
    if name in {"OpenClawGatewayClient", "OpenClawGatewayService", "OpenClawDiagnostic"}:
        from .openclaw_gateway import OpenClawGatewayClient
        from .openclaw_service import OpenClawDiagnostic, OpenClawGatewayService

        return {
            "OpenClawGatewayClient": OpenClawGatewayClient,
            "OpenClawGatewayService": OpenClawGatewayService,
            "OpenClawDiagnostic": OpenClawDiagnostic,
        }[name]
    raise AttributeError(name)
