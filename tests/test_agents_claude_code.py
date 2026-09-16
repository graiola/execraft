"""Tests for the real Claude Code CLI agent adapter.

Every test here uses an injected fake runner (or, for the stdin-closing
regression test, a trivial real subprocess that never touches `claude`) —
none of these spend on a real model call. The success/failure JSON
fixtures below are captured verbatim from two live, user-approved
`claude -p --output-format json` invocations rather than invented.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from execraft.agents.claude_code_adapter import ClaudeCodeAgentAdapter
from execraft.orchestrate.orchestrator import OrchestrationConfig, ProjectOrchestrator
from execraft.orchestrate.scheduler import (
    AgentCapability,
    AgentExecutionError,
    Availability,
    StructuredHandoff,
)

# Captured from: claude -p "Reply with exactly the single word: OK"
#   --output-format json --model haiku
_SUCCESS_STDOUT = (
    '{"type":"result","subtype":"success","is_error":false,"api_error_status":null,'
    '"duration_ms":1782,"duration_api_ms":1751,"num_turns":1,"result":"OK",'
    '"stop_reason":"end_turn","session_id":"3f5a8edc-dcff-4457-8b11-6284c1666903",'
    '"total_cost_usd":0.0160664,"usage":{"input_tokens":10,'
    '"cache_creation_input_tokens":7205,"cache_read_input_tokens":14364,'
    '"output_tokens":42,"service_tier":"standard"},"permission_denials":[],'
    '"terminal_reason":"completed","uuid":"f9803128-a1e2-415c-be0d-885a85929c09"}\n'
)

# Captured from the same session with --model totally-not-a-real-model-xyz.
_FAILURE_STDOUT = (
    '{"type":"result","subtype":"success","is_error":true,"api_error_status":404,'
    '"duration_ms":1294,"duration_api_ms":0,"num_turns":1,'
    '"result":"There\'s an issue with the selected model '
    "(totally-not-a-real-model-xyz). It may not exist or you may not have access to "
    'it.","stop_reason":"stop_sequence",'
    '"session_id":"7ff4eba1-1474-47c8-9aa9-a1526d439b19","total_cost_usd":0,'
    '"usage":{"input_tokens":0,"output_tokens":0},"permission_denials":[],'
    '"terminal_reason":"api_error","uuid":"3973949f-bb0d-46ac-8352-dd95c6abadee"}\n'
)


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _ScriptedClaudeRunner:
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



class _ProductionShapeClaudeRunner(_ScriptedClaudeRunner):
    def __call__(self, args: list[str], *, cwd, timeout, **kwargs):
        self.calls.append(
            {"args": list(args), "cwd": cwd, "timeout": timeout, **kwargs}
        )
        return _FakeCompletedProcess(self.returncode, self.stdout, self.stderr)

def _handoff(**overrides) -> StructuredHandoff:
    defaults = dict(work_package_id="pkg-1", stage="implement", summary="Do the thing")
    defaults.update(overrides)
    return StructuredHandoff(**defaults)


class TestClaudeCodeAgentAdapterExecute:
    def test_success_returns_final_message_usage_and_cost(self, tmp_path):
        runner = _ScriptedClaudeRunner(returncode=0, stdout=_SUCCESS_STDOUT)
        adapter = ClaudeCodeAgentAdapter(workdir=tmp_path, runner=runner)

        result = adapter.execute(_handoff())

        assert result["ok"] is True
        assert result["work_package_id"] == "pkg-1"
        assert result["final_message"] == "OK"
        assert result["usage"]["input_tokens"] == 10
        assert result["total_cost_usd"] == pytest.approx(0.0160664)
        assert result["session_id"] == "3f5a8edc-dcff-4457-8b11-6284c1666903"
        assert adapter.availability == Availability.AVAILABLE

    def test_command_includes_expected_flags_and_prompt(self, tmp_path):
        runner = _ScriptedClaudeRunner(returncode=0, stdout=_SUCCESS_STDOUT)
        adapter = ClaudeCodeAgentAdapter(
            workdir=tmp_path, runner=runner, model="haiku", permission_mode="acceptEdits"
        )

        control_root = str(tmp_path / "project-control")
        adapter.execute(
            _handoff(
                summary="Implement the widget",
                additional_writable_roots=[control_root],
            )
        )

        args = runner.calls[0]["args"]
        assert args[0] == "claude"
        assert args[1] == "-p"
        assert "Implement the widget" in args[2]
        assert "pkg-1" in args[2]
        assert args[args.index("--output-format") + 1] == "json"
        assert args[args.index("--model") + 1] == "haiku"
        assert args[args.index("--permission-mode") + 1] == "acceptEdits"
        assert args[args.index("--add-dir") + 1] == control_root
        assert runner.calls[0]["cwd"] == tmp_path

    def test_real_runner_sends_large_prompt_via_stdin_not_argv(self, tmp_path):
        runner = _ProductionShapeClaudeRunner(returncode=0, stdout=_SUCCESS_STDOUT)
        adapter = ClaudeCodeAgentAdapter(
            workdir=tmp_path,
            runner=runner,
            live_sessions=False,
        )
        adapter._runner_is_injected = False

        adapter.execute(_handoff(summary="x" * (3 * 1024 * 1024)))

        call = runner.calls[0]
        assert call["args"][:2] == ["claude", "-p"]
        assert max(len(item) for item in call["args"]) < 4096
        assert len(call["input_data"]) > 3 * 1024 * 1024
        assert "terminal_callback" not in call

    def test_failure_result_raises_with_result_text(self, tmp_path):
        # This CLI reports a request-level failure via is_error:true in a
        # normal (exit-1) result object, not a differently-shaped payload.
        runner = _ScriptedClaudeRunner(returncode=1, stdout=_FAILURE_STDOUT)
        adapter = ClaudeCodeAgentAdapter(workdir=tmp_path, runner=runner)

        with pytest.raises(RuntimeError, match="issue with the selected model"):
            adapter.execute(_handoff())

    def test_timeout_raises_and_marks_busy(self, tmp_path):
        runner = _ScriptedClaudeRunner(raise_timeout=True)
        adapter = ClaudeCodeAgentAdapter(workdir=tmp_path, runner=runner, timeout_seconds=5)

        with pytest.raises(RuntimeError, match="timed out"):
            adapter.execute(_handoff())
        assert adapter.availability == Availability.BUSY

    @pytest.mark.parametrize(
        "message,expected",
        [
            ("authentication failed, please run claude login", Availability.AUTH_FAILED),
            ("quota exceeded for this account", Availability.QUOTA_EXHAUSTED),
            ("rate limit exceeded, try again later", Availability.RATE_LIMITED),
        ],
    )
    def test_availability_reflects_classified_failure(self, tmp_path, message, expected):
        stdout = (
            '{"type":"result","is_error":true,"result":' + repr(message).replace("'", '"') + "}\n"
        )
        runner = _ScriptedClaudeRunner(returncode=1, stdout=stdout)
        adapter = ClaudeCodeAgentAdapter(workdir=tmp_path, runner=runner)

        with pytest.raises(RuntimeError):
            adapter.execute(_handoff())
        assert adapter.availability == expected

    def test_unclassified_failure_leaves_availability_unaffected(self, tmp_path):
        runner = _ScriptedClaudeRunner(returncode=1, stdout=_FAILURE_STDOUT)
        adapter = ClaudeCodeAgentAdapter(workdir=tmp_path, runner=runner)

        with pytest.raises(RuntimeError):
            adapter.execute(_handoff())
        assert adapter.availability == Availability.AVAILABLE

    def test_session_limit_preserves_reset_deadline(self, tmp_path):
        message = "You've hit your session limit · resets 11:59pm (Europe/Rome)"
        stdout = (
            '{"type":"result","is_error":true,"result":'
            + json.dumps(message)
            + "}\n"
        )
        adapter = ClaudeCodeAgentAdapter(
            workdir=tmp_path,
            runner=_ScriptedClaudeRunner(returncode=1, stdout=stdout),
        )

        with pytest.raises(AgentExecutionError) as captured:
            adapter.execute(_handoff())

        assert captured.value.classification == "session_limit"
        assert captured.value.persistent is True
        assert captured.value.retry_after_seconds is not None
        assert adapter.availability == Availability.SESSION_LIMIT

    def test_unparseable_stdout_still_fails_closed_on_nonzero_exit(self, tmp_path):
        runner = _ScriptedClaudeRunner(returncode=1, stdout="not json", stderr="boom")
        adapter = ClaudeCodeAgentAdapter(workdir=tmp_path, runner=runner)

        with pytest.raises(RuntimeError, match="boom"):
            adapter.execute(_handoff())

    def test_missing_binary_is_disabled_without_invoking_runner(self, tmp_path):
        runner = _ScriptedClaudeRunner(returncode=0, stdout=_SUCCESS_STDOUT)
        adapter = ClaudeCodeAgentAdapter(
            workdir=tmp_path, runner=runner, binary="definitely-not-a-real-binary-xyz"
        )
        assert adapter.availability == Availability.DISABLED

    def test_default_capabilities(self, tmp_path):
        adapter = ClaudeCodeAgentAdapter(workdir=tmp_path, runner=_ScriptedClaudeRunner())
        assert adapter.capabilities == {
            AgentCapability.IMPLEMENT,
            AgentCapability.REVIEW,
            AgentCapability.FIX_REVIEW,
        }



class TestClaudeCodeAgentAdapterThroughOrchestrator:
    def test_orchestrator_drives_claude_code_adapter_via_the_agent_adapter_protocol(
        self, tmp_path
    ):
        runner = _ScriptedClaudeRunner(returncode=0, stdout=_SUCCESS_STDOUT)
        claude_code = ClaudeCodeAgentAdapter(workdir=tmp_path, runner=runner)

        config = OrchestrationConfig(strict_checks=False, auto_commit=False, state_dir=tmp_path / "state")
        orch = ProjectOrchestrator("claude-code-e2e", config=config)
        orch.register_agent(claude_code)
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
