"""Deterministic package/aggregate workspace-finalization tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from execraft.orchestrate.models import (
    AcceptanceCriterion,
    PlanGraph,
    TaskExecutionState,
    WorkPackage,
    WorkPackageStage,
)
from execraft.orchestrate.normalizer import NormalizationReport
from execraft.orchestrate.orchestrator import (
    OrchestrationConfig,
    ProjectOrchestrator,
    _StageEscalated,
)
from execraft.orchestrate.package_finalization import (
    WorkspaceFinalizationPolicy,
    assess_aggregate_children,
    workspace_finalization_policy_from_scheduling,
    workspace_path_fingerprints,
)


def _git(path: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=path,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    return completed.stdout.strip()


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "user.email", "test@example.invalid")
    (path / "README.md").write_text("base\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-q", "-m", "base")


def _package(
    package_id: str,
    repository_id: str,
    *,
    parent_id: str = "",
    execution_mode: str = "standard",
) -> WorkPackage:
    return WorkPackage(
        id=package_id,
        title=package_id,
        requirements=[f"Implement {package_id}"],
        acceptance_criteria=[
            AcceptanceCriterion(
                id=f"{package_id}-done",
                description=f"{package_id} is complete",
            )
        ],
        affected_repositories=[repository_id],
        stage=WorkPackageStage.READY_TO_COMMIT,
        parent_id=parent_id,
        execution_mode=execution_mode,
    )


def _orchestrator(
    tmp_path: Path,
    packages: list[WorkPackage],
    repositories: dict[str, Path],
    *,
    policy: WorkspaceFinalizationPolicy | None = None,
    allow_verified_noop: bool = False,
) -> ProjectOrchestrator:
    config = OrchestrationConfig(
        state_dir=tmp_path / "state",
        strict_checks=True,
        auto_commit=True,
        require_verification=False,
        require_acceptance_evidence=False,
        require_repository_changes=True,
        allow_verified_noop_commits=allow_verified_noop,
        workspace_finalization_policy=policy or WorkspaceFinalizationPolicy(),
    )
    orchestrator = ProjectOrchestrator(
        "finalization-test",
        config=config,
        repository_paths=repositories,
        workspace_root=tmp_path,
    )
    orchestrator.initialize_graph(
        PlanGraph(work_packages=packages), NormalizationReport()
    )
    orchestrator.transition_to(TaskExecutionState.RUNNING)
    return orchestrator


def _last_event(orchestrator: ProjectOrchestrator, event_type: str):
    return next(
        event
        for event in reversed(orchestrator._journal.read())
        if event.event_type == event_type
    )


def test_workspace_finalization_policy_requires_strict_booleans():
    with pytest.raises(ValueError, match="enabled must be a boolean"):
        workspace_finalization_policy_from_scheduling(
            {"workspace_finalization": {"enabled": "false"}}
        )


def test_standard_package_commits_and_removes_allow_listed_artifacts(tmp_path):
    repository = tmp_path / "repo"
    _init_repo(repository)
    package = _package("WP1", "repo")
    orchestrator = _orchestrator(tmp_path, [package], {"repo": repository})

    (repository / "README.md").write_text("changed\n", encoding="utf-8")
    cache = repository / ".pytest_cache" / "v" / "cache" / "nodeids"
    cache.parent.mkdir(parents=True)
    cache.write_text("[]\n", encoding="utf-8")

    orchestrator._process_package(package)

    assert package.stage == WorkPackageStage.COMPLETED
    assert _git(repository, "status", "--porcelain") == ""
    assert not cache.exists()
    assert _git(repository, "log", "-1", "--pretty=%s") == "execraft(WP1): WP1"
    committed_paths = set(_git(repository, "show", "--pretty=", "--name-only").splitlines())
    assert committed_paths == {"README.md"}
    transaction = orchestrator._commit_journal.last_by_work_package("WP1")
    assert transaction is not None and transaction.status == "committed"
    finalized = _last_event(orchestrator, "package_workspace_finalized")
    assert finalized.payload["transaction_id"] == transaction.transaction_id
    assert finalized.payload["removed_artifacts"] == [
        "repo:.pytest_cache/v/cache/nodeids"
    ]
    assert finalized.payload["cleanliness_enforced"] is True


def test_verified_noop_shard_has_commit_evidence_for_aggregate(tmp_path):
    repository = tmp_path / "repo"
    _init_repo(repository)
    shard = _package(
        "WP1__noop",
        "repo",
        parent_id="WP1",
        execution_mode="standard_shard",
    )
    parent = WorkPackage(
        id="WP1",
        title="Aggregate",
        affected_repositories=["repo"],
        stage=WorkPackageStage.READY_TO_COMMIT,
        execution_mode="aggregate",
        shard_ids=[shard.id],
    )
    orchestrator = _orchestrator(
        tmp_path,
        [parent, shard],
        {"repo": repository},
        allow_verified_noop=True,
    )

    orchestrator._process_package(shard)
    transaction = orchestrator._commit_journal.last_by_work_package(shard.id)
    assert transaction is not None and transaction.status == "committed"
    assert _last_event(orchestrator, "verified_noop_commit").payload[
        "transaction_id"
    ] == transaction.transaction_id

    orchestrator._process_package(parent)

    assert parent.stage == WorkPackageStage.COMPLETED
    assert _git(repository, "status", "--porcelain") == ""
    aggregate = _last_event(orchestrator, "aggregate_workspace_finalized")
    assert aggregate.payload["child_evidence"][0]["transaction_status"] == (
        "committed"
    )


def test_post_commit_hook_dirty_state_fails_finalization_without_supervisor(tmp_path, monkeypatch):
    repository = tmp_path / "repo"
    _init_repo(repository)
    hook = repository / ".git" / "hooks" / "post-commit"
    hook.write_text("#!/bin/sh\nprintf 'late\\n' > late.txt\n", encoding="utf-8")
    hook.chmod(0o755)
    package = _package("WP1", "repo")
    orchestrator = _orchestrator(tmp_path, [package], {"repo": repository})
    (repository / "README.md").write_text("changed\n", encoding="utf-8")

    with pytest.raises(_StageEscalated):
        orchestrator._process_package(package)

    assert package.stage == WorkPackageStage.READY_TO_COMMIT
    assert orchestrator.state == TaskExecutionState.HUMAN_REQUIRED
    failure = _last_event(orchestrator, "workspace_finalization_failed")
    assert failure.payload["qualified_paths"] == ["repo:late.txt"]
    action = _last_event(orchestrator, "human_intervention_required")
    assert action.payload["stage"] == "workspace_finalization"
    assert action.payload["supervisor_eligible"] is False

    monkeypatch.setattr(
        orchestrator._supervisor_coordinator, "supervisor_adapter", lambda: object()
    )
    monkeypatch.setattr(
        orchestrator,
        "_supervisor_package",
        lambda *_args, **_kwargs: pytest.fail("Supervisor must not inspect this incident"),
    )
    assert orchestrator._run_supervision_unlocked() is False


def test_parallel_standard_shards_commit_before_clean_aggregate_completion(tmp_path):
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    _init_repo(repo_a)
    _init_repo(repo_b)
    shard_a = _package(
        "WP1__a", "a", parent_id="WP1", execution_mode="standard_shard"
    )
    shard_b = _package(
        "WP1__b", "b", parent_id="WP1", execution_mode="standard_shard"
    )
    parent = WorkPackage(
        id="WP1",
        title="Aggregate",
        requirements=["Integrate both shards"],
        acceptance_criteria=[
            AcceptanceCriterion(id="aggregate", description="Both shards pass")
        ],
        affected_repositories=["a", "b"],
        stage=WorkPackageStage.READY_TO_COMMIT,
        execution_mode="aggregate",
        shard_ids=[shard_a.id, shard_b.id],
        dependencies=[shard_a.id, shard_b.id],
    )
    orchestrator = _orchestrator(
        tmp_path,
        [parent, shard_a, shard_b],
        {"a": repo_a, "b": repo_b},
    )
    orchestrator._set_parallel_dirty_owners({"a": shard_a.id, "b": shard_b.id})
    (repo_a / "a.txt").write_text("a\n", encoding="utf-8")
    (repo_b / "b.txt").write_text("b\n", encoding="utf-8")

    orchestrator._process_package(shard_a)
    assert orchestrator._parallel_dirty_owners() == {"b": shard_b.id}
    orchestrator._process_package(shard_b)
    assert orchestrator._parallel_dirty_owners() == {}

    generated = repo_a / ".pytest_cache" / "v" / "cache" / "lastfailed"
    generated.parent.mkdir(parents=True)
    generated.write_text("{}\n", encoding="utf-8")
    orchestrator._process_package(parent)

    assert parent.stage == WorkPackageStage.COMPLETED
    assert _git(repo_a, "status", "--porcelain") == ""
    assert _git(repo_b, "status", "--porcelain") == ""
    assert not generated.exists()
    assert orchestrator._commit_journal.last_by_work_package(parent.id) is None
    child_transactions = {
        item.work_package_id: item
        for item in orchestrator._commit_journal._all()
        if item.work_package_id in {shard_a.id, shard_b.id}
    }
    assert set(child_transactions) == {shard_a.id, shard_b.id}
    assert all(item.status == "committed" for item in child_transactions.values())
    aggregate = _last_event(orchestrator, "aggregate_workspace_finalized")
    assert aggregate.payload["transaction_id"] is None
    assert {
        item["package_id"] for item in aggregate.payload["child_evidence"]
    } == {shard_a.id, shard_b.id}
    completed = _last_event(orchestrator, "package_completed")
    assert completed.payload == {"package_id": "WP1", "transaction_id": None}


def test_aggregate_rejects_completed_child_without_commit_transaction(tmp_path):
    repository = tmp_path / "repo"
    _init_repo(repository)
    shard = _package(
        "WP1__a", "repo", parent_id="WP1", execution_mode="standard_shard"
    )
    shard.stage = WorkPackageStage.COMPLETED
    shard.status = "completed"
    parent = WorkPackage(
        id="WP1",
        title="Aggregate",
        affected_repositories=["repo"],
        stage=WorkPackageStage.READY_TO_COMMIT,
        execution_mode="aggregate",
        shard_ids=[shard.id],
    )
    orchestrator = _orchestrator(tmp_path, [parent, shard], {"repo": repository})

    with pytest.raises(_StageEscalated):
        orchestrator._process_package(parent)

    failure = _last_event(orchestrator, "workspace_finalization_failed")
    assert "lack committed transactions" in failure.payload["message"]
    assert failure.payload["evidence"]["uncommitted_child_ids"] == [shard.id]


def test_aggregate_rejects_active_parallel_ownership(tmp_path):
    repository = tmp_path / "repo"
    _init_repo(repository)
    shard = _package(
        "WP1__a", "repo", parent_id="WP1", execution_mode="review_shard"
    )
    shard.stage = WorkPackageStage.COMPLETED
    shard.status = "completed"
    parent = WorkPackage(
        id="WP1",
        title="Aggregate",
        affected_repositories=["repo"],
        stage=WorkPackageStage.READY_TO_COMMIT,
        execution_mode="aggregate",
        shard_ids=[shard.id],
    )
    orchestrator = _orchestrator(tmp_path, [parent, shard], {"repo": repository})
    orchestrator._set_parallel_dirty_owners({"repo": shard.id})

    with pytest.raises(_StageEscalated):
        orchestrator._process_package(parent)

    failure = _last_event(orchestrator, "workspace_finalization_failed")
    assert "parallel write ownership is still active" in failure.payload["message"]



def test_aggregate_rejects_active_parallel_wave(tmp_path):
    repository = tmp_path / "repo"
    _init_repo(repository)
    shard = _package(
        "WP1__review", "repo", parent_id="WP1", execution_mode="review_shard"
    )
    shard.stage = WorkPackageStage.COMPLETED
    shard.status = "completed"
    parent = WorkPackage(
        id="WP1",
        title="Aggregate",
        affected_repositories=["repo"],
        stage=WorkPackageStage.READY_TO_COMMIT,
        execution_mode="aggregate",
        shard_ids=[shard.id],
    )
    orchestrator = _orchestrator(tmp_path, [parent, shard], {"repo": repository})
    orchestrator._set_active_parallel_wave(
        {"wave_id": "wave-stale", "package_ids": [shard.id]}
    )

    with pytest.raises(_StageEscalated):
        orchestrator._process_package(parent)

    failure = _last_event(orchestrator, "workspace_finalization_failed")
    assert "parallel shard wave is still active" in failure.payload["message"]




def test_aggregate_fix_result_records_exact_repair_evidence(tmp_path):
    repository = tmp_path / "repo"
    _init_repo(repository)
    parent = WorkPackage(
        id="WP1",
        title="Aggregate",
        affected_repositories=["repo"],
        stage=WorkPackageStage.FIX_REVIEW,
        execution_mode="aggregate",
    )
    orchestrator = _orchestrator(tmp_path, [parent], {"repo": repository})
    (repository / "README.md").write_text("review fix\n", encoding="utf-8")

    orchestrator._apply_implementation_result(
        parent,
        {
            "ok": True,
            "status": "fixed",
            "summary": "fixed review finding",
            "acceptance_evidence": {},
            "_execraft_invocation_id": "fix-1",
            "_execraft_executed_by": "codex",
        },
    )

    event = _last_event(orchestrator, "aggregate_review_fix_delta")
    assert event.payload["qualified_paths"] == ["repo:README.md"]
    assert event.payload["invocation_id"] == "fix-1"
    assert event.payload["agent_id"] == "codex"
    assert event.payload["path_fingerprints"]["repo:README.md"]

def test_aggregate_commits_exact_review_fix_delta_after_child_commits(tmp_path):
    repository = tmp_path / "repo"
    _init_repo(repository)
    shard = _package(
        "WP1__a", "repo", parent_id="WP1", execution_mode="standard_shard"
    )
    parent = WorkPackage(
        id="WP1",
        title="Aggregate",
        affected_repositories=["repo"],
        stage=WorkPackageStage.READY_TO_COMMIT,
        execution_mode="aggregate",
        shard_ids=[shard.id],
    )
    orchestrator = _orchestrator(tmp_path, [parent, shard], {"repo": repository})
    (repository / "child.txt").write_text("child\n", encoding="utf-8")
    orchestrator._process_package(shard)

    (repository / "README.md").write_text("review fix\n", encoding="utf-8")
    dirty = orchestrator._workspace_dirty_paths({"repo"})
    orchestrator._journal.append(
        "aggregate_review_fix_delta",
        {
            "package_id": parent.id,
            "invocation_id": "fix-1",
            "agent_id": "codex",
            "qualified_paths": ["repo:README.md"],
            "path_fingerprints": workspace_path_fingerprints(
                orchestrator._repository_paths, dirty
            ),
        },
    )

    orchestrator._process_package(parent)

    assert parent.stage == WorkPackageStage.COMPLETED
    assert _git(repository, "status", "--porcelain") == ""
    assert _git(repository, "log", "-1", "--pretty=%s") == "execraft(WP1): Aggregate"
    parent_tx = orchestrator._commit_journal.last_by_work_package(parent.id)
    assert parent_tx is not None and parent_tx.status == "committed"
    committed = _last_event(orchestrator, "aggregate_review_fix_committed")
    assert committed.payload["transaction_id"] == parent_tx.transaction_id
    aggregate = _last_event(orchestrator, "aggregate_workspace_finalized")
    assert aggregate.payload["transaction_id"] == parent_tx.transaction_id


def test_aggregate_review_fix_delta_rejects_later_mutation_of_same_path(tmp_path):
    repository = tmp_path / "repo"
    _init_repo(repository)
    shard = _package(
        "WP1__a", "repo", parent_id="WP1", execution_mode="standard_shard"
    )
    parent = WorkPackage(
        id="WP1",
        title="Aggregate",
        affected_repositories=["repo"],
        stage=WorkPackageStage.READY_TO_COMMIT,
        execution_mode="aggregate",
        shard_ids=[shard.id],
    )
    orchestrator = _orchestrator(tmp_path, [parent, shard], {"repo": repository})
    (repository / "child.txt").write_text("child\n", encoding="utf-8")
    orchestrator._process_package(shard)

    (repository / "README.md").write_text("review fix\n", encoding="utf-8")
    dirty = orchestrator._workspace_dirty_paths({"repo"})
    orchestrator._journal.append(
        "aggregate_review_fix_delta",
        {
            "package_id": parent.id,
            "invocation_id": "fix-1",
            "agent_id": "codex",
            "qualified_paths": ["repo:README.md"],
            "path_fingerprints": workspace_path_fingerprints(
                orchestrator._repository_paths, dirty
            ),
        },
    )
    (repository / "README.md").write_text("later mutation\n", encoding="utf-8")

    with pytest.raises(_StageEscalated):
        orchestrator._process_package(parent)

    assert orchestrator._commit_journal.last_by_work_package(parent.id) is None
    failure = _last_event(orchestrator, "workspace_finalization_failed")
    assert failure.payload["qualified_paths"] == ["repo:README.md"]


def test_aggregate_recovers_pre_patch_fix_delta_from_invocation_ledger(tmp_path):
    repository = tmp_path / "repo"
    _init_repo(repository)
    shard = _package(
        "WP1__a", "repo", parent_id="WP1", execution_mode="standard_shard"
    )
    parent = WorkPackage(
        id="WP1",
        title="Aggregate",
        affected_repositories=["repo"],
        stage=WorkPackageStage.READY_TO_COMMIT,
        execution_mode="aggregate",
        shard_ids=[shard.id],
    )
    orchestrator = _orchestrator(tmp_path, [parent, shard], {"repo": repository})
    (repository / "child.txt").write_text("child\n", encoding="utf-8")
    orchestrator._process_package(shard)

    (repository / "README.md").write_text("legacy review fix\n", encoding="utf-8")
    orchestrator._context_assembler.begin_workspace_measurement(parent)
    after_digest = orchestrator._context_assembler.workspace_digest(parent)
    invocation = orchestrator._agent_invocations.begin(
        project_id=orchestrator.project_id,
        task_id=orchestrator.task_id,
        package_id=parent.id,
        stage=WorkPackageStage.FIX_REVIEW.value,
        capability="fix_review",
        attempt=1,
        agent_id="codex",
        handoff={},
    )
    orchestrator._agent_invocations.complete(
        invocation.invocation_id,
        duration_seconds=1.0,
        workspace_after_digest=after_digest,
    )
    parent.last_implementation = {
        "status": "fixed",
        "invocation_id": invocation.invocation_id,
        "agent_id": "codex",
    }

    orchestrator._process_package(parent)

    assert parent.stage == WorkPackageStage.COMPLETED
    recovered = _last_event(orchestrator, "aggregate_review_fix_delta_recovered")
    assert recovered.payload["invocation_id"] == invocation.invocation_id
    assert _git(repository, "status", "--porcelain") == ""

def test_aggregate_rejects_tracked_changes_created_after_child_commit(tmp_path):
    repository = tmp_path / "repo"
    _init_repo(repository)
    shard = _package(
        "WP1__a", "repo", parent_id="WP1", execution_mode="standard_shard"
    )
    parent = WorkPackage(
        id="WP1",
        title="Aggregate",
        affected_repositories=["repo"],
        stage=WorkPackageStage.READY_TO_COMMIT,
        execution_mode="aggregate",
        shard_ids=[shard.id],
    )
    orchestrator = _orchestrator(tmp_path, [parent, shard], {"repo": repository})
    (repository / "child.txt").write_text("child\n", encoding="utf-8")
    orchestrator._process_package(shard)
    (repository / "README.md").write_text("verification rewrote source\n", encoding="utf-8")

    with pytest.raises(_StageEscalated):
        orchestrator._process_package(parent)

    assert parent.stage == WorkPackageStage.READY_TO_COMMIT
    failure = _last_event(orchestrator, "workspace_finalization_failed")
    assert failure.payload["qualified_paths"] == ["repo:README.md"]
    assert "aggregate verification or review" in failure.payload["message"]
    assert orchestrator._commit_journal.last_by_work_package(parent.id) is None


def test_review_shards_do_not_require_commit_evidence():
    parent = WorkPackage(
        id="WP1",
        title="Aggregate",
        execution_mode="aggregate",
        shard_ids=["WP1__review"],
    )
    review = WorkPackage(
        id="WP1__review",
        title="Review",
        parent_id="WP1",
        execution_mode="review_shard",
        stage=WorkPackageStage.COMPLETED,
        status="completed",
    )

    assessment = assess_aggregate_children(
        parent,
        [parent, review],
        {review.id: None},
        require_committed_standard_shards=True,
    )

    assert assessment.ok is True
    assert assessment.children[0].commit_required is False
