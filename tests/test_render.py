"""Tests for execraft.render — workspace shell renderer."""

from __future__ import annotations

import hashlib
import json
import textwrap
from pathlib import Path

import pytest
import yaml

from execraft.agents.opencode_registry import load_opencode_provider_registry

from execraft.render import (
    RenderResult,
    load_project,
    render_claude,
    render_codex,
    render_manifest,
    render_opencode,
    refresh_opencode_providers,
    render_skills,
    render_workspace,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def project_dir(tmp_path: Path) -> Path:
    """Create a minimal project directory structure for testing."""
    root = tmp_path / "Execraft"
    root.mkdir()

    # projects/sample/project.yaml
    sample = root / "projects" / "sample"
    sample.mkdir(parents=True)
    project_yaml = sample / "project.yaml"
    project_yaml.write_text(
        yaml.dump(
            {
                "schema_version": 1,
                "project": "sample",
                "description": "Test project",
                "repositories": [{"id": "test", "path": "test", "role": "test"}],
                "skills_dir": "projects/sample/skills/",
                "opencode_dir": "projects/sample/opencode/",
                "claude_dir": "projects/sample/claude/",
                "codex_dir": "projects/sample/codex/",
            }
        )
    )

    # skills
    skills = sample / "skills" / "ai-test"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("---\nname: ai-test\ndescription: Test skill\n---\nDo the thing.\n")

    # opencode
    oc = sample / "opencode"
    (oc / "agents").mkdir(parents=True)
    (oc / "agents" / "ai-test-agent.md").write_text("---\ndescription: test agent\n---\nBe the agent.\n")
    (oc / "commands").mkdir(parents=True)
    (oc / "commands" / "ai-test-cmd.md").write_text("description: test cmd\nDo it: $ARGUMENTS\n")

    # claude
    cl = sample / "claude"
    cl.mkdir(parents=True)
    (cl / "settings.json").write_text('{"permissions": {"defaultMode": "bypassPermissions"}}')
    (cl / "CLAUDE.md").write_text("@AGENTS.md\n\n# Claude conventions\n")
    (cl / "agents").mkdir()
    (cl / "agents" / "ai-test-agent.md").write_text("---\nname: ai-test-agent\n---\nBe the agent.\n")
    (cl / "commands").mkdir()
    (cl / "commands" / "ai-test-cmd.md").write_text("description: test cmd\nDo it: $ARGUMENTS\n")

    # codex
    cx = sample / "codex"
    cx.mkdir(parents=True)
    (cx / "config.toml").write_text('approval_policy = "never"\n')

    return root


@pytest.fixture()
def workspace_root(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


# ---------------------------------------------------------------------------
# load_project
# ---------------------------------------------------------------------------


class TestLoadProject:
    def test_loads_project_yaml(self, project_dir: Path) -> None:
        project = load_project(project_dir / "projects" / "sample")
        assert project["project"] == "sample"
        assert project["schema_version"] == 1

    def test_missing_project_yaml_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="project.yaml not found"):
            load_project(tmp_path)


# ---------------------------------------------------------------------------
# render_skills
# ---------------------------------------------------------------------------


class TestRenderSkills:
    def test_renders_skill_files(self, project_dir: Path, workspace_root: Path) -> None:
        project = load_project(project_dir / "projects" / "sample")
        files = render_skills(project_dir / "projects" / "sample", workspace_root, project)
        assert len(files) >= 16  # built-in skills plus the project overlay
        project_skill = next(f for f in files if "ai-test" in f.relative_path)
        assert project_skill.source == "project"
        assert project_skill.sha256

    def test_skill_content_matches_source(self, project_dir: Path, workspace_root: Path) -> None:
        project = load_project(project_dir / "projects" / "sample")
        render_skills(project_dir / "projects" / "sample", workspace_root, project)
        rendered = workspace_root / ".agents" / "skills" / "ai-test" / "SKILL.md"
        assert rendered.exists()
        assert "Test skill" in rendered.read_text()

    def test_missing_skills_dir_raises(self, tmp_path: Path, workspace_root: Path) -> None:
        bad = tmp_path / "no_project"
        bad.mkdir()
        (bad / "project.yaml").write_text(yaml.dump({"skills_dir": "missing/"}))
        with pytest.raises(FileNotFoundError, match="Skills directory not found"):
            render_skills(bad, workspace_root, {"skills_dir": "missing/"})


# ---------------------------------------------------------------------------
# render_opencode
# ---------------------------------------------------------------------------


class TestRenderOpenCode:
    def test_renders_agents(self, project_dir: Path, workspace_root: Path) -> None:
        project = load_project(project_dir / "projects" / "sample")
        files = render_opencode(project_dir / "projects" / "sample", workspace_root, project)
        agent_files = [f for f in files if "agents" in f.relative_path]
        assert any("ai-test-agent.md" in f.relative_path for f in agent_files)
        assert any(f.source == "builtin" for f in agent_files)

    def test_renders_commands(self, project_dir: Path, workspace_root: Path) -> None:
        project = load_project(project_dir / "projects" / "sample")
        files = render_opencode(project_dir / "projects" / "sample", workspace_root, project)
        cmd_files = [f for f in files if "commands" in f.relative_path]
        assert any("ai-test-cmd.md" in f.relative_path for f in cmd_files)
        assert any(f.source == "builtin" for f in cmd_files)

    def test_generates_opencode_json(self, project_dir: Path, workspace_root: Path) -> None:
        project = load_project(project_dir / "projects" / "sample")
        render_opencode(project_dir / "projects" / "sample", workspace_root, project)
        oc_json = workspace_root / "opencode.json"
        assert oc_json.exists()
        config = json.loads(oc_json.read_text())
        assert config["$schema"] == "https://opencode.ai/config.json"
        assert config["permission"]["*"] == "ask"
        assert config["permission"]["bash"] == "allow"
        assert config["permission"]["external_directory"] == "deny"

    def test_merges_project_provider_overlay_without_weakening_permissions(
        self, project_dir: Path, workspace_root: Path
    ) -> None:
        project_root = project_dir / "projects" / "sample"
        overlay = project_root / "opencode" / "config.json"
        overlay.write_text(
            json.dumps(
                {
                    "$schema": "unsafe-schema",
                    "permission": {"external_directory": "allow"},
                    "provider": {
                        "ollama": {
                            "npm": "@ai-sdk/openai-compatible",
                            "options": {"baseURL": "http://127.0.0.1:11434/v1"},
                        }
                    },
                }
            )
        )
        project = load_project(project_root)

        render_opencode(project_root, workspace_root, project)

        config = json.loads((workspace_root / "opencode.json").read_text())
        assert config["$schema"] == "https://opencode.ai/config.json"
        assert config["permission"]["external_directory"] == "deny"
        assert config["provider"]["ollama"]["options"]["baseURL"] == (
            "http://127.0.0.1:11434/v1"
        )

    def test_renders_remote_provider_registry_with_environment_override(
        self, project_dir: Path, workspace_root: Path, monkeypatch
    ) -> None:
        project_root = project_dir / "projects" / "sample"
        (project_root / "opencode" / "providers.yaml").write_text(
            """schema_version: 1
endpoints:
  gpu-laptop:
    provider_id: ollama-gpu-a
    base_url: http://10.42.0.107:11434/v1
    base_url_env: EXECRAFT_TEST_OLLAMA_URL
    models:
      qwen3.5:9b:
        name: Qwen 3.5 9B
"""
        )
        monkeypatch.setenv("EXECRAFT_TEST_OLLAMA_URL", "http://192.0.2.18:11434/v1")
        project = load_project(project_root)

        render_opencode(project_root, workspace_root, project)

        config = json.loads((workspace_root / "opencode.json").read_text())
        provider = config["provider"]["ollama-gpu-a"]
        assert provider["options"]["baseURL"] == "http://192.0.2.18:11434/v1"
        assert provider["models"]["qwen3.5:9b"]["name"] == "Qwen 3.5 9B"
        assert config["permission"]["external_directory"] == "deny"

    def test_refresh_syncs_provider_added_after_the_workspace_was_rendered(
        self, project_dir: Path, workspace_root: Path
    ) -> None:
        project_root = project_dir / "projects" / "sample"
        registry_path = project_root / "opencode" / "providers.yaml"
        registry_path.write_text(
            """schema_version: 1
endpoints:
  gpu-laptop:
    provider_id: ollama-gpu-a
    base_url: http://10.42.0.107:11434/v1
    models:
      qwen3.5:9b: {}
"""
        )
        project = load_project(project_root)
        rendered = render_opencode(project_root, workspace_root, project)
        render_manifest(workspace_root, rendered)
        registry_path.write_text(
            """schema_version: 1
endpoints:
  gpu-laptop:
    provider_id: ollama-gpu-a
    base_url: http://10.42.0.107:11434/v1
    models:
      qwen3.5:9b: {}
  local-ollama:
    provider_id: ollama-local
    base_url: http://127.0.0.1:11434/v1
    models:
      qwen3-coder:30b-32k: {}
"""
        )

        changed = refresh_opencode_providers(
            workspace_root, load_opencode_provider_registry(registry_path)
        )

        assert changed == ["ollama-local"]
        config = json.loads((workspace_root / "opencode.json").read_text())
        assert "qwen3-coder:30b-32k" in config["provider"]["ollama-local"]["models"]
        # The generated policy and the pre-existing provider survive the resync.
        assert config["permission"]["external_directory"] == "deny"
        assert "ollama-gpu-a" in config["provider"]
        manifest = json.loads(
            (workspace_root / ".execraft" / "generated-manifest.json").read_text()
        )
        assert manifest["files"]["opencode.json"]["sha256"] == hashlib.sha256(
            (workspace_root / "opencode.json").read_bytes()
        ).hexdigest()

    def test_refresh_is_a_no_op_when_the_workspace_config_matches(
        self, project_dir: Path, workspace_root: Path
    ) -> None:
        project_root = project_dir / "projects" / "sample"
        registry_path = project_root / "opencode" / "providers.yaml"
        registry_path.write_text(
            """schema_version: 1
endpoints:
  local-ollama:
    provider_id: ollama-local
    base_url: http://127.0.0.1:11434/v1
    models:
      qwen3-coder:30b-32k: {}
"""
        )
        project = load_project(project_root)
        render_opencode(project_root, workspace_root, project)
        before = (workspace_root / "opencode.json").read_bytes()

        changed = refresh_opencode_providers(
            workspace_root, load_opencode_provider_registry(registry_path)
        )

        assert changed == []
        assert (workspace_root / "opencode.json").read_bytes() == before

    def test_rejects_provider_declared_in_registry_and_overlay(
        self, project_dir: Path, workspace_root: Path
    ) -> None:
        project_root = project_dir / "projects" / "sample"
        (project_root / "opencode" / "providers.yaml").write_text(
            """schema_version: 1
endpoints:
  gpu-laptop:
    provider_id: ollama-gpu-a
    base_url: http://10.42.0.107:11434/v1
    models:
      qwen3.5:9b: {}
"""
        )
        (project_root / "opencode" / "config.json").write_text(
            json.dumps(
                {
                    "provider": {
                        "ollama-gpu-a": {
                            "options": {"baseURL": "http://127.0.0.1:11434/v1"}
                        }
                    }
                }
            )
        )
        project = load_project(project_root)

        with pytest.raises(ValueError, match="not both"):
            render_opencode(project_root, workspace_root, project)

    def test_rejects_invalid_project_provider_overlay(
        self, project_dir: Path, workspace_root: Path
    ) -> None:
        project_root = project_dir / "projects" / "sample"
        (project_root / "opencode" / "config.json").write_text("[]")
        project = load_project(project_root)

        with pytest.raises(ValueError, match="must contain an object"):
            render_opencode(project_root, workspace_root, project)

    def test_review_agent_allows_only_review_skill_and_denies_edits(
        self, project_dir: Path, workspace_root: Path
    ) -> None:
        project = load_project(project_dir / "projects" / "sample")
        render_opencode(project_dir / "projects" / "sample", workspace_root, project)

        reviewer = (workspace_root / ".opencode" / "agents" / "ai-reviewer.md").read_text()
        assert "edit: deny" in reviewer
        assert '"*": deny' in reviewer
        assert "ai-review: allow" in reviewer
        assert "todowrite: allow" in reviewer
        assert "external_directory:" not in reviewer
        assert "never start with an unbounded `**/*` glob" in reviewer
        assert '"**/PLAN.md": deny' in reviewer
        assert '"**/PLAN.graph.yaml": deny' in reviewer
        assert '"verdict":"approved"' in reviewer

    def test_architect_allows_private_todos_but_denies_workspace_edits(
        self, project_dir: Path, workspace_root: Path
    ) -> None:
        project = load_project(project_dir / "projects" / "sample")
        render_opencode(project_dir / "projects" / "sample", workspace_root, project)

        architect = (
            workspace_root / ".opencode" / "agents" / "ai-architect.md"
        ).read_text()
        assert "todowrite: allow" in architect
        assert "edit: deny" in architect
        assert "task: deny" in architect
        assert "ai-plan: allow" in architect
        assert "ai-replan: allow" in architect

    def test_renders_fixer_and_tool_free_contract_agents(
        self, project_dir: Path, workspace_root: Path
    ) -> None:
        project = load_project(project_dir / "projects" / "sample")
        render_opencode(project_dir / "projects" / "sample", workspace_root, project)

        agents = workspace_root / ".opencode" / "agents"
        fixer = (agents / "ai-fixer.md").read_text()
        contract = (agents / "ai-contract.md").read_text()
        assert "steps: 100" in fixer
        assert "finish with exactly one JSON object" in fixer
        assert "steps: 2" in contract
        assert '"*": deny' in contract
        assert "bash: false" in contract

    def test_generates_gitignore(self, project_dir: Path, workspace_root: Path) -> None:
        project = load_project(project_dir / "projects" / "sample")
        render_opencode(project_dir / "projects" / "sample", workspace_root, project)
        gi = workspace_root / ".opencode" / ".gitignore"
        assert gi.exists()
        assert "node_modules" in gi.read_text()

    def test_does_not_generate_unneeded_package_json(self, project_dir: Path, workspace_root: Path) -> None:
        project = load_project(project_dir / "projects" / "sample")
        render_opencode(project_dir / "projects" / "sample", workspace_root, project)
        assert not (workspace_root / ".opencode" / "package.json").exists()

    def test_overwrites_existing_opencode_json(self, project_dir: Path, workspace_root: Path) -> None:
        existing = {"$schema": "custom", "custom": True}
        (workspace_root / "opencode.json").write_text(json.dumps(existing))
        project = load_project(project_dir / "projects" / "sample")
        render_opencode(project_dir / "projects" / "sample", workspace_root, project)
        # Should overwrite with canonical config
        data = json.loads((workspace_root / "opencode.json").read_text())
        assert data["$schema"] == "https://opencode.ai/config.json"
        assert "custom" not in data


# ---------------------------------------------------------------------------
# render_claude
# ---------------------------------------------------------------------------


class TestRenderClaude:
    def test_renders_settings(self, project_dir: Path, workspace_root: Path) -> None:
        project = load_project(project_dir / "projects" / "sample")
        files = render_claude(project_dir / "projects" / "sample", workspace_root, project)
        settings_files = [f for f in files if "settings.json" in f.relative_path]
        assert len(settings_files) == 1

    def test_renders_claude_md(self, project_dir: Path, workspace_root: Path) -> None:
        project = load_project(project_dir / "projects" / "sample")
        render_claude(project_dir / "projects" / "sample", workspace_root, project)
        claude_md = workspace_root / "CLAUDE.md"
        assert claude_md.exists()
        assert "@AGENTS.md" in claude_md.read_text()

    def test_renders_agents(self, project_dir: Path, workspace_root: Path) -> None:
        project = load_project(project_dir / "projects" / "sample")
        files = render_claude(project_dir / "projects" / "sample", workspace_root, project)
        agent_files = [f for f in files if "agents" in f.relative_path]
        assert any("ai-test-agent.md" in f.relative_path for f in agent_files)
        assert any(f.source == "builtin" for f in agent_files)

    def test_renders_commands(self, project_dir: Path, workspace_root: Path) -> None:
        project = load_project(project_dir / "projects" / "sample")
        files = render_claude(project_dir / "projects" / "sample", workspace_root, project)
        cmd_files = [f for f in files if "commands" in f.relative_path]
        assert any("ai-test-cmd.md" in f.relative_path for f in cmd_files)
        assert any(f.source == "builtin" for f in cmd_files)


# ---------------------------------------------------------------------------
# render_codex
# ---------------------------------------------------------------------------


class TestRenderCodex:
    def test_renders_config(self, project_dir: Path, workspace_root: Path) -> None:
        project = load_project(project_dir / "projects" / "sample")
        files = render_codex(project_dir / "projects" / "sample", workspace_root, project)
        assert len(files) == 1
        assert "config.toml" in files[0].relative_path

    def test_config_content(self, project_dir: Path, workspace_root: Path) -> None:
        project = load_project(project_dir / "projects" / "sample")
        render_codex(project_dir / "projects" / "sample", workspace_root, project)
        config = workspace_root / ".codex" / "config.toml"
        assert config.exists()
        assert "approval_policy" in config.read_text()


# ---------------------------------------------------------------------------
# render_manifest
# ---------------------------------------------------------------------------


class TestRenderManifest:
    def test_creates_manifest(self, workspace_root: Path) -> None:
        from execraft.render import RenderedFile

        files = [
            RenderedFile(relative_path="test.txt", sha256="abc123", source="project"),
            RenderedFile(relative_path="gen.txt", sha256="def456", source="generated"),
        ]
        result = render_manifest(workspace_root, files)
        assert result.sha256

        manifest_path = workspace_root / ".execraft" / "generated-manifest.json"
        assert manifest_path.exists()
        data = json.loads(manifest_path.read_text())
        assert data["schema_version"] == 1
        assert "test.txt" in data["files"]
        assert data["files"]["test.txt"]["source"] == "project"
        assert data["files"]["gen.txt"]["source"] == "generated"


# ---------------------------------------------------------------------------
# render_workspace (integration)
# ---------------------------------------------------------------------------


class TestRenderWorkspace:
    def test_full_render(self, project_dir: Path, workspace_root: Path) -> None:
        result = render_workspace(
            project_dir / "projects" / "sample",
            workspace_root,
        )
        assert len(result.rendered_files) > 0

        # Skills
        assert (workspace_root / ".agents" / "skills" / "ai-test" / "SKILL.md").exists()
        # OpenCode
        assert (workspace_root / ".opencode" / "agents" / "ai-test-agent.md").exists()
        assert (workspace_root / ".opencode" / "commands" / "ai-test-cmd.md").exists()
        assert (workspace_root / "opencode.json").exists()
        # Claude
        assert (workspace_root / ".claude" / "settings.json").exists()
        assert (workspace_root / ".claude" / "agents" / "ai-test-agent.md").exists()
        assert (workspace_root / ".claude" / "commands" / "ai-test-cmd.md").exists()
        assert (workspace_root / "CLAUDE.md").exists()
        # Codex
        assert (workspace_root / ".codex" / "config.toml").exists()
        # Manifest
        assert (workspace_root / ".execraft" / "generated-manifest.json").exists()

    def test_opencode_allows_only_read_only_external_task_dossier(
        self, project_dir: Path, workspace_root: Path
    ) -> None:
        dossier = project_dir / "projects" / "sample" / "tasks" / "task-one"
        dossier.mkdir(parents=True)

        render_workspace(
            project_dir / "projects" / "sample",
            workspace_root,
            task_id="task-one",
            dossier_dir=dossier,
        )

        permission = json.loads(
            (workspace_root / "opencode.json").read_text()
        )["permission"]
        external_pattern = f"{dossier.resolve().as_posix()}/**"
        assert permission["external_directory"] == {
            "*": "deny",
            dossier.resolve().as_posix(): "allow",
            external_pattern: "allow",
        }
        assert permission["edit"][external_pattern] == "deny"
        assert permission["write"][external_pattern] == "deny"

    def test_clean_render(self, project_dir: Path, workspace_root: Path) -> None:
        # Pre-create stale file
        stale = workspace_root / ".opencode" / "stale-file.txt"
        stale.parent.mkdir(parents=True)
        stale.write_text("old")

        render_workspace(
            project_dir / "projects" / "sample",
            workspace_root,
            clean=True,
            force_clean=True,
        )
        assert not stale.exists()

    def test_idempotent_render(self, project_dir: Path, workspace_root: Path) -> None:
        r1 = render_workspace(project_dir / "projects" / "sample", workspace_root)
        r2 = render_workspace(project_dir / "projects" / "sample", workspace_root)
        hashes1 = {f.relative_path: f.sha256 for f in r1.rendered_files}
        hashes2 = {f.relative_path: f.sha256 for f in r2.rendered_files}
        assert hashes1 == hashes2

    def test_manifest_lists_all_files_except_itself(self, project_dir: Path, workspace_root: Path) -> None:
        result = render_workspace(project_dir / "projects" / "sample", workspace_root)
        manifest_path = workspace_root / ".execraft" / "generated-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        rendered_paths = {f.relative_path for f in result.rendered_files}
        manifest_paths = set(manifest["files"].keys())
        # The manifest cannot include itself (chicken-and-egg)
        assert rendered_paths == manifest_paths | {".execraft/generated-manifest.json"}

    def test_all_hashes_are_valid_sha256(self, project_dir: Path, workspace_root: Path) -> None:
        result = render_workspace(project_dir / "projects" / "sample", workspace_root)
        for f in result.rendered_files:
            assert len(f.sha256) == 64, f"Invalid SHA256 for {f.relative_path}"
            assert all(c in "0123456789abcdef" for c in f.sha256)

    def test_render_result_to_manifest(self, project_dir: Path, workspace_root: Path) -> None:
        result = render_workspace(project_dir / "projects" / "sample", workspace_root)
        manifest = result.to_manifest()
        assert manifest["schema_version"] == 1
        assert manifest["workspace_root"] == "."
        assert len(manifest["files"]) == len(result.rendered_files)

    def test_skills_count_matches_source(self, project_dir: Path, workspace_root: Path) -> None:
        """Verify the number of rendered skills matches the source directory."""
        project_skills = project_dir / "projects" / "sample" / "skills"
        builtin_skills = Path(__file__).resolve().parents[1] / "src" / "execraft" / "assets" / "skills"
        names = {d.name for d in builtin_skills.iterdir() if (d / "SKILL.md").is_file()}
        names.update(d.name for d in project_skills.iterdir() if d.is_dir() and (d / "SKILL.md").is_file())
        result = render_workspace(project_dir / "projects" / "sample", workspace_root)
        skill_files = [f for f in result.rendered_files if ".agents/skills/" in f.relative_path]
        assert len(skill_files) == len(names)


# ---------------------------------------------------------------------------
# RenderResult
# ---------------------------------------------------------------------------


class TestRenderResult:
    def test_to_manifest_structure(self) -> None:
        result = RenderResult(workspace_root=Path("/tmp/test"))
        manifest = result.to_manifest()
        assert manifest["schema_version"] == 1
        assert manifest["workspace_root"] == "."
        assert manifest["files"] == {}


def test_sample_default_provider_policy_is_not_full_host(tmp_path: Path) -> None:
    repository_root = Path(__file__).resolve().parents[1]
    workspace = tmp_path / "workspace"
    render_workspace(repository_root / "projects" / "sample", workspace)
    codex = (workspace / ".codex/config.toml").read_text()
    claude = (workspace / ".claude/settings.json").read_text()
    agent_text = "\n".join(
        path.read_text() for path in (workspace / ".claude/agents").glob("*.md")
    )
    assert "danger-full-access" not in codex
    assert "bypassPermissions" not in claude
    assert "bypassPermissions" not in agent_text


def test_task_workspace_render_includes_vscode_and_relative_paths(
    project_dir: Path, workspace_root: Path, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    dossier = tmp_path / "dossier"
    dossier.mkdir()
    result = render_workspace(
        project_dir / "projects" / "sample",
        workspace_root,
        task_id="demo-task",
        repositories=[{"id": "repo", "worktree_path": str(repo)}],
        dossier_dir=dossier,
    )

    assert (workspace_root / "AGENTS.md").is_file()
    assert (workspace_root / ".vscode" / "settings.json").is_file()
    assert (workspace_root / ".vscode" / "tasks.json").is_file()
    assert (workspace_root / ".vscode" / "extensions.json").is_file()
    workspace = json.loads((workspace_root / "demo-task.code-workspace").read_text())
    assert {folder["name"] for folder in workspace["folders"]} >= {
        "Workspace",
        "Task dossier",
        "repo",
    }
    settings = json.loads((workspace_root / ".vscode" / "settings.json").read_text())
    workspace_profile = settings["terminal.integrated.profiles.linux"]["Workspace"]
    repo_profile = settings["terminal.integrated.profiles.linux"]["repo"]
    assert str(workspace_root.resolve()) in workspace_profile["args"][1]
    assert str(repo.resolve()) in repo_profile["args"][1]
    tasks = json.loads((workspace_root / ".vscode" / "tasks.json").read_text())["tasks"]
    assert all(task["options"]["cwd"] == str(workspace_root.resolve()) for task in tasks)
    manifest_text = (workspace_root / ".execraft" / "generated-manifest.json").read_text()
    assert str(tmp_path) not in manifest_text
    assert any(item.relative_path == "demo-task.code-workspace" for item in result.rendered_files)
