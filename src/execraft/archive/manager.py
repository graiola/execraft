"""Lifecycle manager for immutable task completion archives."""

from __future__ import annotations

import json
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

import yaml

from execraft.archive.integrity import (
    _archive_id,
    _copy_tree_without_symlinks,
    _protect_tree,
    _safe_archive_member,
    _tree_digests,
    _utc_now,
    _write_json,
    _write_sha256sums,
    _write_yaml,
)
from execraft.archive.models import ArchiveCheck, ArchivePreflightReport, ArchiveResult
from execraft.persistence import (
    FileLock,
    LockBusyError,
    LockLevel,
    atomic_write_bytes,
    file_lock_is_held,
    sha256_file,
)
from execraft.archive.summary import _verification_summary
from execraft.orchestrate.identity import resolve_storage_identity
from execraft.orchestrate.journal import EventJournal
from execraft.orchestrate.models import TaskExecutionStateRecord, WorkPackageStage
from execraft.repository_sync.transaction import RepositorySyncTransactionStore
from execraft.workspace.task_git import (
    TaskGitError,
    TaskManifest,
    current_branch,
    git_operation,
    head_commit,
    project_task_directory,
    working_tree_dirty,
    write_manifest,
)
from execraft.workspace.workspace_git import WorkspaceRecord, load_workspace

_ARCHIVE_SCHEMA_VERSION = 1
_COMPLETION_SCHEMA_VERSION = 1
_ALLOWED_CLOSE_STATUSES = frozenset(
    {"planned", "in_progress", "review", "approved", "integrating", "merged", "closed"}
)


class TaskArchiveManager:
    """Create, index, inspect, and verify immutable task completion bundles."""

    def __init__(
        self,
        control_root: Path,
        state_root: Path,
        archive_root: Path | None = None,
    ) -> None:
        self.control_root = control_root.expanduser().resolve()
        self.state_root = state_root.expanduser().resolve()
        self.archive_root = (
            archive_root.expanduser().resolve()
            if archive_root is not None
            else self.state_root / "archives"
        )
        self.index_path = self.archive_root / "index.json"
        self._lock_path = self.archive_root / ".archive.lock"

    def preflight(self, manifest: TaskManifest) -> ArchivePreflightReport:
        checks: list[ArchiveCheck] = []
        dossier = project_task_directory(self.control_root, manifest.project, manifest.id)
        checks.append(
            ArchiveCheck(
                "task_status",
                manifest.status in _ALLOWED_CLOSE_STATUSES,
                (
                    f"task status is {manifest.status!r}"
                    if manifest.status in _ALLOWED_CLOSE_STATUSES
                    else (
                        "task must be planned/in progress with completed orchestration, "
                        "in review, approved, integrating, merged, or already closed"
                    )
                ),
            )
        )
        dossier_valid = dossier.is_dir() and (dossier / "TASK.yaml").is_file()
        checks.append(
            ArchiveCheck(
                "dossier",
                dossier_valid,
                f"task dossier {'found' if dossier_valid else 'missing or incomplete'}: {dossier}",
            )
        )
        control_dirty = working_tree_dirty(self.control_root)
        checks.append(
            ArchiveCheck(
                "control_plane_clean",
                not control_dirty,
                (
                    "control-plane checkout is clean"
                    if not control_dirty
                    else "control-plane checkout has uncommitted changes; commit the closure record after archiving"
                ),
                severity="warning",
            )
        )

        state_path = self._project_state_dir(manifest.project, manifest.id) / "state.json"
        state = self._load_state(state_path, checks)
        if state is not None:
            checks.append(
                ArchiveCheck(
                    "orchestration_completed",
                    state.state.value == "completed",
                    f"orchestration state is {state.state.value!r}",
                )
            )
            checks.append(
                ArchiveCheck(
                    "state_task_identity",
                    state.project_id == manifest.id,
                    (
                        "state file belongs to this task"
                        if state.project_id == manifest.id
                        else f"state file belongs to {state.project_id!r}, expected {manifest.id!r}"
                    ),
                )
            )
            incomplete = [
                package.id
                for package in state.plan_graph.work_packages
                if package.stage != WorkPackageStage.COMPLETED
                or package.status != "completed"
            ]
            checks.append(
                ArchiveCheck(
                    "work_packages_completed",
                    not incomplete,
                    (
                        "all work packages are completed"
                        if not incomplete
                        else "incomplete work packages: " + ", ".join(incomplete)
                    ),
                )
            )
            missing_evidence: list[str] = []
            for package in state.plan_graph.work_packages:
                for criterion in package.acceptance_criteria:
                    if not criterion.verified or not criterion.evidence.strip():
                        missing_evidence.append(f"{package.id}/{criterion.id}")
            checks.append(
                ArchiveCheck(
                    "acceptance_evidence",
                    not missing_evidence,
                    (
                        "all acceptance criteria have durable evidence"
                        if not missing_evidence
                        else "missing acceptance evidence: " + ", ".join(missing_evidence)
                    ),
                )
            )

        journal_path = self._journal_path(manifest.project, manifest.id)
        journal = self._load_json_array(journal_path, "event journal", checks)
        if journal is not None:
            verification_events = [
                entry
                for entry in journal
                if isinstance(entry, Mapping)
                and entry.get("event_type") == "verification_command_run"
            ]
            checks.append(
                ArchiveCheck(
                    "verification_evidence",
                    bool(verification_events),
                    (
                        f"found {len(verification_events)} verification command event(s)"
                        if verification_events
                        else "no verification command evidence was recorded"
                    ),
                    severity="warning",
                )
            )

        commit_path = self._project_state_dir(manifest.project, manifest.id) / "commit-journal.json"
        transactions = self._load_json_array(
            commit_path,
            "commit journal",
            checks,
            missing_ok=True,
        )
        if transactions is not None:
            pending = [
                str(item.get("transaction_id", "unknown"))
                for item in transactions
                if isinstance(item, Mapping) and item.get("status") == "pending"
            ]
            checks.append(
                ArchiveCheck(
                    "commit_transactions",
                    not pending,
                    (
                        "no pending commit transactions"
                        if not pending
                        else "pending commit transactions: " + ", ".join(pending)
                    ),
                )
            )

        sync_store = RepositorySyncTransactionStore(
            self._project_state_dir(manifest.project, manifest.id)
        )
        try:
            sync_pending = sync_store.pending_transactions()
            checks.append(
                ArchiveCheck(
                    "repository_sync_transactions",
                    not sync_pending,
                    (
                        "no pending repository-sync transactions"
                        if not sync_pending
                        else "pending repository-sync transactions: "
                        + ", ".join(item.transaction_id for item in sync_pending)
                    ),
                )
            )
        except Exception as exc:
            checks.append(
                ArchiveCheck(
                    "repository_sync_transactions",
                    False,
                    f"repository-sync transaction state is invalid: {exc}",
                )
            )

        checks.append(self._orchestrator_idle_check(manifest.project, manifest.id))

        try:
            workspace = load_workspace(self.control_root, manifest.id)
        except TaskGitError as exc:
            workspace = None
            checks.append(ArchiveCheck("workspace", False, str(exc)))
        else:
            checks.append(
                ArchiveCheck(
                    "workspace",
                    workspace.status != "removed",
                    f"workspace status is {workspace.status!r}",
                )
            )
            workspace_ids = {str(item.get("id", "")) for item in workspace.repositories}
            for repository in manifest.repositories:
                if repository.id in workspace_ids:
                    continue
                checks.append(
                    ArchiveCheck(
                        f"repository:{repository.id}:workspace",
                        not repository.required,
                        f"repository {repository.id} is absent from the workspace",
                        severity="error" if repository.required else "warning",
                    )
                )
            checks.extend(self._repository_checks(workspace, manifest))

        return ArchivePreflightReport(
            task_id=manifest.id,
            project=manifest.project,
            checks=checks,
        )

    def archive(self, manifest: TaskManifest) -> ArchiveResult:
        self.archive_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.archive_root.chmod(0o700)
        with self._exclusive_archive_lock():
            existing = self._latest_record(manifest.project, manifest.id)
            if existing is not None:
                self._append_index_record(existing)
                result = self._result_from_record(existing, created=False)
                self.verify(
                    result.archive_path,
                    expected_manifest_sha256=result.manifest_sha256,
                )
                self._append_archived_event_once(result)
                self._materialize_live_completion(manifest, result)
                return result

            report = self.preflight(manifest)
            report.require_ok()
            workspace = load_workspace(self.control_root, manifest.id)
            state = self._read_json(self._project_state_dir(manifest.project, manifest.id) / "state.json")
            journal = self._read_json(self._journal_path(manifest.project, manifest.id))
            commit_journal_path = self._project_state_dir(manifest.project, manifest.id) / "commit-journal.json"
            commit_journal = (
                self._read_json(commit_journal_path)
                if commit_journal_path.is_file()
                else []
            )

            created_at = _utc_now()
            archive_id = _archive_id(created_at)
            parent = self.archive_root / manifest.project / manifest.id
            final_path = parent / archive_id
            if final_path.exists():
                raise TaskGitError(f"task archive already exists: {final_path}")
            parent.mkdir(parents=True, exist_ok=True)
            temporary = parent / f".{archive_id}.tmp-{uuid.uuid4().hex}"
            temporary.mkdir(mode=0o700)
            try:
                completion = self._build_bundle(
                    temporary,
                    manifest=manifest,
                    workspace=workspace,
                    state=state,
                    journal=journal,
                    commit_journal=commit_journal,
                    archive_id=archive_id,
                    created_at=created_at,
                    final_path=final_path,
                )
                _protect_tree(temporary)
                temporary.replace(final_path)
                record = {
                    "schema_version": _ARCHIVE_SCHEMA_VERSION,
                    "project": manifest.project,
                    "task_id": manifest.id,
                    "title": manifest.title,
                    "archive_id": archive_id,
                    "created_at": created_at,
                    "path": str(final_path),
                    "manifest_sha256": completion["manifest_sha256"],
                    "status": "closed",
                }
                self._append_index_record(record)
                result = self._result_from_record(record, created=True)
                self._append_archived_event_once(result)
                self._materialize_live_completion(manifest, result)
                return result
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary, ignore_errors=True)

    def list_records(self, project: str | None = None) -> list[dict[str, Any]]:
        records_by_path = {
            str(record.get("path", "")): record for record in self._load_index()
        }
        for record in self._discover_records(project=project):
            records_by_path.setdefault(str(record.get("path", "")), record)
        records = list(records_by_path.values())
        if project:
            records = [record for record in records if record.get("project") == project]
        return sorted(
            records,
            key=lambda record: (str(record.get("created_at", "")), str(record.get("task_id", ""))),
            reverse=True,
        )

    def latest(self, project: str, task_id: str) -> ArchiveResult:
        record = self._latest_record(project, task_id)
        if record is None:
            raise TaskGitError(f"no archive found for {project}/{task_id}")
        return self._result_from_record(record, created=False)

    def verify(
        self,
        archive_path: Path,
        *,
        expected_manifest_sha256: str = "",
    ) -> dict[str, Any]:
        root = archive_path.expanduser().resolve()
        if not root.is_dir():
            raise TaskGitError(f"archive directory does not exist: {root}")
        sums_path = root / "SHA256SUMS"
        if not sums_path.is_file():
            raise TaskGitError(f"archive checksum manifest is missing: {sums_path}")
        expected_files: set[str] = set()
        checked = 0
        for line_number, raw_line in enumerate(sums_path.read_text(encoding="utf-8").splitlines(), 1):
            if not raw_line.strip():
                continue
            digest, separator, relative = raw_line.partition("  ")
            if not separator or len(digest) != 64:
                raise TaskGitError(f"invalid SHA256SUMS line {line_number}: {raw_line!r}")
            if relative in expected_files:
                raise TaskGitError(f"duplicate SHA256SUMS entry: {relative}")
            expected_files.add(relative)
            path = _safe_archive_member(root, relative)
            if path.is_symlink():
                raise TaskGitError(f"archive contains a symbolic link: {relative}")
            if not path.is_file():
                raise TaskGitError(f"archived file is missing: {relative}")
            actual = sha256_file(path)
            if actual != digest:
                raise TaskGitError(
                    f"archive checksum mismatch for {relative}: expected {digest}, found {actual}"
                )
            checked += 1

        actual_files = {
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file() and path.relative_to(root).as_posix() != "SHA256SUMS"
        }
        if actual_files != expected_files:
            missing = sorted(expected_files - actual_files)
            unexpected = sorted(actual_files - expected_files)
            raise TaskGitError(
                "archive file inventory mismatch"
                + (f"; missing: {', '.join(missing)}" if missing else "")
                + (f"; unexpected: {', '.join(unexpected)}" if unexpected else "")
            )

        manifest_path = root / "completion-manifest.json"
        manifest = self._read_json(manifest_path)
        if int(manifest.get("schema_version", 0)) != _ARCHIVE_SCHEMA_VERSION:
            raise TaskGitError(f"unsupported completion manifest schema: {manifest_path}")
        manifest_sha256 = sha256_file(manifest_path)
        completion_path = root / "dossier" / "COMPLETION.yaml"
        try:
            completion = yaml.safe_load(completion_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise TaskGitError(f"invalid archived COMPLETION.yaml: {exc}") from exc
        if expected_manifest_sha256 and manifest_sha256 != expected_manifest_sha256:
            raise TaskGitError(
                "archive manifest digest does not match the archive index: "
                f"expected {expected_manifest_sha256}, found {manifest_sha256}"
            )
        recorded_manifest_digest = str(
            ((completion.get("archive") or {}) if isinstance(completion, Mapping) else {}).get(
                "manifest_sha256", ""
            )
        )
        if recorded_manifest_digest != manifest_sha256:
            raise TaskGitError(
                "COMPLETION.yaml manifest digest does not match completion-manifest.json"
            )
        return {
            "ok": True,
            "archive_path": str(root),
            "project": str(manifest.get("project", "")),
            "task_id": str(manifest.get("task_id", "")),
            "archive_id": str(manifest.get("archive_id", "")),
            "files_checked": checked,
            "manifest_sha256": manifest_sha256,
        }

    def _build_bundle(
        self,
        target: Path,
        *,
        manifest: TaskManifest,
        workspace: WorkspaceRecord,
        state: Mapping[str, Any],
        journal: list[Any],
        commit_journal: list[Any],
        archive_id: str,
        created_at: str,
        final_path: Path,
    ) -> dict[str, Any]:
        dossier_source = project_task_directory(self.control_root, manifest.project, manifest.id)
        dossier_target = target / "dossier"
        _copy_tree_without_symlinks(
            dossier_source,
            dossier_target,
            ignored_names={"COMPLETION.yaml"},
            ignored_suffixes={".tmp"},
        )

        _write_json(target / "state.json", state)
        _write_json(target / "event-journal.json", journal)
        _write_json(target / "commit-journal.json", commit_journal)
        _write_json(target / "workspace.json", workspace.as_mapping())

        repository_snapshots = self._repository_snapshots(workspace)
        _write_json(target / "repository-snapshots.json", repository_snapshots)
        verification_summary = _verification_summary(journal)
        _write_json(target / "verification-summary.json", verification_summary)

        project_state_dir = self._project_state_dir(manifest.project, manifest.id)
        artifacts_source = project_state_dir / "agent-artifacts"
        if artifacts_source.is_dir():
            _copy_tree_without_symlinks(artifacts_source, target / "agent-artifacts")
        log_source = project_state_dir / "orchestrator.log"
        if log_source.is_symlink():
            raise TaskGitError(f"orchestrator log must not be a symbolic link: {log_source}")
        if log_source.is_file():
            shutil.copy2(log_source, target / "orchestrator.log")
        sync_transactions_source = project_state_dir / "repository-sync"
        if sync_transactions_source.is_dir():
            _copy_tree_without_symlinks(
                sync_transactions_source, target / "repository-sync"
            )

        for database_name in (
            "agent-invocations.sqlite3",
            "orchestration-checkpoints.sqlite3",
        ):
            source = project_state_dir / database_name
            if source.is_symlink():
                raise TaskGitError(
                    f"orchestration database must not be a symbolic link: {source}"
                )
            if source.is_file():
                self._snapshot_sqlite_database(source, target / database_name)

        content_digests = _tree_digests(
            target,
            excluded={"completion-manifest.json", "SHA256SUMS", "dossier/COMPLETION.yaml"},
        )
        plan_path = dossier_source / "PLAN.graph.yaml"
        completion_manifest = {
            "schema_version": _ARCHIVE_SCHEMA_VERSION,
            "project": manifest.project,
            "task_id": manifest.id,
            "title": manifest.title,
            "archive_id": archive_id,
            "created_at": created_at,
            "final_state": str(state.get("state", "")),
            "task_status_before_close": manifest.status,
            "completed_packages": int(state.get("completed_packages", 0)),
            "total_packages": int(state.get("total_packages", 0)),
            "plan_sha256": sha256_file(plan_path) if plan_path.is_file() else "",
            "control_plane": {
                "branch": current_branch(self.control_root),
                "head_commit": head_commit(self.control_root),
                "dirty_before_archive": working_tree_dirty(self.control_root),
            },
            "repositories": repository_snapshots,
            "verification": verification_summary.get("totals", {}),
            "completion_record": "dossier/COMPLETION.yaml",
            "files": content_digests,
        }
        manifest_path = target / "completion-manifest.json"
        _write_json(manifest_path, completion_manifest)
        manifest_sha256 = sha256_file(manifest_path)

        completion_record = {
            "schema_version": _COMPLETION_SCHEMA_VERSION,
            "project": manifest.project,
            "task_id": manifest.id,
            "title": manifest.title,
            "status": "closed",
            "completed_at": str(state.get("last_transition_at") or created_at),
            "archived_at": created_at,
            "orchestration": {
                "final_state": str(state.get("state", "")),
                "completed_packages": int(state.get("completed_packages", 0)),
                "total_packages": int(state.get("total_packages", 0)),
                "plan_sha256": completion_manifest["plan_sha256"],
            },
            "repositories": [
                {
                    "id": item["repository_id"],
                    "branch": item["branch"],
                    "commit": item["head_commit"],
                    "mutability": item["mutability"],
                }
                for item in repository_snapshots
            ],
            "verification": verification_summary.get("totals", {}),
            "archive": {
                "archive_id": archive_id,
                "manifest_sha256": manifest_sha256,
                "local_path": str(final_path),
            },
        }
        _write_yaml(dossier_target / "COMPLETION.yaml", completion_record)
        _write_sha256sums(target)
        return {
            "manifest_sha256": manifest_sha256,
            "completion_record": completion_record,
        }

    @staticmethod
    def _snapshot_sqlite_database(source: Path, destination: Path) -> None:
        """Create a consistent SQLite backup without copying transient WAL files."""

        destination.parent.mkdir(parents=True, exist_ok=True)
        source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        destination_connection = sqlite3.connect(destination)
        try:
            source_connection.backup(destination_connection)
            destination_connection.execute("PRAGMA integrity_check")
            destination_connection.commit()
        finally:
            destination_connection.close()
            source_connection.close()

    def _materialize_live_completion(
        self,
        manifest: TaskManifest,
        result: ArchiveResult,
    ) -> None:
        archived_completion = result.archive_path / "dossier" / "COMPLETION.yaml"
        if not archived_completion.is_file():
            raise TaskGitError(f"archive completion record is missing: {archived_completion}")
        dossier = project_task_directory(self.control_root, manifest.project, manifest.id)
        completion_path = dossier / "COMPLETION.yaml"
        atomic_write_bytes(completion_path, archived_completion.read_bytes())
        completion_path.chmod(0o644)
        completion = yaml.safe_load(archived_completion.read_text(encoding="utf-8")) or {}
        repository_commits = {
            str(item.get("id", "")): str(item.get("commit", ""))
            for item in (completion.get("repositories") or [])
            if isinstance(item, Mapping)
        }
        for repository in manifest.repositories:
            if repository.id in repository_commits:
                repository.latest_commit = repository_commits[repository.id]
        if manifest.status != "closed" or repository_commits:
            manifest.status = "closed"
            write_manifest(self.control_root, manifest)

    def _append_archived_event_once(self, result: ArchiveResult) -> None:
        journal_path = self._journal_path(result.project, result.task_id)
        existing = self._read_json(journal_path) if journal_path.is_file() else []
        if isinstance(existing, list):
            for entry in existing:
                if not isinstance(entry, Mapping) or entry.get("event_type") != "task_archived":
                    continue
                payload = entry.get("payload") or {}
                if isinstance(payload, Mapping) and payload.get("archive_id") == result.archive_id:
                    return
        EventJournal(journal_path).append(
            "task_archived",
            {
                "project": result.project,
                "task_id": result.task_id,
                "archive_id": result.archive_id,
                "archive_path": str(result.archive_path),
                "manifest_sha256": result.manifest_sha256,
            },
        )

    def _repository_checks(
        self,
        workspace: WorkspaceRecord,
        manifest: TaskManifest,
    ) -> list[ArchiveCheck]:
        checks: list[ArchiveCheck] = []
        expected_branches = {
            repository.id: repository.task_branch for repository in manifest.repositories
        }
        for item in workspace.repositories:
            repository_id = str(item.get("id", ""))
            required = bool(item.get("required", True))
            mutability = str(item.get("mutability", "task_owned"))
            path = Path(str(item.get("worktree_path", ""))).expanduser()
            if not path.is_dir():
                checks.append(
                    ArchiveCheck(
                        f"repository:{repository_id}",
                        not required,
                        f"repository checkout is missing: {path}",
                        severity="error" if required else "warning",
                    )
                )
                continue
            if mutability != "task_owned":
                checks.append(
                    ArchiveCheck(
                        f"repository:{repository_id}",
                        True,
                        f"runtime-only repository present at {path}",
                    )
                )
                continue
            operation = git_operation(path)
            dirty = working_tree_dirty(path)
            branch = current_branch(path)
            expected_branch = expected_branches.get(repository_id, "")
            checks.append(
                ArchiveCheck(
                    f"repository:{repository_id}:branch",
                    not expected_branch or branch == expected_branch,
                    (
                        f"repository {repository_id} is on expected branch {branch!r}"
                        if not expected_branch or branch == expected_branch
                        else f"repository {repository_id} is on {branch!r}, expected {expected_branch!r}"
                    ),
                )
            )
            checks.append(
                ArchiveCheck(
                    f"repository:{repository_id}:clean",
                    not dirty,
                    (
                        f"repository {repository_id} is clean"
                        if not dirty
                        else f"repository {repository_id} has uncommitted changes"
                    ),
                )
            )
            checks.append(
                ArchiveCheck(
                    f"repository:{repository_id}:operation",
                    operation is None,
                    (
                        f"repository {repository_id} has no pending Git operation"
                        if operation is None
                        else f"repository {repository_id} has a pending {operation} operation"
                    ),
                )
            )
        return checks

    def _repository_snapshots(self, workspace: WorkspaceRecord) -> list[dict[str, Any]]:
        snapshots: list[dict[str, Any]] = []
        for item in workspace.repositories:
            path = Path(str(item["worktree_path"])).resolve()
            snapshots.append(
                {
                    "repository_id": str(item["id"]),
                    "branch": current_branch(path),
                    "head_commit": head_commit(path),
                    "dirty": working_tree_dirty(path),
                    "mutability": str(item.get("mutability", "task_owned")),
                    "required": bool(item.get("required", True)),
                    "worktree_path": str(path),
                }
            )
        return snapshots

    def _orchestrator_idle_check(self, project_id: str, task_id: str) -> ArchiveCheck:
        lock_path = self._project_state_dir(project_id, task_id) / "orchestrator.lock"
        if file_lock_is_held(lock_path):
            return ArchiveCheck(
                "orchestrator_idle",
                False,
                "orchestrator is currently running",
            )
        return ArchiveCheck("orchestrator_idle", True, "orchestrator is idle")

    def _load_state(
        self,
        path: Path,
        checks: list[ArchiveCheck],
    ) -> TaskExecutionStateRecord | None:
        if not path.is_file():
            checks.append(ArchiveCheck("state_file", False, f"state file is missing: {path}"))
            return None
        try:
            data = self._read_json(path)
            state = TaskExecutionStateRecord.from_mapping(data)
        except (OSError, ValueError, TypeError, KeyError, TaskGitError) as exc:
            checks.append(ArchiveCheck("state_file", False, f"invalid state file: {exc}"))
            return None
        checks.append(ArchiveCheck("state_file", True, f"state file loaded: {path}"))
        return state

    def _load_json_array(
        self,
        path: Path,
        label: str,
        checks: list[ArchiveCheck],
        *,
        missing_ok: bool = False,
    ) -> list[Any] | None:
        if not path.is_file():
            checks.append(
                ArchiveCheck(
                    label.replace(" ", "_"),
                    False,
                    f"{label} is missing: {path}",
                    severity="warning" if missing_ok else "error",
                )
            )
            return [] if missing_ok else None
        try:
            data = self._read_json(path)
        except (OSError, ValueError, TypeError) as exc:
            checks.append(
                ArchiveCheck(label.replace(" ", "_"), False, f"invalid {label}: {exc}")
            )
            return None
        if not isinstance(data, list):
            checks.append(
                ArchiveCheck(label.replace(" ", "_"), False, f"{label} must be a JSON array")
            )
            return None
        checks.append(
            ArchiveCheck(label.replace(" ", "_"), True, f"{label} loaded: {path}")
        )
        return data

    def _storage_identity(self, project_id: str, task_id: str):
        return resolve_storage_identity(
            self.state_root,
            project_id=project_id,
            task_id=task_id,
        )

    def _project_state_dir(self, project_id: str, task_id: str) -> Path:
        return self._storage_identity(project_id, task_id).state_dir

    def _journal_path(self, project_id: str, task_id: str) -> Path:
        return self._storage_identity(project_id, task_id).journal_path

    def _load_index(self) -> list[dict[str, Any]]:
        if not self.index_path.is_file():
            return []
        data = self._read_json(self.index_path)
        if not isinstance(data, list):
            raise TaskGitError(f"archive index must be a JSON array: {self.index_path}")
        return [dict(item) for item in data if isinstance(item, Mapping)]

    def _append_index_record(self, record: dict[str, Any]) -> None:
        records = self._load_index()
        identity = (
            str(record.get("project", "")),
            str(record.get("task_id", "")),
            str(record.get("archive_id", "")),
        )
        if any(
            (
                str(existing.get("project", "")),
                str(existing.get("task_id", "")),
                str(existing.get("archive_id", "")),
            )
            == identity
            for existing in records
        ):
            return
        records.append(record)
        _write_json(self.index_path, records)

    def _discover_records(
        self,
        *,
        project: str | None = None,
        task_id: str | None = None,
    ) -> list[dict[str, Any]]:
        base = self.archive_root
        if project:
            base = base / project
        if task_id:
            if not project:
                return []
            base = base / task_id
        if not base.is_dir():
            return []
        records: list[dict[str, Any]] = []
        for manifest_path in sorted(base.glob("**/completion-manifest.json")):
            archive_path = manifest_path.parent
            if any(part.startswith(".") for part in archive_path.relative_to(self.archive_root).parts):
                continue
            try:
                manifest = self._read_json(manifest_path)
            except TaskGitError:
                continue
            if not isinstance(manifest, Mapping):
                continue
            record = {
                "schema_version": _ARCHIVE_SCHEMA_VERSION,
                "project": str(manifest.get("project", "")),
                "task_id": str(manifest.get("task_id", "")),
                "title": str(manifest.get("title", "")),
                "archive_id": str(manifest.get("archive_id", archive_path.name)),
                "created_at": str(manifest.get("created_at", "")),
                "path": str(archive_path.resolve()),
                "manifest_sha256": sha256_file(manifest_path),
                "status": "closed",
            }
            if project and record["project"] != project:
                continue
            if task_id and record["task_id"] != task_id:
                continue
            records.append(record)
        return records

    def _latest_record(self, project: str, task_id: str) -> dict[str, Any] | None:
        records_by_id = {
            str(record.get("archive_id", "")): record
            for record in self._load_index()
            if record.get("project") == project and record.get("task_id") == task_id
        }
        for record in self._discover_records(project=project, task_id=task_id):
            records_by_id.setdefault(str(record.get("archive_id", "")), record)
        matches = list(records_by_id.values())
        if not matches:
            return None
        return sorted(matches, key=lambda item: str(item.get("created_at", "")))[-1]

    def _result_from_record(self, record: Mapping[str, Any], *, created: bool) -> ArchiveResult:
        archive_path = Path(str(record["path"])).expanduser().resolve()
        return ArchiveResult(
            project=str(record["project"]),
            task_id=str(record["task_id"]),
            archive_id=str(record["archive_id"]),
            archive_path=archive_path,
            manifest_path=archive_path / "completion-manifest.json",
            manifest_sha256=str(record["manifest_sha256"]),
            created=created,
        )

    def _read_json(self, path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise TaskGitError(f"invalid JSON file {path}: {exc}") from exc

    @contextmanager
    def _exclusive_archive_lock(self) -> Iterator[None]:
        self.archive_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.archive_root.chmod(0o700)
        try:
            with FileLock(
                self._lock_path,
                level=LockLevel.LIFECYCLE,
                timeout=0.0,
            ):
                yield
        except LockBusyError as exc:
            raise TaskGitError("another task archive operation is already running") from exc
