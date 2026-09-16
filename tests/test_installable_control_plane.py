"""End-to-end proof that the built wheel works without an Execraft checkout."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _run(command: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _init_git_repository(path: Path) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)
    (path / "pyproject.toml").write_text(
        "[project]\nname = 'sample-app'\nversion = '0.1.0'\n",
        encoding="utf-8",
    )
    (path / "README.md").write_text("# sample\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True)


def test_wheel_init_and_project_lookup_outside_checkout(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[1]
    wheelhouse = tmp_path / "wheelhouse"
    target = tmp_path / "site"
    wheelhouse.mkdir()
    target.mkdir()

    build = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(wheelhouse),
            str(repository),
        ],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert build.returncode == 0, build.stderr
    wheel = next(wheelhouse.glob("execraft-*.whl"))
    install = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--target",
            str(target),
            str(wheel),
        ],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert install.returncode == 0, install.stderr

    source = tmp_path / "work" / "sample-app"
    _init_git_repository(source)
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "PYTHONPATH": str(target),
    }
    for name in ("EXECRAFT_CONTROL_ROOT", "EXECRAFT_WORKFLOW_ROOT", "AI_WORKFLOW_ROOT"):
        env.pop(name, None)

    control_root = Path(env["XDG_DATA_HOME"]) / "execraft" / "control"
    inspection = _run(
        [sys.executable, "-m", "execraft.cli", "project", "inspect", "--json"],
        cwd=source,
        env=env,
    )
    assert inspection.returncode == 0, inspection.stderr
    inspected = json.loads(inspection.stdout)
    assert inspected["project_id"] == "sample-app"
    assert inspected["evidence"]
    assert not control_root.exists()

    init_preview = _run(
        [sys.executable, "-m", "execraft.cli", "init", "--dry-run", "--json"],
        cwd=source,
        env=env,
    )
    assert init_preview.returncode == 0, init_preview.stderr
    preview = json.loads(init_preview.stdout)
    assert preview["creation"]["applied"] is False
    assert preview["creation"]["plan"]["files"]
    assert not (control_root / "projects" / "sample-app").exists()

    init = _run([sys.executable, "-m", "execraft.cli", "init"], cwd=source, env=env)
    assert init.returncode == 0, init.stderr
    assert "Generated project:" in init.stdout
    assert "Control-plane home:" in init.stdout

    listing = _run(
        [sys.executable, "-m", "execraft.cli", "project", "list", "--json"],
        cwd=source,
        env=env,
    )
    assert listing.returncode == 0, listing.stderr
    rows = json.loads(listing.stdout)
    assert rows[0]["id"] == "sample-app"
    assert rows[0]["origin"] == "registry"
    assert rows[0]["source_root"] == str(source.resolve())

    validation = _run(
        [sys.executable, "-m", "execraft.cli", "project", "validate"],
        cwd=source,
        env=env,
    )
    assert validation.returncode == 0, validation.stderr
    assert "Project sample-app is valid" in validation.stdout

    task_preview = _run(
        [
            sys.executable,
            "-m",
            "execraft.cli",
            "task",
            "new",
            "first-task",
            "--title",
            "First task",
            "--brief",
            "Exercise the installed onboarding domain.",
            "--dry-run",
            "--json",
        ],
        cwd=source,
        env=env,
    )
    assert task_preview.returncode == 0, task_preview.stderr
    task_plan = json.loads(task_preview.stdout)
    assert task_plan["applied"] is False
    descriptor = Path(rows[0]["descriptor"])
    assert not (descriptor.parent / "tasks" / "first-task").exists()

    task = _run(
        [
            sys.executable,
            "-m",
            "execraft.cli",
            "task",
            "new",
            "first-task",
            "--title",
            "First task",
            "--brief",
            "Exercise the installed onboarding domain.",
        ],
        cwd=source,
        env=env,
    )
    assert task.returncode == 0, task.stderr
    assert (descriptor.parent / "tasks" / "first-task" / "TASK.yaml").is_file()
    assert "Exercise the installed onboarding domain." in (
        descriptor.parent / "tasks" / "first-task" / "BRIEF.md"
    ).read_text(encoding="utf-8")

    status = _run(
        [sys.executable, "-m", "execraft.cli", "task", "status", "first-task"],
        cwd=source,
        env=env,
    )
    assert status.returncode == 0, status.stderr
    assert "Control-plane Git: not applicable (installed control home)" in status.stdout

    workspace_root = tmp_path / "workspaces" / "sample-app" / "first-task"
    start = _run(
        [
            sys.executable,
            "-m",
            "execraft.cli",
            "workspace",
            "start",
            "first-task",
            "--workspace-root",
            str(workspace_root),
        ],
        cwd=source,
        env=env,
    )
    assert start.returncode == 0, start.stderr
    assert workspace_root.is_dir()
    assert (control_root / ".registry" / "workspaces" / "first-task.yaml").is_file()

    workspace_status = _run(
        [sys.executable, "-m", "execraft.cli", "workspace", "status", "first-task"],
        cwd=source,
        env=env,
    )
    assert workspace_status.returncode == 0, workspace_status.stderr
    assert f"Workspace: {workspace_root}" in workspace_status.stdout

    destroy = _run(
        [
            sys.executable,
            "-m",
            "execraft.cli",
            "workspace",
            "destroy",
            "first-task",
            "--remove-shell",
            "--force",
        ],
        cwd=source,
        env=env,
    )
    assert destroy.returncode == 0, destroy.stderr
    assert not workspace_root.exists()

    start_preview = _run(
        [
            sys.executable,
            "-m",
            "execraft.cli",
            "start",
            "Add installed health endpoint",
            "--planner",
            "local",
            "--dry-run",
            "--json",
        ],
        cwd=source,
        env=env,
    )
    assert start_preview.returncode == 0, start_preview.stderr
    start_preview_payload = json.loads(start_preview.stdout)
    assert start_preview_payload["task_id"] == "add-installed-health-endpoint"
    assert start_preview_payload["plan"]["work_packages"] == 1

    one_command = _run(
        [
            sys.executable,
            "-m",
            "execraft.cli",
            "start",
            "Add installed health endpoint",
            "--planner",
            "local",
            "--yes",
            "--json",
        ],
        cwd=source,
        env=env,
    )
    assert one_command.returncode == 0, one_command.stderr
    one_command_payload = json.loads(one_command.stdout)
    assert one_command_payload["ready"] is True
    expected_workspace = (
        Path(env["HOME"])
        / "workspace"
        / "ai-workspaces"
        / "sample-app"
        / "add-installed-health-endpoint"
    )
    assert Path(one_command_payload["workspace_root"]) == expected_workspace.resolve()
    assert expected_workspace.is_dir()
    assert (
        descriptor.parent
        / "tasks"
        / "add-installed-health-endpoint"
        / "PLAN.graph.yaml"
    ).is_file()

    greenfield_parent = tmp_path / "greenfield"
    greenfield_parent.mkdir()
    greenfield = _run(
        [
            sys.executable,
            "-m",
            "execraft.cli",
            "new",
            "worker-service",
            "--directory",
            str(greenfield_parent),
            "--template",
            "python-service",
            "--start",
            "Add worker heartbeat",
            "--planner",
            "local",
            "--no-workspace",
            "--yes",
            "--json",
        ],
        cwd=tmp_path,
        env=env,
    )
    assert greenfield.returncode == 0, greenfield.stderr
    greenfield_payload = json.loads(greenfield.stdout)
    assert greenfield_payload["applied"] is True
    assert greenfield_payload["first_task"]["ready"] is True
    generated_source = greenfield_parent / "worker-service"
    assert (generated_source / ".git").is_dir()
    assert (generated_source / "pyproject.toml").is_file()
