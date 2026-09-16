import subprocess
from pathlib import Path

import pytest

from execraft.agents import AgentProviderConfig
from execraft.gui.workspace_changes import WorkspaceChangeError, WorkspaceChangeManager
from execraft.orchestrate.scheduler import AgentCapability
from execraft.workspace.workspace_git import WorkspaceRecord


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=path,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


def _repository(tmp_path: Path, name: str = "repo") -> Path:
    path = tmp_path / name
    path.mkdir()
    _git(path, "init", "-q", "-b", "task/demo")
    _git(path, "config", "user.name", "GUI Test")
    _git(path, "config", "user.email", "gui@example.invalid")
    (path / "tracked.txt").write_text("before\n", encoding="utf-8")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-q", "-m", "baseline")
    return path


def _manager(tmp_path: Path, repository: Path, *, agent_builder=None) -> WorkspaceChangeManager:
    workspace = WorkspaceRecord(
        schema_version=1,
        task_id="demo-task",
        created_at="2026-07-27T00:00:00+00:00",
        source_root=str(repository),
        workspace_root=str(tmp_path),
        compose_project="",
        ros_domain_id=-1,
        port_offset=-1,
        env_file=".ai-task.env",
        repositories=[
            {
                "id": "core",
                "role": "component",
                "mutability": "task_owned",
                "source_path": str(repository),
                "worktree_path": str(repository),
            }
        ],
        capabilities=[],
    )
    kwargs = {}
    if agent_builder is not None:
        kwargs["agent_builder"] = agent_builder
    return WorkspaceChangeManager(
        workspace=workspace,
        expected_branches={"core": "task/demo"},
        state_dir=tmp_path / "state",
        **kwargs,
    )


def test_snapshot_and_diff_include_tracked_untracked_and_spaced_paths(tmp_path: Path):
    repo = _repository(tmp_path)
    (repo / "tracked.txt").write_text("after\n", encoding="utf-8")
    (repo / "new file.txt").write_text("new content\n", encoding="utf-8")
    manager = _manager(tmp_path, repo)

    snapshot = manager.snapshot(driver_active=False)
    row = snapshot["repositories"][0]

    assert snapshot["commit_allowed"] is True
    assert row["branch"] == "task/demo"
    assert [item["path"] for item in row["changes"]] == ["new file.txt", "tracked.txt"]
    tracked = manager.file_diff("core", "tracked.txt")
    untracked = manager.file_diff("core", "new file.txt")
    assert "-before" in tracked["content"]
    assert "+after" in tracked["content"]
    assert "new content" in untracked["content"]


def test_commit_selected_paths_leaves_unselected_worktree_changes(tmp_path: Path):
    repo = _repository(tmp_path)
    (repo / "tracked.txt").write_text("after\n", encoding="utf-8")
    (repo / "unselected.txt").write_text("leave me\n", encoding="utf-8")
    manager = _manager(tmp_path, repo)
    snapshot = manager.snapshot(driver_active=False)
    row = snapshot["repositories"][0]

    result = manager.commit(
        selections={"core": ["tracked.txt"]},
        expected_digests={"core": row["status_digest"]},
        subject="Update tracked behavior",
        body="Verified through the GUI workspace inspector.",
        reviewed=True,
    )

    assert result["commits"][0]["repository_id"] == "core"
    assert _git(repo, "log", "-1", "--pretty=%s") == "Update tracked behavior"
    status = _git(repo, "status", "--porcelain")
    assert "tracked.txt" not in status
    assert "unselected.txt" in status
    assert (tmp_path / "state" / "commit-journal.json").is_file()
    assert (tmp_path / "state" / "gui-commit-audit.jsonl").is_file()


def test_commit_requires_fresh_digest_and_review_acknowledgement(tmp_path: Path):
    repo = _repository(tmp_path)
    (repo / "tracked.txt").write_text("after\n", encoding="utf-8")
    manager = _manager(tmp_path, repo)
    row = manager.snapshot(driver_active=False)["repositories"][0]

    with pytest.raises(WorkspaceChangeError, match="confirm that"):
        manager.commit(
            selections={"core": ["tracked.txt"]},
            expected_digests={"core": row["status_digest"]},
            subject="Update file",
            reviewed=False,
        )

    (repo / "other.txt").write_text("changed after inspection\n", encoding="utf-8")
    with pytest.raises(WorkspaceChangeError, match="changed after inspection"):
        manager.commit(
            selections={"core": ["tracked.txt"]},
            expected_digests={"core": row["status_digest"]},
            subject="Update file",
            reviewed=True,
        )


def test_commit_blocks_pre_staged_paths_outside_selection(tmp_path: Path):
    repo = _repository(tmp_path)
    (repo / "tracked.txt").write_text("after\n", encoding="utf-8")
    (repo / "staged.txt").write_text("already staged\n", encoding="utf-8")
    _git(repo, "add", "staged.txt")
    manager = _manager(tmp_path, repo)
    row = manager.snapshot(driver_active=False)["repositories"][0]

    with pytest.raises(WorkspaceChangeError, match="staged paths outside"):
        manager.commit(
            selections={"core": ["tracked.txt"]},
            expected_digests={"core": row["status_digest"]},
            subject="Update tracked file",
            reviewed=True,
        )


def test_ai_commit_message_uses_read_only_adapter_and_structured_contract(tmp_path: Path):
    repo = _repository(tmp_path)
    (repo / "tracked.txt").write_text("after\n", encoding="utf-8")
    calls = []

    class FakeAdapter:
        def execute(self, handoff):
            calls.append(handoff)
            return {
                "final_message": '{"subject":"Update tracked behavior","body":"Document the verified change."}'
            }

    def builder(provider, *, workdir, read_only):
        calls.append((provider.provider_id, workdir, read_only))
        return FakeAdapter()

    manager = _manager(tmp_path, repo, agent_builder=builder)
    row = manager.snapshot(driver_active=False)["repositories"][0]
    provider = AgentProviderConfig(
        name="reviewer",
        adapter="codex",
        enabled=True,
        provider_id="reviewer",
        binary="codex",
        capabilities=frozenset({AgentCapability.REVIEW}),
        model="test-model",
    )

    result = manager.generate_commit_message(
        selections={"core": ["tracked.txt"]},
        expected_digests={"core": row["status_digest"]},
        provider=provider,
    )

    assert result["subject"] == "Update tracked behavior"
    assert result["body"] == "Document the verified change."
    assert calls[0][0] == "reviewer"
    assert calls[0][2] is True
    handoff = calls[1]
    assert handoff.read_only is True
    assert handoff.expected_output_schema["required"] == ["subject", "body"]
    assert "core:tracked.txt" in handoff.bounded_excerpts


def test_rename_status_can_be_inspected_and_committed(tmp_path: Path):
    repo = _repository(tmp_path)
    _git(repo, "mv", "tracked.txt", "renamed file.txt")
    manager = _manager(tmp_path, repo)
    row = manager.snapshot(driver_active=False)["repositories"][0]
    change = row["changes"][0]

    assert change["path"] == "renamed file.txt"
    assert change["original_path"] == "tracked.txt"
    assert change["kind"] == "renamed"

    manager.commit(
        selections={"core": ["renamed file.txt"]},
        expected_digests={"core": row["status_digest"]},
        subject="Rename tracked file",
        reviewed=True,
    )
    assert not _git(repo, "status", "--porcelain")


def test_snapshot_disables_commit_when_only_dirty_repository_is_read_only(tmp_path: Path):
    repo = _repository(tmp_path)
    (repo / "tracked.txt").write_text("after\n", encoding="utf-8")
    workspace = WorkspaceRecord(
        schema_version=1,
        task_id="demo-task",
        created_at="2026-07-27T00:00:00+00:00",
        source_root=str(repo),
        workspace_root=str(tmp_path),
        compose_project="",
        ros_domain_id=-1,
        port_offset=-1,
        env_file=".ai-task.env",
        repositories=[
            {
                "id": "core",
                "role": "reference",
                "mutability": "read_only",
                "source_path": str(repo),
                "worktree_path": str(repo),
            }
        ],
        capabilities=[],
    )
    manager = WorkspaceChangeManager(
        workspace=workspace,
        expected_branches={"core": "task/demo"},
        state_dir=tmp_path / "state",
    )

    snapshot = manager.snapshot(driver_active=False)

    assert snapshot["dirty_repository_count"] == 1
    assert snapshot["repositories"][0]["committable"] is False
    assert snapshot["commit_allowed"] is False


def test_ai_generation_rejects_unexpected_task_branch(tmp_path: Path):
    repo = _repository(tmp_path)
    (repo / "tracked.txt").write_text("after\n", encoding="utf-8")
    manager = _manager(tmp_path, repo, agent_builder=lambda *args, **kwargs: None)
    row = manager.snapshot(driver_active=False)["repositories"][0]
    _git(repo, "branch", "-m", "other")
    provider = AgentProviderConfig(
        name="reviewer",
        adapter="codex",
        enabled=True,
        provider_id="reviewer",
        binary="codex",
        capabilities=frozenset({AgentCapability.REVIEW}),
        model="test-model",
    )

    with pytest.raises(WorkspaceChangeError, match="expected task branch"):
        manager.generate_commit_message(
            selections={"core": ["tracked.txt"]},
            expected_digests={"core": row["status_digest"]},
            provider=provider,
        )


def test_delete_untracked_removes_generated_file_and_keeps_tracked_changes(tmp_path: Path):
    repo = _repository(tmp_path)
    (repo / "tracked.txt").write_text("after\n", encoding="utf-8")
    cache = repo / "tests" / "__pycache__"
    cache.mkdir(parents=True)
    pyc = cache / "test_route.cpython-310.pyc"
    pyc.write_bytes(b"generated-bytecode")
    manager = _manager(tmp_path, repo)
    snapshot = manager.snapshot(driver_active=False)
    row = snapshot["repositories"][0]
    pyc_change = next(item for item in row["changes"] if item["path"].endswith(".pyc"))

    assert snapshot["delete_allowed"] is True
    assert pyc_change["deletable"] is True

    result = manager.delete_untracked(
        selections={"core": [pyc_change["path"]]},
        expected_digests={"core": row["status_digest"]},
    )

    assert result["deleted_count"] == 1
    assert not pyc.exists()
    assert not cache.exists()
    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "after\n"
    status = _git(repo, "status", "--porcelain")
    assert "tracked.txt" in status
    assert ".pyc" not in status
    assert "workspace_untracked_deleted" in (
        tmp_path / "state" / "gui-commit-audit.jsonl"
    ).read_text(encoding="utf-8")


def test_delete_untracked_rejects_tracked_modifications(tmp_path: Path):
    repo = _repository(tmp_path)
    (repo / "tracked.txt").write_text("after\n", encoding="utf-8")
    manager = _manager(tmp_path, repo)
    row = manager.snapshot(driver_active=False)["repositories"][0]

    with pytest.raises(WorkspaceChangeError, match="only untracked files"):
        manager.delete_untracked(
            selections={"core": ["tracked.txt"]},
            expected_digests={"core": row["status_digest"]},
        )

    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "after\n"


def test_delete_untracked_requires_fresh_workspace_digest(tmp_path: Path):
    repo = _repository(tmp_path)
    generated = repo / "generated.pyc"
    generated.write_bytes(b"bytecode")
    manager = _manager(tmp_path, repo)
    row = manager.snapshot(driver_active=False)["repositories"][0]
    (repo / "other.tmp").write_text("newer change\n", encoding="utf-8")

    with pytest.raises(WorkspaceChangeError, match="changed after inspection"):
        manager.delete_untracked(
            selections={"core": ["generated.pyc"]},
            expected_digests={"core": row["status_digest"]},
        )

    assert generated.exists()
