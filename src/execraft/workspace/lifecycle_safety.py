"""Deterministic safety inspection for task workspace lifecycle actions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Protocol

import yaml

from execraft.archive import TaskArchiveManager
from execraft.orchestrate.identity import resolve_storage_identity
from execraft.persistence import LockUnavailableError, file_lock_is_held
from execraft.repository_sync.transaction import RepositorySyncTransactionStore
from execraft.workspace.lifecycle_types import LifecycleCheck, LifecycleReport
from execraft.workspace.task_git import (
    TaskGitError,
    TaskManifest,
    current_branch,
    git_operation,
    head_commit,
    load_manifest,
    project_task_directory,
    working_tree_dirty,
)
from execraft.workspace.workspace_git import (
    WorkspaceRecord,
    load_workspace,
    parse_worktree_list,
)

PINNED_MARKER = Path(".execraft") / "pinned"
WORKSPACE_MARKER = Path(".execraft") / "workspace.yaml"


class ArchiveVerifier(Protocol):
    """Verify that a task has an immutable completion archive."""

    def verify(self, manifest: TaskManifest) -> LifecycleCheck:
        """Return one archive safety check without mutating task state."""


class CompletionArchiveVerifier:
    """Adapter around :class:`TaskArchiveManager` for lifecycle preflight."""

    def __init__(
        self,
        control_root: Path,
        state_root: Path,
        *,
        archive_root: Path | None = None,
    ) -> None:
        self._control_root = control_root.expanduser().resolve()
        self._manager = TaskArchiveManager(
            self._control_root,
            state_root,
            archive_root=archive_root,
        )

    def verify(self, manifest: TaskManifest) -> LifecycleCheck:
        try:
            result = self._manager.latest(manifest.project, manifest.id)
            self._manager.verify(
                result.archive_path,
                expected_manifest_sha256=result.manifest_sha256,
            )
            self._verify_current_completion(
                manifest, result.archive_path, result.archive_id, result.manifest_sha256
            )
        except TaskGitError as exc:
            return LifecycleCheck(
                "completion_archive",
                False,
                f"verified completion archive is required: {exc}",
            )
        return LifecycleCheck(
            "completion_archive",
            True,
            f"completion archive verified and current: {result.archive_path}",
        )

    def _verify_current_completion(
        self,
        manifest: TaskManifest,
        archive_path: Path,
        archive_id: str,
        manifest_sha256: str,
    ) -> None:
        """Prove that no durable task input or final commit changed later.

        Archive checksum validation alone proves that the bundle is intact, not
        that it still describes the live task. A later clean commit or dossier
        edit must therefore invalidate workspace retirement until a new completion
        revision is archived.
        """

        if manifest.status != "closed":
            raise TaskGitError(
                f"task status changed after archiving: expected 'closed', found {manifest.status!r}"
            )
        dossier = project_task_directory(self._control_root, manifest.project, manifest.id)
        live_completion = self._read_yaml_mapping(dossier / "COMPLETION.yaml")
        if (
            str(live_completion.get("project", "")) != manifest.project
            or str(live_completion.get("task_id", "")) != manifest.id
        ):
            raise TaskGitError("live COMPLETION.yaml does not match the current task identity")
        archive_data = live_completion.get("archive") or {}
        if not isinstance(archive_data, Mapping):
            raise TaskGitError("live COMPLETION.yaml archive record must be a mapping")
        if (
            str(archive_data.get("archive_id", "")) != archive_id
            or str(archive_data.get("manifest_sha256", "")) != manifest_sha256
        ):
            raise TaskGitError(
                "live COMPLETION.yaml does not reference the verified latest archive"
            )

        archived_dossier = archive_path / "dossier"
        archived_manifest = TaskManifest.from_mapping(
            self._read_yaml_mapping(archived_dossier / "TASK.yaml")
        )
        if self._manifest_definition(manifest) != self._manifest_definition(archived_manifest):
            raise TaskGitError("TASK.yaml definition changed after archiving")

        excluded = {"TASK.yaml", "COMPLETION.yaml"}
        live_digests = self._directory_digests(dossier, excluded=excluded)
        archived_digests = self._directory_digests(archived_dossier, excluded=excluded)
        if live_digests != archived_digests:
            changed = sorted(
                relative
                for relative in set(live_digests) | set(archived_digests)
                if live_digests.get(relative) != archived_digests.get(relative)
            )
            raise TaskGitError("task dossier changed after archiving: " + ", ".join(changed))

        workspace = load_workspace(self._control_root, manifest.id)
        repositories = {str(item.get("id", "")): item for item in workspace.repositories}
        completion_repositories = live_completion.get("repositories") or []
        if not isinstance(completion_repositories, list):
            raise TaskGitError("live COMPLETION.yaml repositories must be a list")
        archived_by_id: dict[str, Mapping[str, Any]] = {}
        for archived in completion_repositories:
            if not isinstance(archived, Mapping):
                raise TaskGitError("live COMPLETION.yaml contains an invalid repository record")
            repository_id = str(archived.get("id", ""))
            if not repository_id or repository_id in archived_by_id:
                raise TaskGitError(
                    "live COMPLETION.yaml contains an invalid or duplicate repository ID"
                )
            archived_by_id[repository_id] = archived

        task_owned_ids = {
            repository_id
            for repository_id, item in repositories.items()
            if item.get("mutability", "task_owned") == "task_owned"
        }
        archived_task_owned_ids = {
            repository_id
            for repository_id, item in archived_by_id.items()
            if str(item.get("mutability", "task_owned")) == "task_owned"
        }
        if task_owned_ids != archived_task_owned_ids:
            raise TaskGitError(
                "task-owned repository set changed after archiving: "
                f"expected {sorted(archived_task_owned_ids)}, found {sorted(task_owned_ids)}"
            )

        for repository_id in sorted(task_owned_ids):
            item = repositories[repository_id]
            archived = archived_by_id[repository_id]
            expected_branch = str(archived.get("branch", ""))
            expected_commit = str(archived.get("commit", ""))
            source = Path(str(item.get("source_path", ""))).expanduser().resolve()
            if not source.is_dir():
                raise TaskGitError(
                    f"cannot verify archived repository source {repository_id}: {source}"
                )
            if expected_branch:
                branch_commit = head_commit(source, expected_branch)
                if branch_commit != expected_commit:
                    raise TaskGitError(
                        f"repository {repository_id} changed after archiving: "
                        f"expected {expected_commit}, found {branch_commit}"
                    )

    @staticmethod
    def _manifest_definition(manifest: TaskManifest) -> tuple[Any, ...]:
        repositories = tuple(
            (
                repository.id,
                repository.path,
                repository.base_branch,
                repository.task_branch,
                repository.role,
                repository.required,
                repository.start_commit,
                tuple(repository.verify),
                repository.mutability,
            )
            for repository in manifest.repositories
        )
        return (
            manifest.schema_version,
            manifest.id,
            manifest.project,
            manifest.title,
            manifest.created_at,
            manifest.branch_name,
            manifest.merge_strategy,
            manifest.integration_branch,
            repositories,
            tuple(manifest.integration_verify),
        )

    @staticmethod
    def _read_yaml_mapping(path: Path) -> Mapping[str, Any]:
        if path.is_symlink() or not path.is_file():
            raise TaskGitError(f"completion record is missing or unsafe: {path}")
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise TaskGitError(f"invalid completion record {path}: {exc}") from exc
        if not isinstance(data, Mapping):
            raise TaskGitError(f"completion record must contain a mapping: {path}")
        return data

    @staticmethod
    def _directory_digests(
        root: Path,
        *,
        excluded: set[str],
    ) -> dict[str, str]:
        if root.is_symlink() or not root.is_dir():
            raise TaskGitError(f"task dossier is missing or unsafe: {root}")
        digests: dict[str, str] = {}
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise TaskGitError(f"task dossier contains a symbolic link: {path}")
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            if relative in excluded:
                continue
            digest = hashlib.sha256()
            try:
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
            except OSError as exc:
                raise TaskGitError(f"cannot hash task dossier file {path}: {exc}") from exc
            digests[relative] = digest.hexdigest()
        return digests


class WorkspaceSafetyInspector:
    """Collect lifecycle checks without performing destructive operations."""

    def __init__(
        self,
        control_root: Path,
        state_root: Path,
        *,
        archive_verifier: ArchiveVerifier,
    ) -> None:
        self.control_root = control_root.expanduser().resolve()
        self.state_root = state_root.expanduser().resolve()
        self.archive_verifier = archive_verifier

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
        if operation not in {"stop", "destroy"}:
            raise ValueError(f"unsupported lifecycle operation: {operation}")
        checks: list[LifecycleCheck] = []
        workspace_root = Path(record.workspace_root).expanduser().resolve()
        marker_data = self._load_marker(workspace_root, checks)
        checks.extend(self._identity_checks(record, marker_data, workspace_root))
        pinned = (workspace_root / PINNED_MARKER).exists()
        if not pinned:
            pinned_message = "workspace is not pinned"
        elif allow_pinned:
            pinned_message = "workspace is pinned; the explicit lifecycle operation permits it"
        else:
            pinned_message = "workspace is pinned and cannot be removed automatically"
        checks.append(
            LifecycleCheck(
                "pinned",
                allow_pinned or not pinned,
                pinned_message,
            )
        )

        manifest = self._load_manifest(record.task_id, checks)
        if manifest is not None:
            checks.append(
                LifecycleCheck(
                    "orchestrator_idle",
                    True,
                    "orchestrator is idle and exclusively locked for lifecycle work",
                )
                if orchestrator_lock_held
                else self._orchestrator_idle_check(manifest)
            )
            checks.append(self._pending_commit_check(manifest))
            checks.append(self._pending_repository_sync_check(manifest))
            if require_archive:
                checks.append(self.archive_verifier.verify(manifest))

        if check_repositories:
            checks.extend(self._repository_checks(record))

        return LifecycleReport(
            task_id=record.task_id,
            operation=operation,
            checks=checks,
        )

    def validate_shell_removal(self, record: WorkspaceRecord) -> None:
        """Reject deletion of unmarked shells or user-owned entries.

        Nested repository workspace names are supported. Ancestor directories
        are traversed rather than trusted wholesale, so an unowned sibling next
        to a nested worktree cannot be deleted with the generated shell.
        """

        workspace_root = Path(record.workspace_root).expanduser().resolve()
        metadata_root = workspace_root / ".execraft"
        marker = metadata_root / "workspace.yaml"
        if (
            metadata_root.is_symlink()
            or not metadata_root.is_dir()
            or marker.is_symlink()
            or not marker.is_file()
        ):
            raise TaskGitError(f"refusing to remove unmarked workspace shell: {workspace_root}")

        generated_directories = {
            ".execraft",
            ".vscode",
            ".agents",
            ".claude",
            ".codex",
            ".devcontainer",
            ".gemini",
            ".opencode",
        }
        generated_files = {
            "AGENTS.md",
            "CLAUDE.md",
            "opencode.json",
            f"{record.task_id}.code-workspace",
        }
        worktree_roots: set[Path] = set()
        worktree_ancestors: set[Path] = set()
        for item in record.repositories:
            worktree = Path(str(item["worktree_path"])).expanduser().resolve()
            source = Path(str(item["source_path"])).expanduser().resolve()
            if worktree == source:
                continue
            try:
                relative = worktree.relative_to(workspace_root)
            except ValueError as exc:
                raise TaskGitError(
                    f"recorded worktree escapes workspace shell: {worktree}"
                ) from exc
            if not relative.parts:
                raise TaskGitError(
                    f"recorded worktree cannot equal the workspace shell: {worktree}"
                )
            worktree_roots.add(worktree)
            parent = relative.parent
            while parent != Path("."):
                worktree_ancestors.add(workspace_root / parent)
                parent = parent.parent

        unknown: list[str] = []

        def inspect(path: Path) -> None:
            relative = path.relative_to(workspace_root)
            relative_text = relative.as_posix()
            if path in worktree_roots:
                return
            if relative.parts[0] in generated_directories:
                if path == workspace_root / relative.parts[0] and path.is_symlink():
                    unknown.append(relative_text)
                return
            if relative_text in generated_files:
                if path.is_symlink() or not path.is_file():
                    unknown.append(relative_text)
                return
            if path in worktree_ancestors and path.is_dir() and not path.is_symlink():
                for child in sorted(path.iterdir(), key=lambda item: item.name):
                    inspect(child)
                return
            unknown.append(relative_text)

        for child in sorted(workspace_root.iterdir(), key=lambda item: item.name):
            inspect(child)
        if unknown:
            raise TaskGitError(
                "refusing to remove workspace shell with unowned entries: " + ", ".join(unknown)
            )

    def _load_marker(
        self,
        workspace_root: Path,
        checks: list[LifecycleCheck],
    ) -> Mapping[str, Any] | None:
        marker = workspace_root / WORKSPACE_MARKER
        if not workspace_root.is_dir():
            checks.append(
                LifecycleCheck(
                    "workspace_root",
                    False,
                    f"workspace root is missing: {workspace_root}",
                    severity="fatal",
                )
            )
            return None
        if marker.is_symlink() or not marker.is_file():
            checks.append(
                LifecycleCheck(
                    "workspace_marker",
                    False,
                    f"workspace ownership marker is missing or unsafe: {marker}",
                    severity="fatal",
                )
            )
            return None
        try:
            data = yaml.safe_load(marker.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            checks.append(
                LifecycleCheck(
                    "workspace_marker",
                    False,
                    f"invalid workspace ownership marker: {exc}",
                    severity="fatal",
                )
            )
            return None
        if not isinstance(data, Mapping):
            checks.append(
                LifecycleCheck(
                    "workspace_marker",
                    False,
                    "workspace ownership marker must contain a mapping",
                    severity="fatal",
                )
            )
            return None
        checks.append(
            LifecycleCheck(
                "workspace_marker",
                True,
                f"workspace ownership marker loaded: {marker}",
            )
        )
        return data

    def _identity_checks(
        self,
        record: WorkspaceRecord,
        marker: Mapping[str, Any] | None,
        workspace_root: Path,
    ) -> list[LifecycleCheck]:
        source_roots = [
            Path(str(item["source_path"])).expanduser().resolve() for item in record.repositories
        ]
        safe_location = (
            workspace_root != self.control_root
            and self.control_root not in workspace_root.parents
            and workspace_root not in self.control_root.parents
            and all(
                workspace_root != source
                and source not in workspace_root.parents
                and workspace_root not in source.parents
                for source in source_roots
            )
        )
        checks = [
            LifecycleCheck(
                "workspace_location",
                safe_location,
                (
                    "workspace shell is outside control and source repositories"
                    if safe_location
                    else "workspace shell overlaps the control plane or a source repository"
                ),
                severity="fatal",
            )
        ]
        if marker is None:
            return checks
        marker_root = Path(str(marker.get("workspace_root", ""))).expanduser().resolve()
        identity_ok = (
            str(marker.get("task_id", "")) == record.task_id
            and marker_root == workspace_root
            and self._immutable_identity(marker) == self._immutable_identity(record.as_mapping())
        )
        checks.append(
            LifecycleCheck(
                "workspace_identity",
                identity_ok,
                (
                    "workspace marker and registry identity match"
                    if identity_ok
                    else "workspace marker and registry identity do not match"
                ),
                severity="fatal",
            )
        )
        return checks

    @staticmethod
    def _immutable_identity(data: Mapping[str, Any]) -> tuple[Any, ...]:
        """Return marker fields that must never drift from the registry.

        Runtime status and lifecycle timestamps intentionally remain excluded so
        the shell marker can stay immutable while the registry records progress.
        Repository order is retained because it is part of workspace generation.
        """

        repositories: list[tuple[str, ...]] = []
        raw_repositories = data.get("repositories") or []
        if isinstance(raw_repositories, list):
            for item in raw_repositories:
                if not isinstance(item, Mapping):
                    repositories.append(("<invalid>",))
                    continue
                repositories.append(
                    tuple(
                        str(item.get(key, ""))
                        for key in (
                            "id",
                            "source_path",
                            "worktree_path",
                            "branch",
                            "role",
                            "mutability",
                        )
                    )
                )
        return (
            str(data.get("task_id", "")),
            str(data.get("source_root", "")),
            str(data.get("workspace_root", "")),
            str(data.get("compose_project", "")),
            tuple(repositories),
        )

    def _load_manifest(
        self,
        task_id: str,
        checks: list[LifecycleCheck],
    ) -> TaskManifest | None:
        try:
            manifest = load_manifest(self.control_root, task_id)
        except TaskGitError as exc:
            checks.append(LifecycleCheck("task_manifest", False, str(exc)))
            return None
        checks.append(
            LifecycleCheck(
                "task_manifest",
                True,
                f"task manifest loaded for {manifest.project}/{manifest.id}",
            )
        )
        return manifest

    def _orchestrator_idle_check(self, manifest: TaskManifest) -> LifecycleCheck:
        identity = resolve_storage_identity(
            self.state_root,
            project_id=manifest.project,
            task_id=manifest.id,
            create=False,
        )
        lock_path = identity.state_dir / "orchestrator.lock"
        try:
            active = file_lock_is_held(lock_path)
        except LockUnavailableError:
            active = False
        if active:
            return LifecycleCheck(
                "orchestrator_idle",
                False,
                "orchestrator is currently running",
            )
        return LifecycleCheck("orchestrator_idle", True, "orchestrator is idle")

    def _pending_repository_sync_check(self, manifest: TaskManifest) -> LifecycleCheck:
        identity = resolve_storage_identity(
            self.state_root,
            project_id=manifest.project,
            task_id=manifest.id,
            create=False,
        )
        try:
            pending = RepositorySyncTransactionStore(
                identity.state_dir
            ).pending_transactions()
        except Exception as exc:
            return LifecycleCheck(
                "repository_sync_transactions",
                False,
                f"repository-sync transaction state is invalid: {exc}",
            )
        if not pending:
            return LifecycleCheck(
                "repository_sync_transactions",
                True,
                "no pending repository-sync transactions",
            )
        return LifecycleCheck(
            "repository_sync_transactions",
            False,
            "pending repository-sync transactions: "
            + ", ".join(item.transaction_id for item in pending),
        )

    def _pending_commit_check(self, manifest: TaskManifest) -> LifecycleCheck:
        identity = resolve_storage_identity(
            self.state_root,
            project_id=manifest.project,
            task_id=manifest.id,
            create=False,
        )
        path = identity.state_dir / "commit-journal.json"
        if not path.is_file():
            return LifecycleCheck(
                "commit_transactions",
                True,
                "no commit journal is present",
            )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return LifecycleCheck(
                "commit_transactions",
                False,
                f"commit journal is invalid: {exc}",
            )
        if not isinstance(data, list):
            return LifecycleCheck(
                "commit_transactions",
                False,
                "commit journal must be a JSON array",
            )
        pending = [
            str(item.get("transaction_id", "unknown"))
            for item in data
            if isinstance(item, Mapping) and item.get("status") == "pending"
        ]
        return LifecycleCheck(
            "commit_transactions",
            not pending,
            (
                "no pending commit transactions"
                if not pending
                else "pending commit transactions: " + ", ".join(pending)
            ),
        )

    @staticmethod
    def _repository_checks(record: WorkspaceRecord) -> list[LifecycleCheck]:
        """Validate repository ownership before considering mutable Git state.

        Route and registration checks are fatal because force mode may bypass
        ordinary lifecycle policy, but must never be able to remove an arbitrary
        directory or a worktree owned by a different source repository.
        """

        checks: list[LifecycleCheck] = []
        workspace_root = Path(record.workspace_root).expanduser().resolve()
        for item in record.repositories:
            if item.get("mutability", "task_owned") != "task_owned":
                continue
            repository_id = str(item.get("id", "unknown"))
            source = Path(str(item.get("source_path", ""))).expanduser().resolve()
            path = Path(str(item.get("worktree_path", ""))).expanduser().resolve()
            generated_worktree = path != source

            source_ok = source.is_dir()
            checks.append(
                LifecycleCheck(
                    f"repository:{repository_id}:source",
                    source_ok,
                    (
                        f"repository {repository_id} source exists: {source}"
                        if source_ok
                        else f"repository {repository_id} source is missing: {source}"
                    ),
                    severity="fatal",
                )
            )

            route_ok = not generated_worktree or WorkspaceSafetyInspector._is_within(
                path, workspace_root
            )
            checks.append(
                LifecycleCheck(
                    f"repository:{repository_id}:route",
                    route_ok,
                    (
                        f"repository {repository_id} worktree route is owned by the workspace"
                        if route_ok
                        else (
                            f"repository {repository_id} worktree escapes workspace shell: {path}"
                        )
                    ),
                    severity="fatal",
                )
            )
            if not source_ok or not route_ok:
                continue

            if not path.is_dir():
                checks.append(
                    LifecycleCheck(
                        f"repository:{repository_id}:present",
                        generated_worktree,
                        (
                            f"repository worktree is already absent: {path}"
                            if generated_worktree
                            else f"in-place source repository is missing: {path}"
                        ),
                        severity="warning" if generated_worktree else "fatal",
                    )
                )
                continue

            registration_ok = True
            registration_message = f"repository {repository_id} uses its in-place source checkout"
            if generated_worktree:
                try:
                    entries = parse_worktree_list(source)
                    registered = next((entry for entry in entries if entry.path == path), None)
                except TaskGitError as exc:
                    registered = None
                    registration_message = (
                        f"cannot verify worktree ownership for {repository_id}: {exc}"
                    )
                else:
                    registration_ok = registered is not None
                    registration_message = (
                        f"repository {repository_id} worktree is registered by its source"
                        if registration_ok
                        else (
                            f"repository {repository_id} path is not a registered worktree "
                            f"of {source}: {path}"
                        )
                    )
            checks.append(
                LifecycleCheck(
                    f"repository:{repository_id}:registered",
                    registration_ok,
                    registration_message,
                    severity="fatal",
                )
            )
            if not registration_ok:
                continue

            expected_branch = str(item.get("branch", "")).strip()
            if expected_branch:
                try:
                    actual_branch = current_branch(path)
                except TaskGitError as exc:
                    branch_ok = False
                    branch_message = str(exc)
                else:
                    branch_ok = actual_branch == expected_branch
                    branch_message = (
                        f"repository {repository_id} is on expected branch {expected_branch}"
                        if branch_ok
                        else (
                            f"repository {repository_id} branch mismatch: expected "
                            f"{expected_branch}, found {actual_branch}"
                        )
                    )
                checks.append(
                    LifecycleCheck(
                        f"repository:{repository_id}:branch",
                        branch_ok,
                        branch_message,
                        severity="fatal",
                    )
                )
                if not branch_ok:
                    continue

            operation = git_operation(path)
            dirty = working_tree_dirty(path)
            checks.append(
                LifecycleCheck(
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
                LifecycleCheck(
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

    @staticmethod
    def _is_within(path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
        except ValueError:
            return False
        return path != parent
