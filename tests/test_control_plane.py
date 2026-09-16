"""Tests for the installable control-plane home and project registry."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from execraft.control_plane import ControlPlaneHome, default_control_root
from execraft.project import (
    ProjectError,
    list_project_catalog,
    load_project_binding,
    load_project_registration,
    load_registered_project,
    project_binding_path,
    register_project_descriptor,
    resolve_current_project,
    write_project_binding,
)


def _write_project(directory: Path, *, project_id: str = "sample") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "task_templates").mkdir(exist_ok=True)
    descriptor = {
        "schema_version": 2,
        "project": project_id,
        "path_base": "project_directory",
        "repositories": [
            {
                "id": "app",
                "path": ".",
                "base_branch": "main",
                "workspace_name": "app",
            }
        ],
        "task_templates": "task_templates",
    }
    path = directory / "project.yaml"
    path.write_text(yaml.safe_dump(descriptor, sort_keys=False), encoding="utf-8")
    return path


def _isolated_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    for name in ("EXECRAFT_CONTROL_ROOT", "EXECRAFT_WORKFLOW_ROOT", "AI_WORKFLOW_ROOT"):
        monkeypatch.delenv(name, raising=False)


def test_control_home_defaults_to_xdg_outside_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_xdg(monkeypatch, tmp_path)
    working = tmp_path / "source"
    working.mkdir()

    home = ControlPlaneHome.resolve(cwd=working).ensure_layout()

    assert home.origin == "xdg"
    assert home.root == default_control_root()
    assert home.projects_dir.is_dir()
    assert home.registry_dir.is_dir()
    assert home.config_dir.is_dir()
    assert home.state_dir.is_dir()


def test_new_control_root_overrides_legacy_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_xdg(monkeypatch, tmp_path)
    monkeypatch.setenv("EXECRAFT_WORKFLOW_ROOT", str(tmp_path / "legacy"))
    monkeypatch.setenv("EXECRAFT_CONTROL_ROOT", str(tmp_path / "explicit"))

    home = ControlPlaneHome.resolve(cwd=tmp_path)

    assert home.root == (tmp_path / "explicit").resolve()
    assert home.origin == "explicit_environment"
    assert home.environment_variable == "EXECRAFT_CONTROL_ROOT"


def test_schema_v1_binding_is_read_and_upgraded_without_losing_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_xdg(monkeypatch, tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    descriptor = _write_project(tmp_path / "external" / "sample")
    binding = tmp_path / "config" / "execraft" / "projects" / "sample.yaml"
    binding.parent.mkdir(parents=True)
    binding.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "project": "sample",
                "source_root": str(source.resolve()),
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    registration_path = register_project_descriptor(descriptor)
    registration = load_project_registration("sample")

    assert registration_path == binding
    assert registration is not None
    assert registration.descriptor == descriptor.resolve()
    assert registration.source_root == source.resolve()
    assert load_project_binding("sample").source_root == source.resolve()  # type: ignore[union-attr]


def test_external_descriptor_is_listed_and_loaded_from_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_xdg(monkeypatch, tmp_path)
    root = ControlPlaneHome.resolve(cwd=tmp_path).ensure_layout().root
    source = tmp_path / "source"
    source.mkdir()
    descriptor = _write_project(tmp_path / "descriptors" / "sample")

    register_project_descriptor(descriptor, source_root=source)

    catalog = list_project_catalog(root)
    assert [(item.project.id, item.origin) for item in catalog] == [
        ("sample", "registry")
    ]
    project = load_registered_project(root, "sample")
    assert project.directory == descriptor.parent.resolve()


def test_current_directory_resolves_deepest_registered_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_xdg(monkeypatch, tmp_path)
    root = ControlPlaneHome.resolve(cwd=tmp_path).ensure_layout().root
    source = tmp_path / "source"
    nested = source / "packages" / "app"
    nested.mkdir(parents=True)
    descriptor = _write_project(tmp_path / "descriptors" / "sample")
    register_project_descriptor(descriptor, source_root=source)

    project = resolve_current_project(root, cwd=nested)

    assert project.id == "sample"


def test_write_binding_preserves_registered_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_xdg(monkeypatch, tmp_path)
    descriptor = _write_project(tmp_path / "descriptors" / "sample")
    source_one = tmp_path / "source-one"
    source_two = tmp_path / "source-two"
    source_one.mkdir()
    source_two.mkdir()
    register_project_descriptor(descriptor, source_root=source_one)

    write_project_binding("sample", source_two)

    registration = load_project_registration("sample")
    assert registration is not None
    assert registration.descriptor == descriptor.resolve()
    assert registration.source_root == source_two.resolve()


def test_home_migrate_registers_legacy_descriptors_without_copying(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from execraft.cli import main

    _isolated_xdg(monkeypatch, tmp_path)
    legacy = tmp_path / "legacy"
    descriptor = _write_project(legacy / "projects" / "sample")

    result = main(["home", "migrate", "--from", str(legacy)])

    assert result == 0
    assert "Registered 1 project" in capsys.readouterr().out
    registration = load_project_registration("sample")
    assert registration is not None
    assert registration.descriptor == descriptor.resolve()
    assert not (default_control_root() / "projects" / "sample").exists()


def test_project_register_command_accepts_external_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from execraft.cli import main

    _isolated_xdg(monkeypatch, tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    descriptor = _write_project(tmp_path / "catalog" / "sample")
    monkeypatch.chdir(source)

    result = main(
        [
            "project",
            "register",
            "--descriptor",
            str(descriptor),
            "--source",
            str(source),
        ]
    )

    assert result == 0
    assert "Registered project sample" in capsys.readouterr().out
    project = resolve_current_project(default_control_root(), cwd=source)
    assert project.directory == descriptor.parent.resolve()


def test_conflicting_descriptor_registration_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_xdg(monkeypatch, tmp_path)
    first = _write_project(tmp_path / "catalog-a" / "sample")
    second = _write_project(tmp_path / "catalog-b" / "sample")
    register_project_descriptor(first)

    with pytest.raises(ProjectError, match="already registered"):
        register_project_descriptor(second)

    registration = load_project_registration("sample")
    assert registration is not None
    assert registration.descriptor == first.resolve()


def test_current_directory_rejects_equal_depth_ambiguous_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_xdg(monkeypatch, tmp_path)
    root = ControlPlaneHome.resolve(cwd=tmp_path).ensure_layout().root
    source = tmp_path / "source"
    source.mkdir()
    register_project_descriptor(
        _write_project(tmp_path / "catalog" / "alpha", project_id="alpha"),
        source_root=source,
    )
    register_project_descriptor(
        _write_project(tmp_path / "catalog" / "beta", project_id="beta"),
        source_root=source,
    )

    with pytest.raises(ProjectError, match="matches multiple projects: alpha, beta"):
        resolve_current_project(root, cwd=source)


def test_project_id_cannot_escape_registration_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_xdg(monkeypatch, tmp_path)

    with pytest.raises(ProjectError, match="invalid project id"):
        project_binding_path("../escape")


def test_home_show_json_reports_xdg_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from execraft.cli import main

    _isolated_xdg(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)

    assert main(["home", "show", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["origin"] == "xdg"
    assert Path(payload["root"]) == default_control_root()
    assert Path(payload["config"]) == (tmp_path / "config" / "execraft").resolve()
