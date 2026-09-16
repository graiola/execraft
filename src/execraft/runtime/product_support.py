"""Product-support policy for runtime execution.

The normalized schema intentionally remains broader than the promoted product:
older experimental definitions must stay readable for compatibility, while the
normal execution path supports Native runtimes plus OpenClaw on local or
inference-endpoint targets only.  Keeping this policy separate from parsing
preserves the third-runtime registration seam and avoids making compatibility
configuration unreadable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from execraft.runtime_config import RuntimeKind
from execraft.targets.config import ExecutionTargetKind


class ProductSupportError(ValueError):
    """Raised when configured execution selects a non-promoted product feature."""


@dataclass(frozen=True)
class ProductSupport:
    status: str
    supported: bool
    reason: str = ""

    def as_mapping(self) -> dict[str, object]:
        return {
            "status": self.status,
            "supported": self.supported,
            "reason": self.reason,
        }


SUPPORTED = ProductSupport("supported", True)
REGISTERED_EXTENSION = ProductSupport(
    "registered_extension",
    True,
    "Runtime support is supplied by an explicitly registered extension.",
)
REMOTE_RUNTIME_DISABLED = ProductSupport(
    "experimental_disabled",
    False,
    "Remote full-runtime targets are compatibility-only and are not supported for execution.",
)
SUBAGENTS_DISABLED_REASON = (
    "OpenClaw sub-agents are experimental-disabled "
    "in the supported runtime architecture."
)


def runtime_product_support(runtime: Any) -> ProductSupport:
    """Return product support without restricting registered third-party runtimes."""

    kind = _value(getattr(runtime, "kind", ""))
    if kind in {RuntimeKind.NATIVE.value, RuntimeKind.OPENCLAW.value}:
        return SUPPORTED
    return REGISTERED_EXTENSION


def target_product_support(target: Any) -> ProductSupport:
    """Local/inference targets are promoted; full remote runtime placement is not."""

    kind = _value(getattr(target, "kind", ""))
    if kind == ExecutionTargetKind.REMOTE_RUNTIME.value:
        return REMOTE_RUNTIME_DISABLED
    return SUPPORTED


def profile_product_support(execution: Any, profile: Any) -> ProductSupport:
    """Return the effective placement support for one execution profile."""

    runtime = execution.runtime(profile.runtime_id)
    runtime_support = runtime_product_support(runtime)
    if not runtime_support.supported:
        return runtime_support
    route = execution.model_route(profile.model_route_id) if profile.model_route_id else None
    target_id = profile.target_id or (route.default_target if route else "")
    if target_id:
        target_support = target_product_support(execution.target(target_id))
        if not target_support.supported:
            return target_support
    return runtime_support


def ensure_supported_execution(execution: Any, *, subagent_strategy: Any = None) -> None:
    """Fail closed before constructing candidates for unsupported experimental paths.

    Parsing remains intentionally permissive enough to read historical disabled
    definitions.  This function is the product execution check and therefore only
    examines enabled candidates represented by ``execution.agents``.
    """

    if bool(getattr(subagent_strategy, "active", False)):
        raise ProductSupportError(SUBAGENTS_DISABLED_REASON)
    for profile in execution.agents:
        support = profile_product_support(execution, profile)
        if not support.supported:
            raise ProductSupportError(
                f"execution profile {profile.id!r} is {support.status}: {support.reason}"
            )


def _value(value: object) -> str:
    return str(getattr(value, "value", value)).strip().lower()


__all__ = [
    "ProductSupport",
    "ProductSupportError",
    "ensure_supported_execution",
    "profile_product_support",
    "runtime_product_support",
    "target_product_support",
]
