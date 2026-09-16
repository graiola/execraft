"""Regression tests for irreversible Execraft project/task removal."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from execraft.catalog_archive import CatalogArchiveManager
from execraft.orchestrate.identity import resolve_storage_identity
from execraft.permanent_removal import PermanentRemovalService
from execraft.project import load_project_registration, register_project_descriptor
from execraft.removal_models import PermanentRemovalError
from execraft.workspace.task_git import local_manifest_registry_path
from execraft.workspace.workspace_git import WorkspaceRecord, write_workspace


def _configure_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    control = tmp_path / "control"
    state = tmp_path / "state"
    monkeypatch.setenv("EXECRAFT_CONTROL_ROOT", str(control))
    monkeypatch.setenv("EXECRAFT_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("EXECRAFT_STATE_HOME", str(state))
    (control / "projects").mkdir(parents=True)
    state.mkdir()
    return control, state


def _write_project(control: Path, project_id: str, *tasks: str) -> Path:
    project = control / "projects" / project_id
    (project / "tasks").mkdir(parents=True)
    (project / "project.yaml").write_text(
        "\n".join(
            (
                "schema_version: 2",
                f"project: {project_id}",
                f"description: {project_id} project",
                "path_base: project_directory",
                "repositories:",
                "  - id: component",
                "    path: .",
                "    workspace_name: component",
                "",
            )
        ),
        encoding="utf-8",
    )
    for task_id in tasks:
        _write_task(control, project, project_id, task_id)
    return project


def _write_task(control: Path, project: Path, project_id: str, task_id: str) -> Path:
    task = project / "tasks" / task_id
    task.mkdir(parents=True, exist_ok=True)
    manifest = (
        "\n".join(
            (
                "schema_version: 1",
                f"id: {task_id}",
                f"project: {project_id}",
                f"title: {task_id}",
                "status: review",
                "git:",
                f"  branch_name: task/{task_id}",
                "  merge_strategy: squash",
                "repositories: []",
                "integration:",
                "  verify: []",
                "",
            )
        )
    )
    (task / "TASK.yaml").write_text(manifest, encoding="utf-8")
    registry = local_manifest_registry_path(control, task_id)
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(manifest, encoding="utf-8")
    return task


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, check=True
    )
    return result.stdout.strip()


def test_remove_active_task_cleans_owned_lifecycle_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control, state = _configure_home(monkeypatch, tmp_path)
    project = _write_project(control, "alpha", "obsolete")
    task = project / "tasks" / "obsolete"
    identity = resolve_storage_identity(
        state, project_id="alpha", task_id="obsolete", create=True
    )
    (identity.state_dir / "state.json").write_text("{}\n", encoding="utf-8")
    identity.journal_path.parent.mkdir(parents=True)
    identity.journal_path.write_text("[]\n", encoding="utf-8")
    start = state / "starts" / "alpha" / "obsolete.yaml"
    start.parent.mkdir(parents=True)
    start.write_text("started: true\n", encoding="utf-8")
    completion = state / "archives" / "alpha" / "obsolete" / "archive-1"
    completion.mkdir(parents=True)
    (completion / "proof.txt").write_text("evidence\n", encoding="utf-8")
    index = state / "archives" / "index.json"
    index.write_text(
        json.dumps(
            [
                {
                    "project": "alpha",
                    "task_id": "obsolete",
                    "archive_id": "archive-1",
                    "path": str(completion),
                },
                {"project": "beta", "task_id": "keep", "archive_id": "a2"},
            ]
        ),
        encoding="utf-8",
    )
    active = control / ".registry" / "active-task"
    active.parent.mkdir(parents=True, exist_ok=True)
    active.write_text("obsolete\n", encoding="utf-8")

    result = PermanentRemovalService(control, state).remove_task("alpha", "obsolete")

    assert not task.exists()
    assert not local_manifest_registry_path(control, "obsolete").exists()
    assert not identity.state_dir.exists()
    assert not identity.journal_path.exists()
    assert not start.exists()
    assert not (state / "archives" / "alpha" / "obsolete").exists()
    assert not active.exists()
    records = json.loads(index.read_text(encoding="utf-8"))
    assert records == [{"project": "beta", "task_id": "keep", "archive_id": "a2"}]
    assert result.kind == "task"
    assert "source repositories" in result.preserved


def test_remove_archived_task_deletes_catalog_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control, state = _configure_home(monkeypatch, tmp_path)
    _write_project(control, "alpha", "obsolete")
    archive = CatalogArchiveManager(control, state)
    archive.archive_task("alpha", "obsolete")
    archived = control / "projects" / ".archive" / "tasks" / "alpha" / "obsolete"
    assert archived.is_dir()

    PermanentRemovalService(control, state).remove_task("alpha", "obsolete")

    assert not archived.exists()
    assert not local_manifest_registry_path(control, "obsolete").exists()


def test_remove_project_cleans_all_internal_tasks_and_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control, state = _configure_home(monkeypatch, tmp_path)
    project = _write_project(control, "alpha", "one", "two")
    source = tmp_path / "source"
    source.mkdir()
    register_project_descriptor(project, source_root=source)
    for task_id in ("one", "two"):
        identity = resolve_storage_identity(
            state, project_id="alpha", task_id=task_id, create=True
        )
        (identity.state_dir / "state.json").write_text("{}\n", encoding="utf-8")
    archived = control / "projects" / ".archive" / "tasks" / "alpha" / "old"
    archived.mkdir(parents=True)
    (archived / "TASK.yaml").write_text("id: old\n", encoding="utf-8")

    result = PermanentRemovalService(control, state).remove_project("alpha")

    assert not project.exists()
    assert load_project_registration("alpha") is None
    assert source.is_dir()
    assert not (control / "projects" / ".archive" / "tasks" / "alpha").exists()
    assert result.kind == "project"
    assert "source repositories" in result.preserved


def test_remove_external_project_preserves_descriptor_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control, state = _configure_home(monkeypatch, tmp_path)
    external = tmp_path / "portable" / "alpha"
    (external / "tasks").mkdir(parents=True)
    (external / "project.yaml").write_text(
        "\n".join(
            (
                "schema_version: 2",
                "project: alpha",
                "path_base: project_directory",
                "repositories:",
                "  - id: component",
                "    path: .",
                "    workspace_name: component",
                "",
            )
        ),
        encoding="utf-8",
    )
    task = _write_task(control, external, "alpha", "one")
    marker = external / "KEEP-ME.txt"
    marker.write_text("portable data\n", encoding="utf-8")
    source = tmp_path / "source"
    source.mkdir()
    register_project_descriptor(external, source_root=source)
    identity = resolve_storage_identity(state, project_id="alpha", task_id="one")
    (identity.state_dir / "state.json").write_text("{}\n", encoding="utf-8")

    result = PermanentRemovalService(control, state).remove_project("alpha")

    assert load_project_registration("alpha") is None
    assert external.is_dir()
    assert (external / "project.yaml").is_file()
    assert task.is_dir()
    assert marker.read_text(encoding="utf-8") == "portable data\n"
    assert not identity.state_dir.exists()
    assert not local_manifest_registry_path(control, "one").exists()
    assert "external project descriptor and project directory" in result.preserved


def test_delete_branches_only_removes_execraft_created_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control, state = _configure_home(monkeypatch, tmp_path)
    _write_project(control, "alpha", "one")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "base")
    _git(repo, "branch", "task/one")
    _git(repo, "branch", "user/keep")
    workspace_root = tmp_path / "already-removed-workspace"
    record = WorkspaceRecord(
        schema_version=1,
        task_id="one",
        created_at="2026-08-27T00:00:00+00:00",
        source_root=str(repo),
        workspace_root=str(workspace_root),
        compose_project="",
        ros_domain_id=-1,
        port_offset=0,
        env_file=".execraft/runtime.env",
        repositories=[
            {
                "id": "component",
                "source_path": str(repo),
                "worktree_path": str(repo),
                "branch": "task/one",
                "role": "deployment",
                "mutability": "task_owned",
                "created_branch": "true",
            }
        ],
        status="removed",
        capabilities=[],
    )
    write_workspace(control, record)

    result = PermanentRemovalService(control, state).remove_task(
        "alpha", "one", delete_branches=True
    )

    branches = set(_git(repo, "branch", "--format=%(refname:short)").splitlines())
    assert "task/one" not in branches
    assert "user/keep" in branches
    assert result.removed_branches == ("component:task/one",)


def test_preexisting_task_branch_is_preserved_even_with_delete_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control, state = _configure_home(monkeypatch, tmp_path)
    _write_project(control, "alpha", "one")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "base")
    _git(repo, "branch", "task/one")
    record = WorkspaceRecord(
        schema_version=1,
        task_id="one",
        created_at="2026-08-27T00:00:00+00:00",
        source_root=str(repo),
        workspace_root=str(tmp_path / "removed"),
        compose_project="",
        ros_domain_id=-1,
        port_offset=0,
        env_file=".execraft/runtime.env",
        repositories=[
            {
                "id": "component",
                "source_path": str(repo),
                "worktree_path": str(repo),
                "branch": "task/one",
                "role": "deployment",
                "mutability": "task_owned",
                "created_branch": "false",
            }
        ],
        status="removed",
        capabilities=[],
    )
    write_workspace(control, record)

    PermanentRemovalService(control, state).remove_task(
        "alpha", "one", delete_branches=True
    )

    assert "task/one" in _git(repo, "branch", "--format=%(refname:short)").splitlines()


def test_task_delete_requires_existing_project_task_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control, state = _configure_home(monkeypatch, tmp_path)
    _write_project(control, "alpha", "one")

    with pytest.raises(PermanentRemovalError, match="task not found"):
        PermanentRemovalService(control, state).remove_task("alpha", "missing")


def test_dry_run_does_not_remove_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control, state = _configure_home(monkeypatch, tmp_path)
    project = _write_project(control, "alpha", "one")

    result = PermanentRemovalService(control, state).remove_task(
        "alpha", "one", dry_run=True
    )

    assert result.dry_run is True
    assert (project / "tasks" / "one").is_dir()
    assert local_manifest_registry_path(control, "one").is_file()


def test_task_delete_rejects_child_of_archived_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control, state = _configure_home(monkeypatch, tmp_path)
    _write_project(control, "alpha", "one")
    CatalogArchiveManager(control, state).archive_project("alpha")

    with pytest.raises(PermanentRemovalError, match="archived project"):
        PermanentRemovalService(control, state).remove_task("alpha", "one")

    archived_project = control / "projects" / ".archive" / "projects" / "alpha"
    assert archived_project.is_dir()
    assert (archived_project / "tasks" / "one").is_dir()


def test_task_delete_honors_custom_completion_archive_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control, state = _configure_home(monkeypatch, tmp_path)
    _write_project(control, "alpha", "one")
    archive_root = tmp_path / "custom-archives"
    archive = archive_root / "alpha" / "one" / "archive-1"
    archive.mkdir(parents=True)
    (archive / "proof.txt").write_text("proof\n", encoding="utf-8")
    (archive_root / "index.json").write_text(
        json.dumps(
            [
                {
                    "project": "alpha",
                    "task_id": "one",
                    "archive_id": "archive-1",
                    "path": str(archive),
                }
            ]
        ),
        encoding="utf-8",
    )

    PermanentRemovalService(
        control, state, archive_root=archive_root
    ).remove_task("alpha", "one")

    assert not (archive_root / "alpha" / "one").exists()
    assert json.loads((archive_root / "index.json").read_text(encoding="utf-8")) == []


def test_task_delete_rejects_ambiguous_global_registry_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control, state = _configure_home(monkeypatch, tmp_path)
    project = _write_project(control, "alpha", "one")
    registry = local_manifest_registry_path(control, "one")
    registry.write_text(
        registry.read_text(encoding="utf-8").replace("project: alpha", "project: beta"),
        encoding="utf-8",
    )

    with pytest.raises(PermanentRemovalError, match="registered to project 'beta'"):
        PermanentRemovalService(control, state).remove_task("alpha", "one")

    assert (project / "tasks" / "one").is_dir()
    assert registry.is_file()


def test_read_only_completion_archive_can_be_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control, state = _configure_home(monkeypatch, tmp_path)
    _write_project(control, "alpha", "one")
    archive = state / "archives" / "alpha" / "one" / "archive-1"
    nested = archive / "dossier"
    nested.mkdir(parents=True)
    proof = nested / "proof.txt"
    proof.write_text("proof\n", encoding="utf-8")
    proof.chmod(0o400)
    nested.chmod(0o500)
    archive.chmod(0o500)

    PermanentRemovalService(control, state).remove_task("alpha", "one")

    assert not (state / "archives" / "alpha" / "one").exists()
