"""Fail-closed OpenClaw approval-event bridge.

OpenClaw approvals are remote-execution-grade authority. This module maps the
public Gateway approval events into Execraft's runtime-neutral callback contract
and resolves only explicitly supported one-shot decisions. Missing callbacks,
malformed requests, read-only turns, plugin approvals, and unknown decisions
deny. Telemetry deliberately excludes command/cwd content so secrets cannot be
copied into the invocation ledger through an approval event.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Mapping

from .contracts import RuntimeApprovalDecision, RuntimeApprovalRequest, RuntimeExecutionRequest
from .openclaw_protocol import GatewayEvent

_ALLOWED_EXEC_DECISIONS = frozenset({"allow-once", "deny"})
_APPROVAL_EVENTS = frozenset(
    {"exec.approval.requested", "plugin.approval.requested", "approval.requested"}
)


@dataclass(frozen=True)
class ApprovalBridgeRecord:
    approval_id: str
    kind: str
    decision: str
    reason: str

    def as_mapping(self) -> dict[str, str]:
        return {
            "approval_id": self.approval_id,
            "kind": self.kind,
            "decision": self.decision,
            "reason": self.reason,
        }


class OpenClawApprovalBridge:
    """Map Gateway approval events for one exact runtime turn."""

    def __init__(
        self,
        *,
        client: Any,
        request: RuntimeExecutionRequest,
        agent_id: str,
        session_key: str,
    ) -> None:
        self.client = client
        self.request = request
        self.agent_id = agent_id
        self.session_key = session_key
        self._records: list[ApprovalBridgeRecord] = []
        self._workers: set[threading.Thread] = set()
        self._resolution_errors = 0
        self._incomplete_workers = 0
        self._lock = threading.RLock()

    def __call__(self, event: GatewayEvent) -> None:
        if event.name not in _APPROVAL_EVENTS and not event.name.endswith(".approval.requested"):
            return
        payload = event.payload if isinstance(event.payload, Mapping) else {}
        if not self._belongs_to_turn(payload):
            return
        # Gateway event handlers execute on the transport reader thread. RPC
        # resolution must run elsewhere or it would wait for that same thread.
        worker = threading.Thread(
            target=self._handle_event,
            args=(event.name, dict(payload)),
            name="execraft-openclaw-approval",
            daemon=True,
        )
        with self._lock:
            self._workers.add(worker)
        worker.start()

    def finish(self, timeout: float = 5.0) -> None:
        """Boundedly join approval workers before runtime telemetry is frozen."""

        with self._lock:
            workers = tuple(self._workers)
        for worker in workers:
            worker.join(timeout=max(0.0, timeout))
        with self._lock:
            self._incomplete_workers += sum(worker.is_alive() for worker in workers)

    def _handle_event(self, event_name: str, payload: Mapping[str, Any]) -> None:
        approval_id = _approval_id(payload)
        kind = _kind(event_name, payload)
        try:
            if not approval_id or kind not in {"exec", "plugin"}:
                self._record(
                    approval_id,
                    kind or "unknown",
                    "deny",
                    "malformed or unsupported approval request",
                )
                return
            if kind != "exec":
                self._resolve(kind, approval_id, "deny")
                self._record(
                    approval_id,
                    kind,
                    "deny",
                    "plugin approvals are not supported by the managed runtime policy",
                )
                return

            approval = _runtime_approval_request(
                payload, approval_id=approval_id, kind=kind
            )
            decision, reason = self._decision(approval)
            self._resolve(kind, approval_id, decision.decision)
            self._record(approval_id, kind, decision.decision, reason or decision.reason)
        except Exception as exc:
            # Failure to resolve must never turn into an implicit allow. Keep a
            # sanitized diagnostic and let OpenClaw's own approval timeout/deny
            # policy stop the privileged operation.
            with self._lock:
                self._resolution_errors += 1
            self._record(
                approval_id,
                kind or "unknown",
                "deny",
                f"approval resolution failed: {type(exc).__name__}",
            )
        finally:
            with self._lock:
                self._workers.discard(threading.current_thread())

    def telemetry(self) -> dict[str, object]:
        with self._lock:
            records = tuple(self._records)
            resolution_errors = self._resolution_errors
            incomplete_workers = self._incomplete_workers
        return {
            "approval_event_count": len(records),
            "approval_denied_count": sum(item.decision == "deny" for item in records),
            "approval_allowed_once_count": sum(
                item.decision == "allow-once" for item in records
            ),
            "approval_resolution_error_count": resolution_errors,
            "approval_worker_incomplete_count": incomplete_workers,
            "approval_records": [item.as_mapping() for item in records],
        }

    def _decision(
        self, approval: RuntimeApprovalRequest
    ) -> tuple[RuntimeApprovalDecision, str]:
        if bool(getattr(self.request.handoff, "read_only", False)):
            return RuntimeApprovalDecision("deny", "read-only turn"), "read-only-turn"
        handler = self.request.approval_handler
        if handler is None:
            return RuntimeApprovalDecision("deny", "no operator approval handler"), "no-handler"
        try:
            decision = handler.decide(approval)
        except Exception as exc:
            return (
                RuntimeApprovalDecision("deny", "approval handler failed"),
                f"handler-error:{type(exc).__name__}",
            )
        if decision.decision not in _ALLOWED_EXEC_DECISIONS:
            return RuntimeApprovalDecision("deny", "unsupported approval decision"), "unsupported-decision"
        if decision.decision not in approval.allowed_decisions:
            return RuntimeApprovalDecision("deny", "decision not offered by Gateway"), "decision-not-offered"
        # Never persist a handler-supplied free-form reason. It may contain a
        # command, path, ticket text, or credential copied from an operator UI.
        reason_code = "operator-allow-once" if decision.decision == "allow-once" else "operator-deny"
        return decision, reason_code

    def _resolve(self, kind: str, approval_id: str, decision: str) -> None:
        method, include_kind = _approval_resolve_method(self.client, kind)
        params: dict[str, str] = {"id": approval_id, "decision": decision}
        if include_kind:
            params["kind"] = kind
        self.client.request(method, params)

    def _belongs_to_turn(self, payload: Mapping[str, Any]) -> bool:
        agent_id = str(payload.get("agentId", payload.get("agent_id", ""))).strip()
        session_key = str(
            payload.get("sessionKey", payload.get("session_key", ""))
        ).strip()
        if agent_id and agent_id != self.agent_id:
            return False
        if session_key and session_key != self.session_key:
            return False
        return True

    def _record(self, approval_id: str, kind: str, decision: str, reason: str) -> None:
        with self._lock:
            record = ApprovalBridgeRecord(approval_id, kind, decision, reason)
            if not self._records or self._records[-1] != record:
                self._records.append(record)


def _approval_resolve_method(client: Any, kind: str) -> tuple[str, bool]:
    """Choose a public approval RPC supported by the connected Gateway.

    Newer OpenClaw releases expose the kind-aware ``approval.resolve`` service
    while the stable exec/plugin-specific methods remain public compatibility
    surfaces.  Feature negotiation prevents Execraft from assuming one release's
    spelling when an older pinned Gateway advertises only the other.
    """

    hello = getattr(client, "hello", None)
    features = getattr(hello, "features", None)
    methods = frozenset(getattr(features, "methods", ()) or ())
    if "approval.resolve" in methods:
        return "approval.resolve", True
    specific = f"{kind}.approval.resolve"
    if specific in methods:
        return specific, False
    # No advertised resolver means the privileged action must remain pending
    # until OpenClaw itself times it out/denies it.  Raising here is fail-closed.
    raise RuntimeError(f"OpenClaw Gateway advertises no approval resolver for {kind!r}")


def _runtime_approval_request(
    payload: Mapping[str, Any], *, approval_id: str, kind: str
) -> RuntimeApprovalRequest:
    command = str(payload.get("command", payload.get("rawCommand", ""))).strip()
    cwd = str(payload.get("cwd", "")).strip()
    raw_allowed = payload.get("allowedDecisions", ["allow-once", "deny"])
    values = raw_allowed if isinstance(raw_allowed, (list, tuple, set)) else ()
    allowed_decisions = tuple(
        item
        for item in (str(value).strip() for value in values if str(value).strip())
        if item in _ALLOWED_EXEC_DECISIONS
    ) or ("deny",)
    title = str(payload.get("title", "OpenClaw execution approval")).strip()
    description = str(payload.get("description", "")).strip()
    return RuntimeApprovalRequest(
        approval_id=approval_id,
        kind=kind,
        title=title[:80],
        description=description[:512],
        command=command[:2048],
        cwd=cwd[:1024],
        allowed_decisions=allowed_decisions,
    )


def _approval_id(payload: Mapping[str, Any]) -> str:
    for key in ("id", "approvalId", "approval_id", "requestId"):
        value = str(payload.get(key, "")).strip()
        if value:
            return value
    return ""


def _kind(event_name: str, payload: Mapping[str, Any]) -> str:
    if event_name.startswith("plugin."):
        return "plugin"
    if event_name.startswith("exec."):
        return "exec"
    return str(payload.get("kind", "")).strip().lower()
