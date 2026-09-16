"""Crash-safe journal for Work Package card synchronization intent.

A card request crosses two durable systems: the definition service publishes a new task-definition
revision, then orchestration installs the optional post-sync hold.  The
small journal in this module closes the crash window between those operations.
It contains no Git state; the repository synchronization transaction remains
the sole authority for fetch/merge/commit recovery.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from execraft.persistence import FileLock, LockLevel, atomic_write_json


class RepositorySyncCardIntentError(RuntimeError):
    """Raised when a persisted card intent is malformed or inconsistent."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_ALLOWED_PHASES = {
    "prepared",
    "replanned",
    "execution_policy_installed",
    "boundary_released",
    "complete",
}
_PHASE_ORDER = {
    "prepared": 0,
    "replanned": 1,
    "execution_policy_installed": 2,
    "boundary_released": 3,
    "complete": 4,
}


@dataclass(frozen=True)
class RepositorySyncCardIntent:
    command_id: str
    package_id: str
    sync_package_id: str
    request: Mapping[str, Any]
    phase: str = "prepared"
    candidate_id: str = ""
    revision: int = 0
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        if not self.command_id.strip():
            raise RepositorySyncCardIntentError("card sync intent command_id is required")
        if not self.package_id.strip() or not self.sync_package_id.strip():
            raise RepositorySyncCardIntentError(
                "card sync intent package IDs must not be empty"
            )
        if self.phase not in _ALLOWED_PHASES:
            raise RepositorySyncCardIntentError(
                f"unsupported card sync intent phase: {self.phase!r}"
            )
        if not isinstance(self.request, Mapping):
            raise RepositorySyncCardIntentError("card sync intent request must be a mapping")
        if self.revision < 0:
            raise RepositorySyncCardIntentError("card sync intent revision cannot be negative")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "RepositorySyncCardIntent":
        if not isinstance(raw, Mapping):
            raise RepositorySyncCardIntentError("card sync intent must be a mapping")
        request = raw.get("request", {})
        if not isinstance(request, Mapping):
            raise RepositorySyncCardIntentError("card sync intent request must be a mapping")
        revision = raw.get("revision", 0)
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise RepositorySyncCardIntentError("card sync intent revision must be an integer")
        return cls(
            command_id=str(raw.get("command_id", "")),
            package_id=str(raw.get("package_id", "")),
            sync_package_id=str(raw.get("sync_package_id", "")),
            request=dict(request),
            phase=str(raw.get("phase", "prepared")),
            candidate_id=str(raw.get("candidate_id", "")),
            revision=revision,
            created_at=str(raw.get("created_at", "")),
            updated_at=str(raw.get("updated_at", "")),
        )

    def as_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "command_id": self.command_id,
            "package_id": self.package_id,
            "sync_package_id": self.sync_package_id,
            "request": dict(self.request),
            "phase": self.phase,
            "candidate_id": self.candidate_id,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def advanced(
        self,
        phase: str,
        *,
        candidate_id: str | None = None,
        revision: int | None = None,
    ) -> "RepositorySyncCardIntent":
        return RepositorySyncCardIntent(
            command_id=self.command_id,
            package_id=self.package_id,
            sync_package_id=self.sync_package_id,
            request=dict(self.request),
            phase=phase,
            candidate_id=self.candidate_id if candidate_id is None else candidate_id,
            revision=self.revision if revision is None else revision,
            created_at=self.created_at or _now(),
            updated_at=_now(),
        )


class RepositorySyncCardIntentStore:
    """Atomic per-task card-intent journal with immutable command identity.

    Card coordination runs immediately outside the orchestrator driver lock so
    that the definition service can acquire its own locks. Multiple CLI drivers can therefore reach
    this store concurrently. The store serializes every read/modify/write
    operation with a task-local advisory lock and rejects phase regression.
    """

    def __init__(self, directory: Path):
        self.directory = Path(directory).expanduser().resolve()
        self.lock_path = self.directory.with_name(f".{self.directory.name}.lock")

    @staticmethod
    def _filename(command_id: str) -> str:
        digest = hashlib.sha256(str(command_id).encode("utf-8")).hexdigest()
        return f"{digest}.json"

    def path_for(self, command_id: str) -> Path:
        return self.directory / self._filename(command_id)

    def _exclusive_lock(self) -> FileLock:
        return FileLock(self.lock_path, level=LockLevel.RECORD)

    def _load_unlocked(self, command_id: str) -> RepositorySyncCardIntent | None:
        path = self.path_for(command_id)
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RepositorySyncCardIntentError(
                f"invalid card sync intent {path}: {exc}"
            ) from exc
        return RepositorySyncCardIntent.from_mapping(raw)

    def load(self, command_id: str) -> RepositorySyncCardIntent | None:
        with self._exclusive_lock():
            return self._load_unlocked(command_id)

    def _pending_unlocked(self) -> tuple[RepositorySyncCardIntent, ...]:
        if not self.directory.is_dir():
            return ()
        intents: list[RepositorySyncCardIntent] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                intent = RepositorySyncCardIntent.from_mapping(raw)
            except (OSError, json.JSONDecodeError, RepositorySyncCardIntentError) as exc:
                raise RepositorySyncCardIntentError(
                    f"invalid card sync intent {path}: {exc}"
                ) from exc
            if intent.phase != "complete":
                intents.append(intent)
        return tuple(intents)

    def pending(self) -> tuple[RepositorySyncCardIntent, ...]:
        with self._exclusive_lock():
            return self._pending_unlocked()

    @staticmethod
    def _assert_same_identity(
        existing: RepositorySyncCardIntent,
        replacement: RepositorySyncCardIntent,
    ) -> None:
        if (
            existing.command_id != replacement.command_id
            or existing.package_id != replacement.package_id
            or existing.sync_package_id != replacement.sync_package_id
            or dict(existing.request) != dict(replacement.request)
        ):
            raise RepositorySyncCardIntentError(
                "card sync intent identity conflicts with the persisted request"
            )
        if _PHASE_ORDER[replacement.phase] < _PHASE_ORDER[existing.phase]:
            raise RepositorySyncCardIntentError(
                "card sync intent phase cannot move backwards "
                f"({existing.phase!r} -> {replacement.phase!r})"
            )
        if (
            existing.candidate_id
            and replacement.candidate_id
            and existing.candidate_id != replacement.candidate_id
        ):
            raise RepositorySyncCardIntentError(
                "card sync intent candidate identity cannot change"
            )
        if (
            existing.revision
            and replacement.revision
            and existing.revision != replacement.revision
        ):
            raise RepositorySyncCardIntentError(
                "card sync intent accepted revision cannot change"
            )

    def prepare(
        self,
        *,
        command_id: str,
        package_id: str,
        sync_package_id: str,
        request: Mapping[str, Any],
    ) -> RepositorySyncCardIntent:
        with self._exclusive_lock():
            existing = self._load_unlocked(command_id)
            if existing is not None:
                probe = RepositorySyncCardIntent(
                    command_id=command_id,
                    package_id=package_id,
                    sync_package_id=sync_package_id,
                    request=dict(request),
                    phase=existing.phase,
                    candidate_id=existing.candidate_id,
                    revision=existing.revision,
                    created_at=existing.created_at,
                    updated_at=existing.updated_at,
                )
                self._assert_same_identity(existing, probe)
                return existing
            now = _now()
            intent = RepositorySyncCardIntent(
                command_id=command_id,
                package_id=package_id,
                sync_package_id=sync_package_id,
                request=dict(request),
                phase="prepared",
                created_at=now,
                updated_at=now,
            )
            self._save_unlocked(intent)
            return intent

    def _save_unlocked(self, intent: RepositorySyncCardIntent) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.path_for(intent.command_id)
        existing = self._load_unlocked(intent.command_id)
        if existing is not None:
            self._assert_same_identity(existing, intent)
        atomic_write_json(
            path,
            intent.as_mapping(),
            indent=2,
            ensure_ascii=False,
            trailing_newline=True,
        )

    def save(self, intent: RepositorySyncCardIntent) -> None:
        with self._exclusive_lock():
            self._save_unlocked(intent)


__all__ = [
    "RepositorySyncCardIntent",
    "RepositorySyncCardIntentError",
    "RepositorySyncCardIntentStore",
]
