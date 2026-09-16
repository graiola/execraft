from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from execraft.orchestrate import (
    AcceptanceCriterion,
    AgentCapability,
    AgentExecutionError,
    Availability,
    IncidentClass,
    IncidentStatus,
    HumanDecisionRequest,
    OrchestrationConfig,
    ProjectOrchestrator,
    TaskExecutionState,
    SupervisorConfigError,
    SupervisorAutoDecisionPolicy,
    SupervisorIncidentStore,
    SupervisorPolicy,
    VerificationCommand,
    VerificationRegistry,
    WorkPackage,
    WorkPackageStage,
    normalize_work_packages,
)
from execraft.orchestrate.daemon import run_until_terminal
from execraft.orchestrate.supervisor import (
    SupervisorDecision,
    SupervisorDelegation,
    classify_incident,
    evaluate_supervisor_auto_decision,
    incident_escalation_key,
    incident_fingerprint,
    provider_is_allowed_supervisor,
    supervisor_output_schema,
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


class _Supervisor:
    adapter_name = "codex"

    def __init__(self, decisions: list[dict], *, repo: Path | None = None) -> None:
        self.decisions = list(decisions)
        self.repo = repo
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
        decision = self.decisions.pop(0)
        if decision.pop("_modify", False):
            assert self.repo is not None
            (self.repo / "RECOVERY.md").write_text("recovered\n", encoding="utf-8")
        decision.setdefault("resume_stage", "")
        evidence = decision.setdefault("acceptance_evidence", [])
        if isinstance(evidence, dict):
            decision["acceptance_evidence"] = [
                {"criterion_id": key, "evidence": value}
                for key, value in evidence.items()
            ]
        decision.setdefault("implementation_summary", "")
        decision.setdefault("retain_paths", [])
        decision.setdefault("discard_paths", [])
        delegations = decision.setdefault("delegations", [])
        for delegation in delegations:
            delegation.setdefault("agent_id", "")
        decision.setdefault(
            "human_question",
            {
                "question": "",
                "context": "",
                "recommended_option": "",
                "options": [],
            },
        )
        options = decision["human_question"].get("options", [])
        if options and any("weight" not in option for option in options):
            base, remainder = divmod(100, len(options))
            for index, option in enumerate(options):
                option.setdefault("weight", base + (1 if index < remainder else 0))
                option.setdefault("risk", "unknown")
        return decision


class _Specialist:
    adapter_name = "opencode"

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.calls = []

    @property
    def provider_id(self):
        return "specialist"

    @property
    def availability(self):
        return Availability.AVAILABLE

    @property
    def capabilities(self):
        return {AgentCapability.IMPLEMENT, AgentCapability.REVIEW}

    def execute(self, handoff):
        self.calls.append(handoff)
        if handoff.stage.startswith("supervisor_delegate"):
            (self.repo / "delegated.txt").write_text("done\n", encoding="utf-8")
            return {"ok": True, "status": "implemented", "summary": "delegated fix"}
        return {"ok": True, "verdict": "approved", "findings": [], "summary": "approved"}


class _Completed:
    returncode = 0
    stdout = "ok"
    stderr = ""


def _passing_runner(command: str, *, cwd: Path, timeout: int):
    return _Completed()


def _orchestrator(tmp_path: Path, repo: Path, *, policy: SupervisorPolicy | None = None):
    config = OrchestrationConfig(state_dir=tmp_path / "state")
    config.supervisor_policy = policy or SupervisorPolicy(agent_id="codex")
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
        task_dossier_dir=tmp_path / "project" / "tasks" / "task",
    )
    graph, report = normalize_work_packages(
        [
            WorkPackage(
                id="wp1",
                title="Blocked package",
                requirements=["Recover the blocked work"],
                acceptance_criteria=[AcceptanceCriterion(id="done", description="Done")],
                affected_repositories=["repo"],
                write_scope=["repo"],
            )
        ]
    )
    orch.initialize_graph(graph, report)
    orch.transition_to(TaskExecutionState.RUNNING)
    package = orch._state_record.plan_graph.package_by_id("wp1")
    package.stage = WorkPackageStage.FINAL_REVIEW
    package.status = "running"
    orch.save_state()
    orch._journal.append(
        "human_intervention_required",
        {
            "package_id": "wp1",
            "stage": "final_review",
            "reason": "review/fix cycle budget exhausted",
            "evidence": ["HANDOFF.md evidence does not match the workspace"],
        },
    )
    orch.transition_to(TaskExecutionState.HUMAN_REQUIRED)
    return orch, package


def _resolved(**extra):
    return {
        "ok": True,
        "decision": "resolved",
        "classification": "review_exhausted",
        "summary": "Corrected the durable evidence and implementation summary.",
        "actions_taken": ["inspected PLAN", "repaired evidence"],
        "acceptance_evidence": [
            {
                "criterion_id": "done",
                "evidence": "RECOVERY.md and passing fixture-check",
            }
        ],
        "resume_stage": "",
        "implementation_summary": "",
        "retain_paths": [],
        "discard_paths": [],
        "delegations": [],
        "human_question": {
            "question": "",
            "context": "",
            "recommended_option": "",
            "options": [],
        },
        **extra,
    }


def test_supervisor_policy_defaults_enabled_and_validates_bounds():
    policy = SupervisorPolicy.from_mapping(None)
    assert policy.enabled is True
    assert policy.skill_id == "ai-supervise"
    assert policy.auto_decision.enabled is False
    with pytest.raises(SupervisorConfigError):
        SupervisorPolicy.from_mapping({"max_attempts_per_incident": 0})
    with pytest.raises(SupervisorConfigError):
        SupervisorPolicy.from_mapping({"trigger_classes": ["not-real"]})
    pool = SupervisorPolicy.from_mapping({"agents": ["codex", "claude-code"]})
    assert pool.agent_id == "codex"
    assert pool.agent_ids == ("codex", "claude-code")
    with pytest.raises(SupervisorConfigError):
        SupervisorPolicy.from_mapping(
            {"agent": "codex", "agents": ["codex", "claude-code"]}
        )
    with pytest.raises(SupervisorConfigError):
        SupervisorPolicy.from_mapping({"agents": ["codex", "codex"]})
    weighted = SupervisorPolicy.from_mapping(
        {
            "auto_decision": {
                "enabled": True,
                "minimum_weight": 60,
                "minimum_margin": 15,
                "max_per_incident": 2,
                "allowed_classes": ["commit_transaction"],
            }
        }
    )
    assert weighted.auto_decision.enabled is True
    assert weighted.auto_decision.minimum_weight == 60
    assert weighted.auto_decision.allowed_classes == {
        IncidentClass.COMMIT_TRANSACTION
    }
    with pytest.raises(SupervisorConfigError):
        SupervisorPolicy.from_mapping(
            {"auto_decision": {"minimum_weight": 101}}
        )


def test_weighted_auto_decision_requires_unique_routine_winner():
    question = HumanDecisionRequest.from_mapping(
        {
            "question": "How should the clean-start conflict be resolved?",
            "context": "The approved recovery is still uncommitted.",
            "recommended_option": "commit",
            "options": [
                {
                    "id": "commit",
                    "label": "Commit the approved recovery",
                    "consequence": "Resume from a clean workspace.",
                    "weight": 75,
                    "risk": "routine",
                },
                {
                    "id": "discard",
                    "label": "Discard the recovery",
                    "consequence": "Lose the approved fixes.",
                    "weight": 25,
                    "risk": "destructive",
                },
            ],
        }
    )
    evaluation = evaluate_supervisor_auto_decision(
        question,
        classification=IncidentClass.COMMIT_TRANSACTION,
        policy=SupervisorAutoDecisionPolicy(
            enabled=True,
            minimum_weight=60,
            minimum_margin=15,
        ),
        decisions_taken=0,
        require_human_for_destructive_actions=True,
    )

    assert evaluation.eligible is True
    assert evaluation.option is not None
    assert evaluation.option.id == "commit"
    assert evaluation.margin == 50


def test_weighted_auto_decision_keeps_product_and_destructive_choices_human_gated():
    product_question = HumanDecisionRequest.from_mapping(
        {
            "question": "Should compatibility remain?",
            "context": "The PLAN is ambiguous.",
            "recommended_option": "keep",
            "options": [
                {
                    "id": "keep",
                    "label": "Keep compatibility",
                    "consequence": "Preserve the current behavior.",
                    "weight": 90,
                    "risk": "product",
                },
                {
                    "id": "remove",
                    "label": "Remove compatibility",
                    "consequence": "Change the rollout contract.",
                    "weight": 10,
                    "risk": "product",
                },
            ],
        }
    )
    policy = SupervisorAutoDecisionPolicy(
        enabled=True,
        minimum_weight=51,
        minimum_margin=0,
        allowed_classes=frozenset({IncidentClass.REQUIREMENT_AMBIGUITY}),
    )
    product = evaluate_supervisor_auto_decision(
        product_question,
        classification=IncidentClass.REQUIREMENT_AMBIGUITY,
        policy=policy,
        decisions_taken=0,
        require_human_for_destructive_actions=True,
    )
    assert product.eligible is False
    assert "not 'routine'" in product.reason

    destructive_question = HumanDecisionRequest.from_mapping(
        {
            "question": "Which workspace recovery should run?",
            "context": "One path loses approved work.",
            "recommended_option": "discard",
            "options": [
                {
                    "id": "discard",
                    "label": "Discard all recovery files",
                    "consequence": "Restore the baseline and lose the fixes.",
                    "weight": 90,
                    "risk": "routine",
                },
                {
                    "id": "commit",
                    "label": "Commit the recovery",
                    "consequence": "Preserve approved work.",
                    "weight": 10,
                    "risk": "routine",
                },
            ],
        }
    )
    destructive = evaluate_supervisor_auto_decision(
        destructive_question,
        classification=IncidentClass.COMMIT_TRANSACTION,
        policy=SupervisorAutoDecisionPolicy(
            enabled=True,
            minimum_weight=51,
            minimum_margin=0,
        ),
        decisions_taken=0,
        require_human_for_destructive_actions=True,
    )
    assert destructive.eligible is False
    assert "destructive-action language" in destructive.reason


def test_incident_classification_prefers_review_budget_over_evidence_wording():
    assert classify_incident(
        {
            "stage": "final_review",
            "reason": "review/fix cycle budget exhausted",
            "evidence": ["evidence mismatch"],
        }
    ) == IncidentClass.REVIEW_EXHAUSTED


def test_incident_escalation_key_ignores_journal_instance_metadata():
    first = {
        "sequence": 10,
        "timestamp": "2026-08-25T10:00:00+00:00",
        "package_id": "WP22__WP22-S4",
        "stage": "final_review",
        "reason": "review/fix cycle budget exhausted",
        "evidence": ["  same   failure  ", "second item"],
    }
    replay = {
        **first,
        "sequence": 42,
        "timestamp": "2026-08-25T14:00:00+00:00",
        "evidence": ["second item", "same failure"],
    }
    different = {
        **replay,
        "evidence": ["different failure"],
    }

    assert incident_fingerprint(first) != incident_fingerprint(replay)
    assert incident_escalation_key(first) == incident_escalation_key(replay)
    assert incident_escalation_key(first) != incident_escalation_key(different)


def test_incident_store_reuses_semantic_campaign_across_new_event_fingerprint(tmp_path):
    store = SupervisorIncidentStore(tmp_path / "incidents.json")
    incident = store.open_or_reuse(
        fingerprint="event-1",
        escalation_key="campaign",
        package_id="wp1",
        stage="review",
        classification=IncidentClass.REVIEW_EXHAUSTED,
        escalation_sequence=1,
        supervisor_agent_id="codex",
        workspace_digest="before",
    )
    incident.attempts = 2
    store.save(incident)

    reopened = store.open_or_reuse(
        fingerprint="event-2",
        escalation_key="campaign",
        package_id="wp1",
        stage="review",
        classification=IncidentClass.REVIEW_EXHAUSTED,
        escalation_sequence=2,
        supervisor_agent_id="codex",
        workspace_digest="after",
    )

    assert reopened.incident_id == incident.incident_id
    assert reopened.attempts == 2
    assert reopened.fingerprint == "event-1"
    assert reopened.escalation_key == "campaign"


def test_incident_store_reuses_active_fingerprint_without_resetting_attempt_budget(tmp_path):
    store = SupervisorIncidentStore(tmp_path / "incidents.json")
    incident = store.open_or_reuse(
        fingerprint="same",
        package_id="wp1",
        stage="review",
        classification=IncidentClass.REVIEW_EXHAUSTED,
        escalation_sequence=1,
        supervisor_agent_id="codex",
        workspace_digest="before",
    )
    incident.attempts = 2
    store.save(incident)
    reopened = store.open_or_reuse(
        fingerprint="same",
        package_id="wp1",
        stage="review",
        classification=IncidentClass.REVIEW_EXHAUSTED,
        escalation_sequence=1,
        supervisor_agent_id="codex",
        workspace_digest="after",
    )
    assert reopened.incident_id == incident.incident_id
    assert reopened.attempts == 2


def test_exhausted_supervisor_campaign_is_terminal_across_reemitted_escalation(tmp_path):
    repo = _repo(tmp_path / "repo")
    orch, package = _orchestrator(tmp_path, repo)
    supervisor = _Supervisor([_resolved()])
    orch.register_agent(supervisor)

    action = orch._supervisor_action()
    incident = orch._supervisor_incidents.open_or_reuse(
        fingerprint=incident_fingerprint(action),
        package_id=package.id,
        stage=package.stage.value,
        classification=classify_incident(action),
        escalation_sequence=int(action["sequence"]),
        supervisor_agent_id=supervisor.provider_id,
        workspace_digest=orch._workspace_digest(),
    )
    incident.attempts = orch.config.supervisor_policy.max_attempts_per_incident
    incident.summary = "bounded supervisor budget exhausted"
    incident.touch(status=IncidentStatus.EXHAUSTED)
    orch._supervisor_incidents.save(incident)

    # Re-emit the same durable condition as a new journal event, which is what
    # restart/reconciliation paths can do. The exact audit fingerprint changes;
    # the semantic retry-budget identity must not.
    orch._journal.append(
        "human_intervention_required",
        {
            "package_id": package.id,
            "stage": "final_review",
            "reason": "review/fix cycle budget exhausted",
            "evidence": ["HANDOFF.md evidence does not match the workspace"],
        },
    )
    replay = orch._supervisor_action()
    assert incident_fingerprint(replay) != incident.fingerprint
    assert incident_escalation_key(replay) == incident_escalation_key(action)

    assert orch.can_supervise_human_required() is False
    # Eligibility/status reads must remain side-effect free even while they can
    # reconstruct a legacy semantic key from the journal.
    assert orch._supervisor_incidents.get(incident.incident_id).escalation_key == ""
    before = orch._supervisor_incidents.list()
    assert orch._run_supervision_unlocked() is False
    after = orch._supervisor_incidents.list()
    assert [item.incident_id for item in after] == [item.incident_id for item in before]
    assert supervisor.calls == []
    assert orch.state == TaskExecutionState.HUMAN_REQUIRED
    report = orch.supervisor_status_report()["incident"]
    assert report["status"] == "exhausted"
    assert report["escalation_key"] == incident_escalation_key(action)


def test_legacy_duplicate_active_incident_with_new_sequence_is_reconciled(tmp_path):
    repo = _repo(tmp_path / "repo")
    orch, package = _orchestrator(tmp_path, repo)
    supervisor = _Supervisor([_resolved()])
    orch.register_agent(supervisor)

    original_action = orch._supervisor_action()
    exhausted = orch._supervisor_incidents.open_or_reuse(
        fingerprint=incident_fingerprint(original_action),
        package_id=package.id,
        stage=package.stage.value,
        classification=classify_incident(original_action),
        escalation_sequence=int(original_action["sequence"]),
        supervisor_agent_id=supervisor.provider_id,
        workspace_digest=orch._workspace_digest(),
    )
    exhausted.attempts = orch.config.supervisor_policy.max_attempts_per_incident
    exhausted.touch(status=IncidentStatus.EXHAUSTED)
    orch._supervisor_incidents.save(exhausted)

    # Reproduce the real pre-fix state: a semantically identical escalation was
    # journaled again and the old code opened a fresh 1/3 incident because the
    # exact sequence-sensitive fingerprint had changed. Both records deliberately
    # omit escalation_key to exercise migration of already-persisted installations.
    orch._journal.append(
        "human_intervention_required",
        {
            "package_id": package.id,
            "stage": "final_review",
            "reason": "review/fix cycle budget exhausted",
            "evidence": ["HANDOFF.md evidence does not match the workspace"],
        },
    )
    replay = orch._supervisor_action()
    assert incident_fingerprint(replay) != exhausted.fingerprint
    duplicate = orch._supervisor_incidents.open_or_reuse(
        fingerprint=incident_fingerprint(replay),
        package_id=package.id,
        stage=package.stage.value,
        classification=classify_incident(replay),
        escalation_sequence=int(replay["sequence"]),
        supervisor_agent_id=supervisor.provider_id,
        workspace_digest=orch._workspace_digest(),
    )
    duplicate.attempts = 2
    orch._supervisor_incidents.save(duplicate)
    orch._state_record.agent_waits = {
        package.id: {
            "package_id": package.id,
            "stage": package.stage.value,
            "capability": "supervise",
            "cycle": 1,
        }
    }
    orch._state_record.waiting = dict(orch._state_record.agent_waits[package.id])
    orch.transition_to(TaskExecutionState.SUPERVISING)
    orch.transition_to(TaskExecutionState.WAITING_FOR_AGENT)

    assert orch._run_supervision_unlocked() is False
    assert orch.state == TaskExecutionState.HUMAN_REQUIRED
    assert orch._state_record.agent_waits == {}
    assert orch._supervisor_incidents.active() is None
    repaired = orch._supervisor_incidents.get(duplicate.incident_id)
    migrated_exhausted = orch._supervisor_incidents.get(exhausted.incident_id)
    assert repaired is not None
    assert migrated_exhausted is not None
    assert repaired.status == IncidentStatus.EXHAUSTED
    assert migrated_exhausted.escalation_key == incident_escalation_key(original_action)
    assert "Legacy duplicate Supervisor incident suppressed" in repaired.summary
    assert any(
        entry.event_type == "supervisor_duplicate_incident_reconciled"
        for entry in orch._journal.read()
    )
    assert supervisor.calls == []


def test_new_human_escalation_after_exhaustion_gets_a_new_supervision_budget(tmp_path):
    repo = _repo(tmp_path / "repo")
    orch, package = _orchestrator(tmp_path, repo)
    supervisor = _Supervisor([_resolved()])
    orch.register_agent(supervisor)

    old_action = orch._supervisor_action()
    old_incident = orch._supervisor_incidents.open_or_reuse(
        fingerprint=incident_fingerprint(old_action),
        package_id=package.id,
        stage=package.stage.value,
        classification=classify_incident(old_action),
        escalation_sequence=int(old_action["sequence"]),
        supervisor_agent_id=supervisor.provider_id,
        workspace_digest=orch._workspace_digest(),
    )
    old_incident.attempts = orch.config.supervisor_policy.max_attempts_per_incident
    old_incident.touch(status=IncidentStatus.EXHAUSTED)
    orch._supervisor_incidents.save(old_incident)
    assert orch.can_supervise_human_required() is False

    orch._journal.append(
        "human_intervention_required",
        {
            "package_id": package.id,
            "stage": package.stage.value,
            "reason": "review/fix cycle budget exhausted after operator repair",
            "evidence": ["fresh escalation after an explicit operator change"],
        },
    )

    new_action = orch._supervisor_action()
    assert incident_fingerprint(new_action) != old_incident.fingerprint
    assert incident_escalation_key(new_action) != incident_escalation_key(old_action)
    assert orch.can_supervise_human_required() is True


def test_resolved_supervisor_infers_remaining_candidate_paths_as_retained(tmp_path):
    repo = _repo(tmp_path / "repo")
    foreign = _repo(tmp_path / "foreign")
    orch, package = _orchestrator(tmp_path, repo)
    orch._repository_paths["foreign"] = foreign

    class _MinimalSupervisor(_Supervisor):
        def execute(self, handoff):
            self.calls.append(handoff)
            (foreign / "unclassified.txt").write_text("recovered\n", encoding="utf-8")
            # The permissive contract no longer requires the model to enumerate
            # every still-dirty path. The compiler infers current incident
            # candidates as retained; protected-path policy remains downstream.
            return {"decision": "resolved", "summary": "Recovery is complete."}

    supervisor = _MinimalSupervisor([])
    orch.register_agent(supervisor)

    assert orch._run_supervision_unlocked() is True

    assert len(supervisor.calls) == 1
    assert orch.state == TaskExecutionState.RUNNING
    assert package.stage == WorkPackageStage.REGRESSION_VERIFY
    assert "foreign" in package.affected_repositories
    assert "foreign/unclassified.txt" in package.write_scope
    decision_event = next(
        entry for entry in orch._journal.read() if entry.event_type == "supervisor_decision"
    )
    assert any("inferred as retained" in item for item in decision_event.payload["normalization_warnings"])


def test_supervisor_repairs_human_required_and_returns_to_verified_pipeline(tmp_path):
    repo = _repo(tmp_path / "repo")
    orch, package = _orchestrator(tmp_path, repo)
    supervisor = _Supervisor([_resolved(_modify=True)], repo=repo)
    reviewer = _Specialist(repo)
    orch.register_agent(supervisor)
    orch.register_agent(reviewer)

    orch.run_pipeline()

    assert orch.state == TaskExecutionState.COMPLETED
    assert supervisor.calls[0].additional_writable_roots == [str(tmp_path / "project")]
    incident = orch.supervisor_status_report()["incident"]
    assert incident == {}  # resolved incidents are retained in history, not active
    stored = SupervisorIncidentStore(
        tmp_path / "state" / "projects" / "task" / "supervisor-incidents.json"
    ).list()[-1]
    assert stored.status == IncidentStatus.RESOLVED
    assert stored.actions_taken == ["inspected PLAN", "repaired evidence"]
    assert package.last_fixer_id == "codex"
    assert _git(repo, "status", "--porcelain") == ""
    assert _git(repo, "log", "-1", "--pretty=%s").startswith("execraft(wp1)")


def test_supervisor_delegates_bounded_skill_work_then_resolves(tmp_path):
    repo = _repo(tmp_path / "repo")
    orch, package = _orchestrator(tmp_path, repo)
    supervisor = _Supervisor(
        [
            {
                "ok": True,
                "decision": "delegate",
                "classification": "evidence_mismatch",
                "summary": "Need one specialist repair.",
                "actions_taken": ["diagnosed mismatch"],
                "delegations": [
                    {
                        "task": "Repair the durable handoff evidence.",
                        "capability": "implement",
                        "agent_id": "specialist",
                        "skills": ["ai-implement"],
                        "read_only": False,
                    }
                ],
            },
            _resolved(),
        ]
    )
    specialist = _Specialist(repo)
    orch.register_agent(supervisor)
    orch.register_agent(specialist)

    assert orch._run_supervision_unlocked() is True

    assert orch.state == TaskExecutionState.RUNNING
    assert package.stage == WorkPackageStage.REGRESSION_VERIFY
    assert len(specialist.calls) == 1
    stored = SupervisorIncidentStore(
        tmp_path / "state" / "projects" / "task" / "supervisor-incidents.json"
    ).list()[-1]
    assert stored.delegated_tasks == 1
    assert stored.status == IncidentStatus.RESOLVED


def test_supervisor_resumes_pending_delegation_after_provider_failover(tmp_path):
    repo = _repo(tmp_path / "repo")
    orch, package = _orchestrator(tmp_path, repo)
    orch.config.max_agent_attempts_per_stage = 1
    orch.config.agent_retry_initial_seconds = 0.01
    orch.config.agent_retry_max_seconds = 0.01

    supervisor = _Supervisor(
        [
            {
                "ok": True,
                "decision": "delegate",
                "classification": "evidence_mismatch",
                "summary": "Delegate one bounded repair.",
                "actions_taken": ["diagnosed mismatch"],
                "delegations": [
                    {
                        "task": "Repair the evidence and return a structured result.",
                        "capability": "fix_review",
                        "skills": ["ai-fix-review"],
                        "read_only": False,
                    }
                ],
            },
            _resolved(),
        ]
    )

    class _NoisyBrokenFixer:
        adapter_name = "opencode"

        @property
        def provider_id(self):
            return "opencode-zen-free"

        @property
        def availability(self):
            return Availability.AVAILABLE

        @property
        def capabilities(self):
            return {AgentCapability.FIX_REVIEW}

        def execute(self, _handoff):
            raise AgentExecutionError(
                "invalid structured output",
                classification="invalid_output",
            )

    class _HealthyFixer:
        adapter_name = "opencode"

        def __init__(self):
            self.calls = 0

        @property
        def provider_id(self):
            return "local-fixer"

        @property
        def availability(self):
            return Availability.AVAILABLE

        @property
        def capabilities(self):
            return {AgentCapability.FIX_REVIEW}

        def execute(self, _handoff):
            self.calls += 1
            (repo / "delegated.txt").write_text("fixed\n", encoding="utf-8")
            return {"ok": True, "status": "fixed", "summary": "fixed locally"}

    broken = _NoisyBrokenFixer()
    healthy = _HealthyFixer()
    orch.register_agent(supervisor)
    orch.register_agent(broken)
    orch.register_agent(healthy)

    assert orch._run_supervision_unlocked() is True
    assert orch.state == TaskExecutionState.WAITING_FOR_AGENT
    stored = SupervisorIncidentStore(
        tmp_path / "state" / "projects" / "task" / "supervisor-incidents.json"
    ).active()
    assert stored is not None
    assert stored.status == IncidentStatus.DELEGATING
    assert stored.pending_delegation_index == 0
    assert len(stored.pending_delegations) == 1
    assert len(supervisor.calls) == 1

    # The next driver invocation resumes the durable delegation. It must not
    # ask the Supervisor to plan again before the fallback provider completes.
    assert orch._run_supervision_unlocked() is True

    assert healthy.calls == 1
    assert len(supervisor.calls) == 2
    assert orch.state == TaskExecutionState.RUNNING
    assert package.stage == WorkPackageStage.REGRESSION_VERIFY
    completed = SupervisorIncidentStore(
        tmp_path / "state" / "projects" / "task" / "supervisor-incidents.json"
    ).list()[-1]
    assert completed.pending_delegations == []
    assert completed.pending_delegation_index == 0
    assert completed.delegated_tasks == 1
    assert completed.status == IncidentStatus.RESOLVED


def test_supervisor_asks_plain_language_question_and_resumes_from_answer(tmp_path):
    repo = _repo(tmp_path / "repo")
    orch, package = _orchestrator(tmp_path, repo)
    supervisor = _Supervisor(
        [
            {
                "ok": True,
                "decision": "ask_human",
                "classification": "requirement_ambiguity",
                "summary": "The PLAN does not decide compatibility lifetime.",
                "actions_taken": ["compared PLAN and implementation"],
                "human_question": {
                    "question": "Should the compatibility route remain until WP17-S5?",
                    "context": "Removing it now changes the rollout contract.",
                    "recommended_option": "keep",
                    "options": [
                        {"id": "keep", "label": "Keep it until WP17-S5", "consequence": "Only evidence is corrected."},
                        {"id": "remove", "label": "Remove it now", "consequence": "The Supervisor changes code and tests."},
                    ],
                },
            },
            _resolved(),
        ]
    )
    orch.register_agent(supervisor)

    assert orch._run_supervision_unlocked() is True
    assert orch.state == TaskExecutionState.WAITING_FOR_HUMAN_DECISION
    status = orch.supervisor_status_report()
    assert status["incident"]["human_question"]["recommended_option"] == "keep"

    orch.submit_supervisor_answer("keep", message="Keep the staged migration plan.")
    assert orch.state == TaskExecutionState.SUPERVISING
    assert orch._run_supervision_unlocked() is True
    assert orch.state == TaskExecutionState.RUNNING
    assert package.stage == WorkPackageStage.REGRESSION_VERIFY
    assert json.loads(
        (tmp_path / "state" / "projects" / "task" / "supervisor-incidents.json").read_text()
    )[-1]["human_answer"]["option_id"] == "keep"


def test_supervisor_answer_retires_pre_round_budgets(tmp_path):
    repo = _repo(tmp_path / "repo")
    policy = SupervisorPolicy(agent_id="codex")
    orch, package = _orchestrator(tmp_path, repo, policy=policy)
    supervisor = _Supervisor(
        [
            {
                "ok": True,
                "decision": "ask_human",
                "classification": "requirement_ambiguity",
                "summary": "The Supervisor exhausted its budget.",
                "actions_taken": ["inspected the workspace"],
                "human_question": {
                    "question": "How should it proceed?",
                    "context": "Supervisor provider-wait budget exhausted.",
                    "recommended_option": "inspect_and_retry",
                    "options": [
                        {"id": "inspect_and_retry", "label": "Inspect again", "consequence": "A fresh bounded attempt runs."},
                        {"id": "leave_paused", "label": "Leave it paused", "consequence": "The task stays paused."},
                    ],
                },
            },
            _resolved(),
        ]
    )
    orch.register_agent(supervisor)

    assert orch._run_supervision_unlocked() is True
    assert orch.state == TaskExecutionState.WAITING_FOR_HUMAN_DECISION

    store = SupervisorIncidentStore(
        tmp_path / "state" / "projects" / "task" / "supervisor-incidents.json"
    )
    # An incident exhausted by the provider-wait gate carries the counters that
    # are checked before any supervision round runs.  The operator answer must
    # retire them, or the resume re-exhausts at attempts=0 and re-asks forever.
    incident = store.list()[-1]
    incident.provider_waits = policy.max_provider_waits
    incident.contract_failures = policy.max_contract_failures
    store.save(incident)

    orch.submit_supervisor_answer("inspect_and_retry", message="Resume and commit.")
    resumed = store.list()[-1]
    assert resumed.provider_waits == 0
    assert resumed.contract_failures == 0
    assert resumed.attempts == 0

    assert orch._run_supervision_unlocked() is True
    assert orch.state == TaskExecutionState.RUNNING
    assert package.stage == WorkPackageStage.REGRESSION_VERIFY


def test_supervisor_auto_selects_unique_weighted_routine_option(tmp_path):
    repo = _repo(tmp_path / "repo")
    policy = SupervisorPolicy(
        agent_id="codex",
        auto_decision=SupervisorAutoDecisionPolicy(
            enabled=True,
            minimum_weight=60,
            minimum_margin=15,
            max_per_incident=2,
        ),
    )
    orch, package = _orchestrator(tmp_path, repo, policy=policy)
    supervisor = _Supervisor(
        [
            {
                "ok": True,
                "decision": "ask_human",
                "classification": "review_exhausted",
                "summary": "Approved recovery is present but not committed.",
                "actions_taken": ["verified all recovery paths"],
                "human_question": {
                    "question": "How should the approved recovery be handled?",
                    "context": "Committing preserves reviewed work and restores a clean start.",
                    "recommended_option": "commit",
                    "options": [
                        {
                            "id": "commit",
                            "label": "Commit the approved recovery",
                            "consequence": "Preserve the reviewed fixes and resume.",
                            "weight": 80,
                            "risk": "routine",
                        },
                        {
                            "id": "discard",
                            "label": "Discard the approved recovery",
                            "consequence": "Lose the reviewed fixes.",
                            "weight": 20,
                            "risk": "destructive",
                        },
                    ],
                },
            },
            _resolved(),
        ]
    )
    orch.register_agent(supervisor)

    assert orch._run_supervision_unlocked() is True

    assert orch.state == TaskExecutionState.RUNNING
    assert package.stage == WorkPackageStage.REGRESSION_VERIFY
    assert len(supervisor.calls) == 2
    stored = SupervisorIncidentStore(
        tmp_path / "state" / "projects" / "task" / "supervisor-incidents.json"
    ).list()[-1]
    assert stored.auto_decisions == 1
    assert stored.human_answer["option_id"] == "commit"
    assert stored.human_answer["source"] == "automatic"
    assert any(
        entry.event_type == "supervisor_human_decision_auto_selected"
        for entry in orch._journal.read()
    )


def test_supervisor_auto_decision_does_not_bypass_requirement_ambiguity(tmp_path):
    repo = _repo(tmp_path / "repo")
    policy = SupervisorPolicy(
        agent_id="codex",
        auto_decision=SupervisorAutoDecisionPolicy(
            enabled=True,
            minimum_weight=51,
            minimum_margin=0,
        ),
    )
    orch, _ = _orchestrator(tmp_path, repo, policy=policy)
    supervisor = _Supervisor(
        [
            {
                "ok": True,
                "decision": "ask_human",
                "classification": "requirement_ambiguity",
                "summary": "Product intent is not recorded.",
                "actions_taken": ["compared PLAN and code"],
                "human_question": {
                    "question": "Should compatibility remain?",
                    "context": "The rollout contract is ambiguous.",
                    "recommended_option": "keep",
                    "options": [
                        {
                            "id": "keep",
                            "label": "Keep compatibility",
                            "consequence": "Preserve current behavior.",
                            "weight": 90,
                            "risk": "product",
                        },
                        {
                            "id": "remove",
                            "label": "Remove compatibility",
                            "consequence": "Change product behavior.",
                            "weight": 10,
                            "risk": "product",
                        },
                    ],
                },
            }
        ]
    )
    orch.register_agent(supervisor)

    assert orch._run_supervision_unlocked() is True

    assert orch.state == TaskExecutionState.WAITING_FOR_HUMAN_DECISION
    stored = SupervisorIncidentStore(
        tmp_path / "state" / "projects" / "task" / "supervisor-incidents.json"
    ).list()[-1]
    assert stored.auto_decisions == 0
    assert stored.human_answer == {}
    assert stored.human_question["recommended_option"] == "keep"


def test_non_codex_or_claude_provider_cannot_be_supervisor(tmp_path):
    repo = _repo(tmp_path / "repo")
    orch, _ = _orchestrator(tmp_path, repo, policy=SupervisorPolicy(agent_id="specialist"))

    class _UnsupportedSupervisor(_Specialist):
        @property
        def capabilities(self):
            return {AgentCapability.SUPERVISE}

    specialist = _UnsupportedSupervisor(repo)
    orch.register_agent(specialist)
    assert orch.supervisor_status_report()["available"] is False
    assert orch.can_supervise_human_required() is False


def test_claude_provider_can_be_selected_as_supervisor(tmp_path):
    repo = _repo(tmp_path / "repo")
    orch, _ = _orchestrator(
        tmp_path,
        repo,
        policy=SupervisorPolicy(agent_id="claude-code"),
    )

    class _ClaudeSupervisor(_Supervisor):
        adapter_name = "claude-code"

        @property
        def provider_id(self):
            return "claude-code"

    supervisor = _ClaudeSupervisor([_resolved()])
    orch.register_agent(supervisor)

    assert orch.supervisor_status_report()["available"] is True
    assert orch.can_supervise_human_required() is True


def test_supervisor_pool_fails_over_from_codex_to_claude_in_one_round(tmp_path):
    repo = _repo(tmp_path / "repo")
    policy = SupervisorPolicy(agent_ids=("codex", "claude-code"))
    orch, package = _orchestrator(tmp_path, repo, policy=policy)

    class _BrokenCodex(_Supervisor):
        def execute(self, handoff):
            self.calls.append(handoff)
            raise AgentExecutionError(
                "Codex rejected its response schema",
                classification="invalid_output",
            )

    class _ClaudeSupervisor(_Supervisor):
        adapter_name = "claude-code"

        @property
        def provider_id(self):
            return "claude-code"

    codex = _BrokenCodex([])
    claude = _ClaudeSupervisor([_resolved()])
    orch.register_agent(codex)
    orch.register_agent(claude)

    assert orch._run_supervision_unlocked() is True

    assert orch.state == TaskExecutionState.RUNNING
    assert package.stage == WorkPackageStage.REGRESSION_VERIFY
    # Supervisor handoffs are not provider-schema-gated, so a provider-level
    # invalid_output failure fails over immediately instead of paying for a
    # second format-only reasoning turn.
    assert len(codex.calls) == 1
    assert len(claude.calls) == 1
    stored = SupervisorIncidentStore(
        tmp_path / "state" / "projects" / "task" / "supervisor-incidents.json"
    ).list()[-1]
    assert stored.supervisor_agent_id == "claude-code"
    failover = next(
        entry
        for entry in orch._journal.read()
        if entry.event_type == "agent_failover"
    )
    assert failover.payload["from_agent"] == "codex"
    assert failover.payload["to_agent"] == "claude-code"


def test_supervisor_provider_wait_does_not_consume_reasoning_attempt(tmp_path):
    repo = _repo(tmp_path / "repo")
    policy = SupervisorPolicy(
        agent_id="codex",
        max_attempts_per_incident=1,
    )
    orch, _ = _orchestrator(tmp_path, repo, policy=policy)
    orch.config.max_agent_attempts_per_stage = 1

    class _BrokenCodex(_Supervisor):
        def execute(self, handoff):
            self.calls.append(handoff)
            raise AgentExecutionError(
                "temporary structured-output transport failure",
                classification="invalid_output",
            )

    codex = _BrokenCodex([])
    orch.register_agent(codex)

    assert orch._run_supervision_unlocked() is True

    assert orch.state == TaskExecutionState.WAITING_FOR_AGENT
    incident = SupervisorIncidentStore(
        tmp_path / "state" / "projects" / "task" / "supervisor-incidents.json"
    ).active()
    assert incident is not None
    assert incident.attempts == 0
    assert any(
        entry.event_type == "supervisor_attempt_deferred"
        for entry in orch._journal.read()
    )


def test_supervisor_reasoning_completion_is_separate_from_mutation_validation(tmp_path):
    repo = _repo(tmp_path / "repo")
    orch, package = _orchestrator(tmp_path, repo)
    supervisor = _Supervisor(
        [_resolved(retain_paths=["repo:file.txt"], discard_paths=["repo:file.txt"])]
    )
    orch.register_agent(supervisor)

    assert orch._run_supervision_unlocked() is True

    # The compiler repairs the representational overlap (retain wins), so the
    # expensive provider invocation completes normally. The strict workspace
    # reconciler then rejects repo:file.txt because it is not an incident
    # candidate. This is an orchestration-effect failure, not provider health.
    history = orch.invocation_history(package_id=package.id)
    assert history[-1]["status"] == "completed"
    assert not any(
        (item.get("failure") or {}).get("classification") == "invalid_output"
        for item in history
    )
    assert any(
        entry.event_type == "supervisor_attempt_failed"
        for entry in orch._journal.read()
    )
    assert orch.state == TaskExecutionState.SUPERVISING
    assert any(
        entry.event_type == "supervisor_retry_scheduled"
        for entry in orch._journal.read()
    )


def test_supervisor_output_schema_is_only_a_permissive_reference_envelope():
    schema = supervisor_output_schema()

    assert schema["type"] == "object"
    assert schema["additionalProperties"] is True
    assert schema["required"] == ["decision", "summary"]
    assert set(schema["properties"]) == {"decision", "summary"}
    assert schema["properties"]["decision"]["type"] == "string"



def test_incident_store_supersedes_an_unresolved_older_escalation(tmp_path):
    store = SupervisorIncidentStore(tmp_path / "incidents.json")
    first = store.open_or_reuse(
        fingerprint="first",
        package_id="wp1",
        stage="review",
        classification=IncidentClass.REVIEW_EXHAUSTED,
        escalation_sequence=1,
        supervisor_agent_id="codex",
        workspace_digest="before",
    )

    second = store.open_or_reuse(
        fingerprint="second",
        package_id="wp1",
        stage="review",
        classification=IncidentClass.EVIDENCE_MISMATCH,
        escalation_sequence=2,
        supervisor_agent_id="codex",
        workspace_digest="after",
    )

    records = store.list()
    assert records[0].incident_id == first.incident_id
    assert records[0].status == IncidentStatus.EXHAUSTED
    assert store.active().incident_id == second.incident_id


def test_supervisor_decision_requires_exact_qualified_paths_and_read_only_reviews():
    with pytest.raises(ValueError, match="repository-qualified"):
        SupervisorDecision.from_mapping(
            _resolved(retain_paths=["not-qualified"])
        )
    with pytest.raises(ValueError, match="both retain and discard"):
        SupervisorDecision.from_mapping(
            _resolved(retain_paths=["repo:file"], discard_paths=["repo:file"])
        )
    with pytest.raises(ValueError, match="review delegations must be read_only"):
        SupervisorDelegation.from_mapping(
            {
                "task": "Review the repair",
                "capability": "review",
                "skills": ["ai-review"],
                "read_only": False,
            }
        )
    with pytest.raises(ValueError, match="option id 'stop' is reserved"):
        SupervisorDecision.from_mapping(
            {
                "ok": True,
                "decision": "ask_human",
                "classification": "requirement_ambiguity",
                "summary": "Need operator intent.",
                "actions_taken": [],
                "human_question": {
                    "question": "Continue?",
                    "context": "The reserved stop action is orchestrator-owned.",
                    "recommended_option": "continue",
                    "options": [
                        {
                            "id": "continue",
                            "label": "Continue",
                            "consequence": "Recovery continues.",
                        },
                        {
                            "id": "stop",
                            "label": "Continue differently",
                            "consequence": "This ambiguous reserved ID is rejected.",
                        },
                    ],
                },
            }
        )


def test_supervisor_acquires_exact_cross_repository_paths_and_updates_summary(tmp_path):
    repo = _repo(tmp_path / "repo")
    foreign = _repo(tmp_path / "foreign")
    orch, package = _orchestrator(tmp_path, repo)
    orch._repository_paths["foreign"] = foreign

    class _CrossRepositorySupervisor(_Supervisor):
        def execute(self, handoff):
            self.calls.append(handoff)
            (foreign / "support.txt").write_text("support\n", encoding="utf-8")
            return _resolved(
                retain_paths=["foreign:support.txt"],
                implementation_summary=(
                    "Repaired the evidence and retained the required support file."
                ),
            )

    supervisor = _CrossRepositorySupervisor([])
    reviewer = _Specialist(repo)
    orch.register_agent(supervisor)
    orch.register_agent(reviewer)

    orch.run_pipeline()

    assert orch.state == TaskExecutionState.COMPLETED
    assert "foreign" in package.affected_repositories
    assert "foreign/support.txt" in package.write_scope
    assert package.implementation_summary == (
        "Repaired the evidence and retained the required support file."
    )
    assert _git(foreign, "status", "--porcelain") == ""
    assert _git(foreign, "log", "-1", "--pretty=%s").startswith("execraft(wp1)")
    event = next(
        entry
        for entry in reversed(orch._journal.read())
        if entry.event_type == "supervisor_incident_resolved"
    )
    assert event.payload["added_repositories"] == ["foreign"]
    assert event.payload["retained_paths"] == ["foreign:support.txt"]


def test_supervisor_cannot_skip_verification_with_unsafe_resume_stage(tmp_path):
    repo = _repo(tmp_path / "repo")
    policy = SupervisorPolicy(
        agent_id="codex",
        max_attempts_per_incident=1,
        ask_human_when_uncertain=False,
    )
    orch, package = _orchestrator(tmp_path, repo, policy=policy)
    supervisor = _Supervisor(
        [_resolved(resume_stage="ready_to_commit")], repo=repo
    )
    orch.register_agent(supervisor)

    assert orch._run_supervision_unlocked() is True

    assert orch.state == TaskExecutionState.HUMAN_REQUIRED
    assert package.stage == WorkPackageStage.FINAL_REVIEW
    stored = SupervisorIncidentStore(
        tmp_path / "state" / "projects" / "task" / "supervisor-incidents.json"
    ).list()[-1]
    assert stored.status == IncidentStatus.EXHAUSTED
    assert "unsafe Supervisor resume_stage" in stored.summary


def test_supervisor_runtime_budget_is_durable_across_restarts(tmp_path):
    repo = _repo(tmp_path / "repo")
    policy = SupervisorPolicy(
        agent_id="codex",
        max_runtime_minutes=1,
        ask_human_when_uncertain=False,
    )
    orch, _ = _orchestrator(tmp_path, repo, policy=policy)
    supervisor = _Supervisor([_resolved()])
    orch.register_agent(supervisor)
    store = SupervisorIncidentStore(
        tmp_path / "state" / "projects" / "task" / "supervisor-incidents.json"
    )
    incident = store.open_or_reuse(
        fingerprint=incident_fingerprint(orch._supervisor_action()),
        package_id="wp1",
        stage="final_review",
        classification=IncidentClass.REVIEW_EXHAUSTED,
        escalation_sequence=1,
        supervisor_agent_id="codex",
        workspace_digest=orch._workspace_digest(),
    )
    incident.active_runtime_seconds = 60
    store.save(incident)

    assert orch._run_supervision_unlocked() is True

    assert orch.state == TaskExecutionState.HUMAN_REQUIRED
    assert supervisor.calls == []
    assert store.list()[-1].status == IncidentStatus.EXHAUSTED
    assert "runtime budget exhausted" in store.list()[-1].summary


def test_supervisor_runtime_budget_excludes_idle_incident_age(tmp_path):
    repo = _repo(tmp_path / "repo")
    policy = SupervisorPolicy(
        agent_id="codex",
        max_runtime_minutes=1,
        ask_human_when_uncertain=False,
    )
    orch, _ = _orchestrator(tmp_path, repo, policy=policy)
    supervisor = _Supervisor([_resolved()])
    orch.register_agent(supervisor)
    store = SupervisorIncidentStore(
        tmp_path / "state" / "projects" / "task" / "supervisor-incidents.json"
    )
    incident = store.open_or_reuse(
        fingerprint=incident_fingerprint(orch._supervisor_action()),
        package_id="wp1",
        stage="final_review",
        classification=IncidentClass.REVIEW_EXHAUSTED,
        escalation_sequence=1,
        supervisor_agent_id="codex",
        workspace_digest=orch._workspace_digest(),
    )
    incident.created_at = (
        datetime.now(timezone.utc) - timedelta(minutes=10)
    ).isoformat()
    store.save(incident)

    assert orch._run_supervision_unlocked() is True

    assert supervisor.calls
    assert orch.state == TaskExecutionState.RUNNING
    stored = store.list()[-1]
    assert stored.status == IncidentStatus.RESOLVED
    assert stored.active_runtime_seconds >= 0


def test_daemon_automatically_enters_supervision_from_human_required(tmp_path):
    repo = _repo(tmp_path / "repo")
    orch, _ = _orchestrator(tmp_path, repo)
    supervisor = _Supervisor([_resolved(_modify=True)], repo=repo)
    reviewer = _Specialist(repo)
    orch.register_agent(supervisor)
    orch.register_agent(reviewer)
    sleeps: list[float] = []

    result = run_until_terminal(orch, sleep=sleeps.append)

    assert result.final_state == TaskExecutionState.COMPLETED
    assert result.attempts == 1
    assert sleeps == []


def test_stale_argmax_human_question_auto_retries_after_transport_repair(tmp_path):
    repo = _repo(tmp_path / "repo")
    orch, _ = _orchestrator(tmp_path, repo)
    supervisor = _Supervisor([_resolved(_modify=True)], repo=repo)
    reviewer = _Specialist(repo)
    orch.register_agent(supervisor)
    orch.register_agent(reviewer)

    store = SupervisorIncidentStore(
        tmp_path / "state" / "projects" / "task" / "supervisor-incidents.json"
    )
    incident = store.open_or_reuse(
        fingerprint=incident_fingerprint(orch._supervisor_action()),
        package_id="wp1",
        stage="final_review",
        classification=IncidentClass.AGENT_FAILURE,
        escalation_sequence=1,
        supervisor_agent_id="codex",
        workspace_digest=orch._workspace_digest(),
    )
    incident.attempts = 3
    incident.summary = (
        "[Errno 7] Argument list too long: '/workspace/.venv/bin/python'"
    )
    incident.human_question = {
        "question": "The Supervisor could not safely resolve this incident.",
        "context": incident.summary,
        "options": [
            {"id": "inspect_and_retry", "label": "Retry"},
            {"id": "stop", "label": "Stop"},
        ],
    }
    incident.touch(status=IncidentStatus.WAITING_FOR_HUMAN)
    store.save(incident)
    orch.transition_to(TaskExecutionState.SUPERVISING)
    orch.transition_to(TaskExecutionState.WAITING_FOR_HUMAN_DECISION)

    package = orch._state_record.plan_graph.package_by_id("wp1")
    orch._state_record.agent_waits = {
        package.id: {
            "package_id": package.id,
            "stage": package.stage.value,
            "next_check_at": "2099-01-01T00:00:00+00:00",
        }
    }
    orch._refresh_wait_summary()
    orch.save_state()

    assert orch.can_auto_resume_supervisor_transport_failure() is True
    result = run_until_terminal(orch, sleep=lambda _delay: None)

    assert result.final_state == TaskExecutionState.COMPLETED
    assert orch.status_report().get("waiting") == {}
    recovered = next(
        entry
        for entry in orch._journal.read()
        if entry.event_type == "supervisor_prompt_transport_recovered"
    )
    assert recovered.payload["package_id"] == "wp1"


def test_stale_human_question_recovers_lost_supervisor_delegation(tmp_path):
    repo = _repo(tmp_path / "repo")
    orch, package = _orchestrator(tmp_path, repo)
    supervisor = _Supervisor([_resolved()])

    class _HealthyFixer:
        adapter_name = "opencode"

        def __init__(self):
            self.calls = 0

        @property
        def provider_id(self):
            return "local-fixer"

        @property
        def availability(self):
            return Availability.AVAILABLE

        @property
        def capabilities(self):
            return {AgentCapability.FIX_REVIEW}

        def execute(self, _handoff):
            self.calls += 1
            (repo / "delegated.txt").write_text("fixed\n", encoding="utf-8")
            return {"ok": True, "status": "fixed", "summary": "fixed locally"}

    fixer = _HealthyFixer()
    reviewer = _Specialist(repo)
    orch.register_agent(supervisor)
    orch.register_agent(fixer)
    orch.register_agent(reviewer)

    store = SupervisorIncidentStore(
        tmp_path / "state" / "projects" / "task" / "supervisor-incidents.json"
    )
    incident = store.open_or_reuse(
        fingerprint=incident_fingerprint(orch._supervisor_action()),
        package_id=package.id,
        stage=package.stage.value,
        classification=IncidentClass.AGENT_FAILURE,
        escalation_sequence=1,
        supervisor_agent_id="codex",
        workspace_digest=orch._workspace_digest(),
    )
    incident.attempts = orch.config.supervisor_policy.max_attempts_per_incident
    incident.summary = "A delegated provider failed and the old driver escalated."
    incident.human_question = {
        "question": "How should recovery continue?",
        "context": incident.summary,
        "options": [
            {"id": "retry", "label": "Retry"},
            {"id": "stop", "label": "Stop"},
        ],
    }
    incident.touch(status=IncidentStatus.WAITING_FOR_HUMAN)
    store.save(incident)
    orch._journal.append(
        "supervisor_decision",
        {
            "incident_id": incident.incident_id,
            "package_id": package.id,
            "decision": "delegate",
        },
    )
    orch._journal.append(
        "supervisor_delegation_started",
        {
            "incident_id": incident.incident_id,
            "package_id": package.id,
            "index": 1,
            "task": "Repair the evidence and return structured output.",
            "capability": "fix_review",
            "agent_id": "opencode-zen-free",
            "skills": ["ai-fix-review"],
            "read_only": False,
        },
    )
    orch.transition_to(TaskExecutionState.SUPERVISING)
    orch.transition_to(TaskExecutionState.WAITING_FOR_HUMAN_DECISION)

    assert orch.can_auto_resume_lost_supervisor_delegation() is True
    result = run_until_terminal(orch, sleep=lambda _delay: None)

    assert result.final_state == TaskExecutionState.COMPLETED
    assert fixer.calls == 1
    assert len(supervisor.calls) == 1
    recovered = next(
        entry
        for entry in orch._journal.read()
        if entry.event_type == "supervisor_delegation_recovered"
    )
    assert recovered.payload["package_id"] == package.id
    completed = store.list()[-1]
    assert completed.status == IncidentStatus.RESOLVED
    assert completed.pending_delegations == []
    assert completed.human_question == {}


@pytest.mark.parametrize(
    "persisted_status",
    [IncidentStatus.DELEGATING, IncidentStatus.EXHAUSTED],
)
def test_terminal_human_wait_resumes_persisted_supervisor_queue(
    tmp_path, persisted_status
):
    """A durable queue is authoritative even when incident/project states drift."""

    repo = _repo(tmp_path / "repo")
    orch, package = _orchestrator(tmp_path, repo)
    supervisor = _Supervisor([_resolved()])

    class _HealthyFixer:
        adapter_name = "opencode"

        def __init__(self):
            self.calls = 0

        @property
        def provider_id(self):
            return "local-fixer"

        @property
        def availability(self):
            return Availability.AVAILABLE

        @property
        def capabilities(self):
            return {AgentCapability.FIX_REVIEW}

        def execute(self, _handoff):
            self.calls += 1
            (repo / "delegated.txt").write_text("fixed\n", encoding="utf-8")
            return {"ok": True, "status": "fixed", "summary": "fixed locally"}

    fixer = _HealthyFixer()
    reviewer = _Specialist(repo)
    orch.register_agent(supervisor)
    orch.register_agent(fixer)
    orch.register_agent(reviewer)

    store = SupervisorIncidentStore(
        tmp_path / "state" / "projects" / "task" / "supervisor-incidents.json"
    )
    incident = store.open_or_reuse(
        fingerprint=incident_fingerprint(orch._supervisor_action()),
        package_id=package.id,
        stage=package.stage.value,
        classification=IncidentClass.AGENT_FAILURE,
        escalation_sequence=1,
        supervisor_agent_id="codex",
        workspace_digest=orch._workspace_digest(),
    )
    incident.attempts = orch.config.supervisor_policy.max_attempts_per_incident
    incident.created_at = "2000-01-01T00:00:00+00:00"
    incident.active_runtime_seconds = (
        orch.config.supervisor_policy.max_runtime_minutes * 60
    )
    incident.pending_delegations = [
        SupervisorDelegation(
            task="Repair the evidence and return structured output.",
            capability=AgentCapability.FIX_REVIEW,
            skill_ids=("ai-fix-review",),
            read_only=False,
        ).as_mapping()
    ]
    incident.pending_delegation_index = 0
    incident.human_question = {
        "question": "How should recovery continue?",
        "options": [{"id": "retry", "label": "Retry"}],
    }
    incident.touch(status=persisted_status)
    store.save(incident)
    orch.transition_to(TaskExecutionState.SUPERVISING)
    orch.transition_to(TaskExecutionState.WAITING_FOR_HUMAN_DECISION)

    assert orch.can_auto_resume_lost_supervisor_delegation() is True
    report = orch.supervisor_status_report()
    assert report["auto_resume_lost_delegation"] is True
    assert report["pending_delegation_count"] == 1
    assert report["pending_delegation_source"] == "persisted_queue"

    result = run_until_terminal(orch, sleep=lambda _delay: None)

    assert result.final_state == TaskExecutionState.COMPLETED
    assert fixer.calls == 1
    recovered = next(
        entry
        for entry in orch._journal.read()
        if entry.event_type == "supervisor_delegation_recovered"
    )
    assert recovered.payload["source"] == "persisted_queue"
    assert recovered.payload["previous_incident_status"] == persisted_status.value
    assert recovered.payload["reset_active_runtime_seconds"] == (
        orch.config.supervisor_policy.max_runtime_minutes * 60
    )
    assert store.list()[-1].active_runtime_seconds < 10


def test_provider_is_allowed_supervisor():
    assert provider_is_allowed_supervisor("codex") is True
    assert provider_is_allowed_supervisor("claude") is True
    assert provider_is_allowed_supervisor("claude-code") is True
    assert provider_is_allowed_supervisor("antigravity") is True
    assert provider_is_allowed_supervisor("antigravity-cli") is True
    assert provider_is_allowed_supervisor("opencode") is False


def test_supervisor_handoff_does_not_request_provider_native_structured_output(tmp_path):
    repo = _repo(tmp_path / "repo")
    orch, _ = _orchestrator(tmp_path, repo)
    supervisor = _Supervisor([_resolved()])
    orch.register_agent(supervisor)

    assert orch._run_supervision_unlocked() is True

    assert supervisor.calls[0].expected_output_schema == {}


def test_semantically_unusable_supervisor_result_counts_attempt_without_contract_quarantine(tmp_path):
    repo = _repo(tmp_path / "repo")
    policy = SupervisorPolicy(agent_id="codex", max_attempts_per_incident=2)
    orch, package = _orchestrator(tmp_path, repo, policy=policy)

    class _UnclearSupervisor(_Supervisor):
        def execute(self, handoff):
            self.calls.append(handoff)
            return {"ok": True, "final_message": "I inspected the repository but have no conclusion."}

    supervisor = _UnclearSupervisor([])
    orch.register_agent(supervisor)

    assert orch._run_supervision_unlocked() is True

    incident = SupervisorIncidentStore(
        tmp_path / "state" / "projects" / "task" / "supervisor-incidents.json"
    ).active()
    assert incident is not None
    assert incident.attempts == 1
    assert incident.contract_failures == 0
    assert orch.state == TaskExecutionState.SUPERVISING
    assert any(
        entry.event_type == "supervisor_retry_scheduled"
        for entry in orch._journal.read()
    )
    history = orch.invocation_history(package_id=package.id)
    assert history[-1]["status"] == "completed"
    assert not any(
        (item.get("failure") or {}).get("classification") == "invalid_output"
        for item in history
    )


def test_legacy_contract_failure_counter_no_longer_blocks_supervision(tmp_path):
    repo = _repo(tmp_path / "repo")
    policy = SupervisorPolicy(
        agent_id="codex",
        max_attempts_per_incident=2,
        max_contract_failures=1,
    )
    orch, package = _orchestrator(tmp_path, repo, policy=policy)
    supervisor = _Supervisor([_resolved()])
    orch.register_agent(supervisor)

    action = orch._supervisor_action(None)
    incident = orch._supervisor_incidents.open_or_reuse(
        fingerprint=incident_fingerprint(action),
        package_id=package.id,
        stage="final_review",
        classification=classify_incident(action),
        escalation_sequence=int(action["sequence"]),
        supervisor_agent_id="codex",
        workspace_digest="legacy",
    )
    incident.contract_failures = policy.max_contract_failures
    orch._supervisor_incidents.save(incident)

    assert orch._run_supervision_unlocked() is True

    assert orch.state == TaskExecutionState.RUNNING
    assert package.stage == WorkPackageStage.REGRESSION_VERIFY
    assert len(supervisor.calls) == 1


def test_wait_summary_prunes_completed_and_repository_sync_pause_records(tmp_path):
    repo = _repo(tmp_path / "repo")
    orch, package = _orchestrator(tmp_path, repo)
    package.stage = WorkPackageStage.COMPLETED
    package.status = "completed"
    orch._state_record.agent_waits = {
        package.id: {
            "kind": "pause_for_repository_sync",
            "package_id": package.id,
            "stage": "completed",
        }
    }
    orch._state_record.waiting = dict(orch._state_record.agent_waits[package.id])

    orch._refresh_wait_summary()

    assert orch._state_record.agent_waits == {}
    assert orch._state_record.waiting == {}
