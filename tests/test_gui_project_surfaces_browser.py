"""Network-free browser regressions for Roadmap and Project Execution surfaces.

These tests deliberately avoid the dashboard HTTP fixture.  They inject the
production HTML/CSS and ES modules into an about:blank Playwright page so they
remain runnable in locked-down environments that forbid localhost navigation.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest


playwright = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parents[1]
GUI = ROOT / "src" / "execraft" / "assets" / "gui"
VIEWPORTS = (
    {"width": 1920, "height": 1080},
    {"width": 1440, "height": 900},
    {"width": 1280, "height": 800},
    {"width": 1024, "height": 768},
)


def _data_module(path: Path) -> str:
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:text/javascript;base64,{payload}"


def _module_source(path: Path, replacements: dict[str, str]) -> str:
    source = path.read_text(encoding="utf-8")
    for old, new in replacements.items():
        source = source.replace(old, new)
    return source


def _shell(page, viewport: dict[str, int]) -> None:
    page.set_viewport_size(viewport)
    html = (GUI / "index.html").read_text(encoding="utf-8").replace(
        '<script type="module" src="/assets/app.js"></script>', ""
    )
    css = (GUI / "gui.css").read_text(encoding="utf-8")
    html = html.replace("</head>", f"<style>{css}</style></head>", 1)
    page.set_content(html, wait_until="domcontentloaded")
    page.evaluate(
        """
        () => {
          const makeStorage = () => {
            const values = new Map();
            return {
              getItem: (key) => values.has(String(key)) ? values.get(String(key)) : null,
              setItem: (key, value) => values.set(String(key), String(value)),
              removeItem: (key) => values.delete(String(key)),
              clear: () => values.clear(),
            };
          };
          Object.defineProperty(window, 'localStorage', {value: makeStorage(), configurable: true});
          Object.defineProperty(window, 'sessionStorage', {value: makeStorage(), configurable: true});
          const home = document.getElementById('homeContent');
          const workspace = document.getElementById('projectWorkspaceView');
          home.classList.remove('hidden');
          for (const child of home.children) {
            if (child.tagName === 'DIALOG') continue;
            child.classList.toggle('hidden', child !== workspace);
          }
          workspace.classList.remove('hidden');
          document.getElementById('mainContent').classList.add('hidden');
        }
        """
    )


def _load_module(page, source: str, global_name: str, export_name: str) -> None:
    page.evaluate(
        """
        async ({source, globalName, exportName}) => {
          const blob = new Blob([source], {type: 'text/javascript'});
          const url = URL.createObjectURL(blob);
          try {
            const module = await import(url);
            window[globalName] = module[exportName];
          } finally {
            URL.revokeObjectURL(url);
          }
        }
        """,
        {"source": source, "globalName": global_name, "exportName": export_name},
    )


def _install_project_execution_view(page) -> None:
    ui_utils = _data_module(GUI / "ui-utils.js")
    source = _module_source(
        GUI / "project-execution-view.js",
        {'"./ui-utils.js"': f'"{ui_utils}"'},
    )
    _load_module(page, source, "ProjectExecutionView", "ProjectExecutionView")


def _install_roadmap_view(page) -> None:
    ui_utils = _data_module(GUI / "ui-utils.js")
    interactions = _data_module(GUI / "roadmap-interactions.js")
    source = _module_source(
        GUI / "roadmap-view.js",
        {
            '"./ui-utils.js"': f'"{ui_utils}"',
            '"./roadmap-interactions.js"': f'"{interactions}"',
        },
    )
    _load_module(page, source, "RoadmapView", "RoadmapView")


PROJECT_EXECUTION_SNAPSHOT = {
    "configured": True,
    "definition_revision": 12,
    "mode": "assisted",
    "held": False,
    "hold_reason": "",
    "attention": [
        {
            "kind": "gate_awaiting_decision",
            "id": "integration-ready",
            "message": "Integration Ready requires an operator decision.",
        }
    ],
    "policy": {},
    "automatic": {},
    "ready_tasks": ["payload-ui"],
    "blocked_tasks": [
        {
            "task_id": "system-demo",
            "reasons": [
                {"kind": "gate_unsatisfied", "message": "Gate safety-ready is not satisfied"}
            ],
        }
    ],
    "phases": [
        {
            "id": "integration",
            "title": "Integration",
            "description": "Integrate payload capability.",
            "state": "active",
            "health": "on_track",
            "schedule": {"start": "2026-09-01", "target": "2026-10-15"},
            "entry_gates": [],
            "exit_gates": ["integration-ready"],
            "tasks": ["payload-ui", "system-demo"],
            "milestones": ["payload-mvp"],
            "health_reasons": [],
        }
    ],
    "gates": [
        {
            "id": "integration-ready",
            "title": "Integration Ready",
            "description": "Human review of current evidence.",
            "state": "awaiting_decision",
            "schedule": {"target": "2026-10-10"},
            "criteria": {"all": [{"type": "human_approval"}]},
            "input_fingerprint": "sha256:0123456789abcdef0123456789abcdef",
            "evaluation": {
                "revision": 3,
                "evidence": [{"type": "human_approval", "satisfied": False}],
                "reasons": ["Human approval required"],
            },
            "decision_history": [],
        },
        {
            "id": "safety-ready",
            "title": "Safety Ready",
            "state": "waiting",
            "criteria": {"all": [{"type": "task_completion", "task_id": "payload-ui"}]},
            "evaluation": {"revision": 0, "evidence": [], "reasons": []},
            "decision_history": [],
        },
    ],
    "milestones": [
        {
            "id": "payload-mvp",
            "title": "Payload MVP",
            "description": "Frozen payload baseline.",
            "state": "pending",
            "health": "on_track",
            "schedule": {"target": "2026-10-15"},
            "requires": {"tasks": ["payload-ui"], "gates": ["integration-ready"], "milestones": []},
            "delivery": {"policy": "candidate"},
            "missing_requirements": ["Task payload-ui is incomplete"],
        }
    ],
    "tasks": [
        {
            "task_id": "payload-ui",
            "title": "Payload UI",
            "phase": "integration",
            "required": True,
            "outcome": "not_started",
            "requires": {"tasks": [], "gates": []},
            "eligibility": {"eligible": True, "reasons": []},
        },
        {
            "task_id": "system-demo",
            "title": "System Demo",
            "phase": "integration",
            "required": True,
            "outcome": "not_started",
            "requires": {"tasks": ["payload-ui"], "gates": ["safety-ready"]},
            "eligibility": {
                "eligible": False,
                "reasons": [{"kind": "gate_unsatisfied", "message": "Gate safety-ready is not satisfied"}],
            },
        },
    ],
}

ROADMAP_SNAPSHOT = {
    "schema_version": 2,
    "project_id": "demo",
    "id": "platform",
    "title": "Platform roadmap",
    "description": "R5 browser fixture",
    "revision": 9,
    "project_execution_revision": 12,
    "project_execution_configured": True,
    "lanes": ["Platform"],
    "items": [
        {
            "id": "task-demo",
            "kind": "task",
            "task_id": "demo-task",
            "lane": "Platform",
            "order": 10,
            "schedule": {"start": "2026-10-01", "target": "2026-10-31"},
            "task": {
                "id": "demo-task",
                "title": "Demo task",
                "status": "in_progress",
                "availability": "active",
                "runtime_state": "running",
                "progress_percent": 45,
            },
            "project_phase": {"id": "integration", "title": "Integration", "order": 0, "kind": "phase"},
        },
        {
            "id": "gate-review",
            "kind": "gate",
            "project_asset_id": "integration-ready",
            "lane": "Platform",
            "order": 20,
            "schedule": {"target": "2026-11-03"},
            "title": "Integration Ready",
            "project_asset": {"state": "awaiting_decision", "health": "on_track"},
            "project_phase": {"id": "integration", "title": "Integration", "order": 0, "kind": "phase"},
        },
    ],
    "relations": [],
    "unscheduled_tasks": [
        {"id": "blocked-task", "title": "Blocked task", "status": "blocked", "availability": "active"}
    ],
    "statistics": {"items": 2, "linked_tasks": 1, "planned_tasks": 0, "milestones": 0, "gates": 1, "unscheduled_tasks": 1},
}


def _large_roadmap_snapshot(size: int) -> dict:
    items = []
    relations = []
    phase_count = max(4, min(20, size // 20))
    for index in range(size):
        month = 10 + ((index // 28) % 3)
        day = (index % 28) + 1
        start = f"2026-{month:02d}-{day:02d}"
        target_day = min(28, day + 3)
        target = f"2026-{month:02d}-{target_day:02d}"
        phase_index = index % phase_count
        item_id = f"planned-{index:04d}"
        items.append(
            {
                "id": item_id,
                "kind": "planned_task",
                "title": f"Planned task {index:04d}",
                "description": "Synthetic scale fixture",
                "lane": f"Lane {index % 12:02d}",
                "order": (index + 1) * 10,
                "schedule": {"start": start, "target": target},
                "project_phase": {
                    "id": f"phase-{phase_index:02d}",
                    "title": f"Phase {phase_index:02d}",
                    "order": phase_index,
                    "kind": "phase",
                },
            }
        )
        if index and index % 4 == 0:
            relations.append(
                {
                    "from": f"planned-{index - 1:04d}",
                    "to": item_id,
                    "kind": "blocks",
                }
            )
    return {
        "schema_version": 2,
        "project_id": "demo",
        "id": f"scale-{size}",
        "title": f"Scale roadmap {size}",
        "description": "R6 scale fixture",
        "revision": 4,
        "project_execution_revision": 17,
        "project_execution_configured": True,
        "lanes": [f"Lane {index:02d}" for index in range(12)],
        "items": items,
        "relations": relations,
        "unscheduled_tasks": [],
        "statistics": {
            "items": size,
            "linked_tasks": 0,
            "planned_tasks": size,
            "milestones": 0,
            "gates": 0,
            "unscheduled_tasks": 0,
        },
    }


def _two_frames(page) -> None:
    page.evaluate(
        "() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
    )



@pytest.fixture
def page():
    with playwright.sync_playwright() as runtime:
        system_chromium = Path("/usr/bin/chromium")
        browser = runtime.chromium.launch(
            executable_path=str(system_chromium) if system_chromium.exists() else None,
            args=["--no-sandbox"] if system_chromium.exists() else None,
        )
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        yield page
        browser.close()


def _render_execution(page, snapshot: dict | None = None, *, post_error: str = "") -> None:
    _install_project_execution_view(page)
    data = snapshot or PROJECT_EXECUTION_SNAPSHOT
    page.evaluate(
        """
        ({snapshot, postError}) => {
          document.getElementById('projectRoadmapView').classList.add('hidden');
          document.getElementById('projectExecutionView').classList.remove('hidden');
          document.getElementById('projectRoadmapTab').classList.remove('active');
          document.getElementById('projectExecutionTab').classList.add('active');
          window.__projectOpened = [];
          const api = async (path, options = {}) => {
            if (options.method === 'POST' && postError) throw new Error(postError);
            if (path.startsWith('/api/project-execution/coordination/history')) {
              return {project_id: 'demo', limit: 20, entries: structuredClone(snapshot.__coordination_history || [])};
            }
            return structuredClone(snapshot);
          };
          window.__projectExecutionView = new window.ProjectExecutionView({
            api,
            download: async () => 'project.pdf',
            toast: () => {},
            onOpenTask: async (_projectId, taskId) => window.__projectOpened.push(taskId),
            onCanonicalChange: () => {},
          });
          window.__projectExecutionView.setProject('demo', [
            {id: 'payload-ui', title: 'Payload UI', status: 'planned'},
            {id: 'system-demo', title: 'System Demo', status: 'planned'},
          ]);
        }
        """,
        {"snapshot": data, "postError": post_error},
    )
    page.evaluate("window.__projectExecutionView.load()")
    page.locator("#projectExecutionWorkspace").wait_for(state="visible")


def _render_roadmap(page, snapshot: dict | None = None) -> float:
    _install_roadmap_view(page)
    data = snapshot or ROADMAP_SNAPSHOT
    page.evaluate(
        """
        ({roadmap}) => {
          document.getElementById('projectExecutionView').classList.add('hidden');
          document.getElementById('projectRoadmapView').classList.remove('hidden');
          document.getElementById('projectExecutionTab').classList.remove('active');
          document.getElementById('projectRoadmapTab').classList.add('active');
          window.__roadmapOpened = [];
          const catalog = {
            project_id: 'demo',
            roadmaps: [{id: roadmap.id, title: roadmap.title, description: roadmap.description, revision: roadmap.revision, item_count: roadmap.items.length, task_count: 1}],
          };
          const api = async (path) => {
            if (path.startsWith('/api/roadmaps')) return structuredClone(catalog);
            if (path.startsWith('/api/roadmap?')) return structuredClone(roadmap);
            throw new Error(`Unexpected fixture API call: ${path}`);
          };
          window.__roadmapView = new window.RoadmapView({
            api,
            download: async () => 'roadmap.svg',
            toast: () => {},
            onOpenTask: async (_projectId, taskId) => window.__roadmapOpened.push(taskId),
            onCreateTaskFromPlanned: async () => {},
            onOpenProjectAsset: async () => {},
            onCanonicalChange: () => {},
          });
          window.__roadmapView.setProject('demo');
        }
        """,
        {"roadmap": data},
    )
    elapsed = page.evaluate(
        """
        async () => {
          const started = performance.now();
          await window.__roadmapView.load();
          await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
          return performance.now() - started;
        }
        """
    )
    page.locator("#roadmapWorkspace").wait_for(state="visible")
    return float(elapsed)


@pytest.mark.parametrize("viewport", VIEWPORTS)
def test_roadmap_geometry_fit_and_execution_layout_are_responsive(page, viewport):
    _shell(page, viewport)
    _render_roadmap(page)

    timeline = page.locator("#roadmapTimelineScroll").bounding_box()
    task = page.locator("[data-roadmap-item='task-demo']").bounding_box()
    assert timeline is not None and task is not None
    assert timeline["width"] >= viewport["width"] * 0.72
    assert task["x"] < timeline["x"] + timeline["width"]
    assert task["x"] + task["width"] > timeline["x"]
    assert page.locator("#roadmapFitBtn").get_attribute("aria-pressed") == "true"

    _render_execution(page)
    workspace = page.locator("#projectExecutionWorkspace").bounding_box()
    definitions = page.locator(".project-execution-asset-grid").bounding_box()
    assert workspace is not None and definitions is not None
    assert workspace["width"] >= viewport["width"] * 0.70
    assert page.locator("#projectExecutionCurrent").is_visible()
    assert page.locator("#projectExecutionReadyTasks").is_visible()
    assert page.locator("#projectExecutionBlockedTasks").is_visible()
    assert page.locator(".project-execution-asset-grid").evaluate("el => el.scrollWidth <= el.clientWidth + 1")


def test_roadmap_keyboard_selection_preserves_focus_and_enter_opens(page):
    _shell(page, {"width": 1280, "height": 800})
    _render_roadmap(page)

    block = page.locator("[data-roadmap-item='task-demo']")
    block.focus()
    block.press("Space")
    page.wait_for_timeout(0)
    assert page.evaluate("document.activeElement?.dataset?.roadmapItem") == "task-demo"
    assert page.locator("[data-roadmap-item='task-demo']").get_attribute("aria-pressed") == "true"
    assert page.locator("#roadmapSelectedBtn").is_enabled()

    page.locator("[data-roadmap-item='task-demo']").press("Enter")
    assert page.evaluate("window.__roadmapOpened") == ["demo-task"]


def test_project_execution_dialogs_fit_and_reference_picker_keeps_keyboard_focus(page):
    _shell(page, {"width": 1024, "height": 768})
    _render_execution(page)

    page.evaluate("window.__projectExecutionView.focusAsset('gate', 'integration-ready')")
    page.locator("[data-project-gate-decision='approved:integration-ready']").click()
    dialog = page.locator("#projectExecutionGateDecisionDialog")
    box = dialog.bounding_box()
    assert dialog.is_visible() and box is not None
    assert box["x"] >= 0 and box["y"] >= 0
    assert box["x"] + box["width"] <= 1024
    assert box["y"] + box["height"] <= 768
    assert page.evaluate("document.activeElement?.id") == "projectExecutionGateDecisionActor"
    assert "sha256:" in page.locator("#projectExecutionGateDecisionFingerprint").inner_text()
    page.locator("#projectExecutionGateDecisionCancel").click()

    page.evaluate("window.__projectExecutionView.focusAsset('phase', 'integration')")
    page.locator("[data-project-asset-edit='phase:integration']").click()
    picker = page.locator("#projectExecutionPhaseExitGates [data-reference-add]")
    picker.focus()
    picker.select_option("safety-ready")
    assert page.evaluate("document.activeElement?.closest('#projectExecutionPhaseExitGates') !== null") is True
    assert page.locator("#projectExecutionPhaseExitGates").get_by_text("Safety Ready").is_visible()


def test_project_execution_held_unconfigured_and_conflict_states_are_explicit(page):
    _shell(page, {"width": 1280, "height": 800})
    held = dict(PROJECT_EXECUTION_SNAPSHOT)
    held["held"] = True
    held["hold_reason"] = "release freeze"
    _render_execution(page, held)
    banner = page.locator("#projectExecutionStateBanner")
    assert banner.is_visible()
    assert "release freeze" in banner.inner_text()
    assert "held" in banner.get_attribute("class")

    # A stale mutation keeps last-known data visible and exposes an explicit
    # reconcile action instead of only a transient toast.
    _shell(page, {"width": 1280, "height": 800})
    _render_execution(page, PROJECT_EXECUTION_SNAPSHOT, post_error="definition revision conflict: refresh before retry")
    page.locator("#projectExecutionMode").select_option("observe")
    page.wait_for_function("document.querySelector('#projectExecutionStateBanner')?.classList.contains('conflict')")
    assert "Concurrent change detected" in page.locator("#projectExecutionStateBanner").inner_text()
    assert page.locator("[data-project-execution-reconcile]").is_visible()

    _shell(page, {"width": 1280, "height": 800})
    _install_project_execution_view(page)
    unconfigured = {"configured": False, "definition_revision": 0, "mode": "assisted"}
    page.evaluate(
        """
        ({snapshot}) => {
          document.getElementById('projectRoadmapView').classList.add('hidden');
          document.getElementById('projectExecutionView').classList.remove('hidden');
          const view = new window.ProjectExecutionView({api: async () => snapshot, download: async () => '', toast: () => {}, onOpenTask: async () => {}});
          view.setProject('demo', []);
          window.__unconfiguredView = view;
        }
        """,
        {"snapshot": unconfigured},
    )
    page.evaluate("window.__unconfiguredView.load()")
    assert page.locator("#projectExecutionWorkspace").is_hidden()
    assert "not initialized" in page.locator("#projectExecutionWorkspaceEmpty").inner_text()
    assert page.locator("#projectExecutionInitialize").is_visible()

def test_roadmap_empty_and_error_states_are_explicit(page):
    _shell(page, {"width": 1280, "height": 800})
    _install_roadmap_view(page)
    page.evaluate(
        """
        () => {
          document.getElementById('projectExecutionView').classList.add('hidden');
          document.getElementById('projectRoadmapView').classList.remove('hidden');
          const view = new window.RoadmapView({
            api: async () => ({project_id: 'demo', roadmaps: []}),
            download: async () => '',
            toast: () => {},
            onOpenTask: async () => {},
            onCreateTaskFromPlanned: async () => {},
            onOpenProjectAsset: async () => {},
            onCanonicalChange: () => {},
          });
          view.setProject('demo');
          window.__emptyRoadmapView = view;
        }
        """
    )
    page.evaluate("window.__emptyRoadmapView.load()")
    empty = page.locator("#roadmapEmptyState")
    assert empty.is_visible()
    assert empty.get_attribute("role") == "status"
    assert "No roadmap yet" in empty.inner_text()

    _shell(page, {"width": 1280, "height": 800})
    _install_roadmap_view(page)
    page.evaluate(
        """
        () => {
          document.getElementById('projectExecutionView').classList.add('hidden');
          document.getElementById('projectRoadmapView').classList.remove('hidden');
          const view = new window.RoadmapView({
            api: async () => { throw new Error('Roadmap projection unavailable'); },
            download: async () => '',
            toast: () => {},
            onOpenTask: async () => {},
            onCreateTaskFromPlanned: async () => {},
            onOpenProjectAsset: async () => {},
            onCanonicalChange: () => {},
          });
          view.setProject('demo');
          window.__failedRoadmapView = view;
        }
        """
    )
    page.evaluate("window.__failedRoadmapView.load()")
    failure = page.locator("#roadmapEmptyState")
    assert failure.is_visible()
    assert failure.get_attribute("role") == "alert"
    assert "Roadmap projection unavailable" in failure.inner_text()


@pytest.mark.parametrize(
    ("size", "initial_budget_ms", "interaction_budget_ms"),
    ((100, 1500.0, 700.0), (250, 2500.0, 1200.0), (500, 5000.0, 2200.0)),
)
def test_roadmap_scale_render_fit_selection_dependencies_and_phase_grouping(
    page, size, initial_budget_ms, interaction_budget_ms
):
    """Guard measured Roadmap scale before introducing speculative virtualization."""

    _shell(page, {"width": 1440, "height": 900})
    initial_ms = _render_roadmap(page, _large_roadmap_snapshot(size))
    assert page.locator("[data-roadmap-item]").count() >= size
    assert page.locator(".roadmap-relations .roadmap-relation").count() >= max(1, size // 5 - 2)

    selection_ms = page.evaluate(
        """
        async () => {
          const item = document.querySelector('[data-roadmap-item="planned-0000"]');
          const started = performance.now();
          item.click();
          await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
          return performance.now() - started;
        }
        """
    )
    fit_ms = page.evaluate(
        """
        async () => {
          const started = performance.now();
          document.getElementById('roadmapFitBtn').click();
          await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
          return performance.now() - started;
        }
        """
    )
    grouping_ms = page.evaluate(
        """
        async () => {
          const select = document.getElementById('roadmapGroupBy');
          const started = performance.now();
          select.value = 'phase';
          select.dispatchEvent(new Event('change', {bubbles: true}));
          await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
          return performance.now() - started;
        }
        """
    )

    # Budgets are intentionally generous and protect against order-of-magnitude
    # regressions rather than machine-specific micro-benchmark noise.
    assert initial_ms < initial_budget_ms, (size, "initial", initial_ms)
    assert float(selection_ms) < interaction_budget_ms, (size, "selection", selection_ms)
    assert float(fit_ms) < interaction_budget_ms, (size, "fit", fit_ms)
    assert float(grouping_ms) < interaction_budget_ms, (size, "phase", grouping_ms)
    assert page.locator(".roadmap-phase-group-row").count() > 0


def test_r7_divergent_coordination_is_visible_without_force_controls(page):
    coordination = {
        "pending": True,
        "project_id": "demo",
        "roadmap_id": "platform",
        "operation_id": "op-r7-divergent",
        "operation": "move_gate",
        "phase": "project_execution_applied",
        "created_at": "2026-09-11T20:00:00+00:00",
        "updated_at": "2026-09-11T20:01:00+00:00",
        "roadmap": {
            "state": "divergent",
            "expected_revision": 9,
            "current_revision": 11,
            "desired_revision": 10,
            "recorded_result_revision": 0,
        },
        "project_execution": {
            "state": "applied",
            "expected_revision": 12,
            "current_revision": 13,
            "desired_revision": 13,
            "recorded_result_revision": 13,
        },
        "safe_actions": [],
        "automatic_action_available": False,
        "divergent": True,
        "message": "At least one durable document changed outside the recorded coordination intent.",
    }

    _shell(page, {"width": 1440, "height": 900})
    execution = dict(PROJECT_EXECUTION_SNAPSHOT)
    execution["coordination"] = coordination
    _render_execution(page, execution)
    banner = page.locator("#projectExecutionStateBanner")
    assert banner.is_visible()
    assert "requires operator review" in banner.inner_text()
    assert "Roadmap: divergent" in banner.inner_text()
    assert "Project Execution: applied" in banner.inner_text()
    assert "No force/overwrite action is safe" in banner.inner_text()
    assert page.locator("[data-project-coordination-action]").count() == 0

    _shell(page, {"width": 1440, "height": 900})
    roadmap = dict(ROADMAP_SNAPSHOT)
    roadmap["coordination"] = coordination
    _render_roadmap(page, roadmap)
    inline = page.locator("#roadmapInlineState")
    assert inline.is_visible()
    assert "Roadmap divergent" in inline.inner_text()
    assert "Open Project Execution for typed recovery details" in inline.inner_text()


def test_r8_coordination_forensics_and_history_are_read_only_and_visible(page):
    coordination = {
        "pending": True,
        "project_id": "demo",
        "roadmap_id": "platform",
        "operation_id": "op-r8-forensic",
        "operation": "move_gate",
        "phase": "project_execution_applied",
        "created_at": "2026-09-12T05:00:00+00:00",
        "updated_at": "2026-09-12T05:01:00+00:00",
        "roadmap": {
            "state": "divergent",
            "expected_revision": 9,
            "current_revision": 11,
            "desired_revision": 10,
            "recorded_result_revision": 0,
        },
        "project_execution": {
            "state": "applied",
            "expected_revision": 12,
            "current_revision": 13,
            "desired_revision": 13,
            "recorded_result_revision": 13,
        },
        "safe_actions": [],
        "automatic_action_available": False,
        "divergent": True,
        "message": "At least one durable document changed outside the recorded coordination intent.",
        "forensics": {
            "available": True,
            "truncated": False,
            "roadmap": {
                "before": {"revision": 9, "digest": "road-before-digest"},
                "desired": {"revision": 10, "digest": "road-desired-digest"},
                "current": {"revision": 11, "digest": "road-current-digest"},
                "subjects": [
                    {
                        "identity": {"item_id": "integration-gate-row"},
                        "before": {"item_id": "integration-gate-row", "kind": "gate", "project_asset_id": "integration-ready", "lane": "Platform", "order": 20},
                        "desired": {"item_id": "integration-gate-row", "kind": "gate", "project_asset_id": "integration-ready", "lane": "Decision", "order": 20},
                        "current": {"item_id": "integration-gate-row", "kind": "gate", "project_asset_id": "integration-ready", "lane": "Operator edit", "order": 30},
                    }
                ],
            },
            "project_execution": {
                "before": {"revision": 12, "digest": "execution-before-digest"},
                "desired": {"revision": 13, "digest": "execution-desired-digest"},
                "current": {"revision": 13, "digest": "execution-desired-digest"},
                "subjects": [
                    {
                        "identity": {"kind": "gate", "asset_id": "integration-ready"},
                        "before": {"asset_id": "integration-ready", "kind": "gate", "title": "Integration Ready", "schedule": {"target": "2026-10-10"}, "criterion_count": 1},
                        "desired": {"asset_id": "integration-ready", "kind": "gate", "title": "Integration Ready", "schedule": {"target": "2026-10-20"}, "criterion_count": 1},
                        "current": {"asset_id": "integration-ready", "kind": "gate", "title": "Integration Ready", "schedule": {"target": "2026-10-20"}, "criterion_count": 1},
                    }
                ],
            },
        },
    }
    execution = dict(PROJECT_EXECUTION_SNAPSHOT)
    execution["coordination"] = coordination
    execution["__coordination_history"] = [
        {
            "operation_id": "old-op",
            "operation": "upsert_gate",
            "phase": "complete",
            "roadmap_revision": 8,
            "project_execution_revision": 12,
            "timestamp": "2026-09-11T21:00:00+00:00",
            "reason": "",
        }
    ]

    _shell(page, {"width": 1440, "height": 900})
    _render_execution(page, execution)
    page.get_by_role("button", name="Inspect three-way state").click()
    panel = page.locator("#projectExecutionCoordinationInspector")
    assert panel.is_visible()
    text = panel.inner_text()
    lower = text.lower()
    assert "Roadmap planning metadata" in text
    assert "Project Execution canonical metadata" in text
    assert "before" in lower and "recorded desired" in lower and "current" in lower
    assert "lane Platform" in text
    assert "lane Decision" in text
    assert "lane Operator edit" in text
    assert "2026-10-10" in text
    assert "2026-10-20" in text
    assert "Upsert Gate · complete" in text
    assert "Roadmap r8 · Execution r12" in text
    assert page.locator("#projectExecutionCoordinationInspector input").count() == 0
    assert page.locator("#projectExecutionCoordinationInspector textarea").count() == 0
    assert page.locator("#projectExecutionCoordinationInspector [data-project-coordination-action]").count() == 0
