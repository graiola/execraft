from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from execraft.runtime.openclaw_subagents import (
    apply_openclaw_subagent_projection,
    delegating_agent_id,
    specialist_agent_id,
)
from execraft.runtime.subagent_policy import (
    SubagentPolicyError,
    parse_subagent_strategy,
    request_has_parallelism,
)


@dataclass
class _Profile:
    id: str = "worker"
    name: str = "Worker"
    enabled: bool = True
    runtime_id: str = "openclaw"


@dataclass
class _Route:
    id: str
    provider: str = "ollama"
    provider_alias: str = "ollama-local"
    model: str = "qwen"


class _Execution:
    def __init__(self):
        self.agents = (_Profile(),)
        self._runtime = SimpleNamespace(
            kind=SimpleNamespace(value="openclaw"),
            openclaw=SimpleNamespace(mode=SimpleNamespace(value="managed")),
        )
        self._route = _Route("child")

    def runtime(self, _runtime_id):
        return self._runtime

    def model_route(self, route_id):
        if route_id != "child":
            raise KeyError(route_id)
        return self._route


def _strategy(**profile_overrides):
    raw = {
        "subagents": {
            "enabled": True,
            "profiles": {"worker": {"enabled": True, **profile_overrides}},
        }
    }
    return parse_subagent_strategy(raw)


def _base_config():
    return {
        "models": {"providers": {"ollama-local": {"models": [{"id": "qwen"}]}}},
        "agents": {
            "defaults": {"skipBootstrap": True},
            "entries": {
                "worker": {
                    "model": "ollama-local/qwen",
                    "sandbox": {"workspaceAccess": "rw"},
                    "tools": {
                        "allow": ["read", "write", "exec"],
                        "deny": ["sessions_spawn", "subagents"],
                    },
                },
                "worker-readonly": {
                    "model": "ollama-local/qwen",
                    "sandbox": {"workspaceAccess": "ro"},
                    "tools": {
                        "allow": ["read"],
                        "deny": [
                            "write", "edit", "exec", "sessions_spawn", "subagents"
                        ],
                    },
                },
            },
        },
    }


def test_is_disabled_by_default_and_rejects_mutating_child_tools():
    assert parse_subagent_strategy({}).active is False
    with pytest.raises(SubagentPolicyError, match="read-oriented"):
        _strategy(allowed_tools=["read", "exec"])
    with pytest.raises(SubagentPolicyError, match="max_spawn_depth=1"):
        _strategy(max_spawn_depth=2)


def test_projection_keeps_wp11_agents_and_adds_explicit_leaf_delegation_identities():
    projected = apply_openclaw_subagent_projection(
        _base_config(), _Execution(), "openclaw", _strategy()
    )
    entries = projected["agents"]["entries"]
    assert entries["worker"]["tools"]["deny"] == ["sessions_spawn", "subagents"]
    parent = entries[delegating_agent_id("worker", read_only=False)]
    child_id = specialist_agent_id("worker")
    assert parent["subagents"] == {"allowAgents": [child_id], "requireAgentId": True}
    assert "sessions_spawn" in parent["tools"]["allow"]
    assert "sessions_yield" in parent["tools"]["allow"]
    assert "sessions_spawn" not in parent["tools"]["deny"]
    child = entries[child_id]
    assert child["sandbox"]["workspaceAccess"] == "ro"
    assert child["tools"]["allow"] == ["read", "grep", "glob"]
    assert "exec" in child["tools"]["deny"]
    assert child["subagents"]["allowAgents"] == []
    defaults = projected["agents"]["defaults"]["subagents"]
    assert defaults["maxSpawnDepth"] == 1
    assert defaults["requireAgentId"] is True


def test_external_gateway_delegation_fails_closed():
    execution = _Execution()
    execution._runtime.openclaw.mode.value = "external"
    with pytest.raises(SubagentPolicyError, match="requires managed OpenClaw"):
        apply_openclaw_subagent_projection(_base_config(), execution, "openclaw", _strategy())


def test_child_model_override_requires_route_already_projected():
    projected = apply_openclaw_subagent_projection(
        _base_config(), _Execution(), "openclaw", _strategy(child_model_route="child")
    )
    assert projected["agents"]["entries"]["worker-specialist"]["model"] == "ollama-local/qwen"


def test_execraft_parallelism_suppresses_nested_parallelism():
    request = SimpleNamespace(
        metadata={"shard_count": 3},
        handoff=SimpleNamespace(execution_context={}),
    )
    assert request_has_parallelism(request)
    request = SimpleNamespace(
        metadata={},
        handoff=SimpleNamespace(execution_context={"wave_size": 2}),
    )
    assert request_has_parallelism(request)
    request = SimpleNamespace(
        metadata={"scheduler": {"parallel_execution": True}},
        handoff=SimpleNamespace(execution_context={}),
    )
    assert request_has_parallelism(request)
    request = SimpleNamespace(metadata={}, handoff=SimpleNamespace(execution_context={}))
    assert not request_has_parallelism(request)



def test_generated_agent_identity_collision_fails_closed():
    config = _base_config()
    config["agents"]["entries"]["worker-specialist"] = {"model": "ollama-local/qwen"}
    with pytest.raises(SubagentPolicyError, match="agent id collision"):
        apply_openclaw_subagent_projection(config, _Execution(), "openclaw", _strategy())
