"""Restart-safe task completion transaction.

A task is never retired merely because orchestration says ``completed``. The
service first creates/reuses an immutable completion archive and verifies it,
then delegates runtime/worktree retirement to the lifecycle safety service.
The task branch, live dossier, archive and workspace tombstone are retained.

The transaction file is intentionally outside the disposable workspace. A crash
at any point can therefore be resumed by invoking :meth:`complete` again.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

import yaml

from execraft.archive import TaskArchiveManager
from execraft.orchestrate.identity import resolve_storage_identity
from execraft.orchestrate.journal import EventJournal
from execraft.persistence import FileLock, LockBusyError, LockLevel, atomic_write_text
from execraft.workspace.cleanup import WorkspaceLifecycleService
from execraft.workspace.task_git import (
    TaskGitError,
    TaskManifest,
    load_manifest,
    write_manifest,
)
from execraft.workspace.workspace_git import load_workspace

from .models import TaskCompletionError, TaskCompletionPolicy, TaskCompletionResult

_COMPLETION_SCHEMA_VERSION = 1
_TRANSACTION_NAME = "completion-transaction.yaml"
_REPORT_NAME = "completion-report.yaml"
_LOCK_NAME = "completion.lock"
_TERMINAL_PHASES = frozenset({"completed", "failed"})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskCompletionService:
    """Coordinate immutable archive creation and disposable workspace retirement."""

    def __init__(
        self,
        control_root: Path,
        state_root: Path,
        *,
        archive_root: Path | None = None,
        policy: TaskCompletionPolicy | None = None,
        archive_manager: TaskArchiveManager | None = None,
        workspace_lifecycle: WorkspaceLifecycleService | None = None,
    ) -> None:
        self.control_root = Path(control_root).expanduser().resolve()
        self.state_root = Path(state_root).expanduser().resolve()
        self.archive_root = (
            Path(archive_root).expanduser().resolve() if archive_root is not None else None
        )
        self.policy = policy or TaskCompletionPolicy()
        self.archives = archive_manager or TaskArchiveManager(
            self.control_root,
            self.state_root,
            archive_root=self.archive_root,
        )
        self.workspaces = workspace_lifecycle or WorkspaceLifecycleService(
            self.control_root,
            self.state_root,
            archive_root=self.archive_root,
        )

    def complete(
        self,
        task_id: str,
        *,
        dry_run: bool = False,
    ) -> TaskCompletionResult:
        """Complete one task or resume an interrupted completion transaction."""

        manifest = load_manifest(self.control_root, task_id)
        identity = resolve_storage_identity(
            self.state_root,
            project_id=manifest.project,
            task_id=manifest.id,
        )
        state_dir = identity.state_dir
        state_dir.mkdir(parents=True, exist_ok=True)
        transaction_path = state_dir / _TRANSACTION_NAME
        report_path = state_dir / _REPORT_NAME
        journal = EventJournal(identity.journal_path)

        with self._exclusive_lock(state_dir):
            manifest = self._reconcile_stale_manifest_status(
                manifest,
                persist=not dry_run,
            )
            previous = self._read_mapping(transaction_path, missing_ok=True)
            if previous:
                self._validate_existing_record(previous, manifest)
            resumed = bool(previous) and str(previous.get("phase", "")) not in _TERMINAL_PHASES
            if previous and str(previous.get("phase", "")) == "completed":
                self._verify_completed_record(previous, manifest.id)
                return self._result_from_record(previous, report_path=report_path, resumed=True)

            record: dict[str, Any] = self._base_record(manifest, resumed=resumed)
            if previous:
                record.update(previous)
                record["resumed"] = True
                record["updated_at"] = _utc_now()
            if dry_run:
                return self._dry_run(manifest, record, report_path)

            self._write_mapping(transaction_path, record)
            journal.append(
                "task_completion_started" if not resumed else "task_completion_resumed",
                {
                    "project": manifest.project,
                    "task_id": manifest.id,
                    "phase": str(record.get("phase", "prepared")),
                    "policy": self.policy.as_mapping(),
                },
            )
            try:
                if self.policy.require_archive:
                    saved_archive = str(record.get("archive_path", "")).strip()
                    saved_sha = str(record.get("archive_manifest_sha256", "")).strip()
                    if saved_archive and saved_sha:
                        archive_path = Path(saved_archive).expanduser().resolve()
                        self.archives.verify(
                            archive_path,
                            expected_manifest_sha256=saved_sha,
                        )
                        record["archive_verified"] = True
                        record["phase"] = "archive_verified"
                        record["updated_at"] = _utc_now()
                        self._write_mapping(transaction_path, record)
                    else:
                        archive_result = self.archives.archive(manifest)
                        record.update(
                            {
                                "phase": "archived",
                                "archive_id": archive_result.archive_id,
                                "archive_path": str(archive_result.archive_path),
                                "archive_manifest_sha256": archive_result.manifest_sha256,
                                "updated_at": _utc_now(),
                            }
                        )
                        self._write_mapping(transaction_path, record)
                        if self.policy.verify_archive_before_cleanup:
                            self.archives.verify(
                                archive_result.archive_path,
                                expected_manifest_sha256=archive_result.manifest_sha256,
                            )
                            record["archive_verified"] = True
                            record["phase"] = "archive_verified"
                            record["updated_at"] = _utc_now()
                            self._write_mapping(transaction_path, record)

                actions: list[str] = list(record.get("actions") or [])
                removed_worktrees: tuple[str, ...] = tuple(
                    str(item) for item in (record.get("removed_worktrees") or [])
                )
                shell_removed = bool(record.get("shell_removed", False))
                runtime_stopped = bool(record.get("runtime_stopped", False))

                if self.policy.remove_worktrees:
                    lifecycle = self.workspaces.destroy(
                        manifest.id,
                        remove_shell=self.policy.remove_workspace_shell,
                        dry_run=False,
                        force=False,
                        require_archive=self.policy.require_archive,
                    )
                    actions.extend(lifecycle.actions)
                    removed_worktrees = tuple(lifecycle.removed_worktrees)
                    shell_removed = lifecycle.shell_removed or shell_removed
                    runtime_stopped = not lifecycle.runtime.skipped or runtime_stopped
                    record["phase"] = "workspace_retired"
                elif self.policy.stop_runtime:
                    lifecycle = self.workspaces.stop(manifest.id, dry_run=False, force=False)
                    actions.extend(lifecycle.actions)
                    runtime_stopped = not lifecycle.runtime.skipped or runtime_stopped
                    record["phase"] = "runtime_stopped"

                workspace_status = ""
                try:
                    workspace_status = load_workspace(self.control_root, manifest.id).status
                except TaskGitError:
                    # The retained tombstone is expected by policy; if a custom
                    # embedding omits a workspace registry, archive completion is
                    # still reportable but the default policy will already have
                    # failed inside WorkspaceLifecycleService before this point.
                    workspace_status = "unavailable"

                record.update(
                    {
                        "schema_version": _COMPLETION_SCHEMA_VERSION,
                        "status": "completed",
                        "phase": "completed",
                        "completed_at": _utc_now(),
                        "updated_at": _utc_now(),
                        "workspace_status": workspace_status,
                        "runtime_stopped": runtime_stopped,
                        "removed_worktrees": list(removed_worktrees),
                        "shell_removed": shell_removed,
                        "actions": self._unique(actions),
                        "error": "",
                    }
                )
                self._write_mapping(transaction_path, record)
                self._write_mapping(report_path, record)
                journal.append(
                    "task_completion_completed",
                    {
                        "project": manifest.project,
                        "task_id": manifest.id,
                        "archive_id": str(record.get("archive_id", "")),
                        "archive_path": str(record.get("archive_path", "")),
                        "workspace_status": workspace_status,
                        "runtime_stopped": runtime_stopped,
                        "removed_worktrees": list(removed_worktrees),
                        "shell_removed": shell_removed,
                        "report_path": str(report_path),
                    },
                )
                return self._result_from_record(record, report_path=report_path, resumed=resumed)
            except Exception as exc:
                record.update(
                    {
                        "status": "incomplete",
                        "updated_at": _utc_now(),
                        "error": str(exc),
                    }
                )
                self._write_mapping(transaction_path, record)
                self._write_mapping(report_path, record)
                journal.append(
                    "task_completion_incomplete",
                    {
                        "project": manifest.project,
                        "task_id": manifest.id,
                        "phase": str(record.get("phase", "prepared")),
                        "error": str(exc)[:2000],
                        "report_path": str(report_path),
                    },
                )
                raise TaskCompletionError(
                    f"task completion stopped safely at phase {record.get('phase', 'prepared')}: {exc}",
                    report_path=report_path,
                ) from exc

    def status(self, task_id: str) -> TaskCompletionResult | None:
        manifest = load_manifest(self.control_root, task_id)
        identity = resolve_storage_identity(
            self.state_root,
            project_id=manifest.project,
            task_id=manifest.id,
            create=False,
        )
        record = self._read_mapping(identity.state_dir / _TRANSACTION_NAME, missing_ok=True)
        if not record:
            return None
        return self._result_from_record(
            record,
            report_path=identity.state_dir / _REPORT_NAME,
            resumed=False,
        )

    def _validate_existing_record(self, record: Mapping[str, Any], manifest: Any) -> None:
        try:
            schema_version = int(record.get("schema_version", 0))
        except (TypeError, ValueError) as exc:
            raise TaskCompletionError("completion transaction has an invalid schema version") from exc
        if schema_version != _COMPLETION_SCHEMA_VERSION:
            raise TaskCompletionError(
                f"unsupported completion transaction schema: {schema_version}"
            )
        if str(record.get("project", "")) != manifest.project or str(
            record.get("task_id", "")
        ) != manifest.id:
            raise TaskCompletionError("completion transaction identity does not match the task")
        recorded_policy = record.get("policy") or {}
        if recorded_policy != self.policy.as_mapping():
            raise TaskCompletionError(
                "completion policy changed during an existing transaction; "
                "resume with the original policy or inspect the transaction first"
            )

    def _verify_completed_record(self, record: Mapping[str, Any], task_id: str) -> None:
        if self.policy.require_archive:
            archive_path_raw = str(record.get("archive_path", "")).strip()
            manifest_sha = str(record.get("archive_manifest_sha256", "")).strip()
            if not archive_path_raw or not manifest_sha:
                raise TaskCompletionError("completed transaction is missing archive integrity data")
            try:
                self.archives.verify(
                    Path(archive_path_raw).expanduser().resolve(),
                    expected_manifest_sha256=manifest_sha,
                )
            except TaskGitError as exc:
                raise TaskCompletionError(
                    f"completed transaction archive verification failed: {exc}"
                ) from exc
        if self.policy.remove_worktrees:
            workspace = load_workspace(self.control_root, task_id)
            if workspace.status != "removed":
                raise TaskCompletionError(
                    "completion record says completed but workspace tombstone is not removed"
                )
            if self.policy.remove_workspace_shell and Path(
                workspace.workspace_root
            ).expanduser().resolve().exists():
                raise TaskCompletionError(
                    "completion record says completed but generated workspace shell still exists"
                )

    def _dry_run(
        self,
        manifest: TaskManifest,
        record: dict[str, Any],
        report_path: Path,
    ) -> TaskCompletionResult:
        actions: list[str] = []
        if self.policy.require_archive:
            archive_report = self.archives.preflight(manifest)
            archive_report.require_ok()
            actions.append("create or reuse and verify immutable completion archive")
        if self.policy.remove_worktrees:
            workspace = load_workspace(self.control_root, manifest.id)
            lifecycle = self.workspaces.preflight(
                workspace,
                operation="destroy",
                require_archive=False,
                check_repositories=True,
                allow_pinned=False,
            )
            lifecycle.require_integrity()
            # Archive is not created in dry-run, so omit its verifier from this
            # preview while still enforcing every non-archive destructive check.
            lifecycle.require_ok()
            actions.append("stop task-owned runtime resources")
            actions.append("remove registered task-owned Git worktrees")
            if self.policy.remove_workspace_shell:
                self.workspaces.safety.validate_shell_removal(workspace)
                actions.append("remove generated workspace shell")
        elif self.policy.stop_runtime:
            actions.append("stop task-owned runtime resources")
        record.update(
            {
                "status": "preview",
                "phase": "preflight",
                "actions": actions,
            }
        )
        return self._result_from_record(record, report_path=report_path, resumed=False)

    def _reconcile_stale_manifest_status(
        self,
        manifest: TaskManifest,
        *,
        persist: bool,
    ) -> TaskManifest:
        """Repair legacy pre-plan status only after completion checks prove it safe.

        Older orchestration runs could reach their durable ``completed`` state
        while TASK.yaml still contained the onboarding status.  Projecting the
        task as in progress lets the canonical archive preflight prove the
        durable state, package evidence, and task identity before any metadata
        is changed.  A dry-run uses the same projection without writing it.
        """

        if manifest.status not in {"draft", "briefed"}:
            return manifest
        reconciled = TaskManifest.from_mapping(manifest.as_mapping())
        reconciled.status = "in_progress"
        self.archives.preflight(reconciled).require_ok()
        if persist:
            write_manifest(self.control_root, reconciled)
        return reconciled

    def _base_record(self, manifest: Any, *, resumed: bool) -> dict[str, Any]:
        now = _utc_now()
        return {
            "schema_version": _COMPLETION_SCHEMA_VERSION,
            "project": manifest.project,
            "task_id": manifest.id,
            "status": "running",
            "phase": "prepared",
            "started_at": now,
            "updated_at": now,
            "completed_at": "",
            "resumed": resumed,
            "policy": self.policy.as_mapping(),
            "archive_id": "",
            "archive_path": "",
            "archive_manifest_sha256": "",
            "archive_verified": False,
            "workspace_status": "",
            "runtime_stopped": False,
            "removed_worktrees": [],
            "shell_removed": False,
            "actions": [],
            "error": "",
        }

    @staticmethod
    def _result_from_record(
        record: Mapping[str, Any],
        *,
        report_path: Path,
        resumed: bool,
    ) -> TaskCompletionResult:
        archive_path_raw = str(record.get("archive_path", "")).strip()
        return TaskCompletionResult(
            project=str(record.get("project", "")),
            task_id=str(record.get("task_id", "")),
            status=str(record.get("status", "")),
            phase=str(record.get("phase", "")),
            report_path=report_path,
            archive_path=Path(archive_path_raw) if archive_path_raw else None,
            archive_id=str(record.get("archive_id", "")),
            workspace_status=str(record.get("workspace_status", "")),
            runtime_stopped=bool(record.get("runtime_stopped", False)),
            removed_worktrees=tuple(str(item) for item in (record.get("removed_worktrees") or [])),
            shell_removed=bool(record.get("shell_removed", False)),
            actions=tuple(str(item) for item in (record.get("actions") or [])),
            resumed=resumed or bool(record.get("resumed", False)),
        )

    @contextmanager
    def _exclusive_lock(self, state_dir: Path) -> Iterator[None]:
        try:
            with FileLock(
                state_dir / _LOCK_NAME,
                level=LockLevel.DRIVER,
                timeout=0.0,
            ):
                yield
        except LockBusyError as exc:
            raise TaskCompletionError(
                "another task completion operation is already running"
            ) from exc

    @staticmethod
    def _read_mapping(path: Path, *, missing_ok: bool) -> dict[str, Any]:
        if not path.exists():
            if missing_ok:
                return {}
            raise TaskCompletionError(f"completion record is missing: {path}")
        if path.is_symlink() or not path.is_file():
            raise TaskCompletionError(f"completion record is unsafe: {path}")
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise TaskCompletionError(f"invalid completion record {path}: {exc}") from exc
        if not isinstance(data, Mapping):
            raise TaskCompletionError(f"completion record must contain a mapping: {path}")
        return dict(data)

    @staticmethod
    def _write_mapping(path: Path, data: Mapping[str, Any]) -> None:
        atomic_write_text(path, yaml.safe_dump(dict(data), sort_keys=False, allow_unicode=True))

    @staticmethod
    def _unique(actions: list[str]) -> list[str]:
        return list(dict.fromkeys(str(item) for item in actions if str(item).strip()))
