"""Tests for boundary guardrail checks."""

from pathlib import Path

import pytest

from execraft.guard import (
    BANNED_CONTENT_PATTERNS,
    BANNED_PATH_PATTERNS,
    is_banned_path,
    report,
    scan_repository,
)


class TestBannedPathPatterns:
    def test_banned_patterns_are_defined(self):
        assert len(BANNED_PATH_PATTERNS) >= 5

    def test_matches_agents_dir(self):
        assert is_banned_path(".agents/skills/foo.yaml")

    def test_matches_claude_dir(self):
        assert is_banned_path(".claude/settings.json")

    def test_matches_codex_dir(self):
        assert is_banned_path(".codex/tasks/foo.yaml")
        assert is_banned_path(".gemini/settings.json")

    def test_matches_opencode_dir(self):
        assert is_banned_path(".opencode/config.json")

    def test_matches_agents_md(self):
        assert is_banned_path("AGENTS.md")

    def test_matches_claude_md(self):
        assert is_banned_path("CLAUDE.md")

    def test_matches_opencode_json(self):
        assert is_banned_path("opencode.json")
        assert is_banned_path("opencode.jsonc")

    def test_matches_ai_task_env(self):
        assert is_banned_path(".ai-task.env")

    def test_matches_ai_workspace_dir(self):
        assert is_banned_path(".ai-workspace/state.json")

    def test_matches_execraft_dir(self):
        assert is_banned_path(".execraft/workspace.yaml")

    def test_matches_tools_ai(self):
        assert is_banned_path("tools/ai/verify.sh")

    def test_matches_docs_ai(self):
        assert is_banned_path("docs/ai/tasks/TASK.yaml")

    def test_does_not_match_normal_files(self):
        assert not is_banned_path("src/main.py")
        assert not is_banned_path("README.md")
        assert not is_banned_path("Dockerfile")
        assert not is_banned_path("deployment/config.yaml")
        assert not is_banned_path("docs/architecture.md")


class TestContentPatterns:
    def test_content_patterns_are_defined(self):
        assert len(BANNED_CONTENT_PATTERNS) >= 1

    def test_matches_tools_ai_verify_reference(self):
        assert BANNED_CONTENT_PATTERNS[0].search("run tools/ai/verify.sh")
        assert BANNED_CONTENT_PATTERNS[0].search("calls tools/ai/verify.sh")


class TestScanRepository:
    def test_detects_banned_paths_in_git_repo(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _init_git(tmp_path)
        banned = tmp_path / ".agents"
        banned.mkdir()
        (banned / "skill.yaml").write_text("name: test")
        (tmp_path / "README.md").write_text("# OK")
        _git_add_all(tmp_path)

        findings = list(scan_repository(tmp_path))
        paths = [p for p, r in findings]
        assert any(".agents/skill.yaml" in p for p in paths)
        assert not any("README.md" in p for p in paths)

    def test_detects_content_patterns(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _init_git(tmp_path)
        bad = tmp_path / "ci.sh"
        bad.write_text("run tools/ai/verify.sh")
        (tmp_path / "README.md").write_text("# OK")
        _git_add_all(tmp_path)

        findings = list(scan_repository(tmp_path, include_content_check=True))
        paths = [p for p, r in findings]
        assert any("ci.sh" in p for p in paths)

    def test_ignores_content_patterns_when_disabled(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _init_git(tmp_path)
        bad = tmp_path / "ci.sh"
        bad.write_text("run tools/ai/verify.sh")
        (tmp_path / "README.md").write_text("# OK")
        _git_add_all(tmp_path)

        findings = list(scan_repository(tmp_path, include_content_check=False))
        paths = [p for p, r in findings]
        assert not any("ci.sh" in p for p in paths)

    def test_handles_non_git_directory(self, tmp_path):
        banned = tmp_path / ".agents"
        banned.mkdir()
        (banned / "skill.yaml").write_text("name: test")

        findings = list(scan_repository(tmp_path))
        paths = [p for p, r in findings]
        assert any(".agents/skill.yaml" in p for p in paths)

    def test_empty_repo_returns_no_findings(self, tmp_path):
        (tmp_path / "README.md").write_text("# OK")
        findings = list(scan_repository(tmp_path))
        assert findings == []


class TestReport:
    def test_clean_repo_returns_zero(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _init_git(tmp_path)
        (tmp_path / "README.md").write_text("# OK")
        _git_add_all(tmp_path)
        assert report(tmp_path) == 0

    def test_violated_repo_returns_one(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _init_git(tmp_path)
        banned = tmp_path / ".agents"
        banned.mkdir()
        (banned / "skill.yaml").write_text("name: test")
        _git_add_all(tmp_path)
        assert report(tmp_path) == 1

    def test_print_output_clean(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        _init_git(tmp_path)
        (tmp_path / "README.md").write_text("# OK")
        _git_add_all(tmp_path)
        report(tmp_path)
        captured = capsys.readouterr()
        assert "OK" in captured.out

    def test_print_output_violations(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        _init_git(tmp_path)
        banned = tmp_path / ".agents"
        banned.mkdir()
        (banned / "skill.yaml").write_text("name: test")
        _git_add_all(tmp_path)
        report(tmp_path)
        captured = capsys.readouterr()
        assert "VIOLATIONS" in captured.out


def _init_git(path: Path) -> None:
    import subprocess
    subprocess.run(["git", "init"], cwd=str(path), capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(path), capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), capture_output=True)


def _git_add_all(path: Path) -> None:
    import subprocess
    subprocess.run(["git", "add", "-A"], cwd=str(path), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(path), capture_output=True)
