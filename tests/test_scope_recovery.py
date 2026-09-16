"""Focused unit and regression tests for ScopeRecoveryCoordinator."""

import json
import subprocess
from pathlib import Path
import pytest

from execraft.orchestrate.orchestrator import OrchestrationConfig, ProjectOrchestrator
from execraft.orchestrate.scope_recovery import ScopeRecoveryCoordinator
from execraft.orchestrate.models import WorkPackage, WorkPackageStage
from execraft.orchestrate.scope_policy import ScopeRecoveryPolicy


def _make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)
    (path / "README.md").write_text("test repo\n")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    return path


def test_scope_recovery_coordinator_initialization(tmp_path):
    repo_dir = _make_repo(tmp_path / "repo")
    config = OrchestrationConfig(
        state_dir=tmp_path / ".execraft",
    )
    orch = ProjectOrchestrator("test_project", config)
    orch._repository_paths = {"Execraft": repo_dir}
    assert isinstance(orch._scope_recovery_coordinator, ScopeRecoveryCoordinator)
    assert orch._scope_recovery_coordinator._host is orch


def test_scope_recovery_coordinator_scope_enforcement_and_snapshot(tmp_path):
    repo_dir = _make_repo(tmp_path / "repo")
    config = OrchestrationConfig(
        state_dir=tmp_path / ".execraft",
    )
    orch = ProjectOrchestrator("test_project", config)
    orch._repository_paths = {"Execraft": repo_dir}
    pkg = WorkPackage(
        id="WP01-S1",
        title="Test Package",
        parent_id="WP01",
        write_scope=["Execraft:src/foo.py"],
        affected_repositories=["Execraft"],
        stage=WorkPackageStage.IMPLEMENT,
    )

    coordinator = orch._scope_recovery_coordinator
    assert coordinator._enforces_declared_write_scope(pkg) is True

    snapshot = coordinator.workspace_scope_snapshot(pkg)
    assert snapshot.candidate_paths == []
    assert coordinator.workspace_recovery_candidates(pkg) == []
    assert coordinator.declared_write_scope_violations(pkg) == []


def test_scope_recovery_coordinator_untracked_artifact_cleanup(tmp_path):
    """Verify provenance-based cleanup: removes known artifacts, escalates unknown."""
    repo_dir = _make_repo(tmp_path / "repo")
    known_artifact = repo_dir / "known.tmp"
    unknown_artifact = repo_dir / "unknown.tmp"
    known_artifact.write_text("test", encoding="utf-8")
    unknown_artifact.write_text("test", encoding="utf-8")

    config = OrchestrationConfig(
        state_dir=tmp_path / ".execraft",
        scope_recovery_policy=ScopeRecoveryPolicy(
            enabled=True,
            cleanup_untracked_artifacts=True,
            cleanup_patterns=["*.tmp"],
        ),
    )
    orch = ProjectOrchestrator("test_project", config)
    orch._repository_paths = {"Execraft": repo_dir}
    pkg = WorkPackage(
        id="WP01-S1",
        title="Test Package",
        write_scope=["Execraft:src/main.py"],
        affected_repositories=["Execraft"],
        stage=WorkPackageStage.IMPLEMENT,
    )

    coordinator = orch._scope_recovery_coordinator
    # Simulate provenance emitted by an active invocation.
    provenance = coordinator._provenance_dir / "test_invocation.json"
    provenance.write_text(json.dumps(["Execraft:known.tmp"]), encoding="utf-8")

    # Mock active invocation
    orch._get_active_invocation_id = lambda pkg_id: "test_invocation"

    removed = coordinator.cleanup_scope_artifacts(
        pkg,
        ["Execraft:known.tmp", "Execraft:unknown.tmp"],
        source="supervisor_preflight",
    )
    # Known artifact removed
    assert "Execraft:known.tmp" in removed
    assert not known_artifact.exists()
    # Unknown artifact preserved
    assert "Execraft:unknown.tmp" not in removed
    assert unknown_artifact.exists()


def test_scope_recovery_coordinator_supervisor_recovery_predicates(tmp_path):
    repo_dir = _make_repo(tmp_path / "repo")
    config = OrchestrationConfig(
        state_dir=tmp_path / ".execraft",
    )
    orch = ProjectOrchestrator("test_project", config)
    orch._repository_paths = {"Execraft": repo_dir}
    coordinator = orch._scope_recovery_coordinator

    assert coordinator.supervisor_recovery_incident() is None
    assert coordinator.pending_supervisor_delegation_recovery() is None
    assert coordinator.can_auto_resume_lost_supervisor_delegation() is False
