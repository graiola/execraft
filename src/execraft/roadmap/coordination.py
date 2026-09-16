"""Crash-safe coordination for canonical Roadmap / Project Execution mutations.

Roadmap v2 owns view/layout metadata while ``PROJECT_EXECUTION.yaml`` owns
Phase/Gate/Milestone business data.  A few legitimate GUI gestures (for
example, moving a canonical Gate in both time and vertical Roadmap order) touch
both durable documents.  This module closes the crash window between those
writes without merging the two domains or pretending the filesystem provides a
multi-file transaction.

The coordinator records an exact intent before either write.  Recovery only
rolls a side forward when its revision and content fingerprint still match the
recorded *before* image.  A divergent side is never overwritten automatically.
"""

from __future__ import annotations

import json
import os
import stat
import uuid
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from execraft.persistence import FileLock, LockLevel, atomic_write_json
from execraft.persistence.atomic import fsync_directory
from execraft.project import ProjectDescriptor, validate_project_id
from execraft.project_execution.errors import ProjectExecutionConflictError, ProjectExecutionError
from execraft.project_execution.models import ProjectExecutionDefinition
from execraft.project_execution.repository import ProjectExecutionRepository

from .coordination_diagnostics import (
    CoordinationDocumentStatus,
    RoadmapCoordinationStatus,
    build_forensic_record,
    content_digest,
    forensic_comparison,
)
from .models import Roadmap, RoadmapConflictError, RoadmapError, validate_roadmap_id
from .repository import RoadmapRepository

_COORDINATION_SCHEMA_VERSION = 1


class RoadmapCoordinationError(RoadmapError):
    """Base error for durable cross-domain Roadmap coordination."""


class RoadmapCoordinationConflictError(RoadmapCoordinationError):
    """Raised when recovery would have to overwrite an unrecorded mutation."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class CanonicalRoadmapIntent:
    """One replayable cross-domain mutation.

    The desired mappings intentionally retain their original revision field.
    Repositories own the actual revision increment.  Recovery therefore uses
    the explicit expected/result revisions plus content digests instead of
    trusting the embedded mapping revision.
    """

    operation_id: str
    project_id: str
    roadmap_id: str
    operation: str
    expected_roadmap_revision: int
    expected_project_execution_revision: int
    before_roadmap_digest: str
    before_project_execution_digest: str
    desired_roadmap: Mapping[str, Any]
    desired_project_execution: Mapping[str, Any]
    forensic: Mapping[str, Any] = field(default_factory=dict)
    phase: str = "prepared"
    roadmap_revision: int = 0
    project_execution_revision: int = 0
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        if not self.operation_id.strip():
            raise RoadmapCoordinationError("coordination operation_id is required")
        validate_project_id(self.project_id)
        validate_roadmap_id(self.roadmap_id)
        if not self.operation.strip():
            raise RoadmapCoordinationError("coordination operation name is required")
        if self.expected_roadmap_revision < 1:
            raise RoadmapCoordinationError("coordination Roadmap revision must be positive")
        if self.expected_project_execution_revision < 1:
            raise RoadmapCoordinationError(
                "coordination Project Execution revision must be positive"
            )
        if self.phase not in {
            "prepared",
            "project_execution_applied",
            "roadmap_applied",
            "complete",
            "aborted",
        }:
            raise RoadmapCoordinationError(
                f"unsupported coordination phase: {self.phase!r}"
            )
        if not isinstance(self.desired_roadmap, Mapping) or not isinstance(
            self.desired_project_execution, Mapping
        ):
            raise RoadmapCoordinationError("coordination desired documents must be mappings")
        if not isinstance(self.forensic, Mapping):
            raise RoadmapCoordinationError("coordination forensic metadata must be a mapping")

    @property
    def desired_roadmap_digest(self) -> str:
        return content_digest(self.desired_roadmap)

    @property
    def desired_project_execution_digest(self) -> str:
        return content_digest(self.desired_project_execution)

    def as_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": _COORDINATION_SCHEMA_VERSION,
            "operation_id": self.operation_id,
            "project_id": self.project_id,
            "roadmap_id": self.roadmap_id,
            "operation": self.operation,
            "expected_roadmap_revision": self.expected_roadmap_revision,
            "expected_project_execution_revision": self.expected_project_execution_revision,
            "before_roadmap_digest": self.before_roadmap_digest,
            "before_project_execution_digest": self.before_project_execution_digest,
            "desired_roadmap": dict(self.desired_roadmap),
            "desired_project_execution": dict(self.desired_project_execution),
            "forensic": dict(self.forensic),
            "phase": self.phase,
            "roadmap_revision": self.roadmap_revision,
            "project_execution_revision": self.project_execution_revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CanonicalRoadmapIntent":
        if not isinstance(raw, Mapping):
            raise RoadmapCoordinationError("coordination intent must be a mapping")
        if int(raw.get("schema_version", 0)) != _COORDINATION_SCHEMA_VERSION:
            raise RoadmapCoordinationError("unsupported coordination intent schema")
        desired_roadmap = raw.get("desired_roadmap")
        desired_execution = raw.get("desired_project_execution")
        if not isinstance(desired_roadmap, Mapping) or not isinstance(
            desired_execution, Mapping
        ):
            raise RoadmapCoordinationError("coordination desired documents are invalid")
        return cls(
            operation_id=str(raw.get("operation_id", "")),
            project_id=str(raw.get("project_id", "")),
            roadmap_id=str(raw.get("roadmap_id", "")),
            operation=str(raw.get("operation", "")),
            expected_roadmap_revision=int(raw.get("expected_roadmap_revision", 0)),
            expected_project_execution_revision=int(
                raw.get("expected_project_execution_revision", 0)
            ),
            before_roadmap_digest=str(raw.get("before_roadmap_digest", "")),
            before_project_execution_digest=str(
                raw.get("before_project_execution_digest", "")
            ),
            desired_roadmap=dict(desired_roadmap),
            desired_project_execution=dict(desired_execution),
            forensic=(dict(raw.get("forensic", {})) if isinstance(raw.get("forensic", {}), Mapping) else {}),
            phase=str(raw.get("phase", "prepared")),
            roadmap_revision=int(raw.get("roadmap_revision", 0)),
            project_execution_revision=int(raw.get("project_execution_revision", 0)),
            created_at=str(raw.get("created_at", "")),
            updated_at=str(raw.get("updated_at", "")),
        )


class RoadmapCoordinationStore:
    """Durable pending intent plus append-only audit journal for one project."""

    def __init__(self, state_root: Path, project_id: str) -> None:
        safe_project_id = validate_project_id(project_id)
        self.directory = (
            Path(state_root).expanduser().resolve()
            / "roadmap-coordination"
            / safe_project_id
        )
        self.pending_path = self.directory / "pending.json"
        self.journal_path = self.directory / "journal.jsonl"

    def load_pending(self) -> CanonicalRoadmapIntent | None:
        if not self.pending_path.exists():
            return None
        self._assert_regular(self.pending_path)
        try:
            raw = json.loads(self.pending_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RoadmapCoordinationError(
                f"cannot read Roadmap coordination intent: {exc}"
            ) from exc
        if not isinstance(raw, Mapping):
            raise RoadmapCoordinationError("Roadmap coordination intent must be a mapping")
        return CanonicalRoadmapIntent.from_mapping(raw)

    def save_pending(self, intent: CanonicalRoadmapIntent) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self._assert_regular(self.pending_path, allow_missing=True)
        atomic_write_json(
            self.pending_path,
            intent.as_mapping(),
            indent=2,
            ensure_ascii=False,
            trailing_newline=True,
            mode=0o600,
        )

    def complete(self, intent: CanonicalRoadmapIntent) -> None:
        self._finalize(intent, phase="complete")

    def abort(self, intent: CanonicalRoadmapIntent, *, reason: str) -> None:
        """Clear an intent only when no coordinated side effect was applied."""

        self._finalize(intent, phase="aborted", reason=reason)

    def _finalize(
        self, intent: CanonicalRoadmapIntent, *, phase: str, reason: str = ""
    ) -> None:
        terminal = replace(intent, phase=phase, updated_at=_utc_now())
        self.save_pending(terminal)
        self._append_journal(terminal, reason=reason)
        try:
            self.pending_path.unlink()
        except FileNotFoundError:
            return
        fsync_directory(self.directory)

    def _append_journal(
        self, intent: CanonicalRoadmapIntent, *, reason: str = ""
    ) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self._assert_regular(self.journal_path, allow_missing=True)
        encoded = (
            json.dumps(
                {
                    "schema_version": _COORDINATION_SCHEMA_VERSION,
                    "operation_id": intent.operation_id,
                    "project_id": intent.project_id,
                    "roadmap_id": intent.roadmap_id,
                    "operation": intent.operation,
                    "phase": intent.phase,
                    "reason": reason,
                    "roadmap_revision": intent.roadmap_revision,
                    "project_execution_revision": intent.project_execution_revision,
                    "created_at": intent.created_at,
                    "updated_at": intent.updated_at,
                    "timestamp": _utc_now(),
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8")
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self.journal_path, flags, 0o600)
        try:
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        fsync_directory(self.directory)

    def read_journal(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Return the newest bounded terminal coordination records, oldest first."""

        bounded = max(1, min(int(limit), 100))
        if not self.journal_path.exists():
            return []
        self._assert_regular(self.journal_path)
        rows: deque[dict[str, Any]] = deque(maxlen=bounded)
        try:
            with self.journal_path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    raw = json.loads(line)
                    if not isinstance(raw, Mapping):
                        raise ValueError(f"entry {line_number} is not a mapping")
                    rows.append(dict(raw))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise RoadmapCoordinationError(
                f"cannot read Roadmap coordination journal: {exc}"
            ) from exc
        return list(rows)

    @staticmethod
    def _assert_regular(path: Path, *, allow_missing: bool = False) -> None:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            if allow_missing:
                return
            raise
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise RoadmapCoordinationError(f"unsafe Roadmap coordination path: {path}")


class RoadmapCanonicalCoordinator:
    """Serialize, persist, apply, and recover legitimate two-document edits."""

    def __init__(self, *, state_root: Path, lock_timeout: float = 5.0) -> None:
        self.state_root = Path(state_root).expanduser().resolve()
        self.lock_timeout = lock_timeout

    def coordinate(
        self,
        *,
        project: ProjectDescriptor,
        roadmaps: RoadmapRepository,
        current_roadmap: Roadmap,
        desired_roadmap: Roadmap,
        current_project_execution: ProjectExecutionDefinition,
        desired_project_execution: ProjectExecutionDefinition,
        operation: str,
    ) -> tuple[Roadmap, ProjectExecutionDefinition]:
        """Apply one cross-domain mutation with durable replay information."""

        with self._lock(project.id):
            self._reconcile_locked(project=project, roadmaps=roadmaps)
            # Re-load after reconciliation so the preflight observes the exact
            # current durable state, not an earlier service snapshot.
            durable_roadmap = roadmaps.load(current_roadmap.id)
            execution_repository = ProjectExecutionRepository(project.directory)
            durable_execution = execution_repository.load()
            if durable_roadmap.revision != current_roadmap.revision:
                raise RoadmapConflictError(
                    "roadmap changed since it was loaded; refresh before saving "
                    f"(expected revision {current_roadmap.revision}, "
                    f"current {durable_roadmap.revision})"
                )
            if durable_execution.revision != current_project_execution.revision:
                raise ProjectExecutionConflictError(
                    "Project Execution changed since the Roadmap projection was loaded; "
                    "refresh before editing canonical project assets "
                    f"(expected revision {current_project_execution.revision}, "
                    f"current {durable_execution.revision})"
                )
            if content_digest(durable_roadmap.as_mapping()) != content_digest(
                current_roadmap.as_mapping()
            ):
                raise RoadmapConflictError(
                    "roadmap content changed without the expected revision; refresh before saving"
                )
            if content_digest(durable_execution.as_mapping()) != content_digest(
                current_project_execution.as_mapping()
            ):
                raise RoadmapCoordinationConflictError(
                    "Project Execution content changed without the expected revision"
                )

            now = _utc_now()
            intent = CanonicalRoadmapIntent(
                operation_id=uuid.uuid4().hex,
                project_id=project.id,
                roadmap_id=current_roadmap.id,
                operation=operation,
                expected_roadmap_revision=current_roadmap.revision,
                expected_project_execution_revision=current_project_execution.revision,
                before_roadmap_digest=content_digest(current_roadmap.as_mapping()),
                before_project_execution_digest=content_digest(
                    current_project_execution.as_mapping()
                ),
                desired_roadmap=desired_roadmap.as_mapping(),
                desired_project_execution=desired_project_execution.as_mapping(),
                forensic=build_forensic_record(
                    before_roadmap=current_roadmap.as_mapping(),
                    desired_roadmap=desired_roadmap.as_mapping(),
                    before_project_execution=current_project_execution.as_mapping(),
                    desired_project_execution=desired_project_execution.as_mapping(),
                ),
                created_at=now,
                updated_at=now,
            )
            store = RoadmapCoordinationStore(self.state_root, project.id)
            store.save_pending(intent)
            return self._apply_locked(
                project=project,
                roadmaps=roadmaps,
                intent=intent,
                store=store,
            )

    def reconcile(self, *, project: ProjectDescriptor, roadmaps: RoadmapRepository) -> bool:
        """Recover a pending canonical Roadmap mutation if one exists."""

        with self._lock(project.id):
            return self._reconcile_locked(project=project, roadmaps=roadmaps)

    def pending(self, project_id: str) -> CanonicalRoadmapIntent | None:
        return RoadmapCoordinationStore(self.state_root, project_id).load_pending()


    def status(self, *, project: ProjectDescriptor, roadmaps: RoadmapRepository) -> RoadmapCoordinationStatus:
        """Inspect a pending intent without mutating either durable domain."""

        with self._lock(project.id):
            return self._status_locked(project=project, roadmaps=roadmaps)

    def reconcile_if_safe(
        self, *, project: ProjectDescriptor, roadmaps: RoadmapRepository
    ) -> RoadmapCoordinationStatus:
        """Finish only a provably safe pending state; leave divergence untouched."""
        status = self.status(project=project, roadmaps=roadmaps)
        if status.pending and status.automatic_action_available:
            for action in ("finalize_terminal", "accept_applied", "retry_roll_forward"):
                if action in status.safe_actions:
                    return self.resolve(project=project, roadmaps=roadmaps, action=action)
        return status
    def resolve(
        self,
        *,
        project: ProjectDescriptor,
        roadmaps: RoadmapRepository,
        action: str,
    ) -> RoadmapCoordinationStatus:
        """Perform one explicitly safe operator resolution action.

        No action accepts arbitrary replacement content.  The only writable
        recovery operation is replay of the exact desired documents already
        captured by the durable intent.
        """

        normalized = str(action or "").strip().lower()
        with self._lock(project.id):
            status = self._status_locked(project=project, roadmaps=roadmaps)
            if not status.pending:
                raise RoadmapCoordinationError("no pending Roadmap coordination intent")
            if normalized not in status.safe_actions:
                allowed = ", ".join(status.safe_actions) or "none"
                raise RoadmapCoordinationConflictError(
                    f"coordination action {normalized!r} is not safe for the current "
                    f"durable state; available actions: {allowed}"
                )
            store = RoadmapCoordinationStore(self.state_root, project.id)
            intent = store.load_pending()
            if intent is None:
                return RoadmapCoordinationStatus(pending=False, project_id=project.id)
            if normalized == "retry_roll_forward":
                self._apply_locked(
                    project=project,
                    roadmaps=roadmaps,
                    intent=intent,
                    store=store,
                )
            elif normalized == "accept_applied":
                store.complete(intent)
            elif normalized == "finalize_terminal":
                if intent.phase == "complete":
                    store.complete(intent)
                elif intent.phase == "aborted":
                    store.abort(intent, reason="recovered terminal aborted intent")
                else:
                    raise RoadmapCoordinationConflictError(
                        "coordination intent is no longer terminal; refresh status"
                    )
            elif normalized == "abort":
                store.abort(
                    intent,
                    reason="operator aborted coordination before either durable side changed",
                )
            else:  # defensive: safe_actions is the sole action source above.
                raise RoadmapCoordinationError(
                    f"unsupported coordination action: {normalized!r}"
                )
            return self._status_locked(project=project, roadmaps=roadmaps)

    def _status_locked(self, *, project: ProjectDescriptor, roadmaps: RoadmapRepository) -> RoadmapCoordinationStatus:
        store = RoadmapCoordinationStore(self.state_root, project.id)
        intent = store.load_pending()
        if intent is None:
            return RoadmapCoordinationStatus(pending=False, project_id=project.id)
        if intent.project_id != project.id:
            raise RoadmapCoordinationConflictError(
                "pending Roadmap coordination intent belongs to another project"
            )

        roadmap_status = self._roadmap_status(roadmaps, intent)
        execution_status = self._execution_status(project, intent)
        states = {roadmap_status.state, execution_status.state}
        desired_states = {"applied", "converged"}
        if intent.phase in {"complete", "aborted"}:
            safe_actions = ("finalize_terminal",)
            message = (
                f"The coordination intent is already terminal ({intent.phase}) and only "
                "durable cleanup remains. No Roadmap or Project Execution write is required."
            )
        elif {roadmap_status.state, execution_status.state}.issubset(desired_states):
            safe_actions = ("accept_applied",)
            message = (
                "Both durable documents match the recorded desired content. "
                "The pending intent can be finalized without another domain write"
                + (" after recognizing later revisioned convergence." if "converged" in states else ".")
            )
        elif states == {"before"}:
            safe_actions = ("retry_roll_forward", "abort")
            message = (
                "Neither durable document contains a coordinated side effect. The exact "
                "recorded mutation may be replayed, or the untouched intent may be aborted."
            )
        elif "divergent" not in states:
            safe_actions = ("retry_roll_forward",)
            message = (
                "One durable document contains the recorded desired result and the other "
                "still matches its exact before image. The intent can be safely rolled forward."
            )
        else:
            safe_actions = ()
            message = (
                "At least one durable document changed outside the recorded coordination "
                "intent. Automatic overwrite, rollback, and force resolution are disabled."
            )

        try:
            current_roadmap = roadmaps.load(intent.roadmap_id).as_mapping()
        except (RoadmapError, OSError, ValueError):
            current_roadmap = None
        try:
            current_execution = ProjectExecutionRepository(project.directory).load().as_mapping()
        except (ProjectExecutionError, OSError, ValueError):
            current_execution = None
        forensics = forensic_comparison(
            record=intent.forensic,
            current_roadmap=current_roadmap,
            current_project_execution=current_execution,
            roadmap_before_revision=intent.expected_roadmap_revision,
            roadmap_before_digest=intent.before_roadmap_digest,
            roadmap_desired_revision=intent.expected_roadmap_revision + 1,
            roadmap_desired_digest=intent.desired_roadmap_digest,
            project_before_revision=intent.expected_project_execution_revision,
            project_before_digest=intent.before_project_execution_digest,
            project_desired_revision=intent.expected_project_execution_revision + 1,
            project_desired_digest=intent.desired_project_execution_digest,
        )

        return RoadmapCoordinationStatus(
            pending=True,
            project_id=project.id,
            roadmap_id=intent.roadmap_id,
            operation_id=intent.operation_id,
            operation=intent.operation,
            phase=intent.phase,
            created_at=intent.created_at,
            updated_at=intent.updated_at,
            roadmap=roadmap_status,
            project_execution=execution_status,
            safe_actions=safe_actions,
            message=message,
            forensics=forensics,
        )

    def _roadmap_status(
        self, roadmaps: RoadmapRepository, intent: CanonicalRoadmapIntent
    ) -> CoordinationDocumentStatus:
        try:
            current = roadmaps.load(intent.roadmap_id)
            current_revision = current.revision
            state = self._document_state(
                revision=current.revision,
                digest=content_digest(current.as_mapping()),
                before_revision=intent.expected_roadmap_revision,
                before_digest=intent.before_roadmap_digest,
                desired_digest=intent.desired_roadmap_digest,
            )
        except (RoadmapError, OSError, ValueError):
            current_revision = 0
            state = "divergent"
        return CoordinationDocumentStatus(
            state=state,
            expected_revision=intent.expected_roadmap_revision,
            current_revision=current_revision,
            desired_revision=intent.expected_roadmap_revision + 1,
            recorded_result_revision=intent.roadmap_revision,
        )

    def _execution_status(
        self, project: ProjectDescriptor, intent: CanonicalRoadmapIntent
    ) -> CoordinationDocumentStatus:
        try:
            current = ProjectExecutionRepository(project.directory).load()
            current_revision = current.revision
            state = self._document_state(
                revision=current.revision,
                digest=content_digest(current.as_mapping()),
                before_revision=intent.expected_project_execution_revision,
                before_digest=intent.before_project_execution_digest,
                desired_digest=intent.desired_project_execution_digest,
            )
        except (ProjectExecutionError, OSError, ValueError):
            current_revision = 0
            state = "divergent"
        return CoordinationDocumentStatus(
            state=state,
            expected_revision=intent.expected_project_execution_revision,
            current_revision=current_revision,
            desired_revision=intent.expected_project_execution_revision + 1,
            recorded_result_revision=intent.project_execution_revision,
        )

    def _reconcile_locked(
        self, *, project: ProjectDescriptor, roadmaps: RoadmapRepository
    ) -> bool:
        store = RoadmapCoordinationStore(self.state_root, project.id)
        intent = store.load_pending()
        if intent is None:
            return False
        if intent.project_id != project.id:
            raise RoadmapCoordinationConflictError(
                "pending Roadmap coordination intent belongs to another project"
            )
        if intent.phase == "complete":
            store.complete(intent)
            return True
        if intent.phase == "aborted":
            store.abort(intent, reason="recovered terminal aborted intent")
            return True
        self._apply_locked(project=project, roadmaps=roadmaps, intent=intent, store=store)
        return True

    def _apply_locked(
        self,
        *,
        project: ProjectDescriptor,
        roadmaps: RoadmapRepository,
        intent: CanonicalRoadmapIntent,
        store: RoadmapCoordinationStore,
    ) -> tuple[Roadmap, ProjectExecutionDefinition]:
        execution_repository = ProjectExecutionRepository(project.directory)
        current_execution = execution_repository.load()
        current_roadmap = roadmaps.load(intent.roadmap_id)
        execution_state = self._document_state(
            revision=current_execution.revision,
            digest=content_digest(current_execution.as_mapping()),
            before_revision=intent.expected_project_execution_revision,
            before_digest=intent.before_project_execution_digest,
            desired_digest=intent.desired_project_execution_digest,
        )
        roadmap_state = self._document_state(
            revision=current_roadmap.revision,
            digest=content_digest(current_roadmap.as_mapping()),
            before_revision=intent.expected_roadmap_revision,
            before_digest=intent.before_roadmap_digest,
            desired_digest=intent.desired_roadmap_digest,
        )
        if "divergent" in {execution_state, roadmap_state}:
            raise RoadmapCoordinationConflictError(
                "cannot automatically reconcile canonical Roadmap mutation "
                f"{intent.operation_id}: Project Execution is {execution_state}, "
                f"Roadmap is {roadmap_state}; refresh and resolve the pending intent "
                f"at {store.pending_path}"
            )

        saved_execution = current_execution
        if execution_state == "before":
            desired_execution = ProjectExecutionDefinition.from_mapping(
                intent.desired_project_execution
            )
            try:
                saved_execution = execution_repository.save(
                    desired_execution,
                    expected_revision=intent.expected_project_execution_revision,
                )
            except ProjectExecutionConflictError as exc:
                # The canonical write did not occur.  If Roadmap is still the
                # exact before image, this transaction has no side effects and
                # can be safely abandoned instead of poisoning future reads.
                roadmap_probe = roadmaps.load(intent.roadmap_id)
                if self._document_state(
                    revision=roadmap_probe.revision,
                    digest=content_digest(roadmap_probe.as_mapping()),
                    before_revision=intent.expected_roadmap_revision,
                    before_digest=intent.before_roadmap_digest,
                    desired_digest=intent.desired_roadmap_digest,
                ) == "before":
                    store.abort(intent, reason=str(exc))
                raise
            intent = replace(
                intent,
                phase="project_execution_applied",
                project_execution_revision=saved_execution.revision,
                updated_at=_utc_now(),
            )
            store.save_pending(intent)
        else:
            intent = replace(
                intent,
                phase="project_execution_applied",
                project_execution_revision=current_execution.revision,
                updated_at=_utc_now(),
            )
            store.save_pending(intent)

        # Re-read Roadmap state after the canonical write.  A separate Roadmap
        # client may have raced despite the project coordinator because normal
        # Roadmap-only edits do not need this cross-domain lock.  We only roll
        # forward from the exact recorded before image.
        current_roadmap = roadmaps.load(intent.roadmap_id)
        roadmap_state = self._document_state(
            revision=current_roadmap.revision,
            digest=content_digest(current_roadmap.as_mapping()),
            before_revision=intent.expected_roadmap_revision,
            before_digest=intent.before_roadmap_digest,
            desired_digest=intent.desired_roadmap_digest,
        )
        if roadmap_state == "divergent":
            raise RoadmapCoordinationConflictError(
                "cannot automatically reconcile canonical Roadmap mutation after "
                "Project Execution was applied because the Roadmap changed; "
                f"pending intent: {store.pending_path}"
            )
        saved_roadmap = current_roadmap
        if roadmap_state == "before":
            desired_roadmap = Roadmap.from_mapping(intent.desired_roadmap)
            saved_roadmap = roadmaps.save(
                desired_roadmap,
                expected_revision=intent.expected_roadmap_revision,
            )
        intent = replace(
            intent,
            phase="roadmap_applied",
            roadmap_revision=saved_roadmap.revision,
            project_execution_revision=saved_execution.revision,
            updated_at=_utc_now(),
        )
        store.save_pending(intent)
        store.complete(intent)
        return saved_roadmap, saved_execution

    @staticmethod
    def _document_state(
        *,
        revision: int,
        digest: str,
        before_revision: int,
        before_digest: str,
        desired_digest: str,
    ) -> str:
        if revision == before_revision and digest == before_digest:
            return "before"
        if digest == desired_digest and revision >= before_revision + 1:
            return "applied" if revision == before_revision + 1 else "converged"
        return "divergent"

    def _lock(self, project_id: str) -> FileLock:
        safe_project_id = validate_project_id(project_id)
        return FileLock(
            self.state_root / "roadmap-coordination" / f"{safe_project_id}.lock",
            level=LockLevel.PROJECT_COORDINATOR,
            timeout=self.lock_timeout,
        )


__all__ = [
    "CanonicalRoadmapIntent",
    "CoordinationDocumentStatus",
    "RoadmapCoordinationStatus",
    "RoadmapCanonicalCoordinator",
    "RoadmapCoordinationConflictError",
    "RoadmapCoordinationError",
    "RoadmapCoordinationStore",
]
