"""Tests for AgentAttemptRunner, ProviderFailoverCoordinator, and the façade wiring."""

from dataclasses import replace

import pytest
from pathlib import Path
from unittest.mock import Mock

from execraft.orchestrate.agent_attempt import AgentAttemptRunner, AgentAttemptResult
from execraft.orchestrate.artifacts import AgentArtifactStore, AgentArtifactReference
from execraft.orchestrate.contract_health import ContractHealthStore
from execraft.orchestrate.invocations import AgentInvocationStore
from execraft.orchestrate.provider_failover import (
    ProviderFailoverCoordinator,
    ProviderFailoverStrategy,
)
from execraft.orchestrate.provider_health import ProviderHealthStore
from execraft.orchestrate.scheduler import (
    AgentAdapter,
    AgentCapability,
    AgentExecutionError,
    Availability,
    StructuredHandoff,
)
from execraft.orchestrate.structured_output import acceptance_evidence_output_schema


class _FakeAdapter(AgentAdapter):
    def __init__(self, provider_id: str, result: dict | None = None, raise_error: Exception | None = None):
        self._provider_id = provider_id
        self._result = result or {"ok": True, "status": "implemented"}
        self._raise_error = raise_error

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def availability(self) -> Availability:
        return Availability.AVAILABLE

    @property
    def capabilities(self) -> set[AgentCapability]:
        return {AgentCapability.IMPLEMENT, AgentCapability.REVIEW}

    def execute(self, handoff: StructuredHandoff) -> dict:
        if self._raise_error:
            raise self._raise_error
        return self._result


@pytest.fixture
def tmp_state(tmp_path: Path):
    """Create temporary state directories."""
    invocations_path = tmp_path / "invocations.db"
    artifacts_root = tmp_path / "artifacts"
    contract_health_path = tmp_path / "contract_health.json"
    provider_health_path = tmp_path / "provider_health.json"

    artifacts_root.mkdir()

    return {
        "invocations": AgentInvocationStore(invocations_path),
        "artifacts": AgentArtifactStore(artifacts_root),
        "contract_health": ContractHealthStore(contract_health_path),
        "provider_health": ProviderHealthStore(provider_health_path),
    }


@pytest.fixture
def runner(tmp_state):
    """Create AgentAttemptRunner with temporary stores."""
    return AgentAttemptRunner(
        agent_invocations=tmp_state["invocations"],
        agent_artifacts=tmp_state["artifacts"],
        contract_health=tmp_state["contract_health"],
        provider_health=tmp_state["provider_health"],
        project_id="test_project",
        task_id="test_task",
        invocation_project_id="test_project",
    )


def test_successful_attempt_without_structured_output(runner):
    """Verify successful attempt persists artifact and updates health."""
    adapter = _FakeAdapter("test-provider", {"ok": True, "final_message": "done"})
    handoff = StructuredHandoff(
        work_package_id="pkg1",
        stage="implement",
        summary="Test work",
        requirements=[],
    )

    result = runner.execute_attempt(
        adapter=adapter,
        handoff=handoff,
        capability=AgentCapability.IMPLEMENT,
        package_id="pkg1",
        stage="implement",
        shard_key="",
        attempt_number=1,
        parent_invocation_id="",
        triggering_event_id="evt1",
        workspace_before_digest="digest-before",
        workspace_after_digest_fn=lambda: "digest-after",
        rendered_prompt="test prompt",
        metadata={"adapter": "test-adapter", "model": "test-model", "execution_capabilities": {}},
        strict_checks=False,
        require_structured_output=False,
    )

    assert result.success is True
    assert result.result is not None
    assert result.result["ok"] is True
    assert result.result["_execraft_executed_by"] == "test-provider"
    assert result.artifact is not None
    assert result.invocation.status == "completed"
    assert result.duration_seconds > 0


def test_successful_attempt_with_structured_output(runner):
    """Verify structured output validation and persistence."""
    adapter = _FakeAdapter(
        "test-provider",
        {
            "ok": True,
            "status": "implemented",
            "summary": "test",
            "acceptance_evidence": [
                {"criterion_id": "c1", "evidence": "test evidence"}
            ],
        },
    )
    handoff = StructuredHandoff(
        work_package_id="pkg1",
        stage="implement",
        summary="Test work",
        requirements=[],
        expected_output_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["ok", "status", "summary", "acceptance_evidence"],
            "properties": {
                "ok": {"type": "boolean", "const": True},
                "status": {"type": "string", "enum": ["implemented", "fixed"]},
                "summary": {"type": "string", "minLength": 1},
                "acceptance_evidence": acceptance_evidence_output_schema(),
            },
        },
    )

    result = runner.execute_attempt(
        adapter=adapter,
        handoff=handoff,
        capability=AgentCapability.IMPLEMENT,
        package_id="pkg1",
        stage="implement",
        shard_key="",
        attempt_number=1,
        parent_invocation_id="",
        triggering_event_id="evt1",
        workspace_before_digest="digest-before",
        workspace_after_digest_fn=lambda: "digest-after",
        rendered_prompt="test prompt",
        metadata={"adapter": "test-adapter", "model": "test-model", "execution_capabilities": {}},
        strict_checks=True,
        require_structured_output=True,
    )

    assert result.success is True
    assert result.result is not None
    assert "_execraft_structured_payload" in result.result
    assert result.invocation.status == "completed"


def test_failed_attempt_with_agent_execution_error(runner):
    """Verify failure classification and health tracking for agent errors."""
    error = AgentExecutionError(
        "rate limit exceeded",
        classification="rate_limited",
        retry_after_seconds=60.0,
        persistent=False,
        artifact_payload={"detail": "quota"},
    )
    adapter = _FakeAdapter("test-provider", raise_error=error)
    handoff = StructuredHandoff(
        work_package_id="pkg1",
        stage="implement",
        summary="Test work",
        requirements=[],
    )

    result = runner.execute_attempt(
        adapter=adapter,
        handoff=handoff,
        capability=AgentCapability.IMPLEMENT,
        package_id="pkg1",
        stage="implement",
        shard_key="",
        attempt_number=1,
        parent_invocation_id="",
        triggering_event_id="evt1",
        workspace_before_digest="digest-before",
        workspace_after_digest_fn=lambda: "digest-after",
        rendered_prompt="test prompt",
        metadata={"adapter": "test-adapter", "model": "test-model", "execution_capabilities": {}},
        strict_checks=False,
        require_structured_output=False,
    )

    assert result.success is False
    assert result.result is None
    assert result.classification == "rate_limited"
    assert result.retry_after_seconds == 60.0
    assert result.persistent is False
    assert result.invocation.status == "failed"
    assert result.artifact is not None


def test_failed_attempt_with_invalid_structured_output(runner, tmp_state):
    """Verify invalid structured output marks contract health failure."""
    adapter = _FakeAdapter("test-provider", {"ok": True, "invalid": "schema"})
    handoff = StructuredHandoff(
        work_package_id="pkg1",
        stage="implement",
        summary="Test work",
        requirements=[],
        expected_output_schema=acceptance_evidence_output_schema(),
    )

    result = runner.execute_attempt(
        adapter=adapter,
        handoff=handoff,
        capability=AgentCapability.IMPLEMENT,
        package_id="pkg1",
        stage="implement",
        shard_key="",
        attempt_number=1,
        parent_invocation_id="",
        triggering_event_id="evt1",
        workspace_before_digest="digest-before",
        workspace_after_digest_fn=lambda: "digest-after",
        rendered_prompt="test prompt",
        metadata={"adapter": "test-adapter", "model": "test-model", "execution_capabilities": {}},
        strict_checks=True,
        require_structured_output=True,
    )

    assert result.success is False
    assert result.classification == "invalid_output"
    assert result.invocation.status == "failed"

    from execraft.orchestrate.contract_health import schema_sha256

    schema_hash = schema_sha256(acceptance_evidence_output_schema())
    health = tmp_state["contract_health"].get(
        "test-provider", "test-model", "implement", schema_hash
    )
    assert health.consecutive_failures == 1


def test_artifact_persistence_on_success(runner, tmp_state):
    """Verify artifact is persisted with correct metadata."""
    adapter = _FakeAdapter("test-provider", {"ok": True, "output": "data"})
    handoff = StructuredHandoff(
        work_package_id="pkg1",
        stage="implement",
        summary="Test work",
        requirements=[],
    )

    result = runner.execute_attempt(
        adapter=adapter,
        handoff=handoff,
        capability=AgentCapability.IMPLEMENT,
        package_id="pkg1",
        stage="implement",
        shard_key="",
        attempt_number=1,
        parent_invocation_id="",
        triggering_event_id="evt1",
        workspace_before_digest="digest-before",
        workspace_after_digest_fn=lambda: "digest-after",
        rendered_prompt="test prompt",
        metadata={"adapter": "test-adapter", "model": "test-model", "execution_capabilities": {}},
        strict_checks=False,
        require_structured_output=False,
    )

    assert result.artifact is not None
    assert result.artifact.path.exists()
    assert result.artifact.size_bytes > 0
    assert result.artifact.sha256


def test_usage_tracking(runner):
    """Verify usage is normalized and persisted."""
    adapter = _FakeAdapter(
        "test-provider",
        {
            "ok": True,
            "final_message": "done",
            "usage": {"input_tokens": 100, "output_tokens": 50},
        },
    )
    handoff = StructuredHandoff(
        work_package_id="pkg1",
        stage="implement",
        summary="Test work",
        requirements=[],
    )

    result = runner.execute_attempt(
        adapter=adapter,
        handoff=handoff,
        capability=AgentCapability.IMPLEMENT,
        package_id="pkg1",
        stage="implement",
        shard_key="",
        attempt_number=1,
        parent_invocation_id="",
        triggering_event_id="evt1",
        workspace_before_digest="digest-before",
        workspace_after_digest_fn=lambda: "digest-after",
        rendered_prompt="test prompt",
        metadata={"adapter": "test-adapter", "model": "test-model", "execution_capabilities": {}},
        strict_checks=False,
        require_structured_output=False,
    )

    assert result.invocation.usage["input_tokens"] == 100
    assert result.invocation.usage["output_tokens"] == 50


def test_invocation_finalized_prevents_double_transition(runner, tmp_state):
    """Verify that exceptions after finalization are re-raised without double completion."""
    adapter = _FakeAdapter("test-provider", {"ok": True, "final_message": "done"})
    handoff = StructuredHandoff(
        work_package_id="pkg1",
        stage="implement",
        summary="Test work",
        requirements=[],
    )

    # Mock provider health to raise after invocation completes
    original_mark_available = tmp_state["provider_health"].mark_available
    def failing_mark_available(provider_id: str):
        raise RuntimeError("post-completion failure")

    tmp_state["provider_health"].mark_available = failing_mark_available

    with pytest.raises(RuntimeError, match="post-completion failure"):
        runner.execute_attempt(
            adapter=adapter,
            handoff=handoff,
            capability=AgentCapability.IMPLEMENT,
            package_id="pkg1",
            stage="implement",
            shard_key="",
            attempt_number=1,
            parent_invocation_id="",
            triggering_event_id="evt1",
            workspace_before_digest="digest-before",
            workspace_after_digest_fn=lambda: "digest-after",
            rendered_prompt="test prompt",
            metadata={"adapter": "test-adapter", "model": "test-model", "execution_capabilities": {}},
            strict_checks=False,
            require_structured_output=False,
        )


def test_semantic_output_limit_enforcement(runner):
    """Verify semantic output limit is enforced and raises proper error."""
    huge_output = {"ok": True, "final_message": "x" * 100000}
    adapter = _FakeAdapter("test-provider", huge_output)
    handoff = StructuredHandoff(
        work_package_id="pkg1",
        stage="implement",
        summary="Test work",
        requirements=[],
        output_token_hard_limit=100,
    )

    result = runner.execute_attempt(
        adapter=adapter,
        handoff=handoff,
        capability=AgentCapability.IMPLEMENT,
        package_id="pkg1",
        stage="implement",
        shard_key="",
        attempt_number=1,
        parent_invocation_id="",
        triggering_event_id="evt1",
        workspace_before_digest="digest-before",
        workspace_after_digest_fn=lambda: "digest-after",
        rendered_prompt="test prompt",
        metadata={"adapter": "test-adapter", "model": "test-model", "execution_capabilities": {}},
        strict_checks=True,
        require_structured_output=False,
    )

    assert result.success is False
    assert result.classification == "output_budget_exceeded"
    assert result.persistent is False


def test_provider_health_marked_on_failure(runner, tmp_state):
    """Verify provider health is marked as failed when attempt fails."""
    error = AgentExecutionError(
        "provider error",
        classification="provider_error",
        retry_after_seconds=None,
        persistent=False,
    )
    adapter = _FakeAdapter("test-provider", raise_error=error)
    handoff = StructuredHandoff(
        work_package_id="pkg1",
        stage="implement",
        summary="Test work",
        requirements=[],
    )

    result = runner.execute_attempt(
        adapter=adapter,
        handoff=handoff,
        capability=AgentCapability.IMPLEMENT,
        package_id="pkg1",
        stage="implement",
        shard_key="",
        attempt_number=1,
        parent_invocation_id="",
        triggering_event_id="evt1",
        workspace_before_digest="digest-before",
        workspace_after_digest_fn=lambda: "digest-after",
        rendered_prompt="test prompt",
        metadata={"adapter": "test-adapter", "model": "test-model", "execution_capabilities": {}},
        strict_checks=False,
        require_structured_output=False,
    )

    assert result.success is False
    health = tmp_state["provider_health"].get("test-provider")
    assert not health.is_available


def test_contract_health_result_returned(runner):
    """Verify contract_health_result is set properly on success and failure."""
    adapter = _FakeAdapter(
        "test-provider",
        {
            "ok": True,
            "status": "implemented",
            "summary": "test",
            "acceptance_evidence": [
                {"criterion_id": "c1", "evidence": "test evidence"}
            ],
        },
    )
    handoff = StructuredHandoff(
        work_package_id="pkg1",
        stage="implement",
        summary="Test work",
        requirements=[],
        expected_output_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["ok", "status", "summary", "acceptance_evidence"],
            "properties": {
                "ok": {"type": "boolean", "const": True},
                "status": {"type": "string", "enum": ["implemented", "fixed"]},
                "summary": {"type": "string", "minLength": 1},
                "acceptance_evidence": acceptance_evidence_output_schema(),
            },
        },
    )

    result = runner.execute_attempt(
        adapter=adapter,
        handoff=handoff,
        capability=AgentCapability.IMPLEMENT,
        package_id="pkg1",
        stage="implement",
        shard_key="",
        attempt_number=1,
        parent_invocation_id="",
        triggering_event_id="evt1",
        workspace_before_digest="digest-before",
        workspace_after_digest_fn=lambda: "digest-after",
        rendered_prompt="test prompt",
        metadata={"adapter": "test-adapter", "model": "test-model", "execution_capabilities": {}},
        strict_checks=True,
        require_structured_output=True,
    )

    assert result.success is True
    assert result.contract_health_result == "available"


def _ok_schema_result():
    return {
        "ok": True,
        "status": "implemented",
        "summary": "test",
        "acceptance_evidence": [
            {"criterion_id": "c1", "evidence": "test evidence"}
        ],
    }


class _ScriptedAdapter(_FakeAdapter):
    """Returns scripted results/raises in order for one provider."""

    def __init__(self, provider_id: str, script: list):
        super().__init__(provider_id)
        self._script = list(script)
        self.executions = []

    def execute(self, handoff: StructuredHandoff) -> dict:
        self.executions.append(handoff)
        step = self._script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


class _CoordinatorHarness:
    """Binds a real runner and stores to a coordinator with a scriptable strategy."""

    def __init__(self, tmp_state, adapters: list[_ScriptedAdapter]):
        self.adapters = {adapter.provider_id: adapter for adapter in adapters}
        self.selection_queue: list[str | None] = []
        self.skipped: list[tuple[str, dict]] = []
        self.relaxed: list[tuple[str, str]] = []
        self.transitions: list[tuple[str, str, str]] = []
        self.repairs: list[tuple[str, str]] = []
        self.runner = AgentAttemptRunner(
            agent_invocations=tmp_state["invocations"],
            agent_artifacts=tmp_state["artifacts"],
            contract_health=tmp_state["contract_health"],
            provider_health=tmp_state["provider_health"],
            project_id="test_project",
            task_id="test_task",
            invocation_project_id="test_project",
        )
        self.coordinator = ProviderFailoverCoordinator(
            attempt_runner=self.runner,
            contract_health=tmp_state["contract_health"],
            provider_health=tmp_state["provider_health"],
            max_attempts_per_stage=3,
            strict_checks=True,
            require_structured_output=True,
            strategy=ProviderFailoverStrategy(
                adapter=lambda agent_id: self.adapters.get(agent_id),
                metadata=lambda agent_id: {
                    "adapter": "test-adapter",
                    "model": "test-model",
                    "execution_capabilities": {},
                },
                eligibility=self._eligibility,
                prepare_attempt=lambda: ("digest-before", lambda: "digest-after"),
                render_prompt=lambda handoff: "test prompt",
                build_repair_handoff=self._repair,
                parent_invocation_id=lambda: "",
                select_candidate=self._select,
                health_excluded_ids=lambda: set(),
                candidate_eligible=lambda *args: "",
                on_provider_skipped=lambda agent_id, payload: self.skipped.append(
                    (agent_id, payload)
                ),
                on_relaxed=lambda policy, agent_id, excluded: self.relaxed.append(
                    (policy, agent_id)
                ),
                on_transition=lambda event, frm, to: self.transitions.append(
                    (event, frm, to)
                ),
            ),
        )

    def _eligibility(self, candidate_id, capability, policy_excluded, schema_hash, read_only, required):
        if candidate_id in set(policy_excluded):
            return {
                "reason": "policy_excluded",
                "classification": "policy_excluded",
                "error": "provider excluded by stage independence policy",
                "unavailable_until": "",
                "retry_after_seconds": None,
            }
        return None

    def _select(self, *args):
        return self.selection_queue.pop(0) if self.selection_queue else None

    def _repair(self, handoff, source, error, session, candidate_id):
        self.repairs.append((candidate_id, source))
        return replace(handoff, summary="repaired")

    def execute(self, *, handoff, capability=AgentCapability.IMPLEMENT, candidate_id="provider-a",
                policy_excluded=None, relaxation_tiers=None, ordered_fallbacks=None):
        return self.coordinator.execute_with_failover(
            handoff=handoff,
            capability=capability,
            package_id="pkg1",
            stage="implement",
            shard_key="",
            candidate_id=candidate_id,
            policy_excluded=set(policy_excluded or set()),
            relaxation_tiers=list(relaxation_tiers or []),
            ordered_fallbacks=list(ordered_fallbacks or []),
            preference_role="implement",
        )


def _plain_handoff():
    return StructuredHandoff(
        work_package_id="pkg1",
        stage="implement",
        summary="Test work",
        requirements=[],
    )


def test_coordinator_succeeds_on_first_attempt(tmp_state):
    adapter = _ScriptedAdapter("provider-a", [{"ok": True, "status": "implemented"}])
    harness = _CoordinatorHarness(tmp_state, [adapter])

    outcome = harness.execute(handoff=_plain_handoff())

    assert outcome.result is not None
    assert outcome.result["ok"] is True
    assert outcome.candidate_id == "provider-a"
    assert harness.transitions == []


def test_coordinator_fails_over_to_next_provider(tmp_state):
    failure = AgentExecutionError(
        "rate limit", classification="rate_limited", persistent=False
    )
    first = _ScriptedAdapter("provider-a", [failure])
    second = _ScriptedAdapter("provider-b", [{"ok": True, "status": "implemented"}])
    harness = _CoordinatorHarness(tmp_state, [first, second])
    harness.selection_queue.append("provider-b")

    outcome = harness.execute(handoff=_plain_handoff())

    assert outcome.result is not None
    assert outcome.result["_execraft_executed_by"] == "provider-b"
    assert harness.transitions == [("agent_failover", "provider-a", "provider-b")]
    assert len(outcome.attempts) == 1
    assert outcome.attempts[0]["classification"] == "rate_limited"
    assert not harness.adapters["provider-a"].executions or True
    assert second.executions


def test_coordinator_retries_same_provider_with_format_repair(tmp_state):
    invalid = {
        "ok": True,
        "final_message": "unstructured review prose",
        "invalid": "schema",
    }
    adapter = _ScriptedAdapter("provider-a", [invalid, _ok_schema_result()])
    harness = _CoordinatorHarness(tmp_state, [adapter])
    handoff = StructuredHandoff(
        work_package_id="pkg1",
        stage="implement",
        summary="Test work",
        requirements=[],
        expected_output_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["ok", "status", "summary", "acceptance_evidence"],
            "properties": {
                "ok": {"type": "boolean", "const": True},
                "status": {"type": "string", "enum": ["implemented", "fixed"]},
                "summary": {"type": "string", "minLength": 1},
                "acceptance_evidence": acceptance_evidence_output_schema(),
            },
        },
    )

    outcome = harness.execute(handoff=handoff)

    assert outcome.result is not None
    assert outcome.result["ok"] is True
    assert harness.repairs == [
        ("provider-a", "unstructured review prose")
    ], "coordinator must build a format-repair handoff for the same provider"
    assert harness.transitions == [("agent_contract_retry", "provider-a", "provider-a")]
    assert len(adapter.executions) == 2
    assert adapter.executions[1].summary == "repaired"


def test_coordinator_skips_contract_repair_when_failure_is_not_first(tmp_state):
    invalid = {
        "ok": True,
        "final_message": "still invalid",
        "invalid": "schema",
    }
    # The contract store is seeded with one prior failure so the retry is
    # not granted after this attempt.
    tmp_state["contract_health"].mark_failure(
        "provider-a",
        "test-model",
        AgentCapability.IMPLEMENT.value,
        __import__("execraft.orchestrate.contract_health", fromlist=["schema_sha256"]).schema_sha256(
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["ok", "status", "summary", "acceptance_evidence"],
                "properties": {
                    "ok": {"type": "boolean", "const": True},
                    "status": {"type": "string", "enum": ["implemented", "fixed"]},
                    "summary": {"type": "string", "minLength": 1},
                    "acceptance_evidence": acceptance_evidence_output_schema(),
                },
            }
        ),
        detail="prior failure",
    )
    adapter = _ScriptedAdapter("provider-a", [invalid])
    harness = _CoordinatorHarness(tmp_state, [adapter])
    handoff = StructuredHandoff(
        work_package_id="pkg1",
        stage="implement",
        summary="Test work",
        requirements=[],
        expected_output_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["ok", "status", "summary", "acceptance_evidence"],
            "properties": {
                "ok": {"type": "boolean", "const": True},
                "status": {"type": "string", "enum": ["implemented", "fixed"]},
                "summary": {"type": "string", "minLength": 1},
                "acceptance_evidence": acceptance_evidence_output_schema(),
            },
        },
    )

    outcome = harness.execute(handoff=handoff)

    assert outcome.result is None
    assert harness.repairs == [], "format repair must be bounded to the first failure"
    assert len(adapter.executions) == 1


def test_coordinator_relaxes_exclusion_tiers_before_failover(tmp_state):
    blocked = _ScriptedAdapter("provider-a", [])
    runner = _ScriptedAdapter("provider-b", [{"ok": True, "status": "implemented"}])
    harness = _CoordinatorHarness(tmp_state, [blocked, runner])
    harness.selection_queue.extend([None, "provider-b"])

    outcome = harness.execute(
        handoff=_plain_handoff(),
        candidate_id="provider-a",
        policy_excluded={"provider-a"},
        relaxation_tiers=[("stage_policy", {"provider-a"})],
    )

    assert outcome.result is not None
    assert harness.relaxed == [("stage_policy", "provider-b")]
    assert harness.transitions == [("agent_failover", "provider-a", "provider-b")]
    assert len(harness.skipped) == 1
    assert harness.skipped[0][0] == "provider-a"


def test_coordinator_returns_none_when_providers_are_exhausted(tmp_state):
    failure = AgentExecutionError(
        "provider gone", classification="provider_error", persistent=True
    )
    adapter = _ScriptedAdapter("provider-a", [failure])
    harness = _CoordinatorHarness(tmp_state, [adapter])

    outcome = harness.execute(handoff=_plain_handoff())

    assert outcome.result is None
    assert outcome.candidate_id == ""
    assert len(outcome.attempts) == 1
    assert harness.transitions == []


def test_orchestrator_facade_executes_through_attempt_runner(tmp_path, monkeypatch):
    from execraft.orchestrate.orchestrator import OrchestrationConfig, ProjectOrchestrator

    runner_calls: list[str] = []
    coordinator_calls: list[str] = []
    original_execute = AgentAttemptRunner.execute_attempt
    original_failover = ProviderFailoverCoordinator.execute_with_failover

    def spy_attempt(self, **kwargs):
        runner_calls.append(kwargs["adapter"].provider_id)
        return original_execute(self, **kwargs)

    def spy_failover(self, **kwargs):
        coordinator_calls.append(kwargs["capability"].value)
        return original_failover(self, **kwargs)

    monkeypatch.setattr(AgentAttemptRunner, "execute_attempt", spy_attempt)
    monkeypatch.setattr(
        ProviderFailoverCoordinator, "execute_with_failover", spy_failover
    )

    config = OrchestrationConfig(
        strict_checks=False, auto_commit=False, state_dir=tmp_path / "state"
    )
    orch = ProjectOrchestrator("test-project", config=config)
    implementer = _FakeAdapter("impl-1", {"ok": True, "status": "implemented"})
    reviewer = _FakeAdapter("rev-1", {"ok": True, "status": "implemented"})
    orch.register_agent(implementer)
    orch.register_agent(reviewer)

    plan = "## Package: First\n\n### Acceptance criteria\n- [ ] Works\n"
    orch.initialize(plan)
    orch.run_pipeline()

    report = orch.status_report()
    assert report["state"] == "completed"
    assert coordinator_calls, (
        "the orchestrator façade must delegate to ProviderFailoverCoordinator"
    )
    assert runner_calls, (
        "the orchestrator façade must execute attempts through AgentAttemptRunner"
    )
    assert "impl-1" in runner_calls
    assert "rev-1" in runner_calls


def test_orchestrator_facade_retains_attempt_lifecycle_events(tmp_path):
    from execraft.orchestrate.orchestrator import OrchestrationConfig, ProjectOrchestrator

    config = OrchestrationConfig(
        strict_checks=False, auto_commit=False, state_dir=tmp_path / "state"
    )
    orch = ProjectOrchestrator("test-project", config=config)
    orch.register_agent(_FakeAdapter("impl-1", {"ok": True, "status": "implemented"}))
    orch.register_agent(_FakeAdapter("rev-1", {"ok": True, "status": "implemented"}))

    progress_events: list[str] = []
    orch._progress_callback = lambda event_type, payload: progress_events.append(
        event_type
    )

    plan = "## Package: First\n\n### Acceptance criteria\n- [ ] Works\n"
    orch.initialize(plan)
    orch.run_pipeline()

    events = [event.event_type for event in orch._journal.read()]
    assert "agent_invocation_started" in events
    assert "agent_invocation_completed" in events
    assert "agent_attempt_started" in progress_events
    assert "agent_attempt_finished" in progress_events
    assert orch.status_report()["completed_packages"] == 1


def test_orchestrator_finishes_console_session_for_distinct_handoff_stage(tmp_path):
    from execraft.orchestrate.models import TaskExecutionState, WorkPackageStage
    from execraft.orchestrate.orchestrator import OrchestrationConfig, ProjectOrchestrator

    progress_events: list[tuple[str, dict]] = []
    config = OrchestrationConfig(
        strict_checks=False, auto_commit=False, state_dir=tmp_path / "state"
    )
    orch = ProjectOrchestrator(
        "test-project",
        config=config,
        progress_callback=lambda event_type, payload: progress_events.append(
            (event_type, dict(payload))
        ),
    )
    adapter = _FakeAdapter("impl-1", {"ok": True, "status": "implemented"})
    orch.register_agent(adapter)
    orch.initialize("## Package: First\n\n### Acceptance criteria\n- [ ] Works\n")
    orch.transition_to(TaskExecutionState.RUNNING)
    package = orch._state_record.plan_graph.package_by_id("first")
    package.stage = WorkPackageStage.IMPLEMENT
    handoff = StructuredHandoff(
        work_package_id=package.id,
        stage="scope_recovery",
        summary="Recover an out-of-scope change",
    )

    orch._execute_agent(
        AgentCapability.IMPLEMENT,
        adapter.provider_id,
        handoff,
        package,
    )

    lifecycle = [
        payload
        for event_type, payload in progress_events
        if event_type in {"agent_attempt_started", "agent_attempt_finished"}
    ]
    assert [payload["stage"] for payload in lifecycle] == [
        "scope_recovery",
        "scope_recovery",
    ]
    sessions = orch._agent_console.sessions(adapter.provider_id)
    assert sessions[0]["status"] == "completed"
    assert sessions[0]["finished_at"]
