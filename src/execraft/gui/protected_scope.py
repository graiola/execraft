"""Protected write-scope approval workflow for the local dashboard.

The dashboard never broadens an agent's permissions implicitly.  This module
projects the deterministic protected-path stop into an explicit operator action,
previews the authoritative current candidate set, and forwards approval through
the canonical orchestration CLI with an optimistic-concurrency guard.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Mapping

from execraft.gui.errors import GuiError
from execraft.orchestrate import SupervisorIncidentStore
from execraft.orchestrate.identity import OrchestrationStorageIdentity
from execraft.orchestrate.journal import EventJournal
from execraft.orchestrate.models import TaskExecutionState, TaskExecutionStateRecord
from execraft.orchestrate.operator_action import is_repository_scope_action


_PROTECTED_SCOPE_MARKERS = (
    "supervisor cannot autonomously acquire protected paths:",
    "scope recovery cannot approve protected paths:",
)


class ProtectedScopeDashboardMixin:
    """Dashboard behavior for explicit protected write-scope authorization."""

    process: Any
    manual_agent_console: Any
    supervisor_incidents: SupervisorIncidentStore
    state_root: Path
    root: Path
    project_id: str
    task_id: str
    storage_identity: OrchestrationStorageIdentity
    _workspace_action_lock: Any
    _load_state: Callable[[], TaskExecutionStateRecord | None]
    _command_environment: Callable[[], dict[str, str]]

    @staticmethod
    def _protected_scope_paths_from_text(text: str) -> list[str]:
        """Extract repository-qualified protected paths from a recovery error."""

        normalized = str(text or "").strip()
        lowered = normalized.lower()
        for marker in _PROTECTED_SCOPE_MARKERS:
            index = lowered.find(marker)
            if index < 0:
                continue
            suffix = normalized[index + len(marker) :]
            return [
                item.strip().rstrip(".;")
                for item in suffix.split(",")
                if ":" in item and item.strip()
            ]
        return []

    def _latest_human_action(self) -> dict[str, Any]:
        """Return the newest durable operator action with journal identity."""

        journal = EventJournal(self.storage_identity.journal_path)
        try:
            entries = journal.read()
        except (OSError, ValueError):
            return {}
        for entry in reversed(entries):
            if entry.event_type != "human_intervention_required":
                continue
            payload = dict(entry.payload)
            payload.setdefault("sequence", entry.sequence)
            payload.setdefault("timestamp", entry.timestamp)
            return payload
        return {}

    def _protected_scope_approval(self, action: Mapping[str, Any]) -> dict[str, Any]:
        """Project a protected-scope Supervisor stop into one operator action."""

        if not is_repository_scope_action(action):
            return {}
        package_id = str(action.get("package_id", "")).strip()
        if not package_id:
            return {}
        action_sequence = action.get("sequence")
        for incident in self.supervisor_incidents.recent(limit=8):
            if incident.package_id != package_id:
                continue
            if (
                action_sequence is not None
                and incident.escalation_sequence is not None
                and int(action_sequence) != incident.escalation_sequence
            ):
                continue
            paths = self._protected_scope_paths_from_text(incident.summary)
            if paths:
                return {
                    "available": True,
                    "package_id": package_id,
                    "paths": paths,
                    "reason": incident.summary,
                    "incident_id": incident.incident_id,
                }
        evidence = [
            str(action.get("reason", "")),
            *(str(item) for item in action.get("evidence", []) or []),
        ]
        for text in evidence:
            paths = self._protected_scope_paths_from_text(text)
            if paths:
                return {
                    "available": True,
                    "package_id": package_id,
                    "paths": paths,
                    "reason": text,
                    "incident_id": "",
                }
        return {}

    def protected_scope_run_control(self, action: Mapping[str, Any]) -> dict[str, Any]:
        """Return the special action-center control for a protected scope check."""

        approval = self._protected_scope_approval(action)
        if not approval.get("available"):
            return {}
        dirty_paths = self._currently_dirty_scope_paths(approval.get("paths", []))
        if not dirty_paths:
            # The path may have been restored or committed from another safe GUI
            # action while the driver was stopped.  Do not let the historical
            # incident mask the ordinary stale-Check reconciliation path: Run can
            # now revalidate the clean workspace and resume durably.
            return {}
        approval = {**approval, "paths": dirty_paths}
        return {
            "can_start": False,
            "label": "Protected scope approval required",
            "reason": (
                "The Supervisor determined that the current package needs protected "
                "workspace paths. Review and explicitly approve the exact current "
                "candidate set before resuming verification."
            ),
            "human_action": dict(action),
            "scope_approval": approval,
        }

    def _currently_dirty_scope_paths(self, paths: Any) -> list[str]:
        """Return the requested repository-qualified paths that are still dirty."""

        requested = {
            str(item).strip() for item in (paths or []) if str(item).strip()
        }
        if not requested:
            return []
        try:
            snapshot = self._workspace_manager().snapshot(driver_active=False)
        except Exception:
            # Fail closed when workspace inspection is unavailable.  The CLI
            # remains authoritative when the operator opens the preview.
            return sorted(requested)
        current = {
            f"{repository.get('id', '')}:{change.get('path', '')}"
            for repository in snapshot.get("repositories", [])
            if isinstance(repository, Mapping)
            for change in repository.get("changes", [])
            if isinstance(change, Mapping)
            and str(repository.get("id", "")).strip()
            and str(change.get("path", "")).strip()
        }
        return sorted(requested & current)

    def reject_protected_scope_commit_bypass(
        self,
        selections: Mapping[str, list[str]],
    ) -> None:
        """Keep protected-scope authorization separate from manual Git commits."""

        approval = self._protected_scope_approval(self._latest_human_action())
        if not approval.get("available"):
            return
        selected = {
            f"{repository_id}:{str(path).strip()}"
            for repository_id, paths in selections.items()
            for path in paths
            if str(repository_id).strip() and str(path).strip()
        }
        protected = sorted(selected & set(approval.get("paths", [])))
        if protected:
            raise GuiError(
                "protected scope candidates must be approved through the exact-scope "
                "action before they can be committed: " + ", ".join(protected)
            )

    def _ensure_idle_workspace_action(self, label: str) -> None:
        run_status = self.process.status()
        if run_status["owned_running"] or run_status["external_running"]:
            raise GuiError(f"stop the orchestrator before {label}")
        if self.manual_agent_console.any_running():
            raise GuiError(f"stop standalone agent consoles before {label}")

    def _scope_cli_json(
        self,
        package_id: str,
        *,
        accept: bool = False,
        expected_candidates: list[str] | None = None,
    ) -> dict[str, Any]:
        command = [
            sys.executable,
            "-m",
            "execraft.cli",
            "orchestrate",
            "scope",
            "--project",
            self.project_id,
            "--task-id",
            self.task_id,
            "--package-id",
            package_id,
            "--state-dir",
            str(self.state_root),
            "--json",
        ]
        if accept:
            command.extend(
                [
                    "--accept-scope",
                    "--expected-scope-json",
                    json.dumps(expected_candidates or [], separators=(",", ":")),
                ]
            )
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
                or "repository-scope operation failed"
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise GuiError("repository-scope command returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise GuiError("repository-scope command returned an invalid payload")
        return payload

    def scope_approval_preview(self, package_id: str) -> dict[str, Any]:
        """Return the authoritative candidate set for a protected-scope confirmation."""

        package_id = str(package_id).strip()
        if not package_id:
            raise GuiError("a package ID is required for scope approval")
        self._ensure_idle_workspace_action("previewing protected scope")
        record = self._load_state()
        approval = self._protected_scope_approval(self._latest_human_action())
        if record is None or record.state != TaskExecutionState.HUMAN_REQUIRED:
            raise GuiError(
                "protected scope can only be approved while human action is required"
            )
        if not approval.get("available") or approval.get("package_id") != package_id:
            raise GuiError("the active operator action is not a protected-scope approval")

        report = self._scope_cli_json(package_id)
        workspace_scope = report.get("workspace_scope") or {}
        candidates = workspace_scope.get("candidates") or []
        if not isinstance(candidates, list):
            candidates = []
        candidate_paths = [
            str(item.get("path", "")).strip()
            for item in candidates
            if isinstance(item, Mapping) and str(item.get("path", "")).strip()
        ]
        protected_paths = sorted(
            set(candidate_paths) & set(str(item) for item in approval.get("paths", []))
        )
        if not candidate_paths:
            raise GuiError(
                "the protected-scope condition has changed; refresh the dashboard "
                "instead of approving an empty candidate set"
            )
        if not protected_paths:
            raise GuiError(
                "the protected paths recorded by the Supervisor are no longer in the "
                "current candidate set; refresh before approving"
            )
        return {
            "package_id": package_id,
            "stage": str(report.get("stage", "")),
            "candidate_paths": candidate_paths,
            "candidates": candidates,
            "protected_paths": protected_paths,
            "affected_repositories": list(report.get("affected_repositories") or []),
            "write_scope": list(report.get("write_scope") or []),
            "reason": str(approval.get("reason", "")),
        }

    def approve_protected_scope(
        self,
        package_id: str,
        *,
        expected_candidates: list[str],
    ) -> dict[str, Any]:
        """Approve one previewed protected-scope candidate set and resume the driver."""

        package_id = str(package_id).strip()
        expected = [
            str(item).strip() for item in expected_candidates if str(item).strip()
        ]
        if not package_id:
            raise GuiError("a package ID is required for scope approval")
        if not expected:
            raise GuiError("scope approval requires the previewed candidate paths")
        if not self._workspace_action_lock.acquire(blocking=False):
            raise GuiError("another workspace action is already in progress")
        try:
            self._ensure_idle_workspace_action("approving protected scope")
            approval = self._protected_scope_approval(self._latest_human_action())
            if not approval.get("available") or approval.get("package_id") != package_id:
                raise GuiError(
                    "the active operator action is not a protected-scope approval"
                )
            report = self._scope_cli_json(
                package_id,
                accept=True,
                expected_candidates=expected,
            )
            driver: dict[str, Any] = {}
            record = self._load_state()
            if record is not None and record.state == TaskExecutionState.RUNNING:
                driver = self.process.start()
            return {
                "approved": True,
                "package_id": package_id,
                "scope": report,
                "run": driver,
            }
        finally:
            self._workspace_action_lock.release()
