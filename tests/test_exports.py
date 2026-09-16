from __future__ import annotations

import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import threading
from types import SimpleNamespace
from urllib.request import Request, urlopen

import pytest
import xml.etree.ElementTree as ET
import yaml

from execraft.cli import main as cli_main
from execraft.export import ExportFormat, ExportTheme, ProjectExportService
from execraft.gui.downloads import BinaryDownload
from execraft.gui.routes import exports as export_routes
from execraft.gui.server import _handler_factory
from execraft.project_execution.models import (
    GateCriterion,
    MilestoneRequirements,
    ProjectExecutionDefinition,
    ProjectGate,
    ProjectMilestone,
    ProjectPhase,
    ProjectSchedule,
    ProjectTask,
)
from execraft.project_execution.repository import ProjectExecutionRepository
from execraft.roadmap import RoadmapService


def _project(root: Path) -> Path:
    project = root / "projects" / "sample"
    task = project / "tasks" / "build"
    task.mkdir(parents=True)
    (project / "project.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 2,
                "project": "sample",
                "description": "Autonomy platform",
                "repositories": [
                    {
                        "id": "app",
                        "path": ".",
                        "base_branch": "main",
                        "workspace_name": "app",
                        "required": True,
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (task / "TASK.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 2,
                "id": "build",
                "project": "sample",
                "title": "Build autonomous navigation",
                "status": "in_progress",
                "created_at": "2026-09-11T00:00:00+00:00",
                "git": {"branch_name": "task/build", "merge_strategy": "squash"},
                "repositories": [
                    {
                        "id": "app",
                        "base_branch": "main",
                        "task_branch": "task/build",
                    }
                ],
                "integration": {"verify": []},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (task / "PLAN.graph.yaml").write_text(
        """schema_version: 1
work_packages:
  - id: WP1
    title: Navigation foundation
    priority: 10
    risk: medium
    requirements: [Implement navigation]
    affected_repositories: [app]
    acceptance_criteria:
      - id: A1
        description: Navigation tests pass
  - id: WP2
    title: Integrate planner
    dependencies: [WP1]
    requirements: [Integrate planner]
    affected_repositories: [app]
    acceptance_criteria:
      - id: A2
        description: Planner is integrated
""",
        encoding="utf-8",
    )
    return project


def _roadmap(root: Path, state_root: Path) -> str:
    service = RoadmapService(
        control_root=root,
        state_root=state_root,
        active_task=lambda: None,
    )
    created = service.create("sample", title="Autonomy 2026", description="Navigation delivery")
    current = service.link_task(
        "sample",
        created["id"],
        expected_revision=created["revision"],
        task_id="build",
        lane="Autonomy",
        target="2026-10-15",
    )
    current = service.upsert_item(
        "sample",
        current["id"],
        expected_revision=current["revision"],
        raw_item={
            "kind": "planned_task",
            "title": "Field validation",
            "lane": "Validation",
            "schedule": {"target": "2026-11-05"},
        },
    )
    return current["id"]


def _project_execution(project: Path) -> None:
    definition = ProjectExecutionDefinition(
        project="sample",
        phases=(
            ProjectPhase(
                id="foundation",
                title="Foundation",
                schedule=ProjectSchedule(start="2026-09-01", target="2026-10-31"),
                tasks=("build",),
                exit_gates=("integration-ready",),
                milestones=("navigation-mvp",),
            ),
        ),
        tasks=(ProjectTask(task_id="build", phase="foundation"),),
        gates=(
            ProjectGate(
                id="integration-ready",
                title="Integration Ready",
                criteria=(GateCriterion(type="task_completion", task_id="build"),),
            ),
        ),
        milestones=(
            ProjectMilestone(
                id="navigation-mvp",
                title="Navigation MVP",
                target="2026-10-31",
                requires=MilestoneRequirements(
                    tasks=("build",),
                    gates=("integration-ready",),
                ),
            ),
        ),
    )
    ProjectExecutionRepository(project).create(definition)


def test_roadmap_svg_and_pdf_are_deterministic_vector_artifacts(tmp_path: Path) -> None:
    project = _project(tmp_path)
    roadmap_id = _roadmap(tmp_path, tmp_path / "state")
    _project_execution(project)
    service = ProjectExportService(control_root=tmp_path, state_root=tmp_path / "state")

    svg = service.roadmap("sample", roadmap_id, format=ExportFormat.SVG, theme=ExportTheme.DARK)
    assert svg.content_type.startswith("image/svg+xml")
    assert svg.filename.endswith("-roadmap.svg")
    text = svg.content.decode("utf-8")
    assert text.startswith("<svg")
    ET.fromstring(text)
    assert "Build autonomous navigation" in text
    assert "Autonomy" in text
    assert "Generated by Execraft" in text

    pdf = service.roadmap("sample", roadmap_id, format=ExportFormat.PDF)
    assert pdf.content_type == "application/pdf"
    assert pdf.content.startswith(b"%PDF-1.4")
    assert b"xref" in pdf.content
    assert len(pdf.content) > 1500


def test_task_svg_and_pdf_include_work_package_details(tmp_path: Path) -> None:
    _project(tmp_path)
    service = ProjectExportService(control_root=tmp_path, state_root=tmp_path / "state")

    svg = service.task_report("sample", "build", format=ExportFormat.SVG)
    text = svg.content.decode("utf-8")
    assert "Navigation foundation" in text
    assert "Integrate planner" in text
    assert "WORK PACKAGES" in text

    pdf = service.task_report("sample", "build", format=ExportFormat.PDF)
    assert pdf.content.startswith(b"%PDF-1.4")
    assert b"Navigation foundation" in pdf.content
    assert b"Integrate planner" in pdf.content


def test_project_execution_report_is_multi_domain_but_read_only(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _project_execution(project)
    runtime_path = tmp_path / "state" / "project-execution" / "sample" / "state.json"
    service = ProjectExportService(control_root=tmp_path, state_root=tmp_path / "state")

    artifact = service.project_report("sample")

    assert artifact.filename == "sample-project-execution-report.pdf"
    assert artifact.content.startswith(b"%PDF-1.4")
    assert b"Foundation" in artifact.content
    assert b"Integration Ready" in artifact.content
    assert b"Navigation MVP" in artifact.content
    # Export projection must never manufacture/reconcile runtime state.
    assert not runtime_path.exists()


def test_export_gui_route_returns_binary_download(tmp_path: Path) -> None:
    project = _project(tmp_path)
    roadmap_id = _roadmap(tmp_path, tmp_path / "state")
    _project_execution(project)
    service = SimpleNamespace(
        exports=ProjectExportService(control_root=tmp_path, state_root=tmp_path / "state")
    )
    result = export_routes.dispatch_get(
        service,
        "/api/export/roadmap",
        {
            "project_id": ["sample"],
            "roadmap_id": [roadmap_id],
            "format": ["svg"],
            "theme": ["executive"],
        },
    )
    assert isinstance(result, BinaryDownload)
    assert result.filename.endswith(".svg")
    assert result.content_type.startswith("image/svg+xml")
    assert result.content.startswith(b"<svg")



def test_export_http_transport_streams_authenticated_attachment(tmp_path: Path) -> None:
    project = _project(tmp_path)
    roadmap_id = _roadmap(tmp_path, tmp_path / "state")
    _project_execution(project)
    service = SimpleNamespace(
        exports=ProjectExportService(control_root=tmp_path, state_root=tmp_path / "state")
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_factory(service, "secret"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        request = Request(
            f"http://{host}:{port}/api/export/roadmap?project_id=sample&roadmap_id={roadmap_id}&format=svg&theme=executive",
            headers={"X-Execraft-Token": "secret"},
        )
        with urlopen(request, timeout=5) as response:
            body = response.read()
            assert response.status == 200
            assert response.headers["Content-Type"].startswith("image/svg+xml")
            assert response.headers["Content-Disposition"].startswith("attachment; filename=")
            assert response.headers["Cache-Control"] == "no-store"
            assert body.startswith(b"<svg")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

def test_task_runtime_state_overlays_plan_progress(tmp_path: Path) -> None:
    _project(tmp_path)
    state = tmp_path / "state" / "projects" / "build"
    state.mkdir(parents=True)
    state.joinpath("state.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project_id": "sample",
                "state": "running",
                "plan_graph": {
                    "work_packages": [
                        {
                            "id": "WP1",
                            "title": "Navigation foundation",
                            "requirements": ["Implement navigation"],
                            "acceptance_criteria": [
                                {"id": "A1", "description": "Done", "verified": True}
                            ],
                            "stage": "completed",
                            "status": "completed",
                        },
                        {
                            "id": "WP2",
                            "title": "Integrate planner",
                            "requirements": ["Integrate planner"],
                            "acceptance_criteria": [
                                {"id": "A2", "description": "Done", "verified": False}
                            ],
                            "stage": "implement",
                            "status": "running",
                        },
                    ]
                },
                "completed_packages": 1,
                "total_packages": 2,
            }
        ),
        encoding="utf-8",
    )
    service = ProjectExportService(control_root=tmp_path, state_root=tmp_path / "state")
    model = service.projection.task_report("sample", "build")
    assert model.progress_percent == 50
    assert model.runtime_state == "running"
    assert model.work_packages[0].progress_label == "Complete"
    assert model.work_packages[0].acceptance_verified == 1


def test_cli_export_writes_requested_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project(tmp_path)
    roadmap_id = _roadmap(tmp_path, tmp_path / "state")
    _project_execution(project)
    output = tmp_path / "out" / "roadmap.svg"
    monkeypatch.setenv("EXECRAFT_CONTROL_ROOT", str(tmp_path))

    rc = cli_main([
        "export",
        "roadmap",
        "--project",
        "sample",
        "--roadmap",
        roadmap_id,
        "--format",
        "svg",
        "--theme",
        "executive",
        "--state-dir",
        str(tmp_path / "state"),
        "--output",
        str(output),
    ])

    assert rc == 0
    assert output.read_bytes().startswith(b"<svg")
