"""Human-readable task runtime status derived from the durable orchestrator state.

The versioned task dossier describes intent and execution contracts. Runtime state
is host-local and authoritative under ``~/.local/state/execraft``. This module renders
that state into an ignored ``RUNTIME_STATUS.md`` beside the dossier so operators can
inspect progress without reverse-engineering JSON or mistaking ``HANDOFF.md`` for a
live status file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from execraft.persistence.atomic import atomic_write_text

from .identity import resolve_storage_identity
from .models import TaskExecutionStateRecord, WorkPackage, WorkPackageStage

RUNTIME_STATUS_FILENAME = "RUNTIME_STATUS.md"


@dataclass(frozen=True)
class RuntimeStatusSnapshot:
    """Result of synchronizing a task's human-readable runtime status."""

    path: Path
    state_path: Path
    record: TaskExecutionStateRecord | None

    @property
    def has_state(self) -> bool:
        return self.record is not None


def _format_local_timestamp(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "not recorded"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        return text
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S%z")


def _one_line(value: str, *, limit: int = 160) -> str:
    text = " ".join(str(value or "").split()).replace("|", "\\|")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _package_label(package: WorkPackage | None) -> str:
    if package is None:
        return "none"
    return f"`{package.id}` — {package.title}"


def _active_packages(record: TaskExecutionStateRecord) -> list[WorkPackage]:
    return sorted(
        (
            package
            for package in record.plan_graph.work_packages
            if package.stage not in {WorkPackageStage.PREPARE, WorkPackageStage.COMPLETED}
        ),
        key=lambda package: (-package.priority, package.id),
    )


def _ready_packages(record: TaskExecutionStateRecord) -> list[WorkPackage]:
    return [
        package
        for package in record.plan_graph.ready_packages()
        if package.stage == WorkPackageStage.PREPARE
    ]


def render_runtime_status(
    record: TaskExecutionStateRecord | None,
    *,
    task_id: str,
    state_root: Path,
    project_id: str = "",
) -> str:
    """Render a deterministic Markdown view of the local orchestration state."""

    state_root = Path(state_root).expanduser().resolve()
    identity = resolve_storage_identity(
        state_root,
        project_id=project_id,
        task_id=task_id,
    )
    state_dir = identity.state_dir
    state_path = state_dir / "state.json"
    journal_path = identity.journal_path
    log_path = state_dir / "orchestrator.log"
    artifacts_path = state_dir / "agent-artifacts"

    lines = [
        f"# Runtime status: `{task_id}`",
        "",
        "> **Generated file — do not edit.** The authoritative runtime state is",
        f"> `{state_path}`. Regenerate with `execraft task sync-status {task_id}`.",
        "",
    ]

    if record is None:
        lines.extend(
            [
                "## Orchestration",
                "",
                "No local orchestration state exists yet.",
                "",
                "Initialize the task with:",
                "",
                "```bash",
                "execraft orchestrate init \\",
                "  --project <project-id> \\",
                f"  --task-id {task_id} \\",
                "  --plan-file <task-dossier>/PLAN.graph.yaml",
                "```",
                "",
            ]
        )
    else:
        active = _active_packages(record)
        ready = _ready_packages(record)
        current = active[0] if active else None
        next_package = ready[0] if ready else None
        lines.extend(
            [
                "## Orchestration",
                "",
                f"- State: **`{record.state.value}`**",
                f"- Progress: **{record.completed_packages}/{record.total_packages} packages**",
                f"- Current package: {_package_label(current)}",
                (
                    f"- Current stage: **`{current.stage.value}`** "
                    f"(status `{current.status}`)"
                    if current is not None
                    else "- Current stage: none"
                ),
                f"- Next ready package: {_package_label(next_package)}",
                f"- Started: {_format_local_timestamp(record.started_at)}",
                f"- Last transition: {_format_local_timestamp(record.last_transition_at)}",
                "",
            ]
        )

        scheduler = dict(record.scheduler or {})
        parallel_wave = scheduler.get("parallel_wave")
        if isinstance(parallel_wave, dict) and parallel_wave:
            package_ids = ", ".join(
                f"`{item}`" for item in parallel_wave.get("package_ids", [])
            ) or "none"
            agents = ", ".join(
                f"`{item}`" for item in parallel_wave.get("agents", [])
            ) or "none"
            lines.extend(
                [
                    "## Active parallel wave",
                    "",
                    f"- Wave: `{parallel_wave.get('wave_id', '')}`",
                    f"- Started: {_format_local_timestamp(str(parallel_wave.get('started_at', '')))}",
                    f"- Packages: {package_ids}",
                    f"- Agents: {agents}",
                    "",
                ]
            )

        if record.waiting:
            waiting = dict(record.waiting)
            lines.extend(
                [
                    "## Waiting",
                    "",
                    f"- Package: `{waiting.get('package_id', '')}`",
                    f"- Stage: `{waiting.get('stage', '')}`",
                    f"- Capability: `{waiting.get('capability', '')}`",
                    f"- Poll cycle: {waiting.get('cycle', 0)}",
                    f"- Next check: {_format_local_timestamp(str(waiting.get('next_check_at', '')))}",
                ]
            )
            candidates = waiting.get("candidates") or []
            if isinstance(candidates, list) and candidates:
                lines.extend(["- Candidates:"])
                for candidate in candidates:
                    if not isinstance(candidate, dict):
                        continue
                    provider = candidate.get("agent_id") or "unconfigured"
                    reason = candidate.get("reason") or "unavailable"
                    available_at = candidate.get("available_at")
                    suffix = (
                        f" until {_format_local_timestamp(str(available_at))}"
                        if available_at
                        else ""
                    )
                    lines.append(f"  - `{provider}`: {reason}{suffix}")
            lines.append("")

        completed = [
            package
            for package in record.plan_graph.work_packages
            if package.stage == WorkPackageStage.COMPLETED
        ]
        if completed:
            lines.extend(
                [
                    "## Completed packages",
                    "",
                    "| Package | Title | Implementation summary |",
                    "|---|---|---|",
                ]
            )
            for package in completed:
                summary = _one_line(package.implementation_summary) or "—"
                lines.append(
                    f"| `{package.id}` | {_one_line(package.title, limit=100)} | {summary} |"
                )
            lines.append("")

        pending = [
            package
            for package in record.plan_graph.work_packages
            if package.stage != WorkPackageStage.COMPLETED
        ]
        if pending:
            lines.extend(
                [
                    "## Remaining packages",
                    "",
                    "| Package | Mode | Parent | Stage | Status | Complexity |",
                    "|---|---|---|---|---|---:|",
                ]
            )
            for package in pending:
                lines.append(
                    f"| `{package.id}` | `{package.execution_mode}` | "
                    f"`{package.parent_id or '—'}` | `{package.stage.value}` | "
                    f"`{package.status}` | {package.complexity_score()} |"
                )
            lines.append("")

    lines.extend(
        [
            "## Durable runtime data",
            "",
            f"- State: `{state_path}`",
            f"- Event journal: `{journal_path}`",
            f"- Progress log: `{log_path}`",
            f"- Agent artifacts: `{artifacts_path}`",
            "",
            "`HANDOFF.md` is an append-only engineering history. It is not the",
            "authoritative source for the current package or orchestration stage.",
            "",
        ]
    )
    return "\n".join(lines)


def write_runtime_status(
    dossier_dir: Path,
    record: TaskExecutionStateRecord | None,
    *,
    task_id: str,
    state_root: Path,
    project_id: str = "",
) -> Path:
    """Atomically write ``RUNTIME_STATUS.md`` beside the versioned dossier."""

    dossier_dir = Path(dossier_dir).resolve()
    dossier_dir.mkdir(parents=True, exist_ok=True)
    path = dossier_dir / RUNTIME_STATUS_FILENAME
    atomic_write_text(
        path,
        render_runtime_status(
            record,
            task_id=task_id,
            state_root=state_root,
            project_id=project_id,
        ),
    )
    return path


def sync_runtime_status(
    dossier_dir: Path,
    *,
    task_id: str,
    state_root: Path,
    project_id: str = "",
) -> RuntimeStatusSnapshot:
    """Load durable state when present and refresh the local Markdown view."""

    state_root = Path(state_root).expanduser().resolve()
    identity = resolve_storage_identity(
        state_root,
        project_id=project_id,
        task_id=task_id,
    )
    state_path = identity.state_dir / "state.json"
    record: TaskExecutionStateRecord | None = None
    if state_path.is_file():
        data: Any = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"orchestrator state must be a JSON object: {state_path}")
        record = TaskExecutionStateRecord.from_mapping(data)
    path = write_runtime_status(
        dossier_dir,
        record,
        task_id=task_id,
        state_root=state_root,
        project_id=project_id,
    )
    return RuntimeStatusSnapshot(path=path, state_path=state_path, record=record)
