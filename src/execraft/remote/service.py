"""Transport-neutral remote command authorization and dispatch."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from .models import RemoteCommand, RemoteRequest, SENSITIVE_COMMANDS
from .queue import DecisionQueue, RemoteAuditLog, RemoteStoreError, ReplayGuard


class NotificationAdapter(Protocol):
    """Outbound-only transport port implemented by Matrix/Telegram/etc."""

    def send(self, event: dict[str, Any]) -> None:
        ...


CommandHandler = Callable[[RemoteRequest], dict[str, Any]]


class RemoteControlService:
    def __init__(
        self,
        *,
        allowed_actor_ids: set[str],
        handlers: dict[RemoteCommand, CommandHandler],
        replay_guard: ReplayGuard,
        audit_log: RemoteAuditLog,
        decision_queue: DecisionQueue,
        read_only: bool = False,
    ):
        self.allowed_actor_ids = set(allowed_actor_ids)
        self.handlers = dict(handlers)
        self.replay_guard = replay_guard
        self.audit_log = audit_log
        self.decision_queue = decision_queue
        self.read_only = read_only

    def dispatch(self, request: RemoteRequest) -> dict[str, Any]:
        if request.actor_id not in self.allowed_actor_ids:
            self.audit_log.append("remote_request_denied", {"request_id": request.request_id})
            raise RemoteStoreError("unauthorized remote actor")
        self._validate_timestamp(request.created_at)
        if not self.replay_guard.claim(request.request_id):
            raise RemoteStoreError("duplicate remote request")
        if self.read_only and request.command not in {
            RemoteCommand.STATUS,
            RemoteCommand.PROGRESS,
            RemoteCommand.TIMELINE,
            RemoteCommand.RESOURCES,
        }:
            raise RemoteStoreError("remote control is in read-only mode")

        if request.command in SENSITIVE_COMMANDS:
            result = self._resolve_decision(request)
        else:
            handler = self.handlers.get(request.command)
            if handler is None:
                raise RemoteStoreError(f"remote command is not enabled: {request.command.value}")
            result = handler(request)
        self.audit_log.append(
            "remote_request_applied",
            {"request_id": request.request_id, "command": request.command.value},
        )
        return result

    def _resolve_decision(self, request: RemoteRequest) -> dict[str, Any]:
        payload = request.payload
        decision = self.decision_queue.resolve(
            str(payload.get("decision_id", "")),
            actor_id=request.actor_id,
            revision=int(payload.get("revision", -1)),
            nonce=str(payload.get("nonce", "")),
            resolution=(
                "rejected"
                if request.command == RemoteCommand.REJECT
                else str(payload.get("option", ""))
            ),
        )
        handler = self.handlers.get(request.command)
        if handler is not None:
            handler(request)
        return decision.as_mapping()

    @staticmethod
    def _validate_timestamp(value: str, *, max_age_seconds: int = 900) -> None:
        created = datetime.fromisoformat(value)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - created).total_seconds()
        if age < -60 or age > max_age_seconds:
            raise RemoteStoreError("remote request is expired or from the future")
