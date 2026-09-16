"""Shared validation for task-owned repository synchronization selections."""

from __future__ import annotations

from typing import Sequence

from execraft.workspace.task_git import TaskManifest


class RepositorySyncSelectionError(ValueError):
    """Raised when selected repositories cannot be synchronized safely."""


def validate_sync_repository_selection(
    manifest: TaskManifest, repositories: Sequence[str]
) -> tuple[str, ...]:
    """Normalize and validate a synchronization repository selection."""

    selected = tuple(
        dict.fromkeys(str(item).strip() for item in repositories if str(item).strip())
    )
    if not selected:
        raise RepositorySyncSelectionError(
            "at least one repository must be selected for synchronization"
        )
    by_id = {repository.id: repository for repository in manifest.repositories}
    unknown = [repository_id for repository_id in selected if repository_id not in by_id]
    if unknown:
        raise RepositorySyncSelectionError(
            "repository synchronization references repositories outside TASK.yaml: "
            + ", ".join(unknown)
        )
    immutable = [
        repository_id
        for repository_id in selected
        if by_id[repository_id].mutability != "task_owned"
    ]
    if immutable:
        raise RepositorySyncSelectionError(
            "repository synchronization may select only task_owned repositories: "
            + ", ".join(immutable)
        )
    return selected
