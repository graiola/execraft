"""Dedicated unit tests for ProviderFailoverCoordinator and provider_failover module."""

from dataclasses import replace
import pytest
from pathlib import Path

from execraft.orchestrate.agent_attempt import AgentAttemptRunner
from execraft.orchestrate.artifacts import AgentArtifactStore
from execraft.orchestrate.contract_health import ContractHealthStore
from execraft.orchestrate.invocations import AgentInvocationStore
from execraft.orchestrate.provider_failover import (
    FailoverOutcome,
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


@pytest.fixture
def tmp_state(tmp_path: Path):
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


def test_coordinator_module_imports():
    from execraft.orchestrate.provider_failover import (
        ProviderFailoverCoordinator as PFC1,
    )
    from execraft.orchestrate.provider_failover import (
        ProviderFailoverCoordinator as PFC2,
    )
    assert PFC1 is PFC2


def test_coordinator_succeeds_on_first_attempt(tmp_state):
    adapter = _ScriptedAdapter("provider-a", [{"ok": True, "status": "implemented"}])
    harness = _CoordinatorHarness(tmp_state, [adapter])

    outcome = harness.execute(handoff=_plain_handoff())

    assert isinstance(outcome, FailoverOutcome)
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


def test_coordinator_retries_same_provider_with_format_repair(tmp_state):
    invalid = {
        "ok": True,
        "final_message": "unstructured review prose",
        "invalid": "schema",
    }
    adapter = _ScriptedAdapter("provider-a", [invalid, {"ok": True, "status": "implemented", "summary": "s", "acceptance_evidence": []}])
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
    assert harness.repairs == [("provider-a", "unstructured review prose")]
    assert harness.transitions == [("agent_contract_retry", "provider-a", "provider-a")]


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
