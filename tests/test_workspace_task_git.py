"""Parity tests for ported task_git.py workspace module."""

from pathlib import Path

import pytest
import yaml
from execraft.workspace.task_git import (
    REPOSITORY_ID_RE,
    TASK_ID_RE,
    TASK_STATUSES,
    RepositorySpec,
    TaskGitError,
    TaskManifest,
    is_protected_branch,
    load_project_yaml,
    load_manifest,
    local_manifest_registry_path,
    migrate_v1_to_v2,
    project_repository_path,
    project_task_directory,
    project_task_manifest_path,
    utc_now,
    validate_branch_name,
    validate_manifest,
    validate_task_id,
)

from execraft.workspace.workspace_git import sanitize_compose_project


class TestValidateTaskId:
    def test_accepts_valid(self):
        assert validate_task_id("my-task_1") == "my-task_1"

    def test_accepts_simple(self):
        assert validate_task_id("abc") == "abc"

    def test_rejects_uppercase(self):
        with pytest.raises(TaskGitError):
            validate_task_id("MyTask")

    def test_rejects_special(self):
        with pytest.raises(TaskGitError):
            validate_task_id("task@name")

    def test_strips(self):
        assert validate_task_id("  my-task  ") == "my-task"


class TestValidateBranchName:
    def test_accepts_simple(self):
        assert validate_branch_name("feature/foo") == "feature/foo"

    def test_accepts_with_dot(self):
        assert validate_branch_name("fix.1.2") == "fix.1.2"

    def test_rejects_ends_with_slash(self):
        with pytest.raises(TaskGitError):
            validate_branch_name("feature/")

    def test_rejects_starts_with_dash(self):
        with pytest.raises(TaskGitError):
            validate_branch_name("-bad")

    def test_rejects_empty(self):
        with pytest.raises(TaskGitError):
            validate_branch_name("")

    def test_rejects_double_dot(self):
        with pytest.raises(TaskGitError):
            validate_branch_name("a..b")


class TestIsProtectedBranch:
    def test_main_is_protected(self):
        assert is_protected_branch("main")

    def test_master_is_protected(self):
        assert is_protected_branch("master")

    def test_develop_is_protected(self):
        assert is_protected_branch("develop")

    def test_release_is_protected(self):
        assert is_protected_branch("release/v1.0")

    def test_feature_is_not_protected(self):
        assert not is_protected_branch("feature/foo")


class TestUtcNow:
    def test_returns_iso_format(self):
        result = utc_now()
        assert "T" in result
        assert result.endswith(("+00:00", "Z")) or "+" not in result[-6:]


class TestRepositorySpec:
    def test_defaults(self):
        spec = RepositorySpec(id="test", path=".", base_branch="main", task_branch="task/test")
        assert spec.role == "component"
        assert spec.required is True
        assert spec.start_commit == ""

    def test_from_mapping(self):
        data = {
            "id": "repo1",
            "path": "source/core",
            "base_branch": "main",
            "task_branch": "sample_task",
            "role": "core",
            "required": True,
            "verify": ["make test"],
        }
        spec = RepositorySpec.from_mapping(data)
        assert spec.id == "repo1"
        assert spec.verify == ["make test"]

    def test_from_mapping_verify_rejects_non_list(self):
        with pytest.raises(TaskGitError):
            RepositorySpec.from_mapping({"verify": "not-a-list"})

    def test_as_mapping_roundtrip(self):
        spec = RepositorySpec(id="r1", path=".", base_branch="main", task_branch="task/r1")
        mapping = spec.as_mapping()
        restored = RepositorySpec.from_mapping(mapping)
        assert restored.id == spec.id
        assert restored.path == spec.path
        assert restored.base_branch == spec.base_branch

    def test_runtime_only_roundtrip(self):
        spec = RepositorySpec(
            id="runtime",
            base_branch="main",
            task_branch="task/test",
            mutability="runtime_only",
        )
        restored = RepositorySpec.from_mapping(spec.as_mapping())
        assert restored.mutability == "runtime_only"


class TestTaskManifest:
    VALID_MAPPING = {
        "schema_version": 1,
        "id": "test-task",
        "title": "Test Task",
        "status": "draft",
        "created_at": "2026-01-01T00:00:00",
        "git": {
            "branch_name": "task/test-task",
            "merge_strategy": "squash",
            "integration_branch": "integration/test-task",
        },
        "repositories": [
            {
                "id": "root",
                "path": ".",
                "base_branch": "master",
                "task_branch": "task/test-task",
            }
        ],
        "integration": {"verify": ["make test"]},
    }

    def test_from_mapping(self):
        manifest = TaskManifest.from_mapping(self.VALID_MAPPING)
        assert manifest.id == "test-task"
        assert manifest.schema_version == 1
        assert manifest.branch_name == "task/test-task"
        assert len(manifest.repositories) == 1

    def test_as_mapping_roundtrip(self):
        manifest = TaskManifest.from_mapping(self.VALID_MAPPING)
        restored = TaskManifest.from_mapping(manifest.as_mapping())
        assert restored.id == manifest.id
        assert restored.title == manifest.title
        assert restored.branch_name == manifest.branch_name

    def test_rejects_missing_git(self):
        with pytest.raises(TaskGitError):
            TaskManifest.from_mapping({"schema_version": 1, "id": "bad"})

    def test_rejects_bad_schema_version(self):
        data = dict(self.VALID_MAPPING)
        data["schema_version"] = 99
        with pytest.raises(TaskGitError, match="unsupported"):
            TaskManifest.from_mapping(data)


class TestValidateManifest:
    def test_valid_passes(self):
        manifest = TaskManifest(
            schema_version=1,
            id="test",
            title="Test Task",
            status="draft",
            created_at="2026-01-01T00:00:00",
            branch_name="task/test",
            merge_strategy="squash",
            integration_branch="integration/test",
            repositories=[
                RepositorySpec(id="root", path=".", base_branch="main", task_branch="task/test")
            ],
        )
        validate_manifest(manifest)

    def test_rejects_missing_root_repo(self):
        manifest = TaskManifest(
            schema_version=1,
            id="test",
            title="Test",
            status="draft",
            created_at="2026-01-01T00:00:00",
            branch_name="task/test",
            merge_strategy="squash",
            integration_branch="integration/test",
            repositories=[
                RepositorySpec(id="core", path="source/core", base_branch="main", task_branch="task/test")
            ],
        )
        with pytest.raises(TaskGitError, match="must include"):
            validate_manifest(manifest)

    def test_rejects_empty_title(self):
        manifest = TaskManifest(
            schema_version=1,
            id="test",
            title="",
            status="draft",
            created_at="2026-01-01T00:00:00",
            branch_name="task/test",
            merge_strategy="squash",
            integration_branch="integration/test",
            repositories=[
                RepositorySpec(id="root", path=".", base_branch="main", task_branch="task/test")
            ],
        )
        with pytest.raises(TaskGitError, match="empty"):
            validate_manifest(manifest)


class TestTASK_STATUSES:
    def test_contains_expected(self):
        for status in ("draft", "briefed", "planned", "in_progress", "review", "closed"):
            assert status in TASK_STATUSES


class TestTASK_ID_RE:
    def test_matches_valid(self):
        assert TASK_ID_RE.fullmatch("test-task_1")
        assert TASK_ID_RE.fullmatch("my.cool-task")

    def test_rejects_invalid(self):
        assert not TASK_ID_RE.fullmatch("Test")
        assert not TASK_ID_RE.fullmatch("")


class TestREPOSITORY_ID_RE:
    def test_matches_valid(self):
        assert REPOSITORY_ID_RE.fullmatch("core")
        assert REPOSITORY_ID_RE.fullmatch("component_stack")
        assert REPOSITORY_ID_RE.fullmatch("my-repo_1")

    def test_rejects_invalid(self):
        assert not REPOSITORY_ID_RE.fullmatch("")
        assert not REPOSITORY_ID_RE.fullmatch("bad/path")


class TestTaskManifestV2:
    V2_MAPPING = {
        "schema_version": 2,
        "id": "test-v2",
        "project": "sample",
        "title": "Test Task v2",
        "status": "draft",
        "created_at": "2026-01-01T00:00:00",
        "git": {
            "branch_name": "task/test-v2",
            "merge_strategy": "squash",
        },
        "repositories": [
            {
                "id": "component_stack",
                "base_branch": "master",
                "task_branch": "task/test-v2",
                "role": "deployment",
                "required": True,
                "start_commit": "abc123",
            },
            {
                "id": "core",
                "base_branch": "mission_planner",
                "task_branch": "task/test-v2",
                "role": "core",
                "required": True,
                "start_commit": "def456",
                "latest_commit": "789ghi",
            },
        ],
        "integration": {"verify": ["make test"]},
    }

    def test_from_mapping(self):
        manifest = TaskManifest.from_mapping(self.V2_MAPPING)
        assert manifest.schema_version == 2
        assert manifest.project == "sample"
        assert manifest.id == "test-v2"
        assert len(manifest.repositories) == 2
        assert manifest.repositories[0].path == ""
        assert manifest.repositories[0].role == "deployment"
        assert manifest.integration_branch == ""

    def test_as_mapping_roundtrip(self):
        manifest = TaskManifest.from_mapping(self.V2_MAPPING)
        restored = TaskManifest.from_mapping(manifest.as_mapping())
        assert restored.schema_version == 2
        assert restored.project == "sample"
        assert restored.repositories[0].role == "deployment"

    def test_as_mapping_omits_empty_project(self):
        data = dict(self.V2_MAPPING)
        data.pop("project")
        with pytest.raises(TaskGitError, match="cannot be empty"):
            TaskManifest.from_mapping(data)

    def test_as_mapping_omits_integration_branch_when_empty(self):
        manifest = TaskManifest.from_mapping(self.V2_MAPPING)
        mapping = manifest.as_mapping()
        assert "integration_branch" not in mapping["git"]

    def test_rejects_missing_project_in_v2(self):
        data = dict(self.V2_MAPPING)
        data.pop("project")
        with pytest.raises(TaskGitError, match="cannot be empty"):
            TaskManifest.from_mapping(data)

    def test_rejects_unsupported_schema_version(self):
        data = dict(self.V2_MAPPING)
        data["schema_version"] = 99
        with pytest.raises(TaskGitError, match="unsupported"):
            TaskManifest.from_mapping(data)

    def test_validate_v2_valid_passes(self):
        manifest = TaskManifest.from_mapping(self.V2_MAPPING)
        validate_manifest(manifest)

    def test_validate_v2_rejects_empty_project(self):
        manifest = TaskManifest(
            schema_version=2,
            id="test-v2",
            project="",
            title="Test",
            status="draft",
            created_at="2026-01-01T00:00:00",
            branch_name="task/test-v2",
            merge_strategy="squash",
            repositories=[
                RepositorySpec(id="test", base_branch="main", task_branch="task/test-v2")
            ],
        )
        with pytest.raises(TaskGitError, match="cannot be empty"):
            validate_manifest(manifest)

    def test_validate_v2_allows_repo_without_path(self):
        manifest = TaskManifest(
            schema_version=2,
            id="test-v2",
            project="sample",
            title="Test",
            status="draft",
            created_at="2026-01-01T00:00:00",
            branch_name="task/test-v2",
            merge_strategy="squash",
            repositories=[
                RepositorySpec(id="repo1", base_branch="main", task_branch="task/test-v2")
            ],
        )
        validate_manifest(manifest)

    def test_validate_v2_rejects_duplicate_ids(self):
        manifest = TaskManifest(
            schema_version=2,
            id="test-v2",
            project="sample",
            title="Test",
            status="draft",
            created_at="2026-01-01T00:00:00",
            branch_name="task/test-v2",
            merge_strategy="squash",
            repositories=[
                RepositorySpec(id="dup", base_branch="main", task_branch="task/test-v2"),
                RepositorySpec(id="dup", base_branch="main", task_branch="task/test-v2"),
            ],
        )
        with pytest.raises(TaskGitError, match="duplicate"):
            validate_manifest(manifest)


class TestMigrateV1ToV2:
    def test_migrates_v1_to_v2(self):
        v1 = TaskManifest(
            schema_version=1,
            id="sample_task",
            title="Test Task",
            status="in_progress",
            created_at="2026-01-01T00:00:00",
            branch_name="sample_task",
            merge_strategy="squash",
            integration_branch="integration/sample_task",
            repositories=[
                RepositorySpec(
                    id="component_stack", path=".", base_branch="master",
                    task_branch="sample_task", role="integration",
                    start_commit="abc123",
                ),
                RepositorySpec(
                    id="core", path="source/core", base_branch="mission_planner",
                    task_branch="sample_task", role="component",
                    start_commit="def456", latest_commit="789ghi",
                ),
            ],
        )
        v2 = migrate_v1_to_v2(v1, project="sample")
        assert v2.schema_version == 2
        assert v2.project == "sample"
        assert len(v2.repositories) == 2
        assert v2.repositories[0].path == ""
        assert v2.repositories[0].role == "integration"
        assert v2.repositories[0].start_commit == "abc123"
        assert v2.repositories[1].latest_commit == "789ghi"
        assert v2.integration_branch == "integration/sample_task"
        assert v2.title == "Test Task"

    def test_migrate_uses_project_data_roles(self):
        v1 = TaskManifest(
            schema_version=1,
            id="test",
            title="Test",
            status="draft",
            created_at="2026-01-01T00:00:00",
            branch_name="task/test",
            merge_strategy="squash",
            integration_branch="integration/test",
            repositories=[
                RepositorySpec(id="component_stack", path=".", base_branch="main", task_branch="task/test"),
                RepositorySpec(id="core", path="source/core", base_branch="main", task_branch="task/test"),
            ],
        )
        project_data = {
            "repositories": [
                {"id": "component_stack", "role": "deployment"},
                {"id": "core", "role": "core"},
            ]
        }
        v2 = migrate_v1_to_v2(v1, project="sample", project_data=project_data)
        assert v2.repositories[0].role == "deployment"
        assert v2.repositories[1].role == "core"

    def test_migrate_preserves_catalog_runtime_only_floor(self):
        v1 = TaskManifest(
            schema_version=1,
            id="test",
            title="Test",
            status="draft",
            created_at="2026-01-01T00:00:00",
            branch_name="task/test",
            merge_strategy="squash",
            integration_branch="integration/test",
            repositories=[
                RepositorySpec(
                    id="runtime",
                    path="source/runtime",
                    base_branch="main",
                    task_branch="task/test",
                )
            ],
        )
        project_data = {
            "repositories": [
                {
                    "id": "runtime",
                    "role": "runtime",
                    "required": True,
                    "mutability": "runtime_only",
                }
            ]
        }

        v2 = migrate_v1_to_v2(v1, project="sample", project_data=project_data)

        assert v2.repositories[0].role == "runtime"
        assert v2.repositories[0].mutability == "runtime_only"

    def test_migrate_rejects_non_v1(self):
        manifest = TaskManifest(
            schema_version=99,
            id="test",
            title="Test",
            status="draft",
            created_at="2026-01-01T00:00:00",
            branch_name="task/test",
            merge_strategy="squash",
            integration_branch="integration/test",
            repositories=[RepositorySpec(id="root", path=".", base_branch="main", task_branch="task/test")],
        )
        with pytest.raises(TaskGitError, match="requires schema v1"):
            migrate_v1_to_v2(manifest, project="sample")


class TestProjectTaskPaths:
    def test_project_task_directory(self):
        path = project_task_directory(Path("/root"), "sample", "my-task")
        assert path == Path("/root/projects/sample/tasks/my-task")

    def test_project_task_manifest_path(self):
        path = project_task_manifest_path(Path("/root"), "sample", "my-task")
        assert path == Path("/root/projects/sample/tasks/my-task/TASK.yaml")

    def test_project_task_directory_validates_id(self):
        with pytest.raises(TaskGitError):
            project_task_directory(Path("/root"), "sample", "Bad-ID")

    def test_project_manifest_precedes_stale_local_registry(self, tmp_path: Path):
        task_dir = project_task_directory(tmp_path, "sample", "demo")
        task_dir.mkdir(parents=True)
        current = {
            "schema_version": 2,
            "id": "demo",
            "project": "sample",
            "title": "Current",
            "status": "review",
            "created_at": "2026-01-01T00:00:00+00:00",
            "git": {"branch_name": "task/demo", "merge_strategy": "squash"},
            "repositories": [
                {
                    "id": "app",
                    "base_branch": "main",
                    "task_branch": "task/demo",
                    "role": "component",
                    "required": True,
                }
            ],
        }
        (task_dir / "TASK.yaml").write_text(
            yaml.safe_dump(current, sort_keys=False), encoding="utf-8"
        )
        registry = local_manifest_registry_path(tmp_path, "demo")
        registry.parent.mkdir(parents=True)
        stale = {**current, "status": "in_progress", "title": "Stale"}
        registry.write_text(yaml.safe_dump(stale, sort_keys=False), encoding="utf-8")
        legacy = tmp_path / "docs/ai/tasks/demo/TASK.yaml"
        legacy.parent.mkdir(parents=True)
        legacy.write_text(yaml.safe_dump(stale, sort_keys=False), encoding="utf-8")

        manifest = load_manifest(tmp_path, "demo")

        assert manifest.status == "review"
        assert manifest.title == "Current"


class TestProjectYamlLoading:
    def test_load_project_yaml_missing_raises(self, tmp_path):
        with pytest.raises(TaskGitError, match="not found"):
            load_project_yaml(tmp_path, "nonexistent")

    def test_project_repository_path(self):
        project_data = {
            "repositories": [
                {"id": "component_stack", "path": "component_stack"},
                {"id": "core", "path": "component_stack/source/core"},
            ]
        }
        assert project_repository_path(project_data, "component_stack") == "component_stack"
        assert project_repository_path(project_data, "core") == "component_stack/source/core"

    def test_project_repository_path_missing_raises(self):
        with pytest.raises(TaskGitError, match="not found"):
            project_repository_path({"repositories": [{"id": "x", "path": "x"}]}, "missing")
