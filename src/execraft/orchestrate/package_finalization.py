"""Deterministic work-package and aggregate workspace finalization.

The implementation/review state machine owns source changes, while this module
owns the final invariant that a completed package must not leak unexplained Git
state into the next package.  It deliberately contains no provider or
Supervisor behavior: finalization is a framework check, not an AI repair task.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .models import WorkPackage, WorkPackageStage
from .transactions import CommitTransaction


_DEFAULT_FINALIZATION_CLEANUP_PATTERNS = (
    # Python/tooling caches.
    "__pycache__/**",
    "**/__pycache__/**",
    "*.pyc",
    "**/*.pyc",
    "*.pyo",
    "**/*.pyo",
    ".pytest_cache/**",
    "**/.pytest_cache/**",
    ".mypy_cache/**",
    "**/.mypy_cache/**",
    ".ruff_cache/**",
    "**/.ruff_cache/**",
    # Coverage and browser-test reports.
    ".coverage",
    "**/.coverage",
    ".coverage.*",
    "**/.coverage.*",
    "coverage.xml",
    "**/coverage.xml",
    "htmlcov/**",
    "**/htmlcov/**",
    ".nyc_output/**",
    "**/.nyc_output/**",
    "playwright-report/**",
    "**/playwright-report/**",
    "test-results/**",
    "**/test-results/**",
    # CMake/CTest/Ninja metadata.  Broad build/, install/, and log/ patterns
    # are intentionally excluded because those names can contain intentional
    # source files; projects may opt into them explicitly.
    "CMakeFiles/**",
    "**/CMakeFiles/**",
    "CMakeCache.txt",
    "**/CMakeCache.txt",
    "cmake_install.cmake",
    "**/cmake_install.cmake",
    "CTestTestfile.cmake",
    "**/CTestTestfile.cmake",
    "Testing/**",
    "**/Testing/**",
    ".ninja_deps",
    "**/.ninja_deps",
    ".ninja_log",
    "**/.ninja_log",
)


@dataclass(frozen=True)
class WorkspaceFinalizationPolicy:
    """Policy for the deterministic completion barrier.

    Cleanup is restricted to untracked files matching an explicit allow-list.
    Tracked files, unknown untracked files, and Git history are never modified
    by this policy.  Aggregate completion requires committed standard shards
    when automatic commits are enabled.
    """

    enabled: bool = True
    cleanup_untracked_artifacts: bool = True
    cleanup_patterns: tuple[str, ...] = field(
        default_factory=lambda: _DEFAULT_FINALIZATION_CLEANUP_PATTERNS
    )
    require_clean_after_automatic_commit: bool = True
    require_aggregate_child_commits: bool = True
    allow_supervisor_repair: bool = False

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, Any] | None
    ) -> "WorkspaceFinalizationPolicy":
        if not raw:
            return cls()
        if not isinstance(raw, Mapping):
            raise ValueError("workspace finalization policy must be a mapping")

        def boolean(key: str, default: bool) -> bool:
            value = raw.get(key, default)
            if not isinstance(value, bool):
                raise ValueError(
                    f"workspace finalization policy {key} must be a boolean"
                )
            return value

        patterns = raw.get("cleanup_patterns", _DEFAULT_FINALIZATION_CLEANUP_PATTERNS)
        if not isinstance(patterns, (list, tuple)) or not all(
            isinstance(item, str) and item.strip() for item in patterns
        ):
            raise ValueError(
                "workspace finalization policy cleanup_patterns must be a list "
                "of non-empty strings"
            )
        return cls(
            enabled=boolean("enabled", True),
            cleanup_untracked_artifacts=boolean(
                "cleanup_untracked_artifacts", True
            ),
            cleanup_patterns=tuple(_normalize_path(item) for item in patterns),
            require_clean_after_automatic_commit=boolean(
                "require_clean_after_automatic_commit", True
            ),
            require_aggregate_child_commits=boolean(
                "require_aggregate_child_commits", True
            ),
            allow_supervisor_repair=boolean("allow_supervisor_repair", False),
        )

    def as_mapping(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "cleanup_untracked_artifacts": self.cleanup_untracked_artifacts,
            "cleanup_patterns": list(self.cleanup_patterns),
            "require_clean_after_automatic_commit": (
                self.require_clean_after_automatic_commit
            ),
            "require_aggregate_child_commits": (
                self.require_aggregate_child_commits
            ),
            "allow_supervisor_repair": self.allow_supervisor_repair,
        }

    def matches_cleanup_path(self, relative_path: str) -> bool:
        normalized = _normalize_path(relative_path)
        return any(
            fnmatch.fnmatchcase(normalized, pattern)
            for pattern in self.cleanup_patterns
        )


def workspace_finalization_policy_from_scheduling(
    scheduling: Mapping[str, Any] | None,
) -> WorkspaceFinalizationPolicy:
    """Read ``scheduling.workspace_finalization`` with strict types."""

    if not scheduling:
        return WorkspaceFinalizationPolicy()
    if not isinstance(scheduling, Mapping):
        raise ValueError("agent scheduling policy must be a mapping")
    raw = scheduling.get("workspace_finalization") or {}
    if not isinstance(raw, Mapping):
        raise ValueError("scheduling.workspace_finalization must be a mapping")
    return WorkspaceFinalizationPolicy.from_mapping(raw)


def workspace_path_fingerprints(
    repository_paths: Mapping[str, Path],
    dirty_paths: Mapping[str, Sequence[str]],
) -> dict[str, str]:
    """Fingerprint the exact dirty paths produced by an aggregate review fix.

    The fingerprints let finalization distinguish the durable fixer delta from
    later verifier/reviewer mutations on the same repository.  Paths are keyed
    as ``repository_id:relative/path`` so the evidence is stable across task
    workspace locations.
    """

    fingerprints: dict[str, str] = {}
    for repository_id, relative_paths in sorted(dirty_paths.items()):
        repository = repository_paths.get(repository_id)
        if repository is None:
            continue
        root = Path(repository)
        for relative in sorted(set(relative_paths)):
            qualified = f"{repository_id}:{_normalize_path(relative)}"
            candidate = root / relative
            digest = hashlib.sha256()
            if candidate.is_symlink():
                digest.update(b"symlink\0")
                digest.update(os.readlink(candidate).encode("utf-8", errors="surrogateescape"))
            elif candidate.is_file():
                digest.update(b"file\0")
                with candidate.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
            elif candidate.exists():
                digest.update(b"other\0")
                digest.update(str(candidate.stat().st_mode).encode("ascii"))
            else:
                digest.update(b"deleted\0")
            fingerprints[qualified] = digest.hexdigest()
    return fingerprints


@dataclass(frozen=True)
class ChildFinalizationEvidence:
    """Durable completion evidence for one aggregate child shard."""

    package_id: str
    execution_mode: str
    stage: str
    transaction_id: str = ""
    transaction_status: str = ""
    commit_required: bool = False

    @property
    def completed(self) -> bool:
        return self.stage == WorkPackageStage.COMPLETED.value

    @property
    def commit_satisfied(self) -> bool:
        return not self.commit_required or self.transaction_status == "committed"

    def as_mapping(self) -> dict[str, Any]:
        return {
            "package_id": self.package_id,
            "execution_mode": self.execution_mode,
            "stage": self.stage,
            "transaction_id": self.transaction_id,
            "transaction_status": self.transaction_status,
            "commit_required": self.commit_required,
            "completed": self.completed,
            "commit_satisfied": self.commit_satisfied,
        }


@dataclass(frozen=True)
class AggregateFinalizationAssessment:
    """Pure assessment of whether an aggregate may close."""

    parent_id: str
    children: tuple[ChildFinalizationEvidence, ...]
    missing_child_ids: tuple[str, ...] = ()
    active_owner_package_ids: tuple[str, ...] = ()
    active_wave_package_ids: tuple[str, ...] = ()

    @property
    def incomplete_child_ids(self) -> tuple[str, ...]:
        return tuple(item.package_id for item in self.children if not item.completed)

    @property
    def uncommitted_child_ids(self) -> tuple[str, ...]:
        return tuple(
            item.package_id
            for item in self.children
            if item.completed and not item.commit_satisfied
        )

    @property
    def ok(self) -> bool:
        return not (
            self.missing_child_ids
            or self.incomplete_child_ids
            or self.uncommitted_child_ids
            or self.active_owner_package_ids
            or self.active_wave_package_ids
        ) and bool(self.children)

    def problems(self) -> tuple[str, ...]:
        problems: list[str] = []
        if not self.children and not self.missing_child_ids:
            problems.append("aggregate has no child shards")
        if self.missing_child_ids:
            problems.append(
                "aggregate references missing child shards: "
                + ", ".join(self.missing_child_ids)
            )
        if self.incomplete_child_ids:
            problems.append(
                "aggregate child shards are not completed: "
                + ", ".join(self.incomplete_child_ids)
            )
        if self.uncommitted_child_ids:
            problems.append(
                "aggregate child shards lack committed transactions: "
                + ", ".join(self.uncommitted_child_ids)
            )
        if self.active_owner_package_ids:
            problems.append(
                "parallel write ownership is still active: "
                + ", ".join(self.active_owner_package_ids)
            )
        if self.active_wave_package_ids:
            problems.append(
                "parallel shard wave is still active: "
                + ", ".join(self.active_wave_package_ids)
            )
        return tuple(problems)

    def as_mapping(self) -> dict[str, Any]:
        return {
            "parent_id": self.parent_id,
            "ok": self.ok,
            "children": [item.as_mapping() for item in self.children],
            "missing_child_ids": list(self.missing_child_ids),
            "incomplete_child_ids": list(self.incomplete_child_ids),
            "uncommitted_child_ids": list(self.uncommitted_child_ids),
            "active_owner_package_ids": list(self.active_owner_package_ids),
            "active_wave_package_ids": list(self.active_wave_package_ids),
            "problems": list(self.problems()),
        }


def assess_aggregate_children(
    parent: WorkPackage,
    packages: Sequence[WorkPackage],
    transactions: Mapping[str, CommitTransaction | None],
    *,
    require_committed_standard_shards: bool,
    active_owner_package_ids: Iterable[str] = (),
    active_wave_package_ids: Iterable[str] = (),
) -> AggregateFinalizationAssessment:
    """Build deterministic aggregate-child evidence.

    ``shard_ids`` is authoritative when present.  Older persisted aggregate
    plans without that field are supported by discovering direct children via
    ``parent_id``.  Review-only shards never require commits.
    """

    by_id = {item.id: item for item in packages}
    inferred = sorted(item.id for item in packages if item.parent_id == parent.id)
    child_ids = list(dict.fromkeys(parent.shard_ids or inferred))
    missing = tuple(item for item in child_ids if item not in by_id)
    evidence: list[ChildFinalizationEvidence] = []
    for child_id in child_ids:
        child = by_id.get(child_id)
        if child is None:
            continue
        transaction = transactions.get(child_id)
        commit_required = bool(
            require_committed_standard_shards
            and child.execution_mode == "standard_shard"
        )
        evidence.append(
            ChildFinalizationEvidence(
                package_id=child.id,
                execution_mode=child.execution_mode,
                stage=child.stage.value,
                transaction_id=(
                    transaction.transaction_id if transaction is not None else ""
                ),
                transaction_status=(
                    transaction.status if transaction is not None else ""
                ),
                commit_required=commit_required,
            )
        )
    return AggregateFinalizationAssessment(
        parent_id=parent.id,
        children=tuple(evidence),
        missing_child_ids=missing,
        active_owner_package_ids=tuple(sorted(set(active_owner_package_ids))),
        active_wave_package_ids=tuple(sorted(set(active_wave_package_ids))),
    )


def qualify_dirty_paths(
    dirty_paths: Mapping[str, Iterable[str]],
) -> tuple[str, ...]:
    """Flatten repository/path mappings into stable qualified paths."""

    return tuple(
        f"{repository_id}:{_normalize_path(relative)}"
        for repository_id, paths in sorted(dirty_paths.items())
        for relative in sorted(set(paths))
    )


def _normalize_path(value: object) -> str:
    normalized = str(value).replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lstrip("/")


__all__ = [
    "AggregateFinalizationAssessment",
    "ChildFinalizationEvidence",
    "WorkspaceFinalizationPolicy",
    "assess_aggregate_children",
    "qualify_dirty_paths",
    "workspace_finalization_policy_from_scheduling",
]
