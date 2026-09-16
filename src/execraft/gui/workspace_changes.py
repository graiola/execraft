"""Safe Git inspection and operator-authorized commits for the local dashboard.

The dashboard must never become an arbitrary shell or silently weaken the
orchestrator commit boundary.  This module therefore exposes a deliberately
small Git surface over repositories already registered in the active task
workspace:

* list dirty paths using porcelain-v2 output;
* render bounded textual diffs for one declared path;
* generate a commit message with a configured read-only review agent;
* remove only explicitly selected, digest-checked untracked files or symlinks;
* commit only explicitly selected paths after a stale-state check and operator
  acknowledgement.

No operation pushes, rebases, resets, checks out branches, or accepts arbitrary
commands from the browser.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Sequence

from execraft.agents import AgentProviderConfig
from execraft.runtime.native import build_native_runtime
from execraft.orchestrate.scheduler import AgentCapability, AgentExecutionError, StructuredHandoff
from execraft.orchestrate.structured_output import (
    extract_structured_object,
    validate_structured_object,
)
from execraft.orchestrate.transactions import CommitJournal, snapshot_repository
from execraft.workspace.task_git import TaskGitError, current_branch, head_commit
from execraft.workspace.workspace_git import WorkspaceRecord
from execraft.skills import SkillCatalog


class WorkspaceChangeError(RuntimeError):
    """Raised when a dashboard Git operation is unsafe or stale."""


@dataclass(frozen=True)
class WorkspaceRepository:
    repository_id: str
    path: Path
    role: str
    mutability: str
    expected_branch: str = ""


@dataclass(frozen=True)
class ChangeEntry:
    path: str
    index_status: str
    worktree_status: str
    kind: str
    original_path: str = ""

    @property
    def staged(self) -> bool:
        return self.index_status not in {".", " ", "?"}

    @property
    def unstaged(self) -> bool:
        return self.worktree_status not in {".", " "} or self.index_status == "?"

    @property
    def conflicted(self) -> bool:
        pair = self.index_status + self.worktree_status
        return "U" in pair or pair in {"AA", "DD"}

    @property
    def deletable(self) -> bool:
        """Only untracked paths may be physically removed from the GUI."""

        return self.index_status == "?" and not self.conflicted

    def as_mapping(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "original_path": self.original_path,
            "index_status": self.index_status,
            "worktree_status": self.worktree_status,
            "kind": self.kind,
            "staged": self.staged,
            "unstaged": self.unstaged,
            "conflicted": self.conflicted,
            "deletable": self.deletable,
        }


Runner = Callable[..., subprocess.CompletedProcess[str]]
AgentBuilder = Callable[..., Any]


_COMMIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["subject", "body"],
    "properties": {
        "subject": {"type": "string", "minLength": 1},
        "body": {"type": "string"},
    },
}


class WorkspaceChangeManager:
    """Bounded Git operations for the repositories in one task workspace."""

    def __init__(
        self,
        *,
        workspace: WorkspaceRecord,
        expected_branches: Mapping[str, str],
        state_dir: Path,
        runner: Runner = subprocess.run,
        agent_builder: AgentBuilder = build_native_runtime,
        skill_catalog: SkillCatalog | None = None,
    ) -> None:
        self.workspace = workspace
        self.state_dir = state_dir.resolve()
        self._runner = runner
        self._agent_builder = agent_builder
        self._skill_catalog = skill_catalog or SkillCatalog.load()
        self._repositories: dict[str, WorkspaceRepository] = {}
        for item in workspace.repositories:
            repository_id = str(item.get("id", "")).strip()
            if not repository_id:
                continue
            self._repositories[repository_id] = WorkspaceRepository(
                repository_id=repository_id,
                path=Path(str(item.get("worktree_path", ""))).expanduser().resolve(),
                role=str(item.get("role", "component")),
                mutability=str(item.get("mutability", "task_owned")),
                expected_branch=str(expected_branches.get(repository_id, "")),
            )
        self._journal = CommitJournal(self.state_dir / "commit-journal.json")
        self._audit_path = self.state_dir / "gui-commit-audit.jsonl"

    def snapshot(self, *, driver_active: bool) -> dict[str, Any]:
        repositories: list[dict[str, Any]] = []
        for repository in self._repositories.values():
            row = self._repository_snapshot(repository)
            repositories.append(row)
        dirty = [row for row in repositories if row["dirty"]]
        commit_allowed = any(row["committable"] for row in dirty) and not driver_active
        delete_allowed = (
            any(row["committable"] and row["deletable_count"] for row in dirty)
            and not driver_active
        )
        reason = ""
        if driver_active:
            reason = "Workspace mutations are disabled while an orchestrator driver is active."
        elif not dirty:
            reason = "No uncommitted task-workspace changes were found."
        elif not any(row["committable"] for row in dirty):
            reason = "Dirty repositories are read-only, conflicted, or on an unexpected branch."
        return {
            "available": True,
            "task_id": self.workspace.task_id,
            "workspace_root": self.workspace.workspace_root,
            "repositories": repositories,
            "dirty_repository_count": len(dirty),
            "changed_file_count": sum(int(row["change_count"]) for row in repositories),
            "driver_active": driver_active,
            "commit_allowed": commit_allowed,
            "delete_allowed": delete_allowed,
            "reason": reason,
            "audit_path": str(self._audit_path),
        }

    def file_diff(
        self,
        repository_id: str,
        path: str,
        *,
        max_bytes: int = 600_000,
    ) -> dict[str, Any]:
        repository = self._repository(repository_id)
        normalized = _normalize_relative_path(path)
        changes, _ = self._changes(repository)
        entry = next((item for item in changes if item.path == normalized), None)
        if entry is None:
            raise WorkspaceChangeError(
                f"path is no longer modified in {repository_id}: {normalized}"
            )

        sections: list[str] = []
        if entry.staged:
            staged = self._git(
                repository,
                "diff",
                "--cached",
                "--no-ext-diff",
                "--no-color",
                "--",
                normalized,
                check=True,
            ).stdout
            if staged:
                sections.append("### Staged\n" + staged)
        if entry.unstaged and entry.index_status != "?":
            unstaged = self._git(
                repository,
                "diff",
                "--no-ext-diff",
                "--no-color",
                "--",
                normalized,
                check=True,
            ).stdout
            if unstaged:
                sections.append("### Working tree\n" + unstaged)
        if entry.index_status == "?":
            sections.append("### Untracked file\n" + self._untracked_preview(repository, normalized))

        content = "\n\n".join(sections) or "No textual diff is available."
        encoded = content.encode("utf-8", errors="replace")
        truncated = len(encoded) > max_bytes
        if truncated:
            content = encoded[:max_bytes].decode("utf-8", errors="replace") + "\n… diff truncated …\n"
        return {
            "repository_id": repository_id,
            "path": normalized,
            "original_path": entry.original_path,
            "content": content,
            "truncated": truncated,
            "size_bytes": len(encoded),
            "binary": "Binary file" in content or "binary files" in content.lower(),
            "status": entry.as_mapping(),
        }

    def generate_commit_message(
        self,
        *,
        selections: Mapping[str, Sequence[str]],
        expected_digests: Mapping[str, str],
        provider: AgentProviderConfig,
        max_diff_bytes: int = 120_000,
    ) -> dict[str, Any]:
        selected = self._preflight_selection(
            selections,
            expected_digests=expected_digests,
            require_review_ack=False,
            reviewed=True,
        )
        if AgentCapability.REVIEW not in provider.capabilities:
            raise WorkspaceChangeError(
                f"agent {provider.provider_id!r} is not configured for review"
            )

        excerpts: dict[str, str] = {}
        summaries: list[str] = []
        budget = max(4_000, int(max_diff_bytes))
        for repository, entries, _digest in selected:
            summaries.append(
                f"{repository.repository_id}: {len(entries)} selected path(s) on "
                f"{current_branch(repository.path)}"
            )
            for entry in entries:
                if budget <= 0:
                    break
                diff = self.file_diff(repository.repository_id, entry.path, max_bytes=budget)
                text = str(diff["content"])
                encoded = text.encode("utf-8", errors="replace")
                if len(encoded) > budget:
                    text = encoded[:budget].decode("utf-8", errors="replace")
                budget -= len(text.encode("utf-8", errors="replace"))
                excerpts[f"{repository.repository_id}:{entry.path}"] = text

        workdir = Path(self.workspace.workspace_root).resolve()
        adapter = self._agent_builder(provider, workdir=workdir, read_only=True)
        # Commit-message generation is intentionally short-lived even when the
        # provider has a much larger implementation timeout.
        if hasattr(adapter, "_timeout_seconds"):
            setattr(adapter, "_timeout_seconds", min(int(provider.timeout_seconds), 240))
        if hasattr(adapter, "_inactivity_timeout_seconds"):
            setattr(
                adapter,
                "_inactivity_timeout_seconds",
                min(int(provider.inactivity_timeout_seconds), 120),
            )

        handoff = StructuredHandoff(
            work_package_id=f"gui-ai-commit-{self.workspace.task_id}",
            stage="review",
            summary=(
                "Create a concise Git commit message for the selected, already-reviewed "
                "changes. Do not review the repository again, do not modify files, do not "
                "run tools, and do not invent behavior absent from the supplied diffs. "
                "Use an imperative subject, preferably at most 72 characters. Put useful "
                "implementation detail in the body; an empty body is allowed."
            ),
            repository_diff_summary="; ".join(summaries),
            bounded_excerpts=excerpts,
            expected_output_schema=_COMMIT_SCHEMA,
            working_directory=str(workdir),
            read_only=True,
            workflow_skills=[
                item.as_mapping()
                for item in self._skill_catalog.materialize("commit", ["ai-commit"])
            ],
        )
        try:
            result = adapter.execute(handoff)
        except AgentExecutionError as exc:
            self._audit(
                "ai_commit_message_failed",
                {
                    "provider_id": provider.provider_id,
                    "classification": exc.classification,
                    "error": str(exc),
                    "selection": _selection_mapping(selected),
                },
            )
            raise WorkspaceChangeError(
                f"AI commit-message generation failed ({exc.classification}): {exc}"
            ) from exc

        final_message = str(
            result.get("final_message")
            or result.get("output")
            or result.get("text")
            or ""
        )
        payload = extract_structured_object(
            final_message,
            preferred_keys=("subject", "body"),
        )
        errors = validate_structured_object(payload, _COMMIT_SCHEMA)
        if errors:
            self._audit(
                "ai_commit_message_invalid",
                {
                    "provider_id": provider.provider_id,
                    "errors": errors,
                    "selection": _selection_mapping(selected),
                    "output_preview": final_message[-8_000:],
                },
            )
            raise WorkspaceChangeError(
                "AI agent returned an invalid commit-message contract: " + "; ".join(errors)
            )
        assert payload is not None
        subject = _normalize_subject(str(payload["subject"]))
        body = _normalize_body(str(payload.get("body", "")))
        self._audit(
            "ai_commit_message_generated",
            {
                "provider_id": provider.provider_id,
                "selection": _selection_mapping(selected),
                "subject": subject,
                "body": body,
            },
        )
        return {
            "subject": subject,
            "body": body,
            "provider_id": provider.provider_id,
            "model": provider.model,
        }

    def delete_untracked(
        self,
        *,
        selections: Mapping[str, Sequence[str]],
        expected_digests: Mapping[str, str],
    ) -> dict[str, Any]:
        """Physically remove explicitly selected untracked files or symlinks.

        This is intentionally not a generic discard/restore operation. Tracked
        modifications and staged deletions remain under Git review and commit
        controls, while generated artifacts such as ``*.pyc`` can be removed
        without being accidentally committed.
        """

        selected = self._preflight_selection(
            selections,
            expected_digests=expected_digests,
            require_review_ack=False,
            reviewed=True,
        )
        invalid = [
            f"{repository.repository_id}:{entry.path}"
            for repository, entries, _digest in selected
            for entry in entries
            if not entry.deletable
        ]
        if invalid:
            raise WorkspaceChangeError(
                "only untracked files may be deleted from the workspace: "
                + ", ".join(invalid)
            )

        # Validate every repository before deleting the first path so stale
        # browser state cannot cause a partial multi-repository cleanup.
        for repository, _entries, expected_digest in selected:
            _changes, current_digest = self._changes(repository)
            if current_digest != expected_digest:
                raise WorkspaceChangeError(
                    f"workspace changed after preflight for {repository.repository_id}"
                )

        deleted: list[dict[str, str]] = []
        try:
            for repository, entries, _digest in selected:
                for entry in entries:
                    target = repository.path / entry.path
                    try:
                        target.relative_to(repository.path)
                    except ValueError as exc:  # pragma: no cover - normalized path guard
                        raise WorkspaceChangeError(
                            f"selected path escapes repository {repository.repository_id}"
                        ) from exc
                    try:
                        target.lstat()
                    except FileNotFoundError as exc:
                        raise WorkspaceChangeError(
                            f"selected untracked path no longer exists: "
                            f"{repository.repository_id}:{entry.path}"
                        ) from exc
                    if target.is_dir() and not target.is_symlink():
                        raise WorkspaceChangeError(
                            f"workspace deletion supports files and symlinks only: "
                            f"{repository.repository_id}:{entry.path}"
                        )
                    target.unlink()
                    _remove_empty_parents(repository.path, target.parent)
                    deleted.append(
                        {
                            "repository_id": repository.repository_id,
                            "path": entry.path,
                        }
                    )
        except Exception as exc:
            self._audit(
                "workspace_delete_failed",
                {
                    "selection": _selection_mapping(selected),
                    "deleted_before_failure": deleted,
                    "error": str(exc),
                },
            )
            raise

        self._audit(
            "workspace_untracked_deleted",
            {
                "selection": _selection_mapping(selected),
                "deleted": deleted,
            },
        )
        return {
            "deleted": deleted,
            "deleted_count": len(deleted),
            "audit_path": str(self._audit_path),
        }

    def commit(
        self,
        *,
        selections: Mapping[str, Sequence[str]],
        expected_digests: Mapping[str, str],
        subject: str,
        body: str = "",
        reviewed: bool,
    ) -> dict[str, Any]:
        subject = _normalize_subject(subject)
        body = _normalize_body(body)
        selected = self._preflight_selection(
            selections,
            expected_digests=expected_digests,
            require_review_ack=True,
            reviewed=reviewed,
        )
        timestamp = _utc_now()
        transaction_id = hashlib.sha256(
            f"gui:{self.workspace.task_id}:{timestamp}:{subject}".encode("utf-8")
        ).hexdigest()[:20]
        work_package_id = f"gui-manual-{self.workspace.task_id}-{timestamp.replace(':', '-') }"
        transaction = self._journal.begin(transaction_id, timestamp, work_package_id)
        pre = [snapshot_repository(repo.repository_id, repo.path) for repo, _, _ in selected]
        self._journal.add_snapshots(transaction.transaction_id, pre_snapshots=pre)

        committed: list[dict[str, str]] = []
        try:
            # Preflight every repository before mutating any index. This catches
            # stale selections, unexpected branches and staged paths outside the
            # operator's selection before the first commit is created.
            for repository, entries, digest in selected:
                self._validate_commit_repository(repository, entries, digest)

            for repository, entries, digest in selected:
                # Revalidate immediately before staging as well as during the
                # all-repository preflight. This narrows the race with editors or
                # terminals that may change a selected worktree concurrently.
                self._validate_commit_repository(repository, entries, digest)
                ordinary_paths = [
                    entry.path for entry in entries if not entry.original_path
                ]
                if ordinary_paths:
                    self._git(
                        repository,
                        "add",
                        "-A",
                        "--",
                        *dict.fromkeys(ordinary_paths),
                    )
                for entry in entries:
                    if not entry.original_path:
                        continue
                    # A rename has two sides. Stage the destination normally and
                    # the removed source through the tracked-files-only update;
                    # passing a missing source to `git add -A` is an invalid
                    # pathspec on some Git versions.
                    self._git(repository, "add", "-A", "--", entry.path)
                    self._git(
                        repository,
                        "update-index",
                        "--remove",
                        "--",
                        entry.original_path,
                    )
                staged = self._git(
                    repository,
                    "diff",
                    "--cached",
                    "--quiet",
                    check=False,
                )
                if staged.returncode == 0:
                    raise WorkspaceChangeError(
                        f"selected paths produced no staged changes in {repository.repository_id}"
                    )
                if staged.returncode != 1:
                    raise WorkspaceChangeError(
                        f"unable to inspect staged changes in {repository.repository_id}"
                    )
                args = ["commit", "-m", subject]
                if body:
                    args.extend(["-m", body])
                completed = self._git(repository, *args)
                sha = head_commit(repository.path)
                committed.append(
                    {
                        "repository_id": repository.repository_id,
                        "commit": sha,
                        "short_commit": sha[:12],
                        "detail": (completed.stdout or completed.stderr).strip(),
                    }
                )

            post = [snapshot_repository(repo.repository_id, repo.path) for repo, _, _ in selected]
            self._journal.add_snapshots(transaction.transaction_id, post_snapshots=post)
            self._journal.commit(transaction.transaction_id)
            self._audit(
                "gui_commit_completed",
                {
                    "transaction_id": transaction.transaction_id,
                    "subject": subject,
                    "body": body,
                    "selection": _selection_mapping(selected),
                    "commits": committed,
                },
            )
        except Exception as exc:
            self._journal.fail(transaction.transaction_id, str(exc))
            self._audit(
                "gui_commit_failed",
                {
                    "transaction_id": transaction.transaction_id,
                    "subject": subject,
                    "selection": _selection_mapping(selected),
                    "commits_created_before_failure": committed,
                    "error": str(exc),
                },
            )
            if committed:
                detail = ", ".join(
                    f"{item['repository_id']}@{item['short_commit']}" for item in committed
                )
                raise WorkspaceChangeError(
                    f"multi-repository commit partially completed ({detail}); "
                    f"inspect the commit journal before retrying: {exc}"
                ) from exc
            raise

        return {
            "transaction_id": transaction.transaction_id,
            "subject": subject,
            "body": body,
            "commits": committed,
            "audit_path": str(self._audit_path),
        }

    def _preflight_selection(
        self,
        selections: Mapping[str, Sequence[str]],
        *,
        expected_digests: Mapping[str, str],
        require_review_ack: bool,
        reviewed: bool,
    ) -> list[tuple[WorkspaceRepository, list[ChangeEntry], str]]:
        if require_review_ack and not reviewed:
            raise WorkspaceChangeError(
                "confirm that the selected diffs and verification evidence were reviewed"
            )
        if not isinstance(selections, Mapping) or not selections:
            raise WorkspaceChangeError("select at least one modified path")
        selected: list[tuple[WorkspaceRepository, list[ChangeEntry], str]] = []
        for repository_id, raw_paths in selections.items():
            repository = self._repository(str(repository_id))
            if repository.mutability != "task_owned":
                raise WorkspaceChangeError(
                    f"repository {repository.repository_id} is not task-owned"
                )
            branch = current_branch(repository.path)
            if repository.expected_branch and branch != repository.expected_branch:
                raise WorkspaceChangeError(
                    f"repository {repository.repository_id} is on {branch!r}; "
                    f"expected task branch {repository.expected_branch!r}"
                )
            if not isinstance(raw_paths, Sequence) or isinstance(raw_paths, (str, bytes)):
                raise WorkspaceChangeError(
                    f"selected paths for {repository.repository_id} must be a list"
                )
            normalized_paths = list(
                dict.fromkeys(_normalize_relative_path(str(path)) for path in raw_paths)
            )
            if not normalized_paths:
                continue
            changes, digest = self._changes(repository)
            expected = str(expected_digests.get(repository.repository_id, ""))
            if not expected or expected != digest:
                raise WorkspaceChangeError(
                    f"workspace changed after inspection for {repository.repository_id}; refresh and review again"
                )
            by_path = {item.path: item for item in changes}
            missing = [path for path in normalized_paths if path not in by_path]
            if missing:
                raise WorkspaceChangeError(
                    f"selected paths are no longer modified in {repository.repository_id}: "
                    + ", ".join(missing)
                )
            entries = [by_path[path] for path in normalized_paths]
            if any(item.conflicted for item in entries):
                raise WorkspaceChangeError(
                    f"resolve merge conflicts before committing {repository.repository_id}"
                )
            selected.append((repository, entries, digest))
        if not selected:
            raise WorkspaceChangeError("select at least one modified path")
        return selected

    def _validate_commit_repository(
        self,
        repository: WorkspaceRepository,
        entries: Sequence[ChangeEntry],
        expected_digest: str,
    ) -> None:
        changes, digest = self._changes(repository)
        if digest != expected_digest:
            raise WorkspaceChangeError(
                f"workspace changed after preflight for {repository.repository_id}"
            )
        if repository.expected_branch and current_branch(repository.path) != repository.expected_branch:
            raise WorkspaceChangeError(
                f"repository {repository.repository_id} is on {current_branch(repository.path)!r}; "
                f"expected task branch {repository.expected_branch!r}"
            )
        selected_paths = {entry.path for entry in entries}
        staged_outside = sorted(
            item.path for item in changes if item.staged and item.path not in selected_paths
        )
        if staged_outside:
            raise WorkspaceChangeError(
                f"repository {repository.repository_id} has staged paths outside the selection: "
                + ", ".join(staged_outside)
            )

    def _repository_snapshot(self, repository: WorkspaceRepository) -> dict[str, Any]:
        path = repository.path
        if not path.is_dir():
            return {
                "id": repository.repository_id,
                "path": str(path),
                "role": repository.role,
                "mutability": repository.mutability,
                "available": False,
                "dirty": False,
                "committable": False,
                "reason": "worktree path is missing",
                "changes": [],
                "change_count": 0,
                "deletable_count": 0,
                "status_digest": "",
                "branch": "",
                "expected_branch": repository.expected_branch,
                "head": "",
            }
        try:
            branch = current_branch(path)
            head = head_commit(path)
            changes, digest = self._changes(repository)
        except (TaskGitError, WorkspaceChangeError) as exc:
            return {
                "id": repository.repository_id,
                "path": str(path),
                "role": repository.role,
                "mutability": repository.mutability,
                "available": False,
                "dirty": False,
                "committable": False,
                "reason": str(exc),
                "changes": [],
                "change_count": 0,
                "deletable_count": 0,
                "status_digest": "",
                "branch": "",
                "expected_branch": repository.expected_branch,
                "head": "",
            }
        reasons: list[str] = []
        if repository.mutability != "task_owned":
            reasons.append(f"mutability is {repository.mutability}")
        if repository.expected_branch and branch != repository.expected_branch:
            reasons.append(f"expected branch {repository.expected_branch}")
        if any(item.conflicted for item in changes):
            reasons.append("merge conflicts present")
        return {
            "id": repository.repository_id,
            "path": str(path),
            "role": repository.role,
            "mutability": repository.mutability,
            "available": True,
            "dirty": bool(changes),
            "committable": bool(changes) and not reasons,
            "reason": "; ".join(reasons),
            "changes": [item.as_mapping() for item in changes],
            "change_count": len(changes),
            "deletable_count": sum(1 for item in changes if item.deletable),
            "status_digest": digest,
            "branch": branch,
            "expected_branch": repository.expected_branch,
            "head": head,
            "short_head": head[:12],
        }

    def _changes(
        self, repository: WorkspaceRepository
    ) -> tuple[list[ChangeEntry], str]:
        completed = self._git(
            repository,
            "status",
            "--porcelain=v2",
            "-z",
            "--untracked-files=all",
        )
        raw = completed.stdout
        changes = _parse_porcelain_v2(raw)
        digest = hashlib.sha256(
            (head_commit(repository.path) + "\0" + raw).encode("utf-8", errors="surrogateescape")
        ).hexdigest()
        return changes, digest

    def _untracked_preview(self, repository: WorkspaceRepository, path: str) -> str:
        target = (repository.path / path).resolve()
        try:
            target.relative_to(repository.path)
        except ValueError as exc:
            raise WorkspaceChangeError("untracked path escapes the repository") from exc
        if not target.is_file():
            return "Untracked path is not a regular file."
        data = target.read_bytes()[:600_000]
        if b"\0" in data:
            return f"Binary untracked file ({target.stat().st_size} bytes)."
        text = data.decode("utf-8", errors="replace")
        if target.stat().st_size > len(data):
            text += "\n… file preview truncated …\n"
        return text

    def _repository(self, repository_id: str) -> WorkspaceRepository:
        repository = self._repositories.get(repository_id)
        if repository is None:
            raise WorkspaceChangeError(
                f"repository is not registered in task workspace: {repository_id}"
            )
        return repository

    def _git(
        self,
        repository: WorkspaceRepository,
        *args: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        completed = self._runner(
            ["git", *args],
            cwd=str(repository.path),
            text=True,
            capture_output=True,
            check=False,
        )
        if check and completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise WorkspaceChangeError(
                f"git {' '.join(args)} failed for {repository.repository_id}: {detail}"
            )
        return completed

    def _audit(self, event: str, payload: Mapping[str, Any]) -> None:
        record = {
            "timestamp": _utc_now(),
            "event": event,
            "task_id": self.workspace.task_id,
            **dict(payload),
        }
        self._audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self._audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _remove_empty_parents(repository: Path, parent: Path) -> None:
    """Remove empty untracked directory shells without crossing repository root."""

    while parent != repository:
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def _parse_porcelain_v2(raw: str) -> list[ChangeEntry]:
    """Parse ``git status --porcelain=v2 -z`` without losing spaces in paths."""

    records = raw.split("\0")
    changes: list[ChangeEntry] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record or record.startswith("# ") or record.startswith("! "):
            continue
        prefix = record[:2]
        if prefix == "? ":
            path = _normalize_relative_path(record[2:])
            changes.append(ChangeEntry(path, "?", ".", "untracked"))
            continue
        if prefix == "1 ":
            fields = record.split(" ", 8)
            if len(fields) != 9:
                raise WorkspaceChangeError("unable to parse ordinary Git status record")
            xy = fields[1]
            path = _normalize_relative_path(fields[8])
            changes.append(
                ChangeEntry(
                    path=path,
                    index_status=xy[0],
                    worktree_status=xy[1],
                    kind=_change_kind(xy, ordinary=True),
                )
            )
            continue
        if prefix == "2 ":
            fields = record.split(" ", 9)
            if len(fields) != 10 or index >= len(records):
                raise WorkspaceChangeError("unable to parse renamed/copied Git status record")
            xy = fields[1]
            path = _normalize_relative_path(fields[9])
            original = _normalize_relative_path(records[index])
            index += 1
            changes.append(
                ChangeEntry(
                    path=path,
                    original_path=original,
                    index_status=xy[0],
                    worktree_status=xy[1],
                    kind="renamed" if fields[8].startswith("R") else "copied",
                )
            )
            continue
        if prefix == "u ":
            fields = record.split(" ", 10)
            if len(fields) != 11:
                raise WorkspaceChangeError("unable to parse unmerged Git status record")
            xy = fields[1]
            path = _normalize_relative_path(fields[10])
            changes.append(
                ChangeEntry(
                    path=path,
                    index_status=xy[0],
                    worktree_status=xy[1],
                    kind="conflict",
                )
            )
            continue
        raise WorkspaceChangeError(f"unsupported Git porcelain record: {record[:80]!r}")
    return sorted(changes, key=lambda item: item.path)


def _change_kind(xy: str, *, ordinary: bool = False) -> str:
    if "U" in xy or xy in {"AA", "DD"}:
        return "conflict"
    if "D" in xy:
        return "deleted"
    if "A" in xy:
        return "added"
    if "T" in xy:
        return "typechanged"
    if "M" in xy:
        return "modified"
    return "changed" if ordinary else "unknown"


def _normalize_relative_path(value: str) -> str:
    raw = str(value).replace("\\", "/").strip("/")
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise WorkspaceChangeError(f"invalid repository-relative path: {value!r}")
    return path.as_posix()


def _normalize_subject(value: str) -> str:
    subject = " ".join(str(value).splitlines()).strip()
    if not subject:
        raise WorkspaceChangeError("commit subject cannot be empty")
    if len(subject) > 120:
        raise WorkspaceChangeError("commit subject must be at most 120 characters")
    return subject


def _normalize_body(value: str) -> str:
    body = str(value).replace("\r\n", "\n").strip()
    if len(body) > 8_000:
        raise WorkspaceChangeError("commit body must be at most 8000 characters")
    return body


def _selection_mapping(
    selected: Iterable[tuple[WorkspaceRepository, Sequence[ChangeEntry], str]]
) -> dict[str, list[str]]:
    return {
        repository.repository_id: [entry.path for entry in entries]
        for repository, entries, _digest in selected
    }

def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
