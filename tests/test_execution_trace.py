from __future__ import annotations

from pathlib import Path

from execraft.orchestrate.execution_trace import build_execution_trace
from execraft.orchestrate.invocations import AgentInvocationRecord, AgentInvocationStore
from execraft.orchestrate.journal import JournalEntry
from execraft.orchestrate.models import WorkPackage, WorkPackageStage


def _record(
    invocation_id: str,
    package_id: str,
    stage: str,
    *,
    agent_id: str,
    started_at: str,
    completed_at: str,
    duration_seconds: float,
    status: str = "completed",
    attempt: int = 1,
    outcome: str = "",
) -> AgentInvocationRecord:
    return AgentInvocationRecord(
        invocation_id=invocation_id,
        project_id="demo",
        task_id="task",
        package_id=package_id,
        stage=stage,
        capability="review" if "review" in stage else "implement",
        attempt=attempt,
        agent_id=agent_id,
        adapter="codex",
        model=f"model/{agent_id}",
        status=status,
        started_at=started_at,
        completed_at=completed_at,
        duration_seconds=duration_seconds,
        normalized_result={"verdict": outcome} if outcome else {},
        result_artifact={"path": f"artifacts/{invocation_id}.json"},
    )


def _entry(sequence: int, timestamp: str, event_type: str, **payload):
    return JournalEntry(
        sequence=sequence,
        timestamp=timestamp,
        event_type=event_type,
        payload=payload,
    )


def test_execution_trace_joins_attempts_transitions_and_parallel_shards():
    parent = WorkPackage(
        id="WP18",
        title="Work Package",
        stage=WorkPackageStage.REGRESSION_VERIFY,
        status="running",
        shard_ids=["WP18__S1"],
    )
    shard = WorkPackage(
        id="WP18__S1",
        title="Shard one",
        parent_id="WP18",
        shard_key="S1",
        stage=WorkPackageStage.FINAL_REVIEW,
        status="running",
        parallel_safe=True,
    )
    invocations = [
        _record(
            "impl-1",
            "WP18__S1",
            "implement",
            agent_id="codex",
            started_at="2026-08-04T10:00:00+00:00",
            completed_at="2026-08-04T10:05:00+00:00",
            duration_seconds=300,
        ),
        _record(
            "review-1",
            "WP18__S1",
            "review",
            agent_id="qwen",
            started_at="2026-08-04T10:06:00+00:00",
            completed_at="2026-08-04T10:08:00+00:00",
            duration_seconds=120,
            outcome="changes_requested",
        ),
        _record(
            "fix-1",
            "WP18__S1",
            "fix_review",
            agent_id="codex",
            started_at="2026-08-04T10:09:00+00:00",
            completed_at="2026-08-04T10:12:00+00:00",
            duration_seconds=180,
        ),
        _record(
            "final-1",
            "WP18__S1",
            "final_review",
            agent_id="qwen",
            started_at="2026-08-04T10:14:00+00:00",
            completed_at="",
            duration_seconds=0,
            status="running",
            attempt=2,
        ),
    ]
    entries = [
        _entry(
            1,
            "2026-08-04T10:00:00+00:00",
            "package_stage_transition",
            package_id="WP18__S1",
            from_stage="prepare",
            to_stage="implement",
        ),
        _entry(
            2,
            "2026-08-04T10:05:10+00:00",
            "package_stage_transition",
            package_id="WP18__S1",
            from_stage="implement",
            to_stage="fast_verify",
        ),
        _entry(
            3,
            "2026-08-04T10:05:50+00:00",
            "package_stage_transition",
            package_id="WP18__S1",
            from_stage="fast_verify",
            to_stage="review",
        ),
        _entry(
            4,
            "2026-08-04T10:08:10+00:00",
            "package_stage_transition",
            package_id="WP18__S1",
            from_stage="review",
            to_stage="fix_review",
        ),
        _entry(
            5,
            "2026-08-04T10:08:30+00:00",
            "agent_failover",
            package_id="WP18__S1",
            from_agent="qwen",
            to_agent="codex",
        ),
        _entry(
            6,
            "2026-08-04T10:12:10+00:00",
            "package_stage_transition",
            package_id="WP18__S1",
            from_stage="fix_review",
            to_stage="regression_verify",
        ),
        _entry(
            7,
            "2026-08-04T10:13:30+00:00",
            "package_stage_transition",
            package_id="WP18__S1",
            from_stage="regression_verify",
            to_stage="final_review",
        ),
    ]

    trace = build_execution_trace(
        selected=parent,
        packages=[parent, shard],
        invocations=invocations,
        journal_entries=entries,
        now="2026-08-04T10:16:00+00:00",
    )

    assert trace["scope_package_ids"] == ["WP18", "WP18__S1"]
    assert len(trace["lanes"]) == 2
    shard_lane = next(lane for lane in trace["lanes"] if lane["package_id"] == "WP18__S1")
    stages = [node["stage"] for node in shard_lane["nodes"]]
    assert stages == [
        "implement",
        "fast_verify",
        "review",
        "fix_review",
        "regression_verify",
        "final_review",
    ]
    assert shard_lane["nodes"][2]["outcome"] == "changes_requested"
    assert shard_lane["nodes"][3]["incoming_reason"] == "Changes requested · fallback"
    assert shard_lane["nodes"][-1]["live"] is True
    assert trace["summary"]["review_loops"] == 1
    assert trace["summary"]["provider_fallbacks"] == 1
    assert trace["summary"]["attempt_count"] == 4
    assert trace["summary"]["agents_involved"] == ["codex", "qwen"]
    assert trace["history_complete"] is False  # parent has no transition history yet


def test_execution_trace_projects_historical_milestone_event_and_payload_names():
    package = WorkPackage(
        id="WP1",
        title="Compatibility package",
        stage=WorkPackageStage.PREPARE,
        status="pending",
    )
    trace = build_execution_trace(
        selected=package,
        packages=[package],
        invocations=[],
        journal_entries=[
            _entry(
                1,
                "2026-08-04T09:00:00+00:00",
                "milestone_pause_before_start_reached",
                milestone_id="WP1",
                reason="historical journal",
            )
        ],
        now="2026-08-04T09:01:00+00:00",
    )

    assert trace["lanes"][0]["annotations"] == [
        {
            "id": "journal-1",
            "event_type": "work_package_pause_before_start_reached",
            "label": "Planned pause reached",
            "detail": "historical journal",
            "timestamp": "2026-08-04T09:00:00+00:00",
            "payload": {"milestone_id": "WP1", "reason": "historical journal"},
        }
    ]


def test_execution_trace_preserves_exact_invocations_when_old_history_has_no_transitions():
    package = WorkPackage(
        id="WP1",
        title="Legacy",
        stage=WorkPackageStage.FINAL_REVIEW,
        status="running",
    )
    trace = build_execution_trace(
        selected=package,
        packages=[package],
        invocations=[
            _record(
                "legacy",
                "WP1",
                "implement",
                agent_id="codex",
                started_at="2026-08-04T09:00:00+00:00",
                completed_at="2026-08-04T09:04:00+00:00",
                duration_seconds=240,
            )
        ],
        journal_entries=[],
        now="2026-08-04T09:10:00+00:00",
    )

    assert trace["history_complete"] is False
    assert trace["lanes"][0]["nodes"][0]["invocation_id"] == "legacy"
    assert trace["summary"]["agent_work_seconds"] == 240


def test_invocation_store_lists_a_parent_and_shards_in_one_query(tmp_path: Path):
    store = AgentInvocationStore(tmp_path / "invocations.sqlite3")
    for package_id in ("WP1", "WP1__S1", "OTHER"):
        record = store.begin(
            project_id="demo",
            task_id="task",
            package_id=package_id,
            stage="implement",
            capability="implement",
            attempt=1,
            agent_id="codex",
            handoff={"work_package_id": package_id},
        )
        store.complete(record.invocation_id, duration_seconds=1)

    rows = store.list_for_packages("demo", ["WP1", "WP1__S1"])

    assert [row.package_id for row in rows] == ["WP1", "WP1__S1"]


def test_execution_trace_keeps_parallel_shards_in_separate_lanes():
    parent = WorkPackage(
        id="WP2",
        title="Parallel Work Package",
        stage=WorkPackageStage.REGRESSION_VERIFY,
        status="running",
        shard_ids=["WP2__S1", "WP2__S2"],
    )
    shard_one = WorkPackage(
        id="WP2__S1",
        title="First shard",
        parent_id="WP2",
        shard_key="S1",
        stage=WorkPackageStage.IMPLEMENT,
        status="running",
        parallel_safe=True,
    )
    shard_two = WorkPackage(
        id="WP2__S2",
        title="Second shard",
        parent_id="WP2",
        shard_key="S2",
        stage=WorkPackageStage.REVIEW,
        status="running",
        parallel_safe=True,
    )
    trace = build_execution_trace(
        selected=parent,
        packages=[parent, shard_one, shard_two],
        invocations=[
            _record(
                "parallel-one",
                "WP2__S1",
                "implement",
                agent_id="codex",
                started_at="2026-08-04T10:00:00+00:00",
                completed_at="2026-08-04T10:10:00+00:00",
                duration_seconds=600,
            ),
            _record(
                "parallel-two",
                "WP2__S2",
                "review",
                agent_id="qwen",
                started_at="2026-08-04T10:05:00+00:00",
                completed_at="2026-08-04T10:15:00+00:00",
                duration_seconds=600,
            ),
        ],
        journal_entries=[],
        now="2026-08-04T10:15:00+00:00",
    )

    assert [lane["package_id"] for lane in trace["lanes"]] == [
        "WP2",
        "WP2__S1",
        "WP2__S2",
    ]
    assert trace["summary"]["package_count"] == 3
    assert trace["summary"]["wall_clock_seconds"] == 900
    assert trace["summary"]["agent_work_seconds"] == 1200
    assert trace["summary"]["active_wall_clock_seconds"] == 900
