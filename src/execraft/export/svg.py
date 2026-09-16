"""High-resolution SVG renderers for Roadmap and Task presentation models."""
from __future__ import annotations

from html import escape

from .layout import RoadmapLayoutEngine
from .models import ExportTheme, RoadmapPresentation, TaskReportPresentation


_PALETTES = {
    ExportTheme.DARK: {
        "background": "#09111f",
        "panel": "#101b2d",
        "panel_alt": "#0d1727",
        "text": "#eef4ff",
        "muted": "#91a4c4",
        "grid": "#23324d",
        "accent": "#57d4ff",
        "task": "#4d8dff",
        "planned_task": "#60718d",
        "phase": "#8d68ff",
        "gate": "#ffb84d",
        "milestone": "#4ed6a6",
        "danger": "#ff687a",
    },
    ExportTheme.LIGHT: {
        "background": "#f7f9fc",
        "panel": "#ffffff",
        "panel_alt": "#eef3f9",
        "text": "#142033",
        "muted": "#64748b",
        "grid": "#d9e1ec",
        "accent": "#006fe6",
        "task": "#2878d9",
        "planned_task": "#7b8798",
        "phase": "#7655c7",
        "gate": "#d88914",
        "milestone": "#168c67",
        "danger": "#c7384e",
    },
    ExportTheme.EXECUTIVE: {
        "background": "#f3f1ec",
        "panel": "#fffdf8",
        "panel_alt": "#ebe7df",
        "text": "#171717",
        "muted": "#716b61",
        "grid": "#d7d1c7",
        "accent": "#8d6b2f",
        "task": "#2c5c83",
        "planned_task": "#837d73",
        "phase": "#72557d",
        "gate": "#a66b1f",
        "milestone": "#47795e",
        "danger": "#9b3845",
    },
}


def _safe(value: object) -> str:
    return escape(str(value or ""), quote=True)


def _truncate(value: str, limit: int) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: max(1, limit - 1)].rstrip() + "…"


class SvgRenderer:
    """Render vector-first exports without external browser/image dependencies."""

    def __init__(self) -> None:
        self.layout_engine = RoadmapLayoutEngine()

    def roadmap(self, roadmap: RoadmapPresentation, *, theme: ExportTheme) -> bytes:
        palette = _PALETTES[theme]
        layout = self.layout_engine.layout(roadmap)
        out: list[str] = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{layout.width:.0f}" height="{layout.height:.0f}" viewBox="0 0 {layout.width:.0f} {layout.height:.0f}" role="img">',
            "<defs>",
            '<filter id="shadow" x="-20%" y="-20%" width="140%" height="160%"><feDropShadow dx="0" dy="5" stdDeviation="7" flood-opacity="0.16"/></filter>',
            f'<linearGradient id="hero" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{palette["panel"]}"/><stop offset="1" stop-color="{palette["panel_alt"]}"/></linearGradient>',
            '<marker id="arrow" markerWidth="10" markerHeight="10" refX="9" refY="3" orient="auto" markerUnits="strokeWidth"><path d="M0,0 L0,6 L9,3 z" fill="currentColor"/></marker>',
            "</defs>",
            f'<rect width="100%" height="100%" fill="{palette["background"]}"/>',
            f'<rect x="28" y="24" width="{layout.width - 56:.0f}" height="86" rx="18" fill="url(#hero)" stroke="{palette["grid"]}"/>',
            f'<text x="56" y="58" font-family="Inter,Segoe UI,Arial,sans-serif" font-size="13" font-weight="700" letter-spacing="2" fill="{palette["accent"]}">{_safe(roadmap.project_id.upper())} · ROADMAP</text>',
            f'<text x="56" y="86" font-family="Inter,Segoe UI,Arial,sans-serif" font-size="26" font-weight="750" fill="{palette["text"]}">{_safe(_truncate(roadmap.title, 72))}</text>',
            f'<text x="{layout.width - 58:.0f}" y="58" text-anchor="end" font-family="Inter,Segoe UI,Arial,sans-serif" font-size="12" fill="{palette["muted"]}">rev {roadmap.revision} · {_safe(roadmap.generated_at[:10])}</text>',
        ]
        if roadmap.description:
            out.append(
                f'<text x="{layout.width - 58:.0f}" y="84" text-anchor="end" font-family="Inter,Segoe UI,Arial,sans-serif" font-size="12" fill="{palette["muted"]}">{_safe(_truncate(roadmap.description, 74))}</text>'
            )

        for lane_index, lane in enumerate(roadmap.lanes):
            y = layout.header_height + lane_index * layout.lane_height
            out.extend([
                f'<rect x="28" y="{y:.0f}" width="{layout.width - 56:.0f}" height="{layout.lane_height - 8:.0f}" rx="14" fill="{palette["panel_alt"]}" opacity="0.55"/>',
                f'<text x="54" y="{y + 42:.0f}" font-family="Inter,Segoe UI,Arial,sans-serif" font-size="12" font-weight="700" letter-spacing="1.4" fill="{palette["muted"]}">{_safe(lane.upper())}</text>',
                f'<line x1="{layout.lane_header_width:.0f}" y1="{y + 57:.0f}" x2="{layout.width - 44:.0f}" y2="{y + 57:.0f}" stroke="{palette["grid"]}" stroke-width="1" stroke-dasharray="4 8"/>',
            ])

        for edge in roadmap.edges:
            source = layout.node_index.get(edge.source)
            target = layout.node_index.get(edge.target)
            if source is None or target is None:
                continue
            x1 = source.x + source.width
            y1 = source.y + source.height / 2
            x2 = target.x
            y2 = target.y + target.height / 2
            bend = max(40.0, abs(x2 - x1) * 0.42)
            dash = ' stroke-dasharray="5 7"' if edge.kind == "related" else ""
            out.append(
                f'<path d="M{x1:.1f},{y1:.1f} C{x1 + bend:.1f},{y1:.1f} {x2 - bend:.1f},{y2:.1f} {x2:.1f},{y2:.1f}" fill="none" stroke="{palette["muted"]}" stroke-width="1.8" opacity="0.75" marker-end="url(#arrow)"{dash}/>'
            )

        for placed in layout.positioned:
            node = placed.node
            color = palette.get(node.kind, palette["task"])
            state = _truncate(node.state.replace("_", " ").upper(), 18)
            title = _truncate(node.title, 28)
            subtitle = _truncate(node.subtitle, 30)
            border = palette["accent"] if node.current else color
            out.extend([
                f'<g filter="url(#shadow)">',
                f'<rect x="{placed.x:.1f}" y="{placed.y:.1f}" width="{placed.width:.1f}" height="{placed.height:.1f}" rx="15" fill="{palette["panel"]}" stroke="{border}" stroke-width="{2.4 if node.current else 1.3}"/>',
                f'<rect x="{placed.x:.1f}" y="{placed.y:.1f}" width="5" height="{placed.height:.1f}" rx="3" fill="{color}"/>',
                f'<text x="{placed.x + 18:.1f}" y="{placed.y + 22:.1f}" font-family="Inter,Segoe UI,Arial,sans-serif" font-size="9.5" font-weight="700" letter-spacing="1.1" fill="{color}">{_safe(node.kind.replace("_", " ").upper())}</text>',
                f'<text x="{placed.x + 18:.1f}" y="{placed.y + 44:.1f}" font-family="Inter,Segoe UI,Arial,sans-serif" font-size="14" font-weight="700" fill="{palette["text"]}">{_safe(title)}</text>',
            ])
            if subtitle:
                out.append(
                    f'<text x="{placed.x + 18:.1f}" y="{placed.y + 62:.1f}" font-family="Inter,Segoe UI,Arial,sans-serif" font-size="10" fill="{palette["muted"]}">{_safe(subtitle)}</text>'
                )
            if state:
                out.append(
                    f'<text x="{placed.x + placed.width - 14:.1f}" y="{placed.y + 21:.1f}" text-anchor="end" font-family="Inter,Segoe UI,Arial,sans-serif" font-size="8.5" font-weight="700" fill="{palette["muted"]}">{_safe(state)}</text>'
                )
            if node.progress_percent > 0 or node.kind == "task":
                bar_x = placed.x + 18
                bar_y = placed.y + placed.height - 8
                bar_width = placed.width - 34
                fill_width = bar_width * node.progress_percent / 100.0
                out.extend([
                    f'<rect x="{bar_x:.1f}" y="{bar_y:.1f}" width="{bar_width:.1f}" height="3" rx="2" fill="{palette["grid"]}"/>',
                    f'<rect x="{bar_x:.1f}" y="{bar_y:.1f}" width="{fill_width:.1f}" height="3" rx="2" fill="{color}"/>',
                ])
            out.append("</g>")

        legend_y = layout.height - 28
        legend = (("task", "Task"), ("phase", "Phase"), ("gate", "Gate"), ("milestone", "Milestone"))
        x = 54.0
        for kind, label in legend:
            out.append(f'<circle cx="{x:.0f}" cy="{legend_y:.0f}" r="5" fill="{palette[kind]}"/>')
            out.append(f'<text x="{x + 10:.0f}" y="{legend_y + 4:.0f}" font-family="Inter,Segoe UI,Arial,sans-serif" font-size="10" fill="{palette["muted"]}">{label}</text>')
            x += 82
        out.append(
            f'<text x="{layout.width - 50:.0f}" y="{legend_y + 4:.0f}" text-anchor="end" font-family="Inter,Segoe UI,Arial,sans-serif" font-size="9" fill="{palette["muted"]}">Generated by Execraft · vector export</text>'
        )
        out.append("</svg>")
        return "".join(out).encode("utf-8")

    def task(self, task: TaskReportPresentation, *, theme: ExportTheme) -> bytes:
        palette = _PALETTES[theme]
        width = 1100
        row_h = 64
        height = max(620, 258 + len(task.work_packages) * row_h)
        out = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
            f'<rect width="100%" height="100%" fill="{palette["background"]}"/>',
            f'<rect x="36" y="30" width="1028" height="166" rx="20" fill="{palette["panel"]}" stroke="{palette["grid"]}"/>',
            f'<text x="64" y="66" font-family="Inter,Segoe UI,Arial,sans-serif" font-size="12" font-weight="700" letter-spacing="2" fill="{palette["accent"]}">{_safe(task.project_id.upper())} · TASK</text>',
            f'<text x="64" y="105" font-family="Inter,Segoe UI,Arial,sans-serif" font-size="29" font-weight="750" fill="{palette["text"]}">{_safe(_truncate(task.title, 60))}</text>',
            f'<text x="64" y="134" font-family="Inter,Segoe UI,Arial,sans-serif" font-size="12" fill="{palette["muted"]}">{_safe(task.task_id)} · {_safe(task.runtime_state.replace("_", " ").upper())}</text>',
            f'<text x="1000" y="69" text-anchor="end" font-family="Inter,Segoe UI,Arial,sans-serif" font-size="22" font-weight="700" fill="{palette["accent"]}">{task.progress_percent}%</text>',
            f'<rect x="64" y="157" width="936" height="7" rx="4" fill="{palette["grid"]}"/>',
            f'<rect x="64" y="157" width="{936 * task.progress_percent / 100:.1f}" height="7" rx="4" fill="{palette["accent"]}"/>',
            f'<text x="64" y="226" font-family="Inter,Segoe UI,Arial,sans-serif" font-size="12" font-weight="700" letter-spacing="1.6" fill="{palette["muted"]}">WORK PACKAGES</text>',
        ]
        y = 248
        for package in task.work_packages:
            done = package.progress_label == "Complete"
            color = palette["milestone"] if done else palette["task"]
            out.extend([
                f'<rect x="52" y="{y}" width="996" height="52" rx="12" fill="{palette["panel"]}" stroke="{palette["grid"]}"/>',
                f'<circle cx="76" cy="{y + 26}" r="7" fill="{color}"/>',
                f'<text x="96" y="{y + 22}" font-family="Inter,Segoe UI,Arial,sans-serif" font-size="13" font-weight="700" fill="{palette["text"]}">{_safe(_truncate(package.title, 58))}</text>',
                f'<text x="96" y="{y + 39}" font-family="Inter,Segoe UI,Arial,sans-serif" font-size="10" fill="{palette["muted"]}">{_safe(package.id)} · {_safe(package.progress_label)} · risk {_safe(package.risk)}</text>',
                f'<text x="1014" y="{y + 31}" text-anchor="end" font-family="Inter,Segoe UI,Arial,sans-serif" font-size="10" font-weight="700" fill="{color}">{package.acceptance_verified}/{package.acceptance_total} CRITERIA</text>',
            ])
            y += row_h
        out.append("</svg>")
        return "".join(out).encode("utf-8")
