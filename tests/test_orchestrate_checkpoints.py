from __future__ import annotations

from pathlib import Path
import sqlite3

from execraft.orchestrate import (
    OrchestrationConfig,
    ProjectOrchestrator,
    TaskExecutionState,
    StateCheckpointStore,
)


def test_checkpoint_store_deduplicates_and_bounds_history(tmp_path: Path):
    store = StateCheckpointStore(tmp_path / "checkpoints.sqlite3", retention=8)
    first = store.save(
        {"project_id": "task", "state": "initializing"},
        journal_sequence=1,
        reason="initial",
    )
    duplicate = store.save(
        {"project_id": "task", "state": "initializing"},
        journal_sequence=1,
        reason="duplicate",
    )
    assert duplicate.sequence == first.sequence

    for index in range(20):
        store.save(
            {"project_id": "task", "state": "running", "step": index},
            journal_sequence=index + 2,
            reason="progress",
        )

    history = store.list(limit=100)
    assert len(history) == 8
    assert history[0].state["step"] == 19
    assert history[-1].state["step"] == 12


def test_orchestrator_recovers_corrupt_state_projection_from_checkpoint(tmp_path: Path):
    config = OrchestrationConfig(
        strict_checks=False,
        auto_commit=False,
        state_dir=tmp_path / "state",
    )
    first = ProjectOrchestrator("task", config=config)
    first.initialize("## Package: Only\n\n### Acceptance criteria\n- [ ] Works\n")
    first.transition_to(TaskExecutionState.RUNNING)
    first.transition_to(TaskExecutionState.WAITING_FOR_ENVIRONMENT)
    state_path = tmp_path / "state" / "projects" / "task" / "state.json"
    state_path.write_text("{broken", encoding="utf-8")

    recovered = ProjectOrchestrator("task", config=config)
    record = recovered.load_state()

    assert record.state == TaskExecutionState.WAITING_FOR_ENVIRONMENT
    assert recovered.status_report()["checkpoint"]["state_sha256"]
    assert "\"state\": \"waiting_for_environment\"" in state_path.read_text(encoding="utf-8")
    events = recovered._journal.read()
    assert events[-1].event_type == "state_projection_recovered"
    assert "invalid state projection" in events[-1].payload["reason"]


def test_orchestrator_recovers_missing_state_projection_from_checkpoint(tmp_path: Path):
    config = OrchestrationConfig(
        strict_checks=False,
        auto_commit=False,
        state_dir=tmp_path / "state",
    )
    first = ProjectOrchestrator("task", config=config)
    first.initialize("## Package: Only\n\n### Acceptance criteria\n- [ ] Works\n")
    state_path = tmp_path / "state" / "projects" / "task" / "state.json"
    state_path.unlink()

    recovered = ProjectOrchestrator("task", config=config)
    record = recovered.load_state()

    assert record.total_packages == 1
    assert state_path.is_file()
    assert recovered._journal.read()[-1].event_type == "state_projection_recovered"


def test_latest_valid_skips_digest_mismatch_and_future_journal_state(tmp_path: Path):
    path = tmp_path / "checkpoints.sqlite3"
    store = StateCheckpointStore(path)
    valid = store.save({"step": 1}, journal_sequence=3)
    future = store.save({"step": 2}, journal_sequence=99)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE checkpoints SET state_json = ? WHERE sequence = ?",
            ('{"step":999}', future.sequence),
        )

    recovered = store.latest_valid(max_journal_sequence=3)

    assert recovered is not None
    assert recovered.sequence == valid.sequence
    assert recovered.state == {"step": 1}
