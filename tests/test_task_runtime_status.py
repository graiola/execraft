import json
from pathlib import Path

from execraft.orchestrate.models import (
    PlanGraph,
    TaskExecutionState,
    TaskExecutionStateRecord,
    WorkPackage,
    WorkPackageStage,
)
from execraft.orchestrate.orchestrator import OrchestrationConfig, ProjectOrchestrator
from execraft.orchestrate.task_status import (
    RUNTIME_STATUS_FILENAME,
    render_runtime_status,
    sync_runtime_status,
)


def _record() -> TaskExecutionStateRecord:
    completed = WorkPackage(
        id="WP01",
        title="Completed package",
        stage=WorkPackageStage.COMPLETED,
        status="completed",
        implementation_summary="Implemented the baseline and verified it.",
        requirements=["R1"],
        acceptance_criteria=[],
    )
    active = WorkPackage(
        id="WP02",
        title="Active package",
        stage=WorkPackageStage.IMPLEMENT,
        status="running",
        priority=10,
        complexity=55,
        requirements=["R2"],
        acceptance_criteria=[],
    )
    return TaskExecutionStateRecord(
        project_id="demo",
        state=TaskExecutionState.WAITING_FOR_AGENT,
        plan_graph=PlanGraph(work_packages=[completed, active]),
        started_at="2026-07-23T12:00:00+00:00",
        last_transition_at="2026-07-23T13:00:00+00:00",
        completed_packages=1,
        total_packages=2,
        waiting={
            "package_id": "WP02",
            "stage": "implement",
            "capability": "implement",
            "cycle": 3,
            "next_check_at": "2026-07-23T13:05:00+00:00",
            "candidates": [
                {
                    "agent_id": "local-coder",
                    "reason": "provider_error",
                    "available_at": "2026-07-23T13:05:00+00:00",
                }
            ],
        },
    )


def test_render_runtime_status_separates_history_from_live_state(tmp_path):
    text = render_runtime_status(_record(), task_id="demo", state_root=tmp_path)

    assert "State: **`waiting_for_agent`**" in text
    assert "Progress: **1/2 packages**" in text
    assert "`WP02` — Active package" in text
    assert "local-coder" in text
    assert "Implemented the baseline" in text
    assert "`HANDOFF.md` is an append-only engineering history" in text
    assert str(tmp_path / "projects" / "demo" / "state.json") in text


def test_sync_runtime_status_handles_uninitialized_task(tmp_path):
    dossier = tmp_path / "control" / "tasks" / "demo"

    snapshot = sync_runtime_status(dossier, task_id="demo", state_root=tmp_path / "state")

    assert not snapshot.has_state
    assert snapshot.path == dossier / RUNTIME_STATUS_FILENAME
    assert "No local orchestration state exists yet" in snapshot.path.read_text()


def test_sync_runtime_status_loads_durable_state(tmp_path):
    state_root = tmp_path / "state"
    state_path = state_root / "projects" / "demo" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps(_record().as_mapping()), encoding="utf-8")
    dossier = tmp_path / "control" / "tasks" / "demo"

    snapshot = sync_runtime_status(dossier, task_id="demo", state_root=state_root)

    assert snapshot.has_state
    assert snapshot.record is not None
    assert snapshot.record.completed_packages == 1
    assert "Active package" in snapshot.path.read_text()


def test_orchestrator_save_state_refreshes_runtime_status(tmp_path):
    dossier = tmp_path / "control" / "tasks" / "demo"
    config = OrchestrationConfig(state_dir=tmp_path / "state")
    orchestrator = ProjectOrchestrator(
        "demo",
        config=config,
        task_dossier_dir=dossier,
    )
    orchestrator._state_record = _record()

    orchestrator.save_state()

    runtime_path = dossier / RUNTIME_STATUS_FILENAME
    assert runtime_path.is_file()
    assert "waiting_for_agent" in runtime_path.read_text()
