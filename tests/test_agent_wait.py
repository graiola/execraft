"""Deterministic tests for the extracted provider wait planner."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from execraft.orchestrate.agent_wait import (
    AgentWaitAdapter,
    AgentWaitAttempt,
    AgentWaitConfig,
    build_agent_wait_plan,
)


NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
CONFIG = AgentWaitConfig(
    retry_initial_seconds=30,
    retry_max_seconds=300,
    poll_max_seconds=300,
    known_deadline_poll_max_seconds=1800,
    blocking_wait_max_seconds=1800,
)


def _adapter(**overrides):
    values = {
        "agent_id": "codex",
        "model": "gpt",
        "availability": "available",
        "health_available": True,
        "health_reason": "",
        "unavailable_until": "",
        "max_complexity": 100,
    }
    values.update(overrides)
    return AgentWaitAdapter(**values)


def test_untried_available_provider_is_rechecked_promptly() -> None:
    plan = build_agent_wait_plan(
        package_id="WP1",
        stage="implement",
        capability="implement",
        previous={},
        attempts=(),
        adapters=(_adapter(),),
        excluded_agent_ids=set(),
        task_complexity=20,
        config=CONFIG,
        now=NOW,
    )
    assert plan.poll_after_seconds == 1
    assert plan.candidates[0]["deadline_kind"] == "orchestrator_probe"
    assert plan.next_retry["agent_id"] == "codex"


def test_health_deadline_uses_known_deadline_poll_cap() -> None:
    unblock = (NOW + timedelta(hours=2)).isoformat()
    plan = build_agent_wait_plan(
        package_id="WP1",
        stage="review",
        capability="review",
        previous={},
        attempts=(),
        adapters=(
            _adapter(
                health_available=False,
                health_reason="quota",
                unavailable_until=unblock,
            ),
        ),
        excluded_agent_ids=set(),
        task_complexity=20,
        config=CONFIG,
        now=NOW,
    )
    assert plan.all_deadlines_known is True
    assert plan.poll_after_seconds == 1800
    assert plan.first_reported_unblock["available_at"] == unblock


def test_complexity_only_candidates_are_reported_for_escalation() -> None:
    plan = build_agent_wait_plan(
        package_id="WP1",
        stage="implement",
        capability="implement",
        previous={},
        attempts=(),
        adapters=(_adapter(max_complexity=40),),
        excluded_agent_ids=set(),
        task_complexity=90,
        config=CONFIG,
        now=NOW,
    )
    assert not plan.candidates
    assert plan.policy_excluded[0]["reason"] == "complexity_limit"


def test_wait_expiry_is_deterministic() -> None:
    previous = {
        "package_id": "WP1",
        "stage": "implement",
        "capability": "implement",
        "cycle": 3,
        "since": (NOW - timedelta(seconds=1801)).isoformat(),
    }
    plan = build_agent_wait_plan(
        package_id="WP1",
        stage="implement",
        capability="implement",
        previous=previous,
        attempts=(AgentWaitAttempt("codex", "provider_error", "failed"),),
        adapters=(_adapter(),),
        excluded_agent_ids=set(),
        task_complexity=20,
        config=CONFIG,
        now=NOW,
    )
    assert plan.expired is True
    assert plan.cycle == 4
    assert plan.waited_seconds == 1801


def test_persisted_health_deadline_overrides_transient_adapter_flag() -> None:
    """A just-failed in-memory adapter must not hide its durable reset time."""

    claude_unblock = (NOW + timedelta(hours=3)).isoformat()
    codex_unblock = (NOW + timedelta(days=2)).isoformat()
    plan = build_agent_wait_plan(
        package_id="WP22__WP22-S4",
        stage="supervise",
        capability="supervise",
        previous={},
        attempts=(
            AgentWaitAttempt(
                "claude-code",
                "session_limit",
                "You've hit your session limit",
                retry_after_seconds=3 * 60 * 60,
            ),
        ),
        adapters=(
            _adapter(
                agent_id="codex",
                availability="available",
                health_available=False,
                health_reason="quota_exhausted",
                unavailable_until=codex_unblock,
            ),
            _adapter(
                agent_id="claude-code",
                availability="session_limit",
                health_available=False,
                health_reason="session_limit",
                unavailable_until=claude_unblock,
            ),
        ),
        excluded_agent_ids=set(),
        task_complexity=20,
        config=CONFIG,
        now=NOW,
    )

    by_id = {item["agent_id"]: item for item in plan.candidates}
    assert by_id["claude-code"]["deadline_kind"] == "health_unblock"
    assert by_id["claude-code"]["available_at"] == claude_unblock
    assert plan.all_deadlines_known is True
    assert plan.first_reported_unblock["agent_id"] == "claude-code"
    assert plan.first_reported_unblock["available_at"] == claude_unblock
