"""Versioned, extensible onboarding template catalog.

Templates materialize into a staging directory.  They are intentionally unaware
of registration, locks, and final target paths; transactions own those concerns.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Generic, Iterable, TypeVar, cast


ContextT = TypeVar("ContextT")
TemplateRenderer = Callable[[ContextT, Path], None]


class TemplateCatalogError(ValueError):
    """Raised when a requested template is missing or duplicated."""


@dataclass(frozen=True)
class TemplateDescriptor(Generic[ContextT]):
    id: str
    version: int
    kind: str
    description: str
    renderer: TemplateRenderer[ContextT]

    @property
    def reference(self) -> str:
        return f"{self.id}@{self.version}"


class TemplateCatalog:
    """In-memory registry of versioned project and task templates."""

    def __init__(self) -> None:
        self._templates: dict[tuple[str, str, int], TemplateDescriptor[object]] = {}

    def register(self, descriptor: TemplateDescriptor[ContextT]) -> None:
        if descriptor.version < 1:
            raise TemplateCatalogError("template version must be positive")
        key = (descriptor.kind, descriptor.id, descriptor.version)
        if key in self._templates:
            raise TemplateCatalogError(
                f"duplicate {descriptor.kind} template: {descriptor.reference}"
            )
        self._templates[key] = cast(TemplateDescriptor[object], descriptor)

    def get(self, *, kind: str, template_id: str) -> TemplateDescriptor[ContextT]:
        name, separator, raw_version = template_id.partition("@")
        if separator:
            if not raw_version.isdigit() or int(raw_version) < 1:
                raise TemplateCatalogError(f"invalid template reference: {template_id!r}")
            key = (kind, name, int(raw_version))
            descriptor = self._templates.get(key)
        else:
            candidates = [
                item
                for (item_kind, item_id, _version), item in self._templates.items()
                if item_kind == kind and item_id == name
            ]
            descriptor = max(candidates, key=lambda item: item.version) if candidates else None
        if descriptor is None:
            available = ", ".join(self.ids(kind=kind)) or "none"
            raise TemplateCatalogError(
                f"unknown {kind} template {template_id!r}; available: {available}"
            )
        return cast(TemplateDescriptor[ContextT], descriptor)

    def ids(self, *, kind: str) -> tuple[str, ...]:
        return tuple(
            item.reference for item in self.descriptors(kind=kind)
        )

    def descriptors(self, *, kind: str | None = None) -> tuple[TemplateDescriptor[object], ...]:
        values: Iterable[TemplateDescriptor[object]] = self._templates.values()
        if kind is not None:
            values = (item for item in values if item.kind == kind)
        return tuple(sorted(values, key=lambda item: (item.kind, item.id, item.version)))

    def materialize(
        self,
        *,
        kind: str,
        template_id: str,
        context: ContextT,
        destination: Path,
    ) -> TemplateDescriptor[ContextT]:
        descriptor = self.get(kind=kind, template_id=template_id)
        destination.mkdir(parents=True, exist_ok=True)
        if any(destination.iterdir()):
            raise TemplateCatalogError(f"template destination is not empty: {destination}")
        descriptor.renderer(context, destination)
        return descriptor


@dataclass(frozen=True)
class TaskTemplateContext:
    title: str
    brief: str = ""
    template_directory: Path | None = None


_TASK_TEMPLATE_DEFAULTS = {
    "README.md": (
        "# Task dossier: {{ title }}\n\n"
        "See `BRIEF.md`, `PLAN.md`, `PLAN.graph.yaml`, `TASK.yaml`, `DEFINITION.yaml`, and `REVIEW.md` "
        "for the versioned task contract. `HANDOFF.md` is append-only engineering "
        "history. `RUNTIME_STATUS.md` is generated locally from durable state.\n"
    ),
    ".gitignore": "/RUNTIME_STATUS.md\n",
    "BRIEF.md": "# Brief: {{ title }}\n\n## Intent\n{{ brief }}\n",
    "PLAN.md": "# Plan: {{ title }}\n\nSee `PLAN.graph.yaml` for the executable graph.\n",
    "HANDOFF.md": (
        "# Engineering history: {{ title }}\n\n"
        "> Historical append-only record. Read `RUNTIME_STATUS.md` for live progress.\n"
    ),
    "REVIEW.md": "# Review: {{ title }}\n\n## Findings\n",
}


def render_default_task(context: TaskTemplateContext, destination: Path) -> None:
    """Materialize the built-in task dossier without side effects elsewhere."""

    normalized_brief = context.brief.strip()
    for name, fallback in _TASK_TEMPLATE_DEFAULTS.items():
        source = (context.template_directory / name) if context.template_directory else None
        template = (
            source.read_text(encoding="utf-8")
            if source is not None and source.is_file()
            else fallback
        )
        has_brief_placeholder = "{{ brief }}" in template
        content = template.replace("{{ title }}", context.title)
        content = content.replace("{{ brief }}", normalized_brief)
        if name == "BRIEF.md" and normalized_brief and not has_brief_placeholder:
            content = content.rstrip() + f"\n\n## Initial request\n\n{normalized_brief}\n"
        (destination / name).write_text(content, encoding="utf-8")


def default_task_template() -> TemplateDescriptor[TaskTemplateContext]:
    return TemplateDescriptor(
        id="standard",
        version=1,
        kind="task",
        description="Standard Execraft task dossier.",
        renderer=render_default_task,
    )
