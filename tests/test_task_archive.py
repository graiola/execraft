from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest
import yaml

from execraft.cli import main
from execraft.archive import TaskArchiveManager
from execraft.repository_sync.transaction import (
    RepositorySyncRepositoryState,
    RepositorySyncTransaction,
    RepositorySyncTransactionStore,
)
from execraft.workspace.task_git import RepositorySpec, TaskManifest, load_manifest, write_manifest
from execraft.workspace.workspace_git import WorkspaceRecord, write_workspace


def _git(path: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=path,
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout.strip()


def _init_git(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.name", "Test User")
    _git(path, "config", "user.email", "test@example.com")


def _commit_all(path: Path, message: str = "baseline") -> None:
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", message)


def _build_completed_task(tmp_path: Path) -> tuple[Path, Path, TaskManifest, Path]:
    control = tmp_path / "Execraft"
    _init_git(control)
    (control / "src" / "execraft").mkdir(parents=True)
    (control / "projects" / "sample").mkdir(parents=True)

    repo = tmp_path / "workspace" / "component"
    _init_git(repo)
    (repo / "component.txt").write_text("complete\n", encoding="utf-8")
    _commit_all(repo)
    _git(repo, "checkout", "-qb", "task/task-one")

    manifest = TaskManifest(
        schema_version=2,
        id="task-one",
        project="sample",
        title="Completed task",
        status="approved",
        created_at="2026-07-20T00:00:00+00:00",
        branch_name="task/task-one",
        repositories=[
            RepositorySpec(
                id="component",
                base_branch="master",
                task_branch="task/task-one",
                role="component",
                required=True,
                mutability="task_owned",
            )
        ],
    )
    write_manifest(control, manifest)
    dossier = control / "projects" / "sample" / "tasks" / "task-one"
    (dossier / "BRIEF.md").write_text("# Brief\n", encoding="utf-8")
    (dossier / "PLAN.md").write_text("# Plan\n", encoding="utf-8")
    (dossier / "PLAN.graph.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    _commit_all(control)

    workspace_root = tmp_path / "workspace"
    workspace = WorkspaceRecord(
        schema_version=1,
        task_id="task-one",
        created_at="2026-07-20T00:00:00+00:00",
        source_root=str(tmp_path.resolve()),
        workspace_root=str(workspace_root.resolve()),
        compose_project="",
        ros_domain_id=-1,
        port_offset=0,
        env_file=".ai-task.env",
        repositories=[
            {
                "id": "component",
                "source_path": str(repo.resolve()),
                "worktree_path": str(repo.resolve()),
                "mutability": "task_owned",
                "required": True,
            }
        ],
        policy_profile="workspace-write",
        capabilities=[],
    )
    write_workspace(control, workspace)

    state_root = tmp_path / "state"
    project_state = state_root / "projects" / "task-one"
    project_state.mkdir(parents=True)
    state = {
        "schema_version": 1,
        "project_id": "task-one",
        "state": "completed",
        "plan_graph": {
            "work_packages": [
                {
                    "id": "WP01",
                    "title": "Done",
                    "dependencies": [],
                    "requirements": ["complete the work"],
                    "acceptance_criteria": [
                        {
                            "id": "wp01_exit",
                            "description": "done",
                            "verified": True,
                            "evidence": "verified by focused tests",
                        }
                    ],
                    "affected_repositories": ["component"],
                    "stage": "completed",
                    "status": "completed",
                    "risk": "low",
                    "priority": 1,
                    "verification_profile": "focused",
                    "agent_id": "codex",
                    "reviewer_id": "claude-code",
                    "final_reviewer_id": "opencode",
                    "verification_attempts": 1,
                    "review_cycles": 0,
                    "review_findings": [],
                    "implementation_summary": "done",
                }
            ]
        },
        "started_at": "2026-07-20T00:00:00+00:00",
        "last_transition_at": "2026-07-21T00:00:00+00:00",
        "completed_packages": 1,
        "total_packages": 1,
        "error_message": "",
    }
    (project_state / "state.json").write_text(json.dumps(state), encoding="utf-8")
    (project_state / "commit-journal.json").write_text("[]\n", encoding="utf-8")
    (project_state / "orchestrator.log").write_text("completed\n", encoding="utf-8")
    artifacts = project_state / "agent-artifacts" / "WP01"
    artifacts.mkdir(parents=True)
    (artifacts / "review.json").write_text('{"verdict":"approved"}\n', encoding="utf-8")

    for database_name, table_name in (
        ("agent-invocations.sqlite3", "invocations"),
        ("orchestration-checkpoints.sqlite3", "checkpoints"),
    ):
        connection = sqlite3.connect(project_state / database_name)
        try:
            connection.execute(f"CREATE TABLE {table_name}(value TEXT NOT NULL)")
            connection.execute(f"INSERT INTO {table_name}(value) VALUES('durable')")
            connection.commit()
        finally:
            connection.close()

    journal_dir = state_root / "journals"
    journal_dir.mkdir(parents=True)
    journal = [
        {
            "sequence": 1,
            "timestamp": "2026-07-21T00:00:00+00:00",
            "event_type": "verification_command_run",
            "payload": {
                "package_id": "WP01",
                "repository_id": "component",
                "command": "pytest -q",
                "status": "passed",
                "returncode": 0,
                "duration_seconds": 1.2,
                "stdout_fingerprint": "abc",
            },
        },
        {
            "sequence": 2,
            "timestamp": "2026-07-21T00:01:00+00:00",
            "event_type": "review_result",
            "payload": {
                "package_id": "WP01",
                "verdict": "approved",
                "findings": [],
            },
        },
    ]
    (journal_dir / "task-one.json").write_text(json.dumps(journal), encoding="utf-8")
    return control, state_root, manifest, repo


def test_archive_creates_immutable_bundle_and_live_completion(tmp_path: Path) -> None:
    control, state_root, manifest, _repo = _build_completed_task(tmp_path)
    manager = TaskArchiveManager(control, state_root)

    report = manager.preflight(manifest)
    assert report.ok, [check.message for check in report.errors]

    result = manager.archive(manifest)
    assert result.created is True
    assert result.archive_path.is_dir()
    assert result.manifest_path.is_file()
    assert (result.archive_path / "SHA256SUMS").is_file()
    assert (result.archive_path / "dossier" / "COMPLETION.yaml").is_file()
    assert (result.archive_path / "state.json").is_file()
    assert (result.archive_path / "event-journal.json").is_file()
    assert (result.archive_path / "commit-journal.json").is_file()
    assert (result.archive_path / "verification-summary.json").is_file()
    assert (result.archive_path / "agent-artifacts" / "WP01" / "review.json").is_file()
    for database_name, table_name in (
        ("agent-invocations.sqlite3", "invocations"),
        ("orchestration-checkpoints.sqlite3", "checkpoints"),
    ):
        archived_database = result.archive_path / database_name
        assert archived_database.is_file()
        connection = sqlite3.connect(archived_database)
        try:
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert connection.execute(
                f"SELECT value FROM {table_name}"
            ).fetchone()[0] == "durable"
        finally:
            connection.close()

    verification = manager.verify(result.archive_path)
    assert verification["ok"] is True
    assert verification["files_checked"] >= 10

    closed = load_manifest(control, "task-one")
    assert closed.status == "closed"
    live_completion = yaml.safe_load(
        (control / "projects/sample/tasks/task-one/COMPLETION.yaml").read_text()
    )
    assert live_completion["archive"]["archive_id"] == result.archive_id
    assert live_completion["archive"]["manifest_sha256"] == result.manifest_sha256
    assert live_completion["repositories"][0]["commit"] == _git(_repo, "rev-parse", "HEAD")

    reused = manager.archive(closed)
    assert reused.created is False
    assert reused.archive_path == result.archive_path
    assert len(manager.list_records()) == 1


def test_archive_preserves_completed_repository_sync_journal(tmp_path: Path) -> None:
    control, state_root, manifest, _repo = _build_completed_task(tmp_path)
    store = RepositorySyncTransactionStore(state_root / "projects" / "task-one")
    store.save(
        RepositorySyncTransaction(
            schema_version=1,
            transaction_id="sync-WP20-SYNC-complete",
            package_id="WP20-SYNC",
            package_fingerprint="fingerprint",
            created_at="2026-07-21T00:00:00+00:00",
            updated_at="2026-07-21T00:10:00+00:00",
            phase="complete",
            forward_only=True,
            repositories=[
                RepositorySyncRepositoryState(
                    repository_id="component",
                    remote="origin",
                    source_branch="master",
                    source_commit="a" * 40,
                    target_branch="task/task-one",
                    target_before="b" * 40,
                    status="committed",
                    target_after="c" * 40,
                )
            ],
        )
    )

    result = TaskArchiveManager(control, state_root).archive(manifest)

    archived = result.archive_path / "repository-sync" / store.path_for("WP20-SYNC").name
    assert archived.is_file()
    assert "sync-WP20-SYNC-complete" in archived.read_text(encoding="utf-8")


def test_archive_preflight_blocks_pending_repository_sync_transaction(tmp_path: Path) -> None:
    control, state_root, manifest, _repo = _build_completed_task(tmp_path)
    store = RepositorySyncTransactionStore(state_root / "projects" / "task-one")
    store.save(
        RepositorySyncTransaction(
            schema_version=1,
            transaction_id="sync-WP20-SYNC-pending",
            package_id="WP20-SYNC",
            package_fingerprint="fingerprint",
            created_at="2026-07-21T00:00:00+00:00",
            updated_at="2026-07-21T00:00:00+00:00",
            phase="resolving",
            repositories=[
                RepositorySyncRepositoryState(
                    repository_id="component",
                    remote="origin",
                    source_branch="master",
                    source_commit="a" * 40,
                    target_branch="task/task-one",
                    target_before="b" * 40,
                    status="conflicted",
                    conflict_paths=["component.txt"],
                )
            ],
        )
    )

    report = TaskArchiveManager(control, state_root).preflight(manifest)

    assert not report.ok
    assert any(
        check.id == "repository_sync_transactions" and not check.ok
        for check in report.checks
    )


def test_archive_preflight_blocks_dirty_repository(tmp_path: Path) -> None:
    control, state_root, manifest, repo = _build_completed_task(tmp_path)
    (repo / "component.txt").write_text("dirty\n", encoding="utf-8")

    report = TaskArchiveManager(control, state_root).preflight(manifest)

    assert report.ok is False
    assert any(
        check.id == "repository:component:clean" and not check.ok
        for check in report.checks
    )


def test_archive_verify_detects_tampering(tmp_path: Path) -> None:
    control, state_root, manifest, _repo = _build_completed_task(tmp_path)
    manager = TaskArchiveManager(control, state_root)
    result = manager.archive(manifest)
    (result.archive_path / "state.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(Exception, match="checksum mismatch"):
        manager.verify(result.archive_path)


def test_task_close_check_and_archive_cli(tmp_path: Path, monkeypatch, capsys) -> None:
    control, state_root, _manifest, _repo = _build_completed_task(tmp_path)
    monkeypatch.setenv("EXECRAFT_WORKFLOW_ROOT", str(control))

    assert main([
        "task", "close", "task-one", "--check", "--state-dir", str(state_root)
    ]) == 0
    assert "Result: ready" in capsys.readouterr().out

    assert main([
        "task", "close", "task-one", "--archive", "--state-dir", str(state_root)
    ]) == 0
    output = capsys.readouterr().out
    assert "completion archive" in output
    assert "Closed task task-one" in output

    assert main([
        "archive", "verify", "task-one", "--project", "sample", "--state-dir", str(state_root)
    ]) == 0
    assert "Archive verified" in capsys.readouterr().out


def test_archive_recovers_unindexed_completed_bundle(tmp_path: Path) -> None:
    control, state_root, manifest, _repo = _build_completed_task(tmp_path)
    manager = TaskArchiveManager(control, state_root)
    created = manager.archive(manifest)
    manager.index_path.unlink()

    recovered_manager = TaskArchiveManager(control, state_root)
    recovered = recovered_manager.archive(load_manifest(control, "task-one"))

    assert recovered.created is False
    assert recovered.archive_path == created.archive_path
    records = json.loads(recovered_manager.index_path.read_text(encoding="utf-8"))
    assert len(records) == 1
    assert records[0]["archive_id"] == created.archive_id


def test_archive_verify_rejects_unindexed_extra_file(tmp_path: Path) -> None:
    control, state_root, manifest, _repo = _build_completed_task(tmp_path)
    manager = TaskArchiveManager(control, state_root)
    result = manager.archive(manifest)
    (result.archive_path / "unexpected.txt").write_text("not indexed\n", encoding="utf-8")

    with pytest.raises(Exception, match="inventory mismatch"):
        manager.verify(result.archive_path)


def test_archive_preflight_detects_running_orchestrator(tmp_path: Path) -> None:
    import fcntl

    control, state_root, manifest, _repo = _build_completed_task(tmp_path)
    lock_path = state_root / "projects" / "task-one" / "orchestrator.lock"
    lock_path.touch()
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        report = TaskArchiveManager(control, state_root).preflight(manifest)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    assert report.ok is False
    assert any(
        check.id == "orchestrator_idle" and not check.ok
        for check in report.checks
    )


def test_completion_archive_verifier_requires_and_verifies_latest_archive(
    tmp_path: Path,
) -> None:
    from execraft.workspace.lifecycle_safety import CompletionArchiveVerifier

    control, state_root, manifest, _repo = _build_completed_task(tmp_path)
    verifier = CompletionArchiveVerifier(control, state_root)

    missing = verifier.verify(manifest)
    assert missing.ok is False
    assert "verified completion archive is required" in missing.message

    TaskArchiveManager(control, state_root).archive(manifest)
    closed = load_manifest(control, "task-one")
    verified = verifier.verify(closed)

    assert verified.ok is True
    assert "completion archive verified" in verified.message


def test_completion_archive_verifier_rejects_post_archive_dossier_change(
    tmp_path: Path,
) -> None:
    from execraft.workspace.lifecycle_safety import CompletionArchiveVerifier

    control, state_root, manifest, _repo = _build_completed_task(tmp_path)
    TaskArchiveManager(control, state_root).archive(manifest)
    brief = control / "projects/sample/tasks/task-one/BRIEF.md"
    brief.write_text("# Changed after archive\n", encoding="utf-8")

    check = CompletionArchiveVerifier(control, state_root).verify(
        load_manifest(control, "task-one")
    )

    assert check.ok is False
    assert "task dossier changed after archiving" in check.message


def test_completion_archive_verifier_rejects_post_archive_commit(
    tmp_path: Path,
) -> None:
    from execraft.workspace.lifecycle_safety import CompletionArchiveVerifier

    control, state_root, manifest, repo = _build_completed_task(tmp_path)
    TaskArchiveManager(control, state_root).archive(manifest)
    (repo / "later.txt").write_text("later clean commit\n", encoding="utf-8")
    _commit_all(repo, "post-archive change")

    check = CompletionArchiveVerifier(control, state_root).verify(
        load_manifest(control, "task-one")
    )

    assert check.ok is False
    assert "changed after archiving" in check.message


def test_completion_archive_verifier_rejects_post_archive_task_definition_change(
    tmp_path: Path,
) -> None:
    from execraft.workspace.lifecycle_safety import CompletionArchiveVerifier

    control, state_root, manifest, _repo = _build_completed_task(tmp_path)
    TaskArchiveManager(control, state_root).archive(manifest)
    changed = load_manifest(control, "task-one")
    changed.title = "Retitled after archive"
    write_manifest(control, changed)

    check = CompletionArchiveVerifier(control, state_root).verify(
        load_manifest(control, "task-one")
    )

    assert check.ok is False
    assert "TASK.yaml definition changed after archiving" in check.message
