"""One bounded OpenClaw Gateway turn, separated from the runtime facade.

The facade owns lifecycle/cancellation state.  This module owns the protocol
plumbing and telemetry assembly so the public runtime class stays cohesive and
within architecture complexity budgets.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .contracts import RuntimeExecutionRequest
from .openclaw_approvals import OpenClawApprovalBridge
from .openclaw_run_events import OpenClawRunEventCollector
from .openclaw_session_rpc import session_snapshot
from .openclaw_subagent_budget import OpenClawSubagentBudgetMonitor
from .openclaw_subagent_turn import bind_openclaw_subagent_turn
from .openclaw_security_turn import bind_openclaw_security_turn
from .openclaw_skill_runtime import OpenClawSkillTurn
from .openclaw_turn_optimization import prepare_openclaw_turn


@dataclass(frozen=True)
class ActiveOpenClawRun:
    execution_id: str
    run_id: str
    session_key: str


@dataclass(frozen=True)
class GatewayTurnOutcome:
    active: ActiveOpenClawRun
    wait_payload: Mapping[str, Any]
    final_payload: Mapping[str, Any]
    post_snapshot: Any
    events: OpenClawRunEventCollector
    approvals: OpenClawApprovalBridge
    subagent_budget: OpenClawSubagentBudgetMonitor
    turn: Any


def run_gateway_turn(
    *,
    host: Any,
    runtime_config: Any,
    runtime_request: RuntimeExecutionRequest,
    security_turn: Any,
    subagent_turn: Any,
    subagent_policy: Any,
    skill_turn: OpenClawSkillTurn,
    agent_id: str,
    execution_id: str,
    timeout: float,
    cold_session_key: str,
    agent_params: Callable[..., Mapping[str, Any]],
    set_active: Callable[[ActiveOpenClawRun], None],
    clear_active: Callable[[str], None],
) -> GatewayTurnOutcome:
    with host.lease() as service:
        bind_openclaw_security_turn(service.client, runtime_config, security_turn)
        bind_openclaw_subagent_turn(service.client, security_turn, subagent_turn)
        turn = prepare_openclaw_turn(
            client=service.client,
            request=runtime_request,
            profile_id=agent_id,
            cold_session_key=cold_session_key,
            skill_turn=skill_turn,
            policy=runtime_config.openclaw.optimization,
        )
        params = agent_params(
            prompt=turn.prompt,
            agent_id=agent_id,
            session_key=turn.session_key,
            timeout=timeout,
        )
        events = OpenClawRunEventCollector()
        approvals = OpenClawApprovalBridge(
            client=service.client,
            request=runtime_request,
            agent_id=agent_id,
            session_key=turn.session_key,
        )
        budget = OpenClawSubagentBudgetMonitor(
            client=service.client,
            policy=subagent_policy if subagent_turn.admitted else None,
        )
        for handler in (events, approvals, budget):
            service.client.add_event_handler(handler)
        try:
            accepted = service.client.start_agent(
                params,
                timeout_seconds=min(timeout, float(service.options.request_timeout_seconds)),
                idempotency_key=execution_id,
            )
            events.bind(accepted.run_id)
            budget.bind_parent(accepted.run_id, accepted.session_key or turn.session_key)
            active = ActiveOpenClawRun(
                execution_id,
                accepted.run_id,
                accepted.session_key or turn.session_key,
            )
            set_active(active)
            try:
                wait_payload = service.client.wait_agent_run(
                    accepted.run_id, timeout_seconds=timeout
                )
                final_payload = accepted.wait_final(
                    timeout_seconds=min(
                        5.0, max(1.0, float(service.options.request_timeout_seconds))
                    )
                )
                try:
                    post_snapshot = session_snapshot(service.client, active.session_key)
                except Exception:
                    post_snapshot = None
            finally:
                clear_active(execution_id)
        finally:
            for handler in (budget, approvals, events):
                service.client.remove_event_handler(handler)
            budget.finish()
            approvals.finish()
    return GatewayTurnOutcome(
        active=active,
        wait_payload=wait_payload,
        final_payload=final_payload,
        post_snapshot=post_snapshot,
        events=events,
        approvals=approvals,
        subagent_budget=budget,
        turn=turn,
    )


def build_gateway_runtime_metadata(
    *,
    runtime_config: Any,
    request: RuntimeExecutionRequest,
    security_turn: Any,
    subagent_turn: Any,
    skill_turn: OpenClawSkillTurn,
    agent_id: str,
    status: str,
    outcome: GatewayTurnOutcome,
    cleanup_warning: str,
    extract_provider_model: Callable[[Mapping[str, Any]], tuple[str, str]],
    extract_session_id: Callable[[Mapping[str, Any]], str],
) -> dict[str, Any]:
    metadata = {
        "backend": "gateway",
        "agent_id": agent_id,
        "run_id": outcome.active.run_id,
        "session_key": outcome.active.session_key,
        "status": status,
        "context_epoch": request.context_epoch,
        "event_count": outcome.events.count,
        "prompt_bytes": len(outcome.turn.prompt.encode("utf-8")),
    }
    for telemetry in (
        security_turn.telemetry(),
        subagent_turn.telemetry(),
        outcome.turn.telemetry(),
        outcome.approvals.telemetry(),
        outcome.subagent_budget.telemetry(),
        skill_turn.telemetry(
            prompt=outcome.turn.prompt,
            inline_prompt=outcome.turn.inline_prompt,
            events=outcome.events.snapshot(),
        ),
    ):
        metadata.update(telemetry)
    metadata["session_telemetry_degraded"] = outcome.post_snapshot is None
    if outcome.post_snapshot is not None:
        metadata["post_session"] = outcome.post_snapshot.as_mapping()
    optimization = runtime_config.openclaw.optimization
    metadata["cache_retention"] = optimization.cache_retention
    metadata["context_pruning_mode"] = optimization.context_pruning_mode
    if cleanup_warning:
        metadata["cleanup_warning"] = cleanup_warning
    provider, model = extract_provider_model(outcome.final_payload)
    if provider:
        metadata["provider"] = provider
    if model:
        metadata["model"] = model
    session_id = extract_session_id(outcome.final_payload)
    if session_id:
        metadata["openclaw_session_id"] = session_id
    return metadata
