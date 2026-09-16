"""Tests for execraft.browser — browser agent module."""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from execraft.browser import BrowserAgent, FakeBrowserAdapter
from execraft.browser.adapter import (
    ApplyResult,
    ExecuteResult,
    PrepareResult,
    ProbeResult,
    RunStatus,
    sha256_data,
)
from execraft.browser.archive import ArchiveBundler, _generate_run_id
from execraft.persistence import sha256_file
from execraft.browser.validator import ArchiveValidator
from execraft.browser.apply import FileApplier
from execraft.browser.playwright_adapter import PlaywrightProbeAdapter


# ---------------------------------------------------------------------------
# adapter dataclass tests
# ---------------------------------------------------------------------------


class TestProbeResult:
    def test_default_available(self) -> None:
        r = ProbeResult(available=True)
        assert r.available is True
        assert r.browser_version is None
        assert r.message == ""

    def test_full_constructor(self) -> None:
        r = ProbeResult(available=False, browser_version="1.0", message="fail")
        assert r.available is False
        assert r.browser_version == "1.0"
        assert r.message == "fail"


class TestPrepareResult:
    def test_fields(self) -> None:
        r = PrepareResult(run_id="abc", archive_path=Path("/a.tar.gz"), bundle_manifest={"k": "v"})
        assert r.run_id == "abc"
        assert r.archive_path == Path("/a.tar.gz")
        assert r.bundle_manifest == {"k": "v"}


class TestExecuteResult:
    def test_defaults(self) -> None:
        r = ExecuteResult(run_id="abc", success=True)
        assert r.run_id == "abc"
        assert r.success is True
        assert r.changed_files == []
        assert r.message == ""


class TestRunStatus:
    def test_fields(self) -> None:
        r = RunStatus(run_id="abc", state="completed", message="done")
        assert r.run_id == "abc"
        assert r.state == "completed"
        assert r.message == "done"


class TestApplyResult:
    def test_defaults(self) -> None:
        r = ApplyResult(run_id="abc")
        assert r.run_id == "abc"
        assert r.files_applied == []
        assert r.files_backed_up == []
        assert r.verification_passed is False
        assert r.handoff_updated is False


# ---------------------------------------------------------------------------
# sha256 helpers
# ---------------------------------------------------------------------------


class TestSha256:
    def test_data(self) -> None:
        h = sha256_data(b"hello")
        assert len(h) == 64
        assert h == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"

    def test_file(self, tmp_path: Path) -> None:
        f = tmp_path / "test.txt"
        f.write_text("hello")
        h = sha256_file(f)
        assert len(h) == 64
        assert h == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"


# ---------------------------------------------------------------------------
# FakeBrowserAdapter tests
# ---------------------------------------------------------------------------


class TestFakeBrowserAdapter:
    @pytest.mark.asyncio
    async def test_login(self) -> None:
        adapter = FakeBrowserAdapter()
        result = await adapter.login()
        assert result["authenticated"] is True

    @pytest.mark.asyncio
    async def test_login_with_profile(self) -> None:
        adapter = FakeBrowserAdapter()
        result = await adapter.login(Path("/tmp/profile"))
        assert result["profile"] == "/tmp/profile"

    @pytest.mark.asyncio
    async def test_login_failure(self) -> None:
        adapter = FakeBrowserAdapter(fail_login=True)
        with pytest.raises(RuntimeError, match="Simulated login failure"):
            await adapter.login()

    @pytest.mark.asyncio
    async def test_probe(self) -> None:
        adapter = FakeBrowserAdapter()
        result = await adapter.probe()
        assert result.available is True
        assert result.browser_version == "fake/1.0"

    @pytest.mark.asyncio
    async def test_prepare(self, tmp_path: Path) -> None:
        adapter = FakeBrowserAdapter()
        archive = tmp_path / "bundle.tar.gz"
        manifest = {"task_id": "test"}
        result = await adapter.prepare(archive, manifest)
        assert result.run_id
        assert result.archive_path == archive
        assert result.bundle_manifest == manifest

    @pytest.mark.asyncio
    async def test_execute(self, tmp_path: Path) -> None:
        adapter = FakeBrowserAdapter(repo_prefix="myrepo")
        archive = tmp_path / "bundle.tar.gz"
        prepare = await adapter.prepare(archive, {})
        result = await adapter.execute(prepare.run_id)
        assert result.success is True
        assert len(result.changed_files) == 1
        assert result.changed_files[0]["action"] == "create"
        assert result.changed_files[0]["path"] == "myrepo/test_file.md"

    @pytest.mark.asyncio
    async def test_execute_unknown_run(self) -> None:
        adapter = FakeBrowserAdapter()
        result = await adapter.execute("unknown")
        assert result.success is False
        assert "not found" in result.message

    @pytest.mark.asyncio
    async def test_execute_failure(self) -> None:
        adapter = FakeBrowserAdapter(fail_execute=True)
        archive = Path("/tmp/fake.tar.gz")
        prepare = await adapter.prepare(archive, {})
        with pytest.raises(RuntimeError, match="Simulated execute failure"):
            await adapter.execute(prepare.run_id)

    @pytest.mark.asyncio
    async def test_status(self, tmp_path: Path) -> None:
        adapter = FakeBrowserAdapter()
        archive = tmp_path / "bundle.tar.gz"
        prepare = await adapter.prepare(archive, {})
        status = await adapter.status(prepare.run_id)
        assert status.state == "prepared"

    @pytest.mark.asyncio
    async def test_status_unknown(self) -> None:
        adapter = FakeBrowserAdapter()
        status = await adapter.status("unknown")
        assert status.state == "failed"

    @pytest.mark.asyncio
    async def test_full_lifecycle(self, tmp_path: Path) -> None:
        adapter = FakeBrowserAdapter()
        login = await adapter.login()
        assert login["authenticated"] is True

        probe = await adapter.probe()
        assert probe.available is True

        archive = tmp_path / "bundle.tar.gz"
        prepare = await adapter.prepare(archive, {"task_id": "test"})
        assert prepare.run_id

        execute = await adapter.execute(prepare.run_id)
        assert execute.success is True

        status = await adapter.status(prepare.run_id)
        assert status.state == "completed"


@pytest.mark.asyncio
async def test_playwright_probe_is_non_destructive() -> None:
    result = await PlaywrightProbeAdapter().probe()
    assert isinstance(result.available, bool)
    assert result.message

    @pytest.mark.asyncio
    async def test_state_persists_across_adapter_instances(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        first = FakeBrowserAdapter(state_dir=state_dir)
        prepared = await first.prepare(
            tmp_path / "bundle.tar.gz",
            {"run_id": "persisted", "repos": {"repo": {}}},
        )
        second = FakeBrowserAdapter(state_dir=state_dir)
        executed = await second.execute(prepared.run_id)
        assert executed.success is True
        assert executed.changed_files[0]["path"].startswith("repo/")


# ---------------------------------------------------------------------------
# ArchiveBundler tests
# ---------------------------------------------------------------------------


class TestArchiveBundler:
    def test_bundle_with_task_repos(self, tmp_path: Path) -> None:
        ws = tmp_path / "workspace"
        ws.mkdir()
        runs_dir = ws / ".execraft" / "runs"

        # Create a task-owned repo
        repo = tmp_path / "repo1"
        repo.mkdir()
        (repo / "file.txt").write_text("hello")
        (repo / "subdir").mkdir()
        (repo / "subdir" / "nested.txt").write_text("nested")

        bundler = ArchiveBundler(ws, runs_dir)
        archive_path, manifest = bundler.bundle(
            task_id="test_task",
            task_repos={"repo1": repo},
            runtime_repos={},
            dossier_files={},
        )
        assert archive_path.exists()
        assert tarfile.is_tarfile(archive_path)
        assert manifest["task_id"] == "test_task"
        assert "repo1" in manifest["repos"]
        assert manifest["repos"]["repo1"]["is_runtime"] is False
        assert "file.txt" in str(manifest["files"].keys())

    def test_bundle_with_dossier_files(self, tmp_path: Path) -> None:
        ws = tmp_path / "workspace"
        ws.mkdir()
        runs_dir = ws / ".execraft" / "runs"

        dossier = tmp_path / "dossier"
        dossier.mkdir()
        (dossier / "BRIEF.md").write_text("# Brief")
        (dossier / "PLAN.md").write_text("# Plan")

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "code.py").write_text("print('hi')")

        bundler = ArchiveBundler(ws, runs_dir)
        archive_path, manifest = bundler.bundle(
            task_id="test",
            task_repos={"repo": repo},
            runtime_repos={},
            dossier_files={"BRIEF.md": dossier / "BRIEF.md", "PLAN.md": dossier / "PLAN.md"},
        )

        with tarfile.open(archive_path, "r:gz") as tar:
            names = tar.getnames()
            assert any("dossier/BRIEF.md" in n for n in names)
            assert any("dossier/PLAN.md" in n for n in names)

        assert manifest["files"].get("dossier/BRIEF.md")

    def test_bundle_ignores_pycache(self, tmp_path: Path) -> None:
        ws = tmp_path / "workspace"
        ws.mkdir()
        runs_dir = ws / ".execraft" / "runs"

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "__pycache__").mkdir()
        (repo / "__pycache__" / "cache.pyc").write_text("cached")
        (repo / "real.py").write_text("real")

        bundler = ArchiveBundler(ws, runs_dir)
        archive_path, manifest = bundler.bundle(
            task_id="test", task_repos={"repo": repo}, runtime_repos={}, dossier_files={}
        )

        assert "__pycache__" not in str(manifest["files"].keys())
        assert "repos/repo/real.py" in manifest["files"]

    def test_bundle_manifest_written(self, tmp_path: Path) -> None:
        ws = tmp_path / "workspace"
        ws.mkdir()
        runs_dir = ws / ".execraft" / "runs"

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "a.txt").write_text("a")

        bundler = ArchiveBundler(ws, runs_dir)
        archive_path, manifest = bundler.bundle(
            task_id="test", task_repos={"repo": repo}, runtime_repos={}, dossier_files={}
        )

        manifest_file = archive_path.parent / "bundle-manifest.json"
        assert manifest_file.exists()
        loaded = json.loads(manifest_file.read_text())
        assert loaded["task_id"] == "test"
        assert loaded["run_id"]

    def test_bundle_no_repos(self, tmp_path: Path) -> None:
        ws = tmp_path / "workspace"
        ws.mkdir()
        runs_dir = ws / ".execraft" / "runs"

        bundler = ArchiveBundler(ws, runs_dir)
        archive_path, manifest = bundler.bundle(
            task_id="test", task_repos={}, runtime_repos={}, dossier_files={}
        )
        assert archive_path.exists()
        assert manifest["files"] == {}


# ---------------------------------------------------------------------------
# _generate_run_id tests
# ---------------------------------------------------------------------------


class TestGenerateRunId:
    def test_returns_string(self) -> None:
        rid = _generate_run_id("test")
        assert isinstance(rid, str)
        assert len(rid) == 16

    def test_different_ids(self) -> None:
        id1 = _generate_run_id("test")
        id2 = _generate_run_id("test")
        # Different due to time_ns
        assert id1 != id2


# ---------------------------------------------------------------------------
# ArchiveValidator tests
# ---------------------------------------------------------------------------


class TestArchiveValidator:
    def test_missing_archive(self, tmp_path: Path) -> None:
        v = ArchiveValidator(set())
        errors = v.validate_archive(tmp_path / "nonexistent.tar.gz", {"runtime_repos": {}})
        assert len(errors) == 1
        assert "not found" in errors[0]

    def test_invalid_archive(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.tar.gz"
        f.write_text("not a tar file")
        v = ArchiveValidator(set())
        errors = v.validate_archive(f, {"runtime_repos": {}})
        assert len(errors) == 1
        assert "Not a valid tar archive" in errors[0]

    def test_archive_rejects_path_traversal_member(self, tmp_path: Path) -> None:
        import io

        archive = tmp_path / "unsafe.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            info = tarfile.TarInfo("../escaped.txt")
            payload = b"bad"
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        errors = ArchiveValidator(set()).validate_archive(
            archive, {"runtime_repos": [], "repos": {}, "files": {}}
        )
        assert errors == ["Unsafe archive member path: ../escaped.txt"]

    def test_runtime_repo_detected(self, tmp_path: Path) -> None:
        v = ArchiveValidator({"runtime_only"})
        repo_dir = tmp_path / "repos"
        repo_dir.mkdir(parents=True)
        (repo_dir / "runtime_only").mkdir(parents=True)
        (repo_dir / "runtime_only" / "file.txt").write_text("should not be here")

        archive = tmp_path / "bundle.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(repo_dir, arcname="repos")

        errors = v.validate_archive(archive, {"runtime_repos": {"runtime_only": str(tmp_path)}})
        assert len(errors) >= 1
        assert any("Runtime-only" in e for e in errors)

    def test_clean_archive_no_errors(self, tmp_path: Path) -> None:
        v = ArchiveValidator(set())
        repo_dir = tmp_path / "repos"
        repo_dir.mkdir(parents=True)
        (repo_dir / "myrepo").mkdir()
        (repo_dir / "myrepo" / "clean.txt").write_text("clean")

        archive = tmp_path / "bundle.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(repo_dir, arcname="repos")

        errors = v.validate_archive(
            archive,
            {
                "runtime_repos": {},
                "repos": {"myrepo": {"path": str(repo_dir / "myrepo"), "is_runtime": False}},
                "files": {"repos/myrepo/clean.txt": "hash"},
            },
        )
        assert errors == []

    def test_secret_detected(self, tmp_path: Path) -> None:
        v = ArchiveValidator(set())
        repo_dir = tmp_path / "repos"
        repo_dir.mkdir(parents=True)
        (repo_dir / "myrepo").mkdir()
        (repo_dir / "myrepo" / "secret.txt").write_text("api_key = 'sk-12345678901234567890'")

        archive = tmp_path / "bundle.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(repo_dir, arcname="repos")

        errors = v.validate_archive(
            archive,
            {
                "runtime_repos": {},
                "repos": {"myrepo": {"path": str(repo_dir / "myrepo"), "is_runtime": False}},
                "files": {},
            },
        )
        assert any("secret" in e.lower() for e in errors)

    def test_output_ownership_validation(self) -> None:
        v = ArchiveValidator({"runtime"})
        changes = [
            {"path": "myrepo/src/main.py", "action": "modify", "content": "new"},
            {"path": "runtime/deploy.py", "action": "modify", "content": "bad"},
        ]
        errors = v.validate_output(changes, {"myrepo"})
        assert len(errors) == 1
        assert "runtime" in errors[0]

    @pytest.mark.parametrize(
        "path",
        [
            "myrepo/../../escaped.txt",
            "myrepository/not-owned.txt",
            "/absolute/path.txt",
            "other/myrepo/nested.txt",
        ],
    )
    def test_output_ownership_rejects_escaping_or_prefix_spoofing(self, path: str) -> None:
        errors = ArchiveValidator(set()).validate_output(
            [{"path": path, "action": "create", "content": "bad"}], {"myrepo"}
        )
        assert errors

    def test_deletion_ratio_exceeded(self, tmp_path: Path) -> None:
        v = ArchiveValidator(set())
        repo_dir = tmp_path / "repos"
        repo_dir.mkdir(parents=True)
        (repo_dir / "myrepo").mkdir()
        (repo_dir / "myrepo" / "file1.txt").write_text("a")
        (repo_dir / "myrepo" / "file2.txt").write_text("b")
        (repo_dir / "myrepo" / "file3.txt").write_text("c")

        archive = tmp_path / "bundle.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(repo_dir, arcname="repos")

        errors = v.validate_archive(
            archive,
            {
                "runtime_repos": {},
                "repos": {"myrepo": {"path": str(repo_dir / "myrepo"), "is_runtime": False}},
                "files": {"file1.txt": "h1", "file2.txt": "h2", "file3.txt": "h3", "file4.txt": "h4", "file5.txt": "h5"},
            },
        )
        assert len(errors) >= 1
        assert any("deletion" in e.lower() for e in errors)

    def test_fingerprint_conflict_detected(self, tmp_path: Path) -> None:
        target = tmp_path / "target"
        (target / "repo").mkdir(parents=True)
        path = target / "repo/file.txt"
        path.write_text("changed after bundle")
        errors = ArchiveValidator(set()).validate_fingerprints(
            [{"path": "repo/file.txt", "action": "modify", "content": "candidate"}],
            {"repo": target / "repo"},
            {"files": {"repos/repo/file.txt": sha256_data(b"original")}},
        )
        assert errors == ["Source fingerprint changed: repo/file.txt"]

    def test_create_cannot_overwrite_bundled_file(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        (root / "file.txt").write_text("original")
        errors = ArchiveValidator(set()).validate_fingerprints(
            [{"path": "repo/file.txt", "action": "create", "content": "candidate"}],
            {"repo": root},
            {"files": {"repos/repo/file.txt": sha256_data(b"original")}},
        )
        assert errors == ["Create conflicts with existing file: repo/file.txt"]

    def test_repository_symlink_escape_detected(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        outside = tmp_path / "outside"
        root.mkdir()
        outside.mkdir()
        (root / "link").symlink_to(outside, target_is_directory=True)
        errors = ArchiveValidator(set()).validate_fingerprints(
            [{"path": "repo/link/file.txt", "action": "create", "content": "bad"}],
            {"repo": root},
            {"files": {}},
        )
        assert errors == ["Path escapes repository root: repo/link/file.txt"]

    def test_candidate_deletion_ratio_enforced(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        (root / "one.txt").write_text("one")
        errors = ArchiveValidator(set()).validate_fingerprints(
            [{"path": "repo/one.txt", "action": "delete"}],
            {"repo": root},
            {
                "files": {
                    "repos/repo/one.txt": sha256_data(b"one"),
                    "repos/repo/two.txt": sha256_data(b"two"),
                }
            },
        )
        assert any("deletion ratio" in error for error in errors)


# ---------------------------------------------------------------------------
# BrowserAgent integration tests
# ---------------------------------------------------------------------------


class TestBrowserAgent:
    @pytest.mark.asyncio
    async def test_full_fake_lifecycle(self, tmp_path: Path) -> None:
        ws = tmp_path / "workspace"
        ws.mkdir()
        runs_dir = ws / ".execraft" / "runs"

        repo = tmp_path / "myrepo"
        repo.mkdir()
        (repo / "main.py").write_text("print('hello')")

        adapter = FakeBrowserAdapter()
        agent = BrowserAgent(adapter, ws, runs_dir=runs_dir)

        login_result = await agent.login()
        assert login_result["authenticated"] is True

        probe_result = await agent.probe()
        assert probe_result.available is True

        prepare_result = await agent.prepare(
            task_id="test_task",
            task_repos={"myrepo": repo},
        )
        assert prepare_result.run_id
        assert prepare_result.archive_path.exists()

        execute_result = await agent.execute(prepare_result.run_id)
        assert execute_result.success is True
        assert len(execute_result.changed_files) == 1

        status_result = await agent.status(prepare_result.run_id)
        assert status_result.state == "completed"

        target = tmp_path / "target"
        target.mkdir()
        apply_result = await agent.apply(
            run_id=prepare_result.run_id,
            repository_roots={"myrepo": target / "myrepo"},
            output_files=execute_result.changed_files,
            task_repo_ids={"myrepo"},
            runtime_repo_ids=set(),
            bundle_manifest=prepare_result.bundle_manifest,
            verify_commands=["true"],
        )
        assert apply_result.files_applied
        assert (target / "myrepo" / "test_file.md").exists()

    @pytest.mark.asyncio
    async def test_apply_rejects_runtime_changes(self, tmp_path: Path) -> None:
        ws = tmp_path / "workspace"
        ws.mkdir()
        runs_dir = ws / ".execraft" / "runs"

        adapter = FakeBrowserAdapter()
        agent = BrowserAgent(adapter, ws, runs_dir=runs_dir)

        target = tmp_path / "target"
        target.mkdir()
        apply_result = await agent.apply(
            run_id="test",
            repository_roots={"myrepo": target / "myrepo"},
            output_files=[{"path": "runtime_only/deploy.py", "action": "modify", "content": "bad"}],
            task_repo_ids={"myrepo"},
            runtime_repo_ids={"runtime_only"},
            bundle_manifest={"repos": {"myrepo": {}}, "files": {}},
        )
        assert apply_result.files_applied == []
        assert "Ownership validation" in apply_result.message

    @pytest.mark.asyncio
    async def test_prepare_rejects_secret_before_adapter(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "config.txt").write_text("api_key = 'not-a-real-secret-value'")
        agent = BrowserAgent(FakeBrowserAdapter(), workspace)
        with pytest.raises(ValueError, match="input bundle rejected"):
            await agent.prepare("secret-test", {"repo": repo})

    @pytest.mark.asyncio
    async def test_handoff_update(self, tmp_path: Path) -> None:
        ws = tmp_path / "workspace"
        ws.mkdir()
        runs_dir = ws / ".execraft" / "runs"
        backup_dir = runs_dir / "backups"

        repo = tmp_path / "myrepo"
        repo.mkdir()
        (repo / "main.py").write_text("old")

        adapter = FakeBrowserAdapter()
        agent = BrowserAgent(adapter, ws, runs_dir=runs_dir, backup_dir=backup_dir)

        prepare = await agent.prepare("test", {"myrepo": repo})
        execute = await agent.execute(prepare.run_id)

        target = tmp_path / "target"
        target.mkdir()
        handoff = target / "HANDOFF.md"
        handoff.write_text("# Handoff\n\nExisting content.\n")

        apply_result = await agent.apply(
            run_id=prepare.run_id,
            repository_roots={"myrepo": target / "myrepo"},
            output_files=execute.changed_files,
            task_repo_ids={"myrepo"},
            runtime_repo_ids=set(),
            bundle_manifest=prepare.bundle_manifest,
            verify_commands=["true"],
            handoff_path=handoff,
        )
        assert apply_result.handoff_updated is True
        updated = handoff.read_text()
        assert "Browser run" in updated


class TestFileApplierSafety:
    def test_apply_rejects_path_escape(self, tmp_path: Path) -> None:
        target = tmp_path / "target"
        target.mkdir()
        applier = FileApplier(tmp_path / "backups")
        result = applier.apply(
            "escape",
            [{"path": "repo/../../escaped.txt", "action": "create", "content": "bad"}],
            target,
        )
        assert "Rolled back" in result.message
        assert not (tmp_path / "escaped.txt").exists()

    def test_rollback_restores_modified_and_removes_created_files(self, tmp_path: Path) -> None:
        target = tmp_path / "target"
        (target / "repo").mkdir(parents=True)
        original = target / "repo" / "original.txt"
        original.write_text("before")
        applier = FileApplier(tmp_path / "backups")
        result = applier.apply(
            "rollback",
            [
                {"path": "repo/original.txt", "action": "modify", "content": "after"},
                {"path": "repo/created.txt", "action": "create", "content": "new"},
            ],
            target,
        )
        assert result.verification_passed is True
        restored = applier.rollback("rollback", target)
        assert restored
        assert original.read_text() == "before"
        assert not (target / "repo" / "created.txt").exists()

    def test_verification_failure_rolls_back(self, tmp_path: Path) -> None:
        target = tmp_path / "target"
        (target / "repo").mkdir(parents=True)
        original = target / "repo" / "file.txt"
        original.write_text("before")
        applier = FileApplier(tmp_path / "backups")
        result = applier.apply(
            "verify-fail",
            [{"path": "repo/file.txt", "action": "modify", "content": "after"}],
            target,
            verify_commands=["false"],
        )
        assert result.verification_passed is False
        assert original.read_text() == "before"
