"""Deterministic Git engine for repository-synchronization work packages."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from execraft.git import GitClient
from execraft.persistence import FileLock, LockLevel
from execraft.workspace.task_git import (
    RepositorySpec,
    TaskGitError,
    TaskManifest,
    current_branch,
    git,
    git_operation,
    head_commit,
    lookup_repository,
    working_tree_dirty,
)

from .spec import RepositorySyncSpec, validate_remote_name
from .transaction import (
    RepositorySyncRepositoryState,
    RepositorySyncTransaction,
    RepositorySyncTransactionStore,
)


class RepositorySyncError(RuntimeError):
    """Raised when an upstream synchronization cannot proceed safely."""


@dataclass(frozen=True)
class RepositoryDivergence:
    repository_id: str
    remote: str
    source_branch: str
    source_commit: str
    target_branch: str
    target_commit: str
    merge_base: str
    ahead: int
    behind: int
    dirty: bool = False
    git_operation: str = ""
    error: str = ""

    def as_mapping(self) -> dict[str, Any]:
        return {
            "repository_id": self.repository_id,
            "remote": self.remote,
            "source_branch": self.source_branch,
            "source_commit": self.source_commit,
            "target_branch": self.target_branch,
            "target_commit": self.target_commit,
            "merge_base": self.merge_base,
            "ahead": self.ahead,
            "behind": self.behind,
            "dirty": self.dirty,
            "git_operation": self.git_operation,
            "error": self.error,
        }


@dataclass(frozen=True)
class RemoteBranchOption:
    repository_id: str
    remote: str
    branch: str
    commit: str
    configured_base: bool = False
    locally_tracked: bool = False

    def as_mapping(self) -> dict[str, Any]:
        return {
            "repository_id": self.repository_id,
            "remote": self.remote,
            "branch": self.branch,
            "commit": self.commit,
            "configured_base": self.configured_base,
            "locally_tracked": self.locally_tracked,
        }


@dataclass(frozen=True)
class RepositorySyncPrepareResult:
    transaction: RepositorySyncTransaction
    transaction_path: Path
    conflict_paths: Mapping[str, tuple[str, ...]]

    @property
    def needs_resolution(self) -> bool:
        return any(self.conflict_paths.values())


class RepositorySyncService:
    """Own deterministic fetch/merge/commit state for one task.

    The service deliberately never invokes an LLM.  When ``prepare`` leaves
    conflicts, the orchestrator may invoke a bounded conflict-resolution agent;
    ``accept_resolution`` then re-establishes the Git invariants before normal
    verification/review continues.
    """

    def __init__(
        self,
        *,
        state_dir: Path,
        manifest: TaskManifest,
        repository_paths: Mapping[str, Path],
    ) -> None:
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.manifest = manifest
        self.repository_paths = {
            str(key): Path(value).expanduser().resolve()
            for key, value in repository_paths.items()
        }
        self.transactions = RepositorySyncTransactionStore(self.state_dir)
        # Repository refs live in the shared Git common directory, so fetch/ref
        # locks must also be shared by every Execraft task under one state root.
        # Task-local locks would allow two long-running tasks to race while
        # updating the same origin/* refs in sibling worktrees.
        state_parent = self.state_dir.parent
        self.lock_root = (
            state_parent.parent / "repository-ref-locks"
            if state_parent.name == "projects"
            else state_parent / "repository-ref-locks"
        )

    def prepare(
        self,
        *,
        package_id: str,
        package_fingerprint: str,
        spec: RepositorySyncSpec,
    ) -> RepositorySyncPrepareResult:
        """Fetch, pin and merge selected upstream heads without committing."""

        try:
            transaction = self.transactions.load(package_id)
            if transaction is None:
                transaction = self._create_transaction(
                    package_id=package_id,
                    package_fingerprint=package_fingerprint,
                    spec=spec,
                )
            else:
                self._validate_transaction_contract(transaction, package_fingerprint, spec)
                if transaction.complete:
                    return self._prepare_result(transaction)
            self._resume_prepare(transaction)
            return self._prepare_result(transaction)
        except RepositorySyncError:
            raise
        except (TaskGitError, OSError, ValueError) as exc:
            raise RepositorySyncError(f"repository synchronization prepare failed: {exc}") from exc

    def accept_resolution(self, package_id: str) -> RepositorySyncTransaction:
        """Stage agent-resolved conflicts after checking for conflict markers.

        Conflict-resolution agents are intentionally forbidden from running
        ``git add``.  A path therefore remains in the unmerged index until the
        control plane stages it.  We first reject obvious leftover textual
        conflict markers, then stage the candidate and require the index to be
        conflict-free.
        """

        try:
            transaction = self._require_transaction(package_id)
            if transaction.complete:
                return transaction
            if transaction.forward_only:
                raise RepositorySyncError(
                    "cannot accept conflict resolution after repository-sync commits began"
                )
            for item in transaction.repositories:
                if item.status in {"noop", "committed"}:
                    continue
                path = self._path(item.repository_id)
                self._require_target_identity(item, path, allow_committed=False)
                merge_head = self._merge_head(path)
                if merge_head != item.source_commit:
                    raise RepositorySyncError(
                        f"repository {item.repository_id} MERGE_HEAD changed: "
                        f"expected {item.source_commit}, found {merge_head or 'none'}"
                    )
                conflicts = self._unmerged_paths(path)
                if conflicts:
                    marker_paths = self._conflict_marker_paths(path, conflicts)
                    if marker_paths:
                        item.conflict_paths = list(conflicts)
                        item.status = "conflicted"
                        self._save(transaction, phase="resolving")
                        raise RepositorySyncError(
                            f"repository {item.repository_id} still contains merge conflict markers: "
                            + ", ".join(marker_paths)
                        )
                    # The control plane, not the agent, owns index mutation.
                    git(path, "add", "-A")
                    conflicts = self._unmerged_paths(path)
                item.conflict_paths = list(conflicts)
                if conflicts:
                    item.status = "conflicted"
                    self._save(transaction, phase="resolving")
                    raise RepositorySyncError(
                        f"repository {item.repository_id} still has unresolved merge paths: "
                        + ", ".join(conflicts)
                    )
                # Also stage non-conflict compatibility edits made by the resolver.
                git(path, "add", "-A")
                item.status = "resolved"
                item.verified_tree = ""
                item.error_message = ""
            self._save(transaction, phase="ready_verify")
            return transaction
        except RepositorySyncError:
            raise
        except (TaskGitError, OSError, ValueError) as exc:
            raise RepositorySyncError(
                f"repository synchronization resolution validation failed: {exc}"
            ) from exc

    def mark_verified(self, package_id: str) -> RepositorySyncTransaction:
        """Freeze the exact verified candidate tree for every mutable repository."""

        try:
            transaction = self._require_transaction(package_id)
            if transaction.complete:
                return transaction
            # A FIX_REVIEW agent resolving an "upstream not integrated" finding
            # may commit the pinned merge itself. The resulting commit is
            # indistinguishable from the one this service would have created,
            # but the transaction still records the repository as ``resolved``,
            # so re-entering verification rejected it for a HEAD that "changed
            # during sync" and parked the run in human_required. Every retry
            # then re-ran the whole profile and failed identically. Adopt those
            # merges here exactly as the commit path already does, so
            # verification is re-entrant instead of terminally inconsistent.
            self._reconcile_committed_after_crash(transaction)
            for item in transaction.repositories:
                if item.status in {"noop", "committed"}:
                    continue
                path = self._path(item.repository_id)
                # Verification runs against the worktree, so include any
                # compatibility fixes made during FIX_REVIEW before pinning the
                # candidate tree.
                git(path, "add", "-A")
            self._validate_merge_candidates(transaction)
            for item in transaction.repositories:
                if item.status in {"noop", "committed"}:
                    continue
                path = self._path(item.repository_id)
                item.verified_tree = git(path, "write-tree")
            # An adopted merge is already irreversible: keep the committing
            # phase rather than reporting the transaction as merely verified.
            self._save(
                transaction,
                phase="committing" if transaction.forward_only else "verified",
            )
            return transaction
        except RepositorySyncError:
            raise
        except (TaskGitError, OSError, ValueError) as exc:
            raise RepositorySyncError(
                f"repository synchronization verification freeze failed: {exc}"
            ) from exc

    def commit(self, package_id: str, *, title: str) -> RepositorySyncTransaction:
        """Create merge commits, normalizing low-level Git failures."""

        try:
            return self._commit(package_id, title=title)
        except RepositorySyncError:
            raise
        except (TaskGitError, OSError, ValueError) as exc:
            raise RepositorySyncError(
                f"repository synchronization commit failed: {exc}"
            ) from exc

    def _commit(self, package_id: str, *, title: str) -> RepositorySyncTransaction:
        """Create merge commits, persisting after each repository.

        Every candidate is validated before the first commit.  Once any commit
        succeeds ``forward_only`` becomes durable; later recovery must finish
        the remaining repositories instead of resetting already-committed
        history.
        """

        transaction = self._require_transaction(package_id)
        if transaction.complete:
            self._validate_completed(transaction)
            return transaction
        self._reconcile_committed_after_crash(transaction)
        for item in transaction.repositories:
            if item.status != "committed":
                continue
            path = self._path(item.repository_id)
            self._validate_committed_item(item, path)
            if git_operation(path) or working_tree_dirty(path):
                raise RepositorySyncError(
                    f"repository {item.repository_id} is not clean after its recorded sync commit"
                )
        uncommitted = [
            item
            for item in transaction.repositories
            if item.status not in {"noop", "committed"}
        ]
        # Preflight all remaining repositories before crossing the first commit
        # boundary.  In forward-only recovery this still catches corruption
        # before creating another commit.
        for item in uncommitted:
            path = self._path(item.repository_id)
            self._require_target_identity(item, path, allow_committed=False)
            conflicts = self._unmerged_paths(path)
            if conflicts:
                raise RepositorySyncError(
                    f"repository {item.repository_id} cannot commit with unresolved paths: "
                    + ", ".join(conflicts)
                )
            if self._merge_head(path) != item.source_commit:
                raise RepositorySyncError(
                    f"repository {item.repository_id} no longer has the expected merge in progress"
                )
            if not item.verified_tree:
                raise RepositorySyncError(
                    f"repository {item.repository_id} has no verified candidate tree"
                )
            current_tree = git(path, "write-tree")
            if current_tree != item.verified_tree:
                raise RepositorySyncError(
                    f"repository {item.repository_id} candidate changed after verification"
                )
            if self._has_unstaged_or_untracked_changes(path):
                raise RepositorySyncError(
                    f"repository {item.repository_id} has changes not present in the verified index"
                )
        self._save(transaction, phase="committing")
        for item in transaction.repositories:
            if item.status in {"noop", "committed"}:
                continue
            path = self._path(item.repository_id)
            message = (
                f"execraft-sync({package_id}): merge {item.remote}/{item.source_branch}\n\n"
                f"Pinned upstream: {item.source_commit}\n"
                f"Synchronization Work Package: {title}"
            )
            try:
                git(path, "commit", "-m", message)
                after = head_commit(path)
                if not self._is_expected_merge_commit(path, item, after):
                    raise RepositorySyncError(
                        f"repository {item.repository_id} did not create the expected pinned merge commit"
                    )
                item.target_after = after
                item.status = "committed"
                item.error_message = ""
                transaction.forward_only = True
                # Persist the irreversible commit before any post-commit check.
                # A failure from this point onward must recover forward-only.
                self._save(transaction, phase="committing")
                if git_operation(path) or working_tree_dirty(path):
                    raise RepositorySyncError(
                        f"repository {item.repository_id} is not clean after merge commit {after[:12]}"
                    )
            except Exception as exc:
                item.error_message = str(exc)
                transaction.error_message = str(exc)
                self._save(transaction, phase="committing")
                raise RepositorySyncError(
                    f"repository-sync commit failed for {item.repository_id}: {exc}"
                ) from exc
        transaction.error_message = ""
        self._save(transaction, phase="complete")
        return transaction

    def rollback(self, package_id: str) -> RepositorySyncTransaction:
        """Abort every uncommitted merge when history is still rewind-free."""

        try:
            return self._rollback(package_id)
        except RepositorySyncError:
            raise
        except (TaskGitError, OSError, ValueError) as exc:
            raise RepositorySyncError(
                f"repository synchronization rollback failed: {exc}"
            ) from exc

    def _rollback(self, package_id: str) -> RepositorySyncTransaction:
        transaction = self._require_transaction(package_id)
        if transaction.complete:
            raise RepositorySyncError("cannot roll back a completed repository sync")
        if transaction.forward_only or any(
            item.status == "committed" for item in transaction.repositories
        ):
            raise RepositorySyncError(
                "repository-sync transaction is forward-only after its first merge commit"
            )
        for item in transaction.repositories:
            path = self._path(item.repository_id)
            merge_head = self._merge_head(path)
            if merge_head:
                if merge_head != item.source_commit:
                    raise RepositorySyncError(
                        f"repository {item.repository_id} has an unrelated merge in progress"
                    )
                git(path, "merge", "--abort")
            if head_commit(path) != item.target_before:
                raise RepositorySyncError(
                    f"repository {item.repository_id} HEAD changed during rollback"
                )
            item.status = "rolled_back"
            item.conflict_paths = []
        self._save(transaction, phase="rolled_back")
        return transaction

    def remote_branches(
        self,
        repository_id: str,
        *,
        remote: str = "origin",
        refresh: bool = False,
    ) -> tuple[RemoteBranchOption, ...]:
        """List selectable remote branches without mutating the task branch.

        ``refresh=True`` uses ``ls-remote --heads`` under the shared ref lock,
        so branch discovery sees the server's current refs without updating the
        worktree or broad remote-tracking state.  The normal dashboard poll
        never calls this method.
        """

        repository = lookup_repository(self.manifest, repository_id)
        self._require_task_owned(repository)
        path = self._path(repository.id)
        remote_name = validate_remote_name(remote)
        rows: dict[str, tuple[str, bool]] = {}
        if refresh:
            with self._repository_ref_lock(path):
                raw = git(path, "ls-remote", "--heads", remote_name)
            for line in raw.splitlines():
                parts = line.split()
                if len(parts) != 2 or not parts[1].startswith("refs/heads/"):
                    continue
                branch = parts[1][len("refs/heads/") :]
                if branch:
                    rows[branch] = (parts[0], False)
        else:
            prefix = f"refs/remotes/{remote_name}/"
            raw = git(
                path,
                "for-each-ref",
                "--format=%(refname) %(objectname)",
                f"refs/remotes/{remote_name}",
                check=False,
            )
            for line in raw.splitlines():
                parts = line.split()
                if len(parts) != 2 or not parts[0].startswith(prefix):
                    continue
                branch = parts[0][len(prefix) :]
                if not branch or branch == "HEAD":
                    continue
                rows[branch] = (parts[1], True)
        base = repository.base_branch
        if base and base not in rows:
            commit = ""
            if not refresh:
                try:
                    commit = self._resolve_remote_tracking(path, remote_name, base)
                except RepositorySyncError:
                    commit = ""
            rows[base] = (commit, bool(commit))
        return tuple(
            RemoteBranchOption(
                repository_id=repository.id,
                remote=remote_name,
                branch=branch,
                commit=commit,
                configured_base=branch == base,
                locally_tracked=tracked,
            )
            for branch, (commit, tracked) in sorted(
                rows.items(), key=lambda item: (item[0] != base, item[0])
            )
        )

    def divergence_for_branch(
        self,
        repository_id: str,
        *,
        remote: str,
        source_branch: str,
        refresh: bool = True,
    ) -> RepositoryDivergence:
        """Measure one operator-selected source branch against the task branch."""

        spec = RepositorySyncSpec.from_mapping(
            {
                "remote": remote,
                "repositories": {
                    repository_id: {"source_branch": source_branch, "remote": remote}
                },
            }
        )
        return self.divergence(spec, refresh=refresh)[0]

    def divergence(
        self,
        spec: RepositorySyncSpec,
        *,
        refresh: bool = False,
    ) -> list[RepositoryDivergence]:
        """Return ahead/behind information without altering task branches."""

        rows: list[RepositoryDivergence] = []
        for target in spec.targets:
            try:
                repository = lookup_repository(self.manifest, target.repository_id)
                self._require_task_owned(repository)
                path = self._path(repository.id)
                source_branch = target.source_branch or repository.base_branch
                source = (
                    self._fetch_and_pin(path, target.remote, source_branch)
                    if refresh
                    else self._resolve_remote_tracking(path, target.remote, source_branch)
                )
                target_commit = head_commit(path)
                merge_base, ahead, behind = self._divergence(path, target_commit, source)
                rows.append(
                    RepositoryDivergence(
                        repository_id=repository.id,
                        remote=target.remote,
                        source_branch=source_branch,
                        source_commit=source,
                        target_branch=repository.task_branch,
                        target_commit=target_commit,
                        merge_base=merge_base,
                        ahead=ahead,
                        behind=behind,
                        dirty=working_tree_dirty(path),
                        git_operation=git_operation(path) or "",
                    )
                )
            except Exception as exc:
                repository_id = target.repository_id
                rows.append(
                    RepositoryDivergence(
                        repository_id=repository_id,
                        remote=target.remote,
                        source_branch=target.source_branch,
                        source_commit="",
                        target_branch="",
                        target_commit="",
                        merge_base="",
                        ahead=0,
                        behind=0,
                        error=str(exc),
                    )
                )
        return rows

    def transaction_report(self, package_id: str) -> dict[str, Any]:
        transaction = self.transactions.load(package_id)
        if transaction is None:
            return {"present": False, "package_id": package_id}
        return {
            "present": True,
            "path": str(self.transactions.path_for(package_id)),
            **transaction.as_mapping(),
        }

    def _create_transaction(
        self,
        *,
        package_id: str,
        package_fingerprint: str,
        spec: RepositorySyncSpec,
    ) -> RepositorySyncTransaction:
        repositories: list[RepositorySyncRepositoryState] = []
        # Fetch all sources and pin them before mutating any task worktree.
        for target in spec.targets:
            repository = lookup_repository(self.manifest, target.repository_id)
            self._require_task_owned(repository)
            path = self._path(repository.id)
            self._require_clean_target(repository, path)
            source_branch = target.source_branch or repository.base_branch
            source_commit = self._fetch_and_pin(path, target.remote, source_branch)
            target_before = head_commit(path)
            merge_base, ahead, behind = self._divergence(
                path, target_before, source_commit
            )
            repositories.append(
                RepositorySyncRepositoryState(
                    repository_id=repository.id,
                    remote=target.remote,
                    source_branch=source_branch,
                    source_commit=source_commit,
                    target_branch=repository.task_branch,
                    configured_base_branch=repository.base_branch,
                    source_selection=(
                        "operator_override"
                        if target.source_branch and target.source_branch != repository.base_branch
                        else "configured_base"
                    ),
                    target_before=target_before,
                    merge_base=merge_base,
                    ahead_before=ahead,
                    behind_before=behind,
                )
            )
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        transaction = RepositorySyncTransaction(
            schema_version=1,
            transaction_id=f"sync-{package_id}-{uuid.uuid4().hex[:12]}",
            package_id=package_id,
            package_fingerprint=package_fingerprint,
            created_at=now,
            updated_at=now,
            phase="fetched",
            repositories=repositories,
        )
        self.transactions.save(transaction)
        return transaction

    def _resume_prepare(self, transaction: RepositorySyncTransaction) -> None:
        if transaction.phase in {"verified", "committing", "complete"}:
            return
        for item in transaction.repositories:
            if item.status in {"noop", "merge_ready", "resolved", "conflicted", "committed"}:
                self._reconcile_existing_prepare_state(item)
                continue
            repository = lookup_repository(self.manifest, item.repository_id)
            path = self._path(item.repository_id)
            # A transaction may have been interrupted after all source SHAs were
            # pinned but before this repository entered merge state.  Re-check
            # cleanliness and ownership on every such resume so operator edits
            # cannot be silently folded into the synchronization merge.
            self._require_clean_target(repository, path)
            self._require_target_identity(item, path, allow_committed=False)
            if self._is_ancestor(path, item.source_commit, item.target_before):
                item.status = "noop"
                item.conflict_paths = []
                self._save(transaction, phase="merging")
                continue
            merge_head = self._merge_head(path)
            if merge_head:
                self._reconcile_existing_prepare_state(item)
                self._save(transaction, phase="merging")
                continue
            completed = git(
                path,
                "merge",
                "--no-commit",
                "--no-ff",
                item.source_commit,
                check=False,
            )
            # ``git`` returns stdout rather than the return code.  Re-read Git
            # state instead of trusting localized command text.
            del completed
            merge_head = self._merge_head(path)
            conflicts = self._unmerged_paths(path)
            if merge_head != item.source_commit:
                raise RepositorySyncError(
                    f"repository {item.repository_id} failed to establish merge state for pinned upstream {item.source_commit}"
                )
            item.conflict_paths = list(conflicts)
            item.status = "conflicted" if conflicts else "merge_ready"
            self._save(transaction, phase="merging")
        conflicts = {
            item.repository_id: tuple(item.conflict_paths)
            for item in transaction.repositories
            if item.conflict_paths
        }
        self._save(transaction, phase="resolving" if conflicts else "ready_verify")

    def _reconcile_existing_prepare_state(
        self, item: RepositorySyncRepositoryState
    ) -> None:
        path = self._path(item.repository_id)
        if item.status == "noop":
            if head_commit(path) != item.target_before:
                raise RepositorySyncError(
                    f"repository {item.repository_id} changed after no-op synchronization"
                )
            return
        if item.status == "committed":
            self._validate_committed_item(item, path)
            return
        self._require_target_identity(item, path, allow_committed=False)
        merge_head = self._merge_head(path)
        if merge_head != item.source_commit:
            raise RepositorySyncError(
                f"repository {item.repository_id} lost its expected merge state"
            )
        conflicts = self._unmerged_paths(path)
        item.conflict_paths = list(conflicts)
        item.status = "conflicted" if conflicts else (
            "resolved" if item.status == "resolved" else "merge_ready"
        )

    def _validate_transaction_contract(
        self,
        transaction: RepositorySyncTransaction,
        package_fingerprint: str,
        spec: RepositorySyncSpec,
    ) -> None:
        if transaction.package_fingerprint != package_fingerprint:
            raise RepositorySyncError(
                "repository-sync package definition changed after its transaction began; "
                "recover or replan before continuing"
            )
        expected = list(spec.repository_ids)
        actual = [item.repository_id for item in transaction.repositories]
        if actual != expected:
            raise RepositorySyncError(
                "repository-sync transaction repository order/scope differs from the accepted package"
            )

    def _validate_merge_candidates(self, transaction: RepositorySyncTransaction) -> None:
        for item in transaction.repositories:
            path = self._path(item.repository_id)
            if item.status == "noop":
                if head_commit(path) != item.target_before:
                    raise RepositorySyncError(
                        f"repository {item.repository_id} changed after synchronization no-op"
                    )
                continue
            if item.status == "committed":
                self._validate_committed_item(item, path)
                continue
            self._require_target_identity(item, path, allow_committed=False)
            if self._merge_head(path) != item.source_commit:
                raise RepositorySyncError(
                    f"repository {item.repository_id} does not have the pinned merge in progress"
                )
            conflicts = self._unmerged_paths(path)
            if conflicts:
                raise RepositorySyncError(
                    f"repository {item.repository_id} still has unresolved merge paths: "
                    + ", ".join(conflicts)
                )

    def _validate_completed(self, transaction: RepositorySyncTransaction) -> None:
        for item in transaction.repositories:
            path = self._path(item.repository_id)
            if item.status == "noop":
                # Later packages may legitimately advance HEAD after this sync;
                # only require the pinned upstream to remain in ancestry.
                if not self._is_ancestor(path, item.source_commit, head_commit(path)):
                    raise RepositorySyncError(
                        f"repository {item.repository_id} no longer contains synchronized upstream"
                    )
            elif item.status == "committed":
                if not item.target_after:
                    raise RepositorySyncError(
                        f"repository {item.repository_id} completed sync is missing target_after"
                    )
                if not self._is_ancestor(path, item.target_after, head_commit(path)):
                    raise RepositorySyncError(
                        f"repository {item.repository_id} lost its synchronization merge commit"
                    )

    def _reconcile_committed_after_crash(
        self, transaction: RepositorySyncTransaction
    ) -> None:
        changed = False
        for item in transaction.repositories:
            if item.status in {"noop", "committed"}:
                continue
            path = self._path(item.repository_id)
            if self._merge_head(path):
                continue
            current = head_commit(path)
            if current != item.target_before and self._is_expected_merge_commit(
                path, item, current
            ):
                item.status = "committed"
                item.target_after = current
                transaction.forward_only = True
                changed = True
        if changed:
            self._save(transaction, phase="committing")

    def _is_expected_merge_commit(
        self,
        path: Path,
        item: RepositorySyncRepositoryState,
        commit: str,
    ) -> bool:
        parents = git(path, "show", "-s", "--format=%P", commit).split()
        if item.target_before not in parents or item.source_commit not in parents:
            return False
        if item.verified_tree:
            tree = git(path, "rev-parse", f"{commit}^{{tree}}")
            if tree != item.verified_tree:
                return False
        return self._is_ancestor(path, item.source_commit, commit)

    def _validate_committed_item(
        self, item: RepositorySyncRepositoryState, path: Path
    ) -> None:
        if current_branch(path) != item.target_branch:
            raise RepositorySyncError(
                f"repository {item.repository_id} is no longer on task branch {item.target_branch!r}"
            )
        current = head_commit(path)
        expected = item.target_after or current
        if item.target_after and not self._is_ancestor(path, item.target_after, current):
            raise RepositorySyncError(
                f"repository {item.repository_id} no longer contains recorded sync commit {item.target_after}"
            )
        if not self._is_ancestor(path, item.source_commit, expected):
            raise RepositorySyncError(
                f"repository {item.repository_id} recorded merge does not contain pinned source"
            )

    def _require_target_identity(
        self,
        item: RepositorySyncRepositoryState,
        path: Path,
        *,
        allow_committed: bool,
    ) -> None:
        if current_branch(path) != item.target_branch:
            raise RepositorySyncError(
                f"repository {item.repository_id} branch changed: expected {item.target_branch!r}"
            )
        current = head_commit(path)
        if allow_committed and item.target_after and current == item.target_after:
            return
        if current != item.target_before:
            raise RepositorySyncError(
                f"repository {item.repository_id} HEAD changed during sync: "
                f"expected {item.target_before}, found {current}"
            )

    def _require_clean_target(self, repository: RepositorySpec, path: Path) -> None:
        self._require_task_owned(repository)
        if current_branch(path) != repository.task_branch:
            raise RepositorySyncError(
                f"repository {repository.id} is on {current_branch(path)!r}, "
                f"expected task branch {repository.task_branch!r}"
            )
        operation = git_operation(path)
        if operation:
            raise RepositorySyncError(
                f"repository {repository.id} has an active {operation} operation"
            )
        if working_tree_dirty(path):
            raise RepositorySyncError(
                f"repository {repository.id} has uncommitted changes before synchronization"
            )

    @staticmethod
    def _require_task_owned(repository: RepositorySpec) -> None:
        if repository.mutability != "task_owned":
            raise RepositorySyncError(
                f"repository {repository.id} is {repository.mutability}; repository sync may mutate only task_owned repositories"
            )

    def _path(self, repository_id: str) -> Path:
        path = self.repository_paths.get(repository_id)
        if path is None or not path.is_dir():
            raise RepositorySyncError(
                f"repository-sync workspace path is unavailable for {repository_id!r}"
            )
        return path

    def _fetch_and_pin(self, path: Path, remote: str, branch: str) -> str:
        with self._repository_ref_lock(path):
            # Fetch exactly the declared branch.  Do not prune or update other
            # refs as a side effect of a synchronization Work Package.
            git(path, "fetch", "--no-tags", remote, branch)
            source = git(path, "rev-parse", "--verify", "FETCH_HEAD^{commit}")
            if not source:
                raise RepositorySyncError(
                    f"cannot resolve fetched {remote}/{branch} to an immutable commit"
                )
            return source

    @staticmethod
    def _resolve_remote_tracking(path: Path, remote: str, branch: str) -> str:
        ref = f"refs/remotes/{remote}/{branch}"
        source = git(path, "rev-parse", "--verify", f"{ref}^{{commit}}", check=False)
        if not source:
            raise RepositorySyncError(
                f"remote-tracking ref {remote}/{branch} is unavailable; refresh divergence or run the synchronization Work Package"
            )
        return source

    def _repository_ref_lock(self, path: Path) -> FileLock:
        common_raw = git(path, "rev-parse", "--git-common-dir")
        common = Path(common_raw)
        if not common.is_absolute():
            common = (path / common).resolve()
        digest = hashlib.sha256(str(common).encode("utf-8")).hexdigest()
        return FileLock(
            self.lock_root / f"{digest}.lock",
            level=LockLevel.REPOSITORY_REF,
        )

    @staticmethod
    def _divergence(path: Path, target: str, source: str) -> tuple[str, int, int]:
        merge_base = git(path, "merge-base", target, source)
        counts = git(path, "rev-list", "--left-right", "--count", f"{target}...{source}")
        parts = counts.split()
        if len(parts) != 2:
            raise RepositorySyncError(
                f"cannot calculate divergence for {target[:12]}...{source[:12]}"
            )
        return merge_base, int(parts[0]), int(parts[1])

    @staticmethod
    def _is_ancestor(path: Path, ancestor: str, descendant: str) -> bool:
        return RepositorySyncService._command_succeeded(
            path, "merge-base", "--is-ancestor", ancestor, descendant
        )

    @staticmethod
    def _command_succeeded(path: Path, *args: str) -> bool:
        return GitClient().run(path, args, check=False).ok

    @staticmethod
    def _merge_head(path: Path) -> str:
        return git(path, "rev-parse", "--verify", "MERGE_HEAD^{commit}", check=False)

    @staticmethod
    def _unmerged_paths(path: Path) -> tuple[str, ...]:
        raw = git(path, "diff", "--name-only", "--diff-filter=U", "-z", check=False)
        return tuple(item for item in raw.split("\x00") if item)

    @staticmethod
    def _conflict_marker_paths(path: Path, relative_paths: tuple[str, ...]) -> tuple[str, ...]:
        markers = (b"<<<<<<<", b"|||||||", b"=======", b">>>>>>>")
        found: list[str] = []
        for relative in relative_paths:
            candidate = path / relative
            if not candidate.is_file() or candidate.is_symlink():
                continue
            try:
                data = candidate.read_bytes()
            except OSError:
                continue
            if any(
                line.startswith(marker)
                for line in data.splitlines()
                for marker in markers
            ):
                found.append(relative)
        return tuple(found)

    @staticmethod
    def _has_unstaged_or_untracked_changes(path: Path) -> bool:
        unstaged = GitClient().run(path, ("diff", "--quiet"), check=False)
        if not unstaged.ok:
            return True
        untracked = git(path, "ls-files", "--others", "--exclude-standard", "-z", check=False)
        return bool(untracked)

    def _save(self, transaction: RepositorySyncTransaction, *, phase: str) -> None:
        transaction.phase = phase
        transaction.updated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        self.transactions.save(transaction)

    def _require_transaction(self, package_id: str) -> RepositorySyncTransaction:
        transaction = self.transactions.load(package_id)
        if transaction is None:
            raise RepositorySyncError(
                f"repository-sync transaction does not exist for package {package_id}"
            )
        return transaction

    def _prepare_result(
        self, transaction: RepositorySyncTransaction
    ) -> RepositorySyncPrepareResult:
        return RepositorySyncPrepareResult(
            transaction=transaction,
            transaction_path=self.transactions.path_for(transaction.package_id),
            conflict_paths={
                item.repository_id: tuple(item.conflict_paths)
                for item in transaction.repositories
                if item.conflict_paths
            },
        )


__all__ = [
    "RemoteBranchOption",
    "RepositoryDivergence",
    "RepositorySyncError",
    "RepositorySyncPrepareResult",
    "RepositorySyncService",
]
