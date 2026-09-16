"""Append-only event journal with atomic replacement and replay."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from execraft.persistence import FileLock, LockLevel, atomic_write_json, fsync_directory
from .models import OrchestrateError


@dataclass
class JournalEntry:
    sequence: int
    timestamp: str
    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)

    def as_mapping(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "payload": self.payload,
        }

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "JournalEntry":
        return cls(
            sequence=int(data["sequence"]),
            timestamp=str(data["timestamp"]),
            event_type=str(data["event_type"]),
            payload=dict(data.get("payload", {})),
        )


class EventJournal:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self._lock_path = self.path.with_suffix(".lock")

    def append(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        timestamp: str | None = None,
    ) -> JournalEntry:
        """Append one event with a process-safe monotonically increasing sequence."""

        with self._locked(exclusive=True):
            entries = self._read_entries_unlocked()
            sequence = (entries[-1].sequence + 1) if entries else 1
            entry = JournalEntry(
                sequence=sequence,
                timestamp=timestamp or _utc_now(),
                event_type=event_type,
                payload=payload or {},
            )
            entries.append(entry)
            self._write_entries_unlocked(entries)
            return entry

    def read(self, from_sequence: int = 1) -> list[JournalEntry]:
        with self._locked(exclusive=False):
            return [
                entry
                for entry in self._read_entries_unlocked()
                if entry.sequence >= from_sequence
            ]

    def replay(
        self,
        handlers: dict[str, Callable[[JournalEntry], None]],
        from_sequence: int = 1,
    ) -> int:
        count = 0
        for entry in self.read(from_sequence=from_sequence):
            handler = handlers.get(entry.event_type)
            if handler:
                handler(entry)
                count += 1
        return count

    def last_sequence(self) -> int:
        entries = self.read()
        return entries[-1].sequence if entries else 0

    def clear(self) -> None:
        with self._locked(exclusive=True):
            if self.path.is_file():
                self.path.unlink()
                fsync_directory(self.path.parent)

    def _locked(self, *, exclusive: bool) -> FileLock:
        """Serialize journal access across threads and driver processes."""

        return FileLock(
            self._lock_path,
            level=LockLevel.RECORD,
            exclusive=exclusive,
        )

    def _read_entries_unlocked(self) -> list[JournalEntry]:
        if not self.path.is_file():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise OrchestrateError(f"corrupt journal: {self.path}: {exc}") from exc
        if not isinstance(data, list):
            raise OrchestrateError(f"journal must be a JSON array: {self.path}")
        return [JournalEntry.from_mapping(item) for item in data]

    def _write_entries_unlocked(self, entries: list[JournalEntry]) -> None:
        atomic_write_json(
            self.path,
            [entry.as_mapping() for entry in entries],
            indent=2,
            ensure_ascii=False,
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def default_journal_path(state_dir: Path, project_id: str) -> Path:
    return state_dir / "journals" / f"{project_id}.json"
