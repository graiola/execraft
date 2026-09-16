from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from execraft.bootstrap import create_onboarding_service
from execraft.cli import main
from execraft.control_plane import ControlPlaneHome
from execraft.gui.application import (
    ActiveTaskRef,
    ControlCenterError,
    ControlCenterService,
)
from execraft.gui.errors import GuiError
from execraft.gui.server import _dashboard_asset, _dashboard_html
from execraft.project import load_registered_project


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _source_repository(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    _git(path, "config", "user.name", "GUI Test")
    _git(path, "config", "user.email", "gui@example.test")
    (path / "pyproject.toml").write_text(
        "[project]\nname='demo'\nversion='0.1.0'\n",
        encoding="utf-8",
    )
    (path / "test_demo.py").write_text("def test_demo():\n    assert True\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "Initial")
    return path


class FakeTaskDashboard:
    def __init__(self, project_id: str, task_id: str) -> None:
        self.project_id = project_id
        self.task_id = task_id
        self.closed = False
        self.running = False
        self.process = SimpleNamespace(
            status=lambda: {
                "owned_running": self.running,
                "external_running": False,
            }
        )

    def snapshot(self):
        return {
            "project": {"id": self.project_id, "task_id": self.task_id},
            "packages": [],
            "assignments": [],
            "agents": [],
            "nodes": [],
            "configs": [],
            "run": {},
            "run_control": {},
            "supervisor": {},
        }

    def list_tasks(self):
        return [{"id": self.task_id, "current": True}]

    def close(self):
        self.closed = True


@pytest.fixture
def control_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ControlPlaneHome:
    monkeypatch.setenv("EXECRAFT_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("EXECRAFT_STATE_HOME", str(tmp_path / "state"))
    return ControlPlaneHome(root=tmp_path / "control", origin="test")


@pytest.fixture
def service(control_home: ControlPlaneHome):
    created: list[FakeTaskDashboard] = []

    def factory(project_id: str, task_id: str) -> FakeTaskDashboard:
        dashboard = FakeTaskDashboard(project_id, task_id)
        created.append(dashboard)
        return dashboard

    result = ControlCenterService(
        home=control_home,
        onboarding=create_onboarding_service(),
        task_dashboard_factory=factory,
    )
    result.created_dashboards = created  # type: ignore[attr-defined]
    yield result
    result.close()


def test_project_level_archive_works_without_an_open_task(
    service: ControlCenterService, tmp_path: Path
):
    source = _source_repository(tmp_path / "demo")
    service.create_project_from_source(str(source), acknowledged=True)
    project = load_registered_project(service.root, "demo")
    old = project.directory / "tasks" / "old-task"
    old.mkdir(parents=True)
    (old / "TASK.yaml").write_text(
        "schema_version: 1\nid: old-task\ntitle: Old task\nstatus: merged\n",
        encoding="utf-8",
    )

    assert {item["id"] for item in service.archive_catalog()["active_tasks"]} == {
        "old-task"
    }
    archived = service.archive_catalog_entry(
        "task", project_id="demo", item_id="old-task", reason="historical"
    )

    assert archived["verification"]["ok"] is True
    catalog = service.archive_catalog()
    assert not catalog["active_tasks"]
    assert {item["id"] for item in catalog["archived_tasks"]} == {"old-task"}
    inspected = service.inspect_archive(
        "task", project_id="demo", item_id="old-task"
    )
    assert inspected["entry"]["reason"] == "historical"

    restored = service.reactivate_catalog_entry(
        "task", project_id="demo", item_id="old-task"
    )
    assert restored["id"] == "old-task"
    assert {item["id"] for item in service.archive_catalog()["active_tasks"]} == {
        "old-task"
    }


def test_project_level_archive_refuses_the_open_task(
    service: ControlCenterService, tmp_path: Path
):
    source = _source_repository(tmp_path / "demo")
    service.create_project_from_source(str(source), acknowledged=True)
    project = load_registered_project(service.root, "demo")
    task = project.directory / "tasks" / "demo-task"
    task.mkdir(parents=True)
    (task / "TASK.yaml").write_text(
        "schema_version: 1\nid: demo-task\ntitle: Demo task\nstatus: planned\n",
        encoding="utf-8",
    )
    service.open_task("demo", "demo-task", acknowledged=True)

    with pytest.raises(GuiError, match="currently open task"):
        service.archive_catalog_entry(
            "task", project_id="demo", item_id="demo-task", reason="invalid"
        )


def test_project_independent_home_is_valid_with_empty_catalog(service: ControlCenterService):
    snapshot = service.snapshot()

    assert snapshot["mode"] == "home"
    assert snapshot["projects"] == []
    assert snapshot["application"]["active_task"] is None
    assert snapshot["templates"]["greenfield"]


def test_inspect_and_atomically_register_existing_source(
    service: ControlCenterService, tmp_path: Path
):
    source = _source_repository(tmp_path / "demo")

    preview = service.inspect_source(str(source))
    assert preview["report"]["project_id"] == "demo"
    assert preview["creation"]["plan"]["can_apply"] is True
    assert not (service.home.projects_dir / "demo").exists()

    result = service.create_project_from_source(
        str(source), acknowledged=True
    )

    assert result["outcome"]["applied"] is True
    assert load_registered_project(service.root, "demo").id == "demo"
    home = result["home"]
    assert home["focused_project_id"] == "demo"
    assert home["projects"][0]["source_root"] == str(source)

    repeated = service.inspect_source(str(source))
    assert repeated["registered_project"]["id"] == "demo"
    assert repeated["creation"] is None


def test_project_home_exposes_profiles_and_applies_feature_selection(
    service: ControlCenterService, tmp_path: Path
):
    source = _source_repository(tmp_path / "profile-demo")
    catalog = service.template_catalog()
    assert any(item["reference"] == "standard@3" for item in catalog["profiles"])
    assert any(item["reference"] == "standard@1" for item in catalog["profiles"])
    assert not any(item["reference"] == "standard@1" for item in catalog["new_project_profiles"])
    assert any(item["reference"] == "standard@3" for item in catalog["new_project_profiles"])
    assert any(item["reference"] == "devcontainer@1" for item in catalog["features"])

    preview = service.inspect_source(
        str(source),
        template_id="starter",
        feature_ids=("docker",),
        include_devcontainer=True,
    )
    metadata = preview["creation"]["plan"]["metadata"]
    assert metadata["template"] == "starter@1"
    assert "docker@1" in metadata["features"]
    assert "devcontainer@1" in metadata["features"]

    service.create_project_from_source(
        str(source),
        template_id="starter",
        feature_ids=("docker",),
        include_devcontainer=True,
        acknowledged=True,
    )
    project = load_registered_project(service.root, "profile-demo")
    assert project.profile == "starter@1"
    assert "docker@1" in project.features
    assert "devcontainer@1" in project.features


def test_project_creation_requires_explicit_acknowledgement(
    service: ControlCenterService, tmp_path: Path
):
    source = _source_repository(tmp_path / "demo")
    with pytest.raises(ControlCenterError, match="explicit acknowledgement"):
        service.create_project_from_source(str(source))


def test_task_start_review_and_resumable_journal_include_original_intent(
    service: ControlCenterService, tmp_path: Path
):
    source = _source_repository(tmp_path / "demo")
    service.create_project_from_source(str(source), acknowledged=True)
    payload = {
        "project_id": "demo",
        "source_root": str(source),
        "description": "Add a health endpoint",
        "task_id": "health-endpoint",
        "planner": "local",
        "no_workspace": True,
    }

    preview = service.preview_start(payload)
    assert preview["can_apply"] is True
    assert preview["task_id"] == "health-endpoint"

    result = service.apply_start(payload, acknowledged=True)
    assert result["outcome"]["ready"] is True
    review = result["task"]
    assert "Add a health endpoint" in review["brief"]
    assert review["plan_graph"]["work_packages"]
    assert review["journal"]["description"] == "Add a health endpoint"
    assert review["resumable"] is False


def test_start_rejects_string_booleans(service: ControlCenterService, tmp_path: Path):
    source = _source_repository(tmp_path / "demo")
    service.create_project_from_source(str(source), acknowledged=True)
    with pytest.raises(ControlCenterError, match="no_workspace must be a JSON boolean"):
        service.preview_start(
            {
                "project_id": "demo",
                "source_root": str(source),
                "description": "Task",
                "planner": "local",
                "no_workspace": "false",
            }
        )


def test_verification_approval_is_conflict_checked(
    service: ControlCenterService, tmp_path: Path
):
    source = _source_repository(tmp_path / "demo")
    service.create_project_from_source(str(source), acknowledged=True)
    current = service.verification_snapshot("demo")
    assert current["commands"]

    updated = service.update_verification(
        "demo",
        expected_sha256=current["sha256"],
        enabled_indexes=[0],
        require_commands=True,
        acknowledged=True,
    )
    assert updated["verification"]["commands"][0]["enabled"] is True

    with pytest.raises(ControlCenterError, match="changed since it was loaded"):
        service.update_verification(
            "demo",
            expected_sha256=current["sha256"],
            enabled_indexes=[],
            require_commands=True,
            acknowledged=True,
        )


def test_task_session_can_switch_in_place_but_not_while_owned_driver_runs(
    service: ControlCenterService,
):
    first = service.open_task("demo", "one", acknowledged=True)
    assert first["mode"] == "task"
    active = service.created_dashboards[-1]  # type: ignore[attr-defined]
    active.running = True

    with pytest.raises(ControlCenterError, match="stop the dashboard-owned"):
        service.open_task("demo", "two", acknowledged=True)
    assert service.active_task == ActiveTaskRef("demo", "one")

    active.running = False
    second = service.open_task("demo", "two", acknowledged=True)
    assert second["project"]["task_id"] == "two"
    assert active.closed is True


def test_greenfield_preview_and_creation_are_available_from_home(
    service: ControlCenterService, tmp_path: Path
):
    payload = {
        "name": "green-demo",
        "parent": str(tmp_path / "sources"),
        "template": "python-library",
    }
    (tmp_path / "sources").mkdir()

    preview = service.preview_greenfield(payload)
    assert preview["applied"] is False
    assert preview["source_creation"]["files"]

    result = service.apply_greenfield(payload, acknowledged=True)
    assert result["project"]["applied"] is True
    assert (tmp_path / "sources" / "green-demo" / ".git").exists()
    assert any(item["id"] == "green-demo" for item in result["home"]["projects"])


def test_dashboard_assets_expose_onboarding_home_and_module():
    html = _dashboard_html("test-token")
    script, content_type = _dashboard_asset("onboarding-view.js")

    assert 'id="homeContent"' in html
    assert 'id="projectWorkbench"' in html
    assert 'id="taskReviewDialog"' in html
    assert b"class OnboardingView" in script
    assert content_type.startswith("text/javascript")


def test_gui_command_launches_without_a_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("EXECRAFT_CONTROL_ROOT", str(tmp_path / "control"))
    monkeypatch.setenv("EXECRAFT_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("EXECRAFT_STATE_HOME", str(tmp_path / "state"))
    captured = {}

    def fake_serve_dashboard(*, service, **_kwargs):
        captured["snapshot"] = service.snapshot()
        service.close()

    monkeypatch.setattr("execraft.gui.serve_dashboard", fake_serve_dashboard)

    assert main(["gui", "--port", "0", "--no-open-browser"]) == 0
    assert captured["snapshot"]["mode"] == "home"
    assert captured["snapshot"]["projects"] == []


def test_http_handler_exposes_project_independent_onboarding_routes(
    service: ControlCenterService, tmp_path: Path
):
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    from execraft.gui.server import _handler_factory

    source = _source_repository(tmp_path / "http-demo")
    try:
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            _handler_factory(service, "test-token"),
        )
    except PermissionError:
        pytest.skip("loopback sockets are unavailable in this execution sandbox")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(f"{base}/api/home") as response:
            home = json.loads(response.read())
        assert home["mode"] == "home"

        request = urllib.request.Request(
            f"{base}/api/onboarding/inspect",
            data=json.dumps({"source_root": str(source)}).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Execraft-Token": "test-token",
            },
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            preview = json.loads(response.read())
        assert preview["report"]["project_id"] == "http-demo"
        assert preview["creation"]["plan"]["can_apply"] is True

        unsafe = urllib.request.Request(
            f"{base}/api/onboarding/project/create",
            data=json.dumps(
                {
                    "source_root": str(source),
                    "acknowledged": "false",
                }
            ).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Execraft-Token": "test-token",
            },
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(unsafe)
        assert error.value.code == 409
        body = json.loads(error.value.read())
        assert body["error"] == "acknowledged must be a JSON boolean"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_project_catalog_is_a_distinct_navigation_scope(
    service: ControlCenterService, tmp_path: Path
):
    source = _source_repository(tmp_path / "navigation-demo")
    service.create_project_from_source(str(source), acknowledged=True)
    service.apply_start(
        {
            "project_id": "navigation-demo",
            "description": "Create the first task",
            "task_id": "first-task",
            "planner": "local",
            "no_workspace": True,
        },
        acknowledged=True,
    )
    task_snapshot = service.open_task(
        "navigation-demo",
        "first-task",
        acknowledged=True,
    )
    assert task_snapshot["mode"] == "task"

    project_snapshot = service.open_home(acknowledged=True)
    assert project_snapshot["mode"] == "home"
    assert project_snapshot["focused_project_id"] == "navigation-demo"
    assert project_snapshot["projects"][0]["tasks"][0]["id"] == "first-task"

    catalog_snapshot = service.open_catalog(acknowledged=True)
    assert catalog_snapshot["mode"] == "home"
    assert catalog_snapshot["focused_project_id"] == ""
    assert catalog_snapshot["projects"][0]["tasks"][0]["id"] == "first-task"


def test_switching_from_task_to_project_workspace_obeys_process_ownership(
    service: ControlCenterService, tmp_path: Path
):
    source = _source_repository(tmp_path / "safe-navigation")
    service.create_project_from_source(str(source), acknowledged=True)
    service.apply_start(
        {
            "project_id": "safe-navigation",
            "description": "Create a task",
            "task_id": "first-task",
            "planner": "local",
            "no_workspace": True,
        },
        acknowledged=True,
    )
    service.open_task("safe-navigation", "first-task", acknowledged=True)
    dashboard = service.created_dashboards[-1]  # type: ignore[attr-defined]
    dashboard.running = True

    with pytest.raises(ControlCenterError, match="stop the dashboard-owned"):
        service.open_project("safe-navigation", acknowledged=True)

    dashboard.running = False
    snapshot = service.open_project("safe-navigation", acknowledged=True)
    assert snapshot["focused_project_id"] == "safe-navigation"
    assert service.active_task is None
    assert dashboard.closed is True


def test_gui_start_accepts_inline_plan_import_without_description(
    service: ControlCenterService,
    tmp_path: Path,
):
    source = _source_repository(tmp_path / "inline-import")
    service.create_project_from_source(str(source), acknowledged=True)
    plan = "# Plan: GUI imported plan\n\n## Scope\nPreserve this document exactly."
    payload = {
        "project_id": "inline-import",
        "source_root": str(source),
        "description": "",
        "task_id": "gui-import",
        "planner": "local",
        "no_workspace": True,
        "plan_markdown": plan,
        "plan_source": "browser-upload:PLAN.md",
    }

    preview = service.preview_start(payload)
    assert preview["can_apply"] is True
    assert preview["task_id"] == "gui-import"

    result = service.apply_start(payload, acknowledged=True)
    review = result["task"]
    assert review["plan_graph"]["work_packages"]
    dossier = service.root / "projects" / "inline-import" / "tasks" / "gui-import"
    assert (dossier / "PLAN.md").read_text(encoding="utf-8") == plan
    definition = yaml.safe_load((dossier / "DEFINITION.yaml").read_text(encoding="utf-8"))
    assert definition["sources"]["PLAN.md"]["source"] == "browser-upload:PLAN.md"


def test_project_level_permanent_delete_requires_exact_confirmation(
    service: ControlCenterService, tmp_path: Path
):
    source = _source_repository(tmp_path / "demo-delete")
    service.create_project_from_source(str(source), acknowledged=True)
    project = load_registered_project(service.root, "demo-delete")
    task = project.directory / "tasks" / "old-task"
    task.mkdir(parents=True)
    (task / "TASK.yaml").write_text(
        "schema_version: 1\nid: old-task\ntitle: Old task\nstatus: merged\n",
        encoding="utf-8",
    )

    with pytest.raises(GuiError, match="exact project/task ID"):
        service.delete_catalog_entry(
            "task",
            project_id="demo-delete",
            item_id="old-task",
            confirmation="wrong",
        )

    result = service.delete_catalog_entry(
        "task",
        project_id="demo-delete",
        item_id="old-task",
        confirmation="old-task",
    )

    assert result["kind"] == "task"
    assert result["id"] == "old-task"
    assert not task.exists()


def test_project_home_counts_roadmaps_and_permanent_task_delete_is_guarded(
    service: ControlCenterService, tmp_path: Path
) -> None:
    source = _source_repository(tmp_path / "roadmap-delete")
    service.create_project_from_source(str(source), acknowledged=True)
    project = load_registered_project(service.root, "roadmap-delete")
    task = project.directory / "tasks" / "alpha"
    task.mkdir(parents=True)
    repository = project.repositories[0]
    (task / "TASK.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 2,
                "id": "alpha",
                "project": "roadmap-delete",
                "title": "Alpha",
                "status": "planned",
                "created_at": "2026-09-09T00:00:00+00:00",
                "git": {"branch_name": "task/alpha", "merge_strategy": "squash"},
                "repositories": [
                    {
                        "id": repository.id,
                        "base_branch": repository.base_branch,
                        "task_branch": "task/alpha",
                    }
                ],
                "integration": {"verify": []},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    roadmap = service.roadmap_create("roadmap-delete", title="Platform")
    linked = service.roadmap_link_task(
        "roadmap-delete",
        roadmap["id"],
        expected_revision=roadmap["revision"],
        task_id="alpha",
    )

    home = service.home_snapshot()
    project_row = next(item for item in home["projects"] if item["id"] == "roadmap-delete")
    assert project_row["roadmap_count"] == 1
    assert linked["items"][0]["task"]["title"] == "Alpha"

    with pytest.raises(GuiError, match="remove or detach roadmap references"):
        service.delete_catalog_entry(
            "task",
            project_id="roadmap-delete",
            item_id="alpha",
            confirmation="alpha",
        )
    assert task.is_dir()

    service.roadmap_delete_item(
        "roadmap-delete",
        roadmap["id"],
        item_id=linked["items"][0]["id"],
        expected_revision=linked["revision"],
    )
    deleted = service.delete_catalog_entry(
        "task",
        project_id="roadmap-delete",
        item_id="alpha",
        confirmation="alpha",
    )
    assert deleted["id"] == "alpha"
    assert not task.exists()


def test_roadmap_gui_assets_are_packaged_and_expose_interactive_workspace() -> None:
    html = _dashboard_html("token")
    roadmap_js, content_type = _dashboard_asset("roadmap-view.js")
    source = roadmap_js.decode("utf-8")
    interactions, interactions_type = _dashboard_asset("roadmap-interactions.js")
    onboarding = _dashboard_asset("onboarding-view.js")[0].decode("utf-8")

    assert content_type == "text/javascript; charset=utf-8"
    assert interactions_type == "text/javascript; charset=utf-8"
    assert b"rowDropIntent" in interactions
    assert b"laneDropIntent" in interactions
    assert b"connectorCurve" in interactions
    assert b"fittedTimelineDomain" in interactions
    for identifier in (
        "projectRoadmapTab",
        "roadmapTimelineScroll",
        "roadmapTimelineCanvas",
        "roadmapInspector",
        "roadmapUnscheduledTasks",
        "roadmapCreateDialog",
        "roadmapBlockPalette",
        "roadmapPaletteBtn",
        "roadmapFitBtn",
        "roadmapSelectedBtn",
        "roadmapGroupBy",
        "roadmapTaskTrayToggle",
        "roadmapCanonicalInitializeDialog",
    ):
        assert f'id="{identifier}"' in html
    assert "class RoadmapView" in source
    assert "/api/roadmap/item/upsert" in source
    assert "/api/roadmap/item/link-task" in source
    assert "/api/roadmap/item/move" in source
    assert "/api/roadmap/lane/move" in source
    assert "/api/roadmap/relation/upsert" in source
    assert "pointerdown" in source
    assert "dragstart" in source
    assert "data-roadmap-primary" in source
    assert "data-roadmap-connect-out" in source
    assert "data-roadmap-row-drag" in source
    assert "data-roadmap-lane-drag" in source
    assert "data-roadmap-resize" in source
    assert "data-roadmap-schedule-duration" in source
    assert "scheduleWithDuration" in source
    assert "event.shiftKey" in source
    assert "roadmap-interactions.js" in source
    assert "#suppressPostDragClick" in source
    assert "#selectItem" in source
    assert "#openItem" in source
    assert "Open in Execution" in source
    assert "expected_project_execution_revision" in source
    assert "project_phase" in source
    assert "roadmapCanonicalInitializeDialog" in source
    assert "sessionStorage.setItem" in source
    assert 'from "./roadmap-view.js"' in onboarding
    assert "new_project_profiles" in onboarding
    assert "new_project_selectable" in onboarding
    assert "createTaskFromRoadmap" in onboarding
    assert 'this.projectView = "roadmap"' in onboarding
    assert 'id="taskRoadmapBackBtn"' in html
    assert 'id="projectRoadmapTab" class="project-tab active"' in html
    assert 'id="projectTasksView" class="project-detail-view hidden"' in html
