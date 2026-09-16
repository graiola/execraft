"""Translate process/provider failures into scheduler-facing execution errors."""

from __future__ import annotations

from typing import Any

from execraft.process import ManagedProcessTerminated

from .scheduler import (
    AgentExecutionError,
    Availability,
    availability_for_failure,
    diagnose_failure,
)


def managed_process_failure(
    exc: ManagedProcessTerminated,
) -> tuple[Availability, AgentExecutionError]:
    """Translate one supervised-process stop into availability and typed error."""

    signal = exc.signal
    availability = (
        availability_for_failure(signal.category)
        if signal.persistent
        else Availability.AVAILABLE
    )
    error = AgentExecutionError(
        signal.summary,
        classification=signal.category,
        retry_after_seconds=signal.retry_after_seconds,
        persistent=signal.persistent,
        artifact_payload={
            "signal": {
                "category": signal.category,
                "summary": signal.summary,
                "retry_after_seconds": signal.retry_after_seconds,
                "stream": signal.stream,
                "excerpt": signal.excerpt,
            },
            "stdout": exc.stdout,
            "stderr": exc.stderr,
        },
    )
    return availability, error


def provider_failure(
    adapter: str,
    message: str,
    *,
    artifact_payload: dict[str, Any] | None = None,
) -> tuple[Availability, AgentExecutionError]:
    """Translate provider-authored failure text into availability and typed error."""

    diagnosis = diagnose_failure({"adapter": adapter, "message": message})
    return (
        availability_for_failure(diagnosis.category),
        AgentExecutionError(
            message,
            classification=diagnosis.category,
            retry_after_seconds=diagnosis.retry_after_seconds,
            persistent=diagnosis.persistent,
            artifact_payload=artifact_payload,
        ),
    )
