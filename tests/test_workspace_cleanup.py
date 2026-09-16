"""Safety and restartability tests for workspace lifecycle operations."""

from __future__ import annotations

import fcntl
import json
import subprocess
from pathlib import Path

import pytest

from execraft.archive import TaskArchiveManager
from execraft.orchestrate.identity import resolve_storage_identity
from execraft.repository_sync.transaction import (
    RepositorySyncRepositoryState,
    RepositorySyncTransaction,
    RepositorySyncTransactionStore,
)
from execraft.workspace.cleanup import (
    DockerRuntimeController,
    LifecycleCheck,
    WorkspaceLifecycleService,
)
from execraft.workspace.task_git import RepositorySpec, TaskGitError, TaskManifest, write_manifest
from execraft.workspace.workspace_git import (
    WorkspaceRecord,
    load_workspace,
    write_env_file,
    write_workspace,
)


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=path,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _repository(path: Path) -> None:
    path.mkdir(parents=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.name", "execraft test")
    _git(path, "config", "user.email", "execraft@example.invalid")
    (path / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "baseline")


class _ArchiveVerifier:
    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[str] = []

    def verify(self, manifest: TaskManifest) -> LifecycleCheck:
        self.calls.append(manifest.id)
        return LifecycleCheck(
            "completion_archive",
            self.ok,
            "completion archive verified" if self.ok else "completion archive missing",
        )


class _RuntimeController:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, bool]] = []

    def stop(self, record: WorkspaceRecord, *, dry_run: bool = False):
        from execraft.workspace.cleanup import RuntimeStopResult

        self.calls.append((record.task_id, dry_run))
        if self.fail:
            raise TaskGitError("runtime shutdown failed")
        return RuntimeStopResult(actions=("runtime stopped",))


def _workspace_fixture(tmp_path: Path, *, compose: bool = False):
    control = tmp_path / "control"
    control.mkdir()
    source = tmp_path / "source"
    _repository(source)
    workspace_root = tmp_path / "workspace"
    worktree = workspace_root / "component"
    _git(source, "worktree", "add", "-b", "task/demo", str(worktree), "main")

    manifest = TaskManifest(
        schema_version=2,
        id="demo",
        project="fixture",
        title="Lifecycle fixture",
        status="closed",
        created_at="2026-08-06T00:00:00+00:00",
        branch_name="task/demo",
        repositories=[
            RepositorySpec(
                id="component",
                base_branch="main",
                task_branch="task/demo",
                role="component",
                required=True,
            )
        ],
    )
    write_manifest(control, manifest)
    record = WorkspaceRecord(
        schema_version=1,
        task_id="demo",
        created_at="2026-08-06T00:00:00+00:00",
        source_root=str(tmp_path.resolve()),
        workspace_root=str(workspace_root.resolve()),
        compose_project="ai_demo" if compose else "",
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
            }
        ],
        capabilities=["runtime.compose"] if compose else [],
    )
    write_workspace(control, record)
    write_env_file(workspace_root, record)
    state = tmp_path / "state"
    return control, state, source, workspace_root, worktree, record


def _archived_workspace_fixture(tmp_path: Path):
    control = tmp_path / "control"
    _repository(control)
    source = tmp_path / "source"
    _repository(source)
    workspace_root = tmp_path / "workspace"
    worktree = workspace_root / "component"
    _git(source, "worktree", "add", "-b", "task/demo", str(worktree), "main")

    manifest = TaskManifest(
        schema_version=2,
        id="demo",
        project="fixture",
        title="Archived lifecycle fixture",
        status="approved",
        created_at="2026-08-06T00:00:00+00:00",
        branch_name="task/demo",
        repositories=[
            RepositorySpec(
                id="component",
                base_branch="main",
                task_branch="task/demo",
                role="component",
                required=True,
            )
        ],
    )
    write_manifest(control, manifest)
    dossier = control / "projects" / "fixture" / "tasks" / "demo"
    (dossier / "BRIEF.md").write_text("# Brief\n", encoding="utf-8")
    (dossier / "PLAN.md").write_text("# Plan\n", encoding="utf-8")
    (dossier / "PLAN.graph.yaml").write_text("schema_version: 1\n", encoding="utf-8")

    record = WorkspaceRecord(
        schema_version=1,
        task_id="demo",
        created_at="2026-08-06T00:00:00+00:00",
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
        capabilities=[],
    )
    write_workspace(control, record)
    write_env_file(workspace_root, record)

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
                            "evidence": "verified by lifecycle integration test",
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
        "started_at": "2026-08-06T00:00:00+00:00",
        "last_transition_at": "2026-08-06T01:00:00+00:00",
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
                    "timestamp": "2026-08-06T01:00:00+00:00",
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
    _git(control, "add", ".")
    _git(control, "commit", "-m", "task dossier")
    TaskArchiveManager(control, state_root).archive(manifest)
    return control, state_root, source, workspace_root, worktree


def test_stop_only_stops_runtime_and_retains_worktree(tmp_path: Path) -> None:
    control, state, _, workspace, worktree, _ = _workspace_fixture(tmp_path)
    runtime = _RuntimeController()
    service = WorkspaceLifecycleService(
        control,
        state,
        runtime_controller=runtime,
        archive_verifier=_ArchiveVerifier(),
    )

    result = service.stop("demo")

    assert result.actions == ("runtime stopped",)
    assert runtime.calls == [("demo", False)]
    assert worktree.is_dir()
    assert workspace.is_dir()
    record = load_workspace(control, "demo")
    assert record.status == "ready"
    assert record.runtime_status == "stopped"
    assert record.last_lifecycle_action == "stop"


def test_destroy_requires_archive_and_preserves_worktree(tmp_path: Path) -> None:
    control, state, _, _, worktree, _ = _workspace_fixture(tmp_path)
    service = WorkspaceLifecycleService(
        control,
        state,
        runtime_controller=_RuntimeController(),
        archive_verifier=_ArchiveVerifier(ok=False),
    )

    with pytest.raises(TaskGitError, match="completion archive missing"):
        service.destroy("demo")

    assert worktree.is_dir()
    assert load_workspace(control, "demo").status == "ready"


def test_destroy_blocks_dirty_worktree_before_runtime_shutdown(tmp_path: Path) -> None:
    control, state, _, _, worktree, _ = _workspace_fixture(tmp_path)
    (worktree / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    runtime = _RuntimeController()
    service = WorkspaceLifecycleService(
        control,
        state,
        runtime_controller=runtime,
        archive_verifier=_ArchiveVerifier(),
    )

    with pytest.raises(TaskGitError, match="uncommitted changes"):
        service.destroy("demo")

    assert runtime.calls == []
    assert worktree.is_dir()


def test_destroy_blocks_pending_commit_transaction(tmp_path: Path) -> None:
    control, state, _, _, worktree, _ = _workspace_fixture(tmp_path)
    project_state = state / "projects" / "demo"
    project_state.mkdir(parents=True)
    (project_state / ".execraft-project-id").write_text("fixture\n", encoding="utf-8")
    (project_state / "commit-journal.json").write_text(
        json.dumps([{"transaction_id": "tx-1", "status": "pending"}]),
        encoding="utf-8",
    )
    service = WorkspaceLifecycleService(
        control,
        state,
        runtime_controller=_RuntimeController(),
        archive_verifier=_ArchiveVerifier(),
    )

    with pytest.raises(TaskGitError, match="pending commit transactions"):
        service.destroy("demo")

    assert worktree.is_dir()


def test_destroy_blocks_pending_repository_sync_transaction(tmp_path: Path) -> None:
    control, state, _, _, worktree, _ = _workspace_fixture(tmp_path)
    identity = resolve_storage_identity(
        state, project_id="fixture", task_id="demo", create=False
    )
    RepositorySyncTransactionStore(identity.state_dir).save(
        RepositorySyncTransaction(
            schema_version=1,
            transaction_id="sync-WP20-SYNC-test",
            package_id="WP20-SYNC",
            package_fingerprint="fingerprint",
            created_at="2026-08-08T00:00:00+00:00",
            updated_at="2026-08-08T00:00:00+00:00",
            phase="fetched",
            repositories=[
                RepositorySyncRepositoryState(
                    repository_id="component",
                    remote="origin",
                    source_branch="main",
                    source_commit="a" * 40,
                    target_branch="task/demo",
                    target_before="b" * 40,
                )
            ],
        )
    )
    service = WorkspaceLifecycleService(
        control,
        state,
        runtime_controller=_RuntimeController(),
        archive_verifier=_ArchiveVerifier(),
    )

    with pytest.raises(TaskGitError, match="pending repository-sync transactions"):
        service.destroy("demo")

    assert worktree.is_dir()


def test_destroy_blocks_active_orchestrator_lock(tmp_path: Path) -> None:
    control, state, _, _, worktree, _ = _workspace_fixture(tmp_path)
    project_state = state / "projects" / "demo"
    project_state.mkdir(parents=True)
    (project_state / ".execraft-project-id").write_text("fixture\n", encoding="utf-8")
    lock_path = project_state / "orchestrator.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        service = WorkspaceLifecycleService(
            control,
            state,
            runtime_controller=_RuntimeController(),
            archive_verifier=_ArchiveVerifier(),
        )
        for force in (False, True):
            with pytest.raises(TaskGitError, match="orchestrator is active"):
                service.destroy(
                    "demo",
                    force=force,
                    require_archive=not force,
                )
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    assert worktree.is_dir()


def test_runtime_failure_prevents_worktree_removal(tmp_path: Path) -> None:
    control, state, _, _, worktree, _ = _workspace_fixture(tmp_path)
    service = WorkspaceLifecycleService(
        control,
        state,
        runtime_controller=_RuntimeController(fail=True),
        archive_verifier=_ArchiveVerifier(),
    )

    with pytest.raises(TaskGitError, match="runtime shutdown failed"):
        service.destroy("demo")

    assert worktree.is_dir()
    assert load_workspace(control, "demo").status == "ready"


def test_destroy_uses_git_worktree_removal_and_can_remove_shell(tmp_path: Path) -> None:
    control, state, source, workspace, worktree, _ = _workspace_fixture(tmp_path)
    (workspace / ".devcontainer").mkdir()
    (workspace / ".devcontainer" / "devcontainer.json").write_text("{}\n", encoding="utf-8")
    service = WorkspaceLifecycleService(
        control,
        state,
        runtime_controller=_RuntimeController(),
        archive_verifier=_ArchiveVerifier(),
    )

    result = service.destroy("demo", remove_shell=True)

    assert result.shell_removed is True
    assert not workspace.exists()
    assert not worktree.exists()
    assert str(worktree.resolve()) not in _git(source, "worktree", "list", "--porcelain")
    assert load_workspace(control, "demo").status == "removed"


def test_destroy_validates_unowned_shell_entries_before_changes(tmp_path: Path) -> None:
    control, state, _, workspace, worktree, _ = _workspace_fixture(tmp_path)
    (workspace / "user-notes.txt").write_text("keep me\n", encoding="utf-8")
    runtime = _RuntimeController()
    service = WorkspaceLifecycleService(
        control,
        state,
        runtime_controller=runtime,
        archive_verifier=_ArchiveVerifier(),
    )

    with pytest.raises(TaskGitError, match="unowned entries"):
        service.destroy("demo", remove_shell=True)

    assert runtime.calls == []
    assert worktree.is_dir()
    assert workspace.is_dir()


def test_force_is_explicit_disaster_recovery_path(tmp_path: Path) -> None:
    control, state, _, _, worktree, _ = _workspace_fixture(tmp_path)
    (worktree / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    service = WorkspaceLifecycleService(
        control,
        state,
        runtime_controller=_RuntimeController(fail=True),
        archive_verifier=_ArchiveVerifier(ok=False),
    )

    result = service.destroy(
        "demo",
        force=True,
        require_archive=False,
    )

    assert not worktree.exists()
    assert result.runtime.skipped is True
    assert load_workspace(control, "demo").status == "removed"


class _Process:
    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def test_docker_runtime_stop_is_exact_and_label_scoped() -> None:
    calls: list[list[str]] = []
    running = {"c1"}

    def runner(args: list[str]):
        calls.append(args)
        joined = " ".join(args)
        if args[:3] == ["docker", "ps", "-a"]:
            return _Process("".join(f"{item}\n" for item in sorted(running)))
        if args[:4] == ["docker", "network", "ls", "-q"]:
            return _Process("n1\n")
        if args[:3] == ["docker", "rm", "-f"]:
            running.discard(args[3])
        return _Process()

    record = WorkspaceRecord(
        schema_version=1,
        task_id="demo",
        created_at="2026-08-06T00:00:00+00:00",
        source_root="/tmp/source",
        workspace_root="/tmp/workspace",
        compose_project="ai_demo",
        ros_domain_id=-1,
        port_offset=0,
        env_file=".execraft/runtime.env",
        repositories=[
            {
                "id": "component",
                "source_path": "/tmp/source/component",
                "worktree_path": "/tmp/workspace/component",
            }
        ],
        capabilities=["runtime.compose"],
    )
    controller = DockerRuntimeController(runner=runner, locator=lambda _: "/usr/bin/docker")

    result = controller.stop(record)

    assert result.stopped_containers == ("c1",)
    assert result.removed_networks == ("n1",)
    assert ["docker", "rm", "-f", "c1"] in calls
    assert ["docker", "network", "rm", "n1"] in calls
    list_calls = [call for call in calls if call[:2] == ["docker", "ps"]]
    assert all("com.docker.compose.project=ai_demo" in " ".join(call) for call in list_calls)


def test_docker_runtime_refuses_unowned_compose_container() -> None:
    def runner(args: list[str]):
        joined = " ".join(args)
        if args[:3] == ["docker", "ps", "-a"]:
            return (
                _Process("managed\n")
                if "execraft.managed=true" in joined
                else _Process("managed\nunowned\n")
            )
        return _Process()

    record = WorkspaceRecord(
        schema_version=1,
        task_id="demo",
        created_at="2026-08-06T00:00:00+00:00",
        source_root="/tmp/source",
        workspace_root="/tmp/workspace",
        compose_project="ai_demo",
        ros_domain_id=-1,
        port_offset=0,
        env_file=".execraft/runtime.env",
        repositories=[
            {
                "id": "component",
                "source_path": "/tmp/source/component",
                "worktree_path": "/tmp/workspace/component",
            }
        ],
        capabilities=["runtime.compose"],
    )
    controller = DockerRuntimeController(runner=runner, locator=lambda _: "/usr/bin/docker")

    with pytest.raises(TaskGitError, match="without Execraft ownership labels"):
        controller.stop(record)


def test_force_cannot_bypass_worktree_ownership_integrity(tmp_path: Path) -> None:
    control, state, source, workspace, worktree, record = _workspace_fixture(tmp_path)
    unrelated = tmp_path / "unrelated"
    _repository(unrelated)
    unrelated_worktree = tmp_path / "unrelated-worktree"
    _git(
        unrelated,
        "worktree",
        "add",
        "-b",
        "task/unrelated",
        str(unrelated_worktree),
        "main",
    )
    record.repositories[0]["worktree_path"] = str(unrelated_worktree.resolve())
    write_workspace(control, record)
    # Keep marker identity valid; only the registry's repository route is corrupt.
    write_env_file(workspace, record)
    service = WorkspaceLifecycleService(
        control,
        state,
        runtime_controller=_RuntimeController(),
        archive_verifier=_ArchiveVerifier(ok=False),
    )

    with pytest.raises(TaskGitError, match="integrity check failed"):
        service.destroy("demo", force=True, require_archive=False)

    assert worktree.is_dir()
    assert unrelated_worktree.is_dir()
    assert str(unrelated_worktree.resolve()) in _git(unrelated, "worktree", "list", "--porcelain")
    assert load_workspace(control, "demo").status == "ready"


def test_destroy_is_idempotent_after_completed_retirement(tmp_path: Path) -> None:
    control, state, _, workspace, _, _ = _workspace_fixture(tmp_path)
    service = WorkspaceLifecycleService(
        control,
        state,
        runtime_controller=_RuntimeController(),
        archive_verifier=_ArchiveVerifier(),
    )

    first = service.destroy("demo", remove_shell=True)
    second = service.destroy("demo", remove_shell=True)

    assert first.shell_removed is True
    assert not workspace.exists()
    assert second.actions == ("workspace is already retired",)
    assert second.runtime.skipped is True


def test_destroy_resumes_after_worktree_was_already_removed(tmp_path: Path) -> None:
    control, state, source, workspace, worktree, _ = _workspace_fixture(tmp_path)
    _git(source, "worktree", "remove", str(worktree))
    _git(source, "worktree", "prune")
    service = WorkspaceLifecycleService(
        control,
        state,
        runtime_controller=_RuntimeController(),
        archive_verifier=_ArchiveVerifier(),
    )

    result = service.destroy("demo", remove_shell=True)

    assert result.removed_worktrees == ()
    assert result.shell_removed is True
    assert not workspace.exists()
    assert load_workspace(control, "demo").status == "removed"


def test_force_cannot_bypass_marker_registry_repository_drift(tmp_path: Path) -> None:
    control, state, _, _, worktree, record = _workspace_fixture(tmp_path)
    record.repositories[0]["branch"] = "task/tampered"
    # Only the registry is changed. The immutable workspace marker remains the
    # creation-time ownership witness.
    write_workspace(control, record)
    service = WorkspaceLifecycleService(
        control,
        state,
        runtime_controller=_RuntimeController(),
        archive_verifier=_ArchiveVerifier(ok=False),
    )

    with pytest.raises(TaskGitError, match="integrity check failed"):
        service.destroy("demo", force=True, require_archive=False)

    assert worktree.is_dir()
    assert load_workspace(control, "demo").status == "ready"


def test_stale_retirement_uses_full_lifecycle_and_removes_shell(tmp_path: Path) -> None:
    control, state, source, workspace, worktree, _ = _workspace_fixture(tmp_path)
    service = WorkspaceLifecycleService(
        control,
        state,
        runtime_controller=_RuntimeController(),
        archive_verifier=_ArchiveVerifier(),
    )

    actions = service.retire_stale_workspace(workspace)

    assert any("retire stale managed workspace" in action for action in actions)
    assert not workspace.exists()
    assert str(worktree.resolve()) not in _git(source, "worktree", "list", "--porcelain")
    assert load_workspace(control, "demo").status == "removed"


def test_stale_retirement_preserves_pinned_workspace(tmp_path: Path) -> None:
    control, state, _, workspace, worktree, _ = _workspace_fixture(tmp_path)
    (workspace / ".execraft" / "pinned").write_text("operator\n", encoding="utf-8")
    service = WorkspaceLifecycleService(
        control,
        state,
        runtime_controller=_RuntimeController(),
        archive_verifier=_ArchiveVerifier(),
    )

    actions = service.retire_stale_workspace(workspace)

    assert len(actions) == 1
    assert "preserve stale managed workspace" in actions[0]
    assert "pinned" in actions[0]
    assert workspace.is_dir()
    assert worktree.is_dir()


def test_stale_retirement_preserves_dirty_workspace(tmp_path: Path) -> None:
    control, state, _, workspace, worktree, _ = _workspace_fixture(tmp_path)
    (worktree / "dirty.txt").write_text("do not lose\n", encoding="utf-8")
    service = WorkspaceLifecycleService(
        control,
        state,
        runtime_controller=_RuntimeController(),
        archive_verifier=_ArchiveVerifier(),
    )

    actions = service.retire_stale_workspace(workspace)

    assert len(actions) == 1
    assert "preserve stale managed workspace" in actions[0]
    assert "uncommitted changes" in actions[0]
    assert workspace.is_dir()
    assert (worktree / "dirty.txt").read_text(encoding="utf-8") == "do not lose\n"


def test_force_cannot_remove_registered_worktree_on_unexpected_branch(
    tmp_path: Path,
) -> None:
    control, state, _, _, worktree, _ = _workspace_fixture(tmp_path)
    _git(worktree, "checkout", "-b", "task/other")
    service = WorkspaceLifecycleService(
        control,
        state,
        runtime_controller=_RuntimeController(),
        archive_verifier=_ArchiveVerifier(ok=False),
    )

    with pytest.raises(TaskGitError, match="branch mismatch"):
        service.destroy("demo", force=True, require_archive=False)

    assert worktree.is_dir()
    assert _git(worktree, "branch", "--show-current") == "task/other"
    assert load_workspace(control, "demo").status == "ready"


def test_real_archive_allows_safe_git_aware_retirement(tmp_path: Path) -> None:
    control, state, source, workspace, worktree = _archived_workspace_fixture(tmp_path)
    service = WorkspaceLifecycleService(control, state)

    result = service.destroy("demo", remove_shell=True)

    assert result.report.ok is True
    assert result.shell_removed is True
    assert not workspace.exists()
    assert str(worktree.resolve()) not in _git(source, "worktree", "list", "--porcelain")
    assert load_workspace(control, "demo").status == "removed"
    assert not (control / ".registry" / "workspace-lifecycle-locks").exists()
    assert (control / ".git" / "workspace-lifecycle-locks" / "demo.lock").is_file()
    assert _git(source, "status", "--porcelain") == ""
    assert not (source / ".execraft").exists()


def test_shell_validation_rejects_unowned_sibling_of_nested_worktree(
    tmp_path: Path,
) -> None:
    control, state, source, workspace, worktree, record = _workspace_fixture(tmp_path)
    nested = workspace / "repositories" / "component"
    nested.parent.mkdir(parents=True)
    _git(source, "worktree", "move", str(worktree), str(nested))
    record.repositories[0]["worktree_path"] = str(nested.resolve())
    write_workspace(control, record)
    write_env_file(workspace, record)
    unowned = workspace / "repositories" / "operator-notes.txt"
    unowned.write_text("preserve me\n", encoding="utf-8")
    service = WorkspaceLifecycleService(
        control,
        state,
        runtime_controller=_RuntimeController(),
        archive_verifier=_ArchiveVerifier(),
    )

    with pytest.raises(TaskGitError, match="operator-notes.txt"):
        service.destroy("demo", remove_shell=True)

    assert nested.is_dir()
    assert unowned.read_text(encoding="utf-8") == "preserve me\n"
    assert load_workspace(control, "demo").status == "ready"


def test_stale_retirement_preserves_malformed_marker(tmp_path: Path) -> None:
    control, state, _, workspace, worktree, _ = _workspace_fixture(tmp_path)
    (workspace / ".execraft" / "workspace.yaml").write_text(
        "- not\n- a\n- mapping\n", encoding="utf-8"
    )
    service = WorkspaceLifecycleService(
        control,
        state,
        runtime_controller=_RuntimeController(),
        archive_verifier=_ArchiveVerifier(),
    )

    actions = service.retire_stale_workspace(workspace)

    assert len(actions) == 1
    assert "preserve stale managed workspace" in actions[0]
    assert "must contain a mapping" in actions[0]
    assert workspace.is_dir()
    assert worktree.is_dir()
