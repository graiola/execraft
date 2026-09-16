"""Tests for the Antigravity CLI headless text adapter."""

from __future__ import annotations

import subprocess

import pytest

from execraft.agents.antigravity_cli_adapter import AntigravityCliAgentAdapter
from execraft.orchestrate.scheduler import AgentCapability, Availability, StructuredHandoff


class _Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _Runner:
    def __init__(self, *, returncode=0, stdout="", stderr="", timeout=False):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.timeout = timeout
        self.calls = []

    def __call__(self, args, *, cwd, timeout, **_kwargs):
        self.calls.append({"args": list(args), "cwd": cwd, "timeout": timeout})
        if self.timeout:
            raise subprocess.TimeoutExpired(args, timeout)
        return _Completed(self.returncode, self.stdout, self.stderr)


def _handoff(stage="implement"):
    return StructuredHandoff(
        work_package_id="WP42",
        stage=stage,
        summary="Implement the weighted scheduler",
    )


def test_success_preserves_text_and_uses_supported_command_flags(tmp_path):
    runner = _Runner(stdout='{"ok":true,"status":"implemented","summary":"done"}')
    adapter = AntigravityCliAgentAdapter(
        workdir=tmp_path,
        runner=runner,
        model="gemini-3-pro",
        dangerously_skip_permissions=True,
        sandbox_enabled=True,
        capability_weight=91,
        capability_weights={AgentCapability.REVIEW: 94},
    )

    result = adapter.execute(_handoff())

    assert result["ok"] is True
    assert result["final_message"] == (
        '{"ok":true,"status":"implemented","summary":"done"}'
    )
    assert result["usage"] == {}
    assert result["transport"]["format"] == "text"
    args = runner.calls[0]["args"]
    assert args[0] == "agy"
    assert args[1] == "-p"
    assert "Work package: WP42" in args[2]
    assert "Implement the weighted scheduler" in args[2]
    assert args[args.index("--model") + 1] == "gemini-3-pro"
    assert args[args.index("--print-timeout") + 1] == "600s"
    assert "--cwd" not in args
    assert "-o" not in args
    assert "-m" not in args
    assert "--policy" not in args
    assert "--sandbox" in args
    assert "--sandbox=true" not in args
    assert "--dangerously-skip-permissions" in args
    assert adapter.weight_for_capability(AgentCapability.IMPLEMENT) == 91
    assert adapter.weight_for_capability(AgentCapability.REVIEW) == 94


def test_nonzero_stderr_is_classified(tmp_path):
    runner = _Runner(
        returncode=1,
        stderr="quota exceeded for this account",
    )
    adapter = AntigravityCliAgentAdapter(workdir=tmp_path, runner=runner)

    with pytest.raises(RuntimeError, match="quota exceeded") as exc_info:
        adapter.execute(_handoff())

    assert exc_info.value.classification == "quota_exhausted"
    assert adapter.availability == Availability.QUOTA_EXHAUSTED


def test_unknown_flag_usage_is_configuration_error_not_timeout(tmp_path):
    runner = _Runner(
        returncode=2,
        stderr=(
            "flags provided but not defined: -o\n"
            "Usage of agy:\n"
            "  --print-timeout duration\n"
        ),
    )
    adapter = AntigravityCliAgentAdapter(workdir=tmp_path, runner=runner)

    with pytest.raises(RuntimeError, match="flags provided") as exc_info:
        adapter.execute(_handoff())

    assert exc_info.value.classification == "configuration_error"
    assert exc_info.value.persistent is True
    assert adapter.availability == Availability.DISABLED


def test_plain_non_json_output_is_valid(tmp_path):
    adapter = AntigravityCliAgentAdapter(
        workdir=tmp_path,
        runner=_Runner(stdout="implementation complete\n"),
    )

    result = adapter.execute(_handoff())

    assert result["final_message"] == "implementation complete"


def test_empty_stdout_is_invalid_output(tmp_path):
    adapter = AntigravityCliAgentAdapter(
        workdir=tmp_path,
        runner=_Runner(stdout="", stderr=""),
    )

    with pytest.raises(RuntimeError, match="returned no stdout") as exc_info:
        adapter.execute(_handoff())

    assert exc_info.value.classification == "invalid_output"


def test_policy_paths_fail_closed_without_starting_process(tmp_path):
    runner = _Runner(stdout="unused")
    adapter = AntigravityCliAgentAdapter(
        workdir=tmp_path,
        runner=runner,
        policy_paths=("/tmp/base.policy",),
    )

    with pytest.raises(RuntimeError, match="does not support") as exc_info:
        adapter.execute(_handoff())

    assert exc_info.value.classification == "configuration_error"
    assert exc_info.value.persistent is True
    assert adapter.availability == Availability.DISABLED
    assert runner.calls == []


def test_timeout_marks_adapter_busy(tmp_path):
    adapter = AntigravityCliAgentAdapter(
        workdir=tmp_path,
        runner=_Runner(timeout=True),
        timeout_seconds=3,
    )

    with pytest.raises(RuntimeError, match="timed out"):
        adapter.execute(_handoff())

    assert adapter.availability == Availability.BUSY


def test_configured_timeout_is_forwarded_to_antigravity_print_mode(tmp_path):
    runner = _Runner(stdout="done")
    adapter = AntigravityCliAgentAdapter(
        workdir=tmp_path,
        runner=runner,
        timeout_seconds=3600,
    )

    adapter.execute(_handoff())

    args = runner.calls[0]["args"]
    assert args[args.index("--print-timeout") + 1] == "3600s"


def test_explicit_missing_binary_is_disabled(tmp_path):
    adapter = AntigravityCliAgentAdapter(
        workdir=tmp_path,
        runner=_Runner(stdout="ok"),
        binary="definitely-not-a-real-antigravity-binary",
    )

    assert adapter.availability == Availability.DISABLED



def test_read_only_handoff_still_honours_configured_headless_permissions(tmp_path):
    runner = _Runner(stdout='{"ok":true,"decision":"keep_atomic","reason":"bounded"}')
    adapter = AntigravityCliAgentAdapter(
        workdir=tmp_path,
        runner=runner,
        dangerously_skip_permissions=True,
    )
    handoff = StructuredHandoff(
        work_package_id="WP17",
        stage="decompose",
        summary="Inspect the repository and create a shard plan",
        working_directory=str(tmp_path),
        read_only=True,
    )

    adapter.execute(handoff)

    args = runner.calls[0]["args"]
    assert "--dangerously-skip-permissions" in args
    assert "--cwd" not in args


def test_headless_read_permission_error_is_classified(tmp_path):
    adapter = AntigravityCliAgentAdapter(
        workdir=tmp_path,
        runner=_Runner(
            stdout="",
            stderr=(
                'jetski: no output produced — a tool required the "read_file" '
                "permission that headless mode cannot prompt for, so it was auto-denied."
            ),
        ),
    )

    with pytest.raises(RuntimeError, match="headless mode cannot prompt") as exc_info:
        adapter.execute(_handoff(stage="decompose"))

    assert exc_info.value.classification == "permission_required"

def test_default_runner_uses_regular_file_capture(monkeypatch, tmp_path):
    import execraft.agents.antigravity_cli_adapter as module

    captured = {}

    def fake_managed_run(args, **kwargs):
        captured["args"] = list(args)
        captured.update(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")

    monkeypatch.setattr(module, "managed_run", fake_managed_run)

    result = module._default_runner(
        ["agy", "-p", "hello"],
        cwd=tmp_path,
        timeout=10,
        heartbeat_interval=1.0,
        heartbeat_callback=lambda _payload: None,
    )

    assert result.stdout == "ok"
    assert captured["capture_mode"] == "file"


def test_default_runner_retries_plain_mode_after_empty_success(monkeypatch, tmp_path):
    import execraft.agents.antigravity_cli_adapter as module

    calls = []

    def fake_managed_run(args, **kwargs):
        calls.append(dict(kwargs))
        if len(calls) == 1:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="recovered", stderr="")

    monkeypatch.setattr(module, "managed_run", fake_managed_run)

    result = module._default_runner(
        ["agy", "-p", "hello"],
        cwd=tmp_path,
        timeout=10,
        heartbeat_interval=1.0,
        heartbeat_callback=lambda _payload: None,
    )

    assert result.stdout == "recovered"
    assert calls[0]["capture_mode"] == "file"
    assert calls[1]["capture_mode"] == "pipe"
    assert "heartbeat_callback" not in calls[1]


def test_empty_stdout_surfaces_stderr_and_classification(tmp_path):
    adapter = AntigravityCliAgentAdapter(
        workdir=tmp_path,
        runner=_Runner(
            stdout="",
            stderr="authentication required; please sign in",
        ),
    )

    with pytest.raises(RuntimeError, match="authentication required") as exc_info:
        adapter.execute(_handoff())

    assert exc_info.value.classification in {"auth_failure", "authentication_required"}
    assert exc_info.value.artifact_payload["cwd"] == str(tmp_path.resolve())
    assert exc_info.value.artifact_payload["prompt_bytes"] > 0
