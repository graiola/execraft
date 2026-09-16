"""Transactional provenance for every external agent invocation.

The orchestration state and human-facing event journal remain compact summaries.
This module stores the exact handoff, selected skill snapshots, provider identity,
workspace digests, durable result references, and failure details in SQLite.  A
single invocation row is updated transactionally from ``running`` to a terminal
state, which makes retries and failover causally inspectable after crashes.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator, Mapping, Sequence
import uuid

from execraft.execution_identity import ExecutionIdentity
from execraft.runtime.contracts import RuntimeSessionRef

from .invocation_usage import cache_efficiency, summarize_invocation_usage


_SCHEMA_VERSION = 5
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _mapping(value: str) -> dict[str, Any]:
    if not value:
        return {}
    parsed = json.loads(value)
    return dict(parsed) if isinstance(parsed, dict) else {}


def _list(value: str) -> list[Any]:
    if not value:
        return []
    parsed = json.loads(value)
    return list(parsed) if isinstance(parsed, list) else []


def handoff_sha256(handoff: Mapping[str, Any]) -> str:
    """Return the stable digest used to identify the exact provider contract."""

    return hashlib.sha256(_json(dict(handoff)).encode("utf-8")).hexdigest()



def _cache_efficiency(totals: Mapping[str, Any]) -> dict[str, Any]:
    """Legacy import alias for the extracted usage aggregation helper."""

    return cache_efficiency(totals)


@dataclass(frozen=True)
class AgentInvocationRecord:
    invocation_id: str
    project_id: str
    task_id: str
    package_id: str
    stage: str
    capability: str
    attempt: int
    agent_id: str
    # Normalized execution dimensions. ``agent_id`` remains the legacy provider-shaped
    # identity for backward compatibility with existing state/report readers.
    candidate_id: str = ""
    runtime_id: str = ""
    runtime_backend: str = ""
    model_route_id: str = ""
    model_provider: str = ""
    model_name: str = ""
    target_id: str = ""
    target_kind: str = ""
    concurrency_group: str = ""
    adapter: str = ""
    model: str = ""
    runtime_session: dict[str, str] = field(default_factory=dict)
    runtime_metadata: dict[str, Any] = field(default_factory=dict)
    status: str = "running"
    parent_invocation_id: str = ""
    triggering_event_id: str = ""
    started_at: str = ""
    completed_at: str = ""
    duration_seconds: float = 0.0
    handoff: dict[str, Any] = field(default_factory=dict)
    handoff_sha256: str = ""
    skills: list[dict[str, Any]] = field(default_factory=list)
    isolation: dict[str, Any] = field(default_factory=dict)
    workspace_before_digest: str = ""
    workspace_after_digest: str = ""
    result_artifact: dict[str, Any] = field(default_factory=dict)
    normalized_result: dict[str, Any] = field(default_factory=dict)
    validation_errors: list[str] = field(default_factory=list)
    failure: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)

    def as_mapping(self, *, include_handoff: bool = True) -> dict[str, Any]:
        result = {
            "schema_version": _SCHEMA_VERSION,
            "invocation_id": self.invocation_id,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "package_id": self.package_id,
            "stage": self.stage,
            "capability": self.capability,
            "attempt": self.attempt,
            "agent_id": self.agent_id,
            "candidate_id": self.candidate_id or self.agent_id,
            "runtime_id": self.runtime_id,
            "runtime_backend": self.runtime_backend,
            "model_route_id": self.model_route_id,
            "model_provider": self.model_provider,
            "model_name": self.model_name,
            "target_id": self.target_id,
            "target_kind": self.target_kind,
            "concurrency_group": self.concurrency_group,
            "adapter": self.adapter,
            "model": self.model,
            "runtime_session": dict(self.runtime_session),
            "runtime_metadata": dict(self.runtime_metadata),
            "status": self.status,
            "parent_invocation_id": self.parent_invocation_id,
            "triggering_event_id": self.triggering_event_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_seconds": self.duration_seconds,
            "handoff_sha256": self.handoff_sha256,
            "skills": [dict(item) for item in self.skills],
            "isolation": dict(self.isolation),
            "workspace_before_digest": self.workspace_before_digest,
            "workspace_after_digest": self.workspace_after_digest,
            "result_artifact": dict(self.result_artifact),
            "normalized_result": dict(self.normalized_result),
            "validation_errors": list(self.validation_errors),
            "failure": dict(self.failure),
            "usage": dict(self.usage),
        }
        if include_handoff:
            result["handoff"] = dict(self.handoff)
        return result


class AgentInvocationStore:
    """SQLite-backed invocation ledger safe for threads and multiple processes."""

    def __init__(self, path: Path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
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
        connection.execute("PRAGMA foreign_keys=ON")
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
                CREATE TABLE IF NOT EXISTS invocations (
                    invocation_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    task_id TEXT NOT NULL DEFAULT '',
                    package_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    capability TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    agent_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL DEFAULT '',
                    runtime_id TEXT NOT NULL DEFAULT '',
                    runtime_backend TEXT NOT NULL DEFAULT '',
                    model_route_id TEXT NOT NULL DEFAULT '',
                    model_provider TEXT NOT NULL DEFAULT '',
                    model_name TEXT NOT NULL DEFAULT '',
                    target_id TEXT NOT NULL DEFAULT '',
                    target_kind TEXT NOT NULL DEFAULT '',
                    concurrency_group TEXT NOT NULL DEFAULT '',
                    adapter TEXT NOT NULL,
                    model TEXT NOT NULL,
                    runtime_session_json TEXT NOT NULL DEFAULT '{}',
                    runtime_metadata_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL,
                    parent_invocation_id TEXT NOT NULL,
                    triggering_event_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    duration_seconds REAL NOT NULL,
                    handoff_json TEXT NOT NULL,
                    handoff_sha256 TEXT NOT NULL,
                    skills_json TEXT NOT NULL,
                    isolation_json TEXT NOT NULL,
                    workspace_before_digest TEXT NOT NULL,
                    workspace_after_digest TEXT NOT NULL,
                    result_artifact_json TEXT NOT NULL,
                    normalized_result_json TEXT NOT NULL,
                    validation_errors_json TEXT NOT NULL,
                    failure_json TEXT NOT NULL,
                    usage_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(invocations)").fetchall()
            }
            if "task_id" not in columns:
                connection.execute(
                    "ALTER TABLE invocations ADD COLUMN task_id TEXT NOT NULL DEFAULT ''"
                )
                connection.execute(
                    "UPDATE invocations SET task_id = project_id WHERE task_id = ''"
                )
            if "usage_json" not in columns:
                connection.execute(
                    "ALTER TABLE invocations ADD COLUMN usage_json TEXT NOT NULL DEFAULT '{}'"
                )
            dimension_columns = {
                "candidate_id": "TEXT NOT NULL DEFAULT ''",
                "runtime_id": "TEXT NOT NULL DEFAULT ''",
                "runtime_backend": "TEXT NOT NULL DEFAULT ''",
                "model_route_id": "TEXT NOT NULL DEFAULT ''",
                "model_provider": "TEXT NOT NULL DEFAULT ''",
                "model_name": "TEXT NOT NULL DEFAULT ''",
                "target_id": "TEXT NOT NULL DEFAULT ''",
                "target_kind": "TEXT NOT NULL DEFAULT ''",
                "concurrency_group": "TEXT NOT NULL DEFAULT ''",
                "runtime_session_json": "TEXT NOT NULL DEFAULT '{}'",
                "runtime_metadata_json": "TEXT NOT NULL DEFAULT '{}'",
            }
            for column, declaration in dimension_columns.items():
                if column not in columns:
                    connection.execute(
                        f"ALTER TABLE invocations ADD COLUMN {column} {declaration}"
                    )
            # Existing rows remain valid and gain conservative Native defaults.
            connection.execute(
                "UPDATE invocations SET candidate_id = agent_id "
                "WHERE candidate_id = ''"
            )
            connection.execute(
                "UPDATE invocations SET runtime_id = 'native' "
                "WHERE runtime_id = ''"
            )
            connection.execute(
                "UPDATE invocations SET runtime_backend = adapter "
                "WHERE runtime_backend = ''"
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_invocations_package
                ON invocations(project_id, task_id, package_id, started_at, attempt)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_invocations_parent
                ON invocations(parent_invocation_id)
                """
            )
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
                (str(_SCHEMA_VERSION),),
            )

    def begin(
        self,
        *,
        project_id: str,
        package_id: str,
        task_id: str = "",
        stage: str,
        capability: str,
        attempt: int,
        agent_id: str,
        handoff: Mapping[str, Any],
        adapter: str = "",
        model: str = "",
        execution_identity: ExecutionIdentity | None = None,
        parent_invocation_id: str = "",
        triggering_event_id: str = "",
        skills: Sequence[Mapping[str, Any]] = (),
        isolation: Mapping[str, Any] | None = None,
        workspace_before_digest: str = "",
        invocation_id: str = "",
    ) -> AgentInvocationRecord:
        identity = execution_identity or ExecutionIdentity(
            candidate_id=agent_id,
            runtime_id="native",
            runtime_backend=adapter,
            model=model,
            concurrency_group=agent_id,
            legacy_provider_id=agent_id,
        )
        exact_handoff = dict(handoff)
        identifier = invocation_id or uuid.uuid4().hex
        started_at = _utc_now()
        digest = handoff_sha256(exact_handoff)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO invocations (
                        invocation_id, project_id, task_id, package_id, stage, capability,
                        attempt, agent_id, candidate_id, runtime_id, runtime_backend,
                        model_route_id, model_provider, model_name, target_id, target_kind,
                        concurrency_group, adapter, model, runtime_session_json, status,
                        parent_invocation_id, triggering_event_id, started_at,
                        completed_at, duration_seconds, handoff_json, handoff_sha256,
                        skills_json, isolation_json, workspace_before_digest,
                        workspace_after_digest, result_artifact_json, normalized_result_json,
                        validation_errors_json, failure_json, usage_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}',
                              'running', ?, ?, ?, '', 0, ?, ?, ?, ?, ?, '', '{}', '{}', '[]', '{}', '{}')
                    """,
                    (
                        identifier,
                        project_id,
                        str(task_id).strip() or project_id,
                        package_id,
                        stage,
                        capability,
                        max(1, int(attempt)),
                        identity.provider_id,
                        identity.candidate_id,
                        identity.runtime_id,
                        identity.runtime_backend or adapter,
                        identity.model_route_id,
                        identity.model_provider,
                        identity.model,
                        identity.target_id,
                        identity.target_kind,
                        identity.concurrency_group,
                        adapter,
                        model,
                        parent_invocation_id,
                        triggering_event_id,
                        started_at,
                        _json(exact_handoff),
                        digest,
                        _json([dict(item) for item in skills]),
                        _json(dict(isolation or {})),
                        workspace_before_digest,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self.get(identifier)

    def complete(
        self,
        invocation_id: str,
        *,
        duration_seconds: float,
        workspace_after_digest: str = "",
        result_artifact: Mapping[str, Any] | None = None,
        normalized_result: Mapping[str, Any] | None = None,
        validation_errors: Sequence[object] = (),
        usage: Mapping[str, Any] | None = None,
        runtime_session: RuntimeSessionRef | Mapping[str, Any] | None = None,
        runtime_metadata: Mapping[str, Any] | None = None,
    ) -> AgentInvocationRecord:
        return self._finish(
            invocation_id,
            status="completed",
            duration_seconds=duration_seconds,
            workspace_after_digest=workspace_after_digest,
            result_artifact=result_artifact,
            normalized_result=normalized_result,
            validation_errors=validation_errors,
            failure=None,
            usage=usage,
            runtime_session=runtime_session,
            runtime_metadata=runtime_metadata,
        )

    def fail(
        self,
        invocation_id: str,
        *,
        duration_seconds: float,
        failure: Mapping[str, Any],
        workspace_after_digest: str = "",
        result_artifact: Mapping[str, Any] | None = None,
        validation_errors: Sequence[object] = (),
        usage: Mapping[str, Any] | None = None,
        runtime_session: RuntimeSessionRef | Mapping[str, Any] | None = None,
        runtime_metadata: Mapping[str, Any] | None = None,
    ) -> AgentInvocationRecord:
        return self._finish(
            invocation_id,
            status="failed",
            duration_seconds=duration_seconds,
            workspace_after_digest=workspace_after_digest,
            result_artifact=result_artifact,
            normalized_result=None,
            validation_errors=validation_errors,
            failure=failure,
            usage=usage,
            runtime_session=runtime_session,
            runtime_metadata=runtime_metadata,
        )

    def _finish(
        self,
        invocation_id: str,
        *,
        status: str,
        duration_seconds: float,
        workspace_after_digest: str,
        result_artifact: Mapping[str, Any] | None,
        normalized_result: Mapping[str, Any] | None,
        validation_errors: Sequence[object],
        failure: Mapping[str, Any] | None,
        usage: Mapping[str, Any] | None,
        runtime_session: RuntimeSessionRef | Mapping[str, Any] | None,
        runtime_metadata: Mapping[str, Any] | None,
    ) -> AgentInvocationRecord:
        if status not in _TERMINAL_STATUSES:
            raise ValueError(f"invalid terminal invocation status: {status}")
        if isinstance(runtime_session, RuntimeSessionRef):
            session_mapping = runtime_session.as_mapping()
        elif isinstance(runtime_session, Mapping):
            session_mapping = {str(key): str(value) for key, value in runtime_session.items()}
        else:
            session_mapping = {}
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """
                    UPDATE invocations
                    SET status = ?, completed_at = ?, duration_seconds = ?,
                        workspace_after_digest = ?, result_artifact_json = ?,
                        normalized_result_json = ?, validation_errors_json = ?,
                        failure_json = ?, usage_json = ?, runtime_session_json = ?,
                        runtime_metadata_json = ?
                    WHERE invocation_id = ? AND status = 'running'
                    """,
                    (
                        status,
                        _utc_now(),
                        max(0.0, float(duration_seconds)),
                        workspace_after_digest,
                        _json(dict(result_artifact or {})),
                        _json(dict(normalized_result or {})),
                        _json([str(item) for item in validation_errors]),
                        _json(dict(failure or {})),
                        _json(dict(usage or {})),
                        _json(session_mapping),
                        _json(dict(runtime_metadata or {})),
                        invocation_id,
                    ),
                )
                if cursor.rowcount != 1:
                    current = connection.execute(
                        "SELECT status FROM invocations WHERE invocation_id = ?",
                        (invocation_id,),
                    ).fetchone()
                    if current is None:
                        raise KeyError(f"unknown agent invocation: {invocation_id}")
                    raise RuntimeError(
                        f"agent invocation {invocation_id} is already {current['status']}"
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self.get(invocation_id)

    def recover_incomplete(
        self,
        *,
        reason: str = "orchestrator restarted before the provider attempt completed",
    ) -> list[AgentInvocationRecord]:
        """Atomically mark stale ``running`` rows as interrupted failures.

        Provider processes cannot be assumed to survive a driver restart. Leaving
        rows in ``running`` would make the causal history ambiguous, so startup
        recovery closes them before any new attempt is scheduled.
        """

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                rows = connection.execute(
                    "SELECT invocation_id FROM invocations WHERE status = 'running' "
                    "ORDER BY started_at, attempt"
                ).fetchall()
                identifiers = [str(row["invocation_id"]) for row in rows]
                failure = _json(
                    {
                        "classification": "interrupted",
                        "error": str(reason),
                        "persistent": False,
                    }
                )
                if identifiers:
                    connection.execute(
                        """
                        UPDATE invocations
                        SET status = 'failed', completed_at = ?, failure_json = ?
                        WHERE status = 'running'
                        """,
                        (_utc_now(), failure),
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return [self.get(identifier) for identifier in identifiers]

    def list_running(self, *, limit: int = 50) -> list[AgentInvocationRecord]:
        """List currently open provider invocations in newest-first order.

        The invocation database is already scoped to one project/task state
        directory.  Keeping this query independent from the lagging work-package
        projection gives dashboards and diagnostics the exact provider, package,
        and stage that own execution right now.
        """

        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM invocations
                WHERE status = 'running'
                ORDER BY started_at DESC, attempt DESC
                LIMIT ?
                """,
                (max(1, min(500, int(limit))),),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def list_recent(
        self,
        project_id: str,
        *,
        package_id: str = "",
        limit: int = 50,
        newest_first: bool = True,
    ) -> list[AgentInvocationRecord]:
        """List bounded invocation history for diagnostics and CLI tracing."""

        order = "DESC" if newest_first else "ASC"
        clauses = ["project_id = ?"]
        parameters: list[Any] = [project_id]
        if package_id:
            clauses.append("package_id = ?")
            parameters.append(package_id)
        parameters.append(max(1, min(500, int(limit))))
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM invocations
                WHERE {' AND '.join(clauses)}
                ORDER BY started_at {order}, attempt {order}
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def list_for_packages(
        self,
        project_id: str,
        package_ids: Sequence[str],
        *,
        limit: int = 1000,
        newest_first: bool = False,
    ) -> list[AgentInvocationRecord]:
        """List invocation history for a bounded set of package IDs.

        Work Package execution traces commonly aggregate a parent package and its
        generated shards. Querying the set in one SQLite read avoids one
        connection and sort per swimlane while retaining the existing bounded
        result contract.
        """

        normalized = sorted(
            {str(item).strip() for item in package_ids if str(item).strip()}
        )
        if not normalized:
            return []
        order = "DESC" if newest_first else "ASC"
        placeholders = ",".join("?" for _item in normalized)
        parameters: list[Any] = [
            project_id,
            *normalized,
            max(1, min(5000, int(limit))),
        ]
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM invocations
                WHERE project_id = ? AND package_id IN ({placeholders})
                ORDER BY started_at {order}, attempt {order}
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [self._from_row(row) for row in rows]


    def usage_summary(
        self,
        project_id: str,
        *,
        package_id: str = "",
        limit: int = 5000,
    ) -> dict[str, Any]:
        """Aggregate usage across runtime/model/target execution dimensions."""

        records = self.list_recent(
            project_id,
            package_id=package_id,
            limit=max(1, min(5000, int(limit))),
            newest_first=False,
        )
        return summarize_invocation_usage(records)

    def get(self, invocation_id: str) -> AgentInvocationRecord:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM invocations WHERE invocation_id = ?",
                (invocation_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown agent invocation: {invocation_id}")
        return self._from_row(row)

    def list_for_package(
        self,
        project_id: str,
        package_id: str,
        *,
        limit: int = 20,
        newest_first: bool = True,
    ) -> list[AgentInvocationRecord]:
        order = "DESC" if newest_first else "ASC"
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM invocations
                WHERE project_id = ? AND package_id = ?
                ORDER BY started_at {order}, attempt {order}
                LIMIT ?
                """,
                (project_id, package_id, max(1, min(500, int(limit)))),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    @staticmethod
    def _from_row(row: sqlite3.Row) -> AgentInvocationRecord:
        return AgentInvocationRecord(
            invocation_id=str(row["invocation_id"]),
            project_id=str(row["project_id"]),
            task_id=str(row["task_id"]),
            package_id=str(row["package_id"]),
            stage=str(row["stage"]),
            capability=str(row["capability"]),
            attempt=int(row["attempt"]),
            agent_id=str(row["agent_id"]),
            candidate_id=str(row["candidate_id"] or row["agent_id"]),
            runtime_id=str(row["runtime_id"] or "native"),
            runtime_backend=str(row["runtime_backend"] or row["adapter"]),
            model_route_id=str(row["model_route_id"]),
            model_provider=str(row["model_provider"]),
            model_name=str(row["model_name"]),
            target_id=str(row["target_id"]),
            target_kind=str(row["target_kind"]),
            concurrency_group=str(row["concurrency_group"]),
            adapter=str(row["adapter"]),
            model=str(row["model"]),
            runtime_session={
                str(key): str(value)
                for key, value in _mapping(str(row["runtime_session_json"])).items()
            },
            runtime_metadata=_mapping(str(row["runtime_metadata_json"])),
            status=str(row["status"]),
            parent_invocation_id=str(row["parent_invocation_id"]),
            triggering_event_id=str(row["triggering_event_id"]),
            started_at=str(row["started_at"]),
            completed_at=str(row["completed_at"]),
            duration_seconds=float(row["duration_seconds"]),
            handoff=_mapping(str(row["handoff_json"])),
            handoff_sha256=str(row["handoff_sha256"]),
            skills=[dict(item) for item in _list(str(row["skills_json"])) if isinstance(item, dict)],
            isolation=_mapping(str(row["isolation_json"])),
            workspace_before_digest=str(row["workspace_before_digest"]),
            workspace_after_digest=str(row["workspace_after_digest"]),
            result_artifact=_mapping(str(row["result_artifact_json"])),
            normalized_result=_mapping(str(row["normalized_result_json"])),
            validation_errors=[str(item) for item in _list(str(row["validation_errors_json"]))],
            failure=_mapping(str(row["failure_json"])),
            usage=_mapping(str(row["usage_json"])),
        )
