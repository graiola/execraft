"""Tests for the real Codex CLI agent adapter.

Every test here uses an injected fake runner (or, for the stdin-closing
regression test, a trivial real subprocess that never touches `codex`) —
none of these spend on a real model call. The success/failure JSONL
fixtures below are captured verbatim from one live, user-approved
`codex exec` smoke run (codex-cli 0.144.6) rather than invented, so the
parsing logic is checked against the real event shape.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from execraft.agents.codex_adapter import CodexAgentAdapter
from execraft.orchestrate.orchestrator import OrchestrationConfig, ProjectOrchestrator
from execraft.orchestrate.scheduler import (
    AgentCapability,
    AgentExecutionError,
    Availability,
    StructuredHandoff,
)

# Captured from: codex exec --json --skip-git-repo-check -s read-only -C .
#   -o last-message.txt "Reply with exactly the single word: OK"
_SUCCESS_STDOUT = (
    '{"type":"thread.started","thread_id":"019f802c-a493-7bf2-899a-f7d270afaac1"}\n'
    '{"type":"turn.started"}\n'
    '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"OK"}}\n'
    '{"type":"turn.completed","usage":{"input_tokens":12528,"cached_input_tokens":9984,'
    '"output_tokens":5,"reasoning_output_tokens":0}}\n'
)

# Captured from the same session with -m totally-not-a-real-model-xyz.
_FAILURE_STDOUT = (
    '{"type":"thread.started","thread_id":"019f802c-ff9e-7d83-bbea-11d0c7e4f5d4"}\n'
    '{"type":"item.completed","item":{"id":"item_0","type":"error","message":'
    '"Model metadata for `totally-not-a-real-model-xyz` not found. Defaulting to '
    'fallback metadata; this can degrade performance and cause issues."}}\n'
    '{"type":"turn.started"}\n'
    '{"type":"error","message":"{\\"type\\":\\"error\\",\\"status\\":400,\\"error\\":'
    '{\\"type\\":\\"invalid_request_error\\",\\"message\\":\\"The '
    "'totally-not-a-real-model-xyz' model is not supported when using Codex with a "
    'ChatGPT account.\\"}}"}\n'
    '{"type":"turn.failed","error":{"message":"{\\"type\\":\\"error\\",\\"status\\":400,'
    '\\"error\\":{\\"type\\":\\"invalid_request_error\\",\\"message\\":\\"The '
    "'totally-not-a-real-model-xyz' model is not supported when using Codex with a "
    'ChatGPT account.\\"}}"}}\n'
)


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _ScriptedCodexRunner:
    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        output_text: str | None = None,
        raise_timeout: bool = False,
    ):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.output_text = output_text
        self.raise_timeout = raise_timeout
        self.calls: list[dict] = []

    def __call__(self, args: list[str], *, cwd, timeout):
        self.calls.append({"args": list(args), "cwd": cwd, "timeout": timeout})
        if self.raise_timeout:
            raise subprocess.TimeoutExpired(cmd=args, timeout=timeout)
        if self.output_text is not None:
            output_path = Path(args[args.index("-o") + 1])
            output_path.write_text(self.output_text, encoding="utf-8")
        return _FakeCompletedProcess(self.returncode, self.stdout, self.stderr)



class _ProductionShapeCodexRunner(_ScriptedCodexRunner):
    def __call__(self, args: list[str], *, cwd, timeout, **kwargs):
        self.calls.append(
            {"args": list(args), "cwd": cwd, "timeout": timeout, **kwargs}
        )
        if self.output_text is not None:
            output_path = Path(args[args.index("-o") + 1])
            output_path.write_text(self.output_text, encoding="utf-8")
        return _FakeCompletedProcess(self.returncode, self.stdout, self.stderr)

def _handoff(**overrides) -> StructuredHandoff:
    defaults = dict(work_package_id="pkg-1", stage="implement", summary="Do the thing")
    defaults.update(overrides)
    return StructuredHandoff(**defaults)


class TestCodexAgentAdapterExecute:
    def test_success_returns_final_message_and_usage(self, tmp_path):
        runner = _ScriptedCodexRunner(returncode=0, stdout=_SUCCESS_STDOUT, output_text="OK")
        adapter = CodexAgentAdapter(workdir=tmp_path, runner=runner)

        result = adapter.execute(_handoff())

        assert result["ok"] is True
        assert result["work_package_id"] == "pkg-1"
        assert result["final_message"] == "OK"
        assert result["usage"] == {
            "input_tokens": 12528,
            "cached_input_tokens": 9984,
            "output_tokens": 5,
            "reasoning_output_tokens": 0,
        }
        assert adapter.availability == Availability.AVAILABLE

    def test_command_includes_expected_flags_and_prompt(self, tmp_path):
        runner = _ScriptedCodexRunner(returncode=0, stdout=_SUCCESS_STDOUT, output_text="OK")
        adapter = CodexAgentAdapter(
            workdir=tmp_path, runner=runner, sandbox="read-only", model="gpt-5-mini"
        )

        control_root = str(tmp_path / "project-control")
        adapter.execute(
            _handoff(
                summary="Implement the widget",
                additional_writable_roots=[control_root],
            )
        )

        args = runner.calls[0]["args"]
        assert args[0] == "codex"
        assert args[1] == "exec"
        assert "--json" in args
        assert "--skip-git-repo-check" in args
        assert args[args.index("--add-dir") + 1] == control_root
        assert args[args.index("-s") + 1] == "read-only"
        assert args[args.index("-C") + 1] == str(tmp_path)
        assert args[args.index("-m") + 1] == "gpt-5-mini"
        assert "-o" in args
        assert "Implement the widget" in args[-1]
        assert "pkg-1" in args[-1]

    def test_real_runner_sends_large_prompt_via_stdin_not_argv(self, tmp_path):
        runner = _ProductionShapeCodexRunner(
            returncode=0, stdout=_SUCCESS_STDOUT, output_text="OK"
        )
        adapter = CodexAgentAdapter(
            workdir=tmp_path,
            runner=runner,
            live_sessions=False,
        )
        adapter._runner_is_injected = False

        adapter.execute(_handoff(summary="x" * (3 * 1024 * 1024)))

        call = runner.calls[0]
        assert call["args"][-1] == "-"
        assert max(len(item) for item in call["args"]) < 4096
        assert len(call["input_data"]) > 3 * 1024 * 1024
        assert "terminal_callback" not in call

    def test_failure_unwraps_nested_error_and_raises(self, tmp_path):
        runner = _ScriptedCodexRunner(returncode=1, stdout=_FAILURE_STDOUT)
        adapter = CodexAgentAdapter(workdir=tmp_path, runner=runner)

        with pytest.raises(RuntimeError, match="not supported when using Codex"):
            adapter.execute(_handoff())

    def test_timeout_raises_and_marks_busy(self, tmp_path):
        runner = _ScriptedCodexRunner(raise_timeout=True)
        adapter = CodexAgentAdapter(workdir=tmp_path, runner=runner, timeout_seconds=5)

        with pytest.raises(RuntimeError, match="timed out"):
            adapter.execute(_handoff())
        assert adapter.availability == Availability.BUSY

    @pytest.mark.parametrize(
        "message,expected",
        [
            ("authentication failed, please run codex login", Availability.AUTH_FAILED),
            ("quota exceeded for this account", Availability.QUOTA_EXHAUSTED),
            ("rate limit exceeded, try again later", Availability.RATE_LIMITED),
        ],
    )
    def test_availability_reflects_classified_failure(self, tmp_path, message, expected):
        stdout = (
            '{"type":"turn.failed","error":{"message":' + repr(message).replace("'", '"') + "}}\n"
        )
        runner = _ScriptedCodexRunner(returncode=1, stdout=stdout)
        adapter = CodexAgentAdapter(workdir=tmp_path, runner=runner)

        with pytest.raises(RuntimeError):
            adapter.execute(_handoff())
        assert adapter.availability == expected

    def test_unclassified_failure_leaves_availability_unaffected(self, tmp_path):
        runner = _ScriptedCodexRunner(returncode=1, stdout=_FAILURE_STDOUT)
        adapter = CodexAgentAdapter(workdir=tmp_path, runner=runner)

        with pytest.raises(RuntimeError):
            adapter.execute(_handoff())
        # A one-off bad-model/config error doesn't mean the provider itself
        # is unusable; the orchestrator's own per-call failover handles it.
        assert adapter.availability == Availability.AVAILABLE

    def test_usage_limit_preserves_absolute_retry_deadline(self, tmp_path):
        message = (
            "You've hit your usage limit. Upgrade to Pro or try again at "
            "Dec 31st, 2099 8:00 PM."
        )
        stdout = (
            '{"type":"turn.failed","error":{"message":'
            + json.dumps(message)
            + "}}\n"
        )
        adapter = CodexAgentAdapter(
            workdir=tmp_path,
            runner=_ScriptedCodexRunner(returncode=1, stdout=stdout),
        )

        with pytest.raises(AgentExecutionError) as captured:
            adapter.execute(_handoff())

        assert captured.value.classification == "quota_exhausted"
        assert captured.value.persistent is True
        assert captured.value.retry_after_seconds is not None
        assert adapter.availability == Availability.QUOTA_EXHAUSTED

    def test_missing_binary_is_disabled_without_invoking_runner(self, tmp_path):
        runner = _ScriptedCodexRunner(returncode=0, stdout=_SUCCESS_STDOUT, output_text="OK")
        adapter = CodexAgentAdapter(
            workdir=tmp_path, runner=runner, binary="definitely-not-a-real-binary-xyz"
        )
        assert adapter.availability == Availability.DISABLED

    def test_default_capabilities(self, tmp_path):
        adapter = CodexAgentAdapter(workdir=tmp_path, runner=_ScriptedCodexRunner())
        assert adapter.capabilities == {
            AgentCapability.IMPLEMENT,
            AgentCapability.REVIEW,
            AgentCapability.FIX_REVIEW,
        }



class TestCodexAgentAdapterThroughOrchestrator:
    def test_orchestrator_drives_codex_adapter_via_the_agent_adapter_protocol(
        self, tmp_path
    ):
        runner = _ScriptedCodexRunner(returncode=0, stdout=_SUCCESS_STDOUT, output_text="OK")
        codex = CodexAgentAdapter(workdir=tmp_path, runner=runner)

        config = OrchestrationConfig(strict_checks=False, auto_commit=False, state_dir=tmp_path / "state")
        orch = ProjectOrchestrator("codex-e2e", config=config)
        orch.register_agent(codex)
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
