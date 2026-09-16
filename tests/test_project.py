from pathlib import Path

import pytest
import yaml

from execraft.project import (
    ProjectError,
    list_projects,
    load_project,
    project_directory,
    validate_task_against_project,
)
from execraft.workspace.task_git import RepositorySpec, TaskManifest


def _descriptor(root: Path, repositories: list[dict] | None = None) -> Path:
    project = root / "projects" / "sample"
    project.mkdir(parents=True)
    data = {
        "schema_version": 1,
        "project": "sample",
        "repositories": repositories
        or [{"id": "app", "path": "app", "base_branch": "main"}],
    }
    (project / "project.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    return project


def test_load_minimal_project(tmp_path: Path) -> None:
    project = load_project(_descriptor(tmp_path))
    assert project.id == "sample"
    assert project.repository("app").path == "app"
    assert project.default_policy == "workspace-write"


def test_rejects_duplicate_repository_paths(tmp_path: Path) -> None:
    project_dir = _descriptor(
        tmp_path,
        [
            {"id": "one", "path": "same"},
            {"id": "two", "path": "same"},
        ],
    )
    with pytest.raises(ProjectError, match="duplicate repository path"):
        load_project(project_dir)


def test_rejects_absolute_repository_path(tmp_path: Path) -> None:
    project_dir = _descriptor(tmp_path, [{"id": "app", "path": "/host/app"}])
    with pytest.raises(ProjectError, match="safe relative"):
        load_project(project_dir)


def test_rejects_overlapping_workspace_names(tmp_path: Path) -> None:
    project_dir = _descriptor(
        tmp_path,
        [
            {"id": "one", "path": "one", "workspace_name": "repos"},
            {"id": "two", "path": "two", "workspace_name": "repos/two"},
        ],
    )
    with pytest.raises(ProjectError, match="overlapping"):
        load_project(project_dir)


def test_rejects_reserved_workspace_name(tmp_path: Path) -> None:
    project_dir = _descriptor(
        tmp_path, [{"id": "app", "path": "app", "workspace_name": ".vscode/app"}]
    )
    with pytest.raises(ProjectError, match="collides with generated state"):
        load_project(project_dir)


def test_rejects_missing_configured_directory(tmp_path: Path) -> None:
    project_dir = _descriptor(tmp_path)
    data = yaml.safe_load((project_dir / "project.yaml").read_text())
    data["skills_dir"] = "projects/sample/missing"
    (project_dir / "project.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ProjectError, match="does not exist"):
        load_project(project_dir)


def test_list_projects(tmp_path: Path) -> None:
    _descriptor(tmp_path)
    assert [project.id for project in list_projects(tmp_path)] == ["sample"]


def test_explicit_root_does_not_use_unrelated_host_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_config = tmp_path / "host-config"
    monkeypatch.setenv("EXECRAFT_CONFIG_HOME", str(host_config))
    external = tmp_path / "external" / "sample"
    external.mkdir(parents=True)
    (external / "project.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "project": "sample",
                "repositories": [],
            }
        ),
        encoding="utf-8",
    )
    binding = host_config / "projects" / "sample.yaml"
    binding.parent.mkdir(parents=True)
    binding.write_text(
        yaml.safe_dump(
            {
                "schema_version": 2,
                "project": "sample",
                "descriptor": str(external / "project.yaml"),
            }
        ),
        encoding="utf-8",
    )
    control = tmp_path / "explicit-control"
    local = _descriptor(control)

    assert project_directory(control, "sample") == local.resolve()
    assert [project.id for project in list_projects(control)] == ["sample"]


def _task(*repositories: RepositorySpec) -> TaskManifest:
    return TaskManifest(
        schema_version=2,
        id="demo",
        project="sample",
        title="Demo",
        status="in_progress",
        created_at="2026-01-01T00:00:00+00:00",
        branch_name="task/demo",
        merge_strategy="squash",
        integration_branch="integration/demo",
        repositories=list(repositories),
    )


def _repo(
    repository_id: str,
    *,
    role: str = "component",
    required: bool = True,
    mutability: str = "task_owned",
) -> RepositorySpec:
    return RepositorySpec(
        id=repository_id,
        base_branch="main",
        task_branch="task/demo",
        role=role,
        required=required,
        mutability=mutability,
    )


def test_task_cannot_weaken_catalog_runtime_only(tmp_path: Path) -> None:
    project = load_project(
        _descriptor(
            tmp_path,
            [{"id": "app", "path": "app", "mutability": "runtime_only"}],
        )
    )
    with pytest.raises(ProjectError, match="runtime-only.*writable"):
        validate_task_against_project(_task(_repo("app")), project)


def test_task_may_further_restrict_catalog_mutability(tmp_path: Path) -> None:
    project = load_project(_descriptor(tmp_path))
    validate_task_against_project(
        _task(_repo("app", mutability="runtime_only")), project
    )


@pytest.mark.parametrize(
    ("task", "match"),
    [
        (_task(_repo("unknown")), "absent from the project catalog"),
        (_task(), "omits required"),
        (_task(_repo("app", required=False)), "weakens required"),
        (_task(_repo("app", role="deployment")), "project catalog requires"),
    ],
)
def test_task_catalog_mismatches_are_rejected(
    tmp_path: Path, task: TaskManifest, match: str
) -> None:
    project = load_project(_descriptor(tmp_path))
    with pytest.raises(ProjectError, match=match):
        validate_task_against_project(task, project)
