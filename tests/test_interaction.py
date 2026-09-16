from __future__ import annotations

from execraft.interaction import (
    describe_file_change,
    describe_tool_activity,
    normalize_interaction_payload,
    parse_partial_json_fields,
)
from execraft.orchestrate.live_progress import LiveProgressTracker


def test_read_tool_is_described_with_file_target() -> None:
    description = describe_tool_activity("Read", {"file_path": "src/execraft/gui/server.py"})

    assert description.title == "Inspect file"
    assert description.category == "read"
    assert description.operation == "read"
    assert description.target == "src/execraft/gui/server.py"
    assert description.summary == "src/execraft/gui/server.py"


def test_shell_command_is_classified_as_test_execution() -> None:
    description = describe_tool_activity(
        "Bash",
        {"command": "python -m pytest tests/test_agent_console.py -q", "cwd": "/repo"},
    )

    assert description.title == "Run tests"
    assert description.operation == "test"
    assert description.command == "python -m pytest tests/test_agent_console.py -q"
    assert description.target == "tests/test_agent_console.py"
    assert "cwd: /repo" in description.summary


def test_partial_json_recovers_useful_tool_fields() -> None:
    recovered = parse_partial_json_fields(
        '{"command":"pytest -q tests/test_live.py","description":"verify live console"'
    )

    assert recovered == {
        "command": "pytest -q tests/test_live.py",
        "description": "verify live console",
    }


def test_file_change_description_summarizes_distinct_paths() -> None:
    description = describe_file_change(
        {
            "changes": [
                {"path": "src/a.py"},
                {"path": "src/b.py"},
                {"path": "src/a.py"},
            ]
        }
    )

    assert description.title == "Apply file changes"
    assert description.summary == "2 files: src/a.py, src/b.py"


def test_normalizer_preserves_explicit_provider_fields() -> None:
    normalized = normalize_interaction_payload(
        {
            "kind": "tool",
            "title": "Provider title",
            "summary": "Provider summary",
            "target": "provider-target",
            "data": {"name": "Read", "input": {"file_path": "ignored.py"}},
        }
    )

    assert normalized["title"] == "Provider title"
    assert normalized["summary"] == "Provider summary"
    assert normalized["target"] == "provider-target"
    assert normalized["operation"] == "read"


def test_progress_tracker_warns_only_after_repeated_non_progressing_activity() -> None:
    tracker = LiveProgressTracker()

    for index in range(4):
        snapshot = tracker.observe(
            {
                "kind": "tool",
                "item_id": f"read-{index}",
                "title": "Inspect file",
                "summary": "src/a.py",
                "operation": "read",
                "target": "src/a.py",
                "status": "running",
                "at": f"2026-07-31T08:00:0{index}+00:00",
                "data": {"input": {"file_path": "src/a.py"}},
            }
        )

    assert snapshot["state"] == "possibly_stalled"
    assert snapshot["repeat_count"] == 4
    assert "Inspect file ×4" in snapshot["warning"]
    assert snapshot["files_touched"] == 1


def test_progress_tracker_recognizes_new_target_as_progress() -> None:
    tracker = LiveProgressTracker()
    for index in range(3):
        tracker.observe(
            {
                "kind": "tool",
                "item_id": f"read-{index}",
                "title": "Inspect file",
                "summary": "src/a.py",
                "operation": "read",
                "target": "src/a.py",
                "status": "running",
            }
        )

    snapshot = tracker.observe(
        {
            "kind": "tool",
            "item_id": "read-new",
            "title": "Inspect file",
            "summary": "src/b.py",
            "operation": "read",
            "target": "src/b.py",
            "status": "running",
        }
    )

    assert snapshot["state"] == "working"
    assert snapshot["warning"] == ""
    assert snapshot["distinct_targets"] == 2
