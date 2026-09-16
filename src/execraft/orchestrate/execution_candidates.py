"""Candidate identity, metadata and health projection helpers.

These helpers keep runtime/model/target concerns out of the orchestration state
machine.  The orchestrator still owns scheduling policy and lifecycle, but it
no longer needs to understand how a concrete runtime exposes those dimensions.
"""

from __future__ import annotations

from typing import Any

from execraft.execution_identity import execution_identity_of

from .execution_health import (
    ExecutionHealthStore,
    failure_health_dimension,
)
from .provider_health import ProviderHealthStore
from .scheduler import AgentAdapter, agent_adapter_capabilities


def candidate_id(adapter: AgentAdapter) -> str:
    return execution_identity_of(adapter).candidate_id


def candidate_concurrency_group(adapter: AgentAdapter | None) -> str:
    if adapter is None:
        return ""
    identity = execution_identity_of(adapter)
    return identity.concurrency_group or identity.candidate_id


def candidate_metadata(adapter: AgentAdapter | None) -> dict[str, Any]:
    if adapter is None:
        return {
            "adapter": "",
            "model": "",
            "execution_identity": {},
            "execution_capabilities": {},
            "interaction_mode": "terminal",
            "streaming_interaction": False,
            "steering_supported": False,
            "control_mode": "none",
            "transport": "",
            "interactive_pty": False,
            "session_resume": False,
        }
    execution = agent_adapter_capabilities(adapter)
    identity = execution_identity_of(adapter)
    return {
        "adapter": str(getattr(adapter, "adapter_name", adapter.__class__.__name__)),
        "model": str(getattr(adapter, "model", "")),
        "execution_identity": identity.as_mapping(),
        "execution_capabilities": execution.as_mapping(),
        "interaction_mode": str(
            getattr(
                adapter,
                "interaction_mode",
                "conversation" if execution.semantic_streaming else "activity",
            )
        ),
        "streaming_interaction": execution.semantic_streaming,
        "steering_supported": execution.provider_native_steering,
        "control_mode": str(
            getattr(
                adapter,
                "control_mode",
                "live_steering" if execution.provider_native_steering else "none",
            )
        ),
        "transport": str(getattr(adapter, "transport", "")),
        "interactive_pty": execution.interactive_pty,
        "session_resume": execution.session_resume,
    }


def candidate_is_healthy(
    *,
    adapter: AgentAdapter,
    provider_health: ProviderHealthStore,
    execution_health: ExecutionHealthStore,
) -> bool:
    identity = execution_identity_of(adapter)
    return (
        provider_health.get(identity.provider_id).is_available
        and execution_health.first_unavailable(identity) is None
    )


def mark_candidate_available(
    *, adapter: AgentAdapter, provider_health: ProviderHealthStore,
    execution_health: ExecutionHealthStore,
) -> None:
    """Clear all health dimensions after a successful candidate execution."""

    identity = execution_identity_of(adapter)
    provider_health.mark_available(identity.provider_id)
    execution_health.mark_identity_available(identity)


def record_candidate_failure_health(
    *, adapter: AgentAdapter, classification: str, detail: str,
    retry_after_seconds: float | None, persistent: bool,
    provider_health: ProviderHealthStore, execution_health: ExecutionHealthStore,
    health_dimension_hint: str = "",
):
    """Record a failure on its narrowest execution dimension and legacy projection."""

    identity = execution_identity_of(adapter)
    dimension = failure_health_dimension(
        classification, identity, dimension_hint=health_dimension_hint
    )
    execution = None
    if dimension is not None:
        execution = execution_health.mark_failure(
            *dimension, reason=classification, detail=detail,
            retry_after_seconds=retry_after_seconds, persistent=persistent,
        )
    provider = (
        provider_health.get(identity.provider_id)
        if dimension is not None and dimension[0] == "target"
        else provider_health.mark_failure(
            identity.provider_id, reason=classification, detail=detail,
            retry_after_seconds=retry_after_seconds, persistent=persistent,
        )
    )
    return identity, provider, execution
