"""Durable runtime-session continuation policy for OpenClaw.

Runtime sessions are disposable optimization state.  This module keeps the
compatibility decision in Execraft's control plane and always retains a complete
``StructuredHandoff`` so a missing/corrupt runtime session can be reconstructed
cold without losing work-package state.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator, Mapping, Sequence

from execraft.execution_identity import ExecutionIdentity
from execraft.runtime.contracts import RuntimeSessionRef

from .invocations import AgentInvocationStore
from .scheduler import AgentCapability, StructuredHandoff

_SCHEMA_VERSION = 3


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _stable_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def continuation_role(capability: AgentCapability | str) -> str:
    """Return the isolated package role used to bind runtime sessions.

    Implementation remediation intentionally shares the implementer role while
    reviewer/verifier/supervisor contexts remain isolated.
    """

    value = capability.value if isinstance(capability, AgentCapability) else str(capability)
    if value in {AgentCapability.IMPLEMENT.value, AgentCapability.FIX_REVIEW.value}:
        return "implementer"
    if value == AgentCapability.REVIEW.value:
        return "reviewer"
    if value == AgentCapability.VERIFY.value:
        return "verifier"
    if value == AgentCapability.SUPERVISE.value:
        return "supervisor"
    return value.replace("_", "-") or "agent"


def _package_source_fingerprints(handoff: StructuredHandoff) -> dict[str, str]:
    package_context = handoff.execution_context.get("package_context")
    if not isinstance(package_context, Mapping):
        return {}
    raw_path = str(package_context.get("path", "")).strip()
    if not raw_path:
        return {}
    try:
        raw = json.loads(Path(raw_path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    fingerprints = raw.get("source_fingerprints") if isinstance(raw, Mapping) else None
    if not isinstance(fingerprints, Mapping):
        return {}
    return {str(key): str(value) for key, value in fingerprints.items()}


def context_epoch(
    handoff: StructuredHandoff,
    identity: ExecutionIdentity,
    *,
    runtime_policy: Mapping[str, Any] | None = None,
) -> str:
    """Hash authoritative inputs whose change makes session reuse unsafe.

    Transient evidence, workspace digests, attempt counters and prior results
    are deliberately excluded: those belong in delta handoffs.  Plan/dossier
    source fingerprints are included when the package capsule exposes them.
    """

    skills = _skill_fingerprints(handoff)
    policy = {
        str(key): value
        for key, value in dict(runtime_policy or {}).items()
        if key
        in {
            "read_only_enforcement",
            "workspace_write",
            "network_isolation",
            "command_allowlist",
            "structured_output_enforcement",
        }
    }
    payload = {
        "schema_version": handoff.schema_version,
        "work_package_id": handoff.work_package_id,
        "requirements": list(handoff.requirements),
        "acceptance_criteria": list(handoff.acceptance_criteria),
        "expected_output_schema": handoff.expected_output_schema,
        "working_directory": handoff.working_directory,
        "additional_writable_roots": list(handoff.additional_writable_roots),
        "read_only": handoff.read_only,
        "required_isolation": handoff.required_isolation,
        "skill_manifest": skills,
        "source_fingerprints": _package_source_fingerprints(handoff),
        "runtime_id": identity.runtime_id,
        "runtime_backend": identity.runtime_backend,
        "model_route_id": identity.model_route_id,
        "model_provider": identity.model_provider,
        "model": identity.model,
        "target_id": identity.target_id,
        "target_kind": identity.target_kind,
        "runtime_policy": policy,
    }
    return hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()



def _skill_fingerprints(handoff: StructuredHandoff) -> list[dict[str, str]]:
    """Fingerprint the actual selected skill content, not only its manifest.

    Normal orchestration populates ``skill_manifest`` from the canonical catalog,
    but runtime continuation is also exercised by repair/tests and future callers
    that may supply only ``workflow_skills``.  Hashing the rendered skill content
    makes stale-session reuse fail closed even when a caller forgets to refresh a
    declared manifest hash.
    """

    manifest_by_id = {
        str(item.get("id", "")): item
        for item in handoff.skill_manifest
        if isinstance(item, Mapping) and str(item.get("id", ""))
    }
    fingerprints: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in handoff.workflow_skills:
        if not isinstance(raw, Mapping):
            continue
        skill_id = str(raw.get("id", ""))
        if not skill_id:
            continue
        seen.add(skill_id)
        manifest = manifest_by_id.get(skill_id, {})
        instructions = str(raw.get("instructions", ""))
        fingerprints.append(
            {
                "id": skill_id,
                "version": str(raw.get("version", manifest.get("version", ""))),
                "declared_content_hash": str(
                    raw.get(
                        "content_hash",
                        manifest.get("content_hash", manifest.get("sha256", "")),
                    )
                ),
                "instruction_sha256": hashlib.sha256(
                    instructions.encode("utf-8")
                ).hexdigest(),
                "description": str(raw.get("description", "")),
                "source": str(raw.get("source", manifest.get("source", ""))),
                "selection_reason": str(
                    raw.get("selection_reason", manifest.get("selection_reason", ""))
                ),
            }
        )
    for skill_id, manifest in manifest_by_id.items():
        if skill_id in seen:
            continue
        fingerprints.append(
            {
                "id": skill_id,
                "version": str(manifest.get("version", "")),
                "declared_content_hash": str(
                    manifest.get("content_hash", manifest.get("sha256", ""))
                ),
                "instruction_sha256": "",
                "description": "",
                "source": str(manifest.get("source", "")),
                "selection_reason": str(manifest.get("selection_reason", "")),
            }
        )
    return fingerprints

def _changed_mapping(current: Mapping[str, Any], previous: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in current.items()
        if key not in previous or previous.get(key) != value
    }


def _new_sequence(current: Sequence[Any], previous: Sequence[Any]) -> list[Any]:
    previous_serialized = {_stable_json(item) for item in previous}
    return [item for item in current if _stable_json(item) not in previous_serialized]


def build_delta_handoff(
    current: StructuredHandoff,
    previous: StructuredHandoff,
    *,
    epoch: str,
    prior_invocation_id: str,
    previous_workspace_digest: str,
    current_workspace_digest: str,
) -> StructuredHandoff:
    """Build a continuation handoff containing only changed transient context.

    Workflow-skill bodies remain in the authoritative in-process handoff so a
    managed OpenClaw runtime can rematerialize its disposable lazy-skill catalog
    before every continuation. Lazy-skill projection removes those bodies only from the rendered
    OpenClaw turn prompt; Native prompt behavior remains unchanged.
    """

    context_delta = _changed_mapping(current.execution_context, previous.execution_context)
    # Selected-provider metadata is useful for audit but is already represented
    # by execution identity; do not resend it on same-candidate continuation.
    context_delta.pop("selected_provider", None)
    context_delta["runtime_continuation"] = {
        "context_epoch": epoch,
        "prior_invocation_id": prior_invocation_id,
        "previous_workspace_digest": previous_workspace_digest,
        "current_workspace_digest": current_workspace_digest,
        "cold_reconstructible": True,
    }

    current_excerpts = dict(current.bounded_excerpts)
    previous_excerpts = dict(previous.bounded_excerpts)
    excerpt_delta = _changed_mapping(current_excerpts, previous_excerpts)

    current_manifest = [dict(item) for item in current.context_manifest]
    previous_manifest = [dict(item) for item in previous.context_manifest]
    manifest_delta = _new_sequence(current_manifest, previous_manifest)

    return replace(
        current,
        repository_diff_summary=(
            current.repository_diff_summary
            if current.repository_diff_summary != previous.repository_diff_summary
            else ""
        ),
        verification_summary=(
            current.verification_summary
            if current.verification_summary != previous.verification_summary
            else ""
        ),
        unresolved_findings=(
            list(current.unresolved_findings)
            if list(current.unresolved_findings) != list(previous.unresolved_findings)
            else []
        ),
        relevant_decisions=_new_sequence(
            list(current.relevant_decisions), list(previous.relevant_decisions)
        ),
        bounded_excerpts=excerpt_delta,
        requirements=(
            list(current.requirements)
            if list(current.requirements) != list(previous.requirements)
            else []
        ),
        acceptance_criteria=(
            list(current.acceptance_criteria)
            if list(current.acceptance_criteria) != list(previous.acceptance_criteria)
            else []
        ),
        execution_context=context_delta,
        attempt_history=_new_sequence(
            list(current.attempt_history), list(previous.attempt_history)
        ),
        context_manifest=manifest_delta,
        context_profile=f"{current.context_profile}:continuation",
    )


@dataclass(frozen=True)
class RuntimeSessionBinding:
    package_id: str
    role: str
    candidate_id: str
    runtime_id: str
    model_route_id: str
    target_id: str
    runtime_backend: str
    model_provider: str
    model_name: str
    target_kind: str
    context_epoch: str
    session_ref: RuntimeSessionRef
    last_invocation_id: str
    last_workspace_digest: str = ""
    reuse_count: int = 0
    guarded_compaction_count: int = 0
    updated_at: str = ""

    def identity_compatible(self, identity: ExecutionIdentity) -> bool:
        return (
            self.candidate_id == identity.candidate_id
            and self.runtime_id == identity.runtime_id
            and self.runtime_backend == identity.runtime_backend
            and self.model_route_id == identity.model_route_id
            and self.model_provider == identity.model_provider
            and self.model_name == identity.model
            and self.target_id == identity.target_id
            and self.target_kind == identity.target_kind
            and self.session_ref.runtime_id == identity.runtime_id
            and self.session_ref.candidate_id == identity.candidate_id
        )

    def compatible(self, identity: ExecutionIdentity, epoch: str) -> bool:
        return self.identity_compatible(identity) and self.context_epoch == epoch


class RuntimeSessionBindingStore:
    """SQLite-backed package/role session bindings safe across Execraft restarts."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
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
                CREATE TABLE IF NOT EXISTS runtime_sessions (
                    package_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    runtime_id TEXT NOT NULL,
                    model_route_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    runtime_backend TEXT NOT NULL DEFAULT '',
                    model_provider TEXT NOT NULL DEFAULT '',
                    model_name TEXT NOT NULL DEFAULT '',
                    target_kind TEXT NOT NULL DEFAULT '',
                    context_epoch TEXT NOT NULL,
                    session_json TEXT NOT NULL,
                    last_invocation_id TEXT NOT NULL,
                    last_workspace_digest TEXT NOT NULL,
                    reuse_count INTEGER NOT NULL DEFAULT 0,
                    guarded_compaction_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(package_id, role)
                )
                """
            )
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(runtime_sessions)").fetchall()
            }
            if "guarded_compaction_count" not in columns:
                connection.execute(
                    "ALTER TABLE runtime_sessions ADD COLUMN "
                    "guarded_compaction_count INTEGER NOT NULL DEFAULT 0"
                )
            for column in (
                "runtime_backend",
                "model_provider",
                "model_name",
                "target_kind",
            ):
                if column not in columns:
                    connection.execute(
                        f"ALTER TABLE runtime_sessions ADD COLUMN {column} "
                        "TEXT NOT NULL DEFAULT ''"
                    )
            if not self._metadata_exists(connection):
                self._create_metadata(connection)
            else:
                connection.execute(
                    "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
                    (str(_SCHEMA_VERSION),),
                )

    @staticmethod
    def _metadata_exists(connection: sqlite3.Connection) -> bool:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'"
        ).fetchone()
        return row is not None

    @staticmethod
    def _create_metadata(connection: sqlite3.Connection) -> None:
        connection.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES('schema_version', ?)",
            (str(_SCHEMA_VERSION),),
        )

    def get(self, package_id: str, role: str) -> RuntimeSessionBinding | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_sessions WHERE package_id = ? AND role = ?",
                (package_id, role),
            ).fetchone()
        if row is None:
            return None
        try:
            return self._from_row(row)
        except (KeyError, TypeError, ValueError):
            # A malformed binding is optimization-state corruption, not a
            # workflow failure. Drop it so the next execution cold-reconstructs.
            self.invalidate(package_id, role)
            return None

    def upsert(
        self,
        *,
        package_id: str,
        role: str,
        identity: ExecutionIdentity,
        epoch: str,
        session_ref: RuntimeSessionRef,
        last_invocation_id: str,
        last_workspace_digest: str,
        reused: bool,
        guarded_compaction_count: int | None = None,
    ) -> RuntimeSessionBinding:
        existing = self.get(package_id, role)
        reuse_count = (existing.reuse_count if existing else 0) + (1 if reused else 0)
        guarded_count = (
            max(0, int(guarded_compaction_count))
            if guarded_compaction_count is not None
            else (existing.guarded_compaction_count if existing else 0)
        )
        session = session_ref.as_mapping()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO runtime_sessions(
                        package_id, role, candidate_id, runtime_id, model_route_id,
                        target_id, runtime_backend, model_provider, model_name, target_kind,
                        context_epoch, session_json, last_invocation_id, last_workspace_digest,
                        reuse_count, guarded_compaction_count, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(package_id, role) DO UPDATE SET
                        candidate_id=excluded.candidate_id,
                        runtime_id=excluded.runtime_id,
                        model_route_id=excluded.model_route_id,
                        target_id=excluded.target_id,
                        runtime_backend=excluded.runtime_backend,
                        model_provider=excluded.model_provider,
                        model_name=excluded.model_name,
                        target_kind=excluded.target_kind,
                        context_epoch=excluded.context_epoch,
                        session_json=excluded.session_json,
                        last_invocation_id=excluded.last_invocation_id,
                        last_workspace_digest=excluded.last_workspace_digest,
                        reuse_count=excluded.reuse_count,
                        guarded_compaction_count=excluded.guarded_compaction_count,
                        updated_at=excluded.updated_at
                    """,
                    (
                        package_id,
                        role,
                        identity.candidate_id,
                        identity.runtime_id,
                        identity.model_route_id,
                        identity.target_id,
                        identity.runtime_backend,
                        identity.model_provider,
                        identity.model,
                        identity.target_kind,
                        epoch,
                        _stable_json(session),
                        last_invocation_id,
                        last_workspace_digest,
                        reuse_count,
                        guarded_count,
                        _utc_now(),
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        stored = self.get(package_id, role)
        assert stored is not None
        return stored

    def invalidate(self, package_id: str, role: str) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM runtime_sessions WHERE package_id = ? AND role = ?",
                (package_id, role),
            )
        return cursor.rowcount > 0

    @staticmethod
    def _from_row(row: sqlite3.Row) -> RuntimeSessionBinding:
        raw = json.loads(str(row["session_json"]))
        if not isinstance(raw, Mapping) or not str(raw.get("session_id", "")).strip():
            raise ValueError("runtime session binding has no valid session id")
        return RuntimeSessionBinding(
            package_id=str(row["package_id"]),
            role=str(row["role"]),
            candidate_id=str(row["candidate_id"]),
            runtime_id=str(row["runtime_id"]),
            model_route_id=str(row["model_route_id"]),
            target_id=str(row["target_id"]),
            runtime_backend=str(row["runtime_backend"]),
            model_provider=str(row["model_provider"]),
            model_name=str(row["model_name"]),
            target_kind=str(row["target_kind"]),
            context_epoch=str(row["context_epoch"]),
            session_ref=RuntimeSessionRef(
                runtime_id=str(raw.get("runtime_id", "")),
                candidate_id=str(raw.get("candidate_id", "")),
                session_id=str(raw.get("session_id", "")),
                backend=str(raw.get("backend", "")),
                context_epoch=str(raw.get("context_epoch", row["context_epoch"])),
            ),
            last_invocation_id=str(row["last_invocation_id"]),
            last_workspace_digest=str(row["last_workspace_digest"]),
            reuse_count=int(row["reuse_count"]),
            guarded_compaction_count=int(row["guarded_compaction_count"]),
            updated_at=str(row["updated_at"]),
        )


def previous_handoff(
    invocations: AgentInvocationStore, binding: RuntimeSessionBinding
) -> StructuredHandoff | None:
    """Rehydrate the last authoritative full handoff for delta comparison."""

    try:
        record = invocations.get(binding.last_invocation_id)
    except (KeyError, ValueError, sqlite3.Error):
        return None
    raw = record.handoff
    if not raw:
        return None
    try:
        return StructuredHandoff(**raw)
    except (TypeError, ValueError, KeyError):
        return None
