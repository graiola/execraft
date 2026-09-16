from __future__ import annotations

from types import SimpleNamespace

from execraft.orchestrate.models import (
    AcceptanceCriterion,
    PlanGraph,
    TaskExecutionState,
    WorkPackage,
    WorkPackageKind,
    WorkPackageStage,
)
from execraft.orchestrate.normalizer import NormalizationReport
from execraft.orchestrate.orchestrator import OrchestrationConfig, ProjectOrchestrator
from execraft.orchestrate.supervisor import (
    IncidentClass,
    IncidentStatus,
    SupervisorIncident,
)
from execraft.orchestrate.verification_outcome import (
    PASSED_WITH_ACCEPTED_BASELINE,
    accepted_baseline_failure_count,
    verification_acceptance_phrase,
    verification_is_accepted,
)
from execraft.repository_sync.spec import RepositorySyncSpec


def _sync_package() -> WorkPackage:
    package = WorkPackage(
        id="WP21-SYNC",
        title="Synchronize upstream repositories around WP21",
        kind=WorkPackageKind.REPOSITORY_SYNC,
        repository_sync=RepositorySyncSpec.from_mapping({"repositories": ["core", "ui"]}),
        affected_repositories=["core", "ui"],
        requirements=["sync"],
        acceptance_criteria=[AcceptanceCriterion(id="sync_done", description="Sync complete")],
        verification_profile="integration",
    )
    package.stage = WorkPackageStage.READY_TO_COMMIT
    package.status = "running"
    package.last_verification = {
        "status": PASSED_WITH_ACCEPTED_BASELINE,
        "accepted_baseline_matches": [
            {"observed": [{"test_id": "A"}, {"test_id": "B"}, {"test_id": "C"}]}
        ],
    }
    package.last_review = {"verdict": "approved"}
    return package


def _transaction() -> SimpleNamespace:
    return SimpleNamespace(
        transaction_id="tx-21",
        phase="verified",
        complete=False,
        repositories=[
            SimpleNamespace(
                repository_id="core",
                remote="origin",
                source_branch="master",
                source_commit="4239d62abc",
            ),
            SimpleNamespace(
                repository_id="ui",
                remote="origin",
                source_branch="master",
                source_commit="c01fdb5abc",
            ),
        ],
    )


def _orchestrator(tmp_path, package: WorkPackage) -> ProjectOrchestrator:
    orch = ProjectOrchestrator(
        "verification-outcome",
        config=OrchestrationConfig(state_dir=tmp_path / "state"),
    )
    orch.initialize_graph(PlanGraph([package]), NormalizationReport())
    return orch


def test_effective_baseline_status_is_a_first_class_success() -> None:
    verification = {
        "status": PASSED_WITH_ACCEPTED_BASELINE,
        "accepted_baseline_matches": [
            {"observed": [{"test_id": "A"}, {"test_id": "B"}]}
        ],
    }

    assert verification_is_accepted(verification)
    assert accepted_baseline_failure_count(verification) == 2
    assert "package-scoped accepted baseline" in verification_acceptance_phrase(verification)
    assert not verification_is_accepted({"status": "failed"})
    assert not verification_is_accepted({"status": "environment_failure"})


def test_repository_sync_commit_evidence_accepts_effective_baseline_status(
    tmp_path, monkeypatch
) -> None:
    package = _sync_package()
    orch = _orchestrator(tmp_path, package)
    tx = _transaction()
    service = SimpleNamespace(transactions=SimpleNamespace(load=lambda _id: tx))
    monkeypatch.setattr(orch, "_repository_sync_service", lambda: service)

    orch._prepare_repository_sync_acceptance_evidence(package)

    criterion = package.acceptance_criteria[0]
    assert criterion.verified is True
    assert "package-scoped accepted baseline" in criterion.evidence
    assert "3 accepted baseline failure(s)" in criterion.evidence
    assert "independent review approved" in criterion.evidence


def test_legacy_commit_gate_human_escalation_auto_resumes_without_supervisor(
    tmp_path, monkeypatch
) -> None:
    package = _sync_package()
    orch = _orchestrator(tmp_path, package)
    tx = _transaction()
    service = SimpleNamespace(transactions=SimpleNamespace(load=lambda _id: tx))
    monkeypatch.setattr(orch, "_repository_sync_service", lambda: service)

    escalation = orch._journal.append(
        "human_intervention_required",
        {
            "package_id": package.id,
            "stage": "commit",
            "blocked_requirement": (
                "repository sync reached commit without passed verification and approved review"
            ),
        },
    )
    orch._state_record.state = TaskExecutionState.HUMAN_REQUIRED
    orch._state_record.error_message = "legacy literal-pass commit readiness check"
    orch.save_state()

    assert orch._supervisor_coordinator.can_resume_obsolete_repository_sync_commit_check()
    assert orch._supervisor_coordinator.resume_obsolete_repository_sync_commit_check()
    assert orch.state == TaskExecutionState.RUNNING
    assert package.status == "pending"
    assert orch._state_record.error_message == ""
    event = next(
        item
        for item in orch._journal.read()
        if item.event_type == "repository_sync_commit_check_superseded"
    )
    assert event.payload["verification_status"] == PASSED_WITH_ACCEPTED_BASELINE
    assert escalation.sequence < event.sequence


def test_active_supervisor_incident_for_legacy_commit_gate_is_retired_before_retry(
    tmp_path, monkeypatch
) -> None:
    package = _sync_package()
    orch = _orchestrator(tmp_path, package)
    tx = _transaction()
    service = SimpleNamespace(transactions=SimpleNamespace(load=lambda _id: tx))
    monkeypatch.setattr(orch, "_repository_sync_service", lambda: service)

    escalation = orch._journal.append(
        "human_intervention_required",
        {
            "package_id": package.id,
            "stage": "commit",
            "blocked_requirement": (
                "repository sync reached commit without passed verification and approved review"
            ),
        },
    )
    incident = SupervisorIncident(
        incident_id="incident-commit-gate",
        fingerprint="legacy-gate",
        package_id=package.id,
        stage="ready_to_commit",
        classification=IncidentClass.UNKNOWN,
        status=IncidentStatus.DIAGNOSING,
        escalation_sequence=escalation.sequence,
    )
    orch._supervisor_incidents.save(incident)
    orch._state_record.state = TaskExecutionState.SUPERVISING
    orch.save_state()

    assert orch._supervisor_coordinator.resume_obsolete_repository_sync_commit_check()
    retired = orch._supervisor_incidents.get(incident.incident_id)
    assert retired is not None
    assert retired.status == IncidentStatus.RESOLVED
    assert any("literal-pass commit readiness check retired" in item for item in retired.actions_taken)
    assert orch.state == TaskExecutionState.RUNNING


def test_legacy_commit_gate_recovery_fails_closed_for_real_commit_failure(
    tmp_path, monkeypatch
) -> None:
    package = _sync_package()
    orch = _orchestrator(tmp_path, package)
    tx = _transaction()
    service = SimpleNamespace(transactions=SimpleNamespace(load=lambda _id: tx))
    monkeypatch.setattr(orch, "_repository_sync_service", lambda: service)
    orch._journal.append(
        "human_intervention_required",
        {
            "package_id": package.id,
            "stage": "commit",
            "blocked_requirement": "repository ui verified tree changed before commit",
        },
    )
    orch._state_record.state = TaskExecutionState.HUMAN_REQUIRED
    orch.save_state()

    assert not orch._supervisor_coordinator.can_resume_obsolete_repository_sync_commit_check()
    assert not orch._supervisor_coordinator.resume_obsolete_repository_sync_commit_check()
    assert orch.state == TaskExecutionState.HUMAN_REQUIRED


def test_legacy_commit_gate_recovery_requires_verified_transaction(
    tmp_path, monkeypatch
) -> None:
    package = _sync_package()
    orch = _orchestrator(tmp_path, package)
    tx = _transaction()
    tx.phase = "ready_verify"
    service = SimpleNamespace(transactions=SimpleNamespace(load=lambda _id: tx))
    monkeypatch.setattr(orch, "_repository_sync_service", lambda: service)
    orch._journal.append(
        "human_intervention_required",
        {
            "package_id": package.id,
            "stage": "commit",
            "blocked_requirement": (
                "repository sync reached commit without passed verification and approved review"
            ),
        },
    )
    orch._state_record.state = TaskExecutionState.HUMAN_REQUIRED
    orch.save_state()

    assert not orch._supervisor_coordinator.can_resume_obsolete_repository_sync_commit_check()


def test_pipeline_restarts_current_ready_to_commit_state_without_supervisor_or_reverification(
    tmp_path, monkeypatch
) -> None:
    package = _sync_package()
    config = OrchestrationConfig(
        state_dir=tmp_path / "state",
        strict_checks=False,
        auto_commit=False,
    )
    orch = ProjectOrchestrator("verification-outcome-pipeline", config=config)
    orch.initialize_graph(PlanGraph([package]), NormalizationReport())

    escalation = orch._journal.append(
        "human_intervention_required",
        {
            "package_id": package.id,
            "stage": "commit",
            "blocked_requirement": (
                "repository sync reached commit without passed verification and approved review"
            ),
        },
    )
    incident = SupervisorIncident(
        incident_id="incident-live-state",
        fingerprint="legacy-gate-live",
        package_id=package.id,
        stage="ready_to_commit",
        classification=IncidentClass.UNKNOWN,
        status=IncidentStatus.DIAGNOSING,
        escalation_sequence=escalation.sequence,
    )
    orch._supervisor_incidents.save(incident)
    orch._state_record.state = TaskExecutionState.SUPERVISING
    orch.save_state()

    class RepoState:
        repository_id = "core"
        remote = "origin"
        source_branch = "master"
        source_commit = "4239d62abc"
        status = "committed"
        target_after = "merge123456789"
        conflict_paths: list[str] = []

        def as_mapping(self):
            return {
                "repository_id": self.repository_id,
                "remote": self.remote,
                "source_branch": self.source_branch,
                "source_commit": self.source_commit,
                "status": self.status,
                "target_after": self.target_after,
            }

    tx = SimpleNamespace(
        transaction_id="tx-live",
        phase="verified",
        complete=False,
        repositories=[RepoState()],
    )
    service = SimpleNamespace(
        transactions=SimpleNamespace(load=lambda _id: tx),
        commit=lambda _id, title: tx,
    )
    monkeypatch.setattr(orch, "_repository_sync_service", lambda: service)
    monkeypatch.setattr(
        orch,
        "_run_supervision_unlocked",
        lambda: (_ for _ in ()).throw(AssertionError("Supervisor must not run")),
    )
    monkeypatch.setattr(
        orch,
        "_run_verification",
        lambda _package: (_ for _ in ()).throw(AssertionError("verification must not rerun")),
    )

    orch._run_pipeline_unlocked()

    assert package.stage == WorkPackageStage.COMPLETED
    assert package.status == "completed"
    assert orch.state == TaskExecutionState.COMPLETED
    retired = orch._supervisor_incidents.get(incident.incident_id)
    assert retired is not None and retired.status == IncidentStatus.RESOLVED
