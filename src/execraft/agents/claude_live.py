"""Claude Code stream-json controller for live orchestrated sessions.

Claude Code's print mode accepts a persistent ``stream-json`` input stream and
emits assistant, thinking, tool and result events as JSONL.  The CLI transport
queues additional user messages; it does not provide the hard in-flight
interrupt semantics of the Agent SDK.  The console therefore labels controls as
queued guidance and preserves that distinction in durable metadata.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from execraft.agents.live_session import LiveSessionUpdate
from execraft.interaction import describe_tool_activity, parse_partial_json_fields
from execraft.orchestrate.scheduler import StructuredHandoff

InteractionCallback = Callable[[Mapping[str, Any]], None]
ControlCallback = Callable[[], list[Mapping[str, Any]]]


class ClaudeStreamController:
    """Translate Claude stream-json messages into provider-neutral UI events."""

    def __init__(
        self,
        *,
        handoff: StructuredHandoff,
        prompt: str,
        interaction_callback: InteractionCallback | None,
        control_callback: ControlCallback | None,
    ) -> None:
        self.handoff = handoff
        self.prompt = prompt
        self._emit_callback = interaction_callback
        self._control_callback = control_callback
        self.final_message = ""
        self.structured_output: dict[str, Any] | None = None
        self.usage: dict[str, Any] = {}
        self.total_cost_usd: float | None = None
        self.session_id = ""
        self.error = ""
        self.work_started = False
        self._assistant_parts: list[str] = []
        self._tool_names: dict[str, str] = {}
        self._tool_ids_by_index: dict[int, str] = {}
        self._tool_input_parts: dict[str, list[str]] = {}
        self._tool_inputs: dict[str, dict[str, Any]] = {}
        self._tool_parents: dict[str, str] = {}

    @property
    def initial_messages(self) -> tuple[dict[str, Any], ...]:
        return (self._user_message(self.prompt),)

    def handle_message(self, message: dict[str, Any]) -> LiveSessionUpdate | None:
        message_type = str(message.get("type", ""))
        if message_type == "system":
            self.session_id = str(message.get("session_id", self.session_id))
            self._emit(
                "session",
                text=str(message.get("subtype", "Claude session initialized")).replace("_", " "),
                status="running",
                data={
                    "session_id": self.session_id,
                    "model": message.get("model", ""),
                    "tools": message.get("tools", []),
                },
            )
            return None
        if message_type == "stream_event":
            self.work_started = True
            self._handle_stream_event(message)
            return None
        if message_type == "assistant":
            self.work_started = True
            self._handle_assistant(message)
            return None
        if message_type == "user":
            self._handle_user_event(message)
            return None
        if message_type == "result":
            self._handle_result(message)
            return LiveSessionUpdate(completed=True)
        if message_type in {
            "tool_progress",
            "tool_use_summary",
            "rate_limit_event",
            "prompt_suggestion",
            "hook_started",
            "hook_progress",
            "hook_response",
        }:
            self._handle_auxiliary_event(message_type, message)
        return None

    def poll_controls(self) -> tuple[dict[str, Any], ...]:
        callback = self._control_callback
        if callback is None:
            return ()
        try:
            controls = list(callback() or [])
        except Exception:
            return ()
        outbound: list[dict[str, Any]] = []
        for control in controls:
            action = str(control.get("action", "")).strip().lower()
            if action in {"steer", "input"}:
                text = str(control.get("data", "")).strip()
                if text:
                    outbound.append(self._user_message(text))
                    self._emit(
                        "operator_ack",
                        text=text,
                        status="queued",
                        summary="Guidance queued for Claude's next input boundary",
                    )
            elif action == "signal" and str(control.get("signal", "")) == "interrupt":
                text = (
                    "Stop the current approach at the next safe input boundary, reassess the "
                    "latest operator guidance, and continue safely."
                )
                outbound.append(self._user_message(text))
                self._emit(
                    "operator_ack",
                    text=text,
                    status="queued",
                    summary="Stop-and-reassess request queued; this CLI transport cannot hard-interrupt",
                )
        return tuple(outbound)

    def _handle_stream_event(self, payload: Mapping[str, Any]) -> None:
        event = payload.get("event") if isinstance(payload.get("event"), Mapping) else {}
        event_type = str(event.get("type", ""))
        index = _integer(event.get("index"))
        parent_id = str(
            payload.get("parent_tool_use_id")
            or event.get("parent_tool_use_id")
            or ""
        )

        if event_type == "content_block_start":
            block = event.get("content_block") if isinstance(event.get("content_block"), Mapping) else {}
            if block.get("type") == "tool_use":
                self._start_tool(block, index=index, parent_id=parent_id)
            return

        if event_type == "content_block_delta":
            delta = event.get("delta") if isinstance(event.get("delta"), Mapping) else {}
            delta_type = str(delta.get("type", ""))
            if delta_type == "text_delta":
                text = str(delta.get("text", ""))
                if text:
                    self._assistant_parts.append(text)
                    self._emit(
                        "assistant_delta",
                        text=text,
                        parent_item_id=parent_id,
                        status="streaming",
                    )
            elif delta_type == "thinking_delta":
                text = str(delta.get("thinking", ""))
                if text:
                    self._emit(
                        "reasoning_delta",
                        text=text,
                        parent_item_id=parent_id,
                        status="streaming",
                    )
            elif delta_type == "input_json_delta":
                self._append_tool_input(index, str(delta.get("partial_json", "")))
            return

        if event_type == "content_block_stop":
            self._finish_tool_input(index)
            return

        if event_type == "message_start":
            message = event.get("message") if isinstance(event.get("message"), Mapping) else {}
            self.session_id = str(payload.get("session_id", self.session_id))
            self._emit(
                "status",
                text="Claude is working",
                status="running",
                data={"message_id": message.get("id", ""), "session_id": self.session_id},
            )
        elif event_type == "message_delta":
            usage = event.get("usage")
            if isinstance(usage, Mapping):
                self.usage.update(dict(usage))
                self._emit("usage", status="updated", data=self.usage)

    def _start_tool(self, block: Mapping[str, Any], *, index: int | None, parent_id: str) -> None:
        tool_id = str(block.get("id", "tool"))
        name = str(block.get("name", "Tool"))
        if index is not None:
            self._tool_ids_by_index[index] = tool_id
        self._tool_names[tool_id] = name
        self._tool_input_parts.setdefault(tool_id, [])
        input_value = block.get("input") if isinstance(block.get("input"), Mapping) else {}
        self._tool_inputs[tool_id] = dict(input_value)
        parent = parent_id or str(block.get("parent_tool_use_id", ""))
        if parent:
            self._tool_parents[tool_id] = parent
        description = describe_tool_activity(name, self._tool_inputs[tool_id])
        self._emit(
            "tool",
            title=description.title,
            summary=description.summary,
            category=description.category,
            operation=description.operation,
            target=description.target,
            command=description.command,
            item_id=tool_id,
            parent_item_id=parent,
            status="running",
            data={"name": name, "input": self._tool_inputs[tool_id], **dict(block)},
        )

    def _append_tool_input(self, index: int | None, text: str) -> None:
        if not text:
            return
        block_index = index if index is not None else -1
        tool_id = self._tool_ids_by_index.get(
            block_index,
            f"tool-{index if index is not None else 'unknown'}",
        )
        name = self._tool_names.get(tool_id, "Tool")
        parts = self._tool_input_parts.setdefault(tool_id, [])
        parts.append(text)
        partial_text = "".join(parts)
        recovered = parse_partial_json_fields(partial_text)
        if recovered:
            self._tool_inputs[tool_id] = {**self._tool_inputs.get(tool_id, {}), **recovered}
        description = describe_tool_activity(name, self._tool_inputs.get(tool_id, {}))
        self._emit(
            "tool_input_delta",
            title=description.title,
            text=text,
            summary=description.summary,
            category=description.category,
            operation=description.operation,
            target=description.target,
            command=description.command,
            item_id=tool_id,
            parent_item_id=self._tool_parents.get(tool_id, ""),
            status="running",
            data={"name": name, "partial_input": partial_text, "input": self._tool_inputs.get(tool_id, {})},
        )

    def _finish_tool_input(self, index: int | None) -> None:
        if index is None:
            return
        tool_id = self._tool_ids_by_index.get(index)
        if not tool_id:
            return
        raw = "".join(self._tool_input_parts.get(tool_id, []))
        if raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = parse_partial_json_fields(raw)
            if isinstance(parsed, Mapping):
                self._tool_inputs[tool_id] = dict(parsed)
        name = self._tool_names.get(tool_id, "Tool")
        description = describe_tool_activity(name, self._tool_inputs.get(tool_id, {}))
        self._emit(
            "tool",
            title=description.title,
            summary=description.summary,
            category=description.category,
            operation=description.operation,
            target=description.target,
            command=description.command,
            item_id=tool_id,
            parent_item_id=self._tool_parents.get(tool_id, ""),
            status="running",
            data={"name": name, "input": self._tool_inputs.get(tool_id, {})},
        )

    def _handle_assistant(self, payload: Mapping[str, Any]) -> None:
        message = payload.get("message") if isinstance(payload.get("message"), Mapping) else payload
        content = message.get("content") if isinstance(message.get("content"), list) else []
        parent_id = str(payload.get("parent_tool_use_id", ""))
        for block in content:
            if not isinstance(block, Mapping):
                continue
            block_type = str(block.get("type", ""))
            if block_type == "text":
                text = str(block.get("text", ""))
                if text:
                    self.final_message = text
                    self._emit("assistant", text=text, status="completed", parent_item_id=parent_id)
            elif block_type == "thinking":
                text = str(block.get("thinking", ""))
                if text:
                    self._emit("reasoning", text=text, status="completed", parent_item_id=parent_id)
            elif block_type == "tool_use":
                self._start_tool(block, index=None, parent_id=parent_id)

    def _handle_user_event(self, payload: Mapping[str, Any]) -> None:
        message = payload.get("message") if isinstance(payload.get("message"), Mapping) else payload
        raw_content = message.get("content")
        if isinstance(raw_content, str) and raw_content:
            self._emit("operator_ack", text=raw_content, status="accepted")
            return
        content = raw_content if isinstance(raw_content, list) else []
        text_parts = [
            str(block.get("text", ""))
            for block in content
            if isinstance(block, Mapping) and block.get("type") == "text"
        ]
        if any(text_parts):
            self._emit("operator_ack", text="".join(text_parts), status="accepted")
        for block in content:
            if not isinstance(block, Mapping) or block.get("type") != "tool_result":
                continue
            tool_id = str(block.get("tool_use_id", "tool"))
            content_value = block.get("content", "")
            text = (
                content_value
                if isinstance(content_value, str)
                else json.dumps(content_value, ensure_ascii=False)
            )
            name = self._tool_names.get(tool_id, "Tool result")
            description = describe_tool_activity(name, self._tool_inputs.get(tool_id, {}))
            self._emit(
                "tool_output",
                title=description.title,
                text=text,
                summary=description.summary,
                category=description.category,
                operation=description.operation,
                target=description.target,
                command=description.command,
                item_id=tool_id,
                parent_item_id=self._tool_parents.get(tool_id, ""),
                status="completed" if not block.get("is_error") else "failed",
                data={"name": name, "input": self._tool_inputs.get(tool_id, {})},
            )

    def _handle_auxiliary_event(self, message_type: str, message: Mapping[str, Any]) -> None:
        kind = "status" if message_type == "rate_limit_event" else "tool_progress"
        title = message_type.replace("_", " ")
        text = str(
            message.get("summary", message.get("message", message.get("suggestion", "")))
        )
        tool_id = str(message.get("tool_use_id", message.get("item_id", "")))
        name = self._tool_names.get(tool_id, title)
        description = describe_tool_activity(name, self._tool_inputs.get(tool_id, {}))
        self._emit(
            kind,
            title=description.title if kind == "tool_progress" else title,
            text=text,
            summary=description.summary or text,
            category=description.category if kind == "tool_progress" else "status",
            operation=description.operation if kind == "tool_progress" else message_type,
            target=description.target,
            command=description.command,
            item_id=tool_id,
            parent_item_id=self._tool_parents.get(tool_id, ""),
            status="running",
            data=message,
        )

    def _handle_result(self, payload: Mapping[str, Any]) -> None:
        self.session_id = str(payload.get("session_id", self.session_id))
        usage = payload.get("usage")
        if isinstance(usage, Mapping):
            self.usage = dict(usage)
        cost = payload.get("total_cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            self.total_cost_usd = float(cost)
        structured = payload.get("structured_output")
        if isinstance(structured, Mapping):
            self.structured_output = dict(structured)
            self.final_message = json.dumps(self.structured_output, ensure_ascii=False)
        else:
            result = payload.get("result")
            if isinstance(result, str) and result:
                self.final_message = result
            elif not self.final_message and self._assistant_parts:
                self.final_message = "".join(self._assistant_parts)
        if bool(payload.get("is_error")) or str(payload.get("subtype", "")) in {"error", "failure"}:
            self.error = self.final_message or str(payload.get("error", "Claude Code failed"))
            self._emit("error", text=self.error, status="failed", data=payload)
        else:
            self._emit(
                "status",
                text="Agent work completed",
                status="completed",
                data={"usage": self.usage, "total_cost_usd": self.total_cost_usd},
            )

    @staticmethod
    def _user_message(text: str) -> dict[str, Any]:
        return {
            "type": "user",
            "message": {"role": "user", "content": text},
            "parent_tool_use_id": None,
        }

    def _emit(
        self,
        kind: str,
        *,
        text: str = "",
        title: str = "",
        summary: str = "",
        category: str = "",
        operation: str = "",
        target: str = "",
        command: str = "",
        status: str = "",
        item_id: str = "",
        parent_item_id: str = "",
        data: Mapping[str, Any] | None = None,
    ) -> None:
        callback = self._emit_callback
        if callback is None:
            return
        try:
            callback(
                {
                    "kind": kind,
                    "text": text,
                    "title": title,
                    "summary": summary,
                    "category": category,
                    "operation": operation,
                    "target": target,
                    "command": command,
                    "status": status,
                    "item_id": item_id,
                    "parent_item_id": parent_item_id,
                    "data": dict(data or {}),
                    "provider": "claude-code",
                    "transport": "claude-cli-stream-json",
                    "control_mode": "queued_guidance",
                    "interaction_mode": "conversation",
                    "streaming": True,
                    "steering_supported": True,
                }
            )
        except Exception:
            pass


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
