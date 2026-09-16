from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from execraft.orchestrate import (
    AcceptanceCriterion,
    AgentCapability,
    Availability,
    IncidentClass,
    IncidentStatus,
    OrchestrationConfig,
    ProjectOrchestrator,
    TaskExecutionState,
    RecoveryPlaybookConfigError,
    RecoveryPlaybookPolicy,
    SupervisorIncidentStore,
    SupervisorPolicy,
    VerificationCommand,
    VerificationRegistry,
    WorkPackage,
    WorkPackageStage,
    normalize_work_packages,
)
from execraft.orchestrate.models import _StageEscalated
from execraft.orchestrate.daemon import DaemonConfig, run_until_terminal
from execraft.orchestrate.recovery_playbook import (
    findings_fingerprint,
    indexed_findings,
    recovery_playbook_policy_from_scheduling,
)


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=path, text=True, capture_output=True, check=True
    ).stdout.strip()


def _repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "execraft@example.invalid")
    _git(path, "config", "user.name", "Execraft tests")
    (path / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-q", "-m", "baseline")
    return path


class _Completed:
    returncode = 0
    stdout = "ok"
    stderr = ""


def _passing_runner(command: str, *, cwd: Path, timeout: int):
    return _Completed()


class _NeverSupervisor:
    adapter_name = "codex"

    def __init__(self) -> None:
        self.calls = 0

    @property
    def provider_id(self):
        return "codex"

    @property
    def availability(self):
        return Availability.AVAILABLE

    @property
    def capabilities(self):
        return {AgentCapability.SUPERVISE}

    def execute(self, handoff):
        self.calls += 1
        raise AssertionError("deterministic review recovery must bypass supervision")


class _DigestSupervisor:
    adapter_name = "codex"

    def __init__(self) -> None:
        self.calls = []

    @property
    def provider_id(self):
        return "codex"

    @property
    def availability(self):
        return Availability.AVAILABLE

    @property
    def capabilities(self):
        return {AgentCapability.SUPERVISE}

    def execute(self, handoff):
        self.calls.append(handoff)
        return {
            "ok": True,
            "decision": "ask_human",
            "classification": "requirement_ambiguity",
            "summary": (
                "Two bounded repair campaigns left the direct Edge transport "
                "contract unresolved."
            ),
            "actions_taken": [
                "Compared the final-review findings with the package requirements.",
                "Confirmed that another blind repair retry would repeat exhausted work.",
            ],
            "resume_stage": "",
            "acceptance_evidence": [],
            "implementation_summary": "",
            "retain_paths": [],
            "discard_paths": [],
            "delegations": [],
            "human_question": {
                "question": (
                    "Should WP17-S4 implement the full direct Edge transport now, "
                    "or narrow the package and defer it?"
                ),
                "context": (
                    "The current implementation still routes Edge control through "
                    "Core after two autonomous repair campaigns."
                ),
                "recommended_option": "implement_now",
                "options": [
                    {
                        "id": "implement_now",
                        "label": "Implement the full transport now",
                        "consequence": (
                            "The Supervisor delegates a larger bounded repair and "
                            "reruns verification and final review."
                        ),
                        "weight": 65,
                        "risk": "product",
                    },
                    {
                        "id": "defer",
                        "label": "Defer the transport change",
                        "consequence": (
                            "The package scope and acceptance criteria must be "
                            "replanned before work can continue."
                        ),
                        "weight": 35,
                        "risk": "product",
                    },
                ],
            },
        }


class _RescueFixer:
    adapter_name = "claude-code"

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.calls = []

    @property
    def provider_id(self):
        return "claude-code"

    @property
    def availability(self):
        return Availability.AVAILABLE

    @property
    def capabilities(self):
        return {AgentCapability.FIX_REVIEW}

    def execute(self, handoff):
        self.calls.append(handoff)
        assert handoff.stage == "review_recovery_fix"
        assert "[RF-001]" in handoff.unresolved_findings[0]
        (self.repo / "HANDOFF.md").write_text(
            "## WP17-S4\nDurable completion evidence.\n", encoding="utf-8"
        )
        return {
            "ok": True,
            "status": "fixed",
            "summary": "Added the missing durable evidence and corrected metadata.",
            "implementation_summary": (
                "Added transport routing and lease display while retaining "
                "the legacy path until the next tranche."
            ),
            "resolved_finding_ids": ["RF-001"],
            "acceptance_evidence": {
                "done": "HANDOFF.md contains the durable WP17-S4 section; fixture passed"
            },
        }


class _FinalReviewer:
    adapter_name = "opencode"

    def __init__(self) -> None:
        self.calls = []

    @property
    def provider_id(self):
        return "reviewer"

    @property
    def availability(self):
        return Availability.AVAILABLE

    @property
    def capabilities(self):
        return {AgentCapability.REVIEW}

    def execute(self, handoff):
        self.calls.append(handoff)
        return {
            "ok": True,
            "verdict": "approved",
            "findings": [],
            "summary": "The evidence and implementation summary now match the workspace.",
        }


def _blocked_orchestrator(tmp_path: Path):
    repo = _repo(tmp_path / "repo")
    config = OrchestrationConfig(state_dir=tmp_path / "state")
    config.supervisor_policy = SupervisorPolicy(agent_id="codex")
    config.recovery_playbook_policy = RecoveryPlaybookPolicy.from_mapping(
        {
            "enabled": True,
            "review_exhausted": {
                "max_rescue_cycles": 2,
                "prefer_non_supervisor_fixer": True,
                "allow_supervisor_fallback": False,
            },
        }
    )
    orch = ProjectOrchestrator(
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
    graph, report = normalize_work_packages(
        [
            WorkPackage(
                id="wp1",
                title="Evidence repair",
                requirements=["Keep durable evidence aligned with implementation"],
                acceptance_criteria=[
                    AcceptanceCriterion(id="done", description="Evidence is durable")
                ],
                affected_repositories=["repo"],
                write_scope=["repo"],
                verification_profile="focused",
            )
        ]
    )
    orch.initialize_graph(graph, report)
    supervisor = _NeverSupervisor()
    fixer = _RescueFixer(repo)
    reviewer = _FinalReviewer()
    for adapter in (supervisor, fixer, reviewer):
        orch.register_agent(adapter)
    orch.transition_to(TaskExecutionState.RUNNING)
    package = orch._state_record.plan_graph.package_by_id("wp1")
    package.stage = WorkPackageStage.FINAL_REVIEW
    package.status = "running"
    package.review_cycles = config.max_review_cycles + 1
    package.review_findings = [
        "HANDOFF.md lacks the durable WP17-S4 section and the implementation summary overstates legacy removal."
    ]
    package.implementation_summary = "Legacy path removed."
    package.reviewer_id = "reviewer"
    orch.save_state()
    orch._journal.append(
        "human_intervention_required",
        {
            "package_id": package.id,
            "stage": "final_review",
            "reason": "review/fix cycle budget exhausted",
            "evidence": list(package.review_findings),
        },
    )
    orch.transition_to(TaskExecutionState.HUMAN_REQUIRED)
    incident = orch._supervisor_incidents.open_or_reuse(
        fingerprint="review-exhausted",
        package_id=package.id,
        stage="final_review",
        classification=IncidentClass.REVIEW_EXHAUSTED,
        escalation_sequence=orch._journal.read()[-1].sequence,
        supervisor_agent_id="codex",
        workspace_digest=orch._workspace_digest(),
    )
    incident.summary = "Codex returned invalid structured output."
    incident.human_question = {
        "question": "Retry supervision?",
        "options": [{"id": "retry", "label": "Retry"}],
    }
    incident.touch(status=IncidentStatus.WAITING_FOR_HUMAN)
    orch._supervisor_incidents.save(incident)
    orch.transition_to(TaskExecutionState.SUPERVISING)
    orch.transition_to(TaskExecutionState.WAITING_FOR_HUMAN_DECISION)
    return orch, package, repo, supervisor, fixer, reviewer


def test_recovery_playbook_policy_is_strict_and_enabled_by_default():
    policy = recovery_playbook_policy_from_scheduling({})
    assert policy.enabled is False
    assert policy.review_exhausted.max_rescue_cycles == 2
    assert policy.review_exhausted.allow_supervisor_fallback is False
    with pytest.raises(RecoveryPlaybookConfigError):
        recovery_playbook_policy_from_scheduling(
            {"recovery_playbooks": {"review_exhausted": {"max_findings": 0}}}
        )


def test_review_finding_ids_and_fingerprint_are_stable():
    findings = ["one", "two"]
    assert indexed_findings(findings) == {"RF-001": "one", "RF-002": "two"}
    assert findings_fingerprint(findings) == findings_fingerprint(list(findings))


def test_waiting_supervisor_loop_is_replaced_by_direct_repair_and_completes(tmp_path):
    orch, package, repo, supervisor, fixer, reviewer = _blocked_orchestrator(tmp_path)

    report = orch.supervisor_status_report()["deterministic_recovery"]
    assert report["available"] is True
    assert report["avoid_supervisor_agent"] is True

    result = run_until_terminal(
        orch,
        config=DaemonConfig(max_attempts=4, initial_backoff_seconds=0),
        sleep=lambda _seconds: None,
    )

    assert result.final_state == TaskExecutionState.COMPLETED
    assert supervisor.calls == 0
    assert len(fixer.calls) == 1
    assert reviewer.calls
    assert package.review_recovery_cycles == 1
    assert package.stage == WorkPackageStage.COMPLETED
    assert "retaining the legacy path" in package.implementation_summary
    assert "## WP17-S4" in (repo / "HANDOFF.md").read_text(encoding="utf-8")
    incident = SupervisorIncidentStore(
        tmp_path / "state" / "projects" / "task" / "supervisor-incidents.json"
    ).recent(1)[0]
    assert incident.status == IncidentStatus.RESOLVED
    assert "deterministic" in incident.summary.lower()


def test_live_review_budget_queues_playbook_before_human_escalation(tmp_path):
    orch, package, _repo_path, _supervisor, _fixer, _reviewer = _blocked_orchestrator(
        tmp_path
    )
    # Recreate the live state immediately before the budget check.
    orch.transition_to(TaskExecutionState.SUPERVISING)
    orch.transition_to(TaskExecutionState.RUNNING)
    package.stage = WorkPackageStage.FINAL_REVIEW
    package.status = "running"
    package.review_cycles = orch.config.max_review_cycles
    package.review_recovery_cycles = 0
    orch.save_state()

    orch._queue_review_fixes(package, ["exact final-review finding"])

    assert orch.state == TaskExecutionState.RUNNING
    assert package.stage == WorkPackageStage.FIX_REVIEW
    assert package.review_recovery_cycles == 1
    assert package.review_findings == ["exact final-review finding"]




def test_rescue_wait_keeps_supervisor_excluded_when_no_other_fixer(tmp_path):
    orch, package, _repo_path, supervisor, _fixer, _reviewer = _blocked_orchestrator(
        tmp_path
    )
    package.review_recovery_cycles = 1
    package.review_recovery_fingerprint = "fingerprint"
    orch._agent_slots = [supervisor]

    selection = orch._select_fixer(package)

    assert selection.agent_id == ""
    assert selection.excluded_agent_ids == frozenset({"codex"})
    assert selection.policy == "review_recovery_wait_non_supervisor"

def test_playbook_fails_closed_when_finding_set_exceeds_policy(tmp_path):
    orch, package, _repo_path, _supervisor, _fixer, _reviewer = _blocked_orchestrator(
        tmp_path
    )
    limit = orch.config.recovery_playbook_policy.review_exhausted.max_findings
    package.review_findings = [f"finding {index}" for index in range(limit + 1)]
    orch.save_state()

    assert orch.can_auto_resume_review_recovery() is False

def test_playbook_exhaustion_enters_supervisor_before_requesting_human(tmp_path):
    orch, package, _repo_path, _supervisor, _fixer, _reviewer = _blocked_orchestrator(
        tmp_path
    )
    supervisor = _DigestSupervisor()
    orch._agent_slots = [
        supervisor,
        *[
            adapter
            for adapter in orch._agent_slots
            if adapter.provider_id != supervisor.provider_id
        ],
    ]
    policy = orch.config.recovery_playbook_policy.review_exhausted
    package.review_recovery_cycles = policy.max_rescue_cycles
    package.review_cycles = orch.config.max_review_cycles
    orch.transition_to(TaskExecutionState.SUPERVISING)
    orch.transition_to(TaskExecutionState.RUNNING)

    with pytest.raises(Exception):
        orch._queue_review_fixes(package, ["still broken"])

    assert orch.state == TaskExecutionState.HUMAN_REQUIRED
    assert orch.can_supervise_human_required() is True

    orch.run_pipeline()

    assert len(supervisor.calls) == 1
    assert supervisor.calls[0].stage == "supervise"
    assert supervisor.calls[0].unresolved_findings == ["still broken"]
    assert orch.state == TaskExecutionState.WAITING_FOR_HUMAN_DECISION
    incident = orch.supervisor_status_report()["incident"]
    assert incident["status"] == "waiting_for_human"
    assert "repair campaigns" in incident["summary"]
    question = incident["human_question"]
    assert question["recommended_option"] == "implement_now"
    assert {option["id"] for option in question["options"]} == {
        "implement_now",
        "defer",
    }


def test_external_review_blocker_escalates_without_spending_fix_budget(tmp_path):
    orch, package, _repo_path, _supervisor, _fixer, _reviewer = _blocked_orchestrator(
        tmp_path
    )
    orch.transition_to(TaskExecutionState.SUPERVISING)
    orch.transition_to(TaskExecutionState.RUNNING)
    package.stage = WorkPackageStage.FINAL_REVIEW
    package.status = "running"
    package.review_cycles = 0
    package.review_recovery_cycles = 0
    orch.save_state()

    finding = (
        "WP24-FR-003 [critical] real-simulation acceptance is unmet; "
        "the implementation result conceded that the sandbox denied Docker/AF_INET. "
        "Execute the real Gazebo/PX4 acceptance run and capture durable evidence."
    )

    with pytest.raises(_StageEscalated):
        orch._queue_review_fixes(package, [finding])

    assert orch.state == TaskExecutionState.HUMAN_REQUIRED
    assert package.stage == WorkPackageStage.FINAL_REVIEW
    assert package.review_cycles == 0
    assert package.review_recovery_cycles == 0
    assert package.review_findings == [finding]
    action = orch.human_required_report()
    assert action is not None
    assert action["reason"] == "external acceptance action required"
    assert action["evidence"] == [finding]
    assert action["supervisor_eligible"] is False
    assert "external acceptance run" in action["recommended_decision"]
    assert orch.can_supervise_human_required() is False
