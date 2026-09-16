"""Runtime-neutral dispatch helpers shared by serial and parallel execution paths.

The orchestration state machine owns scheduling and durable workflow state.  This
module only adapts a selected candidate to the runtime execution contract while
preserving the legacy ``execute(StructuredHandoff)`` surface for in-process and
test adapters during the schema-v3 migration window.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from execraft.execution_identity import execution_identity_of
from execraft.runtime.contracts import (
    RuntimeExecutionRequest,
    RuntimeExecutionResult,
    RuntimeSessionRef,
)

from .scheduler import AgentAdapter, StructuredHandoff


@dataclass(frozen=True)
class RuntimeDispatchResult:
    """Normalized dispatch output with backwards-compatible tuple unpacking."""

    output: dict[str, Any]
    session_ref: RuntimeSessionRef | None = None
    runtime_metadata: Mapping[str, Any] = field(default_factory=dict)
    rendered_prompt: str = ""

    def __iter__(self):
        # Existing callers/tests unpack ``result, session_ref``. Keep that
        # surface while newer consumers can inspect runtime telemetry explicitly.
        yield self.output
        yield self.session_ref


def dispatch_runtime(
    adapter: AgentAdapter,
    handoff: StructuredHandoff,
    *,
    capability: str,
    package_id: str,
    stage: str,
    attempt: int = 1,
    metadata: Mapping[str, Any] | None = None,
    session_ref: RuntimeSessionRef | None = None,
    context_epoch: str = "",
    continuation_handoff: StructuredHandoff | None = None,
) -> RuntimeDispatchResult:
    """Execute one selected candidate through the normalized runtime seam.

    Native runtimes implement ``execute_runtime``.  Legacy in-process adapters
    continue to receive the handoff directly until their compatibility window
    closes.  Both paths return the same normalized output/session tuple.
    """

    identity = execution_identity_of(adapter)
    execute_runtime = getattr(adapter, "execute_runtime", None)
    runtime_session: RuntimeSessionRef | None = None
    runtime_metadata: Mapping[str, Any] = {}
    actual_prompt = ""
    if callable(execute_runtime):
        runtime_result = execute_runtime(
            RuntimeExecutionRequest(
                identity=identity,
                handoff=handoff,
                capability=capability,
                package_id=package_id,
                stage=stage,
                attempt=attempt,
                metadata=dict(metadata or {}),
                session_ref=session_ref,
                context_epoch=context_epoch,
                continuation_handoff=continuation_handoff,
            )
        )
        if not isinstance(runtime_result, RuntimeExecutionResult):
            raise RuntimeError("runtime returned an invalid normalized execution result")
        result = runtime_result.output
        runtime_session = runtime_result.session_ref
        runtime_metadata = dict(runtime_result.runtime_metadata)
        actual_prompt = runtime_result.rendered_prompt
    else:
        result = adapter.execute(handoff)
        session_resolver = getattr(adapter, "session_ref_from_result", None)
        if callable(session_resolver):
            runtime_session = session_resolver(result)
    if not isinstance(result, dict) or result.get("ok") is False:
        raise RuntimeError(f"invalid/failed agent result: {result!r}")
    return RuntimeDispatchResult(
        output=dict(result),
        session_ref=runtime_session,
        runtime_metadata=runtime_metadata,
        rendered_prompt=actual_prompt,
    )


def dispatch_parallel_candidate(
    adapter: AgentAdapter,
    *,
    handoff: StructuredHandoff,
    capability: str,
    package_id: str,
    stage: str,
    metadata: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, Exception | None, float, RuntimeSessionRef | None]:
    """Execute one parallel candidate and return a reconciliation-safe outcome."""

    started = time.monotonic()
    try:
        dispatch = dispatch_runtime(
            adapter,
            handoff,
            capability=capability,
            package_id=package_id,
            stage=stage,
            metadata=metadata,
        )
        return dispatch.output, None, time.monotonic() - started, dispatch.session_ref
    except Exception as exc:  # reconciled on the orchestrator thread
        return None, exc, time.monotonic() - started, None


def dispatch_parallel_shard_candidate(adapter: AgentAdapter, candidate):
    """Compatibility-shaped wrapper used by ``ProjectOrchestrator``."""

    return dispatch_parallel_candidate(
        adapter,
        handoff=candidate.handoff,
        capability=candidate.capability.value,
        package_id=candidate.package.id,
        stage=candidate.package.stage.value,
    )
