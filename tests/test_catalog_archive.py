"""Tests for the reversible project/task catalog archive."""

from pathlib import Path

import pytest
import yaml

from execraft.catalog_archive import CatalogArchiveError, CatalogArchiveManager
from execraft.project import load_project_registration, register_project_descriptor


def _write_project(root: Path, project_id: str, *task_ids: str) -> None:
    project = root / "projects" / project_id
    (project / "tasks").mkdir(parents=True)
    (project / "project.yaml").write_text(
        f"schema_version: 2\nproject: {project_id}\ndescription: {project_id} project\nrepositories: []\n",
        encoding="utf-8",
    )
    for task_id in task_ids:
        task = project / "tasks" / task_id
        task.mkdir()
        (task / "TASK.yaml").write_text(
            f"schema_version: 1\nid: {task_id}\ntitle: {task_id} title\nstatus: review\n",
            encoding="utf-8",
        )
        (task / "PLAN.md").write_text(f"# {task_id}\n", encoding="utf-8")


def test_catalog_lists_tasks_across_active_projects(tmp_path: Path) -> None:
    _write_project(tmp_path, "alpha", "current", "old-a")
    _write_project(tmp_path, "beta", "old-b")
    manager = CatalogArchiveManager(tmp_path, tmp_path / "state")

    catalog = manager.catalog(current_project="alpha", current_task="current")

    assert {item["id"] for item in catalog["active_projects"]} == {"alpha", "beta"}
    assert {item["id"] for item in catalog["active_tasks"]} == {
        "current",
        "old-a",
        "old-b",
    }
    current = next(item for item in catalog["active_tasks"] if item["id"] == "current")
    assert current["status"] == "current"


def test_archive_inspect_verify_and_reactivate_task(tmp_path: Path) -> None:
    _write_project(tmp_path, "alpha", "current", "old")
    manager = CatalogArchiveManager(tmp_path, tmp_path / "state")

    archived = manager.archive_task("alpha", "old", reason="superseded")

    assert not (tmp_path / "projects" / "alpha" / "tasks" / "old").exists()
    archive_path = tmp_path / "projects" / ".archive" / "tasks" / "alpha" / "old"
    assert archive_path.is_dir()
    assert archived["entry"]["reason"] == "superseded"
    assert archived["verification"]["ok"] is True
    assert {item["name"] for item in archived["previews"]} >= {"TASK.yaml", "PLAN.md"}

    restored = manager.reactivate_task("alpha", "old")

    active_path = tmp_path / "projects" / "alpha" / "tasks" / "old"
    assert restored["archived"] is False
    assert active_path.is_dir()
    assert not (active_path / ".archive.yaml").exists()
    history = list((active_path / ".execraft-archive-history").glob("*.yaml"))
    assert len(history) == 1


def test_reactivation_rejects_tampered_archive(tmp_path: Path) -> None:
    _write_project(tmp_path, "alpha", "old")
    manager = CatalogArchiveManager(tmp_path)
    manager.archive_task("alpha", "old")
    archive_path = tmp_path / "projects" / ".archive" / "tasks" / "alpha" / "old"
    (archive_path / "PLAN.md").write_text("tampered\n", encoding="utf-8")

    report = manager.verify("task", project_id="alpha", item_id="old")

    assert report["ok"] is False
    assert report["changed"] == ["PLAN.md"]
    with pytest.raises(CatalogArchiveError, match="integrity verification failed"):
        manager.reactivate_task("alpha", "old")


def test_inspection_rejects_archive_metadata_identity_drift(tmp_path: Path) -> None:
    _write_project(tmp_path, "alpha", "old")
    manager = CatalogArchiveManager(tmp_path)
    manager.archive_task("alpha", "old")
    metadata_path = (
        tmp_path
        / "projects"
        / ".archive"
        / "tasks"
        / "alpha"
        / "old"
        / ".archive.yaml"
    )
    metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
    metadata["id"] = "different-task"
    metadata_path.write_text(
        yaml.safe_dump(metadata, sort_keys=False), encoding="utf-8"
    )

    with pytest.raises(CatalogArchiveError, match="identity does not match"):
        manager.inspect("task", project_id="alpha", item_id="old")


def test_archive_and_reactivate_project(tmp_path: Path) -> None:
    _write_project(tmp_path, "alpha", "one", "two")
    manager = CatalogArchiveManager(tmp_path)

    result = manager.archive_project("alpha", reason="inactive program")

    assert result["entry"]["task_count"] == 2
    assert not (tmp_path / "projects" / "alpha").exists()
    restored = manager.reactivate_project("alpha")
    assert restored["id"] == "alpha"
    assert restored["task_count"] == 2


def test_project_archive_preserves_host_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control = tmp_path / "control"
    _write_project(control, "alpha", "one")
    project = control / "projects" / "alpha"
    (project / "project.yaml").write_text(
        "\n".join(
            (
                "schema_version: 2",
                "project: alpha",
                "path_base: project_directory",
                "repositories:",
                "  - id: component",
                "    path: .",
                "    workspace_name: component",
                "",
            )
        ),
        encoding="utf-8",
    )
    source_root = tmp_path / "source"
    source_root.mkdir()
    monkeypatch.setenv("EXECRAFT_CONFIG_HOME", str(tmp_path / "config"))
    register_project_descriptor(project, source_root=source_root)

    manager = CatalogArchiveManager(control)
    manager.archive_project("alpha")

    assert load_project_registration("alpha") is None
    manager.reactivate_project("alpha")
    registration = load_project_registration("alpha")
    assert registration is not None
    assert registration.descriptor == (project / "project.yaml").resolve()
    assert registration.source_root == source_root.resolve()


def test_archive_refuses_symlinks(tmp_path: Path) -> None:
    _write_project(tmp_path, "alpha", "old")
    task = tmp_path / "projects" / "alpha" / "tasks" / "old"
    target = tmp_path / "outside.txt"
    target.write_text("outside", encoding="utf-8")
    try:
        (task / "unsafe-link").symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable on this platform")

    with pytest.raises(CatalogArchiveError, match="do not accept symlinks"):
        CatalogArchiveManager(tmp_path).archive_task("alpha", "old")
