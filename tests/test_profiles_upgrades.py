"""WP5 profile, feature, provenance, and upgrade acceptance tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from execraft import bootstrap
from execraft.cli import main
from execraft.control_plane import ControlPlaneHome
from execraft.onboarding.profiles import (
    ProfileCatalogError,
    ProjectProfileRenderer,
    ProjectTemplateContext,
    default_profile_catalog,
)
from execraft.onboarding.upgrades import ProjectUpgradeError, ProjectUpgradeService
from execraft.project import load_project
from execraft.render import render_workspace


def _init_repo(path: Path, *, python: bool = True) -> Path:
    path.mkdir(parents=True)
    if python:
        (path / "app.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "init",
        ],
        cwd=path,
        check=True,
    )
    return path


def _xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ControlPlaneHome:
    monkeypatch.setenv("EXECRAFT_CONTROL_ROOT", str(tmp_path / "control"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    return ControlPlaneHome.resolve().ensure_layout()


def test_catalog_exposes_versioned_profiles_and_composable_features() -> None:
    catalog = default_profile_catalog()
    assert catalog.profile("standard").reference == "standard@3"
    assert catalog.profile("standard@2").parallelism is False
    assert catalog.profile("autonomous").automatic_commits is True
    assert all(profile.automatic_commits for profile in catalog.profiles())
    assert catalog.profile("standard@1").new_project_selectable is False
    assert "standard@1" not in {profile.reference for profile in catalog.new_project_profiles()}
    assert "standard@3" in {profile.reference for profile in catalog.new_project_profiles()}
    assert catalog.feature("python").reference == "python@1"
    assert catalog.feature("devcontainer").reference == "devcontainer@1"


def test_profile_renderer_generates_provenance_agents_and_devcontainer(tmp_path: Path) -> None:
    source = _init_repo(tmp_path / "source" / "sample")
    report = bootstrap.discover(source)
    destination = tmp_path / "project"
    destination.mkdir()
    renderer = ProjectProfileRenderer(default_profile_catalog())
    renderer.render(
        ProjectTemplateContext(
            report=report,
            profile_reference="starter@1",
            include_devcontainer=True,
        ),
        destination,
    )

    project = load_project(destination)
    assert project.profile == "starter@1"
    assert "python@1" in project.features
    assert "devcontainer@1" in project.features
    assert project.configured_path("devcontainer_dir") == destination / "devcontainer"

    agents = yaml.safe_load((destination / "agents.yaml").read_text(encoding="utf-8"))
    assert agents["supervisor"]["enabled"] is False
    assert agents["commit"]["mode"] == "automatic"
    assert agents["scheduling"]["parallel_shards"]["max_workers"] == 2
    assert "max_parallel" not in agents["scheduling"]["parallel_shards"]
    assert agents["schema_version"] == 4
    assert "providers" not in agents
    assert "model_route" not in agents["agents"]["codex"]
    assert all(
        profile.get("policy", {}).get("dangerously_skip_permissions", False) is False
        for profile in agents["agents"].values()
    )
    instructions = (destination / "instructions" / "AGENTS.md").read_text(encoding="utf-8")
    assert "provider-neutral" in instructions
    provenance = yaml.safe_load(
        (destination / ".execraft-template.yaml").read_text(encoding="utf-8")
    )
    assert provenance["profile"] == "starter@1"
    assert "project.yaml" in provenance["managed_files"]
    assert (destination / "devcontainer" / "devcontainer.json").is_file()


def test_profile_upgrade_is_dry_run_safe_and_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _xdg(monkeypatch, tmp_path)
    source = _init_repo(tmp_path / "source" / "app")
    onboarding = bootstrap.create_onboarding_service()
    created = onboarding.create_project(
        report=onboarding.inspect_project(source),
        output_dir=home.projects_dir,
        template_id="standard@2",
        register=True,
    )
    project = load_project(created.path.parent)
    service = ProjectUpgradeService()

    plan = service.plan(project)
    assert plan.current_profile == "standard@2"
    assert plan.target_profile == "standard@3"
    assert plan.can_apply
    assert any(item.path == "agents.yaml" and item.action == "update" for item in plan.files)
    before = (project.directory / "project.yaml").read_bytes()
    service.cleanup(plan)
    assert (project.directory / "project.yaml").read_bytes() == before

    applied = service.plan(project)
    service.apply(applied)
    upgraded = load_project(project.directory)
    assert upgraded.profile == "standard@3"
    second = service.plan(upgraded)
    try:
        assert second.up_to_date
        assert not second.changes
    finally:
        service.cleanup(second)


def test_upgrade_detects_local_managed_file_conflict_and_force_is_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _xdg(monkeypatch, tmp_path)
    source = _init_repo(tmp_path / "source" / "app")
    onboarding = bootstrap.create_onboarding_service()
    created = onboarding.create_project(
        report=onboarding.inspect_project(source),
        output_dir=home.projects_dir,
        template_id="standard@2",
        register=True,
    )
    project = load_project(created.path.parent)
    agents_path = project.directory / "agents.yaml"
    agents_path.write_text(agents_path.read_text(encoding="utf-8") + "# local edit\n", encoding="utf-8")

    service = ProjectUpgradeService()
    plan = service.plan(project)
    assert any(item.path == "agents.yaml" and item.action == "conflict" for item in plan.files)
    with pytest.raises(ProjectUpgradeError, match="locally modified"):
        service.apply(plan)
    service.apply(plan, force_conflicts=True)
    assert "# local edit" not in agents_path.read_text(encoding="utf-8")
    assert load_project(project.directory).profile == "standard@3"


def test_legacy_project_can_be_adopted_without_touching_task_dossiers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _xdg(monkeypatch, tmp_path)
    source = _init_repo(tmp_path / "source" / "legacy")
    report = bootstrap.discover(source)
    project_file = bootstrap.scaffold_project(report, home.projects_dir)
    task_file = project_file.parent / "tasks" / "keep" / "NOTES.md"
    task_file.parent.mkdir(parents=True)
    task_file.write_text("operator data\n", encoding="utf-8")
    project = load_project(project_file.parent)

    service = ProjectUpgradeService()
    adopted = service.adopt(project, profile_reference="standard@1")
    assert adopted.profile == "standard@1"
    assert all(not path.startswith("tasks/") for path in adopted.managed_files)
    assert task_file.read_text(encoding="utf-8") == "operator data\n"


def test_workspace_render_copies_optional_devcontainer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _xdg(monkeypatch, tmp_path)
    source = _init_repo(tmp_path / "source" / "app")
    onboarding = bootstrap.create_onboarding_service()
    created = onboarding.create_project(
        report=onboarding.inspect_project(source),
        output_dir=home.projects_dir,
        template_id="starter",
        include_devcontainer=True,
        register=True,
    )
    workspace = tmp_path / "workspace"
    result = render_workspace(created.path.parent, workspace)
    assert (workspace / ".devcontainer" / "devcontainer.json").is_file()
    assert any(item.relative_path == ".devcontainer/devcontainer.json" for item in result.rendered_files)


def test_cli_lists_profiles_features_and_applies_upgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = _xdg(monkeypatch, tmp_path)
    source = _init_repo(tmp_path / "source" / "app")
    assert main(["project", "profiles", "--json"]) == 0
    profiles = json.loads(capsys.readouterr().out)
    assert any(item["reference"] == "autonomous@1" for item in profiles)
    assert main(["project", "features", "--json"]) == 0
    features = json.loads(capsys.readouterr().out)
    assert any(item["reference"] == "python@1" for item in features)

    assert main([
        "init", "--source", str(source), "--output", str(home.projects_dir),
        "--template", "standard@2", "--json",
    ]) == 0
    capsys.readouterr()
    assert main(["project", "check-update", "app", "--json"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["target_profile"] == "standard@3"
    assert preview["changes"] > 0
    assert main(["project", "upgrade", "app", "--yes", "--json"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["applied"] is True


def test_feature_detection_uses_repository_build_facts_and_protects_profile_defaults(
    tmp_path: Path,
) -> None:
    source = _init_repo(tmp_path / "source" / "cmake-only", python=False)
    (source / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.20)\nproject(sample)\n", encoding="utf-8"
    )
    report = bootstrap.discover(source)
    catalog = default_profile_catalog()
    features = catalog.detect_features(report, profile=catalog.profile("starter"))
    assert {item.id for item in features} == {"core", "cmake"}
    with pytest.raises(ProfileCatalogError, match="profile-required"):
        catalog.detect_features(
            report, profile=catalog.profile("starter"), excluded=("core",)
        )


def test_upgrade_can_remove_optional_devcontainer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _xdg(monkeypatch, tmp_path)
    source = _init_repo(tmp_path / "source" / "app")
    onboarding = bootstrap.create_onboarding_service()
    created = onboarding.create_project(
        report=onboarding.inspect_project(source),
        output_dir=home.projects_dir,
        template_id="standard@2",
        include_devcontainer=True,
        register=True,
    )
    project = load_project(created.path.parent)
    assert (project.directory / "devcontainer" / "devcontainer.json").is_file()

    service = ProjectUpgradeService()
    plan = service.plan(project, target_profile="standard@2", include_devcontainer=False)
    try:
        assert "devcontainer@1" not in plan.target_features
        assert any(
            item.path == "devcontainer/devcontainer.json" and item.action == "delete"
            for item in plan.files
        )
        service.apply(plan)
    finally:
        service.cleanup(plan)
    assert not (project.directory / "devcontainer" / "devcontainer.json").exists()
