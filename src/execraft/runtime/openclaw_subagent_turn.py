"""Turn-scoped admission and authoritative guard for sub-agent delegation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping

from .openclaw_subagents import delegating_agent_id, specialist_agent_id
from .subagent_policy import (
    SubagentProfilePolicy,
    SubagentUseCase,
    infer_subagent_use_case,
    request_has_parallelism,
)


@dataclass(frozen=True)
class OpenClawSubagentTurn:
    admitted: bool
    reason: str
    use_case: str = ""
    specialist_agent_id: str = ""

    def telemetry(self) -> dict[str, object]:
        return {
            "subagents": {
                "enabled_for_turn": self.admitted,
                "reason": self.reason,
                "use_case": self.use_case,
                "specialist_agent_id": self.specialist_agent_id,
            }
        }


def bind_openclaw_subagent_turn(
    client: Any, security_turn: Any, turn: OpenClawSubagentTurn
) -> None:
    """Bind the specialist to the exact authorized task workspace.

    Sub-agent delegation is managed-Gateway-only, so the public ``agents.update`` surface is an
    Execraft-owned projection boundary.  The child receives exactly the same
    resolved workspace as its parent; it never gets a broader project/control
    root merely because delegation is enabled.
    """

    if not turn.admitted or not turn.specialist_agent_id:
        return
    client.request(
        "agents.update",
        {
            "agentId": turn.specialist_agent_id,
            "workspace": str(security_turn.workspace),
        },
    )


def prepare_openclaw_subagent_turn(
    security_turn: Any,
    profile: Any,
    request: Any,
    policy: SubagentProfilePolicy | None,
) -> tuple[Any, OpenClawSubagentTurn]:
    """Select a dedicated delegation parent without weakening ordinary agents."""

    if policy is None or not policy.enabled:
        return security_turn, OpenClawSubagentTurn(False, "disabled")
    if request_has_parallelism(request):
        return security_turn, OpenClawSubagentTurn(False, "execraft_parallelism_active")
    metadata = getattr(request, "metadata", {})
    metadata_map = metadata if isinstance(metadata, Mapping) else {}
    use_case = infer_subagent_use_case(
        capability=str(getattr(request, "capability", "")),
        stage=str(getattr(request, "stage", "")),
        metadata=metadata_map,
    )
    if use_case not in policy.allowed_use_cases:
        return security_turn, OpenClawSubagentTurn(
            False, "use_case_not_allowed", use_case=use_case.value
        )

    handoff = security_turn.request.handoff
    context = dict(getattr(handoff, "execution_context", {}) or {})
    child_id = specialist_agent_id(profile.id)
    context["openclaw_subagents"] = {
        "schema_version": 1,
        "enabled": True,
        "use_case": use_case.value,
        "specialist_agent_id": child_id,
        "max_spawn_depth": 1,
        "max_concurrent": policy.max_concurrent,
        "max_children_per_parent": policy.max_children_per_parent,
        "run_timeout_seconds": policy.run_timeout_seconds,
        "max_input_tokens": policy.max_input_tokens,
        "max_output_tokens": policy.max_output_tokens,
        "max_estimated_cost_usd": policy.max_estimated_cost_usd,
        "allowed_tools": list(policy.allowed_tools),
        "instructions": (
            "Delegation is optional and bounded. Use sessions_spawn only for the named "
            "specialist agent and always provide its explicit agentId. Delegate only focused "
            "read/analysis work for this use case. The child must not create or replan Work Packages, "
            "change Execraft workflow state, make "
            "integration decisions, or recursively delegate. Wait for any delegated child result "
            "before finalizing this parent turn. The parent remains responsible for the final "
            "Execraft output contract."
        ),
    }
    projected_handoff = replace(handoff, execution_context=context)
    continuation = getattr(security_turn.request, "continuation_handoff", None)
    projected_continuation = None
    if continuation is not None:
        continuation_context = dict(getattr(continuation, "execution_context", {}) or {})
        continuation_context["openclaw_subagents"] = dict(context["openclaw_subagents"])
        projected_continuation = replace(
            continuation, execution_context=continuation_context
        )
    projected_request = replace(
        security_turn.request,
        handoff=projected_handoff,
        continuation_handoff=projected_continuation,
    )
    read_only = bool(getattr(handoff, "read_only", False))
    projected_security = replace(
        security_turn,
        request=projected_request,
        agent_id=delegating_agent_id(profile.id, read_only=read_only),
    )
    return projected_security, OpenClawSubagentTurn(
        True,
        "admitted",
        use_case=use_case.value,
        specialist_agent_id=child_id,
    )
