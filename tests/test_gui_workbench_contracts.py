from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from execraft.gui.execution_lanes import ExecutionLaneAssignmentView, ExecutionLaneView


ROOT = Path(__file__).resolve().parents[1]


def test_execution_lane_view_is_presentation_only_and_secret_free() -> None:
    lane = ExecutionLaneView(
        id="lane-openclaw-qwen-gpu-1",
        display_name="Qwen GPU #1",
        runtime_id="openclaw-local",
        runtime_kind="openclaw",
        model_route_id="qwen3-coder",
        model_display_name="Qwen3 Coder 30B",
        target_id="gpu-1",
        target_display_name="GPU Node A",
        roles=("implement", "review", "fix_review"),
        profile_ids=(
            "openclaw-gpu-coder",
            "openclaw-gpu-review",
            "openclaw-gpu-fix-review",
        ),
        health="healthy",
        availability="ready",
        active_assignments=(
            ExecutionLaneAssignmentView(
                package_id="WP17",
                role="implement",
                status="running",
                agent_id="openclaw-gpu-coder",
            ),
        ),
        diagnostics_summary="ready",
    )

    payload = lane.as_mapping()
    assert payload["roles"] == ["implement", "review", "fix_review"]
    assert payload["profile_ids"] == [
        "openclaw-gpu-coder",
        "openclaw-gpu-review",
        "openclaw-gpu-fix-review",
    ]
    assert payload["active_assignments"][0]["package_id"] == "WP17"
    serialized = json.dumps(payload)
    assert "credential_ref" not in serialized
    assert "auth_ref" not in serialized


def test_workbench_frontend_contracts_with_node() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed; browser/JS CI provides this contract gate")
    completed = subprocess.run(
        [
            node,
            "--test",
            "tests/js/workbench-navigation.test.mjs",
            "tests/js/work-package-execution.test.mjs",
            "tests/js/project-execution-view.test.mjs",
            "tests/js/gui-simplification.test.mjs",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_task_render_imports_compact_counter_formatter() -> None:
    root = ROOT / "src/execraft/assets/gui"
    app = (root / "app.js").read_text(encoding="utf-8")
    utilities = (root / "ui-utils.js").read_text(encoding="utf-8")

    assert "compactCount," in app.split('from "./ui-utils.js"', 1)[0].rsplit("{", 1)[1]
    assert "export function compactCount(value)" in utilities
    assert '$("mTokens").textContent = `${compactCount(totalTokens)} processed`' in app


def test_gui_b_render_and_selection_paths_are_navigation_free() -> None:
    app = (ROOT / "src/execraft/assets/gui/app.js").read_text(encoding="utf-8")

    render_workflow = app.split("function renderWorkflow() {", 1)[1].split(
        "\nfunction renderAgents", 1
    )[0]
    selection_refresh = app.split(
        "function refreshWorkPackageSelectionVisuals() {", 1
    )[1].split("\nfunction openWorkPackageInspector", 1)[0]

    assert ".focus(" not in render_workflow
    assert ".focus(" not in selection_refresh
    assert "workbenchNavigation.setActive(workingIds)" in render_workflow
    assert "scheduleWorkflowNavigation()" in render_workflow
    assert 'return value === "list" ? "list" : "graph"' in app
    assert 'updateJsonStorage(GUI_PREFERENCES_KEY, { workflowViewMode: mode })' in app
    assert 'updateJsonStorage(GUI_PREFERENCES_KEY, { followActive: Boolean(enabled) })' in app


def test_gui_b_workflow_viewport_reserves_vertical_wheel_for_document() -> None:
    viewport = (ROOT / "src/execraft/assets/gui/workflow-viewport.js").read_text(
        encoding="utf-8"
    )
    css = (ROOT / "src/execraft/assets/gui/gui.css").read_text(encoding="utf-8")

    assert "if (!event.shiftKey) return;" in viewport
    assert "Ordinary vertical wheel/trackpad gestures belong to the document" in viewport
    assert "overflow-x: auto; overflow-y: hidden" in css
    assert "snapshot()" in viewport
    assert "restore({ x = 0, y = 0, scale = DEFAULT_SCALE } = {})" in viewport


def test_gui_c_work_package_inspector_replaces_modal_and_preserves_render_state() -> None:
    root = ROOT / "src/execraft/assets/gui"
    index = (root / "index.html").read_text(encoding="utf-8")
    app = (root / "app.js").read_text(encoding="utf-8")
    inspector = (root / "work-package-inspector.js").read_text(encoding="utf-8")
    workflow = (root / "workflow.js").read_text(encoding="utf-8")
    css = (root / "gui.css").read_text(encoding="utf-8")

    assert '<aside id="workPackageInspector"' in index
    assert 'id="workPackageDialog"' not in index
    assert 'id="workPackageOverviewTab"' in index
    assert 'id="workPackageExecutionTab"' in index
    assert 'id="workPackageEvidenceTab"' in index
    assert 'data-inspector-panel="overview"' in app
    assert 'data-inspector-panel="execution"' in app
    assert 'data-inspector-panel="evidence"' in app
    assert "workPackageInspector.captureRenderState()" in app
    assert "workPackageInspector.restoreRenderState(renderState)" in app
    assert "workPackageInspector.isOpenFor(packageId)" in app
    assert "showModal()" not in app.split("function openWorkPackageInspector", 1)[1].split(
        "async function setWorkPackageDirective", 1
    )[0]
    assert "captureRenderState()" in inspector
    assert "restoreRenderState(snapshot)" in inspector
    assert 'event.key !== "Escape"' in inspector
    assert 'focus({ preventScroll: true })' in inspector
    assert '.work-package-inspector { position: fixed' in css
    assert '.work-package-inspector { z-index: 95; inset: 0; width: 100vw' in css

    card_markup = workflow.split("function cardMarkup", 1)[1].split(
        "function markerForAppearance", 1
    )[0]
    assert 'workPackageQuickActions(item, { controlsLocked })' in card_markup
    quick_actions = workflow.split("function workPackageQuickActions", 1)[1].split("function listItemMarkup", 1)[0]
    assert quick_actions.count('data-work-package-action="execution"') == 1
    assert quick_actions.count('data-work-package-action="agent"') == 1
    assert 'data-work-package-action="repository-sync"' in quick_actions
    assert 'data-work-package-action="${paused ? "resume" : "pause"}"' in quick_actions
    assert "decompositionAction" not in card_markup
    assert "syncLabel" not in card_markup


def test_gui_d_execution_lane_presentation_stays_contextual() -> None:
    root = ROOT / "src/execraft/assets/gui"
    index = (root / "index.html").read_text(encoding="utf-8")
    app = (root / "app.js").read_text(encoding="utf-8")
    execution = (root / "work-package-execution.js").read_text(encoding="utf-8")
    runtime_control = (root / "runtime-control.js").read_text(encoding="utf-8")

    assert 'import { WorkPackageExecutionView } from "./work-package-execution.js";' in app
    assert "workPackageExecutionView.render(state.snapshot, p" in app
    assert 'id="runtimeSelectionPackage"' not in index
    assert 'id="runtimeTopologyLanes"' in index
    assert "/api/runtime/selection/preview" in execution
    assert "/api/runtime/selection/apply" in execution
    assert "/api/runtime/selection/preview" not in runtime_control
    assert "profile_ids" in execution.split("#advancedMarkup", 1)[1]
