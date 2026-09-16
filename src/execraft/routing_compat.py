"""Compatibility checks across runtime, model route, and physical target.

The module captures facts known before runtime execution. It combines
Native/OpenCode checks with OpenClaw provider/API/placement facts validated
against the pinned Gateway release, while provider configuration rendering
remains isolated under :mod:`execraft.runtime`.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from execraft.model_routes import ModelRouteConfig
from execraft.network import is_loopback_host
from execraft.runtime_config import OpenClawMode, RuntimeConfig, RuntimeKind
from execraft.targets.config import ExecutionTargetConfig, ExecutionTargetKind


@dataclass(frozen=True)
class RouteCompatibility:
    compatible: bool
    reason: str = ""


_OPENCLAW_ENDPOINT_APIS = frozenset(
    {"", "openai-compatible", "openai-completions", "openai-responses", "anthropic-messages", "ollama"}
)


def evaluate_route_compatibility(
    runtime: RuntimeConfig,
    route: ModelRouteConfig,
    target: ExecutionTargetConfig | None = None,
) -> RouteCompatibility:
    """Validate compatibility facts known before runtime execution."""

    if (
        runtime.kind == RuntimeKind.NATIVE
        and runtime.adapter == "opencode"
        and route.api_family
        and route.api_family != "openai-compatible"
    ):
        return RouteCompatibility(
            False,
            "Native OpenCode supports only openai-compatible routed endpoints",
        )

    if target is not None:
        if target.kind == ExecutionTargetKind.REMOTE_RUNTIME:
            if runtime.kind == RuntimeKind.NATIVE:
                return RouteCompatibility(
                    False,
                    "Native runtime cannot execute on a remote_runtime target",
                )
            if (
                runtime.kind != RuntimeKind.OPENCLAW
                or runtime.openclaw is None
                or runtime.openclaw.mode != OpenClawMode.EXTERNAL
            ):
                return RouteCompatibility(
                    False,
                    "remote_runtime targets require an external OpenClaw runtime",
                )
            if target.workspace_transport != "shared":
                return RouteCompatibility(
                    False,
                    "remote_runtime targets require shared workspace transport",
                )
        if target.kind == ExecutionTargetKind.INFERENCE_ENDPOINT and not target.endpoint:
            return RouteCompatibility(False, "inference_endpoint target requires endpoint")
        if (
            target.kind != ExecutionTargetKind.REMOTE_RUNTIME
            and route.endpoint
            and target.endpoint
            and route.endpoint.rstrip("/") != target.endpoint.rstrip("/")
        ):
            return RouteCompatibility(
                False,
                "model route endpoint does not match the selected execution target",
            )

    if runtime.kind != RuntimeKind.OPENCLAW:
        return RouteCompatibility(True)

    endpoint = route.endpoint or (
        target.endpoint
        if target is not None and target.kind != ExecutionTargetKind.REMOTE_RUNTIME
        else ""
    )
    provider = route.provider.strip().lower()
    if provider == "ollama" and not endpoint:
        return RouteCompatibility(False, "OpenClaw Ollama routes require an endpoint target")
    if endpoint and route.api_family not in _OPENCLAW_ENDPOINT_APIS:
        return RouteCompatibility(
            False,
            f"OpenClaw does not support routed api_family {route.api_family!r}",
        )
    if (
        runtime.openclaw is not None
        and runtime.openclaw.mode == OpenClawMode.EXTERNAL
        and target is not None
        and target.kind == ExecutionTargetKind.LOCAL
        and endpoint
        and _is_loopback_url(endpoint)
        and not _is_loopback_url(runtime.openclaw.gateway)
    ):
        return RouteCompatibility(
            False,
            "remote external OpenClaw cannot reinterpret a control-host loopback target; "
            "remote runtime placement requires a remote_runtime target",
        )
    return RouteCompatibility(True)


def _is_loopback_url(value: str) -> bool:
    return is_loopback_host((urlsplit(value).hostname or "").lower())
