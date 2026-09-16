from __future__ import annotations

from pathlib import Path


def _asset(name: str) -> str:
    return (Path("src/execraft/assets/gui") / name).read_text(encoding="utf-8")


def test_run_surface_uses_compact_execution_health_strip_and_explicit_drawer() -> None:
    index = _asset("index.html")
    css = _asset("gui.css")

    assert 'id="diagnosticsStrip" class="execution-health-strip"' in index
    assert 'id="executionHealthDrawerOpen"' in index
    assert 'aria-controls="executionHealthDrawer"' in index
    assert 'id="executionHealthDrawer"' in index
    assert 'role="dialog" aria-modal="false"' in index
    assert 'id="executionHealthDrawerClose"' in index
    assert ".execution-health-drawer" in css
    assert ".execution-health-strip-button" in css
    assert "overscroll-behavior: contain" in css


def test_execution_health_progressively_discloses_profile_maintenance() -> None:
    index = _asset("index.html")
    maintenance = _asset("profile-maintenance.js")
    health = _asset("execution-health-view.js")

    assert 'id="executionHealthOverview"' in index
    assert 'id="executionProfileMaintenance"' in index
    assert "Advanced profile maintenance" in index
    assert "Profile IDs, timeouts, complexity ceilings" in index
    assert 'if (!this.healthView.isOpen() || !$("executionProfileMaintenance").open)' in maintenance
    assert "Available lanes" in health
    assert "Attention required" in health
    assert "Active" in health
    assert "if (this.isOpen()) this.#renderOverviewIfChanged()" in health
    assert "if (key === this.overviewKey) return" in health


def test_redundant_current_assignments_surface_is_removed() -> None:
    index = _asset("index.html")
    app = _asset("app.js")

    assert "Current assignments" not in index
    assert 'id="assignmentCount"' not in index
    assert 'id="assignmentRows"' not in index
    assert "function renderAssignments" not in app


def test_health_drawer_focus_and_responsive_contracts_are_explicit() -> None:
    health = _asset("execution-health-view.js")
    css = _asset("gui.css")

    assert 'this.openButton.setAttribute("aria-expanded", "true")' in health
    assert 'this.openButton.setAttribute("aria-expanded", "false")' in health
    assert 'event.key !== "Escape"' in health
    assert "preventScroll: true" in health
    assert "@media (max-width: 720px)" in css
    assert ".execution-health-drawer { inset: 0; width: 100vw" in css
    assert "prefers-reduced-motion" in css


def test_native_maintenance_is_retained_but_not_on_the_quiet_run_surface() -> None:
    index = _asset("index.html")
    maintenance = _asset("profile-maintenance.js")

    drawer_start = index.index('id="executionHealthDrawer"')
    maintenance_start = index.index('id="executionProfileMaintenance"', drawer_start)
    workflow_start = index.index('class="panel workflow-panel"', maintenance_start)
    assert maintenance_start < workflow_start
    assert "Doctor test" in maintenance
    assert "Reset health" in maintenance
    assert "promotion-action" in maintenance
    assert "OpenClaw lifecycle and model/location checks" in maintenance
    open_agent = maintenance.index("openAgent(agentId")
    assert maintenance.index('this.healthView.close({ restoreFocus: false });', open_agent) < maintenance.index(
        "this.agentConsole.open", open_agent
    )
    assert 'this.openAgent(button.dataset.agent' in maintenance


def test_action_bar_has_stable_default_height_and_incidents_are_deliberate_expansion() -> None:
    css = _asset("gui.css")
    index = _asset("index.html")

    assert ".action-center {" in css
    assert "min-height: 82px" in css
    assert 'id="actionDetails" class="action-details"' in index
    assert 'id="supervisorPanel" class="supervisor-notice hidden"' in index


def test_execution_health_labels_wrap_instead_of_being_clipped() -> None:
    css = _asset("gui.css")
    health = _asset("execution-health-view.js")

    assert 'grid-template-areas: "active available" "attention attention"' in css
    assert '.execution-health-group.attention { grid-area: attention; }' in css
    assert '.execution-health-item strong,.execution-health-item small { display: block; min-width: 0; white-space: normal; overflow-wrap: anywhere; }' in css
    assert '.execution-health-strip small { min-width: 0;' in css
    assert 'white-space: normal; overflow-wrap: anywhere;' in css
    assert 'font-size: 9.5px; line-height: 1.45;' in css
    assert 'class="execution-health-group active"' in health
    assert 'class="execution-health-group attention"' in health
    assert 'class="execution-health-group available"' in health
