"""Argument parser for presentation-quality project exports."""
from __future__ import annotations

from pathlib import Path
from typing import Any


def add_export_command(sub: Any) -> None:
    export = sub.add_parser(
        "export",
        help="Export roadmap, project, or task views as SVG/PDF",
    )
    export.add_argument("kind", choices=["roadmap", "project", "task"])
    export.add_argument("--project", dest="project_id", required=True)
    export.add_argument("--roadmap", dest="roadmap_id", default="")
    export.add_argument("--task", dest="task_id", default="")
    export.add_argument("--format", choices=["svg", "pdf"], default="pdf")
    export.add_argument(
        "--theme",
        choices=["light", "dark", "executive"],
        default="dark",
        help="Visual theme used by SVG exports",
    )
    export.add_argument("--output", type=Path)
    export.add_argument("--state-dir", type=Path)
