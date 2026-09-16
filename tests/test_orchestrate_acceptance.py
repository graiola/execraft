from __future__ import annotations

import pytest

from execraft.orchestrate.acceptance import collect_acceptance_evidence
from execraft.orchestrate.journal import JournalEntry
from execraft.orchestrate.models import (
    AcceptanceCriterion,
    TaskExecutionState,
    WorkPackage,
    WorkPackageStage,
)
from execraft.orchestrate.orchestrator import (
    OrchestrationConfig,
    ProjectOrchestrator,
    _StageEscalated,
)
from execraft.orchestrate.structured_output import (
    StructuredOutputError,
    normalize_structured_result,
    review_output_schema,
)


def _entry(sequence: int, event_type: str, **payload) -> JournalEntry:
    return JournalEntry(
        sequence=sequence,
        timestamp=f"2026-07-22T00:00:{sequence:02d}+00:00",
        event_type=event_type,
        payload={"package_id": "WP11", **payload},
    )


def _package(criterion_id: str = "wp11_exit_criteria") -> WorkPackage:
    return WorkPackage(
        id="WP11",
        title="Typed planner",
        acceptance_criteria=[
            AcceptanceCriterion(
                id=criterion_id,
                description="All documented WP11 exit criteria are satisfied with durable evidence.",
            )
        ],
        verification_profile="integration",
    )


def _successful_entries(*, findings: list[str] | None = None) -> list[JournalEntry]:
    return [
        _entry(
            1,
            "agent_result_persisted",
            capability="fix_review",
            stage="fix_review",
            agent_id="codex",
            artifact={"path": "/artifacts/fix.json", "sha256": "fix"},
        ),
        _entry(
            2,
            "verification_command_run",
            command="./tools/verify.sh focused",
            repository_id="app",
            status="passed",
            returncode=0,
            stdout_fingerprint="focused",
        ),
        _entry(
            3,
            "verification_command_run",
            command="tools/run_static_validation.sh",
            repository_id="core",
            status="passed",
            returncode=0,
            stdout_fingerprint="static",
        ),
        _entry(
            4,
            "verification_passed",
            effective_status="passed",
            command_count=2,
            commands=[],
        ),
        _entry(
            5,
            "agent_result_persisted",
            capability="review",
            stage="final_review",
            agent_id="opencode-zen-free",
            artifact={
                "path": "/artifacts/final-review.json",
                "sha256": "review-digest",
            },
        ),
        _entry(
            6,
            "review_result",
            verdict="approved",
            findings=list(findings or []),
            observations=[],
        ),
    ]


def _completed_children(*ids: str) -> list[dict[str, object]]:
    return [
        {
            "package_id": child_id,
            "completed": True,
            "commit_satisfied": True,
        }
        for child_id in ids
    ]


def test_collects_durable_package_exit_evidence_from_existing_journal():
    package = _package()
    package.shard_ids = ["WP11-S1", "WP11-S2"]  # Mark as aggregate

    bundle = collect_acceptance_evidence(
        package,
        _successful_entries(findings=["non-blocking note"]),
        require_verification=True,
        child_evidence=_completed_children("WP11-S1", "WP11-S2"),
    )

    assert bundle is not None
    assert bundle.criterion_ids == ("wp11_exit_criteria",)
    assert len(bundle.verification_commands) == 2
    assert bundle.reviewer_id == "opencode-zen-free"
    assert bundle.legacy_observations == ("non-blocking note",)
    assert "final-review.json" in bundle.evidence_text()
    assert "2 command(s) evaluated" in bundle.evidence_text()


def test_aggregate_evidence_from_completed_children():
    """Aggregate packages use child relationships, not criterion naming."""
    package = _package("generic_work_package")
    package.shard_ids = ["WP01-S1", "WP01-S2"]  # Has shards = aggregate
    package.acceptance_criteria[0].description = "Generic criterion"

    bundle = collect_acceptance_evidence(
        package,
        _successful_entries(),
        require_verification=True,
        child_evidence=_completed_children("WP01-S1", "WP01-S2"),
    )
    assert bundle is not None
    assert bundle.criterion_ids == ("generic_work_package",)
    assert _is_aggregate_package(package, []) is True


def test_legacy_execution_mode_aggregate_collects_without_shard_ids():
    """Legacy aggregates remain identifiable without shard metadata or events."""
    package = _package("legacy_aggregate_exit")
    package.execution_mode = "aggregate"

    bundle = collect_acceptance_evidence(
        package,
        _successful_entries(),
        require_verification=True,
        child_evidence=_completed_children("WP11-S1", "WP11-S2"),
    )

    assert bundle is not None
    assert bundle.criterion_ids == ("legacy_aggregate_exit",)
    assert _is_aggregate_package(package, []) is True


def test_graph_parent_aggregate_collects_from_supplied_child_assessments():
    """Graph-only aggregates are detected from orchestrator child evidence."""
    package = _package("graph_aggregate_exit")
    children = _completed_children("WP11-S1", "WP11-S2")

    bundle = collect_acceptance_evidence(
        package,
        _successful_entries(),
        require_verification=True,
        child_evidence=children,
    )

    assert bundle is not None
    assert bundle.criterion_ids == ("graph_aggregate_exit",)


def test_aggregate_collector_requires_all_completed_child_evidence():
    package = _package("generic_work_package")
    package.shard_ids = ["WP11-S1", "WP11-S2"]

    assert collect_acceptance_evidence(
        package,
        _successful_entries(),
        require_verification=True,
        child_evidence=_completed_children("WP11-S1"),
    ) is None


def _is_aggregate_package(pkg: WorkPackage, entries: list) -> bool:
    """Test helper exposing aggregate detection."""
    from execraft.orchestrate.acceptance import _is_aggregate_package as impl
    return impl(pkg, entries)


def test_collector_rejects_failed_or_stale_verification():
    entries = _successful_entries()
    entries[1].payload["status"] = "failed"
    entries[1].payload["returncode"] = 1
    entries[3].event_type = "verification_failed"
    entries[3].payload = {"package_id": "WP11", "attempt": 1}

    assert collect_acceptance_evidence(
        _package(), entries, require_verification=True,
        child_evidence=_completed_children("WP11-S1", "WP11-S2"),
    ) is None

    stale = _successful_entries()
    # A later fix occurs after the accepted verification and before review, so
    # the verification evidence is stale for that mutation.
    stale.insert(
        4,
        _entry(
            5,
            "agent_result_persisted",
            capability="fix_review",
            stage="fix_review",
            agent_id="codex",
            artifact={"path": "/artifacts/later-fix.json", "sha256": "later"},
        ),
    )
    stale[-2].sequence = 6
    stale[-1].sequence = 7

    assert collect_acceptance_evidence(
        _package(), stale, require_verification=True,
        child_evidence=_completed_children("WP11-S1", "WP11-S2"),
    ) is None



def test_collector_accepts_effective_baseline_verification_without_rewriting_raw_failure():
    package = _package("baseline_exit")
    package.shard_ids = ["WP11-S1"]
    entries = [
        _entry(
            1,
            "agent_result_persisted",
            capability="fix_review",
            stage="fix_review",
            agent_id="codex",
            artifact={"path": "/artifacts/fix.json", "sha256": "fix"},
        ),
        _entry(
            2,
            "verification_command_run",
            command="colcon test",
            repository_id="frontend",
            status="failed",
            returncode=1,
            stdout_fingerprint="raw-failure",
        ),
        _entry(
            3,
            "verification_passed_with_accepted_baseline",
            effective_status="passed_with_accepted_baseline",
            command_count=1,
            accepted_baseline_matches=[{"observed": [{"test_id": "A"}]}],
            commands=[],
        ),
        _entry(
            4,
            "agent_result_persisted",
            capability="review",
            stage="final_review",
            agent_id="reviewer",
            artifact={"path": "/artifacts/review.json", "sha256": "review"},
        ),
        _entry(5, "review_result", verdict="approved", findings=[], observations=[]),
    ]

    bundle = collect_acceptance_evidence(
        package,
        entries,
        require_verification=True,
        child_evidence=_completed_children("WP11-S1"),
    )

    assert bundle is not None
    assert bundle.verification_status == "passed_with_accepted_baseline"
    assert bundle.verification_commands[0]["status"] == "failed"
    assert bundle.accepted_baseline_matches == ({"observed": [{"test_id": "A"}]},)
    assert "passed with package-scoped accepted baseline" in bundle.evidence_text()


def test_collector_uses_latest_successful_verification_attempt_only():
    package = _package("retry_exit")
    package.shard_ids = ["WP11-S1"]
    entries = [
        _entry(1, "verification_command_run", command="check", status="failed", returncode=1),
        _entry(2, "verification_failed", attempt=1),
        _entry(3, "verification_command_run", command="check", status="passed", returncode=0),
        _entry(4, "verification_passed", effective_status="passed", command_count=1),
        _entry(
            5, "agent_result_persisted", capability="review", stage="final_review",
            agent_id="reviewer", artifact={"path": "/artifacts/review.json", "sha256": "review"},
        ),
        _entry(6, "review_result", verdict="approved", findings=[], observations=[]),
    ]

    bundle = collect_acceptance_evidence(
        package, entries, require_verification=True,
        child_evidence=_completed_children("WP11-S1"),
    )

    assert bundle is not None
    assert len(bundle.verification_commands) == 1
    assert bundle.verification_commands[0]["status"] == "passed"


def test_approved_findings_are_rejected_before_acceptance(tmp_path):
    raw = {
        "verdict": "approved",
        "findings": ["Consider documenting the optional path"],
        "summary": "Approved with a note",
    }
    with pytest.raises(StructuredOutputError):
        normalize_structured_result(raw, review_output_schema())


def test_validation_auto_collects_and_persists_evidence(tmp_path):
    orch = ProjectOrchestrator(
        "collect-evidence",
        config=OrchestrationConfig(state_dir=tmp_path / "state"),
    )
    package = _package()
    package.execution_mode = "aggregate"
    children = [
        WorkPackage(
            id=child_id,
            title=child_id,
            parent_id=package.id,
            stage=WorkPackageStage.COMPLETED,
        )
        for child_id in ("WP11-S1", "WP11-S2")
    ]
    orch._state_record.plan_graph.work_packages = [package, *children]
    for entry in _successful_entries(findings=["legacy note"]):
        orch._journal.append(entry.event_type, entry.payload, timestamp=entry.timestamp)

    orch._validate_acceptance_evidence(package)

    criterion = package.acceptance_criteria[0]
    assert criterion.verified is True
    assert "verification profile 'integration'" in criterion.evidence
    collected = next(
        entry for entry in reversed(orch._journal.read())
        if entry.event_type == "acceptance_evidence_collected"
    )
    assert collected.payload["criteria"] == ["wp11_exit_criteria"]
    assert collected.payload["review"]["legacy_observations"] == ["legacy note"]


def test_uncollectable_evidence_records_current_escalation_without_auto_resume(tmp_path):
    orch = ProjectOrchestrator(
        "missing-evidence",
        config=OrchestrationConfig(state_dir=tmp_path / "state"),
    )
    package = _package("specific_behavior")
    package.acceptance_criteria[0].description = "Specific behavior works"
    orch._state_record.plan_graph.work_packages = [package]
    orch._state_record.state = TaskExecutionState.RUNNING
    orch.save_state()

    with pytest.raises(_StageEscalated):
        orch._validate_acceptance_evidence(package)

    assert orch.state == TaskExecutionState.HUMAN_REQUIRED
    report = orch.human_required_report()
    assert report is not None
    assert report["stage"] == "ready_to_commit"
    assert "acceptance evidence missing" in report["reason"]
    assert orch.can_auto_resume_agent_wait() is False


def test_collector_preserves_legacy_all_raw_passing_journal_compatibility():
    package = _package("legacy_verification_exit")
    package.shard_ids = ["WP11-S1"]
    entries = [
        _entry(
            1,
            "verification_command_run",
            command="legacy-check",
            repository_id="core",
            status="passed",
            returncode=0,
            stdout_fingerprint="legacy",
        ),
        _entry(
            2,
            "agent_result_persisted",
            capability="review",
            stage="final_review",
            agent_id="reviewer",
            artifact={"path": "/artifacts/review.json", "sha256": "review"},
        ),
        _entry(3, "review_result", verdict="approved", findings=[], observations=[]),
    ]

    bundle = collect_acceptance_evidence(
        package,
        entries,
        require_verification=True,
        child_evidence=_completed_children("WP11-S1"),
    )

    assert bundle is not None
    assert bundle.verification_status == "passed"
    assert bundle.verification_commands[0]["status"] == "passed"
