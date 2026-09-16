"""Tests for project bootstrap (inspect + scaffold)."""

from pathlib import Path

import pytest
import yaml

from execraft.bootstrap import (
    DiscoveredRepo,
    DiscoveryReport,
    discover,
    doctor_project,
    scaffold_project,
)
from execraft.project import load_project


class TestDiscover:
    def test_discovers_single_git_repo(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _init_git(tmp_path)
        (tmp_path / "README.md").write_text("# test")
        _git_add_all(tmp_path)

        report = discover(tmp_path)
        assert len(report.repositories) >= 1
        assert report.source_root == str(tmp_path)

    def test_detects_python(self, tmp_path):
        _init_git(tmp_path)
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("print('hello')")
        _git_add_all(tmp_path)

        report = discover(tmp_path)
        assert "python" in report.languages

    def test_detects_docker(self, tmp_path):
        _init_git(tmp_path)
        (tmp_path / "Dockerfile").write_text("FROM ubuntu")
        _git_add_all(tmp_path)

        report = discover(tmp_path)
        assert "docker" in report.languages

    def test_detects_ros(self, tmp_path):
        _init_git(tmp_path)
        (tmp_path / "package.xml").write_text("<package/>")
        _git_add_all(tmp_path)

        report = discover(tmp_path)
        assert "ros" in report.languages

    def test_detects_docker_compose(self, tmp_path):
        _init_git(tmp_path)
        (tmp_path / "docker-compose.yml").write_text("version: '3'")
        _git_add_all(tmp_path)

        report = discover(tmp_path)
        assert report.has_docker_compose

    def test_derive_project_id_from_directory_name(self, tmp_path):
        source = tmp_path / "my-cool-project"
        source.mkdir()
        report = discover(source)
        assert report.project_id == "my-cool-project"

    def test_empty_directory_reports_unresolved(self, tmp_path):
        report = discover(tmp_path)
        assert len(report.unresolved_choices) > 0


class TestScaffold:
    def test_generates_valid_project_yaml(self, tmp_path):
        source = tmp_path / "srcroot"
        source.mkdir()
        (source / "repo1").mkdir()
        report = DiscoveryReport(project_id="test", source_root=str(source))
        report.repositories = [_make_discovered_repo("repo1", str(source / "repo1"))]
        output = tmp_path / "projects"
        project_file = scaffold_project(report, output)
        assert project_file.is_file()

        descriptor = load_project(output / "test")
        assert descriptor.id == "test"
        assert len(descriptor.repositories) == 1
        templates = output / "test" / "task_templates"
        assert (templates / "README.md").is_file()
        assert (templates / ".gitignore").read_text() == "/RUNTIME_STATUS.md\n"
        assert "Historical append-only record" in (templates / "HANDOFF.md").read_text()

    def test_scaffold_roundtrip(self, tmp_path):
        source = tmp_path / "srcroot"
        source.mkdir()
        (source / "a").mkdir()
        (source / "b").mkdir()
        report = DiscoveryReport(project_id="roundtrip", source_root=str(source))
        report.repositories = [
            _make_discovered_repo("a", str(source / "a")),
            _make_discovered_repo("b", str(source / "b")),
        ]
        project_file = scaffold_project(report, tmp_path / "projects")
        raw = yaml.safe_load(project_file.read_text())
        assert raw["schema_version"] == 2
        assert raw["project"] == "roundtrip"
        assert raw["path_base"] == "project_directory"
        assert raw["task_templates"] == "task_templates"
        assert len(raw["repositories"]) == 2

    def test_raises_on_existing_project(self, tmp_path):
        source = tmp_path / "src"
        source.mkdir()
        (source / "r").mkdir()
        report = DiscoveryReport(project_id="dup", source_root=str(source))
        report.repositories = [_make_discovered_repo("r", str(source / "r"))]
        output = tmp_path / "projects"
        scaffold_project(report, output)
        with pytest.raises(Exception):
            scaffold_project(report, output)

    def test_dry_run_does_not_write(self, tmp_path):
        source = tmp_path / "src"
        source.mkdir()
        (source / "r").mkdir()
        report = DiscoveryReport(project_id="dry", source_root=str(source))
        report.repositories = [_make_discovered_repo("r", str(source / "r"))]
        output = tmp_path / "projects"
        project_file = scaffold_project(report, output, dry_run=True)
        assert not project_file.exists()


class TestDoctor:
    def test_healthy_project(self, tmp_path):
        _init_git(tmp_path)
        report = DiscoveryReport(project_id="healthy", source_root=str(tmp_path))
        report.repositories = [_make_discovered_repo("root", str(tmp_path))]
        output = tmp_path / "projects"
        scaffold_project(report, output)
        issues = doctor_project(output / "healthy" / "project.yaml", tmp_path)
        assert issues == []

    def test_missing_repo_reported(self, tmp_path):
        source = tmp_path / "src"
        source.mkdir()
        report = DiscoveryReport(project_id="missing", source_root=str(source))
        report.repositories = [_make_discovered_repo("ghost", str(source / "ghost"))]
        project_file = scaffold_project(report, tmp_path / "projects")
        issues = doctor_project(project_file, source)
        assert any("not found" in i for i in issues)

    def test_unhealthy_project_returns_issues(self, tmp_path):
        project_file = tmp_path / "projects" / "bad" / "project.yaml"
        project_file.parent.mkdir(parents=True)
        project_file.write_text("schema_version: 1\nproject: bad\nrepositories: []\n")
        issues = doctor_project(project_file, tmp_path)
        assert len(issues) > 0


def _init_git(path: Path) -> None:
    import subprocess
    subprocess.run(["git", "init"], cwd=str(path), capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(path), capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), capture_output=True)


def _git_add_all(path: Path) -> None:
    import subprocess
    subprocess.run(["git", "add", "-A"], cwd=str(path), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(path), capture_output=True)


def _make_discovered_repo(repo_id: str, path: str) -> DiscoveredRepo:
    return DiscoveredRepo(
        id=repo_id,
        path=path,
        branch="main",
        has_python=True,
        base_branch="main",
    )


def test_scaffold_writes_portable_opencode_endpoint_registry(tmp_path):
    source = tmp_path / "srcroot"
    source.mkdir()
    (source / "repo1").mkdir()
    report = DiscoveryReport(project_id="registry", source_root=str(source))
    report.repositories = [_make_discovered_repo("repo1", str(source / "repo1"))]

    scaffold_project(report, tmp_path / "projects")

    registry_path = (
        tmp_path
        / "projects"
        / "registry"
        / "provider_overrides"
        / "opencode"
        / "providers.yaml"
    )
    registry = yaml.safe_load(registry_path.read_text())
    endpoint = registry["endpoints"]["local-ollama"]
    assert endpoint["provider_id"] == "ollama-local"
    assert endpoint["base_url_env"] == "EXECRAFT_OLLAMA_LOCAL_URL"
    assert "qwen2.5-coder:14b" in endpoint["models"]


def test_project_doctor_reports_invalid_opencode_registry(tmp_path):
    source = tmp_path / "srcroot"
    source.mkdir()
    repo = source / "repo1"
    repo.mkdir()
    _init_git(repo)
    report = DiscoveryReport(project_id="invalid-registry", source_root=str(source))
    report.repositories = [_make_discovered_repo("repo1", str(repo))]
    project_file = scaffold_project(report, tmp_path / "projects")
    registry_path = (
        project_file.parent
        / "provider_overrides"
        / "opencode"
        / "providers.yaml"
    )
    registry_path.write_text("schema_version: 999\nendpoints: {}\n")

    issues = doctor_project(project_file, source)

    assert any("unsupported OpenCode provider registry" in issue for issue in issues)


def test_scaffold_enables_validated_decomposition_and_parallel_shard_policy(tmp_path):
    source = tmp_path / "srcroot"
    source.mkdir()
    (source / "repo1").mkdir()
    report = DiscoveryReport(project_id="sharded", source_root=str(source))
    report.repositories = [_make_discovered_repo("repo1", str(source / "repo1"))]

    project_file = scaffold_project(report, tmp_path / "projects")
    agents = yaml.safe_load((project_file.parent / "agents.yaml").read_text())

    assert agents["scheduling"]["allow_same_provider_review"] is True
    decomposition = agents["scheduling"]["decomposition"]
    assert decomposition["enabled"] is True
    assert decomposition["maximum_shard_complexity"] == 55
    parallel = agents["scheduling"]["parallel_shards"]
    assert parallel["enabled"] is True
    assert parallel["stages"] == ["implement", "review", "fix_review"]
    recovery = agents["scheduling"]["recovery_playbooks"]
    assert recovery["enabled"] is True
    assert recovery["review_exhausted"] == {
        "enabled": True,
        "max_rescue_cycles": 2,
        "max_findings": 32,
        "prefer_non_supervisor_fixer": True,
        "allow_supervisor_fallback": False,
    }
    assert agents["supervisor"] == {
        "enabled": True,
        "agents": ["codex", "claude-code"],
        "skill": "ai-supervise",
        "max_attempts_per_incident": 3,
        "max_agent_delegations": 6,
        "max_delegation_rounds": 2,
        "max_runtime_minutes": 60,
        "ask_human_when_uncertain": True,
        "require_human_for_destructive_actions": True,
        "auto_decision": {
            "enabled": False,
            "minimum_weight": 70,
            "minimum_margin": 20,
            "max_per_incident": 2,
        },
    }
    assert "supervise" in agents["providers"]["codex"]["capabilities"]
    assert "supervise" in agents["providers"]["claude"]["capabilities"]
    assert "supervise" not in agents["providers"]["antigravity"]["capabilities"]
    zen = agents["providers"]["opencode_zen_free"]
    assert set(zen["capabilities"]) == {
        "decompose", "implement", "review", "fix_review"
    }
    assert zen["max_complexity_by_capability"]["implement"] == 45
