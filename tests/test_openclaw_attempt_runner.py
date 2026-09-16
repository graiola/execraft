from pathlib import Path

from execraft.orchestrate.agent_attempt import AgentAttemptRunner
from execraft.orchestrate.artifacts import AgentArtifactStore
from execraft.orchestrate.contract_health import ContractHealthStore
from execraft.orchestrate.execution_health import ExecutionHealthStore
from execraft.orchestrate.invocations import AgentInvocationStore
from execraft.orchestrate.provider_health import ProviderHealthStore
from execraft.orchestrate.scheduler import AgentCapability
from tests.test_openclaw_agent_runtime import _request, _runtime


def test_agent_attempt_runner_persists_openclaw_dimensions_usage_and_session(tmp_path: Path):
    runtime, _service = _runtime(tmp_path)
    request = _request(tmp_path, runtime)
    runner = AgentAttemptRunner(
        agent_invocations=AgentInvocationStore(tmp_path / "invocations.db"),
        agent_artifacts=AgentArtifactStore(tmp_path / "artifacts"),
        contract_health=ContractHealthStore(tmp_path / "contract-health.json"),
        provider_health=ProviderHealthStore(tmp_path / "provider-health.json"),
        execution_health=ExecutionHealthStore(tmp_path / "execution-health.json"),
        project_id="project",
        task_id="task",
        invocation_project_id="project",
    )

    outcome = runner.execute_attempt(
        adapter=runtime,
        handoff=request.handoff,
        capability=AgentCapability.IMPLEMENT,
        package_id="WP7",
        stage="implementation",
        shard_key="",
        attempt_number=1,
        parent_invocation_id="",
        triggering_event_id="event-wp7",
        workspace_before_digest="before",
        workspace_after_digest_fn=lambda: "after",
        rendered_prompt="cold prompt",
        metadata={
            "adapter": "openclaw",
            "model": "qwen3-coder",
            "execution_capabilities": runtime.execution_capabilities.as_mapping(),
        },
        strict_checks=False,
        require_structured_output=False,
    )

    assert outcome.success is True
    record = outcome.invocation
    assert record.candidate_id == "implementer"
    assert record.runtime_id == "openclaw-managed"
    assert record.runtime_backend == "gateway"
    assert record.model_route_id == "qwen-route"
    assert record.model_provider == "ollama"
    assert record.target_id == "local-ollama"
    assert record.runtime_session["session_id"] == "agent:implementer:cold-1"
    assert record.runtime_session["backend"] == "gateway-session-key"
    assert record.usage["provider"] == "ollama"
    assert record.usage["input_tokens"] == 101
    assert outcome.result["_execraft_runtime_session"]["runtime_id"] == "openclaw-managed"
