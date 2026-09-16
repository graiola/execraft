from __future__ import annotations

from pathlib import Path

from execraft.execution_identity import ExecutionIdentity
from execraft.orchestrate.invocations import AgentInvocationStore
from execraft.runtime.contracts import RuntimeSessionRef


def test_invocation_ledger_persists_and_reports_wp4_execution_dimensions(tmp_path: Path) -> None:
    store = AgentInvocationStore(tmp_path / "invocations.sqlite3")
    identity = ExecutionIdentity(
        candidate_id="implementation-qwen",
        runtime_id="native-opencode",
        runtime_backend="opencode",
        model_route_id="qwen-satellite",
        model_provider="ollama",
        model="qwen3-coder:30b-32k",
        target_id="gpu-1",
        target_kind="inference_endpoint",
        concurrency_group="gpu-1",
        legacy_provider_id="opencode-qwen",
    )
    running = store.begin(
        project_id="project",
        task_id="task",
        package_id="wp1",
        stage="implement",
        capability="implement",
        attempt=1,
        agent_id="opencode-qwen",
        adapter="opencode",
        model="ollama-gpu/qwen3-coder:30b-32k",
        execution_identity=identity,
        handoff={"work_package_id": "wp1", "stage": "implement"},
    )
    completed = store.complete(
        running.invocation_id,
        duration_seconds=1.0,
        usage={
            "provider": "ollama",
            "model": "qwen3-coder:30b-32k",
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
        },
        runtime_session=RuntimeSessionRef(
            runtime_id="native-opencode",
            candidate_id="implementation-qwen",
            session_id="abc123",
            backend="opencode",
        ),
        runtime_metadata={"session_reused": True, "prompt_bytes": 2048},
    )

    assert completed.agent_id == "opencode-qwen"
    assert completed.candidate_id == "implementation-qwen"
    assert completed.runtime_id == "native-opencode"
    assert completed.runtime_backend == "opencode"
    assert completed.model_route_id == "qwen-satellite"
    assert completed.model_provider == "ollama"
    assert completed.model_name == "qwen3-coder:30b-32k"
    assert completed.target_id == "gpu-1"
    assert completed.target_kind == "inference_endpoint"
    assert completed.concurrency_group == "gpu-1"
    assert completed.runtime_session["session_id"] == "abc123"
    assert completed.runtime_metadata == {"prompt_bytes": 2048, "session_reused": True}
    assert completed.as_mapping()["schema_version"] == 5

    report = store.usage_summary("project")
    assert report["by_candidate"]["implementation-qwen"]["total_tokens"] == 120
    assert report["by_runtime"]["native-opencode"]["total_tokens"] == 120
    assert report["by_model_route"]["qwen-satellite"]["total_tokens"] == 120
    assert report["by_target"]["gpu-1"]["total_tokens"] == 120
    assert report["by_provider"]["ollama"]["total_tokens"] == 120
    assert report["by_model"]["qwen3-coder:30b-32k"]["total_tokens"] == 120
