"""Stable context identity and typed workspace snapshot coverage."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from execraft.orchestrate.context import AgentContextAssembler, GitSnapshot
from execraft.orchestrate.context_capsule import PackageContextCapsuleStore
from execraft.orchestrate.invocations import AgentInvocationStore
from execraft.orchestrate.journal import EventJournal
from execraft.orchestrate.models import AcceptanceCriterion, WorkPackage
from execraft.orchestrate.orchestrator import OrchestrationConfig, ProjectOrchestrator
from execraft.orchestrate.scheduler import AgentCapability, Availability, StructuredHandoff


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=path, check=True)
    return path


def _package() -> WorkPackage:
    return WorkPackage(
        id="WP02",
        title="Stable context identity",
        requirements=["Remove dynamic attempt history from stable capsule identity."],
        acceptance_criteria=[
            AcceptanceCriterion(
                id="wp02_context_stability",
                description="Unchanged model-visible context retains a stable digest",
            )
        ],
        affected_repositories=["core"],
        read_scope=["core:src/**"],
        write_scope=["core:src/**"],
    )


class _MutatingAdapter:
    def __init__(self, repository: Path) -> None:
        self.repository = repository

    @property
    def provider_id(self) -> str:
        return "mutating-provider"

    @property
    def availability(self) -> Availability:
        return Availability.AVAILABLE

    @property
    def capabilities(self) -> set[AgentCapability]:
        return {AgentCapability.IMPLEMENT}

    def execute(self, handoff: StructuredHandoff) -> dict:
        (self.repository / "provider-change.txt").write_text(
            "changed by provider\n", encoding="utf-8"
        )
        return {"ok": True, "work_package_id": handoff.work_package_id}


def test_capsule_digest_stable_without_attempt_history(tmp_path: Path) -> None:
    """Verify that attempt history does not pollute the capsule digest."""
    dossier = tmp_path / "dossier"
    dossier.mkdir()
    (dossier / "PLAN.md").write_text(
        "# Plan\n\n## WP02\n\n### Objective\n\nStable context.\n",
        encoding="utf-8",
    )
    invocations = AgentInvocationStore(tmp_path / "state" / "invocations.sqlite3")
    store = PackageContextCapsuleStore(
        dossier_dir=dossier,
        output_dir=tmp_path / "state" / "context",
        invocations=invocations,
        project_id="task",
    )

    # Generate capsule before any invocations
    first, _ = store.generate(_package())

    # Record some invocations
    invocations.begin(
        project_id="task",
        task_id="task",
        package_id="WP02",
        invocation_id="inv-001",
        stage="implement",
        capability="implement",
        attempt=1,
        agent_id="test",
        adapter="test-adapter",
        model="test-model",
        parent_invocation_id="",
        triggering_event_id="",
        handoff={},
        skills=[],
        isolation={},
        workspace_before_digest="",
    )
    invocations.complete(
        invocation_id="inv-001",
        duration_seconds=1.0,
        workspace_after_digest="",
        result_artifact={},
        normalized_result={},
        validation_errors=[],
        usage={},
    )
    invocations.begin(
        project_id="task",
        task_id="task",
        package_id="WP02",
        invocation_id="inv-002",
        stage="implement",
        capability="implement",
        attempt=2,
        agent_id="test",
        adapter="test-adapter",
        model="test-model",
        parent_invocation_id="",
        triggering_event_id="",
        handoff={},
        skills=[],
        isolation={},
        workspace_before_digest="",
    )
    invocations.fail(
        invocation_id="inv-002",
        duration_seconds=2.0,
        workspace_after_digest="",
        validation_errors=[],
        failure={"classification": "timeout", "error": "timeout"},
        usage={},
    )

    # Generate capsule after invocations
    second, _ = store.generate(_package())

    # Digest should be stable
    assert first.capsule_sha256 == second.capsule_sha256
    # Verify recent_invocations is NOT in latest_evidence
    assert "recent_invocations" not in first.latest_evidence
    assert "recent_invocations" not in second.latest_evidence


def test_git_snapshots_collected_once_per_mutation_boundary(tmp_path: Path) -> None:
    """Verify Git operations are executed once and cached."""
    repo = _git_repo(tmp_path / "repo")
    (repo / "new_file.txt").write_text("new content\n", encoding="utf-8")

    assembler = AgentContextAssembler(
        project_id="task",
        repository_paths={"core": repo},
        journal=EventJournal(tmp_path / "journal.json"),
        invocations=AgentInvocationStore(tmp_path / "invocations.sqlite3"),
        dossier_dir=None,
        context_dir=tmp_path / "context",
    )

    package = _package()

    # Mock subprocess.run to count calls
    original_run = subprocess.run
    call_count = {"count": 0}

    def counting_run(*args, **kwargs):
        call_count["count"] += 1
        return original_run(*args, **kwargs)

    with patch("subprocess.run", side_effect=counting_run):
        assembler.begin_workspace_measurement(package)
        calls_after_boundary = call_count["count"]

        # First call to workspace_digest
        digest1 = assembler.workspace_digest(package)
        calls_after_first = call_count["count"]

        # Second call to workspace_digest
        digest2 = assembler.workspace_digest(package)
        calls_after_second = call_count["count"]

        # Call workspace_summary
        summary = assembler.workspace_summary(package)
        calls_after_summary = call_count["count"]

    # Verify snapshots were collected only once
    assert calls_after_boundary == 3
    assert calls_after_first == calls_after_boundary
    assert calls_after_second == calls_after_first  # No new calls
    assert calls_after_summary == calls_after_first  # Still no new calls
    assert digest1 == digest2
    assert "new_file.txt" in summary


def test_git_failure_exposed_explicitly_in_snapshot(tmp_path: Path) -> None:
    """Verify Git command failures are captured and exposed in snapshots."""
    repo = _git_repo(tmp_path / "repo")

    assembler = AgentContextAssembler(
        project_id="task",
        repository_paths={"core": repo},
        journal=EventJournal(tmp_path / "journal.json"),
        invocations=AgentInvocationStore(tmp_path / "invocations.sqlite3"),
        dossier_dir=None,
        context_dir=tmp_path / "context",
    )

    package = _package()

    # Simulate Git failure by mocking subprocess.run
    def failing_git(*args, **kwargs):
        if "git" in args[0][0]:
            return subprocess.CompletedProcess(
                args=args[0],
                returncode=128,
                stdout="",
                stderr="fatal: not a git repository",
            )
        return subprocess.run(*args, **kwargs)

    with patch("subprocess.run", side_effect=failing_git):
        summary = assembler.workspace_summary(package)
        digest = assembler.workspace_digest(package)

    # Verify error is exposed in summary
    assert "Git error" in summary
    assert "fatal: not a git repository" in summary

    # Verify snapshot contains error
    snapshot = assembler._git_snapshots.get("core")
    assert snapshot is not None
    assert snapshot.error != ""
    assert "fatal: not a git repository" in snapshot.error

    # Verify digest still computed
    assert len(digest) == 64  # SHA256 hex


def test_status_only_git_failure_is_exposed_in_snapshot_and_summary(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    assembler = AgentContextAssembler(
        project_id="task",
        repository_paths={"core": repo},
        journal=EventJournal(tmp_path / "journal.json"),
        invocations=AgentInvocationStore(tmp_path / "invocations.sqlite3"),
        dossier_dir=None,
        context_dir=tmp_path / "context",
    )
    original_run = subprocess.run

    def status_only_failure(*args, **kwargs):
        command = args[0]
        if command[:3] == ["git", "status", "--porcelain=v1"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=128,
                stdout="",
                stderr="fatal: status probe failed",
            )
        return original_run(*args, **kwargs)

    with patch("subprocess.run", side_effect=status_only_failure):
        assembler.begin_workspace_measurement(_package())
        summary = assembler.workspace_summary(_package())

    snapshot = assembler._git_snapshots["core"]
    assert snapshot.head
    assert snapshot.stat == ""
    assert snapshot.error == "status: fatal: status probe failed"
    assert "Git error: status: fatal: status probe failed" in summary


def test_git_snapshot_as_mapping() -> None:
    """Verify GitSnapshot serialization."""
    snapshot = GitSnapshot(
        repository="core",
        head="abc123",
        status_porcelain="M file.txt\n",
        status_short="M file.txt\n",
        stat=" file.txt | 2 +-\n 1 file changed",
        error="",
    )
    mapping = snapshot.as_mapping()
    assert mapping == {
        "repository": "core",
        "head": "abc123",
        "status": "M file.txt\n",
        "stat": " file.txt | 2 +-\n 1 file changed",
        "error": "",
    }

    snapshot_with_error = GitSnapshot(
        repository="core",
        head="",
        status_porcelain="",
        status_short="",
        stat="",
        error="rev-parse: fatal: not a git repository",
    )
    mapping_with_error = snapshot_with_error.as_mapping()
    assert mapping_with_error["error"] == "rev-parse: fatal: not a git repository"


def test_workspace_digest_changes_when_git_state_changes(tmp_path: Path) -> None:
    """Verify workspace digest changes when repository state changes."""
    repo = _git_repo(tmp_path / "repo")

    assembler = AgentContextAssembler(
        project_id="task",
        repository_paths={"core": repo},
        journal=EventJournal(tmp_path / "journal.json"),
        invocations=AgentInvocationStore(tmp_path / "invocations.sqlite3"),
        dossier_dir=None,
        context_dir=tmp_path / "context",
    )

    package = _package()

    assembler.begin_workspace_measurement(package)
    digest1 = assembler.workspace_digest(package)

    (repo / "modified.txt").write_text("new\n", encoding="utf-8")

    assembler.begin_workspace_measurement(package)
    digest2 = assembler.workspace_digest(package)

    assert digest1 != digest2


def test_successive_package_boundaries_measure_only_their_repository_scope(
    tmp_path: Path,
) -> None:
    first_repo = _git_repo(tmp_path / "first")
    second_repo = _git_repo(tmp_path / "second")
    assembler = AgentContextAssembler(
        project_id="task",
        repository_paths={"first": first_repo, "second": second_repo},
        journal=EventJournal(tmp_path / "journal.json"),
        invocations=AgentInvocationStore(tmp_path / "invocations.sqlite3"),
        dossier_dir=None,
        context_dir=tmp_path / "context",
    )
    first_package = WorkPackage(id="first", title="First", affected_repositories=["first"])
    second_package = WorkPackage(
        id="second", title="Second", affected_repositories=["second"]
    )
    original_run = subprocess.run
    probed_cwds: list[Path] = []

    def record_probe(*args, **kwargs):
        probed_cwds.append(Path(kwargs["cwd"]))
        return original_run(*args, **kwargs)

    with patch("subprocess.run", side_effect=record_probe):
        assembler.begin_workspace_measurement(first_package)
        assembler.workspace_digest(first_package)
        assembler.workspace_summary(first_package)
        assembler.begin_workspace_measurement(second_package)
        assembler.workspace_digest(second_package)
        assembler.workspace_summary(second_package)

    assert probed_cwds == [first_repo] * 3 + [second_repo] * 3


def test_orchestrator_remeasures_workspace_after_provider_mutation(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    orchestrator = ProjectOrchestrator(
        "task",
        config=OrchestrationConfig(
            state_dir=tmp_path / "state",
            strict_checks=False,
            auto_commit=False,
        ),
        repository_paths={"core": repo},
    )
    adapter = _MutatingAdapter(repo)
    orchestrator.register_agent(adapter)
    package = _package()
    original_run = subprocess.run
    git_probe_count = 0

    def count_git_probes(*args, **kwargs):
        nonlocal git_probe_count
        if args[0][0] == "git":
            git_probe_count += 1
        return original_run(*args, **kwargs)

    with patch("subprocess.run", side_effect=count_git_probes):
        result = orchestrator._execute_agent(
            AgentCapability.IMPLEMENT,
            adapter.provider_id,
            StructuredHandoff(
                work_package_id=package.id,
                stage="implement",
                summary="Implement WP02",
            ),
            package,
        )

    record = orchestrator.invocation_history(package_id=package.id, limit=1)[0]
    assert result["ok"] is True
    assert record["workspace_before_digest"] != record["workspace_after_digest"]
    assert git_probe_count == 6
