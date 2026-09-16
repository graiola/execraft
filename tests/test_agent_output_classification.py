from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from execraft.agents.output_classification import (
    AgentOutputClassifier,
    classify_provider_message,
    parse_duration_seconds,
    parse_retry_after_seconds,
)


def test_parses_compound_reset_duration():
    assert parse_duration_seconds("3 days 21 hours") == 334800
    assert parse_retry_after_seconds("It will reset in 3 days 21 hours") == 334800


def test_parses_absolute_codex_quota_reset_in_local_timezone():
    rome = ZoneInfo("Europe/Rome")
    now = datetime(2026, 7, 22, 23, 28, tzinfo=rome)

    seconds = parse_retry_after_seconds(
        "You've hit your usage limit. try again at Jul 28th, 2026 8:00 PM.",
        now=now,
    )

    assert seconds == 505920


def test_parses_next_claude_session_reset_with_explicit_timezone():
    now = datetime(2026, 7, 22, 23, 28, tzinfo=ZoneInfo("Europe/Rome"))

    seconds = parse_retry_after_seconds(
        "You've hit your session limit · resets 1:40am (Europe/Rome)",
        now=now,
    )

    assert seconds == 7920


def test_claude_weekly_limit_with_hour_only_reset_is_quota_exhausted():
    now = datetime(2026, 8, 26, 13, 25, tzinfo=ZoneInfo("Europe/Rome"))

    signal = classify_provider_message(
        "claude-code",
        "You've hit your weekly limit · resets 8pm (Europe/Rome)",
        now=now,
    )

    assert signal is not None
    assert signal.category == "quota_exhausted"
    assert signal.retry_after_seconds == 23700


def test_time_only_reset_rolls_to_next_day_after_clock_passed():
    now = datetime(2026, 7, 23, 2, 0, tzinfo=ZoneInfo("Europe/Rome"))

    seconds = parse_retry_after_seconds(
        "session limit resets 1:40am (Europe/Rome)",
        now=now,
    )

    assert seconds == 85200


def test_provider_messages_distinguish_quota_session_and_network_failures():
    now = datetime(2026, 7, 22, 23, 28, tzinfo=ZoneInfo("Europe/Rome"))

    quota = classify_provider_message(
        "codex",
        "You've hit your usage limit. try again at Jul 28th, 2026 8:00 PM.",
        now=now,
    )
    session = classify_provider_message(
        "claude-code",
        "You've hit your session limit · resets 1:40am (Europe/Rome)",
        now=now,
    )
    network = classify_provider_message(
        "codex",
        "stream disconnected before completion: failed to lookup address information",
        now=now,
    )

    assert quota is not None and quota.category == "quota_exhausted"
    assert quota.retry_after_seconds == 505920
    assert session is not None and session.category == "session_limit"
    assert session.retry_after_seconds == 7920
    assert network is not None and network.category == "network_transient"
    assert network.retry_after_seconds is None


def test_opencode_monthly_limit_is_terminal_quota_signal():
    classifier = AgentOutputClassifier(
        adapter="opencode",
        provider_id="opencode-go",
        max_internal_retry_delay_seconds=120,
    )

    signal = classifier(
        "stderr",
        "monthly usage limit reached. It will reset in 3 days 21 hours. "
        "retrying in ~3 days attempt #1",
    )

    assert signal is not None
    assert signal.category == "quota_exhausted"
    assert signal.retry_after_seconds == 334800
    assert signal.persistent is True
    assert "monthly usage limit" in signal.excerpt


def test_long_internal_retry_is_terminated_even_without_quota_phrase():
    classifier = AgentOutputClassifier(
        adapter="codex",
        provider_id="codex",
        max_internal_retry_delay_seconds=120,
    )

    signal = classifier("stderr", "temporary provider issue; retrying in 10 minutes")

    assert signal is not None
    assert signal.category == "internal_retry_detected"
    assert signal.retry_after_seconds == 600
    assert signal.persistent is False


def test_short_internal_retry_is_allowed():
    classifier = AgentOutputClassifier(
        adapter="claude-code",
        provider_id="claude-code",
        max_internal_retry_delay_seconds=120,
    )
    assert classifier("stderr", "retrying in 30 seconds") is None


def test_classifier_matches_across_chunks():
    classifier = AgentOutputClassifier(adapter="opencode", provider_id="go")
    assert classifier("stderr", "monthly usage ") is None
    signal = classifier("stderr", "limit reached; reset in 2 days")
    assert signal is not None
    assert signal.category == "quota_exhausted"
    assert signal.retry_after_seconds == 172800


def test_codex_auth_prompt_is_terminal():
    classifier = AgentOutputClassifier(adapter="codex", provider_id="codex")
    signal = classifier("stderr", "Authentication required. Please log in.")
    assert signal is not None
    assert signal.category in {"authentication_required", "auth_failure"}


def test_claude_rate_limit_is_terminal():
    classifier = AgentOutputClassifier(
        adapter="claude-code", provider_id="claude-code"
    )
    signal = classifier("stderr", "Rate limit exceeded; retry after 5 minutes")
    assert signal is not None
    assert signal.category == "rate_limited"
    assert signal.retry_after_seconds == 300


def test_opencode_auto_rejected_skill_permission_is_explicit_failure():
    classifier = AgentOutputClassifier(
        adapter="opencode", provider_id="opencode-zen-free"
    )

    signal = classifier(
        "stderr",
        "\x1b[93m! permission requested: skill (ai-review); auto-rejecting",
    )

    assert signal is not None
    assert signal.category == "permission_required"
    assert signal.persistent is False
    assert "skill for ai-review" in signal.summary
    assert "auto-rejecting" in signal.excerpt


def test_opencode_text_event_cannot_poison_provider_health():
    classifier = AgentOutputClassifier(
        adapter="opencode", provider_id="opencode-zen-free"
    )
    event = (
        '{"type":"text","part":{"type":"text","text":'
        '"configured model is unavailable in the product under review"}}\n'
    )

    assert classifier("stdout", event) is None


def test_opencode_tool_event_cannot_poison_provider_health():
    classifier = AgentOutputClassifier(
        adapter="opencode", provider_id="opencode-zen-free"
    )
    event = (
        '{"type":"tool_use","part":{"type":"tool-use","state":{"output":'
        '"invalid model should be handled by this code path"}}}\n'
    )

    assert classifier("stdout", event) is None


def test_opencode_structured_error_event_classifies_invalid_model():
    classifier = AgentOutputClassifier(
        adapter="opencode", provider_id="opencode-zen-free"
    )
    event = (
        '{"type":"error","error":{"name":"ModelNotFound",'
        '"data":{"message":"model opencode/missing is unavailable"}}}\n'
    )

    signal = classifier("stdout", event)

    assert signal is not None
    assert signal.category == "invalid_model"
    assert signal.persistent is True


def test_opencode_structured_error_is_parsed_across_chunks():
    classifier = AgentOutputClassifier(
        adapter="opencode", provider_id="opencode-zen-free"
    )

    assert classifier("stdout", '{"type":"error","error":{"data":') is None
    signal = classifier(
        "stdout", '{"message":"quota exhausted; reset in 2 days"}}}\n'
    )

    assert signal is not None
    assert signal.category == "quota_exhausted"
    assert signal.retry_after_seconds == 172800


def test_opencode_missing_agent_is_invalid_configuration_not_model_failure():
    classifier = AgentOutputClassifier(
        adapter="opencode", provider_id="opencode-zen-free"
    )

    signal = classifier(
        "stderr",
        '! agent "ai-reviewer" not found. Falling back to default agent',
    )

    assert signal is not None
    assert signal.category == "invalid_configuration"
    assert signal.persistent is False
    assert "ai-reviewer" in signal.summary


def test_codex_agent_message_cannot_poison_provider_health():
    classifier = AgentOutputClassifier(adapter="codex", provider_id="codex")
    event = (
        '{"type":"item.completed","item":{"id":"item_0",'
        '"type":"agent_message","text":"the product reports invalid model"}}\n'
    )

    assert classifier("stdout", event) is None


def test_codex_structured_failure_classifies_invalid_model():
    classifier = AgentOutputClassifier(adapter="codex", provider_id="codex")
    event = (
        '{"type":"turn.failed","error":{"message":'
        '"model gpt-missing is unavailable"}}\n'
    )

    signal = classifier("stdout", event)

    assert signal is not None
    assert signal.category == "invalid_model"
    assert signal.persistent is True


def test_claude_success_json_content_cannot_poison_provider_health():
    classifier = AgentOutputClassifier(
        adapter="claude-code", provider_id="claude-code"
    )
    payload = (
        '{"type":"result","subtype":"success","is_error":false,'
        '"result":"configured model is unavailable in the code under review"}'
    )

    assert classifier("stdout", payload) is None


def test_gemini_success_json_content_cannot_poison_provider_health():
    classifier = AgentOutputClassifier(
        adapter="antigravity-cli", provider_id="antigravity"
    )
    payload = (
        '{"response":"the product should recover when its configured model '
        'is unavailable","stats":{}}'
    )

    assert classifier("stdout", payload) is None
