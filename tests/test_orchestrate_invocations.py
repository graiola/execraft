from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess

import pytest

from execraft.orchestrate.context import AgentContextAssembler
from execraft.orchestrate.invocations import AgentInvocationStore, handoff_sha256
from execraft.orchestrate.journal import EventJournal
from execraft.orchestrate.models import WorkPackage
from execraft.orchestrate.scheduler import AgentCapability, StructuredHandoff


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=path, text=True, capture_output=True, check=True
    ).stdout.strip()


def _repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "execraft@example.invalid")
    _git(path, "config", "user.name", "Execraft tests")
    (path / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-q", "-m", "baseline")
    return path


def _begin(store: AgentInvocationStore, *, attempt: int = 1, parent: str = ""):
    handoff = {
        "schema_version": 2,
        "handoff_id": f"handoff-{attempt}",
        "work_package_id": "wp1",
        "stage": "implement",
        "attempt": attempt,
    }
    return store.begin(
        project_id="task",
        package_id="wp1",
        stage="implement",
        capability="implement",
        attempt=attempt,
        agent_id=f"agent-{attempt}",
        adapter="codex",
        model="gpt-test",
        parent_invocation_id=parent,
        triggering_event_id="journal:7",
        handoff=handoff,
        skills=[
            {
                "id": "ai-implement",
                "version": "2",
                "content_hash": "abc123",
                "source": "builtin",
            }
        ],
        isolation={"required": "hard", "provided": "hard"},
        workspace_before_digest="before",
    )


def test_invocation_store_preserves_exact_contract_and_terminal_result(tmp_path: Path):
    store = AgentInvocationStore(tmp_path / "invocations.sqlite3")
    running = _begin(store)

    assert running.status == "running"
    assert running.handoff_sha256 == handoff_sha256(running.handoff)
    assert running.skills[0]["version"] == "2"
    assert running.isolation == {"provided": "hard", "required": "hard"}

    completed = store.complete(
        running.invocation_id,
        duration_seconds=1.25,
        workspace_after_digest="after",
        result_artifact={"path": "artifact.json", "sha256": "deadbeef"},
        normalized_result={"ok": True, "status": "implemented"},
    )

    assert completed.status == "completed"
    assert completed.duration_seconds == pytest.approx(1.25)
    assert completed.workspace_after_digest == "after"
    assert completed.normalized_result["status"] == "implemented"
    with pytest.raises(RuntimeError, match="already completed"):
        store.fail(
            running.invocation_id,
            duration_seconds=2,
            failure={"classification": "timeout"},
        )


def test_invocation_store_records_causal_failover_chain(tmp_path: Path):
    store = AgentInvocationStore(tmp_path / "invocations.sqlite3")
    first = _begin(store, attempt=1)
    store.fail(
        first.invocation_id,
        duration_seconds=0.5,
        failure={"classification": "network_transient", "error": "offline"},
    )
    second = _begin(store, attempt=2, parent=first.invocation_id)
    store.complete(
        second.invocation_id,
        duration_seconds=0.7,
        normalized_result={"ok": True},
    )

    records = store.list_for_package("task", "wp1", newest_first=False)
    assert [record.status for record in records] == ["failed", "completed"]
    assert records[1].parent_invocation_id == records[0].invocation_id
    assert records[0].failure["classification"] == "network_transient"


def test_invocation_store_recovers_interrupted_rows_atomically(tmp_path: Path):
    store = AgentInvocationStore(tmp_path / "invocations.sqlite3")
    first = _begin(store, attempt=1)
    second = _begin(store, attempt=2, parent=first.invocation_id)

    recovered = store.recover_incomplete(reason="test restart")

    assert {record.invocation_id for record in recovered} == {
        first.invocation_id,
        second.invocation_id,
    }
    assert all(record.status == "failed" for record in recovered)
    assert all(record.failure["classification"] == "interrupted" for record in recovered)
    assert store.recover_incomplete() == []


def test_invocation_store_lists_only_open_attempts_newest_first(tmp_path: Path):
    store = AgentInvocationStore(tmp_path / "invocations.sqlite3")
    first = _begin(store, attempt=1)
    second = _begin(store, attempt=2, parent=first.invocation_id)
    store.complete(first.invocation_id, duration_seconds=0.1)

    running = store.list_running()

    assert [record.invocation_id for record in running] == [second.invocation_id]
    assert running[0].agent_id == "agent-2"


def test_invocation_store_supports_concurrent_writers(tmp_path: Path):
    store = AgentInvocationStore(tmp_path / "invocations.sqlite3")

    def create(index: int) -> str:
        record = store.begin(
            project_id="task",
            package_id=f"wp-{index % 4}",
            stage="review",
            capability="review",
            attempt=index + 1,
            agent_id=f"agent-{index}",
            handoff={"index": index},
        )
        store.complete(
            record.invocation_id,
            duration_seconds=0.01,
            normalized_result={"ok": True, "index": index},
        )
        return record.invocation_id

    with ThreadPoolExecutor(max_workers=8) as pool:
        identifiers = list(pool.map(create, range(32)))

    assert len(set(identifiers)) == 32
    assert len(store.list_recent("task", limit=100)) == 32


def test_context_assembler_propagates_implementation_verification_and_failover(tmp_path: Path):
    repo = _repo(tmp_path / "repo")
    (repo / "feature.py").write_text("value = 1\n", encoding="utf-8")
    journal = EventJournal(tmp_path / "journal.json")
    journal.append("verification_failed", {"package_id": "wp1"})
    store = AgentInvocationStore(tmp_path / "invocations.sqlite3")
    failed = _begin(store)
    store.fail(
        failed.invocation_id,
        duration_seconds=0.2,
        failure={"classification": "timeout", "error": "provider timeout"},
    )
    package = WorkPackage(
        id="wp1",
        title="Package",
        affected_repositories=["repo"],
    )
    package.last_implementation = {
        "status": "implemented",
        "summary": "Added the feature",
        "invocation_id": "impl-1",
    }
    package.last_verification = {
        "status": "failed",
        "attempt": 2,
        "commands": [
            {
                "command": "pytest -q",
                "status": "failed",
                "returncode": 1,
                "stderr": "one failure",
            }
        ],
    }
    assembler = AgentContextAssembler(
        project_id="task",
        repository_paths={"repo": repo},
        journal=journal,
        invocations=store,
    )

    enriched = assembler.enrich(
        StructuredHandoff(
            work_package_id="wp1",
            stage="review",
            summary="Review the implementation",
        ),
        package,
        AgentCapability.REVIEW,
    )

    assert "feature.py" in enriched.repository_diff_summary
    assert enriched.execution_context["implementation"]["summary"] == "Added the feature"
    assert enriched.execution_context["verification"]["status"] == "failed"
    assert "verification-failure.json" in enriched.bounded_excerpts
    assert "implementation-result.json" in enriched.bounded_excerpts
    # Per-attempt detail is carried once, by the budget-planned history block.
    # Only the aggregate rides in the execution context, where the planner
    # cannot trim it.
    assert enriched.execution_context["prior_invocation_summary"]["latest_status"] == "failed"
    assert "prior_invocations" not in enriched.execution_context
    attempts = json.loads(enriched.bounded_excerpts["prior-attempt-summary.json"])
    assert attempts[0]["status"] == "failed"
    assert any("previous provider attempt failed" in item.lower() for item in enriched.relevant_decisions)
    assert enriched.triggering_event_id == "journal:1"
