"""Small dependency-free vector PDF renderer for Execraft reports.

The backend intentionally uses only the PDF standard's built-in Helvetica
fonts and vector primitives. It is not a general document engine; it renders
bounded Execraft presentation contracts deterministically and offline.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .layout import RoadmapLayoutEngine
from .models import ProjectReportPresentation, RoadmapPresentation, TaskReportPresentation


def _pdf_text(value: object) -> str:
    text = str(value or "")
    # Standard Type-1 Helvetica is WinAnsi-like. Replace unsupported characters
    # instead of emitting malformed strings.
    text = text.encode("latin-1", "replace").decode("latin-1")
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _rgb(hex_value: str) -> tuple[float, float, float]:
    value = hex_value.lstrip("#")
    return tuple(int(value[i : i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]


@dataclass
class _PdfCanvas:
    width: float
    height: float
    commands: list[str]

    def text(self, x: float, y: float, value: object, *, size: float = 11, bold: bool = False) -> None:
        font = "/F2" if bold else "/F1"
        self.commands.append(
            f"BT {font} {size:.1f} Tf {x:.1f} {self.height - y:.1f} Td ({_pdf_text(value)}) Tj ET"
        )

    def line(self, x1: float, y1: float, x2: float, y2: float, *, width: float = 1.0) -> None:
        self.commands.append(
            f"{width:.2f} w {x1:.1f} {self.height - y1:.1f} m {x2:.1f} {self.height - y2:.1f} l S"
        )

    def rect(self, x: float, y: float, width: float, height: float, *, fill: str | None = None, stroke: str | None = None) -> None:
        if fill:
            r, g, b = _rgb(fill)
            self.commands.append(f"{r:.3f} {g:.3f} {b:.3f} rg")
        if stroke:
            r, g, b = _rgb(stroke)
            self.commands.append(f"{r:.3f} {g:.3f} {b:.3f} RG")
        op = "B" if fill and stroke else ("f" if fill else "S")
        self.commands.append(
            f"{x:.1f} {self.height - y - height:.1f} {width:.1f} {height:.1f} re {op}"
        )

    def color(self, value: str, *, stroke: bool = False) -> None:
        r, g, b = _rgb(value)
        self.commands.append(f"{r:.3f} {g:.3f} {b:.3f} {'RG' if stroke else 'rg'}")


class _PdfDocument:
    def __init__(self) -> None:
        self.pages: list[_PdfCanvas] = []

    def page(self, width: float, height: float) -> _PdfCanvas:
        canvas = _PdfCanvas(width, height, [])
        self.pages.append(canvas)
        return canvas

    def bytes(self) -> bytes:
        objects: list[bytes] = []
        # 1 catalog, 2 pages tree, 3 regular font, 4 bold font.
        objects.extend([
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"",  # filled after page objects are allocated
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
        ])
        kids: list[str] = []
        for canvas in self.pages:
            content = "\n".join(canvas.commands).encode("latin-1", "replace")
            content_id = len(objects) + 1
            objects.append(
                f"<< /Length {len(content)} >>\nstream\n".encode("ascii")
                + content
                + b"\nendstream"
            )
            page_id = len(objects) + 1
            kids.append(f"{page_id} 0 R")
            objects.append(
                (
                    f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {canvas.width:.1f} {canvas.height:.1f}] "
                    f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {content_id} 0 R >>"
                ).encode("ascii")
            )
        objects[1] = f"<< /Type /Pages /Count {len(self.pages)} /Kids [{' '.join(kids)}] >>".encode("ascii")
        output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0]
        for index, body in enumerate(objects, start=1):
            offsets.append(len(output))
            output.extend(f"{index} 0 obj\n".encode("ascii"))
            output.extend(body)
            output.extend(b"\nendobj\n")
        xref = len(output)
        output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
        output.extend(b"0000000000 65535 f \n")
        for offset in offsets[1:]:
            output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
        output.extend(
            (
                f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
                f"startxref\n{xref}\n%%EOF\n"
            ).encode("ascii")
        )
        return bytes(output)


class PdfRenderer:
    """Render poster and report PDFs from presentation models."""

    def __init__(self) -> None:
        self.layout_engine = RoadmapLayoutEngine()

    def roadmap(self, roadmap: RoadmapPresentation) -> bytes:
        layout = self.layout_engine.layout(roadmap)
        scale = min(1.0, 1120.0 / layout.width, 760.0 / layout.height)
        doc = _PdfDocument()
        page = doc.page(1191.0, 842.0)  # A3 landscape in points, approximately.
        page.rect(0, 0, page.width, page.height, fill="#f7f9fc")
        page.text(48, 48, f"{roadmap.project_id.upper()}  /  ROADMAP", size=11, bold=True)
        page.text(48, 76, roadmap.title[:88], size=23, bold=True)
        page.text(48, 98, f"Revision {roadmap.revision}  |  Generated {roadmap.generated_at[:10]}", size=9)
        ox, oy = 42.0, 120.0
        for lane_index, lane in enumerate(roadmap.lanes):
            y = oy + (layout.header_height + lane_index * layout.lane_height - layout.header_height) * scale
            page.rect(ox, y, (layout.width - 56) * scale, (layout.lane_height - 10) * scale, fill="#eef3f9")
            page.text(ox + 12, y + 24, lane.upper()[:24], size=8, bold=True)
        for edge in roadmap.edges:
            source = layout.node_index.get(edge.source)
            target = layout.node_index.get(edge.target)
            if not source or not target:
                continue
            x1 = ox + source.x * scale + source.width * scale
            y1 = oy + (source.y - layout.header_height) * scale + source.height * scale / 2
            x2 = ox + target.x * scale
            y2 = oy + (target.y - layout.header_height) * scale + target.height * scale / 2
            page.color("#91a4c4", stroke=True)
            page.line(x1, y1, x2, y2, width=0.7)
        colors = {
            "task": "#2878d9",
            "planned_task": "#7b8798",
            "phase": "#7655c7",
            "gate": "#d88914",
            "milestone": "#168c67",
        }
        for placed in layout.positioned:
            x = ox + placed.x * scale
            y = oy + (placed.y - layout.header_height) * scale
            w = placed.width * scale
            h = placed.height * scale
            page.rect(x, y, w, h, fill="#ffffff", stroke=colors.get(placed.node.kind, "#2878d9"))
            page.text(x + 8, y + 14, placed.node.kind.replace("_", " ").upper()[:18], size=6.5, bold=True)
            page.text(x + 8, y + 32, placed.node.title[:30], size=9.5, bold=True)
            page.text(x + 8, y + 47, placed.node.state.replace("_", " ")[:25], size=7)
            if placed.node.kind == "task":
                page.rect(x + 8, y + h - 9, max(0.1, (w - 16) * placed.node.progress_percent / 100), 3, fill=colors["task"])
        return doc.bytes()

    def project_report(self, report: ProjectReportPresentation) -> bytes:
        doc = _PdfDocument()
        page = doc.page(595.0, 842.0)  # A4 portrait.
        self._report_header(page, report.project_id, "PROJECT EXECUTION REPORT", report.generated_at)
        page.text(44, 92, report.title, size=21, bold=True)
        page.text(44, 118, f"Mode: {report.mode}  |  Held: {'yes' if report.held else 'no'}", size=10)
        y = 154.0
        cards = [
            ("Tasks", report.statistics.get("tasks", 0)),
            ("Completed", report.statistics.get("completed_tasks", 0)),
            ("Passed Gates", report.statistics.get("passed_gates", 0)),
            ("Milestones", report.statistics.get("achieved_milestones", 0)),
        ]
        x = 44.0
        for label, value in cards:
            page.rect(x, y, 116, 58, fill="#f2f5f9", stroke="#d9e1ec")
            page.text(x + 10, y + 20, label, size=8)
            page.text(x + 10, y + 43, value, size=18, bold=True)
            x += 126
        y = 244.0
        y = self._section(page, y, "PHASES", report.phases, ("title", "state", "health"))
        y = self._section(page, y + 12, "GATES", report.gates, ("title", "state"))
        if y > 690:
            page = doc.page(595.0, 842.0)
            self._report_header(page, report.project_id, "PROJECT EXECUTION REPORT", report.generated_at)
            y = 86.0
        y = self._section(page, y + 12, "MILESTONES", report.milestones, ("title", "state", "health"))
        if report.tasks:
            page = doc.page(595.0, 842.0)
            self._report_header(page, report.project_id, "TASK STATUS", report.generated_at)
            self._section(page, 88.0, "TASKS", report.tasks, ("title", "outcome", "phase", "verification"), max_rows=22)
        return doc.bytes()

    def task_report(self, task: TaskReportPresentation) -> bytes:
        doc = _PdfDocument()
        page = doc.page(595.0, 842.0)
        self._report_header(page, task.project_id, "TASK REPORT", task.generated_at)
        page.text(44, 92, task.title[:72], size=20, bold=True)
        page.text(44, 117, f"{task.task_id}  |  {task.runtime_state.replace('_', ' ')}", size=10)
        page.text(515, 92, f"{task.progress_percent}%", size=18, bold=True)
        page.rect(44, 136, 500, 7, fill="#e1e7ef")
        page.rect(44, 136, 500 * task.progress_percent / 100, 7, fill="#2878d9")
        page.text(44, 174, "WORK PACKAGES", size=10, bold=True)
        y = 196.0
        for index, package in enumerate(task.work_packages):
            if y > 770:
                page = doc.page(595.0, 842.0)
                self._report_header(page, task.project_id, "TASK REPORT / WORK PACKAGES", task.generated_at)
                y = 86.0
            page.rect(44, y, 500, 48, fill="#f8fafc", stroke="#d9e1ec")
            page.text(56, y + 18, package.title[:58], size=10, bold=True)
            page.text(56, y + 34, f"{package.id} | {package.progress_label} | risk {package.risk}", size=7.5)
            page.text(520, y + 26, f"{package.acceptance_verified}/{package.acceptance_total}", size=8, bold=True)
            y += 57
        return doc.bytes()

    @staticmethod
    def _report_header(page: _PdfCanvas, project_id: str, title: str, generated_at: str) -> None:
        page.text(44, 35, f"{project_id.upper()}  /  {title}", size=10, bold=True)
        page.text(550, 35, generated_at[:10], size=8)
        page.color("#d9e1ec", stroke=True)
        page.line(44, 48, 550, 48, width=0.8)

    @staticmethod
    def _section(
        page: _PdfCanvas,
        y: float,
        title: str,
        rows: Iterable[object],
        keys: tuple[str, ...],
        *,
        max_rows: int = 10,
    ) -> float:
        page.text(44, y, title, size=9.5, bold=True)
        y += 16
        for raw in list(rows)[:max_rows]:
            row = raw if isinstance(raw, dict) else dict(raw)  # type: ignore[arg-type]
            label = str(row.get(keys[0], row.get("id", "")))
            details = "  |  ".join(
                str(row.get(key, "")).replace("_", " ") for key in keys[1:] if row.get(key, "")
            )
            page.text(52, y, label[:46], size=8.5, bold=True)
            page.text(280, y, details[:58], size=7.5)
            y += 20
        return y
