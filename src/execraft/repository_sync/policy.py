"""Divergence policy for long-lived task branches."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


def _nonnegative_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be a non-negative integer")
    if value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _boolean(value: object, *, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a boolean")
    return value


@dataclass(frozen=True)
class RepositorySyncPolicy:
    """Configurable warning/check thresholds; automatic merges are forbidden."""

    warn_behind_commits: int = 0
    require_sync_behind_commits: int = 0
    refresh_before_check: bool = False
    automatic_merge: bool = False

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "RepositorySyncPolicy":
        if not raw:
            return cls()
        if not isinstance(raw, Mapping):
            raise ValueError("scheduling.repository_sync must be a mapping")
        divergence = raw.get("divergence", raw)
        if not isinstance(divergence, Mapping):
            raise ValueError("scheduling.repository_sync.divergence must be a mapping")
        warning = _nonnegative_int(
            divergence.get("warn_behind_commits", 0),
            label="repository-sync warn_behind_commits",
        )
        required = _nonnegative_int(
            divergence.get("require_sync_behind_commits", 0),
            label="repository-sync require_sync_behind_commits",
        )
        automatic = _boolean(
            raw.get("automatic_merge", False),
            label="repository_sync.automatic_merge",
        )
        if automatic:
            raise ValueError(
                "repository_sync.automatic_merge is intentionally unsupported; "
                "divergence may warn/check but only an explicit repository-sync Work Package may merge"
            )
        return cls(
            warn_behind_commits=warning,
            require_sync_behind_commits=required,
            refresh_before_check=_boolean(
                divergence.get(
                    "refresh_before_check",
                    divergence.get("refresh_before_gate", False),
                ),
                label="repository-sync refresh_before_check",
            ),
            automatic_merge=False,
        )

    def severity(self, behind: int) -> str:
        if self.require_sync_behind_commits and behind >= self.require_sync_behind_commits:
            return "required"
        if self.warn_behind_commits and behind >= self.warn_behind_commits:
            return "warning"
        return "current"

    def as_mapping(self) -> dict[str, Any]:
        return {
            "divergence": {
                "warn_behind_commits": self.warn_behind_commits,
                "require_sync_behind_commits": self.require_sync_behind_commits,
                "refresh_before_check": self.refresh_before_check,
            },
            "automatic_merge": False,
        }


__all__ = ["RepositorySyncPolicy"]
