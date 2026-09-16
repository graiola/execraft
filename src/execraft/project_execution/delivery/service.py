"""Provider-neutral Project delivery application service."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import uuid

from ..events import ProjectEventJournal
from ..models import DeliveryPolicy, validate_asset_id
from ..repository import ProjectExecutionRepository
from ..runtime_repository import ProjectRuntimeRepository
from ..errors import ProjectExecutionConflictError, ProjectExecutionError
from .models import (
    DeliveryCandidate,
    DeliveryOperation,
    DeliveryOperationState,
    DeliveryOutcome,
    DeliveryRequest,
    DeliveryResult,
    DeliveryTarget,
)
from .ports import DeliveryProvider
from .repository import DeliveryRepository


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _operation_id(candidate_id: str) -> str:
    # UUID entropy avoids collisions across independent processes while retaining
    # a readable relation to the immutable candidate in audit state.
    suffix = uuid.uuid4().hex[:16]
    prefix = candidate_id[:60].rstrip("-")
    return validate_asset_id(
        f"{prefix}-delivery-{suffix}",
        label="delivery operation id",
    )


class DeliveryUncertainError(ProjectExecutionError):
    """Raised after an adapter exception whose external side effect is unknown."""

    def __init__(self, operation: DeliveryOperation) -> None:
        self.operation = operation
        super().__init__(
            "delivery provider outcome is uncertain; reconcile the existing "
            f"operation before retrying: {operation.operation_id}"
        )


class DeliveryService:
    """Create immutable candidates and coordinate recoverable adapter calls.

    The service knows Project/Milestone identity and immutable baseline content,
    but no provider-specific destination semantics. Provider configuration and
    credentials are resolved entirely by the injected ``DeliveryProvider``.
    """

    def __init__(
        self,
        *,
        definition_repository: ProjectExecutionRepository,
        runtime_repository: ProjectRuntimeRepository,
        delivery_repository: DeliveryRepository,
        journal: ProjectEventJournal | None = None,
    ) -> None:
        self.definitions = definition_repository
        self.runtime = runtime_repository
        self.deliveries = delivery_repository
        self.journal = journal

    def prepare_candidate(self, milestone_id: str) -> DeliveryCandidate:
        """Idempotently materialize a delivery candidate from achieved baseline."""

        definition = self.definitions.load()
        milestone = definition.milestone_index.get(milestone_id)
        if milestone is None:
            raise ProjectExecutionError(f"Milestone not found: {milestone_id}")
        runtime = self.runtime.load()
        baseline = runtime.milestone_achievements.get(milestone_id)
        if baseline is None:
            raise ProjectExecutionError(
                f"Milestone {milestone_id} has not achieved an immutable baseline"
            )
        delivery = baseline.get("delivery", {})
        baseline_policy = (
            delivery.get("policy") if isinstance(delivery, dict) else None
        )
        if baseline_policy != DeliveryPolicy.CANDIDATE.value:
            raise ProjectExecutionError(
                f"Milestone {milestone_id} achievement is not a delivery candidate"
            )
        candidate = DeliveryCandidate.from_baseline(
            project_id=definition.project,
            milestone_id=milestone_id,
            baseline=baseline,
            created_at=_now(),
        )
        persisted, created = self.deliveries.record_candidate(candidate)
        if created:
            self._event(
                "project_delivery_candidate_created",
                {
                    "milestone_id": milestone_id,
                    "candidate_id": persisted.candidate_id,
                    "baseline_digest": persisted.baseline_digest,
                },
            )
        return persisted

    def candidates(self) -> tuple[DeliveryCandidate, ...]:
        """Return persisted immutable candidates for this Project."""

        return self.deliveries.candidates()

    def operations(self) -> tuple[DeliveryOperation, ...]:
        """Return delivery attempts in durable sequence order."""

        return self.deliveries.operations()

    def deliver(
        self,
        candidate_id: str,
        target: DeliveryTarget,
        provider: DeliveryProvider,
        *,
        retry_failed: bool = False,
    ) -> DeliveryOperation:
        """Deliver a candidate, reusing successful/failed attempts by default.

        An unresolved/uncertain attempt is never retried blindly. It must first
        be reconciled through the original provider adapter using the durable
        operation ID.
        """

        candidate = self._candidate(candidate_id)
        provider_id = validate_asset_id(
            provider.provider_id,
            label="delivery provider id",
        )
        latest = self.deliveries.latest_operation(
            candidate_id=candidate_id,
            target_id=target.target_id,
            provider_id=provider_id,
        )
        if latest is not None:
            if latest.state == DeliveryOperationState.SUCCEEDED:
                return latest
            if latest.state in {
                DeliveryOperationState.PENDING,
                DeliveryOperationState.UNCERTAIN,
            }:
                return self.reconcile(latest.operation_id, provider)
            if latest.state == DeliveryOperationState.FAILED and not retry_failed:
                return latest

        requested_at = _now()
        pending = DeliveryOperation(
            operation_id=_operation_id(candidate.candidate_id),
            sequence=1,  # repository assigns the durable monotonic sequence
            candidate_id=candidate.candidate_id,
            target=target,
            provider_id=provider_id,
            state=DeliveryOperationState.PENDING,
            requested_at=requested_at,
        )
        try:
            operation = self.deliveries.begin_operation(pending)
        except ProjectExecutionConflictError:
            # Another process may have won the intent race. Never issue a second
            # side effect merely because this caller observed the race late.
            concurrent = self.deliveries.latest_operation(
                candidate_id=candidate_id,
                target_id=target.target_id,
                provider_id=provider_id,
            )
            if concurrent is None:
                raise
            return concurrent
        request = DeliveryRequest(operation.operation_id, candidate, target)
        self._event(
            "project_delivery_started",
            {
                "operation_id": operation.operation_id,
                "candidate_id": candidate.candidate_id,
                "milestone_id": candidate.milestone_id,
                "target_id": target.target_id,
                "provider_id": provider_id,
            },
        )
        try:
            result = provider.deliver(request)
            if not isinstance(result, DeliveryResult):
                raise TypeError("delivery provider returned an invalid result")
        except Exception as exc:
            uncertain = self._mark_uncertain(
                operation,
                diagnostic=f"{type(exc).__name__}: {exc}",
            )
            raise DeliveryUncertainError(uncertain) from exc
        return self._resolve(operation, result)

    def reconcile(
        self,
        operation_id: str,
        provider: DeliveryProvider,
    ) -> DeliveryOperation:
        """Observe a prior intent without issuing another external delivery."""

        operation = self._operation(operation_id)
        if operation.state.terminal:
            return operation
        provider_id = validate_asset_id(
            provider.provider_id,
            label="delivery provider id",
        )
        if provider_id != operation.provider_id:
            raise ProjectExecutionError(
                "delivery operation must be reconciled by its original provider"
            )
        candidate = self._candidate(operation.candidate_id)
        request = DeliveryRequest(operation.operation_id, candidate, operation.target)
        try:
            result = provider.reconcile(request)
            if result is not None and not isinstance(result, DeliveryResult):
                raise TypeError(
                    "delivery provider returned an invalid reconciliation result"
                )
        except Exception as exc:
            return self._mark_uncertain(
                operation,
                diagnostic=f"{type(exc).__name__}: {exc}",
            )
        if result is None:
            return self._mark_uncertain(
                operation,
                diagnostic="provider could not establish external outcome",
            )
        return self._resolve(operation, result)

    def _mark_uncertain(
        self,
        operation: DeliveryOperation,
        *,
        diagnostic: str,
    ) -> DeliveryOperation:
        uncertain = self.deliveries.update_operation(
            replace(
                operation,
                state=DeliveryOperationState.UNCERTAIN,
                diagnostic=diagnostic,
            )
        )
        if operation.state != DeliveryOperationState.UNCERTAIN:
            self._event(
                "project_delivery_uncertain",
                {
                    "operation_id": uncertain.operation_id,
                    "candidate_id": uncertain.candidate_id,
                    "target_id": uncertain.target.target_id,
                    "provider_id": uncertain.provider_id,
                },
            )
        return uncertain

    def _resolve(
        self,
        operation: DeliveryOperation,
        result: DeliveryResult,
    ) -> DeliveryOperation:
        if not isinstance(result, DeliveryResult):
            raise ProjectExecutionError("delivery provider returned an invalid result")
        state = (
            DeliveryOperationState.SUCCEEDED
            if result.outcome == DeliveryOutcome.SUCCEEDED
            else DeliveryOperationState.FAILED
        )
        resolved = self.deliveries.update_operation(
            replace(
                operation,
                state=state,
                completed_at=_now(),
                result=result,
                diagnostic="",
            )
        )
        event_type = (
            "project_delivery_succeeded"
            if state == DeliveryOperationState.SUCCEEDED
            else "project_delivery_failed"
        )
        self._event(
            event_type,
            {
                "operation_id": resolved.operation_id,
                "candidate_id": resolved.candidate_id,
                "target_id": resolved.target.target_id,
                "provider_id": resolved.provider_id,
                "references": list(result.references),
                "message": result.message,
            },
        )
        return resolved

    def _candidate(self, candidate_id: str) -> DeliveryCandidate:
        candidate = self.deliveries.get_candidate(candidate_id)
        if candidate is None:
            raise ProjectExecutionError(
                f"delivery candidate not found: {candidate_id}"
            )
        return candidate

    def _operation(self, operation_id: str) -> DeliveryOperation:
        operation = self.deliveries.get_operation(operation_id)
        if operation is None:
            raise ProjectExecutionError(
                f"delivery operation not found: {operation_id}"
            )
        return operation

    def _event(self, event_type: str, data: dict[str, object]) -> None:
        if self.journal is not None:
            self.journal.append(event_type, dict(data))
