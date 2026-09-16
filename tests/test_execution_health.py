from __future__ import annotations

from pathlib import Path

from execraft.execution_identity import ExecutionIdentity
from execraft.orchestrate.agent_attempt import AgentAttemptRunner
from execraft.orchestrate.artifacts import AgentArtifactStore
from execraft.orchestrate.contract_health import ContractHealthStore
from execraft.orchestrate.execution_health import ExecutionHealthStore
from execraft.orchestrate.invocations import AgentInvocationStore
from execraft.orchestrate.provider_health import ProviderHealthStore
from execraft.orchestrate.scheduler import (
    AgentCapability,
    AgentExecutionError,
    Availability,
    StructuredHandoff,
)
from execraft.runtime.contracts import RuntimeSessionRef


class _TargetAdapter:
    def __init__(self, *, fail: bool) -> None:
        self.fail = fail
        self.execution_identity = ExecutionIdentity(
            candidate_id="qwen-worker",
            runtime_id="native-opencode",
            runtime_backend="opencode",
            model_route_id="qwen-satellite",
            model_provider="ollama",
            model="qwen3-coder:30b-32k",
            target_id="satellite-gpu",
            target_kind="inference_endpoint",
            concurrency_group="satellite-gpu",
            legacy_provider_id="legacy-qwen-worker",
        )

    @property
    def provider_id(self) -> str:
        return self.execution_identity.provider_id

    @property
    def candidate_id(self) -> str:
        return self.execution_identity.candidate_id

    @property
    def availability(self) -> Availability:
        return Availability.AVAILABLE

    @property
    def capabilities(self) -> set[AgentCapability]:
        return {AgentCapability.IMPLEMENT}

    def execute(self, handoff: StructuredHandoff) -> dict:
        if self.fail:
            raise AgentExecutionError(
                "satellite connection reset",
                classification="network_transient",
                persistent=True,
            )
        return {"ok": True, "final_message": "done", "session_id": "session-1"}

    def session_ref_from_result(self, result: dict | None) -> RuntimeSessionRef | None:
        session_id = str((result or {}).get("session_id", ""))
        if not session_id:
            return None
        return RuntimeSessionRef(
            runtime_id=self.execution_identity.runtime_id,
            candidate_id=self.execution_identity.candidate_id,
            session_id=session_id,
            backend=self.execution_identity.runtime_backend,
        )


def _runner(tmp_path: Path):
    execution_health = ExecutionHealthStore(tmp_path / "execution-health.json")
    provider_health = ProviderHealthStore(tmp_path / "provider-health.json")
    runner = AgentAttemptRunner(
        agent_invocations=AgentInvocationStore(tmp_path / "invocations.sqlite3"),
        agent_artifacts=AgentArtifactStore(tmp_path / "artifacts"),
        contract_health=ContractHealthStore(tmp_path / "contract-health.json"),
        provider_health=provider_health,
        execution_health=execution_health,
        project_id="project",
        task_id="task",
        invocation_project_id="project",
    )
    return runner, execution_health, provider_health


def _execute(runner: AgentAttemptRunner, adapter: _TargetAdapter):
    return runner.execute_attempt(
        adapter=adapter,
        handoff=StructuredHandoff(
            work_package_id="wp1",
            stage="implement",
            summary="test target health",
        ),
        capability=AgentCapability.IMPLEMENT,
        package_id="wp1",
        stage="implement",
        shard_key="",
        attempt_number=1,
        parent_invocation_id="",
        triggering_event_id="",
        workspace_before_digest="before",
        workspace_after_digest_fn=lambda: "after",
        rendered_prompt="prompt",
        metadata={
            "adapter": "opencode",
            "model": "ollama-node/qwen3-coder:30b-32k",
            "execution_capabilities": {},
        },
        strict_checks=False,
        require_structured_output=False,
    )


def test_target_network_failure_does_not_poison_legacy_provider_health(tmp_path: Path) -> None:
    runner, execution_health, provider_health = _runner(tmp_path)

    result = _execute(runner, _TargetAdapter(fail=True))

    assert result.success is False
    assert result.execution_health is not None
    assert result.execution_health.dimension == "target"
    assert result.execution_health.identity == "satellite-gpu"
    assert not execution_health.get("target", "satellite-gpu").is_available
    assert provider_health.get("legacy-qwen-worker").is_available


def test_success_restores_dimensioned_health_and_persists_runtime_session(tmp_path: Path) -> None:
    runner, execution_health, _provider_health = _runner(tmp_path)
    _execute(runner, _TargetAdapter(fail=True))

    result = _execute(runner, _TargetAdapter(fail=False))

    assert result.success is True
    assert execution_health.get("target", "satellite-gpu").status == "available"
    invocation = result.invocation
    assert invocation.candidate_id == "qwen-worker"
    assert invocation.runtime_id == "native-opencode"
    assert invocation.model_route_id == "qwen-satellite"
    assert invocation.model_provider == "ollama"
    assert invocation.target_id == "satellite-gpu"
    assert invocation.runtime_session["session_id"] == "session-1"
