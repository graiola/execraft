from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from execraft.runtime.openclaw_subagent_turn import (
    bind_openclaw_subagent_turn,
    prepare_openclaw_subagent_turn,
)
from execraft.runtime.subagent_policy import SubagentProfilePolicy


@dataclass(frozen=True)
class _Handoff:
    execution_context: dict[str, object] = field(default_factory=dict)
    read_only: bool = False


@dataclass(frozen=True)
class _Request:
    handoff: _Handoff
    continuation_handoff: _Handoff | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    capability: str = "implement"
    stage: str = "implementation"


@dataclass(frozen=True)
class _SecurityTurn:
    request: _Request
    agent_id: str
    workspace: Path


@dataclass(frozen=True)
class _Profile:
    id: str = "worker"


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    def request(self, method: str, params: dict[str, str]):
        self.calls.append((method, params))
        return {}


def test_admission_uses_dedicated_parent_and_binds_exact_child_workspace(tmp_path: Path) -> None:
    security = _SecurityTurn(
        request=_Request(handoff=_Handoff()),
        agent_id="worker",
        workspace=tmp_path / "task",
    )
    policy = SubagentProfilePolicy(enabled=True)

    projected, turn = prepare_openclaw_subagent_turn(
        security, _Profile(), security.request, policy
    )

    assert turn.admitted is True
    assert projected.agent_id == "worker-delegate"
    guard = projected.request.handoff.execution_context["openclaw_subagents"]
    assert guard["specialist_agent_id"] == "worker-specialist"
    assert guard["max_spawn_depth"] == 1
    client = _Client()
    bind_openclaw_subagent_turn(client, projected, turn)
    assert client.calls == [
        (
            "agents.update",
            {"agentId": "worker-specialist", "workspace": str(tmp_path / "task")},
        )
    ]


def test_read_only_parent_uses_separate_read_only_delegate(tmp_path: Path) -> None:
    request = _Request(handoff=_Handoff(read_only=True), capability="review", stage="review")
    security = _SecurityTurn(
        request=request,
        agent_id="worker-readonly",
        workspace=tmp_path / "task",
    )
    projected, turn = prepare_openclaw_subagent_turn(
        security, _Profile(), request, SubagentProfilePolicy(enabled=True)
    )
    assert turn.admitted is True
    assert projected.agent_id == "worker-readonly-delegate"


def test_execraft_parallel_request_keeps_ordinary_wp11_identity(tmp_path: Path) -> None:
    request = _Request(handoff=_Handoff(), metadata={"shard_count": 3})
    security = _SecurityTurn(request=request, agent_id="worker", workspace=tmp_path / "task")
    projected, turn = prepare_openclaw_subagent_turn(
        security, _Profile(), request, SubagentProfilePolicy(enabled=True)
    )
    assert turn.admitted is False
    assert turn.reason == "execraft_parallelism_active"
    assert projected is security
