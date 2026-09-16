"""Driver-safe durable Work Package directive queue.

Dashboard/operator requests must never mutate ``state.json`` from a second
process while Task Execution owns it.  Instead, requests are appended to this
small sidecar queue and consumed by the orchestrator at deterministic Work
Package boundaries.

The canonical queue is ``work-package-directives.json``.  The queue performs a
one-way migration from the historical ``milestone-directives.json`` path when
it first opens the canonical path.  After migration only the canonical path is
writable; the legacy file is removed after its complete command history has
been durably published to the new file.
"""

from __future__ import annotations

import json
import stat
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from execraft.persistence import FileLock, LockLevel, atomic_write_json

from .models import utc_now

PAUSE_BEFORE_START = "pause_before_start"
PAUSE_FOR_REPOSITORY_SYNC = "pause_for_repository_sync"
REQUIRE_DECOMPOSITION = "require_decomposition"
WORK_PACKAGE_DIRECTIVE_KINDS = frozenset(
    {PAUSE_BEFORE_START, PAUSE_FOR_REPOSITORY_SYNC, REQUIRE_DECOMPOSITION}
)


class WorkPackageDirectiveError(RuntimeError):
    """Raised when a Work Package directive queue or command is invalid."""


@dataclass
class WorkPackageDirectiveCommand:
    """One idempotent operator request awaiting orchestrator application."""

    id: str
    package_id: str
    kind: str
    enabled: bool
    reason: str = ""
    requested_at: str = ""
    requested_by: str = "operator"
    status: str = "pending"
    resolved_at: str = ""
    result: str = ""
    error: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.package_id = str(self.package_id).strip()
        self.kind = str(self.kind).strip()
        self.reason = str(self.reason).strip()
        self.requested_by = str(self.requested_by).strip() or "operator"
        if not isinstance(self.parameters, dict):
            raise WorkPackageDirectiveError(
                "Work Package directive parameters must be a mapping"
            )
        self.parameters = dict(self.parameters)
        if not self.id:
            self.id = uuid.uuid4().hex
        if not self.package_id:
            raise WorkPackageDirectiveError("package_id is required")
        if self.kind not in WORK_PACKAGE_DIRECTIVE_KINDS:
            raise WorkPackageDirectiveError(
                f"unsupported Work Package directive kind: {self.kind!r}"
            )
        if not self.requested_at:
            self.requested_at = utc_now()
        if self.status not in {"pending", "applied", "rejected", "superseded"}:
            raise WorkPackageDirectiveError(
                f"unsupported Work Package directive status: {self.status!r}"
            )

    def as_mapping(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "package_id": self.package_id,
            "kind": self.kind,
            "enabled": bool(self.enabled),
            "reason": self.reason,
            "requested_at": self.requested_at,
            "requested_by": self.requested_by,
            "status": self.status,
            "resolved_at": self.resolved_at,
            "result": self.result,
            "error": self.error,
            "parameters": dict(self.parameters),
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "WorkPackageDirectiveCommand":
        parameters = data.get("parameters", {})
        if not isinstance(parameters, Mapping):
            raise WorkPackageDirectiveError(
                "Work Package directive parameters must be a mapping"
            )
        return cls(
            id=str(data.get("id", "")),
            package_id=str(data.get("package_id", "")),
            kind=str(data.get("kind", "")),
            enabled=bool(data.get("enabled", False)),
            reason=str(data.get("reason", "")),
            requested_at=str(data.get("requested_at", "")),
            requested_by=str(data.get("requested_by", "operator")),
            status=str(data.get("status", "pending")),
            resolved_at=str(data.get("resolved_at", "")),
            result=str(data.get("result", "")),
            error=str(data.get("error", "")),
            parameters=dict(parameters),
        )


class WorkPackageDirectiveQueue:
    """Atomic, flock-protected queue shared by operators and Task Execution."""

    schema_version = 2
    history_limit = 200
    canonical_filename = "work-package-directives.json"
    legacy_filename = "milestone-directives.json"

    def __init__(self, path: Path, *, legacy_path: Path | None = None):
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        if legacy_path is not None:
            self.legacy_path = Path(legacy_path)
        elif self.path.name == self.canonical_filename:
            self.legacy_path = self.path.with_name(self.legacy_filename)
        else:
            self.legacy_path = None

    def _locked(self) -> FileLock:
        return FileLock(self.lock_path, level=LockLevel.RECORD)

    @staticmethod
    def _read_payload(path: Path) -> list[WorkPackageDirectiveCommand]:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return []
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise WorkPackageDirectiveError(
                f"Work Package directive queue must be a regular file: {path}"
            )
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise WorkPackageDirectiveError(
                f"invalid Work Package directive queue {path}: {exc}"
            ) from exc
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict):
            schema = int(raw.get("schema_version", 1))
            if schema not in {1, 2}:
                raise WorkPackageDirectiveError(
                    f"unsupported Work Package directive schema_version {schema!r}: {path}"
                )
            items = raw.get("commands", [])
        else:
            raise WorkPackageDirectiveError(
                f"Work Package directive queue must be an object: {path}"
            )
        if not isinstance(items, list):
            raise WorkPackageDirectiveError(
                f"Work Package directive commands must be a list: {path}"
            )
        return [
            WorkPackageDirectiveCommand.from_mapping(item)
            for item in items
            if isinstance(item, Mapping)
        ]

    def _migrate_legacy_unlocked(self) -> None:
        legacy = self.legacy_path
        if self.path.exists() or legacy is None or not legacy.exists():
            return
        commands = self._read_payload(legacy)
        self._save_unlocked(commands)
        # The canonical copy is durable before removal.  This deliberately
        # leaves exactly one writable queue path for new code.
        try:
            legacy.unlink()
        except OSError as exc:
            raise WorkPackageDirectiveError(
                f"migrated directives but could not retire legacy queue {legacy}: {exc}"
            ) from exc

    def _load_unlocked(self) -> list[WorkPackageDirectiveCommand]:
        self._migrate_legacy_unlocked()
        return self._read_payload(self.path)

    def _save_unlocked(self, commands: list[WorkPackageDirectiveCommand]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        pending = [item for item in commands if item.status == "pending"]
        resolved = [item for item in commands if item.status != "pending"]
        retained_ids = {item.id for item in pending}
        retained_ids.update(item.id for item in resolved[-self.history_limit :])
        retained = [item for item in commands if item.id in retained_ids]
        payload = {
            "schema_version": self.schema_version,
            "commands": [item.as_mapping() for item in retained],
        }
        atomic_write_json(self.path, payload, indent=2, ensure_ascii=False)

    def enqueue(
        self,
        *,
        package_id: str,
        kind: str,
        enabled: bool,
        reason: str = "",
        requested_by: str = "operator",
        parameters: Mapping[str, Any] | None = None,
    ) -> WorkPackageDirectiveCommand:
        """Append the latest desired value and supersede stale pending values."""

        command = WorkPackageDirectiveCommand(
            id=uuid.uuid4().hex,
            package_id=package_id,
            kind=kind,
            enabled=bool(enabled),
            reason=reason,
            requested_by=requested_by,
            parameters=dict(parameters or {}),
        )
        with self._locked():
            commands = self._load_unlocked()
            now = utc_now()
            for item in commands:
                if (
                    item.status == "pending"
                    and item.package_id == command.package_id
                    and item.kind == command.kind
                ):
                    item.status = "superseded"
                    item.resolved_at = now
                    item.result = f"superseded by {command.id}"
            commands.append(command)
            self._save_unlocked(commands)
        return command

    def pending(self) -> list[WorkPackageDirectiveCommand]:
        with self._locked():
            return [
                item for item in self._load_unlocked() if item.status == "pending"
            ]

    def effective_pending(self) -> dict[str, dict[str, WorkPackageDirectiveCommand]]:
        """Return the latest pending desired value for each package and kind."""

        result: dict[str, dict[str, WorkPackageDirectiveCommand]] = {}
        for command in self.pending():
            result.setdefault(command.package_id, {})[command.kind] = command
        return result

    def resolve(
        self,
        command_ids: set[str],
        *,
        status: str,
        result: str = "",
        error: str = "",
    ) -> None:
        if status not in {"applied", "rejected"}:
            raise WorkPackageDirectiveError(
                f"unsupported Work Package directive resolution: {status!r}"
            )
        if not command_ids:
            return
        with self._locked():
            commands = self._load_unlocked()
            changed = False
            now = utc_now()
            for item in commands:
                if item.id not in command_ids or item.status != "pending":
                    continue
                item.status = status
                item.resolved_at = now
                item.result = str(result).strip()
                item.error = str(error).strip()
                changed = True
            if changed:
                self._save_unlocked(commands)
