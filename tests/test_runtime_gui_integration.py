import re
from pathlib import Path

ROOT = Path("src/execraft")


def test_runtime_control_is_wired_into_task_diagnostics_and_router():
    index = (ROOT / "assets/gui/index.html").read_text(encoding="utf-8")
    app = (ROOT / "assets/gui/app.js").read_text(encoding="utf-8")
    router = (ROOT / "gui/routes/router.py").read_text(encoding="utf-8")
    server = (ROOT / "gui/server.py").read_text(encoding="utf-8")
    for element_id in (
        "runtimeRoutingSection",
        "runtimeTopologyLanes",
        "runtimeTopologyRuntimes",
        "runtimeTopologyModels",
        "runtimeTopologyTargets",
        "runtimeTopologyProfiles",
        "projectRuntimeTopologyView",
    ):
        assert f'id="{element_id}"' in index
    assert 'import { RuntimeControlView } from "./runtime-control.js";' in app
    assert 'import { WorkPackageExecutionView } from "./work-package-execution.js";' in app
    assert "new RuntimeControlView" in app
    assert "new WorkPackageExecutionView" in app
    assert "runtimeControlView.renderSnapshot(snapshot)" in app
    assert 'view === "settings"' in app
    assert "runtime.dispatch_get" in router
    assert "runtime.dispatch_post" in router
    assert "RuntimeTopologyDashboardMixin" in server
    assert '"runtime-control.js": "text/javascript; charset=utf-8"' in server


def test_runtime_control_asset_references_only_declared_html_ids():
    index = (ROOT / "assets/gui/index.html").read_text(encoding="utf-8")
    source = (ROOT / "assets/gui/runtime-control.js").read_text(encoding="utf-8")
    ids = set(re.findall(r'id="([A-Za-z0-9_-]+)"', index))
    references = set(re.findall(r'\$\("([A-Za-z0-9_-]+)"\)', source))
    assert references - ids == set()
