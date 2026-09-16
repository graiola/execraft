from __future__ import annotations

from pathlib import Path


APP = Path("src/execraft/assets/gui/app.js")
INSPECTOR = Path("src/execraft/assets/gui/work-package-inspector.js")
EXECUTION = Path("src/execraft/assets/gui/work-package-execution.js")


def _function_body(source: str, name: str, next_name: str | None = None) -> str:
    start = source.index(f"function {name}")
    if next_name is not None:
        end = source.index(f"function {next_name}", start)
    else:
        end = source.find("\nfunction ", start + len(f"function {name}"))
        if end < 0:
            end = len(source)
    return source[start:end]


def test_work_package_render_key_does_not_serialize_whole_package() -> None:
    source = INSPECTOR.read_text(encoding="utf-8")
    start = source.index("export function workPackageDetailSnapshotKey")
    body = source[start : source.index("/**\n * Stable Work Package side-inspector lifecycle.", start)]
    assert "packageInfo: workPackageRenderState(packageInfo)" in body
    assert "relations: packages.map(workPackageRelationRenderState)" in body
    assert "\n    packageInfo,\n" not in body

    evidence = _function_body(
        source, "workPackageEvidenceSignature", "workPackageRenderState"
    )
    assert ".slice(-5)" in evidence
    assert "Object.entries(value)" in evidence
    assert ".slice(0, 16)" in evidence


def test_inspector_opens_before_rich_work_package_rendering() -> None:
    source = APP.read_text(encoding="utf-8")
    body = _function_body(source, "openWorkPackageInspector", "setWorkPackageDirective")
    assert body.index("workPackageInspector.open({") < body.index("showPackageSafely(id)")
    assert "showModal()" not in body
    assert "Work Package details could not be rendered." in source


def test_selection_details_and_assignment_route_through_one_inspector_boundary() -> None:
    source = APP.read_text(encoding="utf-8")
    body = _function_body(source, "handleWorkPackageAction")
    early_branch = body.index('["select", "details", "assignment", "execution"].includes(action)')
    open_call = body.index("openWorkPackageInspector", early_branch)
    execution_tab = body.index('["assignment", "execution"].includes(action) ? "execution" : "overview"')
    return_statement = body.index("return;", open_call)
    assert early_branch < open_call < execution_tab < return_statement


def test_execution_drafts_are_owned_by_inspector_module_and_reset_by_context() -> None:
    app = APP.read_text(encoding="utf-8")
    source = EXECUTION.read_text(encoding="utf-8")
    assert "preferenceDraft" not in app
    assert "this.routingDrafts = new Map()" in source
    assert "this.advancedDrafts = new Map()" in source
    set_context = source[source.index("  setContext(snapshot)") : source.index("  render(snapshot", source.index("  setContext(snapshot)"))]
    assert "this.routingDrafts.clear()" in set_context
    assert "this.advancedDrafts.clear()" in set_context
    assert "this.previewByPackage.clear()" in set_context
