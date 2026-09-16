"""Reasoning-effort configuration and its per-provider CLI spelling."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys

import pytest

from execraft.agents.antigravity_cli_adapter import AntigravityCliAgentAdapter
from execraft.agents.claude_code_adapter import ClaudeCodeAgentAdapter
from execraft.agents.codex_adapter import _effort_config_args
from execraft.agents.config import AgentConfigError, parse_agent_configs
from execraft.agents.effort import EffortPolicy, clamp_effort, normalize_effort
from execraft.orchestrate.scheduler import AgentCapability, StructuredHandoff


def _handoff(tmp_path: Path, capability: str = "review") -> StructuredHandoff:
    return StructuredHandoff(
        work_package_id="WP1",
        stage="review",
        summary="Review the change",
        working_directory=str(tmp_path),
        execution_context={"capability": capability},
    )


def test_normalize_effort_rejects_a_typo_at_configuration_time() -> None:
    with pytest.raises(ValueError):
        normalize_effort("hihg")


def test_normalize_effort_treats_unset_as_provider_default() -> None:
    assert normalize_effort("") == ""
    assert normalize_effort(None) == ""


@pytest.mark.parametrize(
    ("requested", "supported", "expected"),
    [
        ("xhigh", ("low", "medium", "high"), "high"),
        ("max", ("low", "medium", "high"), "high"),
        ("medium", ("low", "medium", "high"), "medium"),
        ("xhigh", ("low", "medium", "high", "xhigh", "max"), "xhigh"),
        # A provider whose ladder starts above the request still runs.
        ("low", ("high", "max"), "high"),
        ("", ("low", "high"), ""),
    ],
)
def test_effort_clamps_onto_each_provider_ladder(
    requested: str, supported: tuple[str, ...], expected: str
) -> None:
    assert clamp_effort(requested, supported) == expected


def test_per_capability_effort_overrides_the_provider_default() -> None:
    policy = EffortPolicy(
        default="high",
        by_capability={"review": "low", "implement": "max"},
        supported=("low", "medium", "high", "xhigh", "max"),
    )

    assert policy.for_capability("review") == "low"
    assert policy.for_capability("implement") == "max"
    assert policy.for_capability("verify") == "high"


def test_handoff_without_a_capability_falls_back_to_the_provider_default() -> None:
    policy = EffortPolicy(default="medium", by_capability={"review": "low"})

    assert policy.for_handoff({}) == "medium"
    assert policy.for_handoff(None) == "medium"


def test_claude_adapter_passes_effort_and_cache_friendly_flags(
    monkeypatch, tmp_path: Path
) -> None:
    seen: list[list[str]] = []

    def fake_session(args, **kwargs):
        seen.append(list(args))
        controller = kwargs["message_callback"].__self__
        controller.handle_message({"type": "system", "subtype": "init", "session_id": "s"})
        controller.handle_message(
            {"type": "result", "is_error": False, "result": "Done", "session_id": "s"}
        )
        from execraft.agents.live_session import LiveProcessResult

        return LiveProcessResult(0, "", "", completed_by_protocol=True)

    monkeypatch.setattr(
        "execraft.agents.claude_code_adapter.managed_jsonl_session", fake_session
    )
    adapter = ClaudeCodeAgentAdapter(
        workdir=tmp_path,
        binary=sys.executable,
        effort="high",
        effort_by_capability={"review": "low"},
    )

    adapter.execute(_handoff(tmp_path, capability="review"))

    args = seen[0]
    assert args[args.index("--effort") + 1] == "low"
    # Moves cwd/env/git-status out of the system prompt so the cached system
    # prefix stays byte-stable across invocations against one workspace.
    assert "--exclude-dynamic-system-prompt-sections" in args


def test_codex_spells_effort_as_a_config_override() -> None:
    assert _effort_config_args("medium") == ["-c", 'model_reasoning_effort="medium"']
    assert _effort_config_args("") == []


def test_antigravity_clamps_the_two_strongest_levels_onto_high(tmp_path: Path) -> None:
    adapter = AntigravityCliAgentAdapter(
        workdir=tmp_path, binary=sys.executable, effort="max"
    )

    args = adapter._build_command(
        "prompt", effort=adapter._effort.for_handoff({"capability": "implement"})
    )

    assert args[args.index("--effort") + 1] == "high"


def test_provider_config_rejects_an_unknown_effort_level() -> None:
    with pytest.raises(AgentConfigError):
        parse_agent_configs(
            {
                "schema_version": 3,
                "providers": {
                    "claude": {
                        "adapter": "claude-code",
                        "enabled": True,
                        "binary": "claude",
                        "capabilities": ["review"],
                        "effort": "turbo",
                    }
                },
            }
        )


def test_provider_config_rejects_effort_for_an_undeclared_capability() -> None:
    with pytest.raises(AgentConfigError):
        parse_agent_configs(
            {
                "schema_version": 3,
                "providers": {
                    "claude": {
                        "adapter": "claude-code",
                        "enabled": True,
                        "binary": "claude",
                        "capabilities": ["review"],
                        "effort_by_capability": {"implement": "low"},
                    }
                },
            }
        )


def test_provider_config_round_trips_effort_by_capability() -> None:
    configs = parse_agent_configs(
        {
            "schema_version": 3,
            "providers": {
                "claude": {
                    "adapter": "claude-code",
                    "enabled": True,
                    "binary": "claude",
                    "capabilities": ["review", "implement"],
                    "effort": "high",
                    "effort_by_capability": {"review": "low"},
                }
            },
        }
    )

    provider = configs[0]
    assert provider.effort_for_capability(AgentCapability.REVIEW) == "low"
    assert provider.effort_for_capability(AgentCapability.IMPLEMENT) == "high"


def test_claude_adapter_resumes_a_prior_session_when_asked(
    monkeypatch, tmp_path: Path
) -> None:
    """A format-repair turn continues the provider's own cached conversation."""

    seen: list[list[str]] = []

    def fake_session(args, **kwargs):
        seen.append(list(args))
        controller = kwargs["message_callback"].__self__
        controller.handle_message({"type": "system", "subtype": "init", "session_id": "s2"})
        controller.handle_message(
            {"type": "result", "is_error": False, "result": "Done", "session_id": "s2"}
        )
        from execraft.agents.live_session import LiveProcessResult

        return LiveProcessResult(0, "", "", completed_by_protocol=True)

    monkeypatch.setattr(
        "execraft.agents.claude_code_adapter.managed_jsonl_session", fake_session
    )
    adapter = ClaudeCodeAgentAdapter(
        provider_id="claude-code", workdir=tmp_path, binary=sys.executable
    )
    handoff = replace(
        _handoff(tmp_path),
        execution_context={
            "capability": "review",
            "resume_session": {
                "provider_id": "claude-code",
                "session_id": "prior-session",
            },
        },
    )

    adapter.execute(handoff)

    args = seen[0]
    assert args[args.index("--resume") + 1] == "prior-session"
    # Forking keeps the failed attempt's transcript intact for the ledger.
    assert "--fork-session" in args


def test_claude_adapter_ignores_a_session_recorded_by_another_provider(
    monkeypatch, tmp_path: Path
) -> None:
    seen: list[list[str]] = []

    def fake_session(args, **kwargs):
        seen.append(list(args))
        controller = kwargs["message_callback"].__self__
        controller.handle_message({"type": "system", "subtype": "init", "session_id": "s"})
        controller.handle_message(
            {"type": "result", "is_error": False, "result": "Done", "session_id": "s"}
        )
        from execraft.agents.live_session import LiveProcessResult

        return LiveProcessResult(0, "", "", completed_by_protocol=True)

    monkeypatch.setattr(
        "execraft.agents.claude_code_adapter.managed_jsonl_session", fake_session
    )
    adapter = ClaudeCodeAgentAdapter(
        provider_id="claude-code", workdir=tmp_path, binary=sys.executable
    )
    handoff = replace(
        _handoff(tmp_path),
        execution_context={
            "capability": "review",
            "resume_session": {"provider_id": "codex", "session_id": "not-ours"},
        },
    )

    adapter.execute(handoff)

    assert "--resume" not in seen[0]


def test_claude_adapter_falls_back_to_a_cold_session_when_resume_fails(
    monkeypatch, tmp_path: Path
) -> None:
    """A stale session id must not turn a recoverable retry into a failure."""

    from execraft.agents.live_session import LiveProcessResult, LiveSessionUnavailable

    seen: list[list[str]] = []

    def fake_session(args, **kwargs):
        seen.append(list(args))
        if "--resume" in args:
            raise LiveSessionUnavailable("No conversation found with session ID")
        controller = kwargs["message_callback"].__self__
        controller.handle_message({"type": "system", "subtype": "init", "session_id": "s"})
        controller.handle_message(
            {"type": "result", "is_error": False, "result": "Done", "session_id": "s"}
        )
        return LiveProcessResult(0, "", "", completed_by_protocol=True)

    monkeypatch.setattr(
        "execraft.agents.claude_code_adapter.managed_jsonl_session", fake_session
    )
    adapter = ClaudeCodeAgentAdapter(
        provider_id="claude-code", workdir=tmp_path, binary=sys.executable
    )
    handoff = replace(
        _handoff(tmp_path),
        execution_context={
            "capability": "review",
            "resume_session": {
                "provider_id": "claude-code",
                "session_id": "stale",
            },
        },
    )

    result = adapter.execute(handoff)

    assert result["final_message"] == "Done"
    assert len(seen) == 2
    assert "--resume" in seen[0]
    assert "--resume" not in seen[1]
