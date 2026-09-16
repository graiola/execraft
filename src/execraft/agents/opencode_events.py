"""Incremental OpenCode JSONL to provider-neutral console events.

OpenCode's unattended ``run --format json`` transport is a structured event
stream but is not a bidirectional session protocol.  This bridge exposes the
useful semantic events in the shared Conversation view without assigning the
worker a PTY or advertising unsupported live steering.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

InteractionCallback = Callable[[Mapping[str, Any]], None]
_MAX_LINE_BYTES = 16 * 1024 * 1024


class OpenCodeEventBridge:
    """Decode arbitrarily chunked OpenCode JSONL into semantic events."""

    def __init__(self, callback: InteractionCallback | None) -> None:
        self._callback = callback
        self._buffer = ""

    def feed(self, stream: str, text: str) -> None:
        if stream != "stdout" or not text or self._callback is None:
            return

        # Bound each protocol record independently. A single stdout read may
        # legitimately contain many small JSONL records whose aggregate size is
        # larger than the per-record safety limit.
        parts = text.split("\n")
        if len(parts) == 1:
            self._buffer += text
            self._discard_oversized_partial_record()
            return

        self._decode_bounded_line(self._buffer + parts[0])
        for line in parts[1:-1]:
            self._decode_bounded_line(line)
        self._buffer = parts[-1]
        self._discard_oversized_partial_record()

    def finish(self) -> None:
        """Decode a final non-newline-terminated record, when present."""
        if self._buffer.strip():
            self._decode_bounded_line(self._buffer)
        self._buffer = ""

    def _discard_oversized_partial_record(self) -> None:
        if len(self._buffer.encode("utf-8", errors="replace")) <= _MAX_LINE_BYTES:
            return
        self._emit(
            "error",
            text="OpenCode emitted an oversized JSONL record",
            status="failed",
        )
        self._buffer = ""

    def _decode_bounded_line(self, line: str) -> None:
        if len(line.encode("utf-8", errors="replace")) > _MAX_LINE_BYTES:
            self._emit(
                "error",
                text="OpenCode emitted an oversized JSONL record",
                status="failed",
            )
            return
        self._decode_line(line)

    def _decode_line(self, line: str) -> None:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(event, Mapping):
            return
        self._translate(dict(event))

    def _translate(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type", "")).strip()
        part = event.get("part") if isinstance(event.get("part"), Mapping) else {}
        item_id = str(
            part.get("messageID")
            or part.get("id")
            or event.get("messageID")
            or event.get("sessionID")
            or event_type
        )

        if event_type == "text":
            text = part.get("text")
            if isinstance(text, str) and text:
                self._emit(
                    "assistant_delta",
                    text=text,
                    item_id=item_id,
                    status="streaming",
                )
            return
        if event_type in {"reasoning", "reasoning_delta"}:
            text = part.get("text", event.get("text", ""))
            if text:
                self._emit(
                    "reasoning_delta",
                    text=str(text),
                    item_id=item_id,
                    status="streaming",
                )
            return
        if event_type == "step_start":
            self._emit(
                "status",
                text="OpenCode step started",
                item_id=item_id,
                status="running",
            )
            return
        if event_type == "step_finish":
            tokens = part.get("tokens") if isinstance(part.get("tokens"), Mapping) else {}
            data: dict[str, Any] = {"tokens": dict(tokens)} if tokens else {}
            if isinstance(part.get("cost"), (int, float)):
                data["cost"] = float(part["cost"])
            self._emit(
                "usage" if data else "status",
                text="OpenCode step completed",
                item_id=item_id,
                status="completed",
                data=data,
            )
            return
        if event_type in {"tool", "tool_start", "tool_use"}:
            state = part.get("state") if isinstance(part.get("state"), Mapping) else {}
            # ``opencode run --format json`` currently emits ``tool_use`` only
            # after the tool has finished and places the real lifecycle state
            # under ``part.state``.  Reporting every such event as ``running``
            # made completed reads look like an endless loop in the shared
            # progress tracker.
            tool_status = str(
                state.get("status") or event.get("status") or "running"
            ).strip().lower()
            self._emit(
                "tool",
                title=str(part.get("tool", event.get("tool", "Tool"))),
                text=str(part.get("text", event.get("text", ""))),
                item_id=item_id,
                status=tool_status,
                data=event,
            )
            return
        if event_type in {"tool_result", "tool_finish"}:
            self._emit(
                "tool_output",
                title=str(part.get("tool", event.get("tool", "Tool"))),
                text=str(part.get("text", event.get("text", ""))),
                item_id=item_id,
                status="completed",
                data=event,
            )
            return
        if event_type == "error":
            error = event.get("error") if isinstance(event.get("error"), Mapping) else {}
            data = error.get("data") if isinstance(error.get("data"), Mapping) else {}
            detail = data.get("message") or error.get("message") or "OpenCode failed"
            self._emit("error", text=str(detail), item_id=item_id, status="failed")

    def _emit(self, kind: str, **payload: Any) -> None:
        callback = self._callback
        if callback is None:
            return
        try:
            callback(
                {
                    "kind": kind,
                    "provider": "opencode",
                    "interaction_mode": "conversation",
                    "streaming": True,
                    "steering_supported": False,
                    "transport": "opencode-run-jsonl",
                    "control_mode": "observation",
                    **payload,
                }
            )
        except Exception:
            return
