"""End-to-end readiness matrix for every built-in project/source template."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest
import yaml

from execraft import bootstrap
from execraft.control_plane import ControlPlaneHome
from execraft.onboarding.greenfield import GreenfieldService, default_greenfield_catalog
from execraft.onboarding.profiles import default_profile_catalog
from execraft.onboarding.upgrades import ProjectUpgradeService, TemplateProvenance
from execraft.project import load_project


def _xdg(monkeypatch: pytest.MonkeyPatch, root: Path) -> ControlPlaneHome:
    monkeypatch.setenv("EXECRAFT_CONTROL_ROOT", str(root / "control"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(root / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(root / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(root / "runtime"))
    return ControlPlaneHome.resolve().ensure_layout()


def _init_repo(path: Path, *, marker: str = "pyproject.toml") -> Path:
    path.mkdir(parents=True)
    if marker == "pyproject.toml":
        (path / marker).write_text(
            "[project]\nname='template-fixture'\nversion='0.1.0'\n",
            encoding="utf-8",
        )
    else:
        (path / marker).write_text("cmake_minimum_required(VERSION 3.20)\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Template Test",
            "-c",
            "user.email=template@example.invalid",
            "commit",
            "-qm",
            "initial",
        ],
        cwd=path,
        check=True,
    )
    return path


def _assert_managed_hashes(project_dir: Path) -> None:
    provenance = TemplateProvenance.load(project_dir / ".execraft-template.yaml")
    assert provenance.managed_files
    for relative, expected in provenance.managed_files.items():
        path = project_dir / relative
        assert path.is_file(), relative
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected


@pytest.mark.parametrize("profile", ["starter@1", "standard@3", "autonomous@1"])
def test_each_profile_generates_a_registered_upgrade_stable_project(
    profile: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _xdg(monkeypatch, tmp_path / profile.replace("@", "-"))
    source = _init_repo(tmp_path / "sources" / profile.replace("@", "-"))
    onboarding = bootstrap.create_onboarding_service()
    report = onboarding.inspect_project(source)
    outcome = onboarding.create_project(
        report=report,
        output_dir=home.projects_dir,
        template_id=profile,
        register=True,
    )
    project = load_project(outcome.path.parent)

    assert project.profile == profile
    descriptor = yaml.safe_load((project.directory / "project.yaml").read_text(encoding="utf-8"))
    assert descriptor["schema_version"] == 3
    assert project.features
    _assert_managed_hashes(project.directory)

    readiness = onboarding.evaluate_readiness(project, source_root=source)
    checks = {item.id: item for item in readiness.checks}
    for check_id in ("descriptor", "source", "repositories", "workspace"):
        assert checks[check_id].status.value == "ready", checks[check_id].details

    upgrade = ProjectUpgradeService()
    plan = upgrade.plan(project, target_profile=profile)
    try:
        assert plan.up_to_date
        assert not plan.changes
    finally:
        upgrade.cleanup(plan)


@pytest.mark.parametrize(
    "template_id",
    [item.reference for item in default_greenfield_catalog().templates()],
)
def test_each_greenfield_template_bootstraps_through_normal_onboarding(
    template_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _xdg(monkeypatch, tmp_path / template_id.replace("@", "-"))
    onboarding = bootstrap.create_onboarding_service()
    greenfield = GreenfieldService(onboarding=onboarding)
    outcome = greenfield.create(
        name=f"sample-{template_id.split('@', 1)[0]}",
        parent=tmp_path / "greenfield",
        descriptor_output=home.projects_dir,
        source_template=template_id,
        project_template="starter@1",
    )

    assert outcome.source_root is not None
    assert (outcome.source_root / ".git").exists()
    project = load_project(outcome.project_outcome.path.parent)
    assert project.profile == "starter@1"
    assert project.repositories[0].base_branch == "main"
    _assert_managed_hashes(project.directory)


def test_profile_and_feature_catalog_references_are_unique_and_versioned() -> None:
    catalog = default_profile_catalog()
    profiles = catalog.profiles()
    features = catalog.features()
    assert len({item.reference for item in profiles}) == len(profiles)
    assert len({item.reference for item in features}) == len(features)
    assert all("@" in item.reference for item in (*profiles, *features))
