"""Git-specific safeguards for permanent task removal."""

from __future__ import annotations

from pathlib import Path

from execraft.removal_models import BranchRemoval, PermanentRemovalError
from execraft.workspace.task_git import (
    TaskGitError,
    branch_exists,
    current_branch,
    git,
    is_protected_branch,
)
from execraft.workspace.workspace_git import WorkspaceRecord


def created_task_branches(record: WorkspaceRecord | None) -> list[BranchRemoval]:
    """Return only branches whose creation provenance is recorded by Execraft."""

    if record is None:
        return []
    branches: list[BranchRemoval] = []
    seen: set[tuple[Path, str]] = set()
    for item in record.repositories:
        if item.get("mutability", "task_owned") != "task_owned":
            continue
        if str(item.get("created_branch", "false")).lower() != "true":
            continue
        source = Path(str(item.get("source_path", ""))).expanduser().resolve()
        branch = str(item.get("branch", "")).strip()
        repository_id = str(item.get("id", "")).strip() or source.name
        if not branch or (source, branch) in seen:
            continue
        seen.add((source, branch))
        branches.append(BranchRemoval(repository_id, source, branch))
    return branches


def validate_branch_removals(branches: list[BranchRemoval]) -> None:
    """Reject protected/current branches before any destructive filesystem work."""

    for item in branches:
        if is_protected_branch(item.branch):
            raise PermanentRemovalError(
                f"refusing to delete protected branch {item.branch!r} in {item.repository_id}"
            )
        if not item.source_path.is_dir():
            raise PermanentRemovalError(
                f"repository source path is unavailable for branch cleanup: {item.source_path}"
            )
        try:
            exists = branch_exists(item.source_path, item.branch)
            checked_out = exists and current_branch(item.source_path) == item.branch
        except TaskGitError as exc:
            raise PermanentRemovalError(
                f"cannot validate branch {item.branch!r} in {item.source_path}: {exc}"
            ) from exc
        if not exists:
            continue
        if checked_out:
            raise PermanentRemovalError(
                f"task branch {item.branch!r} is checked out in source repository "
                f"{item.source_path}; switch branches before permanent deletion"
            )


def delete_created_branches(branches: list[BranchRemoval]) -> list[str]:
    """Delete validated Execraft-created local task branches."""

    removed: list[str] = []
    for item in branches:
        try:
            if not branch_exists(item.source_path, item.branch):
                continue
            result = git(item.source_path, "branch", "-D", item.branch, check=False)
            # Re-probe instead of depending on Git's localized output.
            if branch_exists(item.source_path, item.branch):
                raise PermanentRemovalError(
                    f"failed to delete task branch {item.branch!r} in "
                    f"{item.source_path}: {result}"
                )
        except TaskGitError as exc:
            raise PermanentRemovalError(
                f"failed to delete task branch {item.branch!r} in {item.source_path}: {exc}"
            ) from exc
        removed.append(f"{item.repository_id}:{item.branch}")
    return removed
