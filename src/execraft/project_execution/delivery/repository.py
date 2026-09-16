"""Crash-safe persistence for provider-neutral Project delivery state."""

from __future__ import annotations

import json
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from execraft.persistence.atomic import atomic_write_json
from execraft.persistence.locks import FileLock, LockLevel
from execraft.project import validate_project_id

from ..errors import ProjectExecutionConflictError, ProjectExecutionError
from .models import DeliveryCandidate, DeliveryOperation, DeliveryOperationState

DELIVERY_RUNTIME_SCHEMA_VERSION = 1


@dataclass
class DeliveryRuntimeState:
    """Durable candidates and external delivery attempts for one Project."""

    project_id: str
    candidates: dict[str, dict[str, Any]] = field(default_factory=dict)
    operations: dict[str, dict[str, Any]] = field(default_factory=dict)
    next_sequence: int = 1
    schema_version: int = DELIVERY_RUNTIME_SCHEMA_VERSION

    def __post_init__(self) -> None:
        try:
            self.project_id = validate_project_id(self.project_id)
        except Exception as exc:
            raise ProjectExecutionError(str(exc)) from exc
        if self.schema_version != DELIVERY_RUNTIME_SCHEMA_VERSION:
            raise ProjectExecutionError(
                f"unsupported delivery runtime schema_version: {self.schema_version!r}"
            )
        if int(self.next_sequence) < 1:
            raise ProjectExecutionError("delivery next_sequence must be positive")
        self.next_sequence = int(self.next_sequence)

    def as_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "next_sequence": self.next_sequence,
            "candidates": self.candidates,
            "operations": self.operations,
        }

    @classmethod
    def from_mapping(cls, raw: object) -> "DeliveryRuntimeState":
        if not isinstance(raw, Mapping):
            raise ProjectExecutionError("delivery runtime state must be a mapping")
        candidates = raw.get("candidates") or {}
        operations = raw.get("operations") or {}
        if not isinstance(candidates, Mapping) or not isinstance(operations, Mapping):
            raise ProjectExecutionError(
                "delivery runtime candidates/operations must be mappings"
            )
        invalid_candidates = [
            str(key) for key, value in candidates.items() if not isinstance(value, Mapping)
        ]
        invalid_operations = [
            str(key) for key, value in operations.items() if not isinstance(value, Mapping)
        ]
        if invalid_candidates or invalid_operations:
            raise ProjectExecutionError(
                "delivery runtime contains non-mapping candidate/operation records"
            )
        return cls(
            schema_version=int(raw.get("schema_version", 0)),
            project_id=str(raw.get("project_id", "")),
            next_sequence=int(raw.get("next_sequence", 1)),
            candidates={str(key): dict(value) for key, value in candidates.items()},
            operations={str(key): dict(value) for key, value in operations.items()},
        )


class DeliveryRepository:
    """Atomic repository for delivery candidates and operation intents.

    External provider calls never run while the repository lock is held.  A
    pending operation is first made durable, then the side effect happens, then
    the same operation is resolved. This is the delivery equivalent of the
    Project Task start-intent protocol.
    """

    def __init__(
        self,
        state_root: Path,
        project_id: str,
        *,
        lock_timeout: float = 5.0,
    ) -> None:
        safe_project_id = validate_project_id(project_id)
        self.directory = (
            Path(state_root).expanduser().resolve()
            / "project-execution"
            / safe_project_id
        )
        self.path = self.directory / "delivery.json"
        self.lock_path = self.directory / "delivery.lock"
        self.project_id = safe_project_id
        self.lock_timeout = lock_timeout

    def load(self) -> DeliveryRuntimeState:
        with self._lock(exclusive=False):
            return self._load_unlocked()

    def get_candidate(self, candidate_id: str) -> DeliveryCandidate | None:
        state = self.load()
        raw = state.candidates.get(candidate_id)
        return DeliveryCandidate.from_mapping(raw) if raw is not None else None

    def get_operation(self, operation_id: str) -> DeliveryOperation | None:
        state = self.load()
        raw = state.operations.get(operation_id)
        return DeliveryOperation.from_mapping(raw) if raw is not None else None

    def candidates(self) -> tuple[DeliveryCandidate, ...]:
        state = self.load()
        return tuple(
            DeliveryCandidate.from_mapping(raw)
            for _, raw in sorted(state.candidates.items())
        )

    def operations(self) -> tuple[DeliveryOperation, ...]:
        state = self.load()
        rows = [DeliveryOperation.from_mapping(raw) for raw in state.operations.values()]
        return tuple(sorted(rows, key=lambda item: item.sequence))

    def record_candidate(
        self,
        candidate: DeliveryCandidate,
    ) -> tuple[DeliveryCandidate, bool]:
        if candidate.project_id != self.project_id:
            raise ProjectExecutionError(
                "delivery candidate project identity does not match repository"
            )
        with self._lock():
            state = self._load_unlocked()
            existing = state.candidates.get(candidate.candidate_id)
            if existing is not None:
                persisted = DeliveryCandidate.from_mapping(existing)
                if (
                    persisted.project_id != candidate.project_id
                    or persisted.milestone_id != candidate.milestone_id
                    or persisted.baseline_digest != candidate.baseline_digest
                    or persisted.baseline_json != candidate.baseline_json
                ):
                    raise ProjectExecutionConflictError(
                        f"delivery candidate identity collision: {candidate.candidate_id}"
                    )
                return persisted, False
            state.candidates[candidate.candidate_id] = candidate.as_mapping()
            self._write_unlocked(state)
            return candidate, True

    def begin_operation(self, operation: DeliveryOperation) -> DeliveryOperation:
        """Persist a side-effect intent, rejecting duplicate unresolved attempts."""

        if operation.state != DeliveryOperationState.PENDING:
            raise ProjectExecutionError("new delivery operation must begin pending")
        with self._lock():
            state = self._load_unlocked()
            if operation.operation_id in state.operations:
                raise ProjectExecutionConflictError(
                    f"delivery operation already exists: {operation.operation_id}"
                )
            if operation.candidate_id not in state.candidates:
                raise ProjectExecutionError(
                    f"delivery candidate not found: {operation.candidate_id}"
                )
            for raw in state.operations.values():
                existing = DeliveryOperation.from_mapping(raw)
                if existing.key == operation.key and not existing.state.terminal:
                    raise ProjectExecutionConflictError(
                        "delivery already has an unresolved operation for "
                        f"candidate {operation.candidate_id}, target "
                        f"{operation.target.target_id}"
                    )
            persisted = DeliveryOperation(
                operation_id=operation.operation_id,
                sequence=state.next_sequence,
                candidate_id=operation.candidate_id,
                target=operation.target,
                provider_id=operation.provider_id,
                state=operation.state,
                requested_at=operation.requested_at,
                completed_at=operation.completed_at,
                result=operation.result,
                diagnostic=operation.diagnostic,
            )
            state.next_sequence += 1
            state.operations[persisted.operation_id] = persisted.as_mapping()
            self._write_unlocked(state)
            return persisted

    def update_operation(self, operation: DeliveryOperation) -> DeliveryOperation:
        with self._lock():
            state = self._load_unlocked()
            raw = state.operations.get(operation.operation_id)
            if raw is None:
                raise ProjectExecutionError(
                    f"delivery operation not found: {operation.operation_id}"
                )
            current = DeliveryOperation.from_mapping(raw)
            if (
                current.key != operation.key
                or current.sequence != operation.sequence
            ):
                raise ProjectExecutionError("delivery operation identity cannot change")
            if current.state.terminal and current.as_mapping() != operation.as_mapping():
                raise ProjectExecutionConflictError(
                    f"terminal delivery operation is immutable: {operation.operation_id}"
                )
            allowed = {
                DeliveryOperationState.PENDING: {
                    DeliveryOperationState.PENDING,
                    DeliveryOperationState.UNCERTAIN,
                    DeliveryOperationState.SUCCEEDED,
                    DeliveryOperationState.FAILED,
                },
                DeliveryOperationState.UNCERTAIN: {
                    DeliveryOperationState.UNCERTAIN,
                    DeliveryOperationState.SUCCEEDED,
                    DeliveryOperationState.FAILED,
                },
                DeliveryOperationState.SUCCEEDED: {DeliveryOperationState.SUCCEEDED},
                DeliveryOperationState.FAILED: {DeliveryOperationState.FAILED},
            }
            if operation.state not in allowed[current.state]:
                raise ProjectExecutionConflictError(
                    f"invalid delivery operation transition: {current.state.value} -> "
                    f"{operation.state.value}"
                )
            state.operations[operation.operation_id] = operation.as_mapping()
            self._write_unlocked(state)
            return operation

    def latest_operation(
        self,
        *,
        candidate_id: str,
        target_id: str,
        provider_id: str,
    ) -> DeliveryOperation | None:
        matches = [
            operation
            for operation in self.operations()
            if operation.key == (candidate_id, target_id, provider_id)
        ]
        return matches[-1] if matches else None

    def _load_unlocked(self) -> DeliveryRuntimeState:
        if not self.path.exists():
            return DeliveryRuntimeState(self.project_id)
        metadata = self.path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ProjectExecutionError(f"delivery runtime path is unsafe: {self.path}")
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            state = DeliveryRuntimeState.from_mapping(raw)
        except (OSError, TypeError, ValueError) as exc:
            raise ProjectExecutionError(f"cannot read delivery runtime state: {exc}") from exc
        if state.project_id != self.project_id:
            raise ProjectExecutionError(
                "delivery runtime project identity does not match storage path"
            )
        # Parse every record eagerly so corruption cannot hide until a later action.
        for key, raw_candidate in state.candidates.items():
            candidate = DeliveryCandidate.from_mapping(raw_candidate)
            if key != candidate.candidate_id:
                raise ProjectExecutionError(
                    "delivery candidate storage key does not match identity"
                )
        maximum_sequence = 0
        for key, raw_operation in state.operations.items():
            operation = DeliveryOperation.from_mapping(raw_operation)
            if key != operation.operation_id:
                raise ProjectExecutionError(
                    "delivery operation storage key does not match identity"
                )
            maximum_sequence = max(maximum_sequence, operation.sequence)
        if state.next_sequence <= maximum_sequence:
            raise ProjectExecutionError("delivery next_sequence must exceed persisted operations")
        return state

    def _write_unlocked(self, state: DeliveryRuntimeState) -> None:
        atomic_write_json(self.path, state.as_mapping(), mode=0o600)

    def _lock(self, *, exclusive: bool = True) -> FileLock:
        return FileLock(
            self.lock_path,
            level=LockLevel.RECORD,
            exclusive=exclusive,
            timeout=self.lock_timeout,
        )
