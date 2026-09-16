"""Declarative repository synchronization package configuration.

The synchronization spec is deliberately provider-neutral.  It identifies the
immutable upstream refs that the control plane must resolve and the conflict
policy to use when Git cannot complete a clean merge.  Git state and transaction
progress live in :mod:`execraft.repository_sync.transaction`, never in PLAN.graph.yaml.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from execraft.workspace.task_git import validate_branch_name


_REMOTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_ALLOWED_CONFLICT_POLICIES = {"ai_resolve", "human"}
_ALLOWED_STRATEGIES = {"merge"}


class RepositorySyncSpecError(ValueError):
    """Raised when a repository synchronization declaration is unsafe."""


def validate_remote_name(value: object, *, label: str = "repository_sync remote") -> str:
    if not isinstance(value, str):
        raise RepositorySyncSpecError(f"{label} must be a string")
    remote = value.strip()
    if not remote or not _REMOTE_RE.fullmatch(remote):
        raise RepositorySyncSpecError(f"invalid {label}: {value!r}")
    return remote


@dataclass(frozen=True)
class RepositorySyncTarget:
    """One task-owned repository and the upstream branch to merge from."""

    repository_id: str
    source_branch: str = ""
    remote: str = "origin"

    @classmethod
    def from_value(
        cls,
        repository_id: str,
        value: object,
        *,
        default_remote: str,
    ) -> "RepositorySyncTarget":
        repository = str(repository_id).strip()
        if not repository:
            raise RepositorySyncSpecError("repository sync target ID cannot be empty")
        source_branch = ""
        remote = default_remote
        if value is None:
            pass
        elif isinstance(value, str):
            source_branch = value.strip()
        elif isinstance(value, Mapping):
            source_value = value.get("source_branch", "")
            remote_value = value.get("remote", default_remote)
            if not isinstance(source_value, str):
                raise RepositorySyncSpecError(
                    f"source_branch for {repository!r} must be a string"
                )
            source_branch = source_value.strip()
            remote = validate_remote_name(
                remote_value, label=f"remote for {repository}"
            )
        else:
            raise RepositorySyncSpecError(
                f"repository sync target {repository!r} must be a string, mapping, or null"
            )
        if source_branch:
            try:
                validate_branch_name(
                    source_branch, label=f"repository sync source branch for {repository}"
                )
            except Exception as exc:
                raise RepositorySyncSpecError(str(exc)) from exc
        return cls(
            repository_id=repository,
            source_branch=source_branch,
            remote=(
                remote
                if isinstance(value, Mapping)
                else validate_remote_name(remote, label=f"remote for {repository}")
            ),
        )

    def as_mapping(self) -> dict[str, Any]:
        result: dict[str, Any] = {"remote": self.remote}
        if self.source_branch:
            result["source_branch"] = self.source_branch
        return result


@dataclass(frozen=True)
class RepositorySyncSpec:
    """Validated synchronization declaration attached to a work package."""

    targets: tuple[RepositorySyncTarget, ...]
    strategy: str = "merge"
    conflict_policy: str = "ai_resolve"
    remote: str = "origin"
    require_independent_review: bool = True

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "RepositorySyncSpec":
        if not isinstance(raw, Mapping):
            raise RepositorySyncSpecError("repository_sync must be a mapping")
        strategy = str(raw.get("strategy", "merge")).strip().lower()
        if strategy not in _ALLOWED_STRATEGIES:
            raise RepositorySyncSpecError(
                "repository_sync strategy must be 'merge'; automatic rebase is intentionally unsupported"
            )
        conflict_policy = str(raw.get("conflict_policy", "ai_resolve")).strip().lower()
        if conflict_policy not in _ALLOWED_CONFLICT_POLICIES:
            raise RepositorySyncSpecError(
                "repository_sync conflict_policy must be ai_resolve or human"
            )
        default_remote = validate_remote_name(
            raw.get("remote", "origin"), label="repository_sync remote"
        )
        require_review = raw.get("require_independent_review", True)
        if not isinstance(require_review, bool):
            raise RepositorySyncSpecError(
                "repository_sync require_independent_review must be a boolean"
            )
        repositories = raw.get("repositories")
        targets: list[RepositorySyncTarget] = []
        if isinstance(repositories, Mapping):
            for repository_id, value in repositories.items():
                targets.append(
                    RepositorySyncTarget.from_value(
                        str(repository_id), value, default_remote=default_remote
                    )
                )
        elif isinstance(repositories, list):
            for value in repositories:
                if isinstance(value, str):
                    targets.append(
                        RepositorySyncTarget.from_value(
                            value, None, default_remote=default_remote
                        )
                    )
                elif isinstance(value, Mapping):
                    repository_id = str(
                        value.get("id") or value.get("repository") or ""
                    ).strip()
                    targets.append(
                        RepositorySyncTarget.from_value(
                            repository_id, value, default_remote=default_remote
                        )
                    )
                else:
                    raise RepositorySyncSpecError(
                        "repository_sync repositories list must contain strings or mappings"
                    )
        else:
            raise RepositorySyncSpecError(
                "repository_sync repositories must be a non-empty mapping or list"
            )
        if not targets:
            raise RepositorySyncSpecError(
                "repository_sync must select at least one repository"
            )
        seen: set[str] = set()
        for target in targets:
            if target.repository_id in seen:
                raise RepositorySyncSpecError(
                    f"duplicate repository_sync target: {target.repository_id}"
                )
            seen.add(target.repository_id)
        return cls(
            targets=tuple(targets),
            strategy=strategy,
            conflict_policy=conflict_policy,
            remote=default_remote,
            require_independent_review=require_review,
        )

    @property
    def repository_ids(self) -> tuple[str, ...]:
        return tuple(target.repository_id for target in self.targets)

    def target(self, repository_id: str) -> RepositorySyncTarget:
        for target in self.targets:
            if target.repository_id == repository_id:
                return target
        raise RepositorySyncSpecError(
            f"repository {repository_id!r} is not selected by repository_sync"
        )

    def as_mapping(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "remote": self.remote,
            "conflict_policy": self.conflict_policy,
            "require_independent_review": self.require_independent_review,
            "repositories": {
                target.repository_id: target.as_mapping() for target in self.targets
            },
        }

    def fingerprint(self) -> str:
        payload = json.dumps(
            self.as_mapping(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


__all__ = [
    "RepositorySyncSpec",
    "RepositorySyncSpecError",
    "RepositorySyncTarget",
]
