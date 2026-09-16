"""Repository write-scope matching, classification, and bounded expansion policy.

The orchestrator intentionally keeps generated shard scopes narrow.  This module
centralizes the small amount of policy needed to distinguish routine supporting
changes (tests, verification evidence, package metadata, and adjacent source
files) from broad or protected changes that still require an operator decision.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping


_DEFAULT_DENY_PATTERNS = (
    ".github/**",
    "**/.github/**",
    "PLAN.md",
    "BRIEF.md",
    "TASK.yaml",
    "**/PLAN.md",
    "**/BRIEF.md",
    "**/TASK.yaml",
    "pyproject.toml",
    "**/pyproject.toml",
    "package-lock.json",
    "**/package-lock.json",
    "Cargo.lock",
    "**/Cargo.lock",
    "**/security/**",
)

_GLOB_META = frozenset("*?[")


_DEFAULT_SCOPE_RECOVERY_CLEANUP_PATTERNS = (
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
)


@dataclass(frozen=True)
class ScopePolicy:
    """Bounded policy for automatic exact-path scope expansion."""

    enabled: bool = True
    max_files: int = 8
    max_changed_lines: int = 800
    allow_tests: bool = True
    allow_verification_docs: bool = True
    allow_package_metadata: bool = True
    allow_adjacent_source: bool = True
    deny_patterns: tuple[str, ...] = field(default_factory=lambda: _DEFAULT_DENY_PATTERNS)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "ScopePolicy":
        if not raw:
            return cls()
        if not isinstance(raw, Mapping):
            raise ValueError("scope policy must be a mapping")

        def _positive_int(key: str, default: int) -> int:
            value = raw.get(key, default)
            try:
                parsed = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"scope policy {key} must be an integer") from exc
            if parsed <= 0:
                raise ValueError(f"scope policy {key} must be positive")
            return parsed

        deny_raw = raw.get("deny_patterns", _DEFAULT_DENY_PATTERNS)
        if not isinstance(deny_raw, (list, tuple)) or not all(
            isinstance(item, str) and item.strip() for item in deny_raw
        ):
            raise ValueError("scope policy deny_patterns must be a list of non-empty strings")

        return cls(
            enabled=bool(raw.get("enabled", True)),
            max_files=_positive_int("max_files", cls.max_files),
            max_changed_lines=_positive_int(
                "max_changed_lines", cls.max_changed_lines
            ),
            allow_tests=bool(raw.get("allow_tests", True)),
            allow_verification_docs=bool(
                raw.get("allow_verification_docs", True)
            ),
            allow_package_metadata=bool(
                raw.get("allow_package_metadata", True)
            ),
            allow_adjacent_source=bool(raw.get("allow_adjacent_source", True)),
            deny_patterns=tuple(_normalize_path(item) for item in deny_raw),
        )

    def as_mapping(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "max_files": self.max_files,
            "max_changed_lines": self.max_changed_lines,
            "allow_tests": self.allow_tests,
            "allow_verification_docs": self.allow_verification_docs,
            "allow_package_metadata": self.allow_package_metadata,
            "allow_adjacent_source": self.allow_adjacent_source,
            "deny_patterns": list(self.deny_patterns),
        }


@dataclass(frozen=True)
class ScopeRecoveryPolicy:
    """Policy for agent-assisted recovery from workspace ownership failures.

    One policy now covers write-scope violations and dirty repositories outside
    the current package.  The recovery agent may repair the workspace and
    propose exact paths to retain.  The orchestrator validates every proposal,
    rejects protected paths, updates package ownership, reruns checks when the
    recovered delta changes, and owns the atomic commit transaction.
    """

    enabled: bool = False
    auto_resume: bool = True
    max_resume_attempts: int = 2
    prefer_reviewer: bool = True
    allow_implementer_fallback: bool = True
    allow_cross_repository: bool = True
    cleanup_untracked_artifacts: bool = True
    cleanup_patterns: tuple[str, ...] = field(
        default_factory=lambda: _DEFAULT_SCOPE_RECOVERY_CLEANUP_PATTERNS
    )
    max_files: int = 16
    max_repositories: int = 4
    max_changed_lines: int = 2_000
    max_excerpt_bytes: int = 120_000

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "ScopeRecoveryPolicy":
        if not raw:
            return cls()
        if not isinstance(raw, Mapping):
            raise ValueError("scope recovery policy must be a mapping")

        def _positive_int(key: str, default: int) -> int:
            value = raw.get(key, default)
            try:
                parsed = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"scope recovery policy {key} must be an integer"
                ) from exc
            if parsed <= 0:
                raise ValueError(
                    f"scope recovery policy {key} must be positive"
                )
            return parsed

        def _boolean(key: str, default: bool) -> bool:
            value = raw.get(key, default)
            if not isinstance(value, bool):
                raise ValueError(
                    f"scope recovery policy {key} must be a boolean"
                )
            return value

        cleanup_raw = raw.get(
            "cleanup_patterns", _DEFAULT_SCOPE_RECOVERY_CLEANUP_PATTERNS
        )
        if not isinstance(cleanup_raw, (list, tuple)) or not all(
            isinstance(item, str) and item.strip() for item in cleanup_raw
        ):
            raise ValueError(
                "scope recovery policy cleanup_patterns must be a list of non-empty strings"
            )

        return cls(
            enabled=_boolean("enabled", False),
            auto_resume=_boolean("auto_resume", True),
            max_resume_attempts=_positive_int(
                "max_resume_attempts", cls.max_resume_attempts
            ),
            prefer_reviewer=_boolean("prefer_reviewer", True),
            allow_implementer_fallback=_boolean(
                "allow_implementer_fallback", True
            ),
            allow_cross_repository=_boolean("allow_cross_repository", True),
            cleanup_untracked_artifacts=_boolean(
                "cleanup_untracked_artifacts", True
            ),
            cleanup_patterns=tuple(_normalize_path(item) for item in cleanup_raw),
            max_files=_positive_int("max_files", cls.max_files),
            max_repositories=_positive_int(
                "max_repositories", cls.max_repositories
            ),
            max_changed_lines=_positive_int(
                "max_changed_lines", cls.max_changed_lines
            ),
            max_excerpt_bytes=_positive_int(
                "max_excerpt_bytes", cls.max_excerpt_bytes
            ),
        )

    def as_mapping(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "auto_resume": self.auto_resume,
            "max_resume_attempts": self.max_resume_attempts,
            "prefer_reviewer": self.prefer_reviewer,
            "allow_implementer_fallback": self.allow_implementer_fallback,
            "allow_cross_repository": self.allow_cross_repository,
            "cleanup_untracked_artifacts": self.cleanup_untracked_artifacts,
            "cleanup_patterns": list(self.cleanup_patterns),
            "max_files": self.max_files,
            "max_repositories": self.max_repositories,
            "max_changed_lines": self.max_changed_lines,
            "max_excerpt_bytes": self.max_excerpt_bytes,
        }


def scope_recovery_policy_from_scheduling(
    scheduling: Mapping[str, Any] | None,
) -> ScopeRecoveryPolicy:
    """Build the canonical recovery policy from ``scheduling``.

    ``scheduling.automatic_recovery`` is the single user-facing switch.  The
    older nested ``scope_recovery.enabled`` and ``auto_resume`` keys remain
    accepted for existing projects, but the top-level boolean takes precedence
    and controls both initial recovery and resume from a persisted check.
    """

    if not scheduling:
        return ScopeRecoveryPolicy()
    if not isinstance(scheduling, Mapping):
        raise ValueError("agent scheduling policy must be a mapping")

    nested = scheduling.get("scope_recovery") or {}
    if not isinstance(nested, Mapping):
        raise ValueError("scheduling.scope_recovery must be a mapping")
    raw = dict(nested)

    if "automatic_recovery" in scheduling:
        automatic = scheduling["automatic_recovery"]
        if not isinstance(automatic, bool):
            raise ValueError("scheduling.automatic_recovery must be a boolean")
        raw["enabled"] = automatic
        raw["auto_resume"] = automatic

    return ScopeRecoveryPolicy.from_mapping(raw)


@dataclass(frozen=True)
class ScopePathAssessment:
    """Classification for one repository-qualified changed path."""

    qualified_path: str
    repository_id: str
    relative_path: str
    category: str
    auto_expandable: bool
    reason: str
    changed_lines: int = 0

    def as_mapping(self) -> dict[str, Any]:
        return {
            "path": self.qualified_path,
            "repository_id": self.repository_id,
            "relative_path": self.relative_path,
            "category": self.category,
            "auto_expandable": self.auto_expandable,
            "reason": self.reason,
            "changed_lines": self.changed_lines,
        }


def repository_scope_patterns(
    repository_id: str,
    write_scope: Iterable[str],
) -> list[str]:
    """Return normalized patterns applicable to one repository."""

    prefix = repository_id.rstrip("/") + "/"
    patterns: list[str] = []
    for raw in write_scope:
        pattern = _normalize_path(str(raw).strip())
        if pattern.startswith(prefix):
            pattern = pattern[len(prefix) :]
        elif ":" in pattern:
            declared_repository, _, remainder = pattern.partition(":")
            if declared_repository != repository_id:
                continue
            pattern = remainder
        if pattern and pattern not in patterns:
            patterns.append(pattern)
    return patterns


def path_matches_scope(
    relative_path: str,
    patterns: Iterable[str],
    *,
    repository_root: Path | None = None,
) -> bool:
    """Match exact files, glob declarations, and declared directories.

    Historically a scope entry such as ``repo/service_runtime`` matched only a file
    literally named ``service_runtime``.  When the path exists as a directory (or is
    explicitly written with a trailing slash), it now covers descendants as an
    operator naturally expects.
    """

    relative = _normalize_path(relative_path)
    for raw_pattern in patterns:
        pattern = _normalize_path(raw_pattern)
        if not pattern:
            continue
        if _contains_glob(pattern):
            if fnmatch.fnmatchcase(relative, pattern):
                return True
            continue
        if relative == pattern:
            return True
        directory_declared = raw_pattern.rstrip().replace("\\", "/").endswith("/")
        if repository_root is not None:
            directory_declared = directory_declared or (repository_root / pattern).is_dir()
        if directory_declared and relative.startswith(pattern.rstrip("/") + "/"):
            return True
    return False


def classify_scope_path(
    qualified_path: str,
    *,
    patterns: Iterable[str],
    policy: ScopePolicy,
    changed_lines: int = 0,
) -> ScopePathAssessment:
    """Classify one undeclared path without mutating package state."""

    repository_id, separator, relative = qualified_path.partition(":")
    if not separator:
        repository_id, relative = "", qualified_path
    relative = _normalize_path(relative)
    normalized_patterns = [_normalize_path(item) for item in patterns if str(item).strip()]

    denied = next(
        (pattern for pattern in policy.deny_patterns if fnmatch.fnmatchcase(relative, pattern)),
        "",
    )
    if denied:
        return ScopePathAssessment(
            qualified_path=qualified_path,
            repository_id=repository_id,
            relative_path=relative,
            category="protected",
            auto_expandable=False,
            reason=f"matches protected pattern {denied!r}",
            changed_lines=changed_lines,
        )

    path = PurePosixPath(relative)
    parts = tuple(part.lower() for part in path.parts)
    filename = path.name.lower()

    if (
        policy.allow_verification_docs
        and len(parts) >= 2
        and parts[0] == "docs"
        and parts[1] == "verification"
    ):
        return _safe_assessment(
            qualified_path,
            repository_id,
            relative,
            "verification_docs",
            "durable verification evidence",
            changed_lines,
        )

    is_test = (
        "tests" in parts
        or filename.startswith("test_")
        or filename.endswith("_test.py")
        or filename in {"conftest.py", "pytest.ini"}
    )
    if policy.allow_tests and is_test:
        return _safe_assessment(
            qualified_path,
            repository_id,
            relative,
            "tests",
            "test or test-support path",
            changed_lines,
        )

    if policy.allow_package_metadata and filename in {
        "__init__.py",
        "__init__.pyi",
        "py.typed",
    }:
        return _safe_assessment(
            qualified_path,
            repository_id,
            relative,
            "package_metadata",
            "package registration metadata",
            changed_lines,
        )

    if policy.allow_adjacent_source and _is_adjacent_source(relative, normalized_patterns):
        return _safe_assessment(
            qualified_path,
            repository_id,
            relative,
            "adjacent_source",
            "same source directory as an explicitly declared file",
            changed_lines,
        )

    return ScopePathAssessment(
        qualified_path=qualified_path,
        repository_id=repository_id,
        relative_path=relative,
        category="cross_boundary",
        auto_expandable=False,
        reason="outside safe supporting and adjacent-source categories",
        changed_lines=changed_lines,
    )


def _safe_assessment(
    qualified_path: str,
    repository_id: str,
    relative_path: str,
    category: str,
    reason: str,
    changed_lines: int,
) -> ScopePathAssessment:
    return ScopePathAssessment(
        qualified_path=qualified_path,
        repository_id=repository_id,
        relative_path=relative_path,
        category=category,
        auto_expandable=True,
        reason=reason,
        changed_lines=changed_lines,
    )


def _is_adjacent_source(relative_path: str, patterns: Iterable[str]) -> bool:
    candidate_parent = PurePosixPath(relative_path).parent
    for pattern in patterns:
        if not pattern or _contains_glob(pattern):
            continue
        declared = PurePosixPath(pattern.rstrip("/"))
        # Only exact file declarations establish adjacency. Directory scopes
        # already match recursively in path_matches_scope().
        if declared.suffix and declared.parent == candidate_parent:
            return True
    return False


def _contains_glob(value: str) -> bool:
    return any(character in value for character in _GLOB_META)


def _normalize_path(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lstrip("/")
