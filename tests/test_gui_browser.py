"""Optional browser journeys for the dashboard workbench shell.

Install the test/browser extras and Playwright Chromium to run these locally:

    python -m pip install -e '.[test,browser]'
    python -m playwright install chromium
    pytest -q tests/test_gui_browser.py
"""

from __future__ import annotations

import copy
import json
import threading
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import pytest

from execraft.gui.server import _dashboard_asset, _dashboard_html


playwright = pytest.importorskip("playwright.sync_api")


SNAPSHOT = {
    "mode": "task",
    "project": {"id": "demo", "task_id": "demo-task", "repositories": ["repo"]},
    "orchestration": {
        "state": "waiting_for_agent",
        "started_at": "2026-07-29T12:00:00+00:00",
        "last_transition_at": "2026-07-29T12:00:00+00:00",
        "scheduler": {},
    },
    "packages": [
        {
            "id": "WP1",
            "title": "Workbench shell",
            "stage": "implement",
            "status": "running",
            "dependencies": [],
            "affected_repositories": ["repo"],
            "computed_complexity": 30,
            "risk": "low",
            "execution_mode": "standard",
            "parallel_safe": False,
            "parent_id": "",
            "shard_ids": [],
        }
    ],
    "assignments": [
        {
            "package_id": "WP1",
            "package_title": "Workbench shell",
            "stage": "implement",
            "agent_id": "codex",
            "parallel": False,
        }
    ],
    "agents": [],
    "nodes": [],
    "configs": [],
    "config_errors": [],
    "paths": {"log": "/tmp/events.json", "driver_log": "/tmp/driver.log"},
    "run": {
        "owned_running": True,
        "external_running": False,
        "pid": 1234,
        "started_at": "2026-07-29T12:00:00+00:00",
        "last_exit_code": None,
        "last_exit_summary": "",
    },
    "run_control": {"can_start": False, "label": "Running", "reason": ""},
    "supervisor": {
        "enabled": True,
        "available": True,
        "agent_id": "codex",
        "policy": {},
        "incident": {},
    },
}


HOME_SNAPSHOT = {
    "mode": "home",
    "generated_at": "2026-08-05T10:00:00+00:00",
    "application": {
        "active_task": None,
        "focused_project_id": "",
        "home_available": True,
    },
    "control_home": {"root": "/tmp/control", "origin": "test"},
    "focused_project_id": "",
    "projects": [
        {
            "id": "demo",
            "description": "Browser onboarding fixture",
            "descriptor": "/tmp/control/projects/demo/project.yaml",
            "source_root": "/tmp/demo",
            "origin": "registry",
            "repositories": [
                {
                    "id": "demo",
                    "role": "component",
                    "required": True,
                    "base_branch": "main",
                }
            ],
            "readiness": {"project": "demo", "ready": False, "checks": []},
            "tasks": [
                {
                    "id": "demo-task",
                    "title": "Demo task",
                    "status": "planned",
                    "repositories": ["demo"],
                    "modified_at": "2026-08-05T09:00:00+00:00",
                    "has_plan": True,
                    "current": False,
                },
                {
                    "id": "blocked-task",
                    "title": "Blocked task",
                    "status": "blocked",
                    "repositories": ["demo"],
                    "modified_at": "2026-08-05T08:00:00+00:00",
                    "has_plan": True,
                    "current": False,
                },
            ],
            "task_count": 2,
            "roadmap_count": 1,
            "active": False,
        }
    ],
    "templates": {
        "onboarding": [],
        "greenfield": [
            {
                "id": "python-service",
                "version": 1,
                "reference": "python-service@1",
                "description": "Python service",
            }
        ],
    },
    "onboarding_sessions": [],
}


ROADMAP_SNAPSHOT = {
    "schema_version": 1,
    "project_id": "demo",
    "id": "platform",
    "title": "Platform roadmap",
    "description": "Browser roadmap fixture",
    "revision": 2,
    "created_at": "2026-09-09T08:00:00+00:00",
    "updated_at": "2026-09-09T08:15:00+00:00",
    "sha256": "fixture",
    "lanes": ["Platform"],
    "items": [
        {
            "id": "task-demo",
            "kind": "task",
            "task_id": "demo-task",
            "lane": "Platform",
            "order": 10,
            "schedule": {"start": "2026-09-10", "target": "2026-09-20"},
            "task": {
                "id": "demo-task",
                "title": "Demo task",
                "status": "in_progress",
                "availability": "active",
                "repositories": ["demo"],
                "runtime_state": "running",
                "completed_packages": 1,
                "total_packages": 2,
                "progress_percent": 50,
            },
        },
        {
            "id": "planned-auth",
            "kind": "planned_task",
            "title": "Future auth work",
            "description": "Add authentication",
            "lane": "Platform",
            "order": 20,
            "schedule": {"target": "2026-10-01"},
        },
    ],
    "relations": [],
    "unscheduled_tasks": [
        {
            "id": "blocked-task",
            "title": "Blocked task",
            "status": "blocked",
            "availability": "active",
            "repositories": ["demo"],
            "runtime_state": "",
            "completed_packages": 0,
            "total_packages": 0,
            "progress_percent": 0,
        }
    ],
    "statistics": {
        "items": 2,
        "linked_tasks": 1,
        "planned_tasks": 1,
        "milestones": 0,
        "gates": 0,
        "unscheduled_tasks": 1,
    },
}


SEMANTIC_CONSOLE_PAYLOAD = {
    "selected_session_id": "session-1",
    "sessions_included": True,
    "sessions": [
        {
            "session_id": "session-1",
            "origin": "orchestrator",
            "package_id": "WP1",
            "stage": "implement",
            "status": "running",
            "started_at": "2026-07-29T12:00:00+00:00",
        }
    ],
    "metadata": {
        "session_id": "session-1",
        "agent_id": "codex",
        "package_id": "WP1",
        "stage": "implement",
        "adapter": "codex",
        "model": "test-model",
        "origin": "orchestrator",
        "status": "running",
        "started_at": "2026-07-29T12:00:00+00:00",
        "telemetry": {},
        "artifact": {},
        "interaction": {
            "mode": "conversation",
            "streaming": True,
            "steering_supported": True,
            "progress": {},
        },
        "terminal": {"enabled": True},
    },
    "manual_console": {},
    "events": [],
    "interactions": [],
    "next_offset": 0,
    "interaction_next_offset": 0,
    "version": 1,
}


DENSE_WORKFLOW_SNAPSHOT = {
    **SNAPSHOT,
    "packages": [
        {
            "id": "A",
            "title": "Source",
            "stage": "completed",
            "status": "completed",
            "dependencies": [],
            "affected_repositories": ["repo"],
            "computed_complexity": 10,
            "risk": "low",
            "execution_mode": "standard",
            "parallel_safe": False,
            "parent_id": "",
            "shard_ids": [],
        },
        *[
            {
                "id": package_id,
                "title": f"Parallel branch {package_id}",
                "stage": "completed",
                "status": "completed",
                "dependencies": ["A"],
                "affected_repositories": ["repo"],
                "computed_complexity": 10,
                "risk": "low",
                "execution_mode": "standard",
                "parallel_safe": True,
                "parent_id": "",
                "shard_ids": [],
            }
            for package_id in ("B", "C", "D")
        ],
        {
            "id": "E",
            "title": "Fan-in target",
            "stage": "completed",
            "status": "completed",
            "dependencies": ["B", "C", "D"],
            "affected_repositories": ["repo"],
            "computed_complexity": 10,
            "risk": "low",
            "execution_mode": "standard",
            "parallel_safe": False,
            "parent_id": "",
            "shard_ids": [],
        },
        {
            "id": "F",
            "title": "Straight continuation",
            "stage": "implement",
            "status": "running",
            "dependencies": ["E"],
            "affected_repositories": ["repo"],
            "computed_complexity": 10,
            "risk": "low",
            "execution_mode": "standard",
            "parallel_safe": False,
            "parent_id": "",
            "shard_ids": [],
        },
    ],
    "assignments": [
        {
            "package_id": "F",
            "package_title": "Straight continuation",
            "stage": "implement",
            "agent_id": "codex",
            "parallel": False,
        }
    ],
}


@contextmanager
def dashboard_fixture_server(snapshot=SNAPSHOT, *, console_payload=None, expose_state=False):
    current_snapshot = copy.deepcopy(snapshot)
    current_roadmap = copy.deepcopy(ROADMAP_SNAPSHOT)
    current_console_payload = copy.deepcopy(console_payload) if console_payload is not None else {
        "selected_session_id": "",
        "sessions_included": True,
        "sessions": [],
        "metadata": {},
        "manual_console": {},
        "events": [],
        "interactions": [],
        "next_offset": 0,
        "interaction_next_offset": 0,
        "version": 0,
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            path = urlparse(self.path).path
            if path == "/":
                payload = _dashboard_html("browser-test").encode()
                content_type = "text/html; charset=utf-8"
            elif path.startswith("/assets/"):
                payload, content_type = _dashboard_asset(path.removeprefix("/assets/"))
            elif path == "/api/snapshot":
                payload = json.dumps(current_snapshot).encode()
                content_type = "application/json"
            elif path == "/api/projects":
                payload = json.dumps({"projects": copy.deepcopy(HOME_SNAPSHOT["projects"])}).encode()
                content_type = "application/json"
            elif path == "/api/tasks":
                payload = json.dumps(
                    {
                        "tasks": [
                            {"id": "demo-task", "current": True, "has_state": True},
                            {"id": "blocked-task", "current": False, "has_state": True},
                        ]
                    }
                ).encode()
                content_type = "application/json"
            elif path == "/api/roadmaps":
                payload = json.dumps(
                    {
                        "project_id": "demo",
                        "roadmaps": [
                            {
                                "id": current_roadmap["id"],
                                "title": current_roadmap["title"],
                                "description": current_roadmap["description"],
                                "revision": current_roadmap["revision"],
                                "updated_at": current_roadmap["updated_at"],
                                "item_count": len(current_roadmap["items"]),
                                "task_count": current_roadmap["statistics"]["linked_tasks"],
                            }
                        ],
                    }
                ).encode()
                content_type = "application/json"
            elif path == "/api/roadmap":
                payload = json.dumps(current_roadmap).encode()
                content_type = "application/json"
            elif path == "/api/agent/console":
                payload = json.dumps(current_console_payload).encode()
                content_type = "application/json"
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):  # noqa: N802
            path = urlparse(self.path).path
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            if path == "/api/project/focus":
                focused = str(body.get("project_id", ""))
                replacement = copy.deepcopy(HOME_SNAPSHOT)
                replacement["focused_project_id"] = focused
                replacement["application"]["focused_project_id"] = focused
                current_snapshot.clear()
                current_snapshot.update(replacement)
                payload = json.dumps(current_snapshot).encode()
            elif path == "/api/session/catalog":
                replacement = copy.deepcopy(HOME_SNAPSHOT)
                current_snapshot.clear()
                current_snapshot.update(replacement)
                payload = json.dumps(current_snapshot).encode()
            elif path in {"/api/session/home", "/api/session/project"}:
                replacement = copy.deepcopy(HOME_SNAPSHOT)
                replacement["focused_project_id"] = str(body.get("project_id", "demo"))
                replacement["application"]["focused_project_id"] = replacement["focused_project_id"]
                current_snapshot.clear()
                current_snapshot.update(replacement)
                payload = json.dumps(current_snapshot).encode()
            elif path == "/api/session/open":
                replacement = copy.deepcopy(SNAPSHOT)
                replacement["project"]["id"] = str(body.get("project_id", "demo"))
                replacement["project"]["task_id"] = str(body.get("task_id", "demo-task"))
                current_snapshot.clear()
                current_snapshot.update(replacement)
                payload = json.dumps(current_snapshot).encode()
            elif path == "/api/roadmap/item/upsert":
                item = copy.deepcopy(body.get("item") or {})
                if not item.get("id"):
                    item["id"] = f"fixture-{len(current_roadmap['items']) + 1}"
                current_roadmap["items"] = [
                    existing for existing in current_roadmap["items"]
                    if existing["id"] != item["id"]
                ] + [item]
                current_roadmap["revision"] += 1
                current_roadmap["statistics"]["items"] = len(current_roadmap["items"])
                current_roadmap["statistics"]["planned_tasks"] = sum(
                    1 for existing in current_roadmap["items"]
                    if existing["kind"] == "planned_task"
                )
                current_roadmap["statistics"]["milestones"] = sum(
                    1 for existing in current_roadmap["items"]
                    if existing["kind"] == "milestone"
                )
                current_roadmap["statistics"]["gates"] = sum(
                    1 for existing in current_roadmap["items"]
                    if existing["kind"] == "gate"
                )
                payload = json.dumps(current_roadmap).encode()
            elif path == "/api/roadmap/item/move":
                item_id = str(body.get("item_id", ""))
                moving = next(item for item in current_roadmap["items"] if item["id"] == item_id)
                current_roadmap["items"] = [item for item in current_roadmap["items"] if item["id"] != item_id]
                moving["lane"] = str(body.get("lane", moving.get("lane", "General")))
                schedule = {}
                if body.get("start"):
                    schedule["start"] = str(body["start"])
                if body.get("target"):
                    schedule["target"] = str(body["target"])
                moving["schedule"] = schedule
                target_id = str(body.get("target_item_id", ""))
                placement = str(body.get("placement", "before"))
                insert_at = len(current_roadmap["items"])
                if target_id:
                    for index, existing in enumerate(current_roadmap["items"]):
                        if existing["id"] == target_id:
                            insert_at = index + (1 if placement == "after" else 0)
                            break
                current_roadmap["items"].insert(insert_at, moving)
                for index, existing in enumerate(current_roadmap["items"], 1):
                    existing["order"] = index * 10
                current_roadmap["lanes"] = list(dict.fromkeys(item.get("lane", "General") for item in current_roadmap["items"]))
                current_roadmap["revision"] += 1
                payload = json.dumps(current_roadmap).encode()
            elif path == "/api/roadmap/relation/upsert":
                relation = {
                    "from": str(body.get("from", "")),
                    "to": str(body.get("to", "")),
                    "kind": str(body.get("kind", "blocks")),
                }
                if relation not in current_roadmap["relations"]:
                    current_roadmap["relations"].append(relation)
                current_roadmap["revision"] += 1
                payload = json.dumps(current_roadmap).encode()
            elif path == "/api/roadmap/relation/delete":
                current_roadmap["relations"] = [
                    relation for relation in current_roadmap["relations"]
                    if not (relation["from"] == str(body.get("from", "")) and relation["to"] == str(body.get("to", "")) and relation["kind"] == str(body.get("kind", "blocks")))
                ]
                current_roadmap["revision"] += 1
                payload = json.dumps(current_roadmap).encode()
            elif path == "/api/roadmap/item/link-task":
                task_id = str(body.get("task_id", ""))
                current_roadmap["unscheduled_tasks"] = [
                    task for task in current_roadmap["unscheduled_tasks"]
                    if task["id"] != task_id
                ]
                current_roadmap["items"].append(
                    {
                        "id": f"task-{task_id}",
                        "kind": "task",
                        "task_id": task_id,
                        "lane": str(body.get("lane", "General")),
                        "order": int(body.get("order", 0)),
                        "schedule": {"target": str(body.get("target", ""))},
                        "task": {
                            "id": task_id,
                            "title": "Blocked task",
                            "status": "blocked",
                            "availability": "active",
                            "repositories": ["demo"],
                            "progress_percent": 0,
                        },
                    }
                )
                current_roadmap["revision"] += 1
                current_roadmap["statistics"]["items"] = len(current_roadmap["items"])
                current_roadmap["statistics"]["linked_tasks"] += 1
                current_roadmap["statistics"]["unscheduled_tasks"] = len(
                    current_roadmap["unscheduled_tasks"]
                )
                payload = json.dumps(current_roadmap).encode()
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        yield (url, current_snapshot) if expose_state else url
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def page():
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        yield page
        browser.close()


def test_project_home_renders_without_an_active_task(page):
    with dashboard_fixture_server(HOME_SNAPSHOT) as url:
        page.goto(url)
        page.locator("#homeContent").wait_for(state="visible")

        assert page.locator("#mainContent").is_hidden()
        assert page.locator("#projectsBreadcrumbBtn").inner_text() == "Projects"
        assert page.locator("#projectPicker").is_visible()
        assert page.locator("#projectGrid .project-card").count() == 1
        assert "demo" in page.locator("#projectGrid").inner_text()
        assert "Demo task" in page.locator("#projectGrid").inner_text()
        assert page.locator("#addProjectPanel").is_visible()
        page.locator("#addProjectPanel > summary").click()
        assert page.locator("#existingSourceOnboarding").is_visible()
        assert page.locator("#descriptorOnboarding").is_hidden()
        assert page.locator("#greenfieldOnboarding").is_hidden()
        page.locator("#greenfieldMode").click()
        assert page.locator("#existingSourceOnboarding").is_hidden()
        assert page.locator("#greenfieldOnboarding").is_visible()
        assert page.locator("#greenfieldTemplate option").count() == 1


def test_project_workbench_has_distinct_navigation_and_visible_tasks(page):
    with dashboard_fixture_server(HOME_SNAPSHOT) as url:
        page.goto(url)
        page.locator("#projectGrid .project-details").click()
        page.locator("#projectWorkspaceView").wait_for(state="visible")

        assert page.locator("#projectCatalogView").is_hidden()
        assert page.locator("#projectPicker").input_value() == "demo"
        assert page.locator("#taskPicker").input_value() == ""
        page.locator("#roadmapWorkspace").wait_for(state="visible")
        assert page.locator("#projectRoadmapView").is_visible()
        assert page.locator("#projectTasksView").is_hidden()
        assert page.locator("#projectRoadmapTab").get_attribute("aria-selected") == "true"
        page.locator("#projectTasksTab").click()
        assert page.locator("#projectTasksView").is_visible()
        assert page.locator("#createTaskBtn").is_enabled()
        assert not page.locator("#taskAdvancedOptions").get_attribute("open")
        assert "Demo task" in page.locator("#projectTaskNavigator").inner_text()
        assert "Demo task" in page.locator("#projectTaskList").inner_text()

        page.locator("#projectsBreadcrumbBtn").click()
        page.locator("#projectCatalogView").wait_for(state="visible")
        assert page.locator("#projectWorkspaceView").is_hidden()
        assert "Demo task" in page.locator("#projectGrid").inner_text()


def test_action_center_quick_open_and_keyboard_tabs(page):
    with dashboard_fixture_server() as url:
        page.goto(url)
        page.locator("#actionCenterTitle").wait_for()

        assert "WP1 is implement" in page.locator("#actionCenterTitle").inner_text()
        assert "codex · implement" in page.locator("#actionDetailsSummary").inner_text()
        assert page.locator("#workflowGraphView").is_visible()
        assert page.locator("#workflowList").is_hidden()
        assert page.locator("#supervisorPanel").is_hidden()
        page.wait_for_function("document.querySelector('#taskPicker').options.length === 3")
        assert page.locator("#taskPicker option").count() == 3

        page.locator("#runTab").focus()
        page.keyboard.press("ArrowRight")
        # Primary views are ordered Run, Agents, Plan, Changes.
        assert page.locator("#agentsTab").get_attribute("aria-selected") == "true"
        assert page.locator("#agentsView").is_visible()

        page.keyboard.press("ArrowRight")
        assert page.locator("#planTab").get_attribute("aria-selected") == "true"
        assert page.locator("#planView").is_visible()

        page.keyboard.press("ArrowRight")
        assert page.locator("#changesTab").get_attribute("aria-selected") == "true"
        assert page.locator("#changesView").is_visible()

        page.keyboard.press("Control+K")
        assert page.locator("#commandPalette").get_attribute("open") is not None
        assert page.locator("#commandList .command-item").count() >= 5


def test_completed_pipeline_supersedes_stale_gui_driver_failure(page):
    snapshot = copy.deepcopy(SNAPSHOT)
    snapshot["mode"] = "task"
    snapshot["orchestration"].update(
        {"state": "completed", "completed_packages": 1, "total_packages": 1}
    )
    snapshot["run"].update(
        {
            "owned_running": False,
            "external_running": False,
            "pid": None,
            "last_exit_code": 1,
            "last_exit_superseded": True,
            "last_exit_summary": "Pipeline finished: human_required",
        }
    )
    snapshot["run_control"] = {
        "can_start": False,
        "label": "Completed",
        "reason": "All work packages are complete.",
    }

    with dashboard_fixture_server(snapshot) as url:
        page.goto(url)
        page.locator("#driverState").wait_for()

        assert "prior driver exit superseded" in page.locator("#driverState").inner_text()
        message = page.locator("#runMessage").inner_text()
        assert "newer run completed" in message
        assert "Pipeline finished: human_required" not in message



def test_human_required_control_hold_is_not_rendered_as_driver_crash(page):
    snapshot = copy.deepcopy(SNAPSHOT)
    snapshot["mode"] = "task"
    snapshot["orchestration"].update({"state": "human_required"})
    snapshot["run"].update(
        {
            "owned_running": False,
            "external_running": False,
            "pid": None,
            "last_exit_code": 1,
            "last_exit_superseded": False,
            "last_exit_expected_control_hold": True,
            "last_exit_summary": "Pipeline finished: human_required",
        }
    )
    snapshot["run_control"] = {
        "can_start": False,
        "label": "Action required",
        "reason": "Run the real Gazebo/PX4 acceptance and retain durable evidence.",
        "human_action": {"reason": "external acceptance action required"},
    }

    with dashboard_fixture_server(snapshot) as url:
        page.goto(url)
        page.locator("#driverState").wait_for()

        assert "Action required" in page.locator("#driverState").inner_text()
        message = page.locator("#runMessage").inner_text()
        assert "real Gazebo/PX4 acceptance" in message
        assert "driver exited with code 1" not in message.lower()
        assert "bad" not in (page.locator("#runMessage").get_attribute("class") or "").split()


def test_polling_does_not_rebuild_routing_picker_or_lose_pending_choice(page):
    snapshot = copy.deepcopy(SNAPSHOT)
    snapshot["mode"] = "task"
    snapshot["run"].update({"owned_running": False, "external_running": False, "pid": None})
    snapshot["run_control"] = {"can_start": True, "label": "Start", "reason": ""}
    snapshot["execution_roles"] = [
        {
            "id": "implement",
            "label": "Implement",
            "capability": "implement",
            "read_only": False,
            "default_skills": [],
        }
    ]
    snapshot["agents"] = [
        {
            "id": "codex",
            "enabled": True,
            "adapter": "opencode",
            "runtime_id": "native-local",
            "runtime_kind": "native",
            "model_route_id": "codex-model",
            "target_id": "",
            "model": "test-model",
            "capabilities": ["implement"],
            "max_complexity": {"implement": 100},
            "assignments": [],
            "health": {"status": "available", "failures": 0},
            "timeouts": {},
            "action": {},
        },
        {
            "id": "qwen",
            "enabled": True,
            "adapter": "opencode",
            "runtime_id": "openclaw-local",
            "runtime_kind": "openclaw",
            "model_route_id": "qwen-route",
            "target_id": "gpu-1",
            "model": "qwen3-coder:30b-32k",
            "capabilities": ["implement"],
            "max_complexity": {"implement": 100},
            "assignments": [],
            "health": {"status": "available", "failures": 0},
            "timeouts": {},
            "action": {},
        },
    ]
    snapshot["execution_lanes"] = [
        {
            "id": "lane-native-codex",
            "display_name": "Codex local",
            "runtime_id": "native-local",
            "runtime_kind": "native",
            "model_route_id": "codex-model",
            "model_display_name": "test-model",
            "target_id": None,
            "target_display_name": "",
            "roles": ["implement"],
            "profile_ids": ["codex"],
            "health": "healthy",
            "availability": "ready",
            "active_assignments": [],
            "diagnostics_summary": None,
        },
        {
            "id": "lane-qwen-gpu",
            "display_name": "Qwen GPU #1",
            "runtime_id": "openclaw-local",
            "runtime_kind": "openclaw",
            "model_route_id": "qwen-route",
            "model_display_name": "qwen3-coder:30b-32k",
            "target_id": "gpu-1",
            "target_display_name": "gpu-1",
            "roles": ["implement"],
            "profile_ids": ["qwen"],
            "health": "healthy",
            "availability": "ready",
            "active_assignments": [],
            "diagnostics_summary": None,
        },
    ]
    snapshot["skills"] = []
    snapshot["packages"][0].update(
        {
            "agent_preferences": {},
            "skill_preferences": {},
            "acceptance_criteria": [],
            "requirements": [],
            "review_findings": [],
        }
    )

    with dashboard_fixture_server(snapshot) as url:
        page.goto(url)
        # The Graph card Execution shortcut opens the inspector routing editor
        # (the old dedicated assignment action no longer exists).
        page.locator(
            '#workflowGraphView [data-work-package-action="execution"][data-id="WP1"]'
        ).click()
        picker = page.locator("[data-lane-select]")
        picker.wait_for(state="visible")
        # The draft starts in Automatic routing, which disables the lane pick;
        # choose Prefer so an explicit lane becomes an editable pending choice.
        page.locator("[data-lane-mode]").select_option("prefer")
        picker.wait_for(state="visible")
        page.evaluate(
            "window.__routingPicker = document.querySelector('[data-lane-select]')"
        )
        picker.select_option("lane-qwen-gpu")

        # Cross a full dashboard polling interval. Active form interaction must
        # preserve the contextual lane draft and the actual select node.
        page.wait_for_timeout(3400)

        assert picker.input_value() == "lane-qwen-gpu"
        assert page.evaluate(
            "window.__routingPicker === document.querySelector('[data-lane-select]')"
        )


def test_polling_keeps_context_picker_options_stable_while_focused(page):
    snapshot = copy.deepcopy(SNAPSHOT)
    snapshot["mode"] = "task"

    with dashboard_fixture_server(snapshot) as url:
        page.goto(url)
        page.wait_for_function("document.querySelector('#taskPicker').options.length === 3")
        picker = page.locator("#taskPicker")
        picker.focus()
        page.evaluate(
            "window.__taskOption = document.querySelector('#taskPicker').options[1]"
        )

        page.wait_for_timeout(3400)

        assert page.evaluate(
            "window.__taskOption === document.querySelector('#taskPicker').options[1]"
        )
        assert picker.input_value() == "demo-task"


def test_supervisor_radio_selection_survives_dashboard_polling(page):
    snapshot = copy.deepcopy(SNAPSHOT)
    snapshot["mode"] = "task"
    snapshot["orchestration"]["state"] = "waiting_for_human_decision"
    snapshot["supervisor"]["incident"] = {
        "incident_id": "incident-1",
        "package_id": "WP1",
        "status": "waiting_for_human",
        "human_question": {
            "question": "Choose recovery path",
            "context": "Browser regression fixture",
            "recommended_option": "retry",
            "options": [
                {"id": "retry", "label": "Retry", "consequence": "Retry now"},
                {"id": "pause", "label": "Pause", "consequence": "Wait for operator"},
            ],
        },
    }

    with dashboard_fixture_server(snapshot) as url:
        page.goto(url)
        pause = page.locator('input[name="supervisorOption"][value="pause"]')
        pause.wait_for(state="attached")
        pause.check()

        page.wait_for_timeout(3400)

        assert pause.is_checked()


def test_action_center_lists_parallel_live_agents(page):
    snapshot = json.loads(json.dumps(SNAPSHOT))
    snapshot["packages"].append(
        {
            "id": "WP2",
            "title": "Parallel final review",
            "stage": "final_review",
            "status": "running",
            "dependencies": [],
            "affected_repositories": ["repo"],
            "computed_complexity": 20,
            "risk": "medium",
            "execution_mode": "review_shard",
            "parallel_safe": True,
            "parent_id": "WP1",
            "shard_ids": [],
        }
    )
    snapshot["assignments"].append(
        {
            "package_id": "WP2",
            "package_title": "Parallel final review",
            "stage": "final_review",
            "agent_id": "reviewer",
            "parallel": True,
        }
    )
    snapshot["execution_contexts"] = [
        {
            "package_id": "WP1",
            "package_title": "Workbench shell",
            "stage": "implement",
            "agent_id": "codex",
            "status": "running",
            "source": "invocation",
            "kind": "agent",
            "parallel": True,
            "invocation_id": "inv-1",
            "started_at": "2026-07-29T12:00:00+00:00",
        },
        {
            "package_id": "WP2",
            "package_title": "Parallel final review",
            "stage": "final_review",
            "agent_id": "reviewer",
            "status": "running",
            "source": "invocation",
            "kind": "agent",
            "parallel": True,
            "invocation_id": "inv-2",
            "started_at": "2026-07-29T12:01:00+00:00",
        },
    ]
    snapshot["execution_context"] = {
        key: snapshot["execution_contexts"][0][key]
        for key in (
            "package_id",
            "stage",
            "agent_id",
            "status",
            "source",
            "invocation_id",
            "started_at",
        )
    }

    with dashboard_fixture_server(snapshot) as url:
        page.goto(url)
        page.locator("#actionCenterTitle").wait_for()

        assert page.locator("#actionCenterTitle").inner_text() == (
            "2 agents are running in parallel"
        )
        page.locator("#actionDetails > summary").click()
        assert page.locator("#actionAgent").inner_text() == (
            "2 running · implement, final review"
        )
        rows = page.locator("#executionContexts .execution-context-row")
        assert rows.count() == 2
        assert "WP1" in rows.nth(0).inner_text()
        assert "codex" in rows.nth(0).inner_text()
        assert "WP2" in rows.nth(1).inner_text()
        assert "reviewer" in rows.nth(1).inner_text()
        assert page.locator("#openCurrentAgent").inner_text() == (
            "Open primary agent"
        )


def test_diagnostics_explain_tokens_and_logs_show_orchestrator_state_machine(page):
    snapshot = json.loads(json.dumps(SNAPSHOT))
    snapshot["token_usage"] = {
        "totals": {
            "invocations": 328,
            "input_tokens": 375_619,
            "estimated_input_tokens": 512_009,
            "output_tokens": 188_235,
            "cache_read_tokens": 22_716_301,
            "reasoning_tokens": 9_585,
            "total_tokens": 24_885_741,
        },
        "cache": {"status": "reported", "hit_rate": 0.9457},
        "by_status": {"failed": {"total_tokens": 13_682}},
    }

    with dashboard_fixture_server(snapshot) as url:
        page.goto(url)
        page.locator("#executionHealthDrawerOpen").click()
        page.locator("#executionHealthDrawer").wait_for()
        # Token/capacity metrics live behind the collapsed metrics disclosure.
        page.locator(".execution-health-metrics > summary").click()
        page.wait_for_timeout(100)

        assert page.locator("#mTokens").inner_text() == "24.9M processed"
        breakdown = page.locator("#mTokensBreakdown").inner_text()
        assert "375.6K reported input" in breakdown
        assert "188.2K output" in breakdown
        assert "22.7M cache reads" in breakdown
        assert "9,585 reasoning" in breakdown

        # The drawer overlays the workbench; close it before using the More menu.
        page.locator("#executionHealthDrawerClose").click()
        page.locator("#executionHealthDrawer").wait_for(state="hidden")
        page.locator("#taskMoreMenu summary").click()
        page.locator('[data-utility-view="logs"]').click()
        page.locator("#orchestratorStateMachine").wait_for()
        assert page.locator("#orchestratorMachineState").inner_text() == (
            "waiting for agent"
        )
        active = page.locator(".machine-state.active .machine-copy strong")
        assert active.count() == 2
        assert active.nth(0).inner_text() == "WAIT OR RECOVER"
        assert active.nth(1).inner_text() == "IMPLEMENT"
        assert "WP1 · implement · codex" in page.locator(
            ".machine-current-context"
        ).inner_text()



def test_execution_health_is_compact_lane_first_and_profiles_are_advanced(page):
    snapshot = json.loads(json.dumps(SNAPSHOT))
    snapshot["agents"] = [
        {
            "id": "codex",
            "enabled": True,
            "runtime_kind": "native",
            "runtime_id": "native",
            "model": "GPT",
            "target_id": "local",
            "health": {"status": "available"},
            "assignments": snapshot["assignments"],
            "capabilities": ["implement"],
            "max_complexity": {"implement": 80},
            "effective_max_complexity": {"implement": 80},
            "timeouts": {},
            "native_maintenance": True,
        }
    ]
    snapshot["nodes"] = [
        {
            "id": "local",
            "name": "Local",
            "agents": ["codex"],
            "reachable": True,
            "loaded_models_supported": False,
        }
    ]
    snapshot["execution_lanes"] = [
        {
            "id": "lane-native",
            "display_name": "GPT local",
            "runtime_id": "native",
            "runtime_kind": "native",
            "model_display_name": "GPT",
            "target_id": "local",
            "target_display_name": "Local",
            "roles": ["implement", "review"],
            "profile_ids": ["codex"],
            "health": "available",
            "availability": "ready",
            "active_assignments": [],
        }
    ]

    with dashboard_fixture_server(snapshot) as url:
        page.goto(url)
        strip = page.locator("#diagnosticsStrip")
        assert strip.inner_text().startswith("Execution health")
        assert "Execution healthy" in strip.inner_text()
        assert page.locator("#executionHealthDrawer").is_hidden()
        assert page.locator("#assignmentRows").count() == 0

        opener = page.locator("#executionHealthDrawerOpen")
        opener.click()
        page.locator("#executionHealthDrawer").wait_for()
        # Group titles are rendered uppercase by the drawer stylesheet.
        assert page.locator("#executionHealthActiveTitle").inner_text() == "ACTIVE"
        assert "WP1" in page.locator("#executionHealthOverview").inner_text()
        assert "GPT local" in page.locator("#executionHealthOverview").inner_text()
        assert page.locator("#executionProfileMaintenance").get_attribute("open") is None
        assert page.locator("#agentList").inner_text() == ""

        page.locator("#executionProfileMaintenance > summary").click()
        # The details toggle event renders the profile list asynchronously.
        page.wait_for_function(
            "() => (document.querySelector('#agentList')?.innerText || '').includes('codex')"
        )
        assert "Doctor test" in page.locator("#agentList").inner_text()
        assert "Doctor test" in page.locator("#agentList").inner_text()


def test_execution_health_drawer_escape_restores_focus_without_document_scroll(page):
    with dashboard_fixture_server() as url:
        page.goto(url)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        before = page.evaluate("window.scrollY")
        opener = page.locator("#executionHealthDrawerOpen")
        opener.click()
        assert page.locator("#executionHealthDrawerClose").evaluate(
            "node => document.activeElement === node"
        ) is True
        page.keyboard.press("Escape")
        assert page.locator("#executionHealthDrawer").is_hidden()
        assert opener.evaluate("node => document.activeElement === node") is True
        assert page.evaluate("window.scrollY") == before


def test_execution_health_drawer_uses_full_width_on_narrow_viewport(page):
    page.set_viewport_size({"width": 600, "height": 800})
    with dashboard_fixture_server() as url:
        page.goto(url)
        page.locator("#executionHealthDrawerOpen").click()
        box = page.locator("#executionHealthDrawer").bounding_box()
        assert box is not None
        assert box["x"] == pytest.approx(0, abs=1)
        assert box["width"] == pytest.approx(600, abs=2)


def test_agent_console_keyboard_navigation_skips_unavailable_views(page):
    with dashboard_fixture_server() as url:
        page.goto(url)
        page.locator("#openCurrentAgent").click()
        activity = page.locator('[data-console-view="activity"]')
        page.wait_for_function(
            "document.querySelector('[data-console-view=conversation]').disabled"
        )
        activity.focus()
        page.keyboard.press("ArrowLeft")

        assert activity.get_attribute("aria-selected") == "true"
        assert activity.evaluate("node => document.activeElement === node") is True
        assert page.locator('[data-console-view="result"]').is_hidden()


def test_action_center_shows_prepare_stage_without_an_assigned_agent(page):
    snapshot = json.loads(json.dumps(SNAPSHOT))
    snapshot["orchestration"]["state"] = "running"
    snapshot["packages"][0].update(
        {"stage": "prepare", "status": "pending", "agent_id": ""}
    )
    snapshot["assignments"] = []
    snapshot["execution_context"] = {
        "package_id": "WP1",
        "stage": "prepare",
        "agent_id": "",
        "status": "pending",
        "source": "package",
    }

    with dashboard_fixture_server(snapshot) as url:
        page.goto(url)
        page.locator("#actionCenterTitle").wait_for()

        assert page.locator("#actionCenterTitle").inner_text() == "WP1 is prepare"
        page.locator("#actionDetails > summary").click()
        assert page.locator("#actionAgent").inner_text() == "Unassigned / prepare"
        assert page.locator("#openCurrentAgent").is_disabled()


def test_workflow_supports_pointer_zoom_toolbar_and_drag_pan(page):
    with dashboard_fixture_server() as url:
        page.goto(url)
        page.locator('[data-workflow-view="graph"]').click()
        viewport = page.locator("#workflowWrap")
        viewport.wait_for()
        page.evaluate(
            "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
        )
        initial_zoom = page.locator("#workflowZoom").inner_text()

        viewport.hover()
        page.mouse.wheel(0, -160)
        assert page.locator("#workflowZoom").inner_text() == initial_zoom

        page.keyboard.down("Control")
        page.mouse.wheel(0, -160)
        page.keyboard.up("Control")
        page.wait_for_function(
            "initial => document.querySelector('#workflowZoom').textContent !== initial",
            arg=initial_zoom,
        )
        canvas_style = page.locator("#workflowCanvas").get_attribute("style") or ""
        assert "scale(" in canvas_style

        page.locator('[data-workflow-viewport-action="reset"]').click()
        assert page.locator("#workflowZoom").inner_text() == "100%"

        for _ in range(4):
            page.locator('[data-workflow-viewport-action="zoom-in"]').click()
        page.wait_for_timeout(50)
        page.evaluate("document.querySelector('#workflowWrap').scrollLeft = 180")
        before = page.evaluate("document.querySelector('#workflowWrap').scrollLeft")
        start = viewport.evaluate(
            """node => {
                const rect = node.getBoundingClientRect();
                const right = Math.min(rect.right, window.innerWidth) - 24;
                const bottom = Math.min(rect.bottom, window.innerHeight) - 24;
                for (let y = rect.top + 24; y <= bottom; y += 24) {
                    for (let x = rect.left + 24; x <= right; x += 24) {
                        const target = document.elementFromPoint(x, y);
                        if (target && node.contains(target) &&
                            !target.closest('button, a, input, select, textarea, summary')) {
                            return {x, y};
                        }
                    }
                }
                return null;
            }"""
        )
        assert start is not None
        start_x = start["x"]
        start_y = start["y"]
        page.mouse.move(start_x, start_y)
        page.mouse.down()
        page.mouse.move(start_x + 80, start_y, steps=4)
        page.mouse.up()
        after = page.evaluate("document.querySelector('#workflowWrap').scrollLeft")
        assert after < before


def test_agent_workbench_uses_focused_views_and_advanced_tools(page, tmp_path):
    with dashboard_fixture_server(console_payload=SEMANTIC_CONSOLE_PAYLOAD) as url:
        page.goto(url)
        page.locator("#openCurrentAgent").click()
        dock = page.locator("#agentConsoleModal")

        assert dock.is_visible()
        assert "workbench-open" in (page.locator("body").get_attribute("class") or "")
        assert page.locator('.console-primary-tabs [data-console-view="conversation"]').is_visible()
        assert page.locator('.console-primary-tabs [data-console-view="changes"]').is_visible()
        assert page.locator('#agentAdvancedControls').is_visible()
        assert page.locator('#agentAdvancedControls [data-console-view="terminal"]').is_hidden()

        page.locator('#agentAdvancedControls summary').click()
        assert page.locator('#agentConsoleSession').is_visible()
        assert page.locator('#agentAdvancedControls [data-console-view="terminal"]').is_visible()

        page.locator("#maximizeAgentConsole").click()
        assert "maximized" in (dock.get_attribute("class") or "")

        screenshot = tmp_path / "workbench-shell-1440.png"
        page.screenshot(path=screenshot, full_page=True)
        assert screenshot.stat().st_size > 10_000


def test_dense_workflow_uses_shared_buses_straight_routes_and_local_focus(page):
    page.set_viewport_size({"width": 700, "height": 620})
    with dashboard_fixture_server(DENSE_WORKFLOW_SNAPSHOT) as url:
        page.goto(url)
        # Graph is the first-use default; loading it must not scroll the page.
        page.locator("#workflowWrap").wait_for()
        page.wait_for_timeout(500)

        assert page.evaluate("window.scrollY") == 0
        assert page.locator(".workflow-edge.bundle-bus").count() == 2
        assert page.locator(".workflow-edge.bundle-trunk").count() == 2

        geometry = page.evaluate(
            """() => [...document.querySelectorAll('.workflow-edge')].map((path) => ({
              d: path.getAttribute('d'),
              marker: path.hasAttribute('marker-end'),
              kind: path.getAttribute('class'),
            }))"""
        )
        assert geometry
        assert all(" L " in item["d"] for item in geometry)
        assert all(
            len(item["d"].split(" L ")) == len(set(item["d"].split(" L ")))
            for item in geometry
        )
        straight = [
            item for item in geometry
            if "bundle" not in item["kind"] and item["marker"]
        ]
        assert len(straight) == 1
        assert straight[0]["d"].count(" L ") == 1




def _workflow_viewport_position(page):
    return page.evaluate(
        """() => {
          const node = document.querySelector('#workflowWrap');
          return {left: node.scrollLeft, top: node.scrollTop, pageY: window.scrollY};
        }"""
    )


def _reveal_work_package_card(page, package_id):
    """Center a Work Package card in the document and graph scroll views.

    Playwright and native focus handling both auto-scroll for clipped targets;
    centering the card first keeps any later scroll movement attributable to
    the application rather than to synthesized click mechanics.
    """
    page.evaluate(
        """(packageId) => {
          const card = document.querySelector(
            `[data-work-package-action="select"][data-id="${packageId}"]`,
          );
          card.scrollIntoView({block: "center", inline: "center", behavior: "auto"});
        }""",
        package_id,
    )


def test_selecting_work_package_does_not_recenter_workflow_viewport(page):
    page.set_viewport_size({"width": 700, "height": 620})
    with dashboard_fixture_server(DENSE_WORKFLOW_SNAPSHOT) as url:
        page.goto(url)
        page.locator("#workflowWrap").wait_for()
        page.wait_for_timeout(350)
        for _ in range(3):
            page.locator('[data-workflow-viewport-action="zoom-in"]').click()
        page.evaluate(
            """() => {
              const node = document.querySelector('#workflowWrap');
              node.scrollTo({left: 40, top: 25, behavior: 'auto'});
            }"""
        )
        _reveal_work_package_card(page, "F")
        before = _workflow_viewport_position(page)

        page.locator('[data-work-package-action="select"][data-id="F"]').click()
        page.wait_for_timeout(250)
        after = _workflow_viewport_position(page)

        assert after == before


def test_dashboard_refresh_preserves_selected_work_package_viewport(page):
    page.set_viewport_size({"width": 700, "height": 620})
    with dashboard_fixture_server(DENSE_WORKFLOW_SNAPSHOT) as url:
        page.goto(url)
        page.locator("#workflowWrap").wait_for()
        page.locator('[data-work-package-action="select"][data-id="F"]').click()
        page.locator("#workPackageInspector").wait_for(state="visible")
        # Selection opens the overlay inspector, which covers the viewport
        # toolbar on narrow screens. Close it before exercising zoom.
        page.locator("#closeWorkPackageInspector").click()
        page.locator("#workPackageInspector").wait_for(state="hidden")
        page.evaluate("window.scrollTo(0, 0)")
        for _ in range(3):
            page.locator('[data-workflow-viewport-action="zoom-in"]').click()
        page.evaluate(
            """() => {
              const node = document.querySelector('#workflowWrap');
              node.scrollTo({left: 35, top: 20, behavior: 'auto'});
            }"""
        )
        before = _workflow_viewport_position(page)
        zoom_before = page.locator("#workflowZoom").inner_text()

        page.wait_for_timeout(3400)
        after = _workflow_viewport_position(page)

        assert after == before
        assert page.locator("#workflowZoom").inner_text() == zoom_before


def test_work_package_inspector_close_preserves_viewport_and_focuses_workbench(page):
    page.set_viewport_size({"width": 700, "height": 620})
    with dashboard_fixture_server(DENSE_WORKFLOW_SNAPSHOT) as url:
        page.goto(url)
        page.locator("#workflowWrap").wait_for()
        for _ in range(3):
            page.locator('[data-workflow-viewport-action="zoom-in"]').click()
        page.evaluate(
            """() => {
              const node = document.querySelector('#workflowWrap');
              node.scrollTo({left: 45, top: 20, behavior: 'auto'});
            }"""
        )
        _reveal_work_package_card(page, "F")
        before = _workflow_viewport_position(page)

        page.locator('[data-work-package-action="select"][data-id="F"]').click()
        page.locator("#workPackageInspector").wait_for(state="visible")
        assert _workflow_viewport_position(page) == before
        assert page.evaluate("document.activeElement?.id") == "workPackageInspectorTitle"

        page.locator("#closeWorkPackageInspector").click()
        page.locator("#workPackageInspector").wait_for(state="hidden")
        page.wait_for_timeout(100)
        assert _workflow_viewport_position(page) == before
        assert page.evaluate("document.activeElement?.id") == "workflowWrap"


def test_graph_is_first_use_default_and_list_preference_persists(page):
    with dashboard_fixture_server(DENSE_WORKFLOW_SNAPSHOT) as url:
        page.goto(url)
        page.locator("#workflowGraphView").wait_for(state="visible")

        assert page.locator('[data-workflow-view="graph"]').get_attribute("aria-pressed") == "true"
        assert page.locator("#workflowList").is_hidden()

        page.locator('[data-workflow-view="list"]').click()
        page.locator("#workflowList").wait_for(state="visible")
        assert page.locator('[data-workflow-view="list"]').get_attribute("aria-pressed") == "true"

        page.reload()
        page.locator("#workflowList").wait_for(state="visible")
        assert page.locator('[data-workflow-view="list"]').get_attribute("aria-pressed") == "true"
        assert page.locator("#workflowGraphView").is_hidden()


def test_follow_active_navigates_once_per_primary_transition(page):
    snapshot = copy.deepcopy(DENSE_WORKFLOW_SNAPSHOT)
    snapshot["packages"].append(
        {
            "id": "G",
            "title": "Next active Work Package",
            "stage": "prepare",
            "status": "pending",
            "dependencies": ["F"],
            "affected_repositories": ["repo"],
            "computed_complexity": 10,
            "risk": "low",
            "execution_mode": "standard",
            "parallel_safe": False,
            "parent_id": "",
            "shard_ids": [],
        }
    )

    with dashboard_fixture_server(snapshot, expose_state=True) as fixture:
        url, live_snapshot = fixture
        page.goto(url)
        page.locator("#workflowWrap").wait_for()
        for _ in range(4):
            page.locator('[data-workflow-viewport-action="zoom-in"]').click()
        page.evaluate(
            """() => {
              const node = document.querySelector('#workflowWrap');
              node.scrollLeft = 0;
              window.__workflowLocateCount = 0;
              const original = node.scrollTo.bind(node);
              node.scrollTo = (...args) => {
                window.__workflowLocateCount += 1;
                return original(...args);
              };
            }"""
        )

        page.locator("#workflowFollowActive").click()
        assert page.locator("#workflowFollowActive").get_attribute("aria-pressed") == "true"
        assert page.evaluate("window.__workflowLocateCount") == 0

        for package in live_snapshot["packages"]:
            if package["id"] == "F":
                package.update({"stage": "completed", "status": "completed"})
            elif package["id"] == "G":
                package.update({"stage": "implement", "status": "running"})
        live_snapshot["assignments"] = [
            {
                "package_id": "G",
                "package_title": "Next active Work Package",
                "stage": "implement",
                "agent_id": "codex",
                "parallel": False,
            }
        ]

        page.wait_for_timeout(3700)
        assert page.evaluate("window.__workflowLocateCount") == 1
        first_position = page.evaluate("document.querySelector('#workflowWrap').scrollLeft")
        assert first_position > 0

        page.wait_for_timeout(3200)
        assert page.evaluate("window.__workflowLocateCount") == 1


def _workflow_visible_point(page):
    """Return a viewport coordinate that lies over the graph surface."""
    return page.evaluate(
        """() => {
          const wrap = document.querySelector('#workflowWrap');
          const topbar = document.querySelector('header.topbar');
          const rect = wrap.getBoundingClientRect();
          const topLimit = topbar.getBoundingClientRect().bottom + 20;
          const bottomLimit = window.innerHeight - 20;
          if (rect.top >= bottomLimit || rect.bottom <= topLimit) return null;
          const y = Math.min(Math.max(rect.top + 40, topLimit), bottomLimit);
          const x = rect.left + Math.min(rect.width / 2, 120);
          return {x, y};
        }"""
    )


def test_graph_vertical_wheel_scrolls_page_and_shift_wheel_pans_graph(page):
    page.set_viewport_size({"width": 700, "height": 620})
    with dashboard_fixture_server(DENSE_WORKFLOW_SNAPSHOT) as url:
        page.goto(url)
        viewport = page.locator("#workflowWrap")
        viewport.wait_for()
        page.evaluate(
            """() => {
              const wrap = document.querySelector('#workflowWrap');
              const top = wrap.getBoundingClientRect().top + window.scrollY;
              window.scrollTo(0, Math.max(0, top - 200));
            }"""
        )
        page.wait_for_timeout(100)

        point = _workflow_visible_point(page)
        assert point is not None
        page.mouse.move(point["x"], point["y"])
        before_page = page.evaluate("window.scrollY")
        before_top = page.evaluate("document.querySelector('#workflowWrap').scrollTop")
        page.mouse.wheel(0, 260)
        page.wait_for_timeout(100)
        assert page.evaluate("window.scrollY") > before_page
        assert page.evaluate("document.querySelector('#workflowWrap').scrollTop") == before_top

        for _ in range(4):
            page.locator('[data-workflow-viewport-action="zoom-in"]').click()
        page.evaluate(
            """() => {
              const wrap = document.querySelector('#workflowWrap');
              const top = wrap.getBoundingClientRect().top + window.scrollY;
              window.scrollTo(0, Math.max(0, top - 200));
              wrap.scrollLeft = 40;
            }"""
        )
        before_left = page.evaluate("document.querySelector('#workflowWrap').scrollLeft")
        before_page = page.evaluate("window.scrollY")
        point = _workflow_visible_point(page)
        assert point is not None
        page.mouse.move(point["x"], point["y"])
        page.keyboard.down("Shift")
        page.mouse.wheel(0, 180)
        page.keyboard.up("Shift")
        page.wait_for_timeout(100)

        assert page.evaluate("document.querySelector('#workflowWrap').scrollLeft") > before_left
        assert page.evaluate("window.scrollY") == before_page

def test_work_package_details_and_execution_shortcut_share_inspector(page):
    snapshot = copy.deepcopy(SNAPSHOT)
    snapshot["mode"] = "task"

    with dashboard_fixture_server(snapshot) as url:
        page.goto(url)
        page.locator('[data-workflow-view="list"]').click()
        details = page.locator('[data-work-package-action="details"][data-id="WP1"]')
        details.click()
        page.locator("#workPackageInspector").wait_for(state="visible")
        assert page.locator("#workPackageOverviewTab").get_attribute("aria-selected") == "true"
        assert page.locator("#workPackageOverviewPanel").is_visible()

        page.locator("#closeWorkPackageInspector").click()
        page.locator("#workPackageInspector").wait_for(state="hidden")

        page.locator('[data-workflow-view="graph"]').click()
        # The same Execution action exists in the hidden List projection;
        # scope to the Graph card that is actually visible.
        assignment = page.locator(
            '#workflowGraphView [data-work-package-action="execution"][data-id="WP1"]'
        )
        assignment.click()
        page.locator("#workPackageInspector").wait_for(state="visible")
        assert page.locator("#workPackageExecutionTab").get_attribute("aria-selected") == "true"
        assert page.locator("#workPackageExecutionPanel").is_visible()
        assert page.locator("[data-lane-role]").is_visible()
        assert page.locator("[data-lane-mode]").is_visible()


def test_agents_tab_presents_task_workforce_separately_from_execution_health(page):
    snapshot = copy.deepcopy(SNAPSHOT)
    snapshot["mode"] = "task"
    snapshot["run"]["owned_running"] = False
    snapshot["agents"] = [
        {
            "id": "codex",
            "name": "Implementer",
            "enabled": True,
            "runtime_kind": "native",
            "model": "gpt-local",
            "target_id": "local",
            "capabilities": ["implementation"],
            "max_complexity": {"implementation": 80},
            "health": {"status": "available", "available": True, "failures": 0},
            "assignments": snapshot["assignments"],
            "promotions": [],
            "action": {},
            "native_maintenance": True,
        },
        {
            "id": "reviewer",
            "name": "Reviewer",
            "enabled": True,
            "runtime_kind": "native",
            "model": "gpt-local",
            "target_id": "local",
            "capabilities": ["review"],
            "max_complexity": {"review": 100},
            "health": {"status": "cooldown", "available": False, "reason": "adapter timeout", "failures": 2},
            "assignments": [],
            "promotions": [],
            "action": {},
            "native_maintenance": True,
        },
    ]

    with dashboard_fixture_server(snapshot) as url:
        page.goto(url)
        page.locator("#agentsTab").click()
        page.locator("#agentsView").wait_for(state="visible")
        assert "Working" in page.locator("#agentWorkforceGroups").inner_text()
        assert "Implementer" in page.locator("#agentWorkforceGroups").inner_text()
        assert "Workbench shell" in page.locator("#agentWorkforceGroups").inner_text()
        assert "Needs attention" in page.locator("#agentWorkforceGroups").inner_text()
        assert "adapter timeout" in page.locator("#agentWorkforceGroups").inner_text()
        assert page.locator('[data-worker-action="doctor"][data-agent="reviewer"]').is_enabled()
        assert page.locator('[data-worker-action="promote"][data-agent="reviewer"]').is_disabled()
        assert page.locator("#executionHealthDrawer").is_hidden()


def test_work_package_inspector_tab_and_scroll_survive_snapshot_refresh(page):
    snapshot = copy.deepcopy(SNAPSHOT)
    snapshot["mode"] = "task"
    snapshot["packages"][0]["implementation_summary"] = "\n".join(
        f"Implementation evidence line {index}" for index in range(80)
    )
    snapshot["packages"][0]["review_findings"] = [
        f"Review finding {index}" for index in range(30)
    ]

    with dashboard_fixture_server(snapshot, expose_state=True) as fixture:
        url, live_snapshot = fixture
        page.goto(url)
        page.locator('[data-work-package-action="select"][data-id="WP1"]').click()
        page.locator("#workPackageInspector").wait_for(state="visible")
        page.locator("#workPackageEvidenceTab").click()
        page.locator("#workPackageEvidencePanel").wait_for(state="visible")
        page.evaluate("document.querySelector('#packageDetail').scrollTop = 180")
        before = page.evaluate("document.querySelector('#packageDetail').scrollTop")
        assert before > 0

        live_snapshot["packages"][0]["risk"] = "high"
        page.wait_for_timeout(3400)

        assert page.locator("#workPackageEvidenceTab").get_attribute("aria-selected") == "true"
        assert page.locator("#workPackageEvidencePanel").is_visible()
        assert abs(page.evaluate("document.querySelector('#packageDetail').scrollTop") - before) <= 2


def test_work_package_render_failure_is_visible_in_open_inspector(page):
    snapshot = copy.deepcopy(SNAPSHOT)
    snapshot["mode"] = "task"
    # Deliberately malformed UI payload: the inspector must still open before
    # the rich renderer reaches its error boundary.
    snapshot["packages"][0]["acceptance_criteria"] = [None]

    with dashboard_fixture_server(snapshot) as url:
        page.goto(url)
        page.locator('[data-work-package-action="select"][data-id="WP1"]').click()
        page.locator("#workPackageInspector").wait_for(state="visible")
        message = page.locator("#packageDetail .message.bad")
        message.wait_for(state="visible")
        assert "could not be rendered" in message.inner_text().lower()


def test_work_package_inspector_becomes_full_width_sheet_on_narrow_viewport(page):
    snapshot = copy.deepcopy(SNAPSHOT)
    snapshot["mode"] = "task"
    page.set_viewport_size({"width": 540, "height": 720})

    with dashboard_fixture_server(snapshot) as url:
        page.goto(url)
        page.locator('[data-work-package-action="select"][data-id="WP1"]').click()
        inspector = page.locator("#workPackageInspector")
        inspector.wait_for(state="visible")
        box = inspector.bounding_box()
        assert box is not None
        assert abs(box["x"]) <= 1
        assert abs(box["width"] - 540) <= 1


def test_open_inspector_handles_selected_work_package_removed_on_refresh(page):
    snapshot = copy.deepcopy(SNAPSHOT)
    snapshot["mode"] = "task"

    with dashboard_fixture_server(snapshot, expose_state=True) as fixture:
        url, live_snapshot = fixture
        page.goto(url)
        page.locator('[data-work-package-action="select"][data-id="WP1"]').click()
        inspector = page.locator("#workPackageInspector")
        inspector.wait_for(state="visible")

        live_snapshot["packages"][:] = [
            package for package in live_snapshot["packages"] if package["id"] != "WP1"
        ]
        page.wait_for_timeout(3400)

        assert inspector.is_visible()
        assert "no longer present" in page.locator("#workPackageInspectorMeta").inner_text().lower()
        assert "no longer present" in page.locator("#packageDetail").inner_text().lower()


def test_project_roadmap_is_interactive_and_promotes_planned_work(page):
    with dashboard_fixture_server(HOME_SNAPSHOT) as url:
        page.goto(url)
        page.locator("#projectGrid .project-details").click()
        page.locator("#projectWorkspaceView").wait_for(state="visible")
        page.locator("#roadmapWorkspace").wait_for(state="visible")

        assert page.locator("#roadmapTitle").inner_text() == "Platform roadmap"
        assert page.locator("[data-roadmap-row='task-demo']").is_visible()
        assert page.locator("#roadmapUnscheduledCount").inner_text() == "1"

        # Single click selects without leaving the planning surface. Opening a
        # canonical Task is deliberate: double-click or Enter. Returning from
        # the Task restores the Roadmap view state.
        page.locator("[data-roadmap-primary='task-demo']").click()
        assert page.locator("#projectRoadmapView").is_visible()
        assert "Demo task" in page.locator("#roadmapInspector").inner_text()
        page.locator("[data-roadmap-item='task-demo']").dblclick()
        page.locator("#mainContent").wait_for(state="visible")
        assert page.locator("#taskRoadmapBackBtn").inner_text() == "← demo roadmap"
        page.locator("#taskRoadmapBackBtn").click()
        page.locator("#roadmapWorkspace").wait_for(state="visible")
        assert page.locator("#projectRoadmapView").is_visible()

        page.locator("#roadmapPaletteBtn").click()
        page.locator("[data-roadmap-create-kind='planned_task']").click()
        track = page.locator("[data-roadmap-row='task-demo'] .roadmap-row-track")
        box = track.bounding_box()
        assert box is not None
        placement = page.evaluate(
            """() => {
              const row = document.querySelector("[data-roadmap-row='task-demo']");
              const track = row.querySelector('.roadmap-row-track').getBoundingClientRect();
              const bars = [...row.querySelectorAll('.roadmap-item-bar')]
                .map((bar) => bar.getBoundingClientRect())
                .sort((left, right) => left.x - right.x);
              let x = null;
              for (let index = 0; index < bars.length - 1; index += 1) {
                const gap = bars[index + 1].x - (bars[index].x + bars[index].width);
                if (gap >= 24) {
                  x = bars[index].x + bars[index].width + Math.min(gap / 2, 80);
                  break;
                }
              }
              if (x === null && bars.length && track.right - (bars.at(-1).x + bars.at(-1).width) >= 24) {
                x = bars.at(-1).x + bars.at(-1).width + 40;
              }
              if (x === null) x = track.x + 40;
              return {x, y: track.y + track.height / 2};
            }"""
        )
        page.mouse.click(placement["x"], placement["y"])
        rename = page.locator(".roadmap-inline-rename")
        rename.wait_for(state="visible")
        rename.fill("Field beta")
        rename.press("Enter")
        page.get_by_text("Field beta", exact=True).first.wait_for(state="visible")

        page.locator("[data-roadmap-primary='planned-auth']").click()
        page.locator("[data-hud-promote]").click()
        assert page.locator("#projectTasksView").is_visible()
        assert page.locator("#taskDescriptionInput").input_value() == "Add authentication"
