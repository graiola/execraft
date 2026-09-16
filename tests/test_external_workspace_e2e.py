"""M08A end-to-end cutover fixture using only local disposable repositories."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml

from execraft.cli import main


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _repository(path: Path, marker: str) -> None:
    path.mkdir(parents=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "execraft-test@example.invalid")
    _git(path, "config", "user.name", "execraft test")
    (path / "verify.py").write_text(
        f"from pathlib import Path\nassert Path('{marker}.txt').read_text() == '{marker}'\n",
        encoding="utf-8",
    )
    (path / f"{marker}.txt").write_text(marker, encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "fixture")


def _control_plane(root: Path, source: Path) -> None:
    root.mkdir()
    _git(root, "init", "-b", "main")
    project = root / "projects" / "fixture"
    for directory in (
        "skills/test",
        "opencode/agents",
        "opencode/commands",
        "claude/agents",
        "claude/commands",
        "codex",
        "instructions",
        "vscode",
        "policies",
        "task_templates",
        "tasks/demo",
    ):
        (project / directory).mkdir(parents=True, exist_ok=True)
    (project / "skills/test/SKILL.md").write_text(
        "---\nname: test\ndescription: fixture\n---\nTest.\n", encoding="utf-8"
    )
    (project / "claude/settings.json").write_text("{}\n", encoding="utf-8")
    (project / "claude/CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")
    (project / "codex/config.toml").write_text(
        'sandbox_mode = "workspace-write"\n', encoding="utf-8"
    )
    (project / "instructions/AGENTS.md").write_text(
        "# Fixture instructions\n", encoding="utf-8"
    )
    (project / "vscode/README.md").write_text("fixture\n", encoding="utf-8")
    (project / "policies/workspace-write.yaml").write_text(
        "schema_version: 1\nid: workspace-write\n", encoding="utf-8"
    )
    for name in ("BRIEF.md", "PLAN.md", "HANDOFF.md", "REVIEW.md"):
        (project / "task_templates" / name).write_text(f"# {name}\n", encoding="utf-8")
        (project / "tasks" / "demo" / name).write_text(f"# Demo {name}\n", encoding="utf-8")

    descriptor = {
        "schema_version": 1,
        "project": "fixture",
        "description": "unrelated local fixture",
        "repositories": [
            {
                "id": "product_a",
                "path": "product-a",
                "role": "deployment",
                "base_branch": "main",
                "workspace_name": "deployment",
            },
            {
                "id": "product_b",
                "path": "product-b",
                "role": "component",
                "base_branch": "main",
                "workspace_name": "component",
            },
            {
                "id": "runtime",
                "path": "runtime",
                "role": "runtime",
                "base_branch": "main",
                "workspace_name": "runtime",
                "mutability": "runtime_only",
            },
        ],
        "task_templates": "projects/fixture/task_templates",
        "instructions_dir": "projects/fixture/instructions",
        "skills_dir": "projects/fixture/skills",
        "opencode_dir": "projects/fixture/opencode",
        "claude_dir": "projects/fixture/claude",
        "codex_dir": "projects/fixture/codex",
        "vscode_dir": "projects/fixture/vscode",
        "policies_dir": "projects/fixture/policies",
        "default_policy": "workspace-write",
        "policy_profiles": {"workspace-write": {"description": "fixture"}},
        "editor": {"extensions": ["ms-python.python"]},
    }
    (project / "project.yaml").write_text(
        yaml.safe_dump(descriptor, sort_keys=False), encoding="utf-8"
    )
    task = {
        "schema_version": 2,
        "id": "demo",
        "project": "fixture",
        "title": "M08A fixture",
        "status": "in_progress",
        "created_at": "2026-01-01T00:00:00+00:00",
        "git": {"branch_name": "task/demo", "merge_strategy": "squash"},
        "repositories": [
            {
                "id": "product_a",
                "base_branch": "main",
                "task_branch": "task/demo",
                "role": "deployment",
                "required": True,
                "verify": ["python3 verify.py"],
            },
            {
                "id": "product_b",
                "base_branch": "main",
                "task_branch": "task/demo",
                "role": "component",
                "required": True,
            },
            {
                "id": "runtime",
                "base_branch": "main",
                "task_branch": "task/demo",
                "role": "runtime",
                "required": True,
                "mutability": "runtime_only",
            },
        ],
        "integration": {"verify": []},
    }
    (project / "tasks/demo/TASK.yaml").write_text(
        yaml.safe_dump(task, sort_keys=False), encoding="utf-8"
    )


def test_external_workspace_browser_and_recreation(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _repository(source / "product-a", "alpha")
    _repository(source / "product-b", "beta")
    _repository(source / "runtime", "runtime")
    runtime_head = _git(source / "runtime", "rev-parse", "HEAD")

    control = tmp_path / "Execraft"
    _control_plane(control, source)
    monkeypatch.setenv("EXECRAFT_WORKFLOW_ROOT", str(control))
    config_home = tmp_path / "config"
    monkeypatch.setenv("EXECRAFT_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("EXECRAFT_STATE_HOME", str(tmp_path / "state"))
    workspace = tmp_path / "workspace"

    # An imported task that omits the catalog's runtime-only floor must fail
    # before allocating a workspace or creating any task branches.
    task_path = control / "projects/fixture/tasks/demo/TASK.yaml"
    task_data = yaml.safe_load(task_path.read_text(encoding="utf-8"))
    runtime_task = next(
        item for item in task_data["repositories"] if item["id"] == "runtime"
    )
    runtime_task.pop("mutability")
    task_path.write_text(yaml.safe_dump(task_data, sort_keys=False), encoding="utf-8")
    assert main(
        [
            "workspace",
            "start",
            "demo",
            "--source-root",
            str(source),
            "--workspace-root",
            str(workspace),
        ]
    ) == 2
    assert not workspace.exists()
    assert _git(source / "product-a", "branch", "--list", "task/demo") == ""
    runtime_task["mutability"] = "runtime_only"
    task_path.write_text(yaml.safe_dump(task_data, sort_keys=False), encoding="utf-8")
    invalid_policy_workspace = tmp_path / "invalid-policy-workspace"
    assert main(
        [
            "workspace",
            "start",
            "demo",
            "--source-root",
            str(source),
            "--workspace-root",
            str(invalid_policy_workspace),
            "--policy",
            "removed-policy",
        ]
    ) == 2
    assert not invalid_policy_workspace.exists()

    assert main(["project", "validate", "fixture"]) == 0
    assert main(
        [
            "workspace",
            "start",
            "demo",
            "--source-root",
            str(source),
            "--workspace-root",
            str(source / "product-a"),
        ]
    ) == 2

    failed_workspace = tmp_path / "failed-workspace"
    with monkeypatch.context() as failure_patch:
        failure_patch.setattr(
            "execraft.cli._render_record",
            lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("render failed")),
        )
        assert main(
            [
                "workspace",
                "start",
                "demo",
                "--source-root",
                str(source),
                "--workspace-root",
                str(failed_workspace),
            ]
        ) == 2
    assert not failed_workspace.exists()
    assert _git(source / "product-a", "branch", "--list", "task/demo") == ""
    assert _git(source / "product-b", "branch", "--list", "task/demo") == ""

    assert main(
        [
            "workspace",
            "start",
            "demo",
            "--source-root",
            str(source),
            "--workspace-root",
            str(workspace),
        ]
    ) == 0
    assert main(
        [
            "workspace",
            "run",
            "demo",
            "--repository",
            "runtime",
            "--",
            "python3",
            "verify.py",
        ]
    ) == 2
    binding = yaml.safe_load((config_home / "projects/fixture.yaml").read_text(encoding="utf-8"))
    assert binding["source_root"] == str(source.resolve())
    assert (workspace / ".execraft/runtime.env").is_file()
    assert (workspace / ".execraft/repositories.json").is_file()
    assert (workspace / "demo.code-workspace").is_file()
    assert (workspace / ".vscode/tasks.json").is_file()
    assert (workspace / "deployment/.git").is_file()
    assert (workspace / "component/.git").is_file()
    assert not (workspace / "runtime").exists()

    assert main(
        [
            "workspace",
            "run",
            "demo",
            "--repository",
            "product_a",
            "--",
            "python3",
            "verify.py",
        ]
    ) == 0
    assert main(
        ["browser", "prepare", "--task-id", "demo", "--workspace-root", str(workspace)]
    ) == 0
    state_files = list((workspace / ".execraft/runs/fake-state").glob("*.json"))
    assert len(state_files) == 1
    run_id = state_files[0].stem
    assert main(
        ["browser", "execute", "--run-id", run_id, "--workspace-root", str(workspace)]
    ) == 0
    assert main(
        ["browser", "apply", "--run-id", run_id, "--workspace-root", str(workspace)]
    ) == 0
    assert (workspace / "deployment/test_file.md").is_file()
    assert "Browser run" in (
        control / "projects/fixture/tasks/demo/HANDOFF.md"
    ).read_text()
    assert _git(source / "runtime", "rev-parse", "HEAD") == runtime_head
    assert _git(source / "runtime", "status", "--porcelain") == ""

    registry_path = control / ".git/ai-workspaces/demo.yaml"
    registry = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    product_a_record = next(
        item for item in registry["repositories"] if item["id"] == "product_a"
    )
    registry["policy_profile"] = "removed-policy"
    registry_path.write_text(yaml.safe_dump(registry, sort_keys=False), encoding="utf-8")
    assert main(
        ["workspace", "run", "demo", "--repository", "product_a", "--", "true"]
    ) == 2
    registry["policy_profile"] = "workspace-write"
    original_source = product_a_record["source_path"]
    original_worktree = product_a_record["worktree_path"]
    product_a_record["source_path"] = str(source / "product-b")
    registry_path.write_text(yaml.safe_dump(registry, sort_keys=False), encoding="utf-8")
    assert main(
        ["workspace", "run", "demo", "--repository", "product_a", "--", "true"]
    ) == 2
    product_a_record["source_path"] = original_source
    product_a_record["worktree_path"] = str(source / "product-b")
    registry_path.write_text(yaml.safe_dump(registry, sort_keys=False), encoding="utf-8")
    assert main(
        ["workspace", "run", "demo", "--repository", "product_a", "--", "true"]
    ) == 2
    product_a_record["worktree_path"] = original_worktree
    registry_path.write_text(yaml.safe_dump(registry, sort_keys=False), encoding="utf-8")

    duplicate = dict(product_a_record)
    duplicate["worktree_path"] = str(source / "product-b")
    registry["repositories"].insert(0, duplicate)
    registry_path.write_text(yaml.safe_dump(registry, sort_keys=False), encoding="utf-8")
    assert main(
        ["workspace", "run", "demo", "--repository", "product_a", "--", "true"]
    ) == 2
    registry["repositories"].pop(0)

    product_a_record["branch"] = "other"
    registry_path.write_text(yaml.safe_dump(registry, sort_keys=False), encoding="utf-8")
    assert main(
        ["workspace", "run", "demo", "--repository", "product_a", "--", "true"]
    ) == 2
    product_a_record["branch"] = "task/demo"
    registry_path.write_text(yaml.safe_dump(registry, sort_keys=False), encoding="utf-8")

    product_a_worktree = Path(original_worktree)
    _git(product_a_worktree, "switch", "--detach")
    assert main(
        ["workspace", "run", "demo", "--repository", "product_a", "--", "true"]
    ) == 2
    assert main(
        ["browser", "prepare", "--task-id", "demo", "--workspace-root", str(workspace)]
    ) == 2
    _git(product_a_worktree, "switch", "task/demo")

    # A tightened catalog invalidates stale writable workspace metadata. The
    # unsafe action is blocked, but destroy remains available for recovery.
    descriptor_path = control / "projects/fixture/project.yaml"
    descriptor = yaml.safe_load(descriptor_path.read_text(encoding="utf-8"))
    product_a = next(
        item for item in descriptor["repositories"] if item["id"] == "product_a"
    )
    product_a["mutability"] = "runtime_only"
    descriptor_path.write_text(
        yaml.safe_dump(descriptor, sort_keys=False), encoding="utf-8"
    )
    assert main(
        [
            "workspace",
            "run",
            "demo",
            "--repository",
            "product_a",
            "--",
            "python3",
            "verify.py",
        ]
    ) == 2
    product_a.pop("mutability")
    descriptor_path.write_text(
        yaml.safe_dump(descriptor, sort_keys=False), encoding="utf-8"
    )

    # Runtime stop is reversible and must retain every Git worktree.
    assert main(["workspace", "stop", "demo"]) == 0
    assert (workspace / "deployment").is_dir()
    assert (workspace / "component").is_dir()

    # Candidate changes must be removed before destruction. Normal retirement
    # still refuses this deliberately incomplete, unarchived fixture.
    (workspace / "deployment/test_file.md").unlink()
    assert main(["workspace", "destroy", "demo"]) == 2
    assert (workspace / "deployment").is_dir()
    assert (workspace / "component").is_dir()

    # Explicit disaster recovery is the only bypass for ordinary policy checks.
    assert main(["workspace", "destroy", "demo", "--force"]) == 0
    assert not (workspace / "deployment").exists()
    assert main(
        [
            "workspace",
            "start",
            "demo",
            "--workspace-root",
            str(workspace),
        ]
    ) == 0
    assert (workspace / "deployment").is_dir()

    for repository in (source / "product-a", source / "product-b", source / "runtime"):
        tracked = _git(repository, "ls-files").splitlines()
        assert not any(
            path == "AGENTS.md"
            or path.startswith((".execraft/", ".agents/", ".claude/", ".codex/", ".opencode/"))
            for path in tracked
        )

    # Local manifests are portable and contain no temporary absolute paths.
    generated = (workspace / ".execraft/generated-manifest.json").read_text()
    assert str(tmp_path) not in generated
    assert json.loads(generated)["workspace_root"] == "."
