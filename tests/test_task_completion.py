"""Unified WP3 task-completion lifecycle tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from execraft.archive import TaskArchiveManager
from execraft.completion import TaskCompletionPolicy, task_completion_policy_from_scheduling
from execraft.completion.models import TaskCompletionError
from execraft.completion.service import TaskCompletionService
from execraft.orchestrate.identity import resolve_storage_identity
from execraft.workspace.task_git import (
    RepositorySpec,
    TaskGitError,
    TaskManifest,
    load_manifest,
    write_manifest,
)
from execraft.workspace.workspace_git import (
    WorkspaceRecord,
    load_workspace,
    parse_worktree_list,
    write_env_file,
    write_workspace,
)


def _git(path: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=path,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", "Execraft test")
    _git(path, "config", "user.email", "test@example.invalid")
    (path / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "baseline")


def _completed_task(tmp_path: Path, *, manifest_status: str = "planned"):
    control = tmp_path / "control"
    _init_repo(control)
    dossier = control / "projects" / "fixture" / "tasks" / "demo"
    dossier.mkdir(parents=True)

    source = tmp_path / "source"
    _init_repo(source)
    workspace_root = tmp_path / "ai-workspaces" / "fixture" / "demo"
    worktree = workspace_root / "component"
    _git(source, "worktree", "add", "-q", "-b", "task/demo", str(worktree), "main")
    (worktree / "tracked.txt").write_text("completed\n", encoding="utf-8")
    _git(worktree, "add", ".")
    _git(worktree, "commit", "-qm", "task implementation")
    task_head = _git(worktree, "rev-parse", "HEAD")

    manifest = TaskManifest(
        schema_version=2,
        id="demo",
        project="fixture",
        title="Completion fixture",
        status=manifest_status,
        created_at="2026-08-07T00:00:00+00:00",
        branch_name="task/demo",
        repositories=[
            RepositorySpec(
                id="component",
                base_branch="main",
                task_branch="task/demo",
                role="component",
                required=True,
                mutability="task_owned",
            )
        ],
    )
    write_manifest(control, manifest)
    (dossier / "BRIEF.md").write_text("# Brief\n\nDone.\n", encoding="utf-8")
    (dossier / "PLAN.md").write_text("# Plan\n\nDone.\n", encoding="utf-8")
    (dossier / "PLAN.graph.yaml").write_text("schema_version: 1\n", encoding="utf-8")

    record = WorkspaceRecord(
        schema_version=1,
        task_id="demo",
        created_at="2026-08-07T00:00:00+00:00",
        source_root=str(tmp_path.resolve()),
        workspace_root=str(workspace_root.resolve()),
        compose_project="",
        ros_domain_id=-1,
        port_offset=0,
        env_file=".execraft/runtime.env",
        repositories=[
            {
                "id": "component",
                "source_path": str(source.resolve()),
                "worktree_path": str(worktree.resolve()),
                "branch": "task/demo",
                "role": "component",
                "mutability": "task_owned",
                "required": True,
            }
        ],
        policy_profile="workspace-write",
        capabilities=[],
    )
    write_workspace(control, record)
    write_env_file(workspace_root, record)

    # Commit only the durable dossier. The workspace registry lives below the
    # control repository's Git common dir and therefore never enters product history.
    _git(control, "add", "projects")
    _git(control, "commit", "-qm", "task dossier")

    state_root = tmp_path / "state"
    identity = resolve_storage_identity(state_root, project_id="fixture", task_id="demo")
    state = {
        "schema_version": 1,
        "project_id": "demo",
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
                            "evidence": "focused tests passed",
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
        "started_at": "2026-08-07T00:00:00+00:00",
        "last_transition_at": "2026-08-07T01:00:00+00:00",
        "completed_packages": 1,
        "total_packages": 1,
        "error_message": "",
    }
    (identity.state_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    (identity.state_dir / "commit-journal.json").write_text("[]\n", encoding="utf-8")
    identity.journal_path.parent.mkdir(parents=True, exist_ok=True)
    identity.journal_path.write_text(
        json.dumps(
            [
                {
                    "sequence": 1,
                    "timestamp": "2026-08-07T01:00:00+00:00",
                    "event_type": "verification_command_run",
                    "payload": {
                        "package_id": "WP01",
                        "repository_id": "component",
                        "command": "pytest -q",
                        "status": "passed",
                        "returncode": 0,
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    return control, state_root, source, workspace_root, worktree, dossier, task_head


def test_task_completion_policy_defaults_to_full_safe_retirement() -> None:
    policy = task_completion_policy_from_scheduling({})

    assert policy.automatic is True
    assert policy.require_archive is True
    assert policy.verify_archive_before_cleanup is True
    assert policy.stop_runtime is True
    assert policy.remove_worktrees is True
    assert policy.remove_workspace_shell is True
    assert policy.retain_task_branches is True


def test_task_completion_policy_rejects_incoherent_or_destructive_options() -> None:
    with pytest.raises(ValueError, match="remove_worktrees requires stop_runtime"):
        TaskCompletionPolicy.from_mapping({"stop_runtime": False})
    with pytest.raises(ValueError, match="remove_workspace_shell requires remove_worktrees"):
        TaskCompletionPolicy.from_mapping({"remove_worktrees": False})
    with pytest.raises(ValueError, match="does not delete task branches"):
        TaskCompletionPolicy.from_mapping({"retain_task_branches": False})
    with pytest.raises(ValueError, match="must be a boolean"):
        TaskCompletionPolicy.from_mapping({"automatic": "yes"})


def test_complete_archives_closes_and_removes_only_disposable_workspace(tmp_path: Path) -> None:
    control, state_root, source, workspace_root, worktree, dossier, task_head = _completed_task(
        tmp_path
    )

    result = TaskCompletionService(control, state_root).complete("demo")

    assert result.completed is True
    assert result.phase == "completed"
    assert result.archive_path is not None and result.archive_path.is_dir()
    assert result.shell_removed is True
    assert str(worktree.resolve()) in result.removed_worktrees
    assert not workspace_root.exists()
    assert load_workspace(control, "demo").status == "removed"
    assert load_manifest(control, "demo").status == "closed"
    assert dossier.is_dir()
    assert (dossier / "BRIEF.md").is_file()
    assert (dossier / "COMPLETION.yaml").is_file()
    assert result.report_path.is_file()

    # The branch and its exact final commit remain in the source repository.
    assert _git(source, "rev-parse", "task/demo") == task_head
    assert all(entry.path != worktree.resolve() for entry in parse_worktree_list(source))
    TaskArchiveManager(control, state_root).verify(result.archive_path)


def test_complete_is_idempotent_after_shell_removal(tmp_path: Path) -> None:
    control, state_root, _source, _workspace_root, _worktree, _dossier, _head = _completed_task(
        tmp_path
    )
    service = TaskCompletionService(control, state_root)

    first = service.complete("demo")
    second = service.complete("demo")

    assert second.completed is True
    assert second.archive_id == first.archive_id
    assert second.archive_path == first.archive_path
    assert second.resumed is True
    assert load_workspace(control, "demo").status == "removed"


def test_complete_preserves_workspace_when_archive_preflight_fails(tmp_path: Path) -> None:
    control, state_root, _source, workspace_root, worktree, _dossier, _head = _completed_task(
        tmp_path
    )
    (worktree / "tracked.txt").write_text("dirty after completion\n", encoding="utf-8")

    with pytest.raises(TaskCompletionError, match="task archive preflight failed") as exc_info:
        TaskCompletionService(control, state_root).complete("demo")

    assert workspace_root.is_dir()
    assert worktree.is_dir()
    assert load_workspace(control, "demo").status != "removed"
    assert exc_info.value.report_path is not None
    report = yaml.safe_load(exc_info.value.report_path.read_text(encoding="utf-8"))
    assert report["status"] == "incomplete"
    assert report["phase"] == "prepared"


def test_complete_resumes_after_archive_without_creating_second_archive(tmp_path: Path) -> None:
    control, state_root, _source, workspace_root, _worktree, _dossier, _head = _completed_task(
        tmp_path
    )
    manager = TaskArchiveManager(control, state_root)
    archived = manager.archive(load_manifest(control, "demo"))

    result = TaskCompletionService(control, state_root).complete("demo")

    assert result.completed is True
    assert result.archive_id == archived.archive_id
    assert not workspace_root.exists()
    assert len(manager.list_records()) == 1


def test_completion_dry_run_is_non_destructive(tmp_path: Path) -> None:
    control, state_root, _source, workspace_root, worktree, _dossier, _head = _completed_task(
        tmp_path
    )

    result = TaskCompletionService(control, state_root).complete("demo", dry_run=True)

    assert result.status == "preview"
    assert result.phase == "preflight"
    assert workspace_root.is_dir()
    assert worktree.is_dir()
    assert load_manifest(control, "demo").status == "planned"
    assert not TaskArchiveManager(control, state_root).list_records()


def test_completion_reconciles_legacy_draft_only_after_completed_preflight(
    tmp_path: Path,
) -> None:
    control, state_root, _source, _workspace_root, _worktree, _dossier, _head = (
        _completed_task(tmp_path, manifest_status="draft")
    )
    service = TaskCompletionService(control, state_root)

    preview = service.complete("demo", dry_run=True)

    assert preview.status == "preview"
    assert load_manifest(control, "demo").status == "draft"

    result = service.complete("demo")

    assert result.completed is True
    assert load_manifest(control, "demo").status == "closed"


def test_completion_does_not_reconcile_draft_without_completed_orchestration(
    tmp_path: Path,
) -> None:
    control, state_root, _source, workspace_root, _worktree, _dossier, _head = (
        _completed_task(tmp_path, manifest_status="draft")
    )
    state_path = state_root / "projects" / "demo" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["state"] = "running"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(TaskGitError, match="task archive preflight failed"):
        TaskCompletionService(control, state_root).complete("demo")

    assert load_manifest(control, "demo").status == "draft"
    assert workspace_root.is_dir()


def test_archive_accepts_pre_review_manifest_only_when_runtime_is_completed(tmp_path: Path) -> None:
    control, state_root, _source, _workspace_root, _worktree, _dossier, _head = _completed_task(
        tmp_path, manifest_status="in_progress"
    )

    report = TaskArchiveManager(control, state_root).preflight(load_manifest(control, "demo"))

    assert report.ok is True
    assert next(check for check in report.checks if check.id == "task_status").ok is True

class _FailingWorkspaceLifecycle:
    def destroy(self, *args, **kwargs):
        from execraft.workspace.task_git import TaskGitError

        raise TaskGitError("simulated retirement interruption")

    def stop(self, *args, **kwargs):  # pragma: no cover - policy uses destroy
        raise AssertionError("unexpected stop")


def test_complete_resumes_transaction_after_retirement_interruption(tmp_path: Path) -> None:
    control, state_root, _source, workspace_root, _worktree, _dossier, _head = _completed_task(
        tmp_path
    )
    first_service = TaskCompletionService(
        control,
        state_root,
        workspace_lifecycle=_FailingWorkspaceLifecycle(),
    )

    with pytest.raises(TaskCompletionError, match="simulated retirement interruption"):
        first_service.complete("demo")

    # Archive/close is durable but destructive workspace retirement did not happen.
    assert load_manifest(control, "demo").status == "closed"
    assert workspace_root.is_dir()
    manager = TaskArchiveManager(control, state_root)
    assert len(manager.list_records()) == 1

    resumed = TaskCompletionService(control, state_root).complete("demo")

    assert resumed.completed is True
    assert resumed.resumed is True
    assert not workspace_root.exists()
    assert len(manager.list_records()) == 1


def test_completed_transaction_reverifies_archive_integrity(tmp_path: Path) -> None:
    control, state_root, _source, _workspace_root, _worktree, _dossier, _head = _completed_task(
        tmp_path
    )
    service = TaskCompletionService(control, state_root)
    result = service.complete("demo")
    assert result.archive_path is not None
    (result.archive_path / "verification-summary.json").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(TaskCompletionError, match="archive verification failed"):
        service.complete("demo")


def test_incomplete_transaction_rejects_policy_drift(tmp_path: Path) -> None:
    control, state_root, _source, _workspace_root, _worktree, _dossier, _head = _completed_task(
        tmp_path
    )
    with pytest.raises(TaskCompletionError):
        TaskCompletionService(
            control,
            state_root,
            workspace_lifecycle=_FailingWorkspaceLifecycle(),
        ).complete("demo")

    changed = TaskCompletionPolicy.from_mapping({"automatic": False})
    with pytest.raises(TaskCompletionError, match="policy changed"):
        TaskCompletionService(control, state_root, policy=changed).complete("demo")

def test_task_complete_cli_runs_unified_transaction(tmp_path: Path, monkeypatch, capsys) -> None:
    from execraft.cli import main

    control, state_root, _source, workspace_root, _worktree, _dossier, _head = _completed_task(
        tmp_path
    )
    monkeypatch.setenv("EXECRAFT_CONTROL_ROOT", str(control))

    code = main(
        [
            "task",
            "complete",
            "demo",
            "--state-dir",
            str(state_root),
        ]
    )

    assert code == 0
    assert not workspace_root.exists()
    assert "Task completion: demo [completed/completed]" in capsys.readouterr().out

def test_finished_orchestration_invokes_automatic_completion(monkeypatch, tmp_path: Path) -> None:
    from types import SimpleNamespace

    import execraft.cli as cli
    from execraft.completion.models import TaskCompletionResult

    calls: list[tuple[Path, Path, str]] = []

    class _Service:
        def __init__(self, control_root, state_root, *, policy):
            calls.append((Path(control_root), Path(state_root), "init"))

        def complete(self, task_id):
            calls.append((Path("."), Path("."), task_id))
            return TaskCompletionResult(
                project="fixture",
                task_id=task_id,
                status="completed",
                phase="completed",
                report_path=tmp_path / "completion-report.yaml",
            )

    monkeypatch.setattr(cli, "TaskCompletionService", _Service)
    monkeypatch.setattr(cli, "repository_root", lambda: tmp_path / "control")
    orchestrator = SimpleNamespace(
        config=SimpleNamespace(
            state_dir=tmp_path / "state",
            task_completion_policy=TaskCompletionPolicy(),
        )
    )

    assert cli._complete_finished_orchestration(
        orchestrator=orchestrator,
        task_id="demo",
    ) is True
    assert calls[0] == (tmp_path / "control", tmp_path / "state", "init")
    assert calls[1][2] == "demo"


def test_finished_orchestration_respects_automatic_opt_out(monkeypatch, tmp_path: Path) -> None:
    import execraft.cli as cli
    from types import SimpleNamespace

    class _UnexpectedService:
        def __init__(self, *args, **kwargs):
            raise AssertionError("completion service should not be created")

    monkeypatch.setattr(cli, "TaskCompletionService", _UnexpectedService)
    orchestrator = SimpleNamespace(
        config=SimpleNamespace(
            state_dir=tmp_path / "state",
            task_completion_policy=TaskCompletionPolicy.from_mapping({"automatic": False}),
        )
    )

    assert cli._complete_finished_orchestration(
        orchestrator=orchestrator,
        task_id="demo",
    ) is True
