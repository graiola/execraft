"""Tests for the real OpenCode CLI agent adapter.

Every test here uses an injected fake runner (or, for the stdin-closing
regression test, a trivial real subprocess that never touches `opencode`)
— none of these spend on a real model call. The success/failure JSONL
fixtures below are captured verbatim from two live, user-approved
`opencode run --format json` invocations (against the free "zen" tier,
after an earlier rate limit had cleared) rather than invented.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from execraft.agents.opencode_adapter import OpenCodeAgentAdapter, _default_runner
from execraft.agents.opencode_events import OpenCodeEventBridge
from execraft.interaction import normalize_interaction_payload
from execraft.orchestrate.orchestrator import OrchestrationConfig, ProjectOrchestrator
from execraft.orchestrate.scheduler import (
    AgentCapability,
    AgentExecutionError,
    Availability,
    StructuredHandoff,
)

# Captured from: opencode run "Reply with exactly the single word: OK"
#   --format json --model opencode/deepseek-v4-flash-free --dir . --auto
_SUCCESS_STDOUT = (
    '{"type":"step_start","timestamp":1784626337989,"sessionID":"ses_1",'
    '"part":{"id":"prt_1","messageID":"msg_1","sessionID":"ses_1","type":"step-start"}}\n'
    '{"type":"text","timestamp":1784626338649,"sessionID":"ses_1",'
    '"part":{"id":"prt_2","messageID":"msg_1","sessionID":"ses_1","type":"text",'
    '"text":"OK","time":{"start":1784626338586,"end":1784626338618}}}\n'
    '{"type":"step_finish","timestamp":1784626338649,"sessionID":"ses_1",'
    '"part":{"id":"prt_3","reason":"stop","messageID":"msg_1","sessionID":"ses_1",'
    '"type":"step-finish","tokens":{"total":7777,"input":7761,"output":2,'
    '"reasoning":14,"cache":{"write":0,"read":0}},"cost":0}}\n'
)

# Captured from the same session with --model opencode/totally-not-a-real-model-xyz.
_FAILURE_STDOUT = (
    '{"type":"error","timestamp":1784626349216,"sessionID":"ses_2",'
    '"error":{"name":"UnknownError","data":{"message":"Unexpected server error. '
    'Check server logs for details.","ref":"err_231835b2"}}}\n'
)


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _ScriptedOpenCodeRunner:
    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        raise_timeout: bool = False,
    ):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.raise_timeout = raise_timeout
        self.calls: list[dict] = []

    def __call__(self, args: list[str], *, cwd, timeout):
        self.calls.append({"args": list(args), "cwd": cwd, "timeout": timeout})
        if self.raise_timeout:
            raise subprocess.TimeoutExpired(cmd=args, timeout=timeout)
        return _FakeCompletedProcess(self.returncode, self.stdout, self.stderr)


def _handoff(**overrides) -> StructuredHandoff:
    defaults = dict(work_package_id="pkg-1", stage="implement", summary="Do the thing")
    defaults.update(overrides)
    return StructuredHandoff(**defaults)


class TestOpenCodeAgentAdapterExecute:
    def test_success_returns_final_message_usage_and_cost(self, tmp_path):
        runner = _ScriptedOpenCodeRunner(returncode=0, stdout=_SUCCESS_STDOUT)
        adapter = OpenCodeAgentAdapter(workdir=tmp_path, runner=runner)

        result = adapter.execute(_handoff())

        assert result["ok"] is True
        assert result["work_package_id"] == "pkg-1"
        assert result["final_message"] == "OK"
        assert result["usage"]["total"] == 7777
        assert result["cost"] == 0.0
        assert adapter.availability == Availability.AVAILABLE


    def test_multi_step_session_returns_only_the_last_assistant_message(self, tmp_path):
        events = [
            {"type": "text", "part": {"messageID": "msg-1", "text": "Inspecting "}},
            {"type": "text", "part": {"messageID": "msg-1", "text": "files"}},
            {"type": "step_finish", "part": {"messageID": "msg-1"}},
            {"type": "text", "part": {"messageID": "msg-2", "text": '{"ok":true,'}},
            {
                "type": "text",
                "part": {"messageID": "msg-2", "text": '"summary":"done"}'},
            },
            {"type": "step_finish", "part": {"messageID": "msg-2"}},
        ]
        stdout = "".join(json.dumps(event) + "\n" for event in events)
        runner = _ScriptedOpenCodeRunner(returncode=0, stdout=stdout)
        adapter = OpenCodeAgentAdapter(workdir=tmp_path, runner=runner)

        result = adapter.execute(_handoff())

        assert result["final_message"] == '{"ok":true,"summary":"done"}'
        assert result["transport"]["assistant_message_count"] == 2
        assert result["transport"]["final_message_id"] == "msg-2"


    def test_final_message_uses_the_message_with_the_latest_text_event(self, tmp_path):
        events = [
            {"type": "text", "part": {"messageID": "msg-1", "text": "prefix-"}},
            {"type": "text", "part": {"messageID": "msg-2", "text": "intermediate"}},
            {"type": "text", "part": {"messageID": "msg-1", "text": "final"}},
            {"type": "step_finish", "part": {"messageID": "msg-1"}},
        ]
        stdout = "".join(json.dumps(event) + "\n" for event in events)
        runner = _ScriptedOpenCodeRunner(returncode=0, stdout=stdout)
        adapter = OpenCodeAgentAdapter(workdir=tmp_path, runner=runner)

        result = adapter.execute(_handoff())

        assert result["final_message"] == "prefix-final"
        assert result["transport"]["assistant_message_count"] == 2
        assert result["transport"]["final_message_id"] == "msg-1"

    def test_success_includes_bounded_transport_diagnostics(self, tmp_path):
        runner = _ScriptedOpenCodeRunner(returncode=0, stdout=_SUCCESS_STDOUT)
        adapter = OpenCodeAgentAdapter(workdir=tmp_path, runner=runner)

        result = adapter.execute(_handoff())

        transport = result["transport"]
        assert transport["event_count"] == 3
        assert transport["event_types"] == ["step_start", "text", "step_finish"]
        assert transport["text_event_count"] == 1
        assert transport["error_event_count"] == 0
        assert len(transport["stdout_sha256"]) == 64
        assert "stdout" not in transport

    def test_error_event_fails_even_when_cli_exit_code_is_zero(self, tmp_path):
        runner = _ScriptedOpenCodeRunner(returncode=0, stdout=_FAILURE_STDOUT)
        adapter = OpenCodeAgentAdapter(workdir=tmp_path, runner=runner)

        with pytest.raises(RuntimeError, match="Unexpected server error"):
            adapter.execute(_handoff())

    def test_command_uses_attachment_instead_of_full_prompt_argv(self, tmp_path):
        runner = _ScriptedOpenCodeRunner(returncode=0, stdout=_SUCCESS_STDOUT)
        adapter = OpenCodeAgentAdapter(
            workdir=tmp_path, runner=runner, model="opencode/deepseek-v4-flash-free"
        )

        adapter.execute(_handoff(summary="Implement the widget"))

        args = runner.calls[0]["args"]
        assert args[0] == "opencode"
        assert args[1] == "run"
        assert not any("Implement the widget" in item for item in args)
        assert not any("pkg-1" in item for item in args)
        prompt_path = Path(args[args.index("--file") + 1])
        assert not prompt_path.exists()
        assert args[args.index("--format") + 1] == "json"
        assert args[args.index("--model") + 1] == "opencode/deepseek-v4-flash-free"
        assert adapter.adapter_name == "opencode"
        assert adapter.model == "opencode/deepseek-v4-flash-free"
        assert adapter.binary == "opencode"
        assert args[args.index("--dir") + 1] == str(tmp_path)
        assert "--auto" in args

    def test_auto_approve_can_be_disabled(self, tmp_path):
        runner = _ScriptedOpenCodeRunner(returncode=0, stdout=_SUCCESS_STDOUT)
        adapter = OpenCodeAgentAdapter(workdir=tmp_path, runner=runner, auto_approve=False)

        adapter.execute(_handoff())

        assert "--auto" not in runner.calls[0]["args"]

    @pytest.mark.parametrize("stage", ["review", "final_review"])
    def test_review_stages_select_configured_opencode_agent(self, tmp_path, stage):
        runner = _ScriptedOpenCodeRunner(returncode=0, stdout=_SUCCESS_STDOUT)
        adapter = OpenCodeAgentAdapter(
            workdir=tmp_path,
            runner=runner,
            auto_approve=False,
            agent_by_capability={AgentCapability.REVIEW: "ai-reviewer"},
        )

        adapter.execute(_handoff(stage=stage))

        args = runner.calls[0]["args"]
        assert args[args.index("--agent") + 1] == "ai-reviewer"
        assert "--auto" not in args

    def test_implementation_does_not_reuse_review_agent(self, tmp_path):
        runner = _ScriptedOpenCodeRunner(returncode=0, stdout=_SUCCESS_STDOUT)
        adapter = OpenCodeAgentAdapter(
            workdir=tmp_path,
            runner=runner,
            agent_by_capability={AgentCapability.REVIEW: "ai-reviewer"},
        )

        adapter.execute(_handoff(stage="implement"))

        assert "--agent" not in runner.calls[0]["args"]

    def test_failure_extracts_nested_error_data_message_and_raises(self, tmp_path):
        runner = _ScriptedOpenCodeRunner(returncode=1, stdout=_FAILURE_STDOUT)
        adapter = OpenCodeAgentAdapter(workdir=tmp_path, runner=runner)

        with pytest.raises(RuntimeError, match="Unexpected server error"):
            adapter.execute(_handoff())

    def test_timeout_raises_and_marks_busy(self, tmp_path):
        runner = _ScriptedOpenCodeRunner(raise_timeout=True)
        adapter = OpenCodeAgentAdapter(workdir=tmp_path, runner=runner, timeout_seconds=5)

        with pytest.raises(RuntimeError, match="timed out"):
            adapter.execute(_handoff())
        assert adapter.availability == Availability.BUSY

    @pytest.mark.parametrize(
        "message,expected",
        [
            ("authentication failed, please log in", Availability.AUTH_FAILED),
            ("quota exceeded for this account", Availability.QUOTA_EXHAUSTED),
            ("rate limit exceeded, try again later", Availability.RATE_LIMITED),
        ],
    )
    def test_availability_reflects_classified_failure(self, tmp_path, message, expected):
        stdout = (
            '{"type":"error","error":{"name":"APIError","data":{"message":'
            + repr(message).replace("'", '"')
            + "}}}\n"
        )
        runner = _ScriptedOpenCodeRunner(returncode=1, stdout=stdout)
        adapter = OpenCodeAgentAdapter(workdir=tmp_path, runner=runner)

        with pytest.raises(RuntimeError):
            adapter.execute(_handoff())
        assert adapter.availability == expected

    def test_unclassified_failure_leaves_availability_unaffected(self, tmp_path):
        runner = _ScriptedOpenCodeRunner(returncode=1, stdout=_FAILURE_STDOUT)
        adapter = OpenCodeAgentAdapter(workdir=tmp_path, runner=runner)

        with pytest.raises(RuntimeError):
            adapter.execute(_handoff())
        assert adapter.availability == Availability.AVAILABLE

    def test_missing_binary_is_disabled_without_invoking_runner(self, tmp_path):
        runner = _ScriptedOpenCodeRunner(returncode=0, stdout=_SUCCESS_STDOUT)
        adapter = OpenCodeAgentAdapter(
            workdir=tmp_path, runner=runner, binary="definitely-not-a-real-binary-xyz"
        )
        assert adapter.availability == Availability.DISABLED

    def test_default_capabilities(self, tmp_path):
        adapter = OpenCodeAgentAdapter(workdir=tmp_path, runner=_ScriptedOpenCodeRunner())
        assert adapter.capabilities == {
            AgentCapability.IMPLEMENT,
            AgentCapability.REVIEW,
            AgentCapability.FIX_REVIEW,
        }


class TestDefaultRunnerClosesStdin:
    def test_default_runner_does_not_hang_on_inherited_stdin(self, tmp_path):
        completed = _default_runner(["cat"], cwd=tmp_path, timeout=5)
        assert completed.returncode == 0
        assert completed.stdout == ""


class TestOpenCodeAgentAdapterThroughOrchestrator:
    def test_orchestrator_drives_opencode_adapter_via_the_agent_adapter_protocol(
        self, tmp_path
    ):
        runner = _ScriptedOpenCodeRunner(returncode=0, stdout=_SUCCESS_STDOUT)
        opencode = OpenCodeAgentAdapter(workdir=tmp_path, runner=runner)
        reviewer = OpenCodeAgentAdapter(
            provider_id="opencode-review",
            capabilities={AgentCapability.REVIEW},
            workdir=tmp_path,
            runner=runner,
        )

        config = OrchestrationConfig(strict_checks=False, auto_commit=False, state_dir=tmp_path / "state")
        orch = ProjectOrchestrator("opencode-e2e", config=config)
        orch.register_agent(opencode)
        orch.register_agent(reviewer)
        orch.initialize("## Package: Only\n\n### Acceptance criteria\n- [ ] Works\n")
        orch.run_pipeline()

        assert orch.status_report()["state"] == "completed"
        assert orch.status_report()["completed_packages"] == 1
        expected_stages = ["implement", "review", "final_review"]
        invocation_stages = [
            event.payload["stage"]
            for event in orch._journal.read()
            if event.event_type == "agent_invocation_started"
        ]
        assert len(runner.calls) == len(expected_stages)
        assert invocation_stages == expected_stages


def test_supervisor_fix_review_stage_selects_fix_review_native_agent(tmp_path):
    runner = _ScriptedOpenCodeRunner(returncode=0, stdout=_SUCCESS_STDOUT)
    adapter = OpenCodeAgentAdapter(
        workdir=tmp_path,
        runner=runner,
        agent_by_capability={AgentCapability.FIX_REVIEW: "ai-fixer"},
    )

    adapter.execute(_handoff(stage="supervisor_delegate_fix_review"))

    args = runner.calls[0]["args"]
    assert args[args.index("--agent") + 1] == "ai-fixer"


def test_adapter_accepts_only_the_final_assistant_message(tmp_path):
    events = [
        {
            "type": "text",
            "part": {
                "messageID": "msg-contract",
                "text": '{"ok":true,"status":"fixed","summary":"done"}',
            },
        },
        {
            "type": "text",
            "part": {"messageID": "msg-epilogue", "text": "Finished."},
        },
        {"type": "step_finish", "part": {"messageID": "msg-epilogue"}},
    ]
    runner = _ScriptedOpenCodeRunner(
        returncode=0,
        stdout="".join(json.dumps(event) + "\n" for event in events),
    )
    adapter = OpenCodeAgentAdapter(workdir=tmp_path, runner=runner)

    result = adapter.execute(_handoff())

    assert result["final_message"] == "Finished."
    assert "structured_output_candidates" not in result


def test_adapter_rejects_malformed_jsonl(tmp_path):
    runner = _ScriptedOpenCodeRunner(
        returncode=0,
        stdout='{"type":"step_start"}\nnot-json\n',
    )
    adapter = OpenCodeAgentAdapter(workdir=tmp_path, runner=runner)

    with pytest.raises(AgentExecutionError) as exc_info:
        adapter.execute(_handoff())

    assert exc_info.value.classification == "transport_protocol_error"


def test_adapter_requires_terminal_step_and_final_message(tmp_path):
    runner = _ScriptedOpenCodeRunner(
        returncode=0,
        stdout='{"type":"step_start"}\n',
    )
    adapter = OpenCodeAgentAdapter(workdir=tmp_path, runner=runner)

    with pytest.raises(AgentExecutionError) as exc_info:
        adapter.execute(_handoff())

    assert exc_info.value.classification == "transport_protocol_error"


def test_adapter_rejects_stale_progress_text_when_terminal_step_has_no_message(
    tmp_path,
):
    events = [
        {"type": "step_start", "part": {"messageID": "msg-progress"}},
        {
            "type": "text",
            "part": {"messageID": "msg-progress", "text": "Let me inspect files."},
        },
        {"type": "step_finish", "part": {"messageID": "msg-progress"}},
        {"type": "step_start", "part": {"messageID": "msg-empty"}},
        {
            "type": "step_finish",
            "part": {
                "messageID": "msg-empty",
                "tokens": {"input": 0, "output": 0, "reasoning": 0},
            },
        },
    ]
    runner = _ScriptedOpenCodeRunner(
        returncode=0,
        stdout="".join(json.dumps(event) + "\n" for event in events),
    )
    adapter = OpenCodeAgentAdapter(workdir=tmp_path, runner=runner)

    with pytest.raises(AgentExecutionError) as exc_info:
        adapter.execute(_handoff())

    assert exc_info.value.classification == "transport_protocol_error"
    assert "terminal step" in str(exc_info.value)
    assert exc_info.value.artifact_payload["transport"]["final_message_id"] == ""


def test_format_repair_uses_dedicated_tool_free_agent(tmp_path):
    runner = _ScriptedOpenCodeRunner(returncode=0, stdout=_SUCCESS_STDOUT)
    adapter = OpenCodeAgentAdapter(
        workdir=tmp_path,
        runner=runner,
        agent_by_capability={AgentCapability.FIX_REVIEW: "ai-fixer"},
        format_repair_agent="ai-contract",
    )

    adapter.execute(
        _handoff(
            stage="fix_review",
            execution_context={"format_repair": True},
        )
    )

    args = runner.calls[0]["args"]
    assert args[args.index("--agent") + 1] == "ai-contract"


def test_opencode_event_bridge_decodes_chunked_jsonl_without_steering():
    events = []
    bridge = OpenCodeEventBridge(events.append)

    split = len(_SUCCESS_STDOUT) // 2
    bridge.feed("stdout", _SUCCESS_STDOUT[:split])
    bridge.feed("stderr", "diagnostic text must not become a semantic event")
    bridge.feed("stdout", _SUCCESS_STDOUT[split:])
    bridge.finish()

    assert [event["kind"] for event in events] == [
        "status",
        "assistant_delta",
        "usage",
    ]
    assert events[1]["text"] == "OK"
    assert events[2]["data"]["tokens"]["total"] == 7777
    assert all(event["steering_supported"] is False for event in events)


def test_production_opencode_run_never_receives_a_terminal_callback(monkeypatch, tmp_path):
    import execraft.agents.opencode_adapter as module

    observed = {}

    def fake_default_runner(args, **kwargs):
        observed.update(kwargs)
        callback = kwargs.get("output_callback")
        if callback is not None:
            callback("stdout", _SUCCESS_STDOUT)
            callback("stderr", "runtime diagnostic\n")
        return _FakeCompletedProcess(0, _SUCCESS_STDOUT, "runtime diagnostic\n")

    monkeypatch.setattr(module, "_default_runner", fake_default_runner)
    config_path = tmp_path / "opencode.json"
    config_path.write_text("{}\n", encoding="utf-8")
    adapter = module.OpenCodeAgentAdapter(
        workdir=tmp_path,
        first_output_timeout_seconds=180,
        config_path=config_path,
    )
    semantic_events = []
    raw_events = []
    adapter.configure_interaction(semantic_events.append)
    adapter.configure_output(raw_events.append)

    result = adapter.execute(_handoff())

    assert result["final_message"] == "OK"
    assert "terminal_callback" not in observed
    assert observed["first_output_timeout"] == 180
    assert observed["environment"]["OPENCODE_CONFIG"] == str(config_path)
    assert adapter.execution_capabilities.interactive_pty is False
    assert adapter.execution_capabilities.provider_native_steering is False
    assert adapter.execution_capabilities.semantic_streaming is True
    assert (
        adapter.execution_capabilities.structured_output_enforcement
        == "prompt_only"
    )
    assert [event["stream"] for event in raw_events] == ["stderr"]
    assert [event["kind"] for event in semantic_events] == [
        "status",
        "assistant_delta",
        "usage",
    ]


def test_opencode_event_bridge_bounds_records_not_aggregate_chunks(monkeypatch):
    import execraft.agents.opencode_events as module

    monkeypatch.setattr(module, "_MAX_LINE_BYTES", 90)
    events = []
    bridge = module.OpenCodeEventBridge(events.append)
    line = '{"type":"text","part":{"text":"ok","id":"item"}}'

    assert len(line.encode("utf-8")) < module._MAX_LINE_BYTES
    assert len((line + "\n" + line).encode("utf-8")) > module._MAX_LINE_BYTES
    bridge.feed("stdout", line + "\n" + line + "\n")

    assert [event["kind"] for event in events] == [
        "assistant_delta",
        "assistant_delta",
    ]


def test_opencode_completed_tool_reports_real_target_and_completion() -> None:
    events = []
    bridge = OpenCodeEventBridge(events.append)
    bridge.feed(
        "stdout",
        json.dumps(
            {
                "type": "tool_use",
                "sessionID": "ses-local",
                "part": {
                    "tool": "read",
                    "messageID": "msg-local",
                    "state": {
                        "status": "completed",
                        "input": {"filePath": "/workspace/PLAN.md"},
                        "output": "plan",
                    },
                },
            }
        )
        + "\n",
    )

    assert len(events) == 1
    normalized = normalize_interaction_payload(events[0])
    assert normalized["status"] == "completed"
    assert normalized["title"] == "Inspect file"
    assert normalized["target"] == "/workspace/PLAN.md"


def test_opencode_event_bridge_reports_oversized_partial_record(monkeypatch):
    import execraft.agents.opencode_events as module

    monkeypatch.setattr(module, "_MAX_LINE_BYTES", 16)
    events = []
    bridge = module.OpenCodeEventBridge(events.append)

    bridge.feed("stdout", "x" * 17)

    assert events == [
        {
            "kind": "error",
            "provider": "opencode",
            "interaction_mode": "conversation",
            "streaming": True,
            "steering_supported": False,
            "transport": "opencode-run-jsonl",
            "control_mode": "observation",
            "text": "OpenCode emitted an oversized JSONL record",
            "status": "failed",
        }
    ]


def _model_config_adapter(tmp_path, config: dict | None):
    import execraft.agents.opencode_adapter as module

    config_path = tmp_path / "opencode.json"
    if config is not None:
        config_path.write_text(json.dumps(config), encoding="utf-8")
    return module.OpenCodeAgentAdapter(
        workdir=tmp_path,
        model="ollama-local/qwen3-coder:30b-32k",
        config_path=config_path,
    )


def test_declares_configured_model_reads_the_workspace_config(tmp_path):
    adapter = _model_config_adapter(
        tmp_path,
        {
            "provider": {
                "ollama-local": {
                    "options": {"baseURL": "http://127.0.0.1:11434/v1"},
                    "models": {"qwen3-coder:30b-32k": {"name": "Qwen3 Coder"}},
                }
            }
        },
    )

    assert adapter.declares_configured_model() is True


def test_declares_configured_model_is_false_for_a_stale_config(tmp_path):
    adapter = _model_config_adapter(
        tmp_path,
        {"provider": {"ollama-gpu-a": {"models": {"qwen3-coder:30b-32k": {}}}}},
    )

    assert adapter.declares_configured_model() is False


def test_declares_configured_model_is_false_without_a_config_file(tmp_path):
    adapter = _model_config_adapter(tmp_path, None)

    assert adapter.declares_configured_model() is False
