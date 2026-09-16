"""Read-only routes for presentation-quality SVG/PDF exports."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from execraft.export import ExportFormat, ExportTheme
from execraft.gui.downloads import BinaryDownload
from execraft.gui.errors import GuiError

from .onboarding import _MISSING, _query_value

PROTECTED_GET_PATHS = frozenset({
    "/api/export/roadmap",
    "/api/export/project",
    "/api/export/task",
})


def _choice(query: Mapping[str, list[str]], key: str, allowed: set[str], default: str) -> str:
    value = _query_value(query, key) or default
    if value not in allowed:
        raise GuiError(f"{key} must be one of: {', '.join(sorted(allowed))}")
    return value


def dispatch_get(service: Any, path: str, query: Mapping[str, list[str]]) -> Any:
    if path not in PROTECTED_GET_PATHS:
        return _MISSING
    project_id = _query_value(query, "project_id")
    format = ExportFormat(_choice(query, "format", {"svg", "pdf"}, "pdf"))
    theme = ExportTheme(_choice(query, "theme", {"light", "dark", "executive"}, "dark"))
    exports = service.exports
    if path == "/api/export/roadmap":
        roadmap_id = _query_value(query, "roadmap_id")
        if not roadmap_id:
            raise GuiError("roadmap export requires roadmap_id")
        artifact = exports.roadmap(project_id, roadmap_id, format=format, theme=theme)
    elif path == "/api/export/project":
        if format != ExportFormat.PDF:
            raise GuiError("project report export currently supports PDF")
        artifact = exports.project_report(project_id)
    else:
        task_id = _query_value(query, "task_id")
        if not task_id:
            raise GuiError("task export requires task_id")
        artifact = exports.task_report(project_id, task_id, format=format, theme=theme)
    return BinaryDownload(artifact.filename, artifact.content_type, artifact.content)


def dispatch_post(service: Any, path: str, payload: Mapping[str, Any]) -> Any:
    del service, path, payload
    return _MISSING
