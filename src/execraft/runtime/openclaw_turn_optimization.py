"""Prepare one optimized OpenClaw turn without moving workflow authority into runtime state."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Protocol

from execraft.orchestrate.context_budget import estimate_tokens
from execraft.runtime.contracts import RuntimeExecutionRequest
from execraft.runtime_config import OpenClawOptimizationOptions

from .openclaw_optimization import (
    build_authoritative_guard_handoff,
    compaction_result_reusable,
    decide_continuation_optimization,
)
from .openclaw_session_rpc import (
    OpenClawCompactionResult,
    OpenClawSessionSnapshot,
    compact_session,
)
from .openclaw_skill_runtime import OpenClawSkillTurn


class OpenClawSessionClient(Protocol):
    def describe_session(self, session_key: str) -> Mapping[str, Any] | None: ...

    def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
        idempotency_key: str = "",
    ) -> Any: ...


@dataclass(frozen=True)
class _PromptVolume:
    actual_estimated_tokens: int
    cold_bytes: int
    cold_estimated_tokens: int
    avoided_bytes: int
    avoided_estimated_tokens: int


@dataclass(frozen=True)
class PreparedOpenClawTurn:
    session_key: str
    prompt: str
    inline_prompt: str
    reused: bool = False
    cold_reconstruction: bool = False
    authoritative_guard: bool = False
    decision_reason: str = "cold_start"
    guarded_compaction_count: int = 0
    delta_prompt_bytes: int = 0
    prompt_estimated_tokens: int = 0
    cold_equivalent_prompt_bytes: int = 0
    cold_equivalent_prompt_estimated_tokens: int = 0
    repeated_context_avoided_bytes: int = 0
    repeated_context_avoided_estimated_tokens: int = 0
    pre_snapshot: OpenClawSessionSnapshot | None = None
    compaction: OpenClawCompactionResult = field(
        default_factory=lambda: OpenClawCompactionResult(False, False)
    )

    def telemetry(self) -> dict[str, Any]:
        return {
            "cold_start": not self.reused,
            "session_reused": self.reused,
            "cold_reconstruction": self.cold_reconstruction,
            "continuation_decision": self.decision_reason,
            "authoritative_guard": self.authoritative_guard,
            "guarded_compaction_count": self.guarded_compaction_count,
            "delta_prompt_bytes": self.delta_prompt_bytes,
            "prompt_estimated_tokens": self.prompt_estimated_tokens,
            "cold_equivalent_prompt_bytes": self.cold_equivalent_prompt_bytes,
            "cold_equivalent_prompt_estimated_tokens": (
                self.cold_equivalent_prompt_estimated_tokens
            ),
            "repeated_context_avoided_bytes": self.repeated_context_avoided_bytes,
            "repeated_context_avoided_estimated_tokens": (
                self.repeated_context_avoided_estimated_tokens
            ),
            "pre_session": self.pre_snapshot.as_mapping() if self.pre_snapshot else {},
            "compaction": self.compaction.as_mapping(),
        }


def prepare_openclaw_turn(
    *,
    client: OpenClawSessionClient,
    request: RuntimeExecutionRequest,
    profile_id: str,
    cold_session_key: str,
    skill_turn: OpenClawSkillTurn,
    policy: OpenClawOptimizationOptions,
) -> PreparedOpenClawTurn:
    """Choose cold/delta/guarded continuation and compact only with proof."""

    if request.session_ref is None:
        prompt, inline = skill_turn.render_prompt(request.handoff)
        return _cold_turn(cold_session_key, prompt, inline)

    session_key = request.session_ref.session_id
    row = client.describe_session(session_key)
    described_agent = str(row.get("agentId", "")).strip() if row else ""
    if row is None or (described_agent and described_agent != profile_id):
        return _cold_reconstruction(
            skill_turn, request, cold_session_key, reason="missing_session"
        )

    snapshot = OpenClawSessionSnapshot.from_row(session_key, row)
    cold_prompt, _cold_inline = skill_turn.render_prompt(request.handoff)
    delta = request.continuation_handoff or request.handoff
    delta_prompt, inline_delta = skill_turn.render_prompt(delta)
    binding = request.metadata.get("runtime_continuation_binding")
    binding_meta = dict(binding) if isinstance(binding, Mapping) else {}
    decision = decide_continuation_optimization(
        snapshot=snapshot,
        policy=policy,
        binding_metadata=binding_meta,
        delta_prompt_bytes=len(delta_prompt.encode("utf-8")),
    )
    if not decision.reuse:
        return _cold_reconstruction(
            skill_turn, request, cold_session_key, reason=decision.reason
        )

    compaction = OpenClawCompactionResult(False, False)
    post_compaction_snapshot = snapshot
    guard = decision.authoritative_guard
    if decision.compact:
        compaction = compact_session(
            client,
            session_key,
            agent_id=profile_id,
            timeout_seconds=policy.compaction_timeout_seconds,
        )
        if not compaction.compacted or not compaction_result_reusable(
            tokens_after=compaction.tokens_after,
            snapshot=snapshot,
            policy=policy,
        ):
            return _cold_reconstruction(
                skill_turn,
                request,
                cold_session_key,
                reason=f"compaction_unusable:{decision.reason}",
                compaction=compaction,
            )
        row_after = client.describe_session(session_key)
        if row_after is None:
            return _cold_reconstruction(
                skill_turn,
                request,
                cold_session_key,
                reason=f"compaction_unverified:{decision.reason}",
                compaction=compaction,
            )
        post_compaction_snapshot = OpenClawSessionSnapshot.from_row(
            session_key, row_after
        )
        guard = True

    guarded_count = _binding_guarded_count(binding_meta)
    effective = delta
    if guard:
        if not policy.post_compaction_guard:
            return _cold_reconstruction(
                skill_turn,
                request,
                cold_session_key,
                reason="post_compaction_guard_disabled",
                compaction=compaction,
            )
        effective = build_authoritative_guard_handoff(
            request.handoff,
            delta,
            epoch=request.context_epoch,
            reason=decision.reason,
        )
        guarded_count = post_compaction_snapshot.compaction_count

    prompt, inline_prompt = (
        skill_turn.render_prompt(effective)
        if effective is not delta
        else (delta_prompt, inline_delta)
    )
    volume = _continuation_volume(prompt, cold_prompt)
    return PreparedOpenClawTurn(
        session_key=session_key,
        prompt=prompt,
        inline_prompt=inline_prompt,
        reused=True,
        authoritative_guard=guard,
        decision_reason=decision.reason,
        guarded_compaction_count=guarded_count,
        delta_prompt_bytes=len(delta_prompt.encode("utf-8")),
        prompt_estimated_tokens=volume.actual_estimated_tokens,
        cold_equivalent_prompt_bytes=volume.cold_bytes,
        cold_equivalent_prompt_estimated_tokens=volume.cold_estimated_tokens,
        repeated_context_avoided_bytes=volume.avoided_bytes,
        repeated_context_avoided_estimated_tokens=volume.avoided_estimated_tokens,
        pre_snapshot=post_compaction_snapshot,
        compaction=compaction,
    )


def _cold_reconstruction(
    skill_turn: OpenClawSkillTurn,
    request: RuntimeExecutionRequest,
    session_key: str,
    *,
    reason: str,
    compaction: OpenClawCompactionResult | None = None,
) -> PreparedOpenClawTurn:
    prompt, inline = skill_turn.render_prompt(request.handoff)
    cold = _cold_turn(session_key, prompt, inline)
    return replace(
        cold,
        cold_reconstruction=True,
        decision_reason=reason,
        compaction=compaction or OpenClawCompactionResult(False, False),
    )


def _cold_turn(session_key: str, prompt: str, inline_prompt: str) -> PreparedOpenClawTurn:
    prompt_bytes = len(prompt.encode("utf-8"))
    prompt_tokens = estimate_tokens(prompt)
    return PreparedOpenClawTurn(
        session_key=session_key,
        prompt=prompt,
        inline_prompt=inline_prompt,
        prompt_estimated_tokens=prompt_tokens,
        cold_equivalent_prompt_bytes=prompt_bytes,
        cold_equivalent_prompt_estimated_tokens=prompt_tokens,
    )


def _continuation_volume(prompt: str, cold_prompt: str) -> _PromptVolume:
    cold_bytes = len(cold_prompt.encode("utf-8"))
    cold_tokens = estimate_tokens(cold_prompt)
    actual_bytes = len(prompt.encode("utf-8"))
    actual_tokens = estimate_tokens(prompt)
    return _PromptVolume(
        actual_estimated_tokens=actual_tokens,
        cold_bytes=cold_bytes,
        cold_estimated_tokens=cold_tokens,
        avoided_bytes=max(0, cold_bytes - actual_bytes),
        avoided_estimated_tokens=max(0, cold_tokens - actual_tokens),
    )


def _binding_guarded_count(metadata: Mapping[str, Any]) -> int:
    value = metadata.get("guarded_compaction_count", 0)
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0
