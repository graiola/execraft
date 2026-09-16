"""CLI acceptance tests for WP3 one-command onboarding."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import yaml
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
CLI = [sys.executable, "-m", "execraft.cli"]


def _environment(tmp_path: Path) -> dict[str, str]:
    return {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "EXECRAFT_CONTROL_ROOT": str(tmp_path / "control"),
        "AI_WORKSPACE_HOME": str(tmp_path / "workspaces"),
        "PYTHONPATH": str(REPOSITORY / "src"),
    }


def _run(
    arguments: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*CLI, *arguments],
        cwd=cwd,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def _initialize_repository(path: Path) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
    )
    (path / "README.md").write_text("# app\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=path, check=True)


def test_start_cli_previews_requires_confirmation_and_applies(tmp_path: Path) -> None:
    source = tmp_path / "sources" / "sample-app"
    _initialize_repository(source)
    environment = _environment(tmp_path)
    control_root = Path(environment["EXECRAFT_CONTROL_ROOT"])

    preview = _run(
        ["start", "Add health endpoint", "--planner", "local", "--dry-run", "--json"],
        cwd=source,
        environment=environment,
    )
    assert preview.returncode == 0, preview.stderr
    payload = json.loads(preview.stdout)
    assert payload["applied"] is False
    assert payload["project"] == "sample-app"
    assert payload["task_id"] == "add-health-endpoint"
    assert payload["plan"]["work_packages"] == 1
    assert not control_root.exists()

    refused = _run(
        ["start", "Add health endpoint", "--planner", "local"],
        cwd=source,
        environment=environment,
    )
    assert refused.returncode == 2
    assert "non-interactive creation requires --yes" in refused.stderr
    assert not control_root.exists()

    applied = _run(
        ["start", "Add health endpoint", "--planner", "local", "--yes", "--json"],
        cwd=source,
        environment=environment,
    )
    assert applied.returncode == 0, applied.stderr
    result = json.loads(applied.stdout)
    assert result["ready"] is True
    assert result["workspace_root"] == str(
        (tmp_path / "workspaces" / "sample-app" / "add-health-endpoint").resolve()
    )
    dossier = control_root / "projects" / "sample-app" / "tasks" / "add-health-endpoint"
    assert (dossier / "BRIEF.md").is_file()
    assert (dossier / "PLAN.graph.yaml").is_file()
    journal = (
        tmp_path
        / "state"
        / "execraft"
        / "starts"
        / "sample-app"
        / "add-health-endpoint.yaml"
    )
    assert journal.is_file()

    repeated = _run(
        ["start", "Add health endpoint", "--planner", "local", "--yes", "--json"],
        cwd=source,
        environment=environment,
    )
    assert repeated.returncode == 0, repeated.stderr
    statuses = [item["status"] for item in json.loads(repeated.stdout)["steps"]]
    assert statuses == ["reused", "reused", "reused", "reused"]


def test_new_cli_lists_templates_previews_and_starts_first_task(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    parent = tmp_path / "sources"
    parent.mkdir()

    templates = _run(
        ["new", "--list-templates", "--json"],
        cwd=parent,
        environment=environment,
    )
    assert templates.returncode == 0, templates.stderr
    references = {item["reference"] for item in json.loads(templates.stdout)}
    assert {"minimal@1", "python-library@1", "python-service@1"} <= references

    preview = _run(
        [
            "new",
            "telemetry-service",
            "--template",
            "python-service",
            "--start",
            "Add readiness endpoint",
            "--planner",
            "local",
            "--dry-run",
            "--json",
        ],
        cwd=parent,
        environment=environment,
    )
    assert preview.returncode == 0, preview.stderr
    preview_payload = json.loads(preview.stdout)
    assert preview_payload["applied"] is False
    assert preview_payload["first_task"]["description"] == "Add readiness endpoint"
    assert not (parent / "telemetry-service").exists()

    created = _run(
        [
            "new",
            "telemetry-service",
            "--template",
            "python-service",
            "--start",
            "Add readiness endpoint",
            "--planner",
            "local",
            "--yes",
            "--json",
        ],
        cwd=parent,
        environment=environment,
    )
    assert created.returncode == 0, created.stderr
    payload = json.loads(created.stdout)
    source = parent / "telemetry-service"
    assert payload["applied"] is True
    assert payload["first_task"]["ready"] is True
    assert (source / ".git").is_dir()
    assert (source / "pyproject.toml").is_file()
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    assert len(head) == 40
    dossier = (
        Path(environment["EXECRAFT_CONTROL_ROOT"])
        / "projects"
        / "telemetry-service"
        / "tasks"
        / "add-readiness-endpoint"
    )
    assert (dossier / "PLAN.graph.yaml").is_file()


def test_start_cli_surfaces_and_gates_discovery_decisions(tmp_path: Path) -> None:
    source = tmp_path / "sources" / "feature-app"
    _initialize_repository(source)
    subprocess.run(
        ["git", "checkout", "-qb", "feature/bootstrap"],
        cwd=source,
        check=True,
    )
    subprocess.run(["git", "branch", "-D", "main"], cwd=source, check=True)
    environment = _environment(tmp_path)
    control_root = Path(environment["EXECRAFT_CONTROL_ROOT"])

    preview = _run(
        [
            "start",
            "Add feature behavior",
            "--planner",
            "local",
            "--no-workspace",
            "--dry-run",
            "--json",
        ],
        cwd=source,
        environment=environment,
    )
    assert preview.returncode == 0, preview.stderr
    payload = json.loads(preview.stdout)
    assert payload["can_apply"] is False
    assert payload["steps"][0]["status"] == "blocked"
    assert payload["project_plan"]["pending_decisions"]
    assert not control_root.exists()

    blocked = _run(
        [
            "start",
            "Add feature behavior",
            "--planner",
            "local",
            "--no-workspace",
            "--yes",
        ],
        cwd=source,
        environment=environment,
    )
    assert blocked.returncode == 2
    assert "start preview contains blocking findings" in blocked.stderr
    assert not control_root.exists()

    accepted = _run(
        [
            "start",
            "Add feature behavior",
            "--planner",
            "local",
            "--no-workspace",
            "--accept-decisions",
            "--yes",
            "--json",
        ],
        cwd=source,
        environment=environment,
    )
    assert accepted.returncode == 0, accepted.stderr
    assert json.loads(accepted.stdout)["ready"] is True


def test_start_cli_imports_plan_without_description_and_preserves_markdown(tmp_path: Path) -> None:
    source = tmp_path / "sources" / "import-app"
    _initialize_repository(source)
    environment = _environment(tmp_path)
    control_root = Path(environment["EXECRAFT_CONTROL_ROOT"])
    imported_plan = tmp_path / "PLAN.md"
    plan_text = "# Plan: Imported CLI plan\r\n\r\n## Scope\r\nKeep this plan unchanged."
    imported_plan.write_bytes(plan_text.encode("utf-8"))

    preview = _run(
        [
            "start",
            "--plan-file",
            str(imported_plan),
            "--id",
            "imported-cli",
            "--planner",
            "local",
            "--no-workspace",
            "--dry-run",
            "--json",
        ],
        cwd=source,
        environment=environment,
    )
    assert preview.returncode == 0, preview.stderr
    assert json.loads(preview.stdout)["task_id"] == "imported-cli"

    applied = _run(
        [
            "start",
            "--plan-file",
            str(imported_plan),
            "--id",
            "imported-cli",
            "--planner",
            "local",
            "--no-workspace",
            "--yes",
            "--json",
        ],
        cwd=source,
        environment=environment,
    )
    assert applied.returncode == 0, applied.stderr
    dossier = control_root / "projects" / "import-app" / "tasks" / "imported-cli"
    assert (dossier / "PLAN.md").read_text(encoding="utf-8") == plan_text.replace("\r\n", "\n")
    assert (dossier / "PLAN.graph.yaml").is_file()
    definition = yaml.safe_load((dossier / "DEFINITION.yaml").read_text(encoding="utf-8"))
    assert definition["sources"]["PLAN.md"]["origin"] == "imported"
