from __future__ import annotations

import sys
from pathlib import Path

from execraft.agents.claude_code_adapter import ClaudeCodeAgentAdapter
from execraft.agents.claude_live import ClaudeStreamController
from execraft.agents.codex_adapter import CodexAgentAdapter
from execraft.agents.codex_live import (
    CodexAppServerController,
    _codex_compatible_output_schema,
)
from execraft.agents.live_session import (
    LiveProcessResult,
    LiveSessionUnavailable,
    LiveSessionUpdate,
    managed_jsonl_session,
)
from execraft.orchestrate.agent_console import AgentConsoleStore, InteractiveTerminalPolicy
from execraft.orchestrate.scheduler import StructuredHandoff


def _handoff(tmp_path: Path, **overrides) -> StructuredHandoff:
    values = {
        "work_package_id": "pkg-live",
        "stage": "implement",
        "summary": "Implement the live feature",
        "working_directory": str(tmp_path),
    }
    values.update(overrides)
    return StructuredHandoff(**values)


def test_managed_jsonl_session_round_trip(tmp_path: Path):
    script = tmp_path / "provider.py"
    script.write_text(
        """
import json, sys
message = json.loads(sys.stdin.readline())
print(json.dumps({"type": "reply", "value": message["value"] + 1}), flush=True)
""".strip(),
        encoding="utf-8",
    )
    received = []

    def handle(message):
        received.append(message)
        return LiveSessionUpdate(completed=True)

    result = managed_jsonl_session(
        [sys.executable, str(script)],
        cwd=tmp_path,
        timeout=5,
        initial_messages=({"value": 4},),
        message_callback=handle,
    )

    assert result.returncode == 0
    assert result.completed_by_protocol is True
    assert received == [{"type": "reply", "value": 5}]



def test_managed_jsonl_session_refreshes_timeout_on_protocol_progress(tmp_path: Path):
    script = tmp_path / "provider-progress.py"
    script.write_text(
        """
import json, time
for index in range(3):
    print(json.dumps({"type": "progress", "index": index}), flush=True)
    time.sleep(0.4)
print(json.dumps({"type": "done"}), flush=True)
""".strip(),
        encoding="utf-8",
    )
    received = []

    def handle(message):
        received.append(message)
        if message["type"] == "done":
            return LiveSessionUpdate(completed=True)
        return None

    result = managed_jsonl_session(
        [sys.executable, str(script)],
        cwd=tmp_path,
        timeout=0.7,
        startup_timeout=5,
        initial_messages=(),
        message_callback=handle,
        refresh_timeout_on_protocol_progress=True,
    )

    assert result.completed_by_protocol is True
    assert [message["type"] for message in received] == [
        "progress",
        "progress",
        "progress",
        "done",
    ]


def test_managed_jsonl_session_has_independent_startup_timeout(tmp_path: Path):
    script = tmp_path / "provider-no-protocol.py"
    script.write_text(
        "import time\ntime.sleep(5)\n",
        encoding="utf-8",
    )

    import pytest

    from execraft.process import ManagedProcessTerminated

    with pytest.raises(ManagedProcessTerminated) as raised:
        managed_jsonl_session(
            [sys.executable, str(script)],
            cwd=tmp_path,
            timeout=10,
            startup_timeout=0.3,
            initial_messages=(),
            message_callback=lambda _message: None,
        )

    assert raised.value.signal.category == "first_output_timeout"

def test_managed_jsonl_session_delivers_steering_while_process_is_running(tmp_path: Path):
    script = tmp_path / "provider-steer.py"
    script.write_text(
        """
import json, sys
initial = json.loads(sys.stdin.readline())
print(json.dumps({"type": "ready", "initial": initial["text"]}), flush=True)
steering = json.loads(sys.stdin.readline())
print(json.dumps({"type": "steered", "text": steering["text"]}), flush=True)
""".strip(),
        encoding="utf-8",
    )
    received = []
    controls = []

    def handle(message):
        received.append(message)
        if message["type"] == "ready":
            controls.append({"text": "change direction"})
            return None
        return LiveSessionUpdate(completed=True)

    def poll_controls():
        if not controls:
            return ()
        return (controls.pop(0),)

    result = managed_jsonl_session(
        [sys.executable, str(script)],
        cwd=tmp_path,
        timeout=5,
        initial_messages=({"text": "start"},),
        message_callback=handle,
        input_callback=poll_controls,
    )

    assert result.completed_by_protocol is True
    assert received == [
        {"type": "ready", "initial": "start"},
        {"type": "steered", "text": "change direction"},
    ]


def test_codex_controller_streams_and_steers(tmp_path: Path):
    events = []
    controls = [{"action": "steer", "data": "Focus on the failing test"}]
    controller = CodexAppServerController(
        handoff=_handoff(tmp_path),
        prompt="Do the work",
        model="",
        sandbox="workspace-write",
        workdir=tmp_path,
        interaction_callback=events.append,
        control_callback=lambda: controls.pop(0) if False else [],
    )
    assert controller.initial_messages[0]["method"] == "initialize"
    initialized = controller.handle_message({"id": 1, "result": {}})
    assert [item["method"] for item in initialized.outbound] == ["initialized", "thread/start"]
    started = controller.handle_message({"id": 2, "result": {"thread": {"id": "thread-1"}}})
    assert started.outbound[0]["method"] == "turn/start"
    controller.handle_message({"id": 3, "result": {"turn": {"id": "turn-1"}}})

    controller._control_callback = lambda: [{"action": "steer", "data": "Focus on the failing test"}]
    outbound = controller.poll_controls()
    assert outbound[0]["method"] == "turn/steer"
    assert outbound[0]["params"]["expectedTurnId"] == "turn-1"

    controller.handle_message({"method": "item/agentMessage/delta", "params": {"itemId": "a", "delta": "Working"}})
    controller.handle_message({"method": "turn/plan/updated", "params": {"plan": [{"step": "Run tests", "status": "in_progress"}]}})
    controller.handle_message({"method": "turn/diff/updated", "params": {"diff": "+fixed"}})
    completed = controller.handle_message({"method": "turn/completed", "params": {"turn": {"status": "completed"}}})

    assert completed.completed is True
    assert controller.final_message == "Working"
    assert {event["kind"] for event in events} >= {"session", "operator_ack", "assistant_delta", "plan", "diff", "status"}


def test_codex_controller_strips_unsupported_output_schema_keywords(
    tmp_path: Path,
):
    # Supervisor runtime no longer uses provider-native Structured Outputs. Use
    # an explicit generic contract here so this test stays focused on the Codex
    # schema transport transformation itself.
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["paths", "decision"],
        "properties": {
            "paths": {
                "type": "array",
                "maxItems": 512,
                "uniqueItems": True,
                "items": {"type": "string"},
            },
            "decision": {"type": "string"},
        },
    }
    controller = CodexAppServerController(
        handoff=_handoff(tmp_path, expected_output_schema=schema),
        prompt="Supervise",
        model="",
        sandbox="workspace-write",
        workdir=tmp_path,
        interaction_callback=None,
        control_callback=None,
    )

    controller.initial_messages
    initialized = controller.handle_message({"id": 1, "result": {}})
    turn = controller.handle_message(
        {
            "id": initialized.outbound[-1]["id"],
            "result": {"thread": {"id": "thread-1"}},
        }
    ).outbound[0]
    sent_schema = turn["params"]["outputSchema"]

    assert "uniqueItems" not in sent_schema["properties"]["paths"]
    assert sent_schema["properties"]["paths"]["maxItems"] == 512
    assert sent_schema["properties"]["decision"]["type"] == "string"
    assert schema["properties"]["paths"]["uniqueItems"] is True


def test_codex_schema_transform_adds_types_for_const_and_enum():
    transformed = _codex_compatible_output_schema(
        {
            "type": "object",
            "properties": {
                "status": {"const": "ok"},
                "verdict": {"enum": ["approved", "changes_required"]},
                "enabled": {"const": True},
            },
        }
    )

    assert transformed["properties"]["status"]["type"] == "string"
    assert transformed["properties"]["verdict"]["type"] == "string"
    assert transformed["properties"]["enabled"]["type"] == "boolean"


def test_codex_schema_transform_closes_every_object_and_requires_properties():
    transformed = _codex_compatible_output_schema(
        {
            "type": "object",
            "additionalProperties": True,
            "required": ["status"],
            "x-execraft-private": 3,
            "properties": {
                "status": {"enum": ["implemented"]},
                "details": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "properties": {"summary": {"type": "string"}},
                },
            },
        }
    )

    assert transformed["additionalProperties"] is False
    assert transformed["required"] == ["status", "details"]
    assert "x-execraft-private" not in transformed
    details = transformed["properties"]["details"]
    assert details["additionalProperties"] is False
    assert details["required"] == ["summary"]


def test_claude_controller_queues_operator_message_and_streams(tmp_path: Path):
    events = []
    pending = [[{"action": "steer", "data": "Use the simpler API"}], []]
    controller = ClaudeStreamController(
        handoff=_handoff(tmp_path),
        prompt="Do the work",
        interaction_callback=events.append,
        control_callback=lambda: pending.pop(0),
    )
    assert controller.initial_messages[0]["type"] == "user"
    controller.handle_message({"type": "system", "subtype": "init", "session_id": "session-1"})
    controller.handle_message({
        "type": "stream_event",
        "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Working"}},
    })
    outbound = controller.poll_controls()
    assert outbound[0]["type"] == "user"
    assert outbound[0]["message"]["content"] == "Use the simpler API"
    completed = controller.handle_message({
        "type": "result",
        "is_error": False,
        "result": "Done",
        "session_id": "session-1",
        "usage": {"input_tokens": 10},
    })
    assert completed.completed is True
    assert controller.final_message == "Done"
    assert {event["kind"] for event in events} >= {"session", "assistant_delta", "operator_ack", "status"}


def test_codex_adapter_uses_app_server_live_contract(monkeypatch, tmp_path: Path):
    emitted = []

    def fake_session(args, **kwargs):
        assert args == [sys.executable, "app-server"]
        assert kwargs["refresh_timeout_on_protocol_progress"] is True
        controller = kwargs["message_callback"].__self__
        controller.handle_message({"id": 1, "result": {}})
        controller.handle_message({"id": 2, "result": {"thread": {"id": "thr"}}})
        controller.handle_message({"id": 3, "result": {"turn": {"id": "turn"}}})
        controller.handle_message({"method": "item/agentMessage/delta", "params": {"itemId": "m", "delta": "Done"}})
        controller.handle_message({"method": "turn/completed", "params": {"turn": {"status": "completed"}}})
        return LiveProcessResult(0, "", "", completed_by_protocol=True)

    monkeypatch.setattr("execraft.agents.codex_adapter.managed_jsonl_session", fake_session)
    adapter = CodexAgentAdapter(workdir=tmp_path, binary=sys.executable)
    adapter.configure_interaction(emitted.append)

    result = adapter.execute(_handoff(tmp_path))

    assert result["final_message"] == "Done"
    assert result["interaction_mode"] == "conversation"
    assert adapter.steering_supported is True
    assert any(event["kind"] == "assistant_delta" for event in emitted)


def test_claude_adapter_uses_stream_json_live_contract(monkeypatch, tmp_path: Path):
    emitted = []

    def fake_session(args, **kwargs):
        assert "--input-format" in args and args[args.index("--input-format") + 1] == "stream-json"
        assert kwargs["refresh_timeout_on_protocol_progress"] is True
        assert "--include-partial-messages" in args
        assert "--replay-user-messages" in args
        assert "--forward-subagent-text" in args
        controller = kwargs["message_callback"].__self__
        controller.handle_message({"type": "system", "subtype": "init", "session_id": "s"})
        controller.handle_message({"type": "assistant", "message": {"content": [{"type": "text", "text": "Done"}]}})
        controller.handle_message({"type": "result", "is_error": False, "result": "Done", "session_id": "s"})
        return LiveProcessResult(0, "", "", completed_by_protocol=True)

    monkeypatch.setattr("execraft.agents.claude_code_adapter.managed_jsonl_session", fake_session)
    adapter = ClaudeCodeAgentAdapter(workdir=tmp_path, binary=sys.executable)
    adapter.configure_interaction(emitted.append)

    result = adapter.execute(_handoff(tmp_path))

    assert result["final_message"] == "Done"
    assert result["interaction_mode"] == "conversation"
    assert any(event["kind"] == "assistant" for event in emitted)


def test_claude_controller_associates_streamed_tool_input_with_index_zero(tmp_path: Path):
    events = []
    controller = ClaudeStreamController(
        handoff=_handoff(tmp_path),
        prompt="Do the work",
        interaction_callback=events.append,
        control_callback=None,
    )

    controller.handle_message(
        {
            "type": "stream_event",
            "parent_tool_use_id": "subagent-parent",
            "event": {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "tool-1", "name": "Read", "input": {}},
            },
        }
    )
    controller.handle_message(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"file_path":"src/execraft/gui/server.py"}'},
            },
        }
    )
    controller.handle_message(
        {
            "type": "stream_event",
            "event": {"type": "content_block_stop", "index": 0},
        }
    )

    delta = next(event for event in events if event["kind"] == "tool_input_delta")
    final = [event for event in events if event["kind"] == "tool"][-1]
    assert delta["item_id"] == "tool-1"
    assert delta["title"] == "Inspect file"
    assert delta["target"] == "src/execraft/gui/server.py"
    assert delta["parent_item_id"] == "subagent-parent"
    assert final["item_id"] == "tool-1"
    assert final["data"]["input"] == {"file_path": "src/execraft/gui/server.py"}


def test_claude_adapter_retries_live_stream_without_optional_subagent_flag(
    monkeypatch, tmp_path: Path
):
    emitted = []
    invocations = []

    def fake_session(args, **kwargs):
        invocations.append(list(args))
        if "--forward-subagent-text" in args:
            raise LiveSessionUnavailable("unknown option --forward-subagent-text")
        controller = kwargs["message_callback"].__self__
        controller.handle_message({"type": "system", "subtype": "init", "session_id": "s"})
        controller.handle_message({"type": "result", "is_error": False, "result": "Done", "session_id": "s"})
        return LiveProcessResult(0, "", "", completed_by_protocol=True)

    monkeypatch.setattr("execraft.agents.claude_code_adapter.managed_jsonl_session", fake_session)
    adapter = ClaudeCodeAgentAdapter(workdir=tmp_path, binary=sys.executable)
    adapter.configure_interaction(emitted.append)

    result = adapter.execute(_handoff(tmp_path))

    assert result["final_message"] == "Done"
    assert len(invocations) == 2
    assert "--forward-subagent-text" in invocations[0]
    assert "--forward-subagent-text" not in invocations[1]
    assert any(event.get("status") == "compatibility_retry" for event in emitted)


def test_console_store_exposes_semantic_events_and_accepts_steering(tmp_path: Path):
    store = AgentConsoleStore(
        tmp_path / "console",
        terminal_policy=InteractiveTerminalPolicy(enabled=True),
    )
    payload = {
        "agent_id": "codex",
        "package_id": "pkg-live",
        "stage": "supervise",
        "interaction_mode": "conversation",
        "streaming_interaction": True,
        "steering_supported": True,
    }
    session = store.start(payload)
    store.append_interaction({**payload, "kind": "assistant_delta", "text": "Investigating"})
    accepted = store.queue_control_event(session["session_id"], action="steer", data="Check HANDOFF.md")
    result = store.read_events(session["session_id"], include_events=False)

    assert accepted["accepted"] is True
    assert [item["kind"] for item in result["interactions"]] == ["assistant_delta", "operator"]
    assert result["metadata"]["interaction"]["last_operator_message_at"]
    controls = store.consume_control_events(payload)
    assert controls[0]["action"] == "steer"
    assert controls[0]["data"] == "Check HANDOFF.md"


def test_codex_controller_interrupts_active_turn(tmp_path: Path):
    events = []
    controller = CodexAppServerController(
        handoff=_handoff(tmp_path),
        prompt="Do the work",
        model="",
        sandbox="workspace-write",
        workdir=tmp_path,
        interaction_callback=events.append,
        control_callback=lambda: [{"action": "signal", "signal": "interrupt"}],
    )
    controller.initial_messages
    controller.handle_message({"id": 1, "result": {}})
    controller.handle_message({"id": 2, "result": {"thread": {"id": "thread-1"}}})
    controller.handle_message({"id": 3, "result": {"turn": {"id": "turn-1"}}})

    outbound = controller.poll_controls()

    assert outbound == (
        {
            "method": "turn/interrupt",
            "id": 100,
            "params": {"threadId": "thread-1", "turnId": "turn-1"},
        },
    )
    assert any(event["text"] == "Interrupt requested" for event in events)


def test_claude_replayed_user_message_acknowledges_delivery(tmp_path: Path):
    events = []
    controller = ClaudeStreamController(
        handoff=_handoff(tmp_path),
        prompt="Do the work",
        interaction_callback=events.append,
        control_callback=None,
    )

    controller.handle_message(
        {"type": "user", "message": {"role": "user", "content": "Steer now"}}
    )

    assert events[-1]["kind"] == "operator_ack"
    assert events[-1]["status"] == "accepted"
    assert events[-1]["text"] == "Steer now"


def test_codex_controller_preserves_configured_sandbox_policy(tmp_path: Path):
    full = CodexAppServerController(
        handoff=_handoff(tmp_path),
        prompt="Do the work",
        model="",
        sandbox="danger-full-access",
        workdir=tmp_path,
        interaction_callback=None,
        control_callback=None,
    )
    full.initial_messages
    initialized = full.handle_message({"id": 1, "result": {}})
    assert initialized.outbound[1]["params"]["sandbox"] == "danger-full-access"
    request = full.handle_message(
        {"id": 2, "result": {"thread": {"id": "thread-full"}}}
    ).outbound[0]
    assert request["params"]["sandboxPolicy"] == {"type": "dangerFullAccess"}

    read_only = CodexAppServerController(
        handoff=_handoff(tmp_path, read_only=True),
        prompt="Review",
        model="",
        sandbox="danger-full-access",
        workdir=tmp_path,
        interaction_callback=None,
        control_callback=None,
    )
    assert read_only.initial_messages
    initialized = read_only.handle_message({"id": 1, "result": {}})
    assert initialized.outbound[1]["params"]["sandbox"] == "read-only"
    request = read_only.handle_message(
        {"id": 2, "result": {"thread": {"id": "thread-ro"}}}
    ).outbound[0]
    assert request["params"]["sandboxPolicy"] == {"type": "readOnly"}

    workspace = CodexAppServerController(
        handoff=_handoff(
            tmp_path,
            additional_writable_roots=[str(tmp_path / "project-control")],
        ),
        prompt="Repair",
        model="",
        sandbox="workspace-write",
        workdir=tmp_path,
        interaction_callback=None,
        control_callback=None,
    )
    assert workspace.initial_messages
    initialized = workspace.handle_message({"id": 1, "result": {}})
    assert initialized.outbound[1]["params"]["sandbox"] == "workspace-write"
    request = workspace.handle_message(
        {"id": 2, "result": {"thread": {"id": "thread-workspace"}}}
    ).outbound[0]
    assert request["params"]["sandboxPolicy"] == {
        "type": "workspaceWrite",
        "writableRoots": [str(tmp_path), str(tmp_path / "project-control")],
        "networkAccess": False,
    }


def test_codex_controller_retries_camel_thread_sandbox_variant(tmp_path: Path):
    events = []
    controller = CodexAppServerController(
        handoff=_handoff(tmp_path),
        prompt="Do the work",
        model="",
        sandbox="workspace-write",
        workdir=tmp_path,
        interaction_callback=events.append,
        control_callback=None,
    )
    controller.initial_messages
    initialized = controller.handle_message({"id": 1, "result": {}})
    first_thread = initialized.outbound[-1]
    assert first_thread["params"]["sandbox"] == "workspace-write"

    retry = controller.handle_message(
        {
            "id": first_thread["id"],
            "error": {
                "message": (
                    "unknown variant `workspace-write`, expected one of "
                    "`readOnly`, `workspaceWrite`, `dangerFullAccess`"
                )
            },
        }
    )

    assert retry is not None
    second_thread = retry.outbound[0]
    assert second_thread["params"]["sandbox"] == "workspaceWrite"
    assert second_thread["id"] != first_thread["id"]
    assert any(event["status"] == "compatibility_retry" for event in events)


def test_codex_controller_retries_legacy_kebab_sandbox_policy(tmp_path: Path):
    events = []
    controller = CodexAppServerController(
        handoff=_handoff(tmp_path),
        prompt="Do the work",
        model="",
        sandbox="workspace-write",
        workdir=tmp_path,
        interaction_callback=events.append,
        control_callback=None,
    )
    controller.initial_messages
    initialized = controller.handle_message({"id": 1, "result": {}})
    thread_request = initialized.outbound[-1]
    assert thread_request["params"]["sandbox"] == "workspace-write"

    thread_started = controller.handle_message(
        {"id": thread_request["id"], "result": {"thread": {"id": "thread-1"}}}
    )
    first_turn = thread_started.outbound[0]
    assert first_turn["params"]["sandboxPolicy"]["type"] == "workspaceWrite"

    retry = controller.handle_message(
        {
            "id": first_turn["id"],
            "error": {
                "message": (
                    "unknown variant `workspaceWrite`, expected one of "
                    "`read-only`, `workspace-write`, `danger-full-access`"
                )
            },
        }
    )

    assert retry is not None
    second_turn = retry.outbound[0]
    assert second_turn["params"]["sandboxPolicy"] == {"type": "workspace-write"}
    assert any(event["status"] == "compatibility_retry" for event in events)


def test_codex_rejected_steering_does_not_abort_agent_turn(tmp_path: Path):
    events = []
    controller = CodexAppServerController(
        handoff=_handoff(tmp_path),
        prompt="Do the work",
        model="",
        sandbox="workspace-write",
        workdir=tmp_path,
        interaction_callback=events.append,
        control_callback=lambda: [{"action": "steer", "data": "Change direction"}],
    )
    controller.initial_messages
    controller.handle_message({"id": 1, "result": {}})
    controller.handle_message({"id": 2, "result": {"thread": {"id": "thread-1"}}})
    controller.handle_message({"id": 3, "result": {"turn": {"id": "turn-1"}}})
    request = controller.poll_controls()[0]

    update = controller.handle_message(
        {"id": request["id"], "error": {"message": "turn changed"}}
    )

    assert update is None
    assert controller.error == ""
    assert events[-1]["kind"] == "operator_ack"
    assert events[-1]["status"] == "failed"
