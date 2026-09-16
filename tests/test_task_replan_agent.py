"""Focused contract tests for the read-only ai-replan collaborator."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from execraft.agents.config import AgentProviderConfig
from execraft.onboarding.providers import ProviderInventoryItem
from execraft.onboarding.start_models import ProviderChoice
from execraft.orchestrate.scheduler import AgentCapability
from execraft.replan.agent import AgentReplanner
from execraft.replan.models import ReplanConsistencyError, ReplanError


class _Adapter:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.handoff = None

    def execute(self, handoff):
        self.handoff = handoff
        return self.payload


def _provider() -> ProviderChoice:
    item = ProviderInventoryItem(
        name="planner",
        provider_id="planner",
        adapter="codex",
        enabled=True,
        binary="codex",
        binary_path="/usr/bin/codex",
        model="",
        capabilities=("plan",),
        priority=100,
    )
    config = AgentProviderConfig(
        name="planner",
        adapter="codex",
        enabled=True,
        provider_id="planner",
        binary="codex",
        capabilities=frozenset({AgentCapability.PLAN}),
        capability_weight=100,
        priority=100,
    )
    return ProviderChoice(item, config, "test")


def _proposal(*, consistent: bool = True) -> str:
    return json.dumps(
        {
            "consistent": consistent,
            "consistency_summary": "coherent" if consistent else "brief contradicts plan",
            "brief_markdown": "# Brief\n\nUpdated.\n",
            "plan_markdown": "# Plan\n\nUpdated.\n",
            "plan_graph": {
                "schema_version": 1,
                "source_document": "PLAN.md",
                "work_packages": [],
            },
            "package_mapping": {},
            "change_summary": "updated definition",
        }
    )


def _invoke(replanner: AgentReplanner, tmp_path: Path):
    return replanner.propose(
        provider=_provider(),
        workdir=tmp_path,
        project_id="app",
        task_id="demo",
        allowed_repositories=("app",),
        current_brief="# Brief\n",
        current_plan="# Plan\n",
        current_graph_yaml="schema_version: 1\nwork_packages: []\n",
        state_summary={"project_state": "running", "packages": []},
        requested_change="Add a new pending Work Package",
    )


def test_agent_replanner_is_read_only_and_materializes_replan_skill(tmp_path: Path) -> None:
    adapter = _Adapter({"ok": True, "final_message": _proposal()})
    calls = []

    def builder(config, *, workdir, read_only):
        calls.append((config.provider_id, workdir, read_only))
        return adapter

    proposal = _invoke(AgentReplanner(adapter_builder=builder), tmp_path)

    assert proposal.provider_id == "planner"
    assert calls == [("planner", tmp_path, True)]
    assert adapter.handoff.read_only is True
    assert adapter.handoff.stage == "replan"
    assert adapter.handoff.execution_context["mode"] == "task_definition_replan"
    assert any(skill["id"] == "ai-replan" for skill in adapter.handoff.workflow_skills)
    graph_schema = adapter.handoff.expected_output_schema["properties"]["plan_graph"]
    packages = graph_schema["properties"]["work_packages"]
    assert packages["minItems"] == 1
    repository_schema = packages["items"]["properties"]["affected_repositories"]
    assert repository_schema["items"]["enum"] == ["app"]


def test_agent_replanner_rejects_inconsistent_candidate(tmp_path: Path) -> None:
    adapter = _Adapter({"ok": True, "final_message": _proposal(consistent=False)})
    replanner = AgentReplanner(adapter_builder=lambda *_args, **_kwargs: adapter)

    with pytest.raises(ReplanConsistencyError, match="materially inconsistent"):
        _invoke(replanner, tmp_path)


def test_agent_replanner_rejects_invalid_provider_contract(tmp_path: Path) -> None:
    malformed = json.loads(_proposal())
    malformed["consistent"] = "yes"
    adapter = _Adapter({"ok": True, "final_message": json.dumps(malformed)})
    replanner = AgentReplanner(adapter_builder=lambda *_args, **_kwargs: adapter)

    with pytest.raises(ReplanError, match="consistent must be a boolean"):
        _invoke(replanner, tmp_path)


def test_agent_replanner_enforces_bounded_context(tmp_path: Path) -> None:
    adapter = _Adapter({"ok": True, "final_message": _proposal()})
    replanner = AgentReplanner(adapter_builder=lambda *_args, **_kwargs: adapter)

    with pytest.raises(ReplanError, match="safety budget"):
        replanner.propose(
            provider=_provider(),
            workdir=tmp_path,
            project_id="app",
            task_id="demo",
            allowed_repositories=("app",),
            current_brief="x" * (385 * 1024),
            current_plan="# Plan\n",
            current_graph_yaml="schema_version: 1\nwork_packages: []\n",
            state_summary={},
            requested_change="change",
        )
