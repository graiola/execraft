"""Transactional orchestration-state checkpoints.

``state.json`` remains the human-readable compatibility projection. Each save is
first recorded in SQLite with a digest and the observed event-journal sequence,
so a crash or partial filesystem write can be recovered without guessing from
multiple loosely coupled files.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator, Mapping


_SCHEMA_VERSION = 1
_DEFAULT_RETENTION = 256


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _encode(value: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


@dataclass(frozen=True)
class StateCheckpoint:
    sequence: int
    saved_at: str
    journal_sequence: int
    reason: str
    state_sha256: str
    state: dict[str, Any] = field(default_factory=dict)

    def as_mapping(self, *, include_state: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": _SCHEMA_VERSION,
            "sequence": self.sequence,
            "saved_at": self.saved_at,
            "journal_sequence": self.journal_sequence,
            "reason": self.reason,
            "state_sha256": self.state_sha256,
        }
        if include_state:
            result["state"] = dict(self.state)
        return result


class StateCheckpointStore:
    """SQLite checkpoint store with bounded retention and process-safe writes."""

    def __init__(self, path: Path, *, retention: int = _DEFAULT_RETENTION) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.retention = max(8, min(4096, int(retention)))
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.path,
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS checkpoints (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    saved_at TEXT NOT NULL,
                    journal_sequence INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    state_sha256 TEXT NOT NULL,
                    state_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_checkpoints_saved_at "
                "ON checkpoints(saved_at, sequence)"
            )
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
                (str(_SCHEMA_VERSION),),
            )

    def save(
        self,
        state: Mapping[str, Any],
        *,
        journal_sequence: int,
        reason: str = "state_update",
    ) -> StateCheckpoint:
        serialized = _encode(state)
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                latest = connection.execute(
                    "SELECT * FROM checkpoints ORDER BY sequence DESC LIMIT 1"
                ).fetchone()
                if (
                    latest is not None
                    and str(latest["state_sha256"]) == digest
                    and int(latest["journal_sequence"]) == int(journal_sequence)
                ):
                    try:
                        checkpoint = self._from_row(latest)
                    except (ValueError, json.JSONDecodeError):
                        checkpoint = None
                    if checkpoint is not None:
                        connection.execute("COMMIT")
                        return checkpoint
                cursor = connection.execute(
                    """
                    INSERT INTO checkpoints(
                        saved_at, journal_sequence, reason, state_sha256, state_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        _utc_now(),
                        max(0, int(journal_sequence)),
                        str(reason).strip() or "state_update",
                        digest,
                        serialized,
                    ),
                )
                sequence = int(cursor.lastrowid)
                connection.execute(
                    """
                    DELETE FROM checkpoints
                    WHERE sequence NOT IN (
                        SELECT sequence FROM checkpoints
                        ORDER BY sequence DESC LIMIT ?
                    )
                    """,
                    (self.retention,),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        checkpoint = self.get(sequence)
        assert checkpoint is not None
        return checkpoint

    def latest(self) -> StateCheckpoint | None:
        return self.latest_valid()

    def latest_valid(
        self, *, max_journal_sequence: int | None = None
    ) -> StateCheckpoint | None:
        """Return the newest digest-valid checkpoint consistent with the journal."""

        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM checkpoints ORDER BY sequence DESC"
            ).fetchall()
        for row in rows:
            if (
                max_journal_sequence is not None
                and int(row["journal_sequence"]) > int(max_journal_sequence)
            ):
                continue
            try:
                return self._from_row(row)
            except (ValueError, json.JSONDecodeError):
                continue
        return None

    def get(self, sequence: int) -> StateCheckpoint | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM checkpoints WHERE sequence = ?", (int(sequence),)
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def list(self, *, limit: int = 20) -> list[StateCheckpoint]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM checkpoints ORDER BY sequence DESC LIMIT ?",
                (max(1, min(500, int(limit))),),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    @staticmethod
    def _from_row(row: sqlite3.Row) -> StateCheckpoint:
        serialized = str(row["state_json"])
        parsed = json.loads(serialized)
        if not isinstance(parsed, dict):
            raise ValueError("checkpoint state must be a JSON object")
        actual_digest = hashlib.sha256(
            _encode(parsed).encode("utf-8")
        ).hexdigest()
        stored_digest = str(row["state_sha256"])
        if actual_digest != stored_digest:
            raise ValueError(
                f"checkpoint digest mismatch at sequence {int(row['sequence'])}"
            )
        return StateCheckpoint(
            sequence=int(row["sequence"]),
            saved_at=str(row["saved_at"]),
            journal_sequence=int(row["journal_sequence"]),
            reason=str(row["reason"]),
            state_sha256=stored_digest,
            state=dict(parsed),
        )
