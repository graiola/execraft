"""Codex app-server client used by the orchestrated live console.

The app-server is the same integration surface used by rich Codex clients.  It
streams plans, command/file items, diffs, and assistant text while accepting
``turn/steer`` requests on the active turn.  This controller intentionally
contains all protocol-specific state; the orchestrator sees only the existing
``AgentAdapter`` result contract and provider-neutral interaction events.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from execraft.agents.live_session import LiveSessionUpdate
from execraft.orchestrate.scheduler import StructuredHandoff

InteractionCallback = Callable[[Mapping[str, Any]], None]
ControlCallback = Callable[[], list[Mapping[str, Any]]]

_CODEX_UNSUPPORTED_OUTPUT_SCHEMA_KEYWORDS = frozenset(
    {
        # Codex structured outputs reject this JSON Schema keyword. Execraft
        # still applies it when validating the returned payload locally.
        "uniqueItems",
    }
)


def _codex_compatible_output_schema(value: Any) -> Any:
    """Return a strict deep copy containing only schema keywords Codex accepts.

    Codex native Structured Outputs requires every object to be closed and all
    declared properties to be required.  Execraft's local validator may accept a
    more permissive provider-neutral contract, so enforce the native boundary
    here as a final compatibility guard without mutating the durable schema.
    """

    if isinstance(value, Mapping):
        normalized = {
            key: _codex_compatible_output_schema(child)
            for key, child in value.items()
            if key not in _CODEX_UNSUPPORTED_OUTPUT_SCHEMA_KEYWORDS
            and not str(key).startswith("x-")
        }
        if "type" not in normalized:
            if "const" in normalized:
                inferred = _json_schema_type(normalized["const"])
                if inferred:
                    normalized["type"] = inferred
            elif isinstance(normalized.get("enum"), list):
                inferred_types = {
                    inferred
                    for item in normalized["enum"]
                    if (inferred := _json_schema_type(item))
                }
                if len(inferred_types) == 1:
                    normalized["type"] = inferred_types.pop()
        if normalized.get("type") == "object" or isinstance(
            normalized.get("properties"), Mapping
        ):
            properties = normalized.get("properties")
            if not isinstance(properties, Mapping):
                properties = {}
                normalized["properties"] = properties
            normalized["additionalProperties"] = False
            normalized["required"] = list(properties)
        return normalized
    if isinstance(value, list):
        return [_codex_compatible_output_schema(child) for child in value]
    return value


def _json_schema_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if value is None:
        return "null"
    return ""


class CodexAppServerController:
    """Stateful JSON-RPC controller for one Codex thread and turn."""

    def __init__(
        self,
        *,
        handoff: StructuredHandoff,
        prompt: str,
        model: str,
        sandbox: str,
        workdir: Path,
        interaction_callback: InteractionCallback | None,
        control_callback: ControlCallback | None,
    ) -> None:
        self.handoff = handoff
        self.prompt = prompt
        self.model = model
        self.sandbox = sandbox
        self.workdir = workdir
        self._emit_callback = interaction_callback
        self._control_callback = control_callback
        self._next_protocol_id = 2
        self._next_id = 100
        self.thread_id = ""
        self.turn_id = ""
        self.final_message = ""
        self.usage: dict[str, Any] = {}
        self.error = ""
        self.work_started = False
        self._pending_steering: list[str] = []
        self._control_requests: dict[int, str] = {}
        self._agent_message_parts: dict[str, str] = {}
        self._authoritative_messages: list[str] = []
        self._thread_start_params: dict[str, Any] = {}
        self._thread_request_ids: set[int] = set()
        self._turn_request_ids: set[int] = set()
        self._thread_sandbox_style = "kebab"
        self._turn_sandbox_style = "camel"
        self._thread_sandbox_styles_tried = {self._thread_sandbox_style}
        self._turn_sandbox_styles_tried = {self._turn_sandbox_style}

    @property
    def initial_messages(self) -> tuple[dict[str, Any], ...]:
        thread_params: dict[str, Any] = {
            "cwd": str(self.workdir),
            "approvalPolicy": "never",
            "serviceName": "execraft",
        }
        if self.model:
            thread_params["model"] = self.model
        self._thread_start_params = thread_params
        return (
            {
                "method": "initialize",
                "id": 1,
                "params": {
                    "clientInfo": {
                        "name": "execraft",
                        "title": "Execraft Agent Console",
                        "version": "0.1.0",
                    },
                    "capabilities": {"experimentalApi": True},
                },
            },
        )

    def handle_message(self, message: dict[str, Any]) -> LiveSessionUpdate | None:
        if "id" in message and "method" in message:
            return self._handle_server_request(message)
        if "id" in message:
            return self._handle_response(message)
        method = str(message.get("method", ""))
        params = message.get("params") if isinstance(message.get("params"), Mapping) else {}
        return self._handle_notification(method, dict(params))

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
                if not text:
                    continue
                if self.thread_id and self.turn_id:
                    outbound.append(self._steer_request(text))
                else:
                    self._pending_steering.append(text)
            elif action == "signal" and str(control.get("signal", "")) == "interrupt":
                if self.thread_id and self.turn_id:
                    outbound.append(
                        self._request(
                            "turn/interrupt",
                            {"threadId": self.thread_id, "turnId": self.turn_id},
                        )
                    )
                    self._emit("status", text="Interrupt requested", status="pending")
        return tuple(outbound)

    def _handle_response(self, message: Mapping[str, Any]) -> LiveSessionUpdate | None:
        request_id = message.get("id")
        control_method = (
            self._control_requests.pop(request_id, "")
            if isinstance(request_id, int)
            else ""
        )
        if isinstance(message.get("error"), Mapping):
            error = message["error"]
            detail = str(error.get("message", "Codex app-server request failed"))
            compatibility_retry = self._sandbox_compatibility_retry(request_id, detail)
            if compatibility_retry is not None:
                return compatibility_retry
            if control_method:
                self._emit(
                    "operator_ack",
                    text=f"{control_method} failed: {detail}",
                    status="failed",
                )
                return None
            self.error = detail
            self._emit("error", text=self.error, status="failed")
            return LiveSessionUpdate(completed=True, terminate_process=True)
        result = message.get("result") if isinstance(message.get("result"), Mapping) else {}
        if control_method:
            self._emit(
                "operator_ack" if control_method == "turn/steer" else "status",
                text=(
                    "Steering accepted"
                    if control_method == "turn/steer"
                    else "Interrupt accepted"
                ),
                status="accepted",
                data=dict(result),
            )
            return None
        if request_id == 1:
            return LiveSessionUpdate(
                outbound=(
                    {"method": "initialized", "params": {}},
                    self._thread_start_request(),
                )
            )
        if request_id in self._thread_request_ids:
            thread = result.get("thread") if isinstance(result.get("thread"), Mapping) else {}
            self.thread_id = str(thread.get("id", ""))
            if not self.thread_id:
                self.error = "Codex app-server did not return a thread id"
                return LiveSessionUpdate(completed=True, terminate_process=True)
            self._emit(
                "session",
                text="Codex thread started",
                status="running",
                data={"thread_id": self.thread_id, "session_id": thread.get("sessionId", "")},
            )
            return LiveSessionUpdate(outbound=(self._turn_start_request(),))
        if request_id in self._turn_request_ids:
            turn = result.get("turn") if isinstance(result.get("turn"), Mapping) else {}
            self.turn_id = str(turn.get("id", self.turn_id))
            self.work_started = bool(self.turn_id)
            outbound = tuple(self._flush_pending_steering())
            return LiveSessionUpdate(outbound=outbound)
        return None

    def _handle_notification(
        self, method: str, params: dict[str, Any]
    ) -> LiveSessionUpdate | None:
        if method == "turn/started":
            turn = params.get("turn") if isinstance(params.get("turn"), Mapping) else {}
            self.turn_id = str(turn.get("id", self.turn_id))
            self.work_started = True
            self._emit(
                "status",
                text="Agent is working",
                status="running",
                data={"thread_id": self.thread_id, "turn_id": self.turn_id},
            )
            return LiveSessionUpdate(outbound=tuple(self._flush_pending_steering()))

        if method == "item/agentMessage/delta":
            delta = _string_value(params, "delta", "text")
            item_id = str(params.get("itemId", params.get("item_id", "agent")))
            if delta:
                self._agent_message_parts[item_id] = (
                    self._agent_message_parts.get(item_id, "") + delta
                )
                self._emit("assistant_delta", text=delta, item_id=item_id, status="streaming")
            return None

        if method == "item/reasoning/summaryTextDelta":
            delta = _string_value(params, "delta", "text")
            if delta:
                self._emit(
                    "reasoning_delta",
                    text=delta,
                    item_id=str(params.get("itemId", "reasoning")),
                    status="streaming",
                )
            return None

        if method == "item/commandExecution/outputDelta":
            delta = _string_value(params, "delta", "text")
            if delta:
                self._emit(
                    "tool_output",
                    text=delta,
                    item_id=str(params.get("itemId", "command")),
                    status="running",
                )
            return None

        if method == "turn/plan/updated":
            plan = params.get("plan") if isinstance(params.get("plan"), list) else []
            self._emit(
                "plan",
                text=str(params.get("explanation", "")),
                status="running",
                data={"steps": [dict(item) for item in plan if isinstance(item, Mapping)]},
            )
            return None

        if method == "turn/diff/updated":
            self._emit(
                "diff",
                text=str(params.get("diff", "")),
                status="updated",
                data={"turn_id": params.get("turnId", self.turn_id)},
            )
            return None

        if method in {"item/started", "item/completed"}:
            item = params.get("item") if isinstance(params.get("item"), Mapping) else {}
            self._handle_item(dict(item), completed=method == "item/completed")
            return None

        if method == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage", params.get("usage", {}))
            if isinstance(usage, Mapping):
                self.usage = dict(usage)
                self._emit("usage", status="updated", data=self.usage)
            return None

        if method == "turn/completed":
            turn = params.get("turn") if isinstance(params.get("turn"), Mapping) else {}
            status = str(turn.get("status", "completed"))
            error = turn.get("error") if isinstance(turn.get("error"), Mapping) else {}
            if status == "failed":
                self.error = str(error.get("message", "Codex turn failed"))
                self._emit("error", text=self.error, status="failed")
            elif status == "interrupted":
                self.error = "Codex turn was interrupted by the operator"
                self._emit("status", text=self.error, status="interrupted")
            else:
                self._emit("status", text="Agent work completed", status="completed")
            self.final_message = self._best_final_message()
            return LiveSessionUpdate(completed=True, terminate_process=True)

        if method in {
            "model/rerouted",
            "model/safetyBuffering/updated",
            "hook/started",
            "hook/completed",
        }:
            self._emit("status", text=method.replace("/", " · "), status="running", data=params)
        return None

    def _handle_item(self, item: dict[str, Any], *, completed: bool) -> None:
        item_type = str(item.get("type", ""))
        item_id = str(item.get("id", item_type or "item"))
        status = str(item.get("status", "completed" if completed else "running"))
        if item_type in {"agentMessage", "agent_message"}:
            text = str(item.get("text", ""))
            if text:
                if completed:
                    self._authoritative_messages.append(text)
                self._emit(
                    "assistant" if completed else "assistant_delta",
                    text=text,
                    item_id=item_id,
                    status=status,
                )
            return
        if item_type == "reasoning":
            summary = item.get("summary", [])
            if isinstance(summary, list):
                text = "\n".join(
                    str(part.get("text", part)) if isinstance(part, Mapping) else str(part)
                    for part in summary
                )
            else:
                text = str(summary)
            if text:
                self._emit("reasoning", text=text, item_id=item_id, status=status)
            return
        if item_type == "commandExecution":
            command = item.get("command", "")
            rendered_command = (
                " ".join(str(part) for part in command)
                if isinstance(command, list)
                else str(command)
            )
            self._emit(
                "tool",
                title="Command",
                text=str(item.get("aggregatedOutput", "")) if completed else "",
                item_id=item_id,
                status=status,
                data={
                    "command": rendered_command,
                    "cwd": item.get("cwd", ""),
                    "exit_code": item.get("exitCode"),
                    "parent_tool_use_id": item.get("parentId", item.get("parent_id", "")),
                },
            )
            return
        if item_type == "fileChange":
            changes = item.get("changes") if isinstance(item.get("changes"), list) else []
            self._emit(
                "file_change",
                title="File changes",
                item_id=item_id,
                status=status,
                data={
                    "changes": [
                        dict(change)
                        for change in changes
                        if isinstance(change, Mapping)
                    ]
                },
            )
            return
        if item_type in {"mcpToolCall", "dynamicToolCall", "webSearch"}:
            self._emit(
                "tool",
                title=str(item.get("tool", item.get("server", item_type))),
                text=str(item.get("result", "")) if completed else "",
                item_id=item_id,
                status=status,
                data=item,
            )

    def _handle_server_request(self, message: dict[str, Any]) -> LiveSessionUpdate:
        method = str(message.get("method", ""))
        request_id = message.get("id")
        params = message.get("params") if isinstance(message.get("params"), Mapping) else {}
        if method == "item/commandExecution/requestApproval":
            decision = "acceptForSession"
            self._emit(
                "approval",
                title="Command approval",
                text=str(params.get("reason", params.get("command", ""))),
                status="auto_accepted",
                data=dict(params),
            )
            return LiveSessionUpdate(
                outbound=({"id": request_id, "result": {"decision": decision}},)
            )
        if method == "item/fileChange/requestApproval":
            decision = "decline" if self.handoff.read_only else "acceptForSession"
            self._emit(
                "approval",
                title="File change approval",
                text=str(params.get("reason", "")),
                status="auto_declined" if self.handoff.read_only else "auto_accepted",
                data=dict(params),
            )
            return LiveSessionUpdate(
                outbound=({"id": request_id, "result": {"decision": decision}},)
            )
        if method == "item/tool/requestUserInput":
            self._emit(
                "question",
                title="Agent requested input",
                text=str(params.get("question", params.get("message", ""))),
                status="unanswered",
                data=dict(params),
            )
            # The orchestrator prompt requires autonomous execution. Return an
            # empty answer rather than deadlocking the package; the operator can
            # still provide steering through turn/steer.
            return LiveSessionUpdate(outbound=({"id": request_id, "result": {"answers": {}}},))
        return LiveSessionUpdate(
            outbound=(
                {
                    "id": request_id,
                    "error": {"code": -32601, "message": f"Unsupported client request: {method}"},
                },
            )
        )

    def _sandbox_mode(self) -> str:
        """Return the canonical CLI sandbox mode used by thread/start."""

        if self.handoff.read_only or self.sandbox == "read-only":
            return "read-only"
        if self.sandbox == "danger-full-access":
            return "danger-full-access"
        return "workspace-write"

    def _camel_sandbox_mode(self) -> str:
        return {
            "read-only": "readOnly",
            "workspace-write": "workspaceWrite",
            "danger-full-access": "dangerFullAccess",
        }[self._sandbox_mode()]

    def _thread_start_request(self) -> dict[str, Any]:
        params = dict(self._thread_start_params)
        params["sandbox"] = (
            self._sandbox_mode()
            if self._thread_sandbox_style == "kebab"
            else self._camel_sandbox_mode()
        )
        request_id = self._allocate_protocol_request_id()
        self._thread_request_ids.add(request_id)
        return {"method": "thread/start", "id": request_id, "params": params}

    def _sandbox_policy(self) -> dict[str, Any]:
        if self._turn_sandbox_style == "kebab":
            return {"type": self._sandbox_mode()}

        mode = self._camel_sandbox_mode()
        if mode == "readOnly":
            return {"type": "readOnly"}
        if mode == "dangerFullAccess":
            return {"type": "dangerFullAccess"}
        return {
            "type": "workspaceWrite",
            "writableRoots": list(
                dict.fromkeys(
                    [str(self.workdir), *self.handoff.additional_writable_roots]
                )
            ),
            "networkAccess": False,
        }

    def _turn_start_request(self) -> dict[str, Any]:
        sandbox_policy = self._sandbox_policy()
        params: dict[str, Any] = {
            "threadId": self.thread_id,
            "input": [{"type": "text", "text": self.prompt}],
            "cwd": str(self.workdir),
            "approvalPolicy": "never",
            "sandboxPolicy": sandbox_policy,
            "summary": "concise",
        }
        if self.model:
            params["model"] = self.model
        if self.handoff.expected_output_schema:
            params["outputSchema"] = _codex_compatible_output_schema(
                self.handoff.expected_output_schema
            )
        request_id = self._allocate_protocol_request_id()
        self._turn_request_ids.add(request_id)
        return {"method": "turn/start", "id": request_id, "params": params}

    def _sandbox_compatibility_retry(
        self, request_id: object, detail: str
    ) -> LiveSessionUpdate | None:
        """Retry once with the alternate Codex sandbox enum representation.

        Codex app-server versions have shipped both kebab-case CLI modes and
        camelCase tagged sandbox policies.  Negotiating on the explicit
        ``unknown variant`` response keeps the adapter compatible without
        pinning every deployment to one CLI release.
        """

        if not isinstance(request_id, int) or not self._is_variant_error(detail):
            return None
        if request_id in self._thread_request_ids and not self.thread_id:
            alternate = "camel" if self._thread_sandbox_style == "kebab" else "kebab"
            if alternate in self._thread_sandbox_styles_tried:
                return None
            self._thread_sandbox_style = alternate
            self._thread_sandbox_styles_tried.add(alternate)
            self._emit(
                "status",
                text=(
                    "Codex sandbox syntax differs from the configured protocol; "
                    f"retrying thread startup with {alternate}-case variants"
                ),
                status="compatibility_retry",
            )
            return LiveSessionUpdate(outbound=(self._thread_start_request(),))
        if request_id in self._turn_request_ids and not self.work_started:
            alternate = "kebab" if self._turn_sandbox_style == "camel" else "camel"
            if alternate in self._turn_sandbox_styles_tried:
                return None
            self._turn_sandbox_style = alternate
            self._turn_sandbox_styles_tried.add(alternate)
            self._emit(
                "status",
                text=(
                    "Codex sandbox policy syntax differs from the configured "
                    f"protocol; retrying turn startup with {alternate}-case variants"
                ),
                status="compatibility_retry",
            )
            return LiveSessionUpdate(outbound=(self._turn_start_request(),))
        return None

    @staticmethod
    def _is_variant_error(detail: str) -> bool:
        normalized = detail.casefold()
        return "unknown variant" in normalized or "unknown enum" in normalized

    def _allocate_protocol_request_id(self) -> int:
        request_id = self._next_protocol_id
        self._next_protocol_id += 1
        return request_id

    def _steer_request(self, text: str) -> dict[str, Any]:
        request = self._request(
            "turn/steer",
            {
                "threadId": self.thread_id,
                "input": [{"type": "text", "text": text}],
                "expectedTurnId": self.turn_id,
            },
        )
        self._emit("operator_ack", text=text, status="sent")
        return request

    def _flush_pending_steering(self) -> list[dict[str, Any]]:
        if not self.thread_id or not self.turn_id:
            return []
        pending, self._pending_steering = self._pending_steering, []
        return [self._steer_request(text) for text in pending]

    def _request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._control_requests[request_id] = method
        return {"method": method, "id": request_id, "params": dict(params)}

    def _best_final_message(self) -> str:
        if self._authoritative_messages:
            return self._authoritative_messages[-1]
        if self._agent_message_parts:
            return list(self._agent_message_parts.values())[-1]
        return self.final_message

    def _emit(
        self,
        kind: str,
        *,
        text: str = "",
        title: str = "",
        status: str = "",
        item_id: str = "",
        data: Mapping[str, Any] | None = None,
    ) -> None:
        callback = self._emit_callback
        if callback is None:
            return
        event = {
            "kind": kind,
            "text": text,
            "title": title,
            "status": status,
            "item_id": item_id,
            "data": dict(data or {}),
            "provider": "codex",
            "transport": "codex-app-server",
            "control_mode": "live_steering",
            "interaction_mode": "conversation",
            "streaming": True,
            "steering_supported": True,
        }
        try:
            callback(event)
        except Exception:
            pass


def _string_value(mapping: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, Mapping):
            nested = value.get("text")
            if isinstance(nested, str):
                return nested
    return ""
