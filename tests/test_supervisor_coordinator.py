"""Tests for SupervisorCoordinator extraction."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from execraft.orchestrate import (
    AgentCapability,
    Availability,
    IncidentStatus,
    OrchestrationConfig,
    ProjectOrchestrator,
    TaskExecutionState,
    SupervisorIncidentStore,
    SupervisorPolicy,
    VerificationCommand,
    VerificationRegistry,
    WorkPackageStage,
)

from tests.test_supervisor import (
    _repo,
    _orchestrator,
    _Supervisor,
    _resolved,
    _passing_runner,
)


def test_delegation_results_preserved_for_next_supervisor_round(tmp_path):
    """Prove delegated summary, findings, and artifact reach the next Supervisor handoff."""
    repo = _repo(tmp_path / "repo")
    orch, package = _orchestrator(tmp_path, repo)

    supervisor = _Supervisor(
        [
            {
                "ok": True,
                "decision": "delegate",
                "classification": "evidence_mismatch",
                "summary": "Delegate one task",
                "actions_taken": ["diagnosed"],
                "delegations": [
                    {
                        "task": "Fix the issue",
                        "capability": "fix_review",
                        "skills": ["ai-fix-review"],
                        "read_only": False,
                    }
                ],
            },
            _resolved(),
        ]
    )

    class _Fixer:
        adapter_name = "opencode"

        def __init__(self):
            self.calls = []

        @property
        def provider_id(self):
            return "local-fixer"

        @property
        def availability(self):
            return Availability.AVAILABLE

        @property
        def capabilities(self):
            return {AgentCapability.FIX_REVIEW}

        def execute(self, handoff):
            self.calls.append(handoff)
            (repo / "fixed.txt").write_text("repaired\n", encoding="utf-8")
            return {
                "ok": True,
                "status": "fixed",
                "summary": "delegated fix complete",
            }

    fixer = _Fixer()
    orch.register_agent(supervisor)
    orch.register_agent(fixer)

    # The coordinator completes the bounded delegation and immediately gives
    # its durable result to the next Supervisor reasoning round.
    assert orch._run_supervision_unlocked() is True
    assert len(fixer.calls) == 1
    assert len(supervisor.calls) == 2

    second_handoff = supervisor.calls[1]
    delegation_results = json.loads(
        second_handoff.bounded_excerpts["delegation-results.json"]
    )
    assert len(delegation_results) == 1
    result = delegation_results[0]
    assert result["summary"] == "delegated fix complete"
    assert result["findings"] == []
    assert result["artifact"]["sha256"]
    assert Path(result["artifact"]["path"]).is_file()
    assert result["agent_id"] == "local-fixer"
    assert result["capability"] == "fix_review"


def test_delegation_crash_recovery_does_not_duplicate_work(tmp_path):
    """Prove crash after cursor save does not re-execute delegation after restart."""
    repo = _repo(tmp_path / "repo")
    orch, package = _orchestrator(tmp_path, repo)

    supervisor = _Supervisor(
        [
            {
                "ok": True,
                "decision": "delegate",
                "classification": "evidence_mismatch",
                "summary": "Delegate one task",
                "actions_taken": ["diagnosed"],
                "delegations": [
                    {
                        "task": "Fix the issue",
                        "capability": "fix_review",
                        "skills": ["ai-fix-review"],
                        "read_only": False,
                    }
                ],
            },
            _resolved(),
        ]
    )

    class _Fixer:
        adapter_name = "opencode"
        call_count = 0

        @property
        def provider_id(self):
            return "local-fixer"

        @property
        def availability(self):
            return Availability.AVAILABLE

        @property
        def capabilities(self):
            return {AgentCapability.FIX_REVIEW}

        def execute(self, handoff):
            self.call_count += 1
            return {
                "ok": True,
                "status": "fixed",
                "summary": f"attempt {self.call_count}",
            }

    fixer = _Fixer()
    orch.register_agent(supervisor)
    orch.register_agent(fixer)

    # Inject a crash after SupervisorIncidentStore.save() advances the delegation cursor
    # but before supervisor_delegation_finished completes.
    original_save = orch._supervisor_incidents.save
    crash_triggered = False

    def _crashing_save(incident):
        original_save(incident)
        if incident.pending_delegation_index == 1:
            nonlocal crash_triggered
            crash_triggered = True
            raise RuntimeError("simulated crash after cursor save")

    orch._supervisor_incidents.save = _crashing_save

    with pytest.raises(RuntimeError, match="simulated crash after cursor save"):
        orch._run_supervision_unlocked()

    assert crash_triggered is True
    assert fixer.call_count == 1

    # Recreate the orchestrator and incident store from durable state
    config = OrchestrationConfig(state_dir=tmp_path / "state")
    config.supervisor_policy = SupervisorPolicy(agent_id="codex")
    orch2 = ProjectOrchestrator(
        "task",
        config=config,
        registry=VerificationRegistry(
            commands=[
                VerificationCommand(
                    id="fixture",
                    command="fixture-check",
                    profile="focused",
                    repository_id="repo",
                )
            ],
            require_commands=True,
        ),
        command_runner=_passing_runner,
        repository_paths={"repo": repo},
        workspace_root=tmp_path,
    )
    orch2.load_state()
    orch2.register_agent(supervisor)
    orch2.register_agent(fixer)

    # Resume supervision from durable state
    assert orch2._run_supervision_unlocked() is True

    # Prove the delegated agent is not invoked again while its result reaches the next Supervisor handoff
    assert fixer.call_count == 1
    assert len(supervisor.calls) == 2

    second_handoff = supervisor.calls[1]
    delegation_results = json.loads(
        second_handoff.bounded_excerpts["delegation-results.json"]
    )
    assert len(delegation_results) == 1
    assert delegation_results[0]["summary"] == "attempt 1"
    assert orch2.state == TaskExecutionState.RUNNING


def test_self_naming_delegation_falls_back_to_another_agent(tmp_path):
    """Prove a Supervisor naming itself as delegate does not wedge the incident.

    The delegation is durable and resumes before every supervision round, so
    raising on the self-reference stranded the incident permanently: no restart,
    operator answer, or budget reset ever reached the code that could replace
    the choice.
    """
    repo = _repo(tmp_path / "repo")
    orch, package = _orchestrator(tmp_path, repo)

    supervisor = _Supervisor(
        [
            {
                "ok": True,
                "decision": "delegate",
                "classification": "review_exhausted",
                "summary": "Delegate the surgical redo",
                "actions_taken": ["diagnosed"],
                "delegations": [
                    {
                        "task": "Redo the revert surgically",
                        "capability": "fix_review",
                        # The Supervisor runs as "codex" and names itself.
                        "agent_id": "codex",
                        "skills": ["ai-fix-review"],
                        "read_only": False,
                    }
                ],
            },
            _resolved(),
        ]
    )

    class _Fixer:
        adapter_name = "opencode"

        def __init__(self):
            self.calls = []

        @property
        def provider_id(self):
            return "local-fixer"

        @property
        def availability(self):
            return Availability.AVAILABLE

        @property
        def capabilities(self):
            return {AgentCapability.FIX_REVIEW}

        def execute(self, handoff):
            self.calls.append(handoff)
            (repo / "fixed.txt").write_text("repaired\n", encoding="utf-8")
            return {
                "ok": True,
                "status": "fixed",
                "summary": "surgical redo complete",
            }

    fixer = _Fixer()
    orch.register_agent(supervisor)
    orch.register_agent(fixer)

    assert orch._run_supervision_unlocked() is True

    # The self-reference is replaced by ordinary selection, which still excludes
    # the supervisor, so the delegation runs on the other capable provider.
    assert len(fixer.calls) == 1
    delegation_results = json.loads(
        supervisor.calls[1].bounded_excerpts["delegation-results.json"]
    )
    assert delegation_results[0]["agent_id"] == "local-fixer"
    assert delegation_results[0]["summary"] == "surgical redo complete"

    incident = SupervisorIncidentStore(
        tmp_path / "state" / "projects" / "task" / "supervisor-incidents.json"
    ).list()[-1]
    assert incident.status == IncidentStatus.RESOLVED
    assert incident.pending_delegations == []
