from __future__ import annotations

from execraft.runtime.openclaw_protocol import GatewayEvent
from execraft.runtime.openclaw_subagent_budget import OpenClawSubagentBudgetMonitor
from execraft.runtime.subagent_policy import SubagentProfilePolicy


class _Client:
    def __init__(self) -> None:
        self.cancelled: list[tuple[str, str]] = []

    def cancel_run(self, run_id: str, *, session_key: str = "") -> None:
        self.cancelled.append((run_id, session_key))


def _event(run_id: str, session_key: str, *, input_tokens: int = 0) -> GatewayEvent:
    return GatewayEvent(
        name="agent",
        payload={
            "type": "subagent-result",
            "childRunId": run_id,
            "childSessionKey": session_key,
            "usage": {
                "inputTokens": input_tokens,
                "outputTokens": 5,
                "totalTokens": input_tokens + 5,
                "estimatedCostUsd": 0.01,
            },
        },
    )


def test_budget_monitor_attributes_usage_and_cancels_token_overrun() -> None:
    client = _Client()
    policy = SubagentProfilePolicy(enabled=True, max_input_tokens=256)
    monitor = OpenClawSubagentBudgetMonitor(client=client, policy=policy)
    monitor(_event("run-1", "child-1", input_tokens=300))

    telemetry = monitor.telemetry()["subagent_usage"]
    assert telemetry["spawn_count"] == 1
    assert telemetry["input_tokens"] == 300
    assert telemetry["attribution_complete"] is True
    assert telemetry["budget_exceeded"] == ["input_tokens"]
    assert client.cancelled == [("run-1", "child-1")]


def test_budget_monitor_cancels_children_beyond_parent_bound() -> None:
    client = _Client()
    policy = SubagentProfilePolicy(enabled=True, max_children_per_parent=1)
    monitor = OpenClawSubagentBudgetMonitor(client=client, policy=policy)
    monitor(_event("run-1", "child-1", input_tokens=10))
    monitor(_event("run-2", "child-2", input_tokens=10))

    telemetry = monitor.telemetry()["subagent_usage"]
    assert telemetry["spawn_count"] == 2
    assert "max_children_per_parent" in telemetry["budget_exceeded"]
    assert ("run-2", "child-2") in client.cancelled


def test_budget_monitor_zero_spawn_has_complete_zero_attribution() -> None:
    client = _Client()
    policy = SubagentProfilePolicy(enabled=True)
    monitor = OpenClawSubagentBudgetMonitor(client=client, policy=policy)
    telemetry = monitor.telemetry()["subagent_usage"]
    assert telemetry["spawn_count"] == 0
    assert telemetry["attribution_complete"] is True


def test_existing_child_count_violation_is_not_overwritten_by_usage_limit() -> None:
    client = _Client()
    policy = SubagentProfilePolicy(
        enabled=True, max_children_per_parent=1, max_input_tokens=256
    )
    monitor = OpenClawSubagentBudgetMonitor(client=client, policy=policy)
    monitor(_event("run-1", "child-1", input_tokens=10))
    monitor(_event("run-2", "child-2", input_tokens=300))
    telemetry = monitor.telemetry()["subagent_usage"]
    second = next(item for item in telemetry["children"] if item["run_id"] == "run-2")
    assert second["exceeded"] == "max_children_per_parent"


def test_budget_monitor_ignores_explicitly_foreign_parent_events() -> None:
    client = _Client()
    policy = SubagentProfilePolicy(enabled=True)
    monitor = OpenClawSubagentBudgetMonitor(client=client, policy=policy)
    monitor.bind_parent("parent-1", "session-parent-1")
    event = _event("child-run", "child-session", input_tokens=10)
    payload = dict(event.payload)
    payload["runId"] = "parent-2"
    monitor(GatewayEvent(name=event.name, payload=payload))
    telemetry = monitor.telemetry()["subagent_usage"]
    assert telemetry["spawn_count"] == 0
    assert telemetry["foreign_events_ignored"] == 1
