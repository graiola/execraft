"""CLI execution for presentation-quality project exports."""
from __future__ import annotations

from pathlib import Path

from execraft.control_plane import ControlPlaneHome
from execraft.export import ExportFormat, ExportTheme, ProjectExportService
from execraft.persistence.atomic import atomic_write_bytes


def run_export_command(args) -> int:
    """Render a read-only presentation artifact from canonical project state."""

    home = ControlPlaneHome.resolve()
    state_root = (
        args.state_dir.expanduser().resolve()
        if getattr(args, "state_dir", None)
        else home.state_dir
    )
    service = ProjectExportService(control_root=home.root, state_root=state_root)
    format = ExportFormat(str(args.format))
    theme = ExportTheme(str(args.theme))
    if args.kind == "roadmap":
        if not str(args.roadmap_id or "").strip():
            raise ValueError("roadmap export requires --roadmap ROADMAP_ID")
        artifact = service.roadmap(
            args.project_id,
            args.roadmap_id,
            format=format,
            theme=theme,
        )
    elif args.kind == "project":
        if format != ExportFormat.PDF:
            raise ValueError("project report export currently supports --format pdf")
        artifact = service.project_report(args.project_id)
    else:
        if not str(args.task_id or "").strip():
            raise ValueError("task export requires --task TASK_ID")
        artifact = service.task_report(
            args.project_id,
            args.task_id,
            format=format,
            theme=theme,
        )
    output = (args.output or Path.cwd() / artifact.filename).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(output, artifact.content, mode=0o644)
    print(f"Exported {artifact.content_type}: {output}")
    return 0
