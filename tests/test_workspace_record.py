"""Parity tests for ported workspace_git.py workspace module."""

import json
from pathlib import Path

import pytest
from execraft.workspace.workspace_git import (
    WorkspaceRecord,
    validate_workspace_record,
    sanitize_compose_project,
    compose_isolation_findings,
    load_env_file,
    repository_environment_name,
    run_in_workspace,
    write_env_file,
    TaskGitError,
)


class TestWorkspaceRecord:
    VALID_DATA = {
        "schema_version": 1,
        "task_id": "test-task",
        "created_at": "2026-01-01T00:00:00",
        "status": "ready",
        "source_root": "/tmp/source",
        "workspace_root": "/tmp/workspace",
        "compose_project": "ai_test-task",
        "ros_domain_id": 42,
        "port_offset": 7100,
        "env_file": ".ai-task.env",
        "repositories": [
            {
                "id": "root",
                "source_path": "/tmp/source",
                "worktree_path": "/tmp/workspace",
            }
        ],
    }

    def test_from_mapping(self):
        record = WorkspaceRecord.from_mapping(self.VALID_DATA)
        assert record.task_id == "test-task"
        assert record.schema_version == 1
        assert record.ros_domain_id == 42

    def test_as_mapping_roundtrip(self):
        record = WorkspaceRecord.from_mapping(self.VALID_DATA)
        restored = WorkspaceRecord.from_mapping(record.as_mapping())
        assert restored.task_id == record.task_id
        assert restored.compose_project == record.compose_project

    def test_rejects_missing_repos(self):
        data = dict(self.VALID_DATA)
        data["repositories"] = []
        with pytest.raises(TaskGitError, match="least one"):
            WorkspaceRecord.from_mapping(data)

    def test_rejects_non_absolute_source(self):
        data = dict(self.VALID_DATA)
        data["source_root"] = "relative/path"
        with pytest.raises(TaskGitError, match="must be absolute"):
            WorkspaceRecord.from_mapping(data)

    def test_rejects_non_absolute_workspace(self):
        data = dict(self.VALID_DATA)
        data["workspace_root"] = "relative/path"
        with pytest.raises(TaskGitError, match="must be absolute"):
            WorkspaceRecord.from_mapping(data)

    def test_rejects_bad_ros_domain(self):
        data = dict(self.VALID_DATA)
        data["ros_domain_id"] = 999
        with pytest.raises(TaskGitError, match="ros_domain_id"):
            WorkspaceRecord.from_mapping(data)

    def test_rejects_bad_port_offset(self):
        data = dict(self.VALID_DATA)
        data["port_offset"] = 999999
        with pytest.raises(TaskGitError, match="port_offset"):
            WorkspaceRecord.from_mapping(data)


class TestSanitizeComposeProject:
    def test_simple(self):
        result = sanitize_compose_project("test-task")
        assert result == "ai_test-task"

    def test_removes_special(self):
        result = sanitize_compose_project("my cool @task!")
        assert " " not in result
        assert "@" not in result
        assert result.startswith("ai_")

    def test_truncates_long(self):
        long_name = "a" * 100
        result = sanitize_compose_project(long_name)
        assert len(result) <= 63

    def test_fallback_empty(self):
        result = sanitize_compose_project("!!!")
        assert result == "ai_task"


class TestValidateWorkspaceRecord:
    def test_valid_passes(self):
        record = WorkspaceRecord(
            schema_version=1,
            task_id="test",
            created_at="2026-01-01T00:00:00",
            source_root="/tmp/source",
            workspace_root="/tmp/workspace",
            compose_project="ai_test",
            ros_domain_id=42,
            port_offset=7100,
            env_file=".env",
            repositories=[
                {"id": "root", "source_path": "/tmp/source", "worktree_path": "/tmp/ws"}
            ],
        )
        validate_workspace_record(record)

    def test_rejects_unsupported_schema(self):
        record = WorkspaceRecord(
            schema_version=99,
            task_id="test",
            created_at="2026-01-01T00:00:00",
            source_root="/tmp/s",
            workspace_root="/tmp/w",
            compose_project="ai_test",
            ros_domain_id=42,
            port_offset=7100,
            env_file=".env",
            repositories=[
                {"id": "root", "source_path": "/tmp/s", "worktree_path": "/tmp/w"}
            ],
        )
        with pytest.raises(TaskGitError, match="unsupported"):
            validate_workspace_record(record)

    def test_rejects_duplicate_repository_ids(self):
        data = dict(TestWorkspaceRecord.VALID_DATA)
        data["repositories"] = [
            dict(TestWorkspaceRecord.VALID_DATA["repositories"][0]),
            dict(TestWorkspaceRecord.VALID_DATA["repositories"][0]),
        ]
        with pytest.raises(TaskGitError, match="duplicate workspace repository ID"):
            WorkspaceRecord.from_mapping(data)


class TestComposeIsolationFindings:
    def test_default_scan_is_project_agnostic(self, tmp_path):
        legacy = tmp_path / "deployment" / "compose"
        legacy.mkdir(parents=True)
        (legacy / "stack.yaml").write_text(
            "services:\n  app:\n    container_name: fixed-name\n",
            encoding="utf-8",
        )

        assert compose_isolation_findings(tmp_path) == []

    def test_explicit_project_compose_path_is_checked(self, tmp_path):
        compose_dir = tmp_path / "ops" / "compose"
        compose_dir.mkdir(parents=True)
        (compose_dir / "stack.yml").write_text(
            "services:\n  app:\n    network_mode: host\n",
            encoding="utf-8",
        )

        findings = compose_isolation_findings(tmp_path, ["ops/compose"])

        assert findings == [
            "ops/compose/stack.yml:3: host networking is shared across workspaces"
        ]

    def test_conventional_root_file_is_checked(self, tmp_path):
        (tmp_path / "compose.yaml").write_text(
            "name: fixed-project\nservices: {}\n",
            encoding="utf-8",
        )

        assert compose_isolation_findings(tmp_path) == [
            "compose.yaml:1: fixed compose project name"
        ]


class TestWorkspaceEnvironment:
    def _record(self, tmp_path: Path) -> WorkspaceRecord:
        source = tmp_path / "source"
        deployment = tmp_path / "workspace" / "deployment"
        core = tmp_path / "workspace" / "core"
        source.mkdir()
        deployment.mkdir(parents=True)
        core.mkdir(parents=True)
        return WorkspaceRecord(
            schema_version=1,
            task_id="demo",
            created_at="2026-01-01T00:00:00Z",
            source_root=str(source.resolve()),
            workspace_root=str((tmp_path / "workspace").resolve()),
            compose_project="ai_demo",
            ros_domain_id=42,
            port_offset=700,
            env_file=".execraft/runtime.env",
            repositories=[
                {
                    "id": "app",
                    "role": "deployment",
                    "mutability": "task_owned",
                    "source_path": str(source.resolve()),
                    "worktree_path": str(deployment.resolve()),
                },
                {
                    "id": "core-service",
                    "role": "core",
                    "mutability": "task_owned",
                    "source_path": str(source.resolve()),
                    "worktree_path": str(core.resolve()),
                },
            ],
            capabilities=["runtime.compose", "runtime.ros_domain", "runtime.port_namespace"],
        )

    def test_write_env_exports_repository_map_and_paths(self, tmp_path):
        record = self._record(tmp_path)
        env_path = write_env_file(Path(record.workspace_root), record)
        values = load_env_file(env_path)

        assert values["EXECRAFT_REPO_APP"].endswith("/workspace/deployment")
        assert values["EXECRAFT_REPO_CORE_SERVICE"].endswith("/workspace/core")
        repository_map = Path(values["EXECRAFT_REPOSITORY_MAP"])
        data = json.loads(repository_map.read_text(encoding="utf-8"))
        assert data["repositories"]["core-service"]["role"] == "core"
        assert repository_environment_name("core-service") == "EXECRAFT_REPO_CORE_SERVICE"

    def test_run_in_workspace_can_unset_and_override_environment(self, tmp_path):
        record = self._record(tmp_path)
        write_env_file(Path(record.workspace_root), record)
        result = run_in_workspace(
            record,
            [
                "python3 -c 'import os; "
                "assert \"COMPOSE_PREFIX\" not in os.environ; "
                "assert os.environ[\"EXPLICIT\"] == \"yes\"; "
                "assert os.environ[\"EXECRAFT_REPO_CORE_SERVICE\"].endswith(\"/workspace/core\")'"
            ],
            repository_id="app",
            shell=True,
            environment={"EXPLICIT": "yes"},
            unset_environment=["COMPOSE_PREFIX"],
        )
        assert result.returncode == 0
