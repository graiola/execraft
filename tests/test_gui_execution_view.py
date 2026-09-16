"""Regression tests for contextual Execution Lanes and advanced disclosure.

GUI-D moves package routing into the Work Package inspector. The global execution
panel is passive infrastructure/diagnostics; normal users route a Work Package by
role + policy + lane without re-selecting the package or manipulating raw
profile IDs.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "src/execraft/assets/gui"


@pytest.fixture(scope="module")
def index_html() -> str:
    return (ASSETS / "index.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def control_js() -> str:
    return (ASSETS / "runtime-control.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def work_package_execution_js() -> str:
    return (ASSETS / "work-package-execution.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def execution_section(index_html: str) -> str:
    start = index_html.index('<section id="runtimeRoutingSection"')
    return index_html[start : index_html.index("</section>", start)]


def test_global_execution_surface_is_infrastructure_only(execution_section: str) -> None:
    assert '<h3 id="executionTitle">Execution infrastructure</h3>' in execution_section
    assert "Work Package routing stays contextual in the Work Package inspector" in execution_section
    for obsolete_id in (
        "executionSummary",
        "runtimeSelectionPackage",
        "runtimeSelectionRole",
        "runtimeSelectionMode",
        "runtimeSelectionRuntime",
        "runtimeSelectionModel",
        "runtimeSelectionTarget",
    ):
        assert f'id="{obsolete_id}"' not in execution_section


def test_compact_health_remains_explicit_and_passive(execution_section: str) -> None:
    assert '<details id="executionHealth"' in execution_section
    assert 'id="executionHealthPill"' in execution_section
    health = execution_section[execution_section.index('<details id="executionHealth"') :]
    assert not health[: health.index(">")].strip().endswith("open")
    labels = re.findall(r"<label>([A-Za-z ]+)<select", health)
    assert {"Runtime", "Model", "Location"}.issubset(labels)
    assert "never contacts a gateway or model endpoint" in execution_section


def test_advanced_infrastructure_contains_lanes_and_raw_inventory(
    execution_section: str,
) -> None:
    advanced = execution_section[execution_section.index('<details id="executionAdvanced"') :]
    assert "<summary>Advanced configuration</summary>" in advanced
    for element_id in (
        "runtimeTopologyLanes",
        "runtimeTopologyRuntimes",
        "runtimeTopologyModels",
        "runtimeTopologyTargets",
        "runtimeTopologyProfiles",
    ):
        assert f'id="{element_id}"' in advanced
    assert "Execution lanes" in advanced
    assert "Raw profiles" in advanced
    assert 'data-utility-view="config"' in advanced


def test_runtime_control_no_longer_owns_package_routing(control_js: str) -> None:
    assert "/api/runtime/topology" in control_js
    assert "/api/runtime/diagnostics" in control_js
    assert "/api/runtime/selection/preview" not in control_js
    assert "/api/runtime/selection/apply" not in control_js
    assert "runtimeSelectionPackage" not in control_js
    assert "runtimeSelectionRole" not in control_js
    assert "runtimeTopologyLanes" in control_js


def test_work_package_execution_reuses_existing_selection_api(
    work_package_execution_js: str,
) -> None:
    assert "/api/runtime/selection/preview" in work_package_execution_js
    assert "/api/runtime/selection/apply" in work_package_execution_js
    for mode in ('value: "automatic"', 'value: "prefer"', 'value: "force"'):
        assert mode in work_package_execution_js
    assert "Automatic clears the role override" in work_package_execution_js
    assert "Force makes the matching profile pool binding" in work_package_execution_js


def test_contextual_selection_payload_keeps_scheduler_schema(
    work_package_execution_js: str,
) -> None:
    payload = work_package_execution_js[work_package_execution_js.index("#selectionPayload(") :]
    for field in (
        "package_id",
        "role",
        "mode",
        "runtime_id",
        "model_route_id",
        "target_id",
        "apply_to_shards",
    ):
        assert f"{field}:" in payload
    assert "runtime_id: lane?.runtime_id" in payload
    assert "model_route_id: lane?.model_route_id" in payload
    assert "target_id: lane?.target_id" in payload


def test_normal_routing_is_lane_first_and_package_contextual(
    work_package_execution_js: str,
) -> None:
    render = work_package_execution_js[
        work_package_execution_js.index("  render(snapshot, packageInfo") :
        work_package_execution_js.index("  bind(container", work_package_execution_js.index("  render(snapshot, packageInfo"))
    ]
    assert "Execution lane" in render
    assert "data-lane-role" in render
    assert "data-lane-mode" in render
    assert "data-lane-select" in render
    assert "Package<select" not in render
    assert "profile_ids" not in render
    assert "Apply routing to direct child shards" in render


def test_raw_profile_ids_exist_only_in_advanced_work_package_policy(
    work_package_execution_js: str,
) -> None:
    normal = work_package_execution_js[: work_package_execution_js.index("  #advancedMarkup(")]
    advanced = work_package_execution_js[work_package_execution_js.index("  #advancedMarkup(") :]
    assert "Profiles</dt>" not in normal
    assert "Profiles</dt>" in advanced
    assert "lane.profile_ids" in advanced
    assert "Advanced profile and skill policy" in advanced


def test_diagnostics_remain_explicit_only(control_js: str) -> None:
    assert control_js.count("/api/runtime/diagnostics") == 1
    diagnose = control_js[control_js.index("async #diagnose()") :]
    assert "/api/runtime/diagnostics" in diagnose
