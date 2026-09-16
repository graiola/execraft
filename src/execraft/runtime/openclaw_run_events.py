"""Turn-local OpenClaw event collection for usage and output telemetry."""

from __future__ import annotations

import threading
from typing import Any, Mapping

from .openclaw_protocol import GatewayEvent


class OpenClawRunEventCollector:
    """Best-effort telemetry fallback for one Gateway agent run.

    Terminal RPC payloads remain authoritative. Events are retained only to
    recover usage/text metadata when a compatible Gateway omits those optional
    fields from its final response.
    """

    def __init__(self) -> None:
        self.run_id = ""
        self._events: list[Mapping[str, Any]] = []
        self._lock = threading.RLock()

    def bind(self, run_id: str) -> None:
        with self._lock:
            self.run_id = run_id
            self._events = [
                item for item in self._events if str(item.get("runId", "")) == run_id
            ]

    def __call__(self, event: GatewayEvent) -> None:
        if event.name != "agent" or not isinstance(event.payload, Mapping):
            return
        payload = dict(event.payload)
        event_run = str(payload.get("runId", ""))
        with self._lock:
            if self.run_id and event_run != self.run_id:
                return
            self._events.append(payload)

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._events)

    def snapshot(self) -> tuple[Mapping[str, Any], ...]:
        with self._lock:
            return tuple(self._events)

    def usage(self) -> dict[str, Any]:
        with self._lock:
            events = tuple(self._events)
        for payload in reversed(events):
            if str(payload.get("stream", "")).lower() != "usage":
                continue
            data = payload.get("data")
            if isinstance(data, Mapping):
                nested = data.get("usage")
                return dict(nested) if isinstance(nested, Mapping) else dict(data)
        return {}

    def assistant_text(self) -> str:
        with self._lock:
            events = tuple(self._events)
        for payload in reversed(events):
            if str(payload.get("stream", "")).lower() != "assistant":
                continue
            data = payload.get("data")
            if isinstance(data, Mapping):
                for key in ("text", "final", "message"):
                    text = str(data.get(key, "")).strip()
                    if text:
                        return text
        return ""
