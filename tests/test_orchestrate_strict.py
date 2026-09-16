"""Production-default orchestration gates against real temporary Git repositories."""

from __future__ import annotations

import subprocess

import pytest
from pathlib import Path

from execraft.orchestrate import (
    AcceptanceCriterion,
    AgentCapability,
    Availability,
    OrchestrationConfig,
    ProjectOrchestrator,
    TaskExecutionState,
    VerificationCommand,
    VerificationRegistry,
    WorkPackage,
    WorkPackageStage,
    ScopePolicy,
    ScopeRecoveryPolicy,
    scope_recovery_policy_from_scheduling,
    normalize_work_packages,
)

from execraft.orchestrate.transactions import snapshot_repository
from execraft.orchestrate.scope_policy import classify_scope_path


class _Completed:
    returncode = 0
    stdout = "ok"
    stderr = ""


def _passing_runner(command: str, *, cwd: Path, timeout: int):
    assert Path(cwd).is_dir()
    assert timeout > 0
    return _Completed()


def _git(path: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=path, text=True, capture_output=True, check=True
    )
    return completed.stdout.strip()


def _repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "execraft@example.invalid")
    _git(path, "config", "user.name", "Execraft tests")
    (path / "README.md").write_text("baseline\n")
    _git(path, "add", "README.md")
    _git(path, "commit", "-q", "-m", "baseline")
    return path


class _StructuredAgent:
    def __init__(self, repo: Path, *, modify_repo: Path | None = None):
        self.repo = repo
        self.modify_repo = modify_repo or repo
        self.calls: list[str] = []

    @property
    def provider_id(self):
        return "strict-agent"

    @property
    def availability(self):
        return Availability.AVAILABLE

    @property
    def capabilities(self):
        return {
            AgentCapability.IMPLEMENT,
            AgentCapability.REVIEW,
            AgentCapability.FIX_REVIEW,
        }

    def execute(self, handoff):
        self.calls.append(handoff.stage)
        if handoff.stage in {"implement", "fix_review"}:
            (self.modify_repo / "implemented.txt").write_text("done\n")
            return {
                "ok": True,
                "status": "implemented" if handoff.stage == "implement" else "fixed",
                "summary": "implemented strict fixture",
                "acceptance_evidence": {"works": "implemented.txt and verification command"},
            }
        return {"ok": True, "verdict": "approved", "findings": [], "summary": "approved"}


class _StructuredReviewer(_StructuredAgent):
    @property
    def provider_id(self):
        return "strict-reviewer"

    @property
    def capabilities(self):
        return {AgentCapability.REVIEW}


def _graph(repository_id: str):
    return normalize_work_packages(
        [
            WorkPackage(
                id="wp1",
                title="Strict package",
                requirements=["Implement the requested fixture"],
                acceptance_criteria=[AcceptanceCriterion(id="works", description="Works")],
                affected_repositories=[repository_id],
                verification_profile="focused",
            )
        ]
    )


def _orchestrator(tmp_path: Path, repositories: dict[str, Path], registry=None):
    return ProjectOrchestrator(
        "strict-task",
        config=OrchestrationConfig(state_dir=tmp_path / "state"),
        registry=registry or VerificationRegistry(
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
        repository_paths=repositories,
        workspace_root=tmp_path,
    )


def test_strict_pipeline_creates_a_real_commit(tmp_path):
    repo = _repo(tmp_path / "repo")
    before = _git(repo, "rev-parse", "HEAD")
    orch = _orchestrator(tmp_path, {"repo": repo})
    orch.register_agent(_StructuredAgent(repo))
    orch.register_agent(_StructuredReviewer(repo))
    graph, report = _graph("repo")
    orch.initialize_graph(graph, report)

    orch.run_pipeline()

    assert orch.state == TaskExecutionState.COMPLETED
    assert _git(repo, "rev-parse", "HEAD") != before
    assert _git(repo, "status", "--porcelain") == ""
    tx = orch._commit_journal.last_by_work_package("wp1")
    assert tx is not None and tx.status == "committed"
    assert tx.post_snapshots[0].head_commit == _git(repo, "rev-parse", "HEAD")


def test_strict_pipeline_waits_when_no_agent_is_available(tmp_path):
    repo = _repo(tmp_path / "repo")
    orch = _orchestrator(tmp_path, {"repo": repo})
    graph, report = _graph("repo")
    orch.initialize_graph(graph, report)

    orch.run_pipeline()

    assert orch.state == TaskExecutionState.WAITING_FOR_AGENT
    assert orch.status_report()["waiting"]["capability"] == "implement"
    assert orch.status_report()["human_required"] is None
    assert _git(repo, "rev-list", "--count", "HEAD") == "1"


def test_strict_pipeline_blocks_when_verification_is_missing(tmp_path):
    repo = _repo(tmp_path / "repo")
    orch = _orchestrator(
        tmp_path,
        {"repo": repo},
        registry=VerificationRegistry(require_commands=True),
    )
    orch.register_agent(_StructuredAgent(repo))
    orch.register_agent(_StructuredReviewer(repo))
    graph, report = _graph("repo")
    orch.initialize_graph(graph, report)

    orch.run_pipeline()

    assert orch.state == TaskExecutionState.HUMAN_REQUIRED
    assert _git(repo, "rev-list", "--count", "HEAD") == "1"


def test_strict_pipeline_blocks_out_of_scope_repository_changes(tmp_path):
    repo = _repo(tmp_path / "repo")
    other = _repo(tmp_path / "other")
    orch = _orchestrator(tmp_path, {"repo": repo, "other": other})
    orch.register_agent(_StructuredAgent(repo, modify_repo=other))
    orch.register_agent(_StructuredReviewer(repo))
    graph, report = _graph("repo")
    orch.initialize_graph(graph, report)

    orch.run_pipeline()

    assert orch.state == TaskExecutionState.HUMAN_REQUIRED
    assert _git(repo, "rev-list", "--count", "HEAD") == "1"
    assert _git(other, "rev-list", "--count", "HEAD") == "1"


def test_strict_pending_multi_repo_transaction_rolls_forward_after_restart(tmp_path):
    repo = _repo(tmp_path / "repo")
    other = _repo(tmp_path / "other")
    config = OrchestrationConfig(state_dir=tmp_path / "state")
    first = ProjectOrchestrator(
        "strict-recovery",
        config=config,
        repository_paths={"repo": repo, "other": other},
        workspace_root=tmp_path,
    )
    package = WorkPackage(
        id="wp1",
        title="Recover commits",
        requirements=["Commit both repository changes"],
        acceptance_criteria=[
            AcceptanceCriterion(
                id="works", description="Works", verified=True, evidence="verified before crash"
            )
        ],
        affected_repositories=["repo", "other"],
        stage=WorkPackageStage.READY_TO_COMMIT,
    )
    graph, report = normalize_work_packages([package])
    first.initialize_graph(graph, report)
    first.transition_to(TaskExecutionState.RUNNING)
    package = first._state_record.plan_graph.package_by_id("wp1")
    package.stage = WorkPackageStage.READY_TO_COMMIT
    (repo / "one.txt").write_text("one\n")
    (other / "two.txt").write_text("two\n")
    tx = first._commit_journal.begin("pending-recovery", "2026-07-21T00:00:00+00:00", package.id)
    first._commit_journal.add_snapshots(
        tx.transaction_id,
        pre_snapshots=[
            snapshot_repository("repo", repo),
            snapshot_repository("other", other),
        ],
    )
    first._git_commit_repository(package, "repo", repo)
    first.save_state()  # crash before committing `other` and finalizing the journal

    second = ProjectOrchestrator(
        "strict-recovery",
        config=config,
        repository_paths={"repo": repo, "other": other},
        workspace_root=tmp_path,
    )
    second.load_state()
    second.run_pipeline()

    assert second.state == TaskExecutionState.COMPLETED
    recovered = second._commit_journal.get(tx.transaction_id)
    assert recovered is not None and recovered.status == "committed"
    assert _git(repo, "status", "--porcelain") == ""
    assert _git(other, "status", "--porcelain") == ""
    assert _git(repo, "rev-list", "--count", "HEAD") == "2"
    assert _git(other, "rev-list", "--count", "HEAD") == "2"


def test_commit_skips_missing_optional_repository_in_workspace(tmp_path):
    repo = _repo(tmp_path / "repo")
    config = OrchestrationConfig(state_dir=tmp_path / "state")
    orch = ProjectOrchestrator(
        "strict-optional",
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
        repository_requirements={"repo": True, "optional": False},
        workspace_root=tmp_path,
    )
    orch.register_agent(_StructuredAgent(repo))
    orch.register_agent(_StructuredReviewer(repo))
    graph, report = normalize_work_packages(
        [
            WorkPackage(
                id="wp1",
                title="Optional repository scope",
                requirements=["Implement the requested fixture"],
                acceptance_criteria=[AcceptanceCriterion(id="works", description="Works")],
                affected_repositories=["repo", "optional"],
                verification_profile="focused",
            )
        ]
    )
    orch.initialize_graph(graph, report)

    orch.run_pipeline()

    assert orch.state == TaskExecutionState.COMPLETED
    tx = orch._commit_journal.last_by_work_package("wp1")
    assert tx is not None and tx.status == "committed"
    assert [item.repository_id for item in tx.pre_snapshots] == ["repo"]
    skipped = [
        entry
        for entry in orch._journal.read()
        if entry.event_type == "optional_repository_skipped"
    ]
    assert skipped[-1].payload["repository_id"] == "optional"


def test_commit_blocks_when_required_repository_is_missing_from_workspace(tmp_path):
    repo = _repo(tmp_path / "repo")
    config = OrchestrationConfig(state_dir=tmp_path / "state")
    orch = ProjectOrchestrator(
        "strict-required",
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
        repository_requirements={"repo": True, "required_missing": True},
        workspace_root=tmp_path,
    )
    orch.register_agent(_StructuredAgent(repo))
    orch.register_agent(_StructuredReviewer(repo))
    graph, report = normalize_work_packages(
        [
            WorkPackage(
                id="wp1",
                title="Required repository scope",
                requirements=["Implement the requested fixture"],
                acceptance_criteria=[AcceptanceCriterion(id="works", description="Works")],
                affected_repositories=["repo", "required_missing"],
                verification_profile="focused",
            )
        ]
    )
    orch.initialize_graph(graph, report)

    orch.run_pipeline()

    assert orch.state == TaskExecutionState.HUMAN_REQUIRED
    escalation = [
        entry
        for entry in orch._journal.read()
        if entry.event_type == "human_intervention_required"
    ][-1]
    assert "required repository is not present" in escalation.payload["evidence"][0]
    report = orch.human_required_report()
    assert report is not None
    assert report["stage"] == "commit"
    assert report["agent"] == {}
    assert report["artifact"] == {}


def test_generated_fix_scope_can_be_inspected_and_explicitly_approved(tmp_path):
    repo = _repo(tmp_path / "repo")
    orch = _orchestrator(tmp_path, {"repo": repo})
    parent = WorkPackage(
        id="parent",
        title="Aggregate parent",
        requirements=["Complete generated work"],
        acceptance_criteria=[AcceptanceCriterion(id="works", description="Works")],
        affected_repositories=["repo"],
        execution_mode="aggregate",
        stage=WorkPackageStage.PREPARE,
    )
    child = WorkPackage(
        id="parent__fix",
        title="Generated fix shard",
        requirements=["Fix the review finding"],
        acceptance_criteria=[AcceptanceCriterion(id="works", description="Works")],
        affected_repositories=["repo"],
        execution_mode="standard_shard",
        parent_id="parent",
        write_scope=["repo/allowed.py"],
        stage=WorkPackageStage.FIX_REVIEW,
        status="running",
    )
    graph, report = normalize_work_packages([parent, child])
    orch.initialize_graph(graph, report)
    orch.transition_to(TaskExecutionState.RUNNING)
    child = orch._state_record.plan_graph.package_by_id("parent__fix")
    child.stage = WorkPackageStage.FIX_REVIEW
    (repo / "allowed.py").write_text("allowed = True\n")
    (repo / "adjacent.py").write_text("fixed = True\n")
    orch._scope_recovery_coordinator.escalate_scope_failure(
        child,
        "generated shard modified paths outside write_scope: repo:adjacent.py",
    )

    scope = orch.declared_write_scope_report(child.id)

    assert scope["violations"] == ["repo:adjacent.py"]
    result = orch.approve_declared_write_scope(child.id)

    assert result["added_paths"] == ["repo/adjacent.py"]
    assert result["next_stage"] == "regression_verify"
    assert orch.state == TaskExecutionState.RUNNING
    restored = orch._state_record.plan_graph.package_by_id(child.id)
    assert restored.stage == WorkPackageStage.REGRESSION_VERIFY
    assert "repo/adjacent.py" in restored.write_scope
    assert orch.declared_write_scope_report(child.id)["violations"] == []


def test_resolved_protected_fix_scope_resumes_at_regression_verification(tmp_path):
    repo = _repo(tmp_path / "repo")
    orch = _orchestrator(tmp_path, {"repo": repo})
    child = WorkPackage(
        id="child",
        title="Protected fix",
        requirements=["Fix CI policy"],
        acceptance_criteria=[AcceptanceCriterion(id="works", description="Works")],
        affected_repositories=["repo"],
        write_scope=["repo/src.py"],
        stage=WorkPackageStage.FIX_REVIEW,
        status="running",
    )
    graph, report = normalize_work_packages([child])
    orch.initialize_graph(graph, report)
    orch.transition_to(TaskExecutionState.RUNNING)
    child = orch._state_record.plan_graph.package_by_id("child")
    child.stage = WorkPackageStage.FIX_REVIEW
    orch._scope_recovery_coordinator.escalate_scope_failure(
        child,
        "scope recovery cannot approve protected paths: repo:.github/workflows/ci.yml",
    )

    result = orch.reconcile_resolved_scope_check("child")

    assert result["previous_stage"] == "fix_review"
    assert result["next_stage"] == "regression_verify"
    assert child.stage == WorkPackageStage.REGRESSION_VERIFY


def test_scope_approval_rejects_candidate_drift_after_operator_preview(tmp_path):
    repo = _repo(tmp_path / "repo")
    orch = _orchestrator(tmp_path, {"repo": repo})
    parent = WorkPackage(
        id="parent",
        title="Aggregate parent",
        requirements=["Complete generated work"],
        acceptance_criteria=[AcceptanceCriterion(id="works", description="Works")],
        affected_repositories=["repo"],
        execution_mode="aggregate",
        stage=WorkPackageStage.PREPARE,
    )
    child = WorkPackage(
        id="parent__fix",
        title="Generated fix shard",
        requirements=["Fix the review finding"],
        acceptance_criteria=[AcceptanceCriterion(id="works", description="Works")],
        affected_repositories=["repo"],
        execution_mode="standard_shard",
        parent_id="parent",
        write_scope=["repo/allowed.py"],
        stage=WorkPackageStage.FIX_REVIEW,
        status="running",
    )
    graph, report = normalize_work_packages([parent, child])
    orch.initialize_graph(graph, report)
    orch.transition_to(TaskExecutionState.RUNNING)
    child = orch._state_record.plan_graph.package_by_id("parent__fix")
    child.stage = WorkPackageStage.FIX_REVIEW
    (repo / "adjacent.py").write_text("fixed = True\n")
    orch._scope_recovery_coordinator.escalate_scope_failure(
        child,
        "generated shard modified paths outside write_scope: repo:adjacent.py",
    )
    previewed = orch.declared_write_scope_report(child.id)["workspace_scope"][
        "candidates"
    ]
    expected = [item["path"] for item in previewed]
    assert expected == ["repo:adjacent.py"]

    (repo / "new_after_preview.py").write_text("new = True\n")

    with pytest.raises(
        Exception,
        match="workspace scope changed after operator preview",
    ):
        orch.approve_declared_write_scope(
            child.id,
            expected_candidates=expected,
        )
    assert orch.state == TaskExecutionState.HUMAN_REQUIRED


def test_scope_approval_rejects_unrelated_human_required_event(tmp_path):
    repo = _repo(tmp_path / "repo")
    orch = _orchestrator(tmp_path, {"repo": repo})
    package = WorkPackage(
        id="child",
        title="Generated shard",
        requirements=["Do work"],
        acceptance_criteria=[AcceptanceCriterion(id="works", description="Works")],
        affected_repositories=["repo"],
        execution_mode="standard_shard",
        parent_id="parent",
        write_scope=["repo/allowed.py"],
        stage=WorkPackageStage.FIX_REVIEW,
    )
    parent = WorkPackage(
        id="parent",
        title="Parent",
        requirements=["Do work"],
        acceptance_criteria=[AcceptanceCriterion(id="works", description="Works")],
        affected_repositories=["repo"],
    )
    graph, report = normalize_work_packages([parent, package])
    orch.initialize_graph(graph, report)
    orch.transition_to(TaskExecutionState.RUNNING)
    package = orch._state_record.plan_graph.package_by_id("child")
    package.stage = WorkPackageStage.FIX_REVIEW
    (repo / "adjacent.py").write_text("fixed = True\n")
    orch._journal.append(
        "human_intervention_required",
        {
            "package_id": package.id,
            "stage": "verification",
            "blocked_requirement": "verification failed",
            "evidence": ["test failed"],
        },
    )
    orch.transition_to(TaskExecutionState.HUMAN_REQUIRED)

    with pytest.raises(Exception, match="not a write-scope failure"):
        orch.approve_declared_write_scope(package.id)


def test_declared_directory_scope_matches_descendants(tmp_path):
    repo = _repo(tmp_path / "repo")
    (repo / "service_runtime" / "operator").mkdir(parents=True)
    (repo / "service_runtime" / "operator" / "api.py").write_text("BASE = True\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "add package")
    orch = _orchestrator(tmp_path, {"repo": repo})
    parent = WorkPackage(
        id="parent",
        title="Parent",
        requirements=["Do work"],
        acceptance_criteria=[AcceptanceCriterion(id="works", description="Works")],
        affected_repositories=["repo"],
    )
    child = WorkPackage(
        id="child",
        title="Shard",
        requirements=["Do work"],
        acceptance_criteria=[AcceptanceCriterion(id="works", description="Works")],
        affected_repositories=["repo"],
        parent_id="parent",
        execution_mode="standard_shard",
        write_scope=["repo/service_runtime"],
        stage=WorkPackageStage.IMPLEMENT,
    )
    graph, report = normalize_work_packages([parent, child])
    orch.initialize_graph(graph, report)
    (repo / "service_runtime" / "operator" / "leases.py").write_text("LEASE = True\n")

    assert orch.declared_write_scope_report("child")["violations"] == []


def test_safe_supporting_scope_is_auto_expanded_as_exact_paths(tmp_path):
    repo = _repo(tmp_path / "repo")
    orch = _orchestrator(tmp_path, {"repo": repo})
    parent = WorkPackage(
        id="parent",
        title="Parent",
        requirements=["Do work"],
        acceptance_criteria=[AcceptanceCriterion(id="works", description="Works")],
        affected_repositories=["repo"],
    )
    child = WorkPackage(
        id="child",
        title="Shard",
        requirements=["Do work"],
        acceptance_criteria=[AcceptanceCriterion(id="works", description="Works")],
        affected_repositories=["repo"],
        parent_id="parent",
        execution_mode="standard_shard",
        write_scope=["repo/allowed.py"],
        stage=WorkPackageStage.FIX_REVIEW,
    )
    graph, report = normalize_work_packages([parent, child])
    orch.initialize_graph(graph, report)
    orch.transition_to(TaskExecutionState.RUNNING)
    child = orch._state_record.plan_graph.package_by_id("child")
    child.stage = WorkPackageStage.FIX_REVIEW
    (repo / "adjacent.py").write_text("fixed = True\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_fix.py").write_text("def test_fix(): pass\n")
    (repo / "docs" / "verification").mkdir(parents=True)
    (repo / "docs" / "verification" / "evidence.md").write_text("verified\n")

    orch._scope_recovery_coordinator.validate_declared_write_scope(child)

    assert orch.state == TaskExecutionState.RUNNING
    assert "repo/adjacent.py" in child.write_scope
    assert "repo/tests/test_fix.py" in child.write_scope
    assert "repo/docs/verification/evidence.md" in child.write_scope
    events = [entry.event_type for entry in orch._journal.read()]
    assert "write_scope_auto_expanded" in events


def test_protected_scope_path_still_requires_human_review(tmp_path):
    repo = _repo(tmp_path / "repo")
    config = OrchestrationConfig(
        state_dir=tmp_path / "state",
        scope_policy=ScopePolicy(enabled=True),
    )
    orch = ProjectOrchestrator(
        "strict-task",
        config=config,
        repository_paths={"repo": repo},
        workspace_root=tmp_path,
    )
    parent = WorkPackage(
        id="parent",
        title="Parent",
        requirements=["Do work"],
        acceptance_criteria=[AcceptanceCriterion(id="works", description="Works")],
        affected_repositories=["repo"],
    )
    child = WorkPackage(
        id="child",
        title="Shard",
        requirements=["Do work"],
        acceptance_criteria=[AcceptanceCriterion(id="works", description="Works")],
        affected_repositories=["repo"],
        parent_id="parent",
        execution_mode="standard_shard",
        write_scope=["repo/allowed.py"],
        stage=WorkPackageStage.IMPLEMENT,
    )
    graph, report = normalize_work_packages([parent, child])
    orch.initialize_graph(graph, report)
    orch.transition_to(TaskExecutionState.RUNNING)
    child = orch._state_record.plan_graph.package_by_id("child")
    child.stage = WorkPackageStage.IMPLEMENT
    (repo / "pyproject.toml").write_text("[project]\nname='unsafe'\n")

    with pytest.raises(Exception):
        orch._scope_recovery_coordinator.validate_declared_write_scope(child)

    assert orch.state == TaskExecutionState.HUMAN_REQUIRED
    report = orch.declared_write_scope_report("child")
    assert report["assessments"][0]["category"] == "protected"
    assert report["auto_expandable_paths"] == []


def test_completed_scope_check_reconciles_after_workspace_is_committed(tmp_path):
    repo = _repo(tmp_path / "repo")
    orch = _orchestrator(tmp_path, {"repo": repo})
    parent = WorkPackage(
        id="parent",
        title="Parent",
        requirements=["Do work"],
        acceptance_criteria=[AcceptanceCriterion(id="works", description="Works")],
        affected_repositories=["repo"],
        stage=WorkPackageStage.COMPLETED,
        status="completed",
    )
    child = WorkPackage(
        id="child",
        title="Shard",
        requirements=["Do work"],
        acceptance_criteria=[AcceptanceCriterion(id="works", description="Works")],
        affected_repositories=["repo"],
        parent_id="parent",
        execution_mode="standard_shard",
        write_scope=["repo/allowed.py"],
        stage=WorkPackageStage.COMPLETED,
        status="completed",
    )
    graph, report = normalize_work_packages([parent, child])
    orch.initialize_graph(graph, report)
    orch.transition_to(TaskExecutionState.RUNNING)
    child = orch._state_record.plan_graph.package_by_id("child")
    orch._scope_recovery_coordinator.escalate_scope_failure(
        child,
        "generated shard modified paths outside write_scope: repo:adjacent.py",
    )

    result = orch.approve_declared_write_scope("child")

    assert result["reconciled"] is True
    assert result["previous_stage"] == "completed"
    assert result["next_stage"] == "completed"
    assert orch.state == TaskExecutionState.RUNNING


def test_ready_to_commit_scope_check_allows_authorized_dirty_delta(tmp_path):
    repo = _repo(tmp_path / "repo")
    (repo / "allowed.py").write_text("value = 0\n")
    _git(repo, "add", "allowed.py")
    _git(repo, "commit", "-q", "-m", "add allowed fixture")
    orch = _orchestrator(tmp_path, {"repo": repo})
    parent = WorkPackage(
        id="parent",
        title="Completed parent",
        requirements=["Coordinate child"],
        acceptance_criteria=[
            AcceptanceCriterion(
                id="parent-complete",
                description="Parent complete",
                verified=True,
                evidence="child owns the remaining commit",
            )
        ],
        affected_repositories=["repo"],
        stage=WorkPackageStage.COMPLETED,
        status="completed",
    )
    child = WorkPackage(
        id="child",
        title="Ready shard",
        requirements=["Commit the authorized delta"],
        acceptance_criteria=[
            AcceptanceCriterion(
                id="works",
                description="Works",
                verified=True,
                evidence="verification and review completed",
            )
        ],
        affected_repositories=["repo"],
        parent_id="parent",
        execution_mode="standard_shard",
        write_scope=["repo/allowed.py"],
        stage=WorkPackageStage.READY_TO_COMMIT,
    )
    graph, report = normalize_work_packages([parent, child])
    orch.initialize_graph(graph, report)
    orch.transition_to(TaskExecutionState.RUNNING)
    child = orch._state_record.plan_graph.package_by_id("child")
    child.stage = WorkPackageStage.READY_TO_COMMIT
    (repo / "allowed.py").write_text("value = 1\n")
    orch._scope_recovery_coordinator.escalate_scope_failure(
        child,
        "generated shard modified paths outside write_scope: repo:obsolete.py",
    )

    scope = orch.declared_write_scope_report("child")

    assert scope["violations"] == []
    assert scope["reconciliation"]["can_reconcile"] is True
    assert scope["reconciliation"]["authorized_dirty_paths"] == {
        "repo": ["allowed.py"]
    }
    assert orch.can_auto_resume_human_required() is True

    orch.run_pipeline()

    assert orch.state == TaskExecutionState.COMPLETED
    assert _git(repo, "status", "--porcelain") == ""
    assert _git(repo, "show", "HEAD:allowed.py") == "value = 1"
    assert any(
        entry.event_type == "repository_scope_check_reconciled"
        for entry in orch._journal.read()
    )


def test_ready_to_commit_scope_check_rejects_dirty_unaffected_repository(tmp_path):
    repo = _repo(tmp_path / "repo")
    other = _repo(tmp_path / "other")
    (repo / "allowed.py").write_text("value = 0\n")
    _git(repo, "add", "allowed.py")
    _git(repo, "commit", "-q", "-m", "add allowed fixture")
    orch = _orchestrator(tmp_path, {"repo": repo, "other": other})
    parent = WorkPackage(
        id="parent",
        title="Completed parent",
        requirements=["Coordinate child"],
        acceptance_criteria=[
            AcceptanceCriterion(
                id="parent-complete",
                description="Parent complete",
                verified=True,
                evidence="complete",
            )
        ],
        affected_repositories=["repo"],
        stage=WorkPackageStage.COMPLETED,
        status="completed",
    )
    child = WorkPackage(
        id="child",
        title="Ready shard",
        requirements=["Commit the authorized delta"],
        acceptance_criteria=[
            AcceptanceCriterion(
                id="works",
                description="Works",
                verified=True,
                evidence="verified",
            )
        ],
        affected_repositories=["repo"],
        parent_id="parent",
        execution_mode="standard_shard",
        write_scope=["repo/allowed.py"],
        stage=WorkPackageStage.READY_TO_COMMIT,
    )
    graph, report = normalize_work_packages([parent, child])
    orch.initialize_graph(graph, report)
    orch.transition_to(TaskExecutionState.RUNNING)
    child = orch._state_record.plan_graph.package_by_id("child")
    child.stage = WorkPackageStage.READY_TO_COMMIT
    (repo / "allowed.py").write_text("value = 1\n")
    (other / "unrelated.txt").write_text("unrelated\n")
    orch._scope_recovery_coordinator.escalate_scope_failure(
        child,
        "agent modified repositories outside the declared scope: other",
    )

    reconciliation = orch.declared_write_scope_report("child")["reconciliation"]

    assert reconciliation["can_reconcile"] is False
    assert reconciliation["reason"] == (
        "repositories outside the package scope are still dirty"
    )
    assert reconciliation["dirty_paths"] == {"other": ["unrelated.txt"]}
    assert orch.can_auto_resume_human_required() is False


def test_run_auto_reconciles_stale_completed_scope_check(tmp_path):
    repo = _repo(tmp_path / "repo")
    orch = _orchestrator(tmp_path, {"repo": repo})
    package = WorkPackage(
        id="done",
        title="Done shard",
        requirements=["Do work"],
        acceptance_criteria=[
            AcceptanceCriterion(id="works", description="Works", verified=True)
        ],
        affected_repositories=["repo"],
        parent_id="parent",
        execution_mode="standard_shard",
        write_scope=["repo/allowed.py"],
        stage=WorkPackageStage.COMPLETED,
        status="completed",
    )
    parent = WorkPackage(
        id="parent",
        title="Done parent",
        requirements=["Do work"],
        acceptance_criteria=[
            AcceptanceCriterion(id="works", description="Works", verified=True)
        ],
        affected_repositories=["repo"],
        stage=WorkPackageStage.COMPLETED,
        status="completed",
    )
    graph, report = normalize_work_packages([parent, package])
    orch.initialize_graph(graph, report)
    orch.transition_to(TaskExecutionState.RUNNING)
    package = orch._state_record.plan_graph.package_by_id("done")
    orch._scope_recovery_coordinator.escalate_scope_failure(
        package,
        "generated shard modified paths outside write_scope: repo:adjacent.py",
    )

    orch.run_pipeline()

    assert orch.state == TaskExecutionState.COMPLETED
    assert any(
        entry.event_type == "repository_scope_check_reconciled"
        for entry in orch._journal.read()
    )


class _ScopeRecoveryAgent:
    def __init__(self, provider_id: str, capabilities: set[AgentCapability]):
        self._provider_id = provider_id
        self._capabilities = capabilities
        self.calls: list[str] = []

    @property
    def provider_id(self):
        return self._provider_id

    @property
    def availability(self):
        return Availability.AVAILABLE

    @property
    def capabilities(self):
        return set(self._capabilities)

    def execute(self, handoff):
        self.calls.append(handoff.stage)
        if handoff.stage == "scope_recovery":
            assert "repo:CMakeLists.txt" in handoff.unresolved_findings[0]
            schema = handoff.expected_output_schema
            assert schema["additionalProperties"] is False
            assert set(schema["required"]) == set(schema["properties"])
            assert "ok" not in schema["properties"]
            return {
                "ok": True,
                "status": "resolved",
                "summary": "kept the required build-system update",
                "retain_paths": ["repo:CMakeLists.txt"],
                "removed_paths": [],
            }
        return {
            "ok": True,
            "verdict": "approved",
            "findings": [],
            "summary": "independent review approved",
        }


def test_scope_recovery_policy_validates_and_serializes():
    policy = ScopeRecoveryPolicy.from_mapping(
        {
            "enabled": True,
            "auto_resume": False,
            "max_resume_attempts": 3,
            "prefer_reviewer": False,
            "cleanup_patterns": ["**/*.pyc"],
            "max_files": 4,
            "max_changed_lines": 500,
            "max_excerpt_bytes": 4096,
        }
    )

    assert policy.enabled is True
    assert policy.auto_resume is False
    assert policy.max_resume_attempts == 3
    assert policy.prefer_reviewer is False
    assert policy.cleanup_patterns == ("**/*.pyc",)
    assert policy.as_mapping()["max_files"] == 4

    with pytest.raises(ValueError, match="max_files must be positive"):
        ScopeRecoveryPolicy.from_mapping({"max_files": 0})

    with pytest.raises(ValueError, match="max_resume_attempts must be positive"):
        ScopeRecoveryPolicy.from_mapping({"max_resume_attempts": 0})

    with pytest.raises(ValueError, match="allow_cross_repository must be a boolean"):
        ScopeRecoveryPolicy.from_mapping({"allow_cross_repository": "yes"})


def test_scope_patterns_preserve_root_hidden_directories():
    recovery = ScopeRecoveryPolicy.from_mapping(
        {"cleanup_patterns": [".pytest_cache/**", "./.ruff_cache/**"]}
    )
    scope = ScopePolicy.from_mapping(
        {"deny_patterns": [".github/**", "./.execraft/**"]}
    )

    assert recovery.cleanup_patterns == (
        ".pytest_cache/**",
        ".ruff_cache/**",
    )
    assert scope.deny_patterns == (".github/**", ".execraft/**")
    protected = classify_scope_path(
        "repo:.github/workflows/ci.yml",
        patterns=[],
        policy=scope,
        changed_lines=10,
    )
    assert protected.category == "protected"
    assert protected.auto_expandable is False


def test_automatic_recovery_switch_is_canonical_and_overrides_legacy_flags():
    enabled = scope_recovery_policy_from_scheduling(
        {
            "automatic_recovery": True,
            "scope_recovery": {
                "enabled": False,
                "auto_resume": False,
                "allow_cross_repository": True,
                "max_repositories": 6,
            },
        }
    )
    assert enabled.enabled is True
    assert enabled.auto_resume is True
    assert enabled.allow_cross_repository is True
    assert enabled.max_repositories == 6

    disabled = scope_recovery_policy_from_scheduling(
        {
            "automatic_recovery": False,
            "scope_recovery": {"enabled": True, "auto_resume": True},
        }
    )
    assert disabled.enabled is False
    assert disabled.auto_resume is False

    with pytest.raises(ValueError, match="automatic_recovery must be a boolean"):
        scope_recovery_policy_from_scheduling({"automatic_recovery": "yes"})


def test_clean_start_removes_configured_untracked_cache_artifacts(tmp_path):
    repo = _repo(tmp_path / "repo")
    config = OrchestrationConfig(
        state_dir=tmp_path / "state",
        scope_recovery_policy=ScopeRecoveryPolicy(enabled=True),
    )
    orch = ProjectOrchestrator(
        "strict-task",
        config=config,
        repository_paths={"repo": repo},
        workspace_root=tmp_path,
    )
    package = WorkPackage(
        id="package",
        title="Package",
        requirements=["Do work"],
        acceptance_criteria=[AcceptanceCriterion(id="works", description="Works")],
        affected_repositories=["repo"],
    )
    graph, report = normalize_work_packages([package])
    orch.initialize_graph(graph, report)
    orch.transition_to(TaskExecutionState.RUNNING)
    package = orch._state_record.plan_graph.package_by_id("package")
    cache = repo / "tests" / "__pycache__" / "generated.pyc"
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"\0cache")

    orch._validate_clean_start(package)

    assert not cache.exists()
    assert orch.state == TaskExecutionState.RUNNING
    cleanup_events = [
        entry
        for entry in orch._journal.read()
        if entry.event_type == "scope_recovery_artifacts_removed"
    ]
    assert cleanup_events[-1].payload["source"] == "clean_start"


def test_persisted_clean_start_check_auto_cleans_and_reconciles(tmp_path):
    repo = _repo(tmp_path / "repo")
    config = OrchestrationConfig(
        state_dir=tmp_path / "state",
        scope_recovery_policy=ScopeRecoveryPolicy(enabled=True, auto_resume=True),
    )
    orch = ProjectOrchestrator(
        "strict-task",
        config=config,
        repository_paths={"repo": repo},
        workspace_root=tmp_path,
    )
    package = WorkPackage(
        id="package",
        title="Package",
        requirements=["Do work"],
        acceptance_criteria=[AcceptanceCriterion(id="works", description="Works")],
        affected_repositories=["repo"],
        stage=WorkPackageStage.PREPARE,
    )
    graph, report = normalize_work_packages([package])
    orch.initialize_graph(graph, report)
    orch.transition_to(TaskExecutionState.RUNNING)
    package = orch._state_record.plan_graph.package_by_id("package")
    cache = repo / "tests" / "__pycache__" / "generated.pyc"
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"\0cache")
    orch._scope_recovery_coordinator.escalate_scope_failure(
        package,
        "workspace must be clean before a new package starts; "
        "repo: tests/__pycache__/generated.pyc",
    )

    assert orch.can_auto_resume_human_required() is True
    assert orch._scope_recovery_coordinator.auto_recover_scope_check_unlocked() is True

    assert not cache.exists()
    assert orch.state == TaskExecutionState.RUNNING
    assert package.stage == WorkPackageStage.PREPARE


def test_persisted_scope_check_retries_reviewer_recovery_and_advances(tmp_path):
    repo = _repo(tmp_path / "repo")
    (repo / "src").mkdir()
    (repo / "src" / "allowed.cpp").write_text("int allowed = 0;\n")
    (repo / "CMakeLists.txt").write_text("add_library(fixture src/allowed.cpp)\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "add fixture")
    config = OrchestrationConfig(
        state_dir=tmp_path / "state",
        scope_policy=ScopePolicy(enabled=True),
        scope_recovery_policy=ScopeRecoveryPolicy(enabled=True, auto_resume=True),
    )
    orch = ProjectOrchestrator(
        "strict-task",
        config=config,
        repository_paths={"repo": repo},
        workspace_root=tmp_path,
    )
    reviewer = _ScopeRecoveryAgent(
        "reviewer", {AgentCapability.REVIEW, AgentCapability.FIX_REVIEW}
    )
    orch.register_agent(reviewer)
    parent = WorkPackage(
        id="parent",
        title="Parent",
        requirements=["Complete child"],
        acceptance_criteria=[AcceptanceCriterion(id="works", description="Works")],
        affected_repositories=["repo"],
        execution_mode="aggregate",
    )
    child = WorkPackage(
        id="child",
        title="Generated shard",
        requirements=["Implement route-neutral fixture"],
        acceptance_criteria=[AcceptanceCriterion(id="works", description="Works")],
        affected_repositories=["repo"],
        parent_id="parent",
        execution_mode="standard_shard",
        write_scope=["repo/src/allowed.cpp"],
        stage=WorkPackageStage.IMPLEMENT,
        reviewer_id="reviewer",
    )
    graph, report = normalize_work_packages([parent, child])
    orch.initialize_graph(graph, report)
    orch.transition_to(TaskExecutionState.RUNNING)
    child = orch._state_record.plan_graph.package_by_id("child")
    child.stage = WorkPackageStage.IMPLEMENT
    child.reviewer_id = "reviewer"
    (repo / "src" / "allowed.cpp").write_text("int allowed = 1;\n")
    (repo / "CMakeLists.txt").write_text(
        "add_library(fixture src/allowed.cpp)\nadd_subdirectory(tests)\n"
    )
    orch._scope_recovery_coordinator.escalate_scope_failure(
        child,
        "generated shard modified paths outside write_scope: repo:CMakeLists.txt",
    )

    assert orch.can_auto_resume_human_required() is True
    assert orch._scope_recovery_coordinator.auto_recover_scope_check_unlocked() is True

    assert orch.state == TaskExecutionState.RUNNING
    assert child.stage == WorkPackageStage.FAST_VERIFY
    assert "repo/CMakeLists.txt" in child.write_scope
    assert reviewer.calls == ["scope_recovery"]


def test_persisted_scope_recovery_stops_after_bounded_attempts(tmp_path):
    repo = _repo(tmp_path / "repo")
    config = OrchestrationConfig(
        state_dir=tmp_path / "state",
        scope_policy=ScopePolicy(enabled=True),
        scope_recovery_policy=ScopeRecoveryPolicy(
            enabled=True,
            auto_resume=True,
            max_resume_attempts=2,
        ),
    )
    orch = ProjectOrchestrator(
        "strict-task",
        config=config,
        repository_paths={"repo": repo},
        workspace_root=tmp_path,
    )

    class _InvalidRecoveryAgent(_ScopeRecoveryAgent):
        def execute(self, handoff):
            self.calls.append(handoff.stage)
            return {
                "ok": True,
                "status": "resolved",
                "summary": "left the violating path unchanged",
                "retain_paths": [],
                "removed_paths": [],
            }

    reviewer = _InvalidRecoveryAgent(
        "reviewer", {AgentCapability.REVIEW, AgentCapability.FIX_REVIEW}
    )
    orch.register_agent(reviewer)
    parent = WorkPackage(
        id="parent",
        title="Parent",
        requirements=["Complete child"],
        acceptance_criteria=[AcceptanceCriterion(id="works", description="Works")],
        affected_repositories=["repo"],
    )
    child = WorkPackage(
        id="child",
        title="Generated shard",
        requirements=["Do work"],
        acceptance_criteria=[AcceptanceCriterion(id="works", description="Works")],
        affected_repositories=["repo"],
        parent_id="parent",
        execution_mode="standard_shard",
        write_scope=["repo/allowed.py"],
        stage=WorkPackageStage.IMPLEMENT,
        reviewer_id="reviewer",
    )
    graph, report = normalize_work_packages([parent, child])
    orch.initialize_graph(graph, report)
    orch.transition_to(TaskExecutionState.RUNNING)
    child = orch._state_record.plan_graph.package_by_id("child")
    child.stage = WorkPackageStage.IMPLEMENT
    child.reviewer_id = "reviewer"
    (repo / "infra").mkdir()
    (repo / "infra" / "CMakeLists.txt").write_text("project(unresolved)\n")
    orch._scope_recovery_coordinator.escalate_scope_failure(
        child,
        "generated shard modified paths outside write_scope: repo:infra/CMakeLists.txt",
    )

    assert orch._scope_recovery_coordinator.auto_recover_scope_check_unlocked() is False
    assert orch.state == TaskExecutionState.HUMAN_REQUIRED
    assert orch.can_auto_resume_human_required() is True
    assert orch._scope_recovery_coordinator.auto_recover_scope_check_unlocked() is False

    assert orch.state == TaskExecutionState.HUMAN_REQUIRED
    assert orch.can_auto_resume_human_required() is False
    assert reviewer.calls == ["scope_recovery", "scope_recovery"]

    (repo / "infra" / "CMakeLists.txt").write_text("project(revised)\n")
    assert orch.can_auto_resume_human_required() is True


def test_scope_recovery_provider_wait_resumes_same_recovery_stage(tmp_path):
    repo = _repo(tmp_path / "repo")
    (repo / "src").mkdir()
    (repo / "src" / "allowed.cpp").write_text("int allowed = 0;\n")
    (repo / "CMakeLists.txt").write_text("add_library(fixture src/allowed.cpp)\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "add fixture")
    config = OrchestrationConfig(
        state_dir=tmp_path / "state",
        max_agent_attempts_per_stage=1,
        scope_policy=ScopePolicy(enabled=True),
        scope_recovery_policy=ScopeRecoveryPolicy(enabled=True, auto_resume=True),
    )
    orch = ProjectOrchestrator(
        "strict-task",
        config=config,
        repository_paths={"repo": repo},
        workspace_root=tmp_path,
    )

    class _WaitingRecoveryAgent(_ScopeRecoveryAgent):
        def execute(self, handoff):
            self.calls.append(handoff.stage)
            if len(self.calls) == 1:
                raise RuntimeError("temporary provider outage")
            return {
                "ok": True,
                "status": "resolved",
                "summary": "kept the required build update",
                "retain_paths": ["repo:CMakeLists.txt"],
                "removed_paths": [],
            }

    reviewer = _WaitingRecoveryAgent(
        "reviewer", {AgentCapability.REVIEW, AgentCapability.FIX_REVIEW}
    )
    orch.register_agent(reviewer)
    parent = WorkPackage(
        id="parent",
        title="Parent",
        requirements=["Complete child"],
        acceptance_criteria=[AcceptanceCriterion(id="works", description="Works")],
        affected_repositories=["repo"],
    )
    child = WorkPackage(
        id="child",
        title="Generated shard",
        requirements=["Do work"],
        acceptance_criteria=[AcceptanceCriterion(id="works", description="Works")],
        affected_repositories=["repo"],
        parent_id="parent",
        execution_mode="standard_shard",
        write_scope=["repo/src/allowed.cpp"],
        stage=WorkPackageStage.IMPLEMENT,
        reviewer_id="reviewer",
    )
    graph, report = normalize_work_packages([parent, child])
    orch.initialize_graph(graph, report)
    orch.transition_to(TaskExecutionState.RUNNING)
    child = orch._state_record.plan_graph.package_by_id("child")
    child.stage = WorkPackageStage.IMPLEMENT
    child.reviewer_id = "reviewer"
    (repo / "CMakeLists.txt").write_text(
        "add_library(fixture src/allowed.cpp)\nadd_subdirectory(tests)\n"
    )
    orch._scope_recovery_coordinator.escalate_scope_failure(
        child,
        "generated shard modified paths outside write_scope: repo:CMakeLists.txt",
    )

    assert orch._scope_recovery_coordinator.auto_recover_scope_check_unlocked() is False
    assert orch.state == TaskExecutionState.WAITING_FOR_AGENT
    waiting = orch._scope_recovery_coordinator.pending_scope_recovery_wait()
    assert waiting["scope_recovery"]["attempt"] == 1

    assert orch._scope_recovery_coordinator.resume_pending_scope_recovery_unlocked(waiting) is True

    assert orch.state == TaskExecutionState.RUNNING
    assert child.stage == WorkPackageStage.FAST_VERIFY
    assert "repo/CMakeLists.txt" in child.write_scope
    assert reviewer.calls == ["scope_recovery", "scope_recovery"]


def test_agent_scope_recovery_cleans_artifacts_rebalances_review_and_commits(tmp_path):
    repo = _repo(tmp_path / "repo")
    (repo / "src").mkdir()
    (repo / "src" / "allowed.cpp").write_text("int allowed = 0;\n")
    (repo / "CMakeLists.txt").write_text("add_library(fixture src/allowed.cpp)\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "add fixture")
    before = _git(repo, "rev-parse", "HEAD")

    config = OrchestrationConfig(
        state_dir=tmp_path / "state",
        scope_policy=ScopePolicy(enabled=True),
        scope_recovery_policy=ScopeRecoveryPolicy(enabled=True),
    )
    orch = ProjectOrchestrator(
        "strict-task",
        config=config,
        repository_paths={"repo": repo},
        workspace_root=tmp_path,
    )
    implementer = _ScopeRecoveryAgent(
        "implementer", {AgentCapability.IMPLEMENT, AgentCapability.FIX_REVIEW}
    )
    recovery_reviewer = _ScopeRecoveryAgent(
        "recovery-reviewer", {AgentCapability.REVIEW, AgentCapability.FIX_REVIEW}
    )
    independent_reviewer = _ScopeRecoveryAgent(
        "independent-reviewer", {AgentCapability.REVIEW}
    )
    for agent in (implementer, recovery_reviewer, independent_reviewer):
        orch.register_agent(agent)

    parent = WorkPackage(
        id="parent",
        title="Parent",
        requirements=["Complete child"],
        acceptance_criteria=[
            AcceptanceCriterion(
                id="works", description="Works", verified=True, evidence="child"
            )
        ],
        affected_repositories=["repo"],
        execution_mode="aggregate",
        stage=WorkPackageStage.COMPLETED,
        status="completed",
    )
    child = WorkPackage(
        id="child",
        title="Generated shard",
        requirements=["Implement route-neutral fixture"],
        acceptance_criteria=[
            AcceptanceCriterion(
                id="works",
                description="Works",
                verified=True,
                evidence="scope recovery fixture",
            )
        ],
        affected_repositories=["repo"],
        parent_id="parent",
        execution_mode="standard_shard",
        write_scope=["repo/src/allowed.cpp"],
        stage=WorkPackageStage.IMPLEMENT,
        status="running",
        agent_id="implementer",
        reviewer_id="recovery-reviewer",
        final_reviewer_id="independent-reviewer",
    )
    graph, report = normalize_work_packages([parent, child])
    orch.initialize_graph(graph, report)
    orch.transition_to(TaskExecutionState.RUNNING)
    child = orch._state_record.plan_graph.package_by_id("child")
    child.stage = WorkPackageStage.IMPLEMENT
    child.status = "running"
    child.agent_id = "implementer"
    child.reviewer_id = "recovery-reviewer"
    child.final_reviewer_id = "independent-reviewer"

    (repo / "src" / "allowed.cpp").write_text("int allowed = 1;\n")
    (repo / "CMakeLists.txt").write_text(
        "add_library(fixture src/allowed.cpp)\nadd_subdirectory(tests)\n"
    )
    (repo / "tests" / "__pycache__").mkdir(parents=True)
    (repo / "tests" / "test_route.cpp").write_text("// route-neutral test\n")
    pyc = repo / "tests" / "__pycache__" / "generated.pyc"
    pyc.write_bytes(b"\0generated-cache")

    orch._scope_recovery_coordinator.validate_declared_write_scope(child)

    assert not pyc.exists()
    assert "repo/CMakeLists.txt" in child.write_scope
    assert "repo/tests/test_route.cpp" in child.write_scope
    assert child.last_fixer_id == "recovery-reviewer"
    assert child.reviewer_id == ""
    assert recovery_reviewer.calls == ["scope_recovery"]

    schedule = orch._ensure_review_assignments(child)
    assert schedule.reviewer_id == "independent-reviewer"
    assert schedule.reviewer_id != child.last_fixer_id

    child.stage = WorkPackageStage.READY_TO_COMMIT
    orch._process_package(child)

    assert child.stage == WorkPackageStage.COMPLETED
    assert _git(repo, "rev-parse", "HEAD") != before
    assert _git(repo, "status", "--porcelain") == ""
    assert "tests/__pycache__/generated.pyc" not in _git(
        repo, "show", "--name-only", "--format=", "HEAD"
    )
    events = [entry.event_type for entry in orch._journal.read()]
    assert "scope_recovery_artifacts_removed" in events
    assert "scope_recovery_completed" in events


def test_clean_start_removes_root_pytest_cache_artifact(tmp_path):
    repo = _repo(tmp_path / "repo")
    cache_file = repo / ".pytest_cache" / "v" / "cache" / "nodeids"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_text("[]\n")
    config = OrchestrationConfig(
        state_dir=tmp_path / "state",
        scope_recovery_policy=ScopeRecoveryPolicy(enabled=True),
    )
    orch = ProjectOrchestrator(
        "strict-task",
        config=config,
        repository_paths={"repo": repo},
        workspace_root=tmp_path,
    )
    package = WorkPackage(
        id="package",
        title="Package",
        requirements=["Do work"],
        acceptance_criteria=[AcceptanceCriterion(id="works", description="Works")],
        affected_repositories=["repo"],
    )

    orch._validate_clean_start(package)

    assert not cache_file.exists()
    assert not (repo / ".pytest_cache").exists()


def test_agent_scope_recovery_cannot_approve_protected_paths(tmp_path):
    repo = _repo(tmp_path / "repo")
    config = OrchestrationConfig(
        state_dir=tmp_path / "state",
        scope_policy=ScopePolicy(enabled=True),
        scope_recovery_policy=ScopeRecoveryPolicy(enabled=True),
    )
    orch = ProjectOrchestrator(
        "strict-task",
        config=config,
        repository_paths={"repo": repo},
        workspace_root=tmp_path,
    )

    class _UnsafeRecoveryAgent(_ScopeRecoveryAgent):
        def execute(self, handoff):
            self.calls.append(handoff.stage)
            return {
                "ok": True,
                "status": "resolved",
                "summary": "attempted unsafe approval",
                "retain_paths": ["repo:pyproject.toml"],
                "removed_paths": [],
            }

    reviewer = _UnsafeRecoveryAgent(
        "reviewer", {AgentCapability.REVIEW, AgentCapability.FIX_REVIEW}
    )
    orch.register_agent(reviewer)
    parent = WorkPackage(
        id="parent",
        title="Parent",
        requirements=["Complete child"],
        acceptance_criteria=[AcceptanceCriterion(id="works", description="Works")],
        affected_repositories=["repo"],
    )
    child = WorkPackage(
        id="child",
        title="Generated shard",
        requirements=["Do work"],
        acceptance_criteria=[AcceptanceCriterion(id="works", description="Works")],
        affected_repositories=["repo"],
        parent_id="parent",
        execution_mode="standard_shard",
        write_scope=["repo/allowed.py"],
        stage=WorkPackageStage.IMPLEMENT,
        reviewer_id="reviewer",
    )
    graph, report = normalize_work_packages([parent, child])
    orch.initialize_graph(graph, report)
    orch.transition_to(TaskExecutionState.RUNNING)
    child = orch._state_record.plan_graph.package_by_id("child")
    child.stage = WorkPackageStage.IMPLEMENT
    child.reviewer_id = "reviewer"
    (repo / "pyproject.toml").write_text("[project]\nname='unsafe'\n")

    with pytest.raises(Exception):
        orch._scope_recovery_coordinator.validate_declared_write_scope(child)

    assert orch.state == TaskExecutionState.HUMAN_REQUIRED
    assert "repo/pyproject.toml" not in child.write_scope
    assert reviewer.calls == ["scope_recovery"]


def _ready_noop_package(repository_id: str = "repo") -> WorkPackage:
    return WorkPackage(
        id="noop",
        title="Verified no-op",
        requirements=["Preserve the already-satisfied behavior"],
        acceptance_criteria=[
            AcceptanceCriterion(
                id="works",
                description="Behavior remains verified",
                verified=True,
                evidence="verification and final review already passed",
            )
        ],
        affected_repositories=[repository_id],
        stage=WorkPackageStage.READY_TO_COMMIT,
        status="running",
    )


def test_verified_noop_commit_records_transaction_without_weakening_default_gate(tmp_path):
    repo = _repo(tmp_path / "repo")
    before = _git(repo, "rev-parse", "HEAD")
    package = _ready_noop_package()
    graph, report = normalize_work_packages([package])
    orch = ProjectOrchestrator(
        "noop-task",
        config=OrchestrationConfig(
            state_dir=tmp_path / "state",
            allow_verified_noop_commits=True,
        ),
        repository_paths={"repo": repo},
        workspace_root=tmp_path,
    )
    orch.initialize_graph(graph, report)
    orch.transition_to(TaskExecutionState.RUNNING)
    package = orch._state_record.plan_graph.package_by_id("noop")

    orch._process_package(package)

    assert package.stage == WorkPackageStage.COMPLETED
    assert _git(repo, "rev-parse", "HEAD") == before
    transactions = orch._commit_journal._all()
    assert len(transactions) == 1
    assert transactions[0].status == "committed"
    assert transactions[0].pre_snapshots[0].head_commit == before
    assert transactions[0].post_snapshots[0].head_commit == before
    assert "verified_noop_commit" in [entry.event_type for entry in orch._journal.read()]


def test_empty_commit_check_auto_reconciles_only_for_clean_verified_noop(tmp_path):
    repo = _repo(tmp_path / "repo")
    package = _ready_noop_package()
    graph, report = normalize_work_packages([package])
    orch = ProjectOrchestrator(
        "noop-task",
        config=OrchestrationConfig(
            state_dir=tmp_path / "state",
            allow_verified_noop_commits=True,
        ),
        repository_paths={"repo": repo},
        workspace_root=tmp_path,
    )
    orch.initialize_graph(graph, report)
    orch.transition_to(TaskExecutionState.RUNNING)
    package = orch._state_record.plan_graph.package_by_id("noop")
    orch._escalate_commit_failure(
        package, "no changes detected in affected repositories"
    )

    assert orch.can_auto_reconcile_verified_noop_commit() is True
    orch.run_pipeline()

    assert orch.state == TaskExecutionState.COMPLETED
    events = [entry.event_type for entry in orch._journal.read()]
    assert "verified_noop_commit_check_reconciled" in events
    assert "verified_noop_commit" in events


def test_verified_noop_auto_reconcile_refuses_dirty_repository(tmp_path):
    repo = _repo(tmp_path / "repo")
    package = _ready_noop_package()
    graph, report = normalize_work_packages([package])
    orch = ProjectOrchestrator(
        "noop-task",
        config=OrchestrationConfig(
            state_dir=tmp_path / "state",
            allow_verified_noop_commits=True,
        ),
        repository_paths={"repo": repo},
        workspace_root=tmp_path,
    )
    orch.initialize_graph(graph, report)
    orch.transition_to(TaskExecutionState.RUNNING)
    package = orch._state_record.plan_graph.package_by_id("noop")
    orch._escalate_commit_failure(
        package, "no changes detected in affected repositories"
    )
    (repo / "unexpected.txt").write_text("dirty\n")

    assert orch.can_auto_reconcile_verified_noop_commit() is False


class _CrossRepositoryRecoveryAgent:
    def __init__(
        self,
        provider_id: str,
        *,
        retain: list[str] | None = None,
        discard: list[str] | None = None,
        delete: Path | None = None,
        create_artifact: Path | None = None,
    ):
        self._provider_id = provider_id
        self._retain = list(retain or [])
        self._discard = list(discard or [])
        self._delete = delete
        self._create_artifact = create_artifact
        self.calls: list[str] = []

    @property
    def provider_id(self):
        return self._provider_id

    @property
    def availability(self):
        return Availability.AVAILABLE

    @property
    def capabilities(self):
        return {AgentCapability.REVIEW, AgentCapability.FIX_REVIEW}

    def execute(self, handoff):
        self.calls.append(handoff.stage)
        assert handoff.stage == "scope_recovery"
        assert "relationship=undeclared_repository" in "\n".join(
            handoff.unresolved_findings
        )
        if self._delete is not None:
            self._delete.unlink()
        if self._create_artifact is not None:
            self._create_artifact.parent.mkdir(parents=True, exist_ok=True)
            self._create_artifact.write_bytes(b"generated cache")
        return {
            "ok": True,
            "status": "resolved",
            "summary": "resolved the cross-repository workspace delta",
            "retain_paths": self._retain,
            "discard_paths": self._discard,
        }


def test_ready_to_commit_recovery_acquires_foreign_repository_and_rewinds(tmp_path):
    repo = _repo(tmp_path / "repo")
    other = _repo(tmp_path / "other")
    (repo / "allowed.py").write_text("value = 0\n")
    _git(repo, "add", "allowed.py")
    _git(repo, "commit", "-q", "-m", "fixture")
    config = OrchestrationConfig(
        state_dir=tmp_path / "state",
        scope_recovery_policy=ScopeRecoveryPolicy(
            enabled=True,
            allow_cross_repository=True,
        ),
    )
    orch = ProjectOrchestrator(
        "strict-task",
        config=config,
        repository_paths={"repo": repo, "other": other},
        workspace_root=tmp_path,
    )
    recovery = _CrossRepositoryRecoveryAgent(
        "reviewer",
        retain=["other:adapter.py"],
    )
    orch.register_agent(recovery)
    package = WorkPackage(
        id="package",
        title="Cross repository recovery",
        requirements=["Keep the provider adapter change"],
        acceptance_criteria=[
            AcceptanceCriterion(
                id="works",
                description="Works",
                verified=True,
                evidence="verified before scope recovery",
            )
        ],
        affected_repositories=["repo"],
        write_scope=["repo/allowed.py"],
        parent_id="parent",
        execution_mode="standard_shard",
        stage=WorkPackageStage.READY_TO_COMMIT,
        status="running",
        reviewer_id="reviewer",
    )
    parent = WorkPackage(
        id="parent",
        title="Parent",
        requirements=["Coordinate"],
        acceptance_criteria=[
            AcceptanceCriterion(id="done", description="Done", verified=True)
        ],
        affected_repositories=["repo"],
        stage=WorkPackageStage.COMPLETED,
        status="completed",
    )
    graph, report = normalize_work_packages([parent, package])
    orch.initialize_graph(graph, report)
    orch.transition_to(TaskExecutionState.RUNNING)
    package = orch._state_record.plan_graph.package_by_id("package")
    package.stage = WorkPackageStage.READY_TO_COMMIT
    package.status = "running"
    package.reviewer_id = "reviewer"
    (repo / "allowed.py").write_text("value = 1\n")
    (other / "adapter.py").write_text("ROUTE_NEUTRAL = True\n")

    assert orch._scope_recovery_coordinator.prepare_commit_scope(package) is False

    assert package.stage == WorkPackageStage.REGRESSION_VERIFY
    assert package.affected_repositories == ["repo", "other"]
    assert "other/adapter.py" in package.write_scope
    assert package.last_fixer_id == "reviewer"
    assert package.parallel_safe is False
    assert orch._scope_recovery_coordinator.workspace_recovery_candidates(package) == []
    assert recovery.calls == ["scope_recovery"]

    # After the normal regression verification/final review cycle, the existing
    # commit transaction owns both repositories and leaves both clean.
    package.stage = WorkPackageStage.READY_TO_COMMIT
    tx = orch._begin_commit_transaction(package)
    assert tx is not None
    orch._commit_journal.commit(tx.transaction_id)
    assert _git(repo, "status", "--porcelain") == ""
    assert _git(other, "status", "--porcelain") == ""
    assert _git(other, "show", "HEAD:adapter.py") == "ROUTE_NEUTRAL = True"


def test_persisted_scope_check_auto_recovers_foreign_repository(tmp_path):
    repo = _repo(tmp_path / "repo")
    other = _repo(tmp_path / "other")
    config = OrchestrationConfig(
        state_dir=tmp_path / "state",
        scope_recovery_policy=ScopeRecoveryPolicy(enabled=True, auto_resume=True),
    )
    orch = ProjectOrchestrator(
        "strict-task",
        config=config,
        repository_paths={"repo": repo, "other": other},
        workspace_root=tmp_path,
    )
    recovery = _CrossRepositoryRecoveryAgent(
        "reviewer",
        retain=["other:adapter.py"],
    )
    orch.register_agent(recovery)
    package = WorkPackage(
        id="package",
        title="Persisted recovery",
        requirements=["Recover"],
        acceptance_criteria=[AcceptanceCriterion(id="works", description="Works")],
        affected_repositories=["repo"],
        stage=WorkPackageStage.READY_TO_COMMIT,
        status="running",
        reviewer_id="reviewer",
    )
    graph, report = normalize_work_packages([package])
    orch.initialize_graph(graph, report)
    orch.transition_to(TaskExecutionState.RUNNING)
    package = orch._state_record.plan_graph.package_by_id("package")
    package.stage = WorkPackageStage.READY_TO_COMMIT
    package.status = "running"
    package.reviewer_id = "reviewer"
    (other / "adapter.py").write_text("ROUTE_NEUTRAL = True\n")
    orch._scope_recovery_coordinator.escalate_scope_failure(
        package,
        "agent modified repositories outside the declared scope: other",
    )

    status = orch.declared_write_scope_report("package")["auto_recovery"]
    assert status["can_recover"] is True
    assert status["violations"] == ["other:adapter.py"]
    report = orch.declared_write_scope_report("package")
    assert report["workspace_scope"]["candidates"] == [
        {
            "path": "other:adapter.py",
            "repository_id": "other",
            "relative_path": "adapter.py",
            "relationship": "undeclared_repository",
        }
    ]
    assert orch.can_auto_resume_human_required() is True

    assert orch._scope_recovery_coordinator.auto_recover_scope_check_unlocked() is True

    assert orch.state == TaskExecutionState.RUNNING
    assert package.status == "pending"
    assert package.stage == WorkPackageStage.REGRESSION_VERIFY
    assert package.affected_repositories == ["repo", "other"]
    assert "other/adapter.py" in package.write_scope


def test_operator_scope_approval_uses_same_cross_repository_ownership_path(tmp_path):
    repo = _repo(tmp_path / "repo")
    other = _repo(tmp_path / "other")
    orch = _orchestrator(tmp_path, {"repo": repo, "other": other})
    package = WorkPackage(
        id="package",
        title="Operator fallback",
        requirements=["Adopt the legitimate adapter change"],
        acceptance_criteria=[AcceptanceCriterion(id="works", description="Works")],
        affected_repositories=["repo"],
        write_scope=["repo/allowed.py"],
        parent_id="parent",
        execution_mode="standard_shard",
        stage=WorkPackageStage.READY_TO_COMMIT,
        status="running",
    )
    parent = WorkPackage(
        id="parent",
        title="Parent",
        requirements=["Coordinate"],
        acceptance_criteria=[AcceptanceCriterion(id="done", description="Done")],
        affected_repositories=["repo"],
        stage=WorkPackageStage.COMPLETED,
        status="completed",
    )
    graph, report = normalize_work_packages([parent, package])
    orch.initialize_graph(graph, report)
    orch.transition_to(TaskExecutionState.RUNNING)
    package = orch._state_record.plan_graph.package_by_id("package")
    package.stage = WorkPackageStage.READY_TO_COMMIT
    package.status = "running"
    (other / "adapter.py").write_text("ROUTE_NEUTRAL = True\n")
    orch._scope_recovery_coordinator.escalate_scope_failure(
        package,
        "workspace contains changes outside the package ownership: other:adapter.py",
    )

    result = orch.approve_declared_write_scope("package")

    assert result["added_repositories"] == ["other"]
    assert result["added_paths"] == ["other/adapter.py"]
    assert result["next_stage"] == "regression_verify"
    assert package.affected_repositories == ["repo", "other"]
    assert package.write_scope == ["repo/allowed.py", "other/adapter.py"]
    assert package.status == "pending"
    assert package.last_fixer_id == ""
    assert orch.state == TaskExecutionState.RUNNING


def test_cross_repository_recovery_can_discard_accidental_file(tmp_path):
    repo = _repo(tmp_path / "repo")
    other = _repo(tmp_path / "other")
    accidental = other / "accidental.txt"
    config = OrchestrationConfig(
        state_dir=tmp_path / "state",
        scope_recovery_policy=ScopeRecoveryPolicy(enabled=True),
    )
    orch = ProjectOrchestrator(
        "strict-task",
        config=config,
        repository_paths={"repo": repo, "other": other},
        workspace_root=tmp_path,
    )
    recovery = _CrossRepositoryRecoveryAgent(
        "reviewer",
        discard=["other:accidental.txt"],
        delete=accidental,
    )
    orch.register_agent(recovery)
    package = WorkPackage(
        id="package",
        title="Discard accidental delta",
        requirements=["Recover"],
        acceptance_criteria=[AcceptanceCriterion(id="works", description="Works")],
        affected_repositories=["repo"],
        stage=WorkPackageStage.READY_TO_COMMIT,
        reviewer_id="reviewer",
    )
    graph, report = normalize_work_packages([package])
    orch.initialize_graph(graph, report)
    orch.transition_to(TaskExecutionState.RUNNING)
    package = orch._state_record.plan_graph.package_by_id("package")
    package.stage = WorkPackageStage.READY_TO_COMMIT
    package.reviewer_id = "reviewer"
    accidental.write_text("temporary\n")

    assert orch._scope_recovery_coordinator.prepare_commit_scope(package) is False

    assert not accidental.exists()
    assert package.affected_repositories == ["repo"]
    assert package.write_scope == []
    assert package.stage == WorkPackageStage.REGRESSION_VERIFY
    assert package.status == "pending"
    assert orch._scope_recovery_coordinator.workspace_recovery_candidates(package) == []


def test_workspace_recovery_removes_cache_created_by_fixer(tmp_path):
    repo = _repo(tmp_path / "repo")
    other = _repo(tmp_path / "other")
    generated_cache = repo / "__pycache__" / "recovery.cpython-310.pyc"
    config = OrchestrationConfig(
        state_dir=tmp_path / "state",
        scope_recovery_policy=ScopeRecoveryPolicy(enabled=True),
    )
    orch = ProjectOrchestrator(
        "strict-task",
        config=config,
        repository_paths={"repo": repo, "other": other},
        workspace_root=tmp_path,
    )
    recovery = _CrossRepositoryRecoveryAgent(
        "reviewer",
        retain=["other:adapter.py"],
        create_artifact=generated_cache,
    )
    orch.register_agent(recovery)
    package = WorkPackage(
        id="package",
        title="Cleanup fixer artifacts",
        requirements=["Recover"],
        acceptance_criteria=[AcceptanceCriterion(id="works", description="Works")],
        affected_repositories=["repo"],
        stage=WorkPackageStage.READY_TO_COMMIT,
        reviewer_id="reviewer",
    )
    graph, report = normalize_work_packages([package])
    orch.initialize_graph(graph, report)
    orch.transition_to(TaskExecutionState.RUNNING)
    package = orch._state_record.plan_graph.package_by_id("package")
    package.stage = WorkPackageStage.READY_TO_COMMIT
    package.reviewer_id = "reviewer"
    (other / "adapter.py").write_text("ROUTE_NEUTRAL = True\n")

    assert orch._scope_recovery_coordinator.prepare_commit_scope(package) is False

    assert not generated_cache.exists()
    assert not generated_cache.parent.exists()
    assert package.stage == WorkPackageStage.REGRESSION_VERIFY
    assert package.status == "pending"
    assert package.affected_repositories == ["repo", "other"]
    assert orch._scope_recovery_coordinator.workspace_recovery_candidates(package) == []
    removed_events = [
        entry
        for entry in orch._journal.read()
        if entry.event_type == "scope_recovery_artifacts_removed"
    ]
    assert removed_events[-1].payload["source"] == "workspace_recovery_post_agent"
