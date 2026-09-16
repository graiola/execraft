"""OpenClaw continuation, compaction, and authoritative-guard policy."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Mapping

from execraft.orchestrate.scheduler import StructuredHandoff
from execraft.runtime_config import OpenClawOptimizationOptions

from .openclaw_session_rpc import OpenClawSessionSnapshot


@dataclass(frozen=True)
class ContinuationOptimizationDecision:
    reuse: bool
    compact: bool = False
    authoritative_guard: bool = False
    reason: str = "reuse"


def decide_continuation_optimization(
    *,
    snapshot: OpenClawSessionSnapshot | None,
    policy: OpenClawOptimizationOptions,
    binding_metadata: Mapping[str, Any],
    delta_prompt_bytes: int,
) -> ContinuationOptimizationDecision:
    if snapshot is None:
        return ContinuationOptimizationDecision(False, reason="missing_session")
    reuse_count = _int(binding_metadata.get("reuse_count"))
    if reuse_count >= policy.max_session_reuse_turns:
        return ContinuationOptimizationDecision(False, reason="reuse_limit")
    age = _age_seconds(binding_metadata.get("updated_at"))
    if age is not None and age > policy.max_session_age_seconds:
        return ContinuationOptimizationDecision(False, reason="stale_session")

    guarded_count = _int(binding_metadata.get("guarded_compaction_count"))
    if snapshot.compaction_count > guarded_count:
        if not policy.post_compaction_guard:
            return ContinuationOptimizationDecision(False, reason="unguarded_compaction")
        return ContinuationOptimizationDecision(
            True, authoritative_guard=True, reason="observed_compaction"
        )

    if delta_prompt_bytes > policy.max_delta_prompt_bytes:
        if not policy.proactive_compaction:
            return ContinuationOptimizationDecision(False, reason="delta_too_large")
        return ContinuationOptimizationDecision(True, compact=True, reason="delta_too_large")

    if _context_pressure_exceeded(snapshot, policy):
        if not policy.proactive_compaction:
            return ContinuationOptimizationDecision(False, reason="context_pressure")
        return ContinuationOptimizationDecision(True, compact=True, reason="context_pressure")
    return ContinuationOptimizationDecision(True)


def compaction_result_reusable(
    *,
    tokens_after: int | None,
    snapshot: OpenClawSessionSnapshot,
    policy: OpenClawOptimizationOptions,
) -> bool:
    """Require positive post-compaction evidence before continuing a pressured session."""

    if tokens_after is None:
        return False
    if policy.max_context_tokens and tokens_after >= policy.max_context_tokens:
        return False
    if snapshot.context_window:
        return (tokens_after / snapshot.context_window) < policy.context_pressure_ratio
    return True


def build_authoritative_guard_handoff(
    current: StructuredHandoff,
    delta: StructuredHandoff,
    *,
    epoch: str,
    reason: str,
) -> StructuredHandoff:
    """Reassert compact authoritative state after a runtime compaction boundary."""

    context = dict(delta.execution_context)
    continuation = context.get("runtime_continuation")
    continuation_map = dict(continuation) if isinstance(continuation, Mapping) else {}
    continuation_map["authoritative_guard"] = {
        "reason": reason,
        "context_epoch": epoch,
        "requirements_sha256": _digest(current.requirements),
        "acceptance_sha256": _digest(current.acceptance_criteria),
        "output_schema_sha256": _digest(current.expected_output_schema),
        "reasserted": True,
    }
    context["runtime_continuation"] = continuation_map
    return replace(
        delta,
        requirements=list(current.requirements),
        acceptance_criteria=list(current.acceptance_criteria),
        execution_context=context,
    )


def _context_pressure_exceeded(
    snapshot: OpenClawSessionSnapshot, policy: OpenClawOptimizationOptions
) -> bool:
    if policy.max_context_tokens and snapshot.context_tokens is not None:
        if snapshot.context_tokens >= policy.max_context_tokens:
            return True
    pressure = snapshot.context_pressure
    return pressure is not None and pressure >= policy.context_pressure_ratio


def _age_seconds(raw: object) -> float | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - value).total_seconds())


def _int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _digest(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
