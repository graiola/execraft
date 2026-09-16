"""Durable Work Package card repository synchronization requests.

The dashboard may enqueue these requests while an orchestration driver is
running.  The orchestrator consumes them only at a deterministic package
boundary; Git/replan mutation happens later, after the driver releases its run
locks.  Keeping this contract small and strictly validated prevents dashboard
payloads or manually edited sidecars from becoming implicit Git commands.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from execraft.workspace.task_git import TaskManifest

from .selection import RepositorySyncSelectionError, validate_sync_repository_selection
from .spec import RepositorySyncSpec, RepositorySyncSpecError


class RepositorySyncCardRequestError(ValueError):
    """Raised when a card-triggered synchronization request is invalid."""


_ALLOWED_MODES = {"before", "after"}


@dataclass(frozen=True)
class RepositorySyncCardRequest:
    """One operator request to synchronize at a Work Package-safe boundary."""

    package_id: str
    mode: str
    repositories: tuple[str, ...]
    source_branches: Mapping[str, str]
    remote: str = "origin"
    conflict_policy: str = "ai_resolve"
    sync_package_id: str = ""
    auto_resume: bool = True

    @classmethod
    def create(
        cls,
        *,
        manifest: TaskManifest,
        package_id: str,
        mode: str,
        repositories: Sequence[str],
        source_branches: Mapping[str, str] | None = None,
        remote: str = "origin",
        conflict_policy: str = "ai_resolve",
        sync_package_id: str = "",
        auto_resume: bool = True,
    ) -> "RepositorySyncCardRequest":
        package = str(package_id).strip()
        normalized_mode = str(mode).strip().lower()
        if not package:
            raise RepositorySyncCardRequestError("package_id is required")
        if normalized_mode not in _ALLOWED_MODES:
            raise RepositorySyncCardRequestError(
                "repository-sync card mode must be 'before' or 'after'"
            )
        try:
            selected = validate_sync_repository_selection(manifest, repositories)
        except RepositorySyncSelectionError as exc:
            raise RepositorySyncCardRequestError(str(exc)) from exc
        overrides = {
            str(key).strip(): str(value).strip()
            for key, value in (source_branches or {}).items()
            if str(key).strip()
        }
        unknown = sorted(set(overrides) - set(selected))
        if unknown:
            raise RepositorySyncCardRequestError(
                "source branch overrides reference unselected repositories: "
                + ", ".join(unknown)
            )
        # Reuse the authoritative repository-sync spec parser for branch/remote/conflict
        # validation.  The spec remains declarative; source refs are pinned to
        # immutable commits only when the synchronization package executes.
        try:
            spec = RepositorySyncSpec.from_mapping(
                {
                    "strategy": "merge",
                    "remote": str(remote).strip() or "origin",
                    "conflict_policy": str(conflict_policy).strip() or "ai_resolve",
                    "repositories": {
                        repository_id: (
                            {"source_branch": overrides[repository_id]}
                            if overrides.get(repository_id)
                            else {}
                        )
                        for repository_id in selected
                    },
                }
            )
        except RepositorySyncSpecError as exc:
            raise RepositorySyncCardRequestError(str(exc)) from exc
        if not isinstance(auto_resume, bool):
            raise RepositorySyncCardRequestError("auto_resume must be a boolean")
        return cls(
            package_id=package,
            mode=normalized_mode,
            repositories=selected,
            source_branches={
                target.repository_id: target.source_branch
                for target in spec.targets
                if target.source_branch
            },
            remote=spec.remote,
            conflict_policy=spec.conflict_policy,
            sync_package_id=str(sync_package_id).strip(),
            auto_resume=auto_resume,
        )

    @classmethod
    def from_parameters(
        cls,
        parameters: Mapping[str, Any],
        *,
        manifest: TaskManifest,
        package_id: str,
    ) -> "RepositorySyncCardRequest":
        if not isinstance(parameters, Mapping):
            raise RepositorySyncCardRequestError(
                "repository-sync directive parameters must be a mapping"
            )
        repositories = parameters.get("repositories", [])
        if not isinstance(repositories, list) or any(
            not isinstance(item, str) for item in repositories
        ):
            raise RepositorySyncCardRequestError(
                "repository-sync directive repositories must be a list of strings"
            )
        source_branches = parameters.get("source_branches", {})
        if not isinstance(source_branches, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in source_branches.items()
        ):
            raise RepositorySyncCardRequestError(
                "repository-sync directive source_branches must map strings to strings"
            )
        auto_resume = parameters.get("auto_resume", True)
        if not isinstance(auto_resume, bool):
            raise RepositorySyncCardRequestError(
                "repository-sync directive auto_resume must be a boolean"
            )
        return cls.create(
            manifest=manifest,
            package_id=package_id,
            mode=str(parameters.get("mode", "before")),
            repositories=repositories,
            source_branches={str(key): str(value) for key, value in source_branches.items()},
            remote=str(parameters.get("remote", "origin")),
            conflict_policy=str(parameters.get("conflict_policy", "ai_resolve")),
            sync_package_id=str(parameters.get("sync_package_id", "")),
            auto_resume=auto_resume,
        )

    def as_parameters(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "repositories": list(self.repositories),
            "source_branches": dict(self.source_branches),
            "remote": self.remote,
            "conflict_policy": self.conflict_policy,
            "sync_package_id": self.sync_package_id,
            "auto_resume": self.auto_resume,
        }


__all__ = [
    "RepositorySyncCardRequest",
    "RepositorySyncCardRequestError",
]
