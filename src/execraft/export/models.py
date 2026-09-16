"""Provider-neutral presentation contracts for deterministic project exports.

Renderers consume only these immutable models. They never query Roadmap,
Project Execution, Task, or persistence repositories directly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class ExportError(ValueError):
    """Raised when an export request is invalid or cannot be rendered."""


class ExportFormat(str, Enum):
    SVG = "svg"
    PDF = "pdf"


class ExportTheme(str, Enum):
    LIGHT = "light"
    DARK = "dark"
    EXECUTIVE = "executive"


@dataclass(frozen=True)
class RoadmapNodePresentation:
    id: str
    kind: str
    title: str
    subtitle: str = ""
    lane: str = "General"
    order: int = 0
    state: str = ""
    health: str = ""
    progress_percent: int = 0
    start: str = ""
    target: str = ""
    current: bool = False


@dataclass(frozen=True)
class RoadmapEdgePresentation:
    source: str
    target: str
    kind: str = "blocks"


@dataclass(frozen=True)
class RoadmapPresentation:
    project_id: str
    roadmap_id: str
    title: str
    description: str
    revision: int
    generated_at: str
    nodes: tuple[RoadmapNodePresentation, ...]
    edges: tuple[RoadmapEdgePresentation, ...]
    lanes: tuple[str, ...]
    statistics: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkPackagePresentation:
    id: str
    title: str
    stage: str
    status: str
    risk: str
    priority: int
    progress_label: str
    dependencies: tuple[str, ...] = ()
    repositories: tuple[str, ...] = ()
    acceptance_total: int = 0
    acceptance_verified: int = 0


@dataclass(frozen=True)
class TaskReportPresentation:
    project_id: str
    task_id: str
    title: str
    status: str
    runtime_state: str
    progress_percent: int
    generated_at: str
    repositories: tuple[str, ...]
    work_packages: tuple[WorkPackagePresentation, ...]
    summary: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProjectReportPresentation:
    project_id: str
    title: str
    description: str
    generated_at: str
    mode: str
    held: bool
    phases: tuple[Mapping[str, Any], ...]
    gates: tuple[Mapping[str, Any], ...]
    milestones: tuple[Mapping[str, Any], ...]
    tasks: tuple[Mapping[str, Any], ...]
    statistics: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ExportArtifact:
    """One fully rendered immutable download artifact."""

    filename: str
    content_type: str
    content: bytes

    @property
    def size(self) -> int:
        return len(self.content)
