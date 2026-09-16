"""Application service for SVG/PDF Roadmap and project/task exports."""
from __future__ import annotations

from pathlib import Path

from .models import ExportArtifact, ExportFormat, ExportTheme
from .pdf import PdfRenderer
from .projection import ExportProjectionBuilder
from .svg import SvgRenderer


class ProjectExportService:
    """Read-only export facade shared by CLI and GUI transports."""

    def __init__(self, *, control_root: Path, state_root: Path) -> None:
        self.projection = ExportProjectionBuilder(
            control_root=control_root,
            state_root=state_root,
        )
        self.svg = SvgRenderer()
        self.pdf = PdfRenderer()

    def roadmap(
        self,
        project_id: str,
        roadmap_id: str,
        *,
        format: ExportFormat = ExportFormat.SVG,
        theme: ExportTheme = ExportTheme.DARK,
    ) -> ExportArtifact:
        model = self.projection.roadmap(project_id, roadmap_id)
        stem = f"{model.project_id}-{model.roadmap_id}-roadmap"
        if format == ExportFormat.SVG:
            return ExportArtifact(
                filename=f"{stem}.svg",
                content_type="image/svg+xml; charset=utf-8",
                content=self.svg.roadmap(model, theme=theme),
            )
        return ExportArtifact(
            filename=f"{stem}.pdf",
            content_type="application/pdf",
            content=self.pdf.roadmap(model),
        )

    def project_report(self, project_id: str) -> ExportArtifact:
        model = self.projection.project_report(project_id)
        return ExportArtifact(
            filename=f"{model.project_id}-project-execution-report.pdf",
            content_type="application/pdf",
            content=self.pdf.project_report(model),
        )

    def task_report(
        self,
        project_id: str,
        task_id: str,
        *,
        format: ExportFormat = ExportFormat.PDF,
        theme: ExportTheme = ExportTheme.DARK,
    ) -> ExportArtifact:
        model = self.projection.task_report(project_id, task_id)
        stem = f"{model.project_id}-{model.task_id}-task-report"
        if format == ExportFormat.SVG:
            return ExportArtifact(
                filename=f"{stem}.svg",
                content_type="image/svg+xml; charset=utf-8",
                content=self.svg.task(model, theme=theme),
            )
        return ExportArtifact(
            filename=f"{stem}.pdf",
            content_type="application/pdf",
            content=self.pdf.task_report(model),
        )
