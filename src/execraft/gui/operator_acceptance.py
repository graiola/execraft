"""GUI workflow for explicit package-scoped risk acceptance.

The dashboard exposes this only as a late human-decision hold operator decision. The canonical
mutation is still performed by the orchestration CLI so GUI and terminal flows
share locking, stale-action checks, persistence, and audit semantics.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Mapping

from execraft.gui.errors import GuiError
from execraft.orchestrate.identity import OrchestrationStorageIdentity
from execraft.orchestrate.models import TaskExecutionState, TaskExecutionStateRecord
from execraft.orchestrate.operator_acceptance import operator_acceptance_preview


class OperatorAcceptanceDashboardMixin:
    """Preview and execute explicit acceptance of one late human-decision hold."""

    process: Any
    manual_agent_console: Any
    state_root: Path
    root: Path
    project_id: str
    task_id: str
    storage_identity: OrchestrationStorageIdentity
    _workspace_action_lock: Any
    _load_state: Callable[[], TaskExecutionStateRecord | None]
    _command_environment: Callable[[], dict[str, str]]
    _latest_human_action: Callable[[], dict[str, Any]]

    def _operator_acceptance_offer(
        self, action: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        record = self._load_state()
        if record is None or record.state != TaskExecutionState.HUMAN_REQUIRED:
            return {"available": False}
        current = dict(action or self._latest_human_action())
        package_id = str(current.get("package_id", "")).strip()
        if not package_id:
            return {"available": False}
        try:
            package = record.plan_graph.package_by_id(package_id)
        except Exception:
            return {"available": False}
        return operator_acceptance_preview(current, package)

    def operator_acceptance_preview(self, package_id: str) -> dict[str, Any]:
        requested = str(package_id).strip()
        if not requested:
            raise GuiError("package_id is required")
        action = self._latest_human_action()
        active = str(action.get("package_id", "")).strip()
        if active != requested:
            raise GuiError(
                "the requested Work Package is no longer the active human-required action; refresh the dashboard"
            )
        preview = self._operator_acceptance_offer(action)
        if not preview.get("available"):
            raise GuiError(
                str(preview.get("unavailable_reason"))
                or "this human-required action cannot be accepted"
            )
        return preview

    def accept_operator_risk(
        self,
        package_id: str,
        *,
        reason: str,
        expected_sequence: int | None,
        acknowledged: bool,
    ) -> dict[str, Any]:
        """Record the decision through the CLI and automatically continue."""

        package_id = str(package_id).strip()
        rationale = str(reason).strip()
        if not package_id:
            raise GuiError("package_id is required")
        if not acknowledged:
            raise GuiError(
                "acknowledge that the deferred review/acceptance remains unverified"
            )
        if not rationale:
            raise GuiError("record a reason for accepting the deferred risk")
        if len(rationale) > 4000:
            raise GuiError("acceptance reason exceeds 4000 characters")
        if not self._workspace_action_lock.acquire(blocking=False):
            raise GuiError("another workspace action is already in progress")
        try:
            run_status = self.process.status()
            if run_status["owned_running"] or run_status["external_running"]:
                raise GuiError("stop the orchestrator before accepting a deferred check")
            if self.manual_agent_console.any_running():
                raise GuiError(
                    "stop standalone agent consoles before accepting a deferred check"
                )
            preview = self.operator_acceptance_preview(package_id)
            if expected_sequence is None or preview.get("sequence") != expected_sequence:
                raise GuiError(
                    "the human-required action changed after the dialog was opened; refresh and review it again"
                )
            command = [
                sys.executable,
                "-m",
                "execraft.cli",
                "orchestrate",
                "accept-risk",
                "--project",
                self.project_id,
                "--task-id",
                self.task_id,
                "--state-dir",
                str(self.state_root),
                "--package-id",
                package_id,
                "--accept-reason",
                rationale,
                "--expected-action-sequence",
                str(expected_sequence),
                "--acknowledge-unverified",
            ]
            result = subprocess.run(
                command,
                cwd=self.root,
                env=self._command_environment(),
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                raise GuiError(
                    result.stderr.strip()
                    or result.stdout.strip()
                    or "recording operator risk acceptance failed"
                )
            record = self._load_state()
            driver: dict[str, Any] = {}
            if record is not None and record.state == TaskExecutionState.RUNNING:
                driver = self.process.start()
            return {
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "package_id": package_id,
                "run": driver,
            }
        finally:
            self._workspace_action_lock.release()
