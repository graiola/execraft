"""Append-only, fsync-backed Project Execution audit journal."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
from typing import Any

from execraft.persistence.locks import FileLock, LockLevel
from execraft.project import validate_project_id

from .models import ProjectExecutionError

PROJECT_EVENT_TYPES = frozenset(
    {
        "project_execution_started",
        "project_execution_paused",
        "project_task_ready",
        "project_task_start_requested",
        "project_task_started",
        "project_task_completed",
        "project_task_failed",
        "project_gate_ready",
        "project_gate_evaluation_started",
        "project_gate_passed",
        "project_gate_failed",
        "project_gate_decision_required",
        "project_gate_approved",
        "project_gate_rejected",
        "project_gate_waived",
        "project_phase_ready",
        "project_phase_activated",
        "project_phase_completed",
        "project_milestone_achieved",
        "project_definition_changed",
        "project_execution_reconciled",
        "project_automatic_cycle_started",
        "project_automatic_cycle_completed",
        "project_automatic_task_start_failed",
        "project_delivery_candidate_created",
        "project_delivery_started",
        "project_delivery_succeeded",
        "project_delivery_failed",
        "project_delivery_uncertain",
    }
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class ProjectEventJournal:
    """Durable project-domain event journal.

    Events are deliberately separate from Task orchestration events. A Project
    Execution reader can therefore evolve without teaching the Task journal
    about ProjectPhase, ProjectGate, or ProjectMilestone semantics.
    """

    def __init__(self, state_root: Path, project_id: str) -> None:
        safe_project_id = validate_project_id(project_id)
        self.directory = (
            Path(state_root).expanduser().resolve()
            / "project-execution"
            / safe_project_id
        )
        self.path = self.directory / "journal.jsonl"
        self.lock_path = self.directory / "journal.lock"

    def append(
        self,
        event_type: str,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if event_type not in PROJECT_EVENT_TYPES:
            raise ProjectExecutionError(
                f"unsupported project event type: {event_type}"
            )
        event = {
            "type": event_type,
            "timestamp": utc_now(),
            **dict(data or {}),
        }
        encoded = (
            json.dumps(
                event,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8")

        with FileLock(self.lock_path, level=LockLevel.RECORD):
            self.directory.mkdir(parents=True, exist_ok=True)
            self._assert_safe_existing_path()
            descriptor = os.open(self.path, self._append_flags(), 0o600)
            try:
                os.write(descriptor, encoded)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        return event

    def read(self) -> tuple[dict[str, Any], ...]:
        """Read valid complete JSON records, ignoring a crash-truncated tail."""

        if not self.path.exists():
            return ()
        self._assert_safe_existing_path()
        rows: list[dict[str, Any]] = []
        try:
            content = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ProjectExecutionError(
                f"cannot read Project Execution journal: {exc}"
            ) from exc
        for line in content.splitlines():
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except ValueError:
                # Append-only journals may end with a partial record after an
                # abrupt process/filesystem failure. Earlier records stay valid.
                continue
            if isinstance(raw, dict):
                rows.append(raw)
        return tuple(rows)

    def _assert_safe_existing_path(self) -> None:
        if not self.path.exists():
            return
        metadata = self.path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ProjectExecutionError(
                f"project journal path is unsafe: {self.path}"
            )

    @staticmethod
    def _append_flags() -> int:
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        return flags
