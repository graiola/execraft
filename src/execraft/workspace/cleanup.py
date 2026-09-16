"""Safe task-workspace retirement and reversible runtime shutdown.

This module is the only production path allowed to remove a registered task
worktree or generated workspace shell. The CLI and automatic resource-pressure
cleanup both delegate here so every destructive path uses identical safety checks.
"""

from __future__ import annotations

import shutil
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import yaml

from execraft.orchestrate.identity import resolve_storage_identity
from execraft.persistence import FileLock, LockBusyError, LockLevel
from execraft.workspace.lifecycle_safety import (
    ArchiveVerifier,
    CompletionArchiveVerifier,
    WorkspaceSafetyInspector,
)
from execraft.workspace.lifecycle_types import (
    LifecycleCheck,
    LifecycleReport,
    RuntimeStopResult,
    WorkspaceLifecycleResult,
)
from execraft.workspace.runtime_cleanup import DockerRuntimeController
from execraft.workspace.task_git import TaskGitError, load_manifest
from execraft.workspace.workspace_git import (
    WorkspaceRecord,
    load_workspace,
    remove_worktree,
    workspace_registry_dir,
    write_workspace,
)

WORKSPACE_MARKER = Path(".execraft") / "workspace.yaml"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class WorkspaceLifecycleService:
    """Restart-safe workspace stop and retirement service.

    The workspace registry is the durable transaction marker. Worktrees are
    detached through Git, the registry is tombstoned, and the optional generated
    shell is deleted last. Re-running an interrupted operation is safe because
    already-absent generated worktrees are treated as completed steps.
    """

    def __init__(
        self,
        control_root: Path,
        state_root: Path,
        *,
        archive_root: Path | None = None,
        runtime_controller: DockerRuntimeController | None = None,
        archive_verifier: ArchiveVerifier | None = None,
    ) -> None:
        self.control_root = control_root.expanduser().resolve()
        self.state_root = state_root.expanduser().resolve()
        selected_archive_root = (
            archive_root.expanduser().resolve() if archive_root is not None else None
        )
        verifier = archive_verifier or CompletionArchiveVerifier(
            self.control_root,
            self.state_root,
            archive_root=selected_archive_root,
        )
        self.runtime = runtime_controller or DockerRuntimeController()
        self.safety = WorkspaceSafetyInspector(
            self.control_root,
            self.state_root,
            archive_verifier=verifier,
        )
        self._lock_root = (
            workspace_registry_dir(self.control_root).parent / "workspace-lifecycle-locks"
        )

    def preflight(
        self,
        record: WorkspaceRecord,
        *,
        operation: str,
        require_archive: bool,
        check_repositories: bool,
        allow_pinned: bool = False,
        orchestrator_lock_held: bool = False,
    ) -> LifecycleReport:
        return self.safety.preflight(
            record,
            operation=operation,
            require_archive=require_archive,
            check_repositories=check_repositories,
            allow_pinned=allow_pinned,
            orchestrator_lock_held=orchestrator_lock_held,
        )

    def stop(
        self,
        task_id: str,
        *,
        dry_run: bool = False,
        force: bool = False,
    ) -> WorkspaceLifecycleResult:
        """Stop task-owned runtime resources without touching Git worktrees."""

        with self._orchestrator_lease(task_id, force=force) as lock_held:
            with self._exclusive_lock(task_id):
                record = load_workspace(self.control_root, task_id)
                report = self.preflight(
                    record,
                    operation="stop",
                    require_archive=False,
                    check_repositories=False,
                    allow_pinned=True,
                    orchestrator_lock_held=lock_held,
                )
                report.require_integrity()
                if not force:
                    report.require_ok()
                runtime = self._stop_runtime(record, dry_run=dry_run, force=force)
                if not dry_run:
                    self._record_lifecycle(record, action="stop", removed=False)
                return WorkspaceLifecycleResult(
                    report=report,
                    actions=runtime.actions,
                    runtime=runtime,
                )

    def destroy(
        self,
        task_id: str,
        *,
        remove_shell: bool = False,
        dry_run: bool = False,
        force: bool = False,
        require_archive: bool = True,
    ) -> WorkspaceLifecycleResult:
        """Safely detach worktrees and optionally remove the generated shell."""

        # Lock ordering follows the global hierarchy: orchestration ownership is
        # established before the narrower lifecycle mutation lock.
        with self._orchestrator_lease(task_id, force=force) as lock_held:
            with self._exclusive_lock(task_id):
                record = load_workspace(self.control_root, task_id)
                workspace_root = Path(record.workspace_root).expanduser().resolve()
                if record.status == "removed" and not workspace_root.exists():
                    report = LifecycleReport(
                        task_id=record.task_id,
                        operation="destroy",
                        checks=[
                            LifecycleCheck(
                                "already_retired",
                                True,
                                "workspace is already retired",
                            )
                        ],
                    )
                    runtime = RuntimeStopResult(
                        actions=("workspace is already retired",),
                        skipped=True,
                    )
                    return WorkspaceLifecycleResult(
                        report=report,
                        actions=runtime.actions,
                        runtime=runtime,
                    )

                report = self.preflight(
                    record,
                    operation="destroy",
                    require_archive=require_archive,
                    check_repositories=True,
                    allow_pinned=force,
                    orchestrator_lock_held=lock_held,
                )
                report.require_integrity()
                if not force:
                    report.require_ok()
                if remove_shell:
                    # Validate shell ownership before stopping runtime or detaching any
                    # worktree so a policy failure remains completely non-destructive.
                    self.safety.validate_shell_removal(record)

                runtime = self._stop_runtime(record, dry_run=dry_run, force=force)
                actions = list(runtime.actions)
                removed = self._remove_worktrees(
                    record,
                    actions=actions,
                    dry_run=dry_run,
                    force=force,
                )

                shell_removed = False
                if not dry_run:
                    # Publish the tombstone before shell deletion. If deletion is
                    # interrupted, the retained registry record is enough to resume.
                    self._record_lifecycle(record, action="destroy", removed=True)
                if remove_shell:
                    actions.append(f"remove generated workspace shell: {workspace_root}")
                    if not dry_run:
                        shutil.rmtree(workspace_root)
                        shell_removed = True

                return WorkspaceLifecycleResult(
                    report=report,
                    actions=tuple(actions),
                    runtime=runtime,
                    removed_worktrees=tuple(removed),
                    shell_removed=shell_removed,
                )

    def retire_stale_workspace(self, root: Path, *, dry_run: bool = False) -> list[str]:
        """Retire one stale workspace, preserving it on any failed safety check."""

        workspace_root = root.expanduser().resolve()
        marker = workspace_root / WORKSPACE_MARKER
        try:
            if marker.is_symlink() or not marker.is_file():
                raise TaskGitError(f"workspace marker is missing or unsafe: {marker}")
            data = yaml.safe_load(marker.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                raise TaskGitError(f"workspace marker must contain a mapping: {marker}")
            task_id = str(data.get("task_id", "")).strip()
            if not task_id:
                raise TaskGitError(f"workspace marker has no task_id: {marker}")
            record = load_workspace(self.control_root, task_id)
            if Path(record.workspace_root).expanduser().resolve() != workspace_root:
                raise TaskGitError(
                    f"workspace registry does not own the stale candidate: {workspace_root}"
                )
            result = self.destroy(
                task_id,
                remove_shell=True,
                dry_run=dry_run,
                force=False,
                require_archive=True,
            )
        except (OSError, yaml.YAMLError, TaskGitError, ValueError) as exc:
            return [f"preserve stale managed workspace {workspace_root}: {exc}"]
        prefix = "would " if dry_run else ""
        detail_actions = (
            tuple(f"would {action}" for action in result.actions) if dry_run else result.actions
        )
        return [
            f"{prefix}retire stale managed workspace: {workspace_root}",
            *detail_actions,
        ]

    def _remove_worktrees(
        self,
        record: WorkspaceRecord,
        *,
        actions: list[str],
        dry_run: bool,
        force: bool,
    ) -> list[str]:
        removed: list[str] = []
        for item in reversed(record.repositories):
            if item.get("mutability", "task_owned") != "task_owned":
                continue
            source = Path(str(item["source_path"])).expanduser().resolve()
            destination = Path(str(item["worktree_path"])).expanduser().resolve()
            if destination == source or not destination.exists():
                continue
            actions.append(f"remove Git worktree: {destination}")
            if dry_run:
                continue
            remove_worktree(source, destination, force=force)
            removed.append(str(destination))
        return removed

    def _stop_runtime(
        self,
        record: WorkspaceRecord,
        *,
        dry_run: bool,
        force: bool,
    ) -> RuntimeStopResult:
        try:
            return self.runtime.stop(record, dry_run=dry_run)
        except TaskGitError:
            if not force:
                raise
            return RuntimeStopResult(
                actions=("force mode skipped runtime cleanup after ownership or shutdown failure",),
                skipped=True,
            )

    def _record_lifecycle(
        self,
        record: WorkspaceRecord,
        *,
        action: str,
        removed: bool,
    ) -> None:
        record.status = "removed" if removed else record.status
        record.runtime_status = "stopped"
        record.last_lifecycle_action = action
        record.last_lifecycle_at = _utc_now()
        write_workspace(self.control_root, record)

    @contextmanager
    def _orchestrator_lease(self, task_id: str, *, force: bool) -> Iterator[bool]:
        """Hold the real orchestrator lock for the full mutating operation.

        A point-in-time idle check is vulnerable to a restart between preflight
        and worktree removal. Holding this lock makes that race impossible. A
        missing manifest can only be bypassed in explicit force recovery, where
        no reliable project-scoped lock location can be derived.
        """

        try:
            manifest = load_manifest(self.control_root, task_id)
        except TaskGitError:
            if force:
                yield False
                return
            raise
        identity = resolve_storage_identity(
            self.state_root,
            project_id=manifest.project,
            task_id=manifest.id,
            create=False,
        )
        try:
            with FileLock(
                identity.state_dir / "orchestrator.lock",
                level=LockLevel.ORCHESTRATOR,
                timeout=0.0,
            ):
                yield True
        except LockBusyError as exc:
            raise TaskGitError(
                f"orchestrator is active for task {task_id}; lifecycle action refused"
            ) from exc

    @contextmanager
    def _exclusive_lock(self, task_id: str) -> Iterator[None]:
        try:
            with FileLock(
                self._lock_root / f"{task_id}.lock",
                level=LockLevel.LIFECYCLE,
                timeout=0.0,
            ):
                yield
        except LockBusyError as exc:
            raise TaskGitError(
                f"another workspace lifecycle operation is running for {task_id}"
            ) from exc


__all__ = [
    "ArchiveVerifier",
    "CompletionArchiveVerifier",
    "DockerRuntimeController",
    "LifecycleCheck",
    "LifecycleReport",
    "RuntimeStopResult",
    "WorkspaceLifecycleResult",
    "WorkspaceLifecycleService",
]
