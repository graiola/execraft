"""Deterministic workspace-scope snapshots for automatic recovery.

This module deliberately contains no agent or Git mutation logic.  It turns a
multi-repository dirty workspace into one authoritative classification that the
orchestrator can use for reporting, agent handoffs, validation, and commits.
Keeping that classification pure prevents the clean-start, write-scope, and
commit readiness checks from drifting into subtly different definitions of "out of scope".
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from .scope_policy import path_matches_scope, repository_scope_patterns


@dataclass(frozen=True)
class WorkspaceRecoveryCandidate:
    """One dirty path that the current package does not presently own."""

    qualified_path: str
    repository_id: str
    relative_path: str
    relationship: str

    def as_mapping(self) -> dict[str, str]:
        return {
            "path": self.qualified_path,
            "repository_id": self.repository_id,
            "relative_path": self.relative_path,
            "relationship": self.relationship,
        }


@dataclass(frozen=True)
class WorkspaceScopeSnapshot:
    """Authoritative classification of all currently dirty workspace paths."""

    authorized_paths: tuple[str, ...] = ()
    candidates: tuple[WorkspaceRecoveryCandidate, ...] = ()
    ignored_parallel_paths: tuple[str, ...] = ()

    @property
    def candidate_paths(self) -> list[str]:
        return [item.qualified_path for item in self.candidates]

    @property
    def candidate_repositories(self) -> list[str]:
        return list(dict.fromkeys(item.repository_id for item in self.candidates))

    def as_mapping(self) -> dict[str, object]:
        return {
            "authorized_paths": list(self.authorized_paths),
            "candidates": [item.as_mapping() for item in self.candidates],
            "ignored_parallel_paths": list(self.ignored_parallel_paths),
        }


def flatten_dirty_paths(dirty_paths: Mapping[str, Iterable[str]]) -> list[str]:
    """Return stable repository-qualified paths from a dirty-path mapping."""

    return [
        f"{repository_id}:{relative}"
        for repository_id, paths in sorted(dirty_paths.items())
        for relative in sorted(
            dict.fromkeys(str(item) for item in paths if str(item))
        )
    ]


def build_workspace_scope_snapshot(
    *,
    dirty_paths: Mapping[str, Iterable[str]],
    affected_repositories: Iterable[str],
    write_scope: Iterable[str],
    repository_roots: Mapping[str, Path],
    enforce_write_scope: bool,
    require_clean_workspace: bool = False,
    ignored_parallel_repositories: Iterable[str] = (),
) -> WorkspaceScopeSnapshot:
    """Classify every dirty path against one package's current ownership.

    ``require_clean_workspace`` is used only for package clean-start.  In that
    mode every dirty path is a recovery candidate.  Otherwise paths in affected
    repositories are authorized, except when a generated shard's write scope is
    enforced.  Dirty repositories owned by an active parallel sibling are
    explicitly recorded and ignored rather than stolen by another package.
    """

    affected = {str(item) for item in affected_repositories if str(item)}
    ignored = {str(item) for item in ignored_parallel_repositories if str(item)}
    authorized: list[str] = []
    candidates: list[WorkspaceRecoveryCandidate] = []
    ignored_paths: list[str] = []

    for repository_id, paths in sorted(dirty_paths.items()):
        root = repository_roots.get(repository_id)
        patterns = repository_scope_patterns(repository_id, write_scope)
        normalized_paths = sorted(
            dict.fromkeys(str(item) for item in paths if str(item))
        )
        for relative in normalized_paths:
            qualified = f"{repository_id}:{relative}"
            if repository_id in ignored and repository_id not in affected:
                ignored_paths.append(qualified)
                continue
            if require_clean_workspace:
                candidates.append(
                    WorkspaceRecoveryCandidate(
                        qualified_path=qualified,
                        repository_id=repository_id,
                        relative_path=relative,
                        relationship="clean_start_contamination",
                    )
                )
                continue
            if repository_id not in affected:
                candidates.append(
                    WorkspaceRecoveryCandidate(
                        qualified_path=qualified,
                        repository_id=repository_id,
                        relative_path=relative,
                        relationship="undeclared_repository",
                    )
                )
                continue
            if enforce_write_scope and not path_matches_scope(
                relative,
                patterns,
                repository_root=root,
            ):
                candidates.append(
                    WorkspaceRecoveryCandidate(
                        qualified_path=qualified,
                        repository_id=repository_id,
                        relative_path=relative,
                        relationship="outside_write_scope",
                    )
                )
                continue
            authorized.append(qualified)

    return WorkspaceScopeSnapshot(
        authorized_paths=tuple(authorized),
        candidates=tuple(candidates),
        ignored_parallel_paths=tuple(ignored_paths),
    )
