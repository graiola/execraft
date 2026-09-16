from __future__ import annotations

import json
from pathlib import Path

import pytest

from execraft.orchestrate.agent_console import AgentConsoleStore


def _started(agent: str = "qwen") -> dict:
    return {
        "package_id": "WP1__S1",
        "stage": "review",
        "capability": "review",
        "agent_id": agent,
        "adapter": "opencode",
        "model": "ollama/qwen",
        "attempt": 2,
    }


def _pty_started(agent: str = "qwen") -> dict:
    return {**_started(agent), "interaction_mode": "terminal", "interactive_pty": True}


def test_console_store_captures_incremental_output_and_metadata(tmp_path: Path):
    store = AgentConsoleStore(tmp_path / "agent-console")
    metadata = store.start(_started())

    store.append({**_started(), "stream": "stdout", "text": "checking\n"})
    store.append({**_started(), "stream": "stderr", "text": "warning\n"})
    store.heartbeat(
        {
            **_started(),
            "pid": 123,
            "pgid": 123,
            "process_state": "sleeping",
            "process_count": 2,
            "last_output_age_seconds": 4.5,
        }
    )

    first = store.read_events(metadata["session_id"], offset=0)
    assert [item["stream"] for item in first["events"]] == ["stdout", "stderr"]
    assert "checking" in first["events"][0]["text"]
    assert first["metadata"]["telemetry"]["pid"] == 123

    store.append({**_started(), "stream": "stdout", "text": "done\n"})
    second = store.read_events(metadata["session_id"], offset=first["next_offset"])
    assert [item["text"] for item in second["events"]] == ["done\n"]

    artifact = {"path": str(tmp_path / "agent-artifacts" / "WP1__S1" / "result.json")}
    store.finish(
        {
            **_started(),
            "status": "completed",
            "duration_seconds": 12.5,
            "artifact": artifact,
        }
    )
    final = store.read_events(metadata["session_id"])["metadata"]
    assert final["status"] == "completed"
    assert final["duration_seconds"] == 12.5
    assert final["artifact"] == artifact


def test_console_store_lists_only_requested_agent(tmp_path: Path):
    store = AgentConsoleStore(tmp_path / "agent-console")
    first = store.start(_started("qwen-a"))
    store.finish({**_started("qwen-a"), "status": "completed"})
    store.start(_started("qwen-b"))

    rows = store.sessions("qwen-a")
    assert [item["session_id"] for item in rows] == [first["session_id"]]


def test_console_store_rejects_session_traversal(tmp_path: Path):
    store = AgentConsoleStore(tmp_path / "agent-console")
    with pytest.raises(ValueError, match="invalid agent console session"):
        store.read_events("../state.json")


def test_console_store_reads_only_task_agent_artifacts(tmp_path: Path):
    console_root = tmp_path / "project" / "agent-console"
    artifact = tmp_path / "project" / "agent-artifacts" / "WP1" / "result.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(json.dumps({"ok": True}), encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    store = AgentConsoleStore(console_root)

    assert '"ok": true' in store.read_artifact(str(artifact))["content"]
    with pytest.raises(ValueError, match="outside"):
        store.read_artifact(str(outside))


def test_console_store_bounds_live_capture_and_marks_truncation(tmp_path: Path):
    store = AgentConsoleStore(tmp_path / "agent-console", max_session_bytes=4096)
    session = store.start(_started())
    for _ in range(4):
        store.append({**_started(), "stream": "stdout", "text": "x" * 2048})

    result = store.read_events(session["session_id"])
    assert result["metadata"]["truncated"] is True
    assert any(item["stream"] == "system" for item in result["events"])
    assert result["size_bytes"] < 12_000


def test_console_store_prunes_old_completed_sessions_per_agent(tmp_path: Path):
    store = AgentConsoleStore(tmp_path / "agent-console", max_sessions_per_agent=2)
    session_ids = []
    for index in range(3):
        payload = {**_started(), "stage": f"review-{index}"}
        session_ids.append(store.start(payload)["session_id"])
        store.finish({**payload, "status": "completed"})

    remaining = {item["session_id"] for item in store.sessions("qwen")}
    assert session_ids[-1] in remaining
    assert session_ids[0] not in remaining
    assert len(remaining) == 2


def test_interactive_terminal_policy_validates_limits():
    from execraft.orchestrate.agent_console import InteractiveTerminalPolicy

    policy = InteractiveTerminalPolicy.from_mapping(
        {
            "enabled": True,
            "max_input_event_bytes": 2048,
            "max_pending_input_bytes": 8192,
            "default_rows": 50,
            "default_columns": 160,
        }
    )
    assert policy.enabled is True
    assert policy.default_columns == 160
    with pytest.raises(ValueError, match="enabled must be a boolean"):
        InteractiveTerminalPolicy.from_mapping({"enabled": "yes"})
    with pytest.raises(ValueError, match="unsupported keys"):
        InteractiveTerminalPolicy.from_mapping({"shell": True})


def test_console_store_queues_consumes_and_audits_terminal_input(tmp_path: Path):
    from execraft.orchestrate.agent_console import InteractiveTerminalPolicy

    store = AgentConsoleStore(
        tmp_path / "agent-console",
        terminal_policy=InteractiveTerminalPolicy(enabled=True),
    )
    session = store.start(_pty_started())
    result = store.queue_control_event(
        session["session_id"], action="input", data="continue\n"
    )
    store.queue_control_event(
        session["session_id"], action="resize", rows=48, columns=150
    )

    assert result["accepted"] is True
    records = store.consume_control_events(_started())
    assert records[0]["data"] == "continue\n"
    assert records[1]["rows"] == 48
    metadata = store.read_events(session["session_id"])["metadata"]
    assert metadata["controls"]["control_offset"] == 0
    control_path = (
        tmp_path / "agent-console" / session["session_id"] / "control.jsonl"
    )
    assert control_path.stat().st_size == 0

    audit = (tmp_path / "agent-console" / "operator-input-audit.jsonl").read_text()
    assert "continue" not in audit
    assert '"bytes": 9' in audit
    assert '"sha256":' in audit


def test_console_store_terminal_input_is_fail_closed(tmp_path: Path):
    from execraft.orchestrate.agent_console import InteractiveTerminalPolicy

    disabled = AgentConsoleStore(tmp_path / "disabled")
    disabled_session = disabled.start(_pty_started())
    with pytest.raises(ValueError, match="disabled"):
        disabled.queue_control_event(
            disabled_session["session_id"], action="input", data="x"
        )

    enabled = AgentConsoleStore(
        tmp_path / "enabled",
        terminal_policy=InteractiveTerminalPolicy(
            enabled=True, max_input_event_bytes=4, max_pending_input_bytes=1024
        ),
    )
    session = enabled.start(_pty_started())
    with pytest.raises(ValueError, match="per-event"):
        enabled.queue_control_event(
            session["session_id"], action="input", data="12345"
        )
    enabled.finish({**_started(), "status": "completed"})
    with pytest.raises(ValueError, match="running"):
        enabled.queue_control_event(
            session["session_id"], action="input", data="x"
        )



def test_console_store_enforces_pending_queue_limit_before_append(tmp_path: Path):
    from execraft.orchestrate.agent_console import InteractiveTerminalPolicy

    store = AgentConsoleStore(
        tmp_path / "agent-console",
        terminal_policy=InteractiveTerminalPolicy(
            enabled=True,
            max_input_event_bytes=800,
            max_pending_input_bytes=1024,
        ),
    )
    session = store.start(_pty_started())
    store.queue_control_event(
        session["session_id"], action="input", data="x" * 700
    )

    with pytest.raises(ValueError, match="queue is full"):
        store.queue_control_event(
            session["session_id"], action="input", data="y" * 700
        )

    control_path = (
        tmp_path / "agent-console" / session["session_id"] / "control.jsonl"
    )
    assert control_path.read_text(encoding="utf-8").count("\n") == 1


def test_console_store_surfaces_failed_session_in_conversation(tmp_path: Path):
    store = AgentConsoleStore(tmp_path / "agent-console")
    payload = {
        **_started(),
        "interaction_mode": "conversation",
        "streaming_interaction": True,
        "steering_supported": True,
    }
    session = store.start(payload)

    store.finish(
        {
            **payload,
            "status": "failed",
            "detail": "timeout: no protocol progress",
        }
    )
    result = store.read_events(session["session_id"], include_events=False)

    assert result["metadata"]["status"] == "failed"
    assert result["interactions"][-1]["kind"] == "status"
    assert result["interactions"][-1]["status"] == "failed"
    assert "no protocol progress" in result["interactions"][-1]["text"]


def test_console_policy_does_not_create_pty_controls_for_opencode(tmp_path: Path):
    from execraft.orchestrate.agent_console import InteractiveTerminalPolicy

    store = AgentConsoleStore(
        tmp_path / "agent-console",
        terminal_policy=InteractiveTerminalPolicy(enabled=True),
    )
    payload = {
        **_started(),
        "interaction_mode": "conversation",
        "streaming_interaction": True,
        "steering_supported": False,
        "interactive_pty": False,
    }
    session = store.start(payload)
    store.append_interaction(
        {**payload, "kind": "assistant_delta", "text": "Inspecting files"}
    )

    metadata = store.read_events(session["session_id"])["metadata"]
    assert metadata["controls"]["enabled"] is False
    assert metadata["terminal"]["enabled"] is False
    assert metadata["interaction"]["streaming"] is True
    with pytest.raises(ValueError, match="controls are disabled"):
        store.queue_control_event(
            session["session_id"], action="input", data="continue\n"
        )

def test_console_store_persists_latest_terminal_screen(tmp_path: Path):
    store = AgentConsoleStore(tmp_path / "agent-console")
    session = store.start(_pty_started())

    store.update_terminal_screen(
        _started(),
        {
            "content": "Claude Code\n> ready",
            "rows": 40,
            "columns": 120,
            "cursor_row": 1,
            "cursor_column": 7,
            "cursor_visible": True,
            "revision": 4,
        },
    )

    result = store.read_events(session["session_id"])
    assert result["terminal_screen"]["content"] == "Claude Code\n> ready"
    assert result["terminal_screen"]["revision"] == 4
    assert result["terminal_screen"]["updated_at"]


def test_console_store_renders_raw_terminal_output_for_orchestrated_sessions(tmp_path: Path):
    from execraft.orchestrate.agent_console import InteractiveTerminalPolicy

    store = AgentConsoleStore(
        tmp_path / "agent-console",
        terminal_policy=InteractiveTerminalPolicy(enabled=True),
    )
    session = store.start(_pty_started())

    store.append(
        {
            **_started(),
            "stream": "terminal",
            "text": "status: waiting\rstatus: ready\x1b[K",
        }
    )

    result = store.read_events(session["session_id"])
    assert result["terminal_screen"]["content"] == "status: ready"
    assert result["events"][0]["text"] == "status: ready"

    store.queue_control_event(
        session["session_id"], action="resize", rows=48, columns=150
    )
    resized = store.read_events(session["session_id"])["terminal_screen"]
    assert resized["rows"] == 48
    assert resized["columns"] == 150


def test_console_store_batches_high_frequency_events_until_read(tmp_path: Path, monkeypatch):
    # GitHub-hosted runners may have less uptime than the configured interval.
    # The first event must still be published immediately.
    monkeypatch.setattr("execraft.orchestrate.agent_console.time.monotonic", lambda: 1.0)
    store = AgentConsoleStore(
        tmp_path / "agent-console",
        event_flush_interval_seconds=60,
    )
    session = store.start(_started())
    event_path = (
        tmp_path / "agent-console" / session["session_id"] / "events.jsonl"
    )

    store.append({**_started(), "stream": "terminal", "text": "frame 0\n"})
    first_flush_size = event_path.stat().st_size
    assert first_flush_size > 0

    for index in range(1, 20):
        store.append({**_started(), "stream": "terminal", "text": f"frame {index}\n"})

    # The first frame is immediately visible; the burst that follows is
    # coalesced until the reader asks for the next delta.
    assert event_path.stat().st_size == first_flush_size
    result = store.read_events(session["session_id"])
    assert len(result["events"]) == 20
    assert event_path.stat().st_size > first_flush_size


def test_console_store_omits_unchanged_terminal_screen_by_token(tmp_path: Path):
    from execraft.orchestrate.agent_console import InteractiveTerminalPolicy

    store = AgentConsoleStore(
        tmp_path / "agent-console",
        terminal_policy=InteractiveTerminalPolicy(enabled=True),
    )
    session = store.start(_pty_started())
    store.append({**_started(), "stream": "terminal", "text": "ready"})

    first = store.read_events(session["session_id"])
    assert first["terminal_screen"]["content"] == "ready"
    assert first["terminal_screen_token"]

    second = store.read_events(
        session["session_id"],
        terminal_screen_token=first["terminal_screen_token"],
    )
    assert second["terminal_screen"] == {}
    assert second["terminal_screen_unchanged"] is True
    assert second["terminal_screen_token"] == first["terminal_screen_token"]


def test_running_console_terminal_poll_uses_memory_without_flushing_activity(tmp_path: Path):
    from execraft.orchestrate.agent_console import InteractiveTerminalPolicy

    store = AgentConsoleStore(
        tmp_path / "agent-console",
        terminal_policy=InteractiveTerminalPolicy(enabled=True),
        event_flush_interval_seconds=60,
        screen_flush_interval_seconds=60,
        metadata_flush_interval_seconds=60,
    )
    session = store.start(_pty_started())
    store.append({**_started(), "stream": "terminal", "text": "frame one\n"})
    event_path = tmp_path / "agent-console" / session["session_id"] / "events.jsonl"
    first_size = event_path.stat().st_size
    store.append({**_started(), "stream": "terminal", "text": "frame two\n"})

    terminal_poll = store.read_events(
        session["session_id"], include_events=False
    )
    assert terminal_poll["events"] == []
    assert terminal_poll["events_included"] is False
    assert event_path.stat().st_size == first_size
    assert "frame two" in terminal_poll["terminal_screen"]["content"]
    assert terminal_poll["terminal_screen_token"].startswith("memory:")

    unchanged = store.read_events(
        session["session_id"],
        include_events=False,
        terminal_screen_token=terminal_poll["terminal_screen_token"],
    )
    assert unchanged["terminal_screen"] == {}
    assert unchanged["terminal_screen_unchanged"] is True

    activity_poll = store.read_events(session["session_id"], include_events=True)
    assert activity_poll["events_included"] is True
    assert len(activity_poll["events"]) == 2
    assert event_path.stat().st_size > first_size


def test_console_store_batches_semantic_deltas_and_wakes_long_poll(tmp_path: Path):
    import threading
    import time

    from execraft.orchestrate.agent_console import InteractiveTerminalPolicy

    store = AgentConsoleStore(
        tmp_path / "agent-console",
        terminal_policy=InteractiveTerminalPolicy(enabled=True),
    )
    payload = {
        **_started(),
        "interaction_mode": "conversation",
        "streaming_interaction": True,
        "steering_supported": True,
    }
    session = store.start(payload)
    store.append_interaction({**payload, "kind": "assistant_delta", "item_id": "a", "text": "first"})
    first = store.read_events(session["session_id"], include_events=False)
    results = []

    def wait_for_delta():
        results.append(
            store.read_events(
                session["session_id"],
                include_events=False,
                interaction_offset=first["interaction_next_offset"],
                since_version=first["version"],
                wait_seconds=1.0,
            )
        )

    waiter = threading.Thread(target=wait_for_delta)
    waiter.start()
    time.sleep(0.01)
    store.append_interaction({**payload, "kind": "assistant_delta", "item_id": "a", "text": " second"})
    store.append_interaction({**payload, "kind": "assistant_delta", "item_id": "a", "text": " third"})
    waiter.join(timeout=2)

    assert not waiter.is_alive()
    assert len(results) == 1
    assert results[0]["version"] > first["version"]
    assert [event["text"] for event in results[0]["interactions"]] == [" second third"]


def test_console_store_enriches_tool_activity_and_exposes_progress_snapshot(tmp_path: Path):
    store = AgentConsoleStore(tmp_path / "agent-console")
    payload = {
        **_started(),
        "interaction_mode": "conversation",
        "streaming_interaction": True,
        "steering_supported": True,
        "transport": "claude-cli-stream-json",
        "control_mode": "queued_guidance",
    }
    session = store.start(payload)
    store.append_interaction(
        {
            **payload,
            "kind": "tool_input_delta",
            "item_id": "tool-tests",
            "title": "Bash",
            "text": '{"command":"python -m pytest tests/test_agent_console.py -q"}',
            "status": "running",
            "data": {"name": "Bash"},
        }
    )

    result = store.read_events(session["session_id"], include_events=False)
    event = result["interactions"][0]
    progress = result["metadata"]["interaction"]["progress"]
    heartbeat = store.progress_snapshot(payload)

    assert event["title"] == "Run tests"
    assert event["operation"] == "test"
    assert event["command"] == "python -m pytest tests/test_agent_console.py -q"
    assert event["target"] == "tests/test_agent_console.py"
    assert progress["current_activity"] == "Run tests"
    assert progress["commands"] == 1
    assert heartbeat["semantic_activity"] == "Run tests"
    assert heartbeat["semantic_target"] == "tests/test_agent_console.py"
    assert heartbeat["transport"] == "claude-cli-stream-json"
    assert heartbeat["control_mode"] == "queued_guidance"


def test_console_store_flags_repeated_tool_cycles_conservatively(tmp_path: Path):
    store = AgentConsoleStore(tmp_path / "agent-console")
    payload = {
        **_started(),
        "interaction_mode": "conversation",
        "streaming_interaction": True,
        "steering_supported": False,
        "transport": "opencode-run-jsonl",
        "control_mode": "observation",
    }
    session = store.start(payload)
    for index in range(4):
        store.append_interaction(
            {
                **payload,
                "kind": "tool",
                "item_id": f"read-{index}",
                "title": "Read",
                "status": "running",
                "data": {"input": {"file_path": "src/execraft/orchestrate/orchestrator.py"}},
            }
        )

    metadata = store.read_events(session["session_id"], include_events=False)["metadata"]
    progress = metadata["interaction"]["progress"]

    assert progress["state"] == "possibly_stalled"
    assert progress["repeat_count"] == 4
    assert "Repeated activity detected" in progress["warning"]


def test_console_store_accepts_semantic_interrupt_signal(tmp_path: Path):
    from execraft.orchestrate.agent_console import InteractiveTerminalPolicy

    store = AgentConsoleStore(
        tmp_path / "agent-console",
        terminal_policy=InteractiveTerminalPolicy(enabled=True),
    )
    payload = {
        **_started(),
        "interaction_mode": "conversation",
        "streaming_interaction": True,
        "steering_supported": True,
    }
    session = store.start(payload)

    accepted = store.queue_control_event(
        session["session_id"], action="signal", signal_name="interrupt"
    )
    controls = store.consume_control_events(payload)

    assert accepted["action"] == "signal"
    assert controls == [{"at": controls[0]["at"], "action": "signal", "signal": "interrupt"}]
