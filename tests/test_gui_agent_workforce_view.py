from pathlib import Path


ROOT = Path("src/execraft/assets/gui")


def _asset(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_agents_are_a_first_class_task_view_not_execution_health_content() -> None:
    index = _asset("index.html")
    assert 'id="agentsTab"' in index
    assert 'data-view="agents"' in index
    assert 'id="agentsView"' in index
    assert 'id="agentWorkforceSummary"' in index
    assert 'id="agentWorkforceGroups"' in index
    assert index.index('id="agentsView"') > index.index('id="runView"')
    assert index.index('id="agentsView"') > index.index('id="executionHealthDrawer"')


def test_workforce_view_is_task_centric_and_preserves_profile_identity_as_technical_detail() -> None:
    workforce = _asset("agent-workforce-view.js")
    assert "export function summarizeAgentWorkforce" in workforce
    assert '"working"' in workforce
    assert '"attention"' in workforce
    assert '"available"' in workforce
    assert "package_title" in workforce
    assert "lane_label" in workforce
    assert "profile_id" in workforce
    assert "Technical details" in workforce
    assert 'data-worker-action="open"' in workforce
    assert 'data-worker-action="doctor"' in workforce
    assert 'data-worker-action="promote"' in workforce
    assert 'data-worker-action="reset"' in workforce
    assert 'data-worker-action="diagnostics"' in workforce


def test_workforce_actions_reuse_existing_profile_maintenance_contracts() -> None:
    app = _asset("app.js")
    maintenance = _asset("profile-maintenance.js")
    assert 'import { AgentWorkforceView } from "./agent-workforce-view.js";' in app
    assert "new AgentWorkforceView({" in app
    assert 'profileMaintenance.openAgent(agentId, options)' in app
    assert 'profileMaintenance.runAgentAction(agentId, "doctor")' in app
    assert 'profileMaintenance.runAgentAction(agentId, "reset-health")' in app
    assert "profileMaintenance.openPromotion(agentId)" in app
    assert "openAgent(agentId" in maintenance
    assert "runAgentAction(agentId, action)" in maintenance
    assert "openPromotion(agentId)" in maintenance


def test_work_package_cards_restore_operational_shortcuts_without_making_card_click_execute() -> None:
    workflow = _asset("workflow.js")
    app = _asset("app.js")
    quick = workflow.split("function workPackageQuickActions", 1)[1].split(
        "function listItemMarkup", 1
    )[0]
    assert 'data-work-package-action="${paused ? "resume" : "pause"}"' in quick
    assert 'data-work-package-action="repository-sync"' in quick
    assert 'data-work-package-action="execution"' in quick
    assert 'data-work-package-action="agent"' in quick
    assert 'data-work-package-action="select"' in workflow
    action_handler = app.split("function handleWorkPackageAction", 1)[1].split(
        "function renderConfigMenu", 1
    )[0]
    assert '["select", "details", "assignment", "execution"].includes(action)' in action_handler
    assert '["assignment", "execution"].includes(action) ? "execution" : "overview"' in action_handler
