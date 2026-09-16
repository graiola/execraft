from __future__ import annotations

from dataclasses import replace

import pytest

from execraft.project_execution.delivery import (
    DeliveryCandidate,
    DeliveryOperation,
    DeliveryOperationState,
    DeliveryOutcome,
    DeliveryProvider,
    DeliveryRepository,
    DeliveryRequest,
    DeliveryResult,
    DeliveryService,
    DeliveryTarget,
    DeliveryUncertainError,
)
from execraft.project_execution.events import ProjectEventJournal
from execraft.project_execution.models import (
    DeliveryPolicy,
    MilestoneRequirements,
    ProjectExecutionDefinition,
    ProjectExecutionError,
    ProjectMilestone,
)
from execraft.project_execution.repository import ProjectExecutionRepository
from execraft.project_execution.runtime_repository import (
    ProjectExecutionRuntimeState,
    ProjectRuntimeRepository,
)


BASELINE = {
    "achieved_at": "2026-09-10T12:00:00+00:00",
    "definition_revision": 3,
    "tasks": {
        "task-a": {
            "outcome": "completed",
            "task_digest": "sha256:task",
            "plan_digest": "sha256:plan",
            "verification": {"outcome": "passed", "passed": 1, "failed": 0, "total": 1},
        }
    },
    "gates": {},
    "repositories": {"repo": "0123456789abcdef"},
    "artifacts": ["artifact://build/result"],
    "delivery": {"policy": "candidate"},
}


class FakeProvider:
    provider_id = "fake-provider"

    def __init__(self, *, result: DeliveryResult | None = None) -> None:
        self.result = result or DeliveryResult(
            DeliveryOutcome.SUCCEEDED,
            references=("external:release/42",),
        )
        self.deliver_calls: list[DeliveryRequest] = []
        self.reconcile_calls: list[DeliveryRequest] = []
        self.reconciled_result: DeliveryResult | None = None
        self.raise_on_deliver: Exception | None = None
        self.raise_on_reconcile: Exception | None = None

    def deliver(self, request: DeliveryRequest) -> DeliveryResult:
        self.deliver_calls.append(request)
        if self.raise_on_deliver is not None:
            raise self.raise_on_deliver
        return self.result

    def reconcile(self, request: DeliveryRequest) -> DeliveryResult | None:
        self.reconcile_calls.append(request)
        if self.raise_on_reconcile is not None:
            raise self.raise_on_reconcile
        return self.reconciled_result


def _service(tmp_path, *, baseline=None, policy=DeliveryPolicy.CANDIDATE):
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)
    definitions = ProjectExecutionRepository(project_dir)
    definitions.create(
        ProjectExecutionDefinition(
            project="sample",
            milestones=(
                ProjectMilestone(
                    "release-m1",
                    "Release M1",
                    requires=MilestoneRequirements(),
                    delivery_policy=policy,
                ),
            ),
        )
    )
    runtime = ProjectRuntimeRepository(tmp_path / "state", "sample")
    state = ProjectExecutionRuntimeState("sample")
    if baseline is not None:
        state.milestone_achievements["release-m1"] = dict(baseline)
    runtime.save(state)
    deliveries = DeliveryRepository(tmp_path / "state", "sample")
    journal = ProjectEventJournal(tmp_path / "state", "sample")
    service = DeliveryService(
        definition_repository=definitions,
        runtime_repository=runtime,
        delivery_repository=deliveries,
        journal=journal,
    )
    return service, definitions, runtime, deliveries, journal


def test_candidate_is_deterministic_idempotent_and_baseline_immutable(tmp_path):
    service, _definitions, _runtime, deliveries, journal = _service(
        tmp_path,
        baseline=BASELINE,
    )

    first = service.prepare_candidate("release-m1")
    second = service.prepare_candidate("release-m1")

    assert first.candidate_id == second.candidate_id
    assert first.baseline_digest.startswith("sha256:")
    assert first.repositories == {"repo": "0123456789abcdef"}
    assert first.artifacts == ("artifact://build/result",)
    assert len(deliveries.candidates()) == 1

    mutable_copy = first.baseline
    mutable_copy["repositories"]["repo"] = "mutated"
    assert first.repositories == {"repo": "0123456789abcdef"}

    assert [row["type"] for row in journal.read()].count(
        "project_delivery_candidate_created"
    ) == 1


def test_candidate_authority_comes_from_immutable_achievement_policy(tmp_path):
    service, definitions, _runtime, _deliveries, _journal = _service(
        tmp_path,
        baseline=BASELINE,
    )
    current = definitions.load()
    definitions.save(
        replace(
            current,
            milestones=(replace(current.milestones[0], delivery_policy=DeliveryPolicy.NONE),),
        ),
        expected_revision=current.revision,
    )

    # Definition edits after achievement do not rewrite historical delivery semantics.
    assert service.prepare_candidate("release-m1").milestone_id == "release-m1"

    no_candidate_baseline = {**BASELINE, "delivery": {"policy": "none"}}
    service2, *_ = _service(tmp_path / "other", baseline=no_candidate_baseline)
    with pytest.raises(ProjectExecutionError, match="not a delivery candidate"):
        service2.prepare_candidate("release-m1")


def test_candidate_requires_achieved_milestone(tmp_path):
    service, *_ = _service(tmp_path, baseline=None)
    with pytest.raises(ProjectExecutionError, match="has not achieved"):
        service.prepare_candidate("release-m1")


def test_successful_delivery_is_durable_and_idempotently_reused(tmp_path):
    service, _definitions, _runtime, deliveries, journal = _service(
        tmp_path,
        baseline=BASELINE,
    )
    candidate = service.prepare_candidate("release-m1")
    provider = FakeProvider()
    target = DeliveryTarget("release-channel", "Release channel")

    first = service.deliver(candidate.candidate_id, target, provider)
    second = service.deliver(candidate.candidate_id, target, provider)

    assert first.state == DeliveryOperationState.SUCCEEDED
    assert first.result and first.result.references == ("external:release/42",)
    assert second.operation_id == first.operation_id
    assert len(provider.deliver_calls) == 1
    assert len(provider.reconcile_calls) == 0
    assert deliveries.operations() == (first,)
    types = [row["type"] for row in journal.read()]
    assert "project_delivery_started" in types
    assert "project_delivery_succeeded" in types


def test_failed_delivery_reuses_result_unless_explicit_retry(tmp_path):
    service, *_ = _service(tmp_path, baseline=BASELINE)
    candidate = service.prepare_candidate("release-m1")
    provider = FakeProvider(result=DeliveryResult(DeliveryOutcome.FAILED, message="rejected"))
    target = DeliveryTarget("release-channel")

    first = service.deliver(candidate.candidate_id, target, provider)
    second = service.deliver(candidate.candidate_id, target, provider)
    retried = service.deliver(
        candidate.candidate_id,
        target,
        provider,
        retry_failed=True,
    )

    assert first.state == DeliveryOperationState.FAILED
    assert second.operation_id == first.operation_id
    assert retried.operation_id != first.operation_id
    assert retried.sequence == 2
    assert len(provider.deliver_calls) == 2


def test_provider_exception_becomes_uncertain_and_never_blindly_redelivers(tmp_path):
    service, _definitions, _runtime, deliveries, journal = _service(
        tmp_path,
        baseline=BASELINE,
    )
    candidate = service.prepare_candidate("release-m1")
    provider = FakeProvider()
    provider.raise_on_deliver = RuntimeError("connection lost after submit")
    target = DeliveryTarget("release-channel")

    with pytest.raises(DeliveryUncertainError) as caught:
        service.deliver(candidate.candidate_id, target, provider)
    uncertain = caught.value.operation
    assert uncertain.state == DeliveryOperationState.UNCERTAIN
    assert "connection lost" in uncertain.diagnostic

    provider.raise_on_deliver = None
    observed = service.deliver(candidate.candidate_id, target, provider)
    assert observed.state == DeliveryOperationState.UNCERTAIN
    assert len(provider.deliver_calls) == 1
    assert len(provider.reconcile_calls) == 1
    assert deliveries.operations()[0].operation_id == uncertain.operation_id
    assert "project_delivery_uncertain" in [row["type"] for row in journal.read()]


def test_restart_reconciles_existing_pending_operation_without_new_side_effect(tmp_path):
    service, definitions, runtime, deliveries, journal = _service(
        tmp_path,
        baseline=BASELINE,
    )
    candidate = service.prepare_candidate("release-m1")
    target = DeliveryTarget("release-channel")
    pending = deliveries.begin_operation(
        DeliveryOperation(
            operation_id="release-m1-manual-intent",
            sequence=1,
            candidate_id=candidate.candidate_id,
            target=target,
            provider_id="fake-provider",
            state=DeliveryOperationState.PENDING,
            requested_at="2026-09-10T12:01:00+00:00",
        )
    )
    provider = FakeProvider()
    provider.reconciled_result = DeliveryResult(
        DeliveryOutcome.SUCCEEDED,
        references=("external:release/recovered",),
    )

    restarted = DeliveryService(
        definition_repository=definitions,
        runtime_repository=runtime,
        delivery_repository=DeliveryRepository(tmp_path / "state", "sample"),
        journal=journal,
    )
    resolved = restarted.deliver(candidate.candidate_id, target, provider)

    assert resolved.operation_id == pending.operation_id
    assert resolved.state == DeliveryOperationState.SUCCEEDED
    assert len(provider.deliver_calls) == 0
    assert len(provider.reconcile_calls) == 1


def test_reconciliation_requires_original_provider_identity(tmp_path):
    service, *_ = _service(tmp_path, baseline=BASELINE)
    candidate = service.prepare_candidate("release-m1")
    provider = FakeProvider()
    provider.raise_on_deliver = RuntimeError("unknown")
    with pytest.raises(DeliveryUncertainError) as caught:
        service.deliver(candidate.candidate_id, DeliveryTarget("target"), provider)

    other = FakeProvider()
    other.provider_id = "other-provider"
    with pytest.raises(ProjectExecutionError, match="original provider"):
        service.reconcile(caught.value.operation.operation_id, other)


def test_delivery_repository_rejects_symlink_and_duplicate_unresolved_operation(tmp_path):
    service, _definitions, _runtime, deliveries, _journal = _service(
        tmp_path,
        baseline=BASELINE,
    )
    candidate = service.prepare_candidate("release-m1")
    target = DeliveryTarget("target")
    first = deliveries.begin_operation(
        DeliveryOperation(
            operation_id="intent-one",
            sequence=1,
            candidate_id=candidate.candidate_id,
            target=target,
            provider_id="fake-provider",
            state=DeliveryOperationState.PENDING,
            requested_at="2026-09-10T12:01:00+00:00",
        )
    )
    with pytest.raises(ProjectExecutionError, match="unresolved operation"):
        deliveries.begin_operation(
            replace(first, operation_id="intent-two", sequence=2)
        )

    deliveries.path.unlink()
    target_file = tmp_path / "elsewhere.json"
    target_file.write_text("{}", encoding="utf-8")
    deliveries.path.symlink_to(target_file)
    with pytest.raises(ProjectExecutionError, match="unsafe"):
        deliveries.load()


def test_terminal_delivery_operation_is_immutable(tmp_path):
    service, _definitions, _runtime, deliveries, _journal = _service(
        tmp_path,
        baseline=BASELINE,
    )
    candidate = service.prepare_candidate("release-m1")
    operation = service.deliver(
        candidate.candidate_id,
        DeliveryTarget("target"),
        FakeProvider(),
    )
    assert operation.state == DeliveryOperationState.SUCCEEDED
    with pytest.raises(ProjectExecutionError, match="immutable"):
        deliveries.update_operation(replace(operation, diagnostic="changed"))


def test_delivery_provider_protocol_is_structural():
    provider: DeliveryProvider = FakeProvider()
    assert provider.provider_id == "fake-provider"


def test_candidate_rejects_tampered_persisted_baseline(tmp_path):
    service, _definitions, _runtime, deliveries, _journal = _service(
        tmp_path,
        baseline=BASELINE,
    )
    candidate = service.prepare_candidate("release-m1")
    raw = deliveries.load().as_mapping()
    raw["candidates"][candidate.candidate_id]["baseline"]["repositories"]["repo"] = "tampered"
    deliveries.path.write_text(__import__("json").dumps(raw), encoding="utf-8")

    with pytest.raises(ProjectExecutionError, match="digest does not match"):
        deliveries.load()


def test_invalid_provider_result_is_treated_as_uncertain(tmp_path):
    service, *_ = _service(tmp_path, baseline=BASELINE)
    candidate = service.prepare_candidate("release-m1")
    provider = FakeProvider()
    provider.result = object()  # type: ignore[assignment]

    with pytest.raises(DeliveryUncertainError) as caught:
        service.deliver(candidate.candidate_id, DeliveryTarget("target"), provider)

    assert caught.value.operation.state == DeliveryOperationState.UNCERTAIN
    assert "invalid result" in caught.value.operation.diagnostic


def test_reconcile_exception_preserves_uncertain_operation(tmp_path):
    service, *_ = _service(tmp_path, baseline=BASELINE)
    candidate = service.prepare_candidate("release-m1")
    provider = FakeProvider()
    provider.raise_on_deliver = RuntimeError("lost")
    with pytest.raises(DeliveryUncertainError) as caught:
        service.deliver(candidate.candidate_id, DeliveryTarget("target"), provider)

    provider.raise_on_deliver = None
    provider.raise_on_reconcile = RuntimeError("provider unavailable")
    reconciled = service.reconcile(caught.value.operation.operation_id, provider)
    assert reconciled.state == DeliveryOperationState.UNCERTAIN
    assert "provider unavailable" in reconciled.diagnostic


def test_delivery_target_persists_only_logical_identity():
    target = DeliveryTarget("production-candidate", "Production candidate")
    assert target.as_mapping() == {
        "target_id": "production-candidate",
        "label": "Production candidate",
    }
    assert set(target.__dataclass_fields__) == {"target_id", "label"}


def test_candidate_timestamp_requires_timezone(tmp_path):
    bad = {**BASELINE, "achieved_at": "2026-09-10T12:00:00"}
    service, *_ = _service(tmp_path, baseline=bad)
    with pytest.raises(ProjectExecutionError, match="timezone"):
        service.prepare_candidate("release-m1")


def test_invalid_operation_transition_is_rejected(tmp_path):
    service, _definitions, _runtime, deliveries, _journal = _service(
        tmp_path,
        baseline=BASELINE,
    )
    candidate = service.prepare_candidate("release-m1")
    pending = deliveries.begin_operation(
        DeliveryOperation(
            operation_id="transition-intent",
            sequence=1,
            candidate_id=candidate.candidate_id,
            target=DeliveryTarget("target"),
            provider_id="fake-provider",
            state=DeliveryOperationState.PENDING,
            requested_at="2026-09-10T12:01:00+00:00",
        )
    )
    uncertain = deliveries.update_operation(
        replace(pending, state=DeliveryOperationState.UNCERTAIN)
    )
    with pytest.raises(ProjectExecutionError, match="invalid delivery operation transition"):
        deliveries.update_operation(
            replace(uncertain, state=DeliveryOperationState.PENDING)
        )


def test_delivery_runtime_rejects_sequence_regression(tmp_path):
    service, _definitions, _runtime, deliveries, _journal = _service(
        tmp_path,
        baseline=BASELINE,
    )
    candidate = service.prepare_candidate("release-m1")
    deliveries.begin_operation(
        DeliveryOperation(
            operation_id="sequence-intent",
            sequence=1,
            candidate_id=candidate.candidate_id,
            target=DeliveryTarget("target"),
            provider_id="fake-provider",
            state=DeliveryOperationState.PENDING,
            requested_at="2026-09-10T12:01:00+00:00",
        )
    )
    raw = deliveries.load().as_mapping()
    raw["next_sequence"] = 1
    deliveries.path.write_text(__import__("json").dumps(raw), encoding="utf-8")
    with pytest.raises(ProjectExecutionError, match="next_sequence"):
        deliveries.load()


def test_concurrent_delivery_requests_create_one_external_side_effect(tmp_path):
    import threading

    service, _definitions, _runtime, deliveries, _journal = _service(
        tmp_path,
        baseline=BASELINE,
    )
    candidate = service.prepare_candidate("release-m1")
    target = DeliveryTarget("target")
    started = threading.Event()
    release = threading.Event()

    class BlockingProvider(FakeProvider):
        def deliver(self, request: DeliveryRequest) -> DeliveryResult:
            self.deliver_calls.append(request)
            started.set()
            assert release.wait(5)
            return self.result

    provider = BlockingProvider()
    results: list[DeliveryOperation] = []
    errors: list[BaseException] = []

    def invoke() -> None:
        try:
            results.append(service.deliver(candidate.candidate_id, target, provider))
        except BaseException as exc:  # pragma: no cover - assertion surfaced below
            errors.append(exc)

    first = threading.Thread(target=invoke)
    second = threading.Thread(target=invoke)
    first.start()
    assert started.wait(5)
    second.start()
    second.join(5)
    release.set()
    first.join(5)

    assert not errors
    assert len(provider.deliver_calls) == 1
    assert len(deliveries.operations()) == 1
    assert {result.operation_id for result in results} == {
        deliveries.operations()[0].operation_id
    }


def test_long_milestone_ids_cannot_alias_candidate_identity():
    common = "m" * 68
    first = DeliveryCandidate.from_baseline(
        project_id="sample",
        milestone_id=common + "-alpha",
        baseline=BASELINE,
        created_at="2026-09-10T12:01:00+00:00",
    )
    second = DeliveryCandidate.from_baseline(
        project_id="sample",
        milestone_id=common + "-bravo",
        baseline=BASELINE,
        created_at="2026-09-10T12:01:00+00:00",
    )
    assert first.candidate_id != second.candidate_id
    assert len(first.candidate_id) <= 96
    assert len(second.candidate_id) <= 96


def test_terminal_operation_requires_completion_timestamp():
    with pytest.raises(ProjectExecutionError, match="requires completed_at"):
        DeliveryOperation(
            operation_id="invalid-terminal",
            sequence=1,
            candidate_id="candidate",
            target=DeliveryTarget("target"),
            provider_id="fake-provider",
            state=DeliveryOperationState.SUCCEEDED,
            requested_at="2026-09-10T12:01:00+00:00",
            result=DeliveryResult(DeliveryOutcome.SUCCEEDED),
        )


def test_candidate_rejects_provider_specific_baseline_delivery_metadata(tmp_path):
    contaminated = {
        **BASELINE,
        "delivery": {"policy": "candidate", "provider": "vendor-x"},
    }
    service, *_ = _service(tmp_path, baseline=contaminated)
    with pytest.raises(ProjectExecutionError, match="provider-neutral"):
        service.prepare_candidate("release-m1")
