"""Best-effort live budget enforcement for bounded OpenClaw child runs.

OpenClaw owns the child tool loop. Execraft registers this observer on the public
Gateway event stream and cancels an identified child run as soon as observable
usage exceeds the configured input/output/cost budget.  If the Gateway does not
expose per-child usage, telemetry is explicitly degraded and the sub-agent support
check cannot pass; missing telemetry is never interpreted as zero cost.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Mapping

from .openclaw_protocol import GatewayEvent
from .subagent_policy import SubagentProfilePolicy


@dataclass
class _Child:
    run_id: str = ""
    session_key: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    usage_seen: bool = False
    cancelled: bool = False
    exceeded: str = ""


class OpenClawSubagentBudgetMonitor:
    """Track and cancel child runs that exceed the Execraft sub-agent budget."""

    def __init__(self, *, client: Any, policy: SubagentProfilePolicy | None) -> None:
        self._client = client
        self._policy = policy
        self._children: dict[str, _Child] = {}
        self._lock = threading.RLock()
        self._active = bool(policy is not None and policy.enabled)
        self._parent_run_id = ""
        self._parent_session_key = ""
        self._foreign_events = 0

    def bind_parent(self, run_id: str, session_key: str = "") -> None:
        """Bind accounting to one accepted parent invocation."""

        with self._lock:
            self._parent_run_id = str(run_id).strip()
            self._parent_session_key = str(session_key).strip()

    def __call__(self, event: GatewayEvent) -> None:
        if not self._active or not isinstance(event.payload, Mapping):
            return
        with self._lock:
            if not self._belongs_to_parent(event.payload):
                self._foreign_events += 1
                return
            for mapping in _walk_mappings(event.payload, max_nodes=96):
                child = self._observe_mapping(mapping)
                if child is not None:
                    self._enforce(child)

    def telemetry(self) -> dict[str, object]:
        if not self._active:
            return {"subagent_usage": {"enabled": False}}
        with self._lock:
            children = tuple(self._children.values())
        return {
            "subagent_usage": {
                "enabled": True,
                "spawn_count": len(children),
                "input_tokens": sum(child.input_tokens for child in children),
                "output_tokens": sum(child.output_tokens for child in children),
                "total_tokens": sum(child.total_tokens for child in children),
                "estimated_cost_usd": round(
                    sum(child.estimated_cost_usd for child in children), 8
                ),
                "attribution_complete": not children
                or all(child.usage_seen for child in children),
                "cancelled_for_budget": sum(child.cancelled for child in children),
                "foreign_events_ignored": self._foreign_events,
                "budget_exceeded": [
                    child.exceeded for child in children if child.exceeded
                ],
                "children": [
                    {
                        "run_id": child.run_id,
                        "session_key": child.session_key,
                        "input_tokens": child.input_tokens,
                        "output_tokens": child.output_tokens,
                        "total_tokens": child.total_tokens,
                        "estimated_cost_usd": child.estimated_cost_usd,
                        "usage_seen": child.usage_seen,
                        "cancelled": child.cancelled,
                        "exceeded": child.exceeded,
                    }
                    for child in children[:16]
                ],
            }
        }

    def finish(self) -> None:
        """Reserved for symmetry with other per-turn event bridges."""


    def _belongs_to_parent(self, payload: Mapping[str, Any]) -> bool:
        """Reject an event when it positively identifies a different parent.

        Some Gateway event versions put the parent run/session on the outer
        payload while child receipts are nested.  When no parent identity is
        exposed we cannot prove foreign ownership, so the event remains
        observable but attribution is still reported as degraded attribution.
        """

        if not self._parent_run_id and not self._parent_session_key:
            return True
        run_id = _text(payload, "parentRunId", "runId", "run_id")
        session_key = _text(
            payload, "parentSessionKey", "sessionKey", "session_key"
        )
        if run_id and self._parent_run_id and run_id != self._parent_run_id:
            return False
        if (
            session_key
            and self._parent_session_key
            and session_key != self._parent_session_key
        ):
            return False
        return True

    def _observe_mapping(self, value: Mapping[str, Any]) -> _Child | None:
        child_session = _text(value, "childSessionKey")
        child_run = _text(value, "childRunId")
        if not child_session and not child_run:
            return None
        key, child = self._find_child(child_session=child_session, child_run=child_run)
        if child is None:
            key = child_session or f"run:{child_run}"
            child = _Child(run_id=child_run, session_key=child_session)
            self._children[key] = child
            policy = self._policy
            if policy is not None and len(self._children) > policy.max_children_per_parent:
                child.exceeded = "max_children_per_parent"
        if child_run:
            child.run_id = child_run
        if child_session:
            child.session_key = child_session
        usage = value.get("usage")
        if isinstance(usage, Mapping):
            child.usage_seen = True
            child.input_tokens = max(
                child.input_tokens,
                _token(
                    usage,
                    "input_tokens",
                    "inputTokens",
                    "prompt_tokens",
                    "promptTokens",
                    "input",
                ),
            )
            child.output_tokens = max(
                child.output_tokens,
                _token(
                    usage,
                    "output_tokens",
                    "outputTokens",
                    "completion_tokens",
                    "completionTokens",
                    "output",
                ),
            )
            total = _token(usage, "total_tokens", "totalTokens", "total")
            child.total_tokens = max(
                child.total_tokens,
                total or (child.input_tokens + child.output_tokens),
            )
            child.estimated_cost_usd = max(
                child.estimated_cost_usd,
                _number(usage, "estimated_cost_usd", "estimatedCostUsd", "cost"),
            )
        return child

    def _find_child(self, *, child_session: str, child_run: str) -> tuple[str, _Child | None]:
        if child_session and child_session in self._children:
            return child_session, self._children[child_session]
        run_key = f"run:{child_run}" if child_run else ""
        if run_key and run_key in self._children:
            return run_key, self._children[run_key]
        for key, child in self._children.items():
            if child_session and child.session_key == child_session:
                return key, child
            if child_run and child.run_id == child_run:
                return key, child
        return "", None

    def _enforce(self, child: _Child) -> None:
        policy = self._policy
        if policy is None or child.cancelled or not child.run_id:
            return
        exceeded = child.exceeded
        if not exceeded:
            if child.input_tokens > policy.max_input_tokens:
                exceeded = "input_tokens"
            elif child.output_tokens > policy.max_output_tokens:
                exceeded = "output_tokens"
            elif child.estimated_cost_usd > policy.max_estimated_cost_usd:
                exceeded = "estimated_cost_usd"
        if not exceeded:
            return
        child.exceeded = exceeded
        try:
            self._client.cancel_run(child.run_id, session_key=child.session_key)
            child.cancelled = True
        except Exception:
            # The parent run must continue to the normal Execraft recovery boundary;
            # telemetry records the attempted violation and incomplete cancellation.
            child.cancelled = False


def _walk_mappings(value: Any, *, max_nodes: int):
    pending = [value]
    seen = 0
    while pending and seen < max_nodes:
        current = pending.pop()
        seen += 1
        if isinstance(current, Mapping):
            yield current
            pending.extend(current.values())
        elif isinstance(current, (list, tuple)):
            pending.extend(current)


def _text(value: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        text = str(value.get(key, "")).strip()
        if text:
            return text[:256]
    return ""


def _token(value: Mapping[str, Any], *keys: str) -> int:
    for key in keys:
        raw = value.get(key)
        if isinstance(raw, bool):
            continue
        if isinstance(raw, (int, float)):
            return max(0, int(raw))
    return 0


def _number(value: Mapping[str, Any], *keys: str) -> float:
    for key in keys:
        raw = value.get(key)
        if isinstance(raw, bool):
            continue
        if isinstance(raw, (int, float)):
            return max(0.0, float(raw))
    return 0.0
