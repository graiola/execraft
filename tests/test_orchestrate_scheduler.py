"""Tests for agent scheduling, availability, handoffs, and failure classification."""

from execraft.orchestrate.scheduler import (
    AgentCapability,
    AgentSchedule,
    Availability,
    StructuredHandoff,
    build_agent_prompt,
    classify_failure,
    diagnose_failure,
    select_agent,
)
from execraft.orchestrate.scheduler import AgentAdapter


class _FakeAgent:
    def __init__(
        self,
        provider_id: str,
        caps: set[AgentCapability],
        availability: Availability = Availability.AVAILABLE,
        weight: int = 50,
        capability_weights: dict[AgentCapability, int] | None = None,
    ):
        self._id = provider_id
        self._caps = caps
        self._avail = availability
        self._weight = weight
        self._weights = dict(capability_weights or {})

    @property
    def availability(self) -> Availability:
        return self._avail

    @property
    def provider_id(self) -> str:
        return self._id

    @property
    def capabilities(self) -> set[AgentCapability]:
        return self._caps

    def weight_for_capability(self, capability: AgentCapability) -> int:
        return self._weights.get(capability, self._weight)

    def execute(self, handoff: StructuredHandoff) -> dict:
        return {"ok": True}


class _ConcreteAgentAdapter(AgentAdapter):
    """Concrete adapter for type checking in tests."""

    @property
    def availability(self) -> Availability:
        return Availability.AVAILABLE

    @property
    def provider_id(self) -> str:
        return "test-agent"

    @property
    def capabilities(self) -> set[AgentCapability]:
        return {AgentCapability.IMPLEMENT, AgentCapability.REVIEW}

    def execute(self, handoff: StructuredHandoff) -> dict:
        return {"handoff": handoff.work_package_id}


class TestAvailability:
    def test_values(self):
        assert Availability.AVAILABLE.value == "available"
        assert Availability.BUSY.value == "busy"
        assert Availability.DISABLED.value == "disabled"
        assert Availability.QUOTA_EXHAUSTED.value == "quota_exhausted"
        assert Availability.SESSION_LIMIT.value == "session_limit"
        assert Availability.NETWORK_TRANSIENT.value == "network_transient"


class TestSelectAgent:
    def test_selects_available_agent(self):
        slots = [
            _FakeAgent("agent-1", {AgentCapability.IMPLEMENT}),
            _FakeAgent("agent-2", {AgentCapability.REVIEW}),
        ]
        agent_id = select_agent(slots, AgentCapability.IMPLEMENT)
        assert agent_id == "agent-1"

    def test_selects_reviewer(self):
        slots = [
            _FakeAgent("agent-1", {AgentCapability.IMPLEMENT}),
            _FakeAgent("agent-2", {AgentCapability.REVIEW}),
        ]
        agent_id = select_agent(slots, AgentCapability.REVIEW)
        assert agent_id == "agent-2"

    def test_excludes_specified_agents(self):
        slots = [
            _FakeAgent("agent-1", {AgentCapability.REVIEW}),
            _FakeAgent("agent-2", {AgentCapability.REVIEW}),
        ]
        agent_id = select_agent(slots, AgentCapability.REVIEW, exclude_ids={"agent-1"})
        assert agent_id == "agent-2"

    def test_returns_none_when_no_available(self):
        slots = [
            _FakeAgent("agent-1", {AgentCapability.IMPLEMENT}, availability=Availability.BUSY),
        ]
        agent_id = select_agent(slots, AgentCapability.IMPLEMENT)
        assert agent_id is None

    def test_returns_none_when_no_capability(self):
        slots = [
            _FakeAgent("agent-1", {AgentCapability.IMPLEMENT}),
        ]
        agent_id = select_agent(slots, AgentCapability.REVIEW)
        assert agent_id is None

    def test_returns_none_when_all_excluded(self):
        slots = [
            _FakeAgent("agent-1", {AgentCapability.REVIEW}),
        ]
        agent_id = select_agent(slots, AgentCapability.REVIEW, exclude_ids={"agent-1"})
        assert agent_id is None

    def test_circular_selection_starts_after_previous_provider(self):
        slots = [
            _FakeAgent("codex", {AgentCapability.IMPLEMENT}),
            _FakeAgent("claude", {AgentCapability.IMPLEMENT}),
            _FakeAgent("opencode", {AgentCapability.IMPLEMENT}),
        ]

        assert (
            select_agent(
                slots,
                AgentCapability.IMPLEMENT,
                start_after_id="codex",
            )
            == "claude"
        )
        assert (
            select_agent(
                slots,
                AgentCapability.IMPLEMENT,
                start_after_id="opencode",
            )
            == "codex"
        )


class TestAgentSchedule:
    def test_reviewer_independent_by_default(self):
        schedule = AgentSchedule(implementer_id="a", reviewer_id="b")
        assert schedule.reviewer_is_independent() is True

    def test_reviewer_not_independent_when_same(self):
        schedule = AgentSchedule(implementer_id="a", reviewer_id="a")
        assert schedule.reviewer_is_independent() is False

    def test_final_reviewer_independence(self):
        schedule = AgentSchedule(implementer_id="a", reviewer_id="b", final_reviewer_id="c")
        assert schedule.final_reviewer_is_independent() is True

    def test_final_reviewer_not_independent_when_same_as_reviewer(self):
        schedule = AgentSchedule(implementer_id="a", reviewer_id="b", final_reviewer_id="b")
        assert schedule.final_reviewer_is_independent() is False

    def test_final_reviewer_not_independent_when_same_as_implementer(self):
        schedule = AgentSchedule(implementer_id="a", reviewer_id="b", final_reviewer_id="a")
        assert schedule.final_reviewer_is_independent() is False


class TestClassifyFailure:
    def test_auth_failure(self):
        assert classify_failure({"type": "auth_error"}) == "auth_failure"

    def test_quota_exhausted(self):
        assert classify_failure({"type": "quota_exceeded"}) == "quota_exhausted"

    def test_rate_limited(self):
        assert classify_failure({"message": "rate limit exceeded"}) == "rate_limited"

    def test_timeout(self):
        assert classify_failure({"type": "timeout"}) == "timeout"
        assert classify_failure({"message": "request timed out"}) == "timeout"

    def test_cli_usage_error_is_not_poisoned_by_timeout_help_flag(self):
        diagnosis = diagnose_failure(
            {
                "adapter": "antigravity-cli",
                "message": (
                    "flags provided but not defined: -o\n"
                    "Usage of agy:\n  --print-timeout duration"
                ),
            }
        )

        assert diagnosis.category == "configuration_error"
        assert diagnosis.persistent is True

    def test_tool_failure(self):
        assert classify_failure({"type": "tool_crash"}) == "tool_failure"

    def test_invalid_output(self):
        assert classify_failure({"type": "invalid_response"}) == "invalid_output"

    def test_product_verification(self):
        assert classify_failure({"type": "verify_error"}) == "product_verification_failure"

    def test_unclassified(self):
        assert classify_failure({"type": "unknown"}) == "unclassified"
        assert classify_failure({}) == "unclassified"

    def test_message_based_classification(self):
        assert classify_failure({"message": "authentication failed"}) == "auth_failure"
        assert classify_failure({"message": "quota exceeded for today"}) == "quota_exhausted"
        assert classify_failure({"message": "policy violation: cannot modify"}) == "policy_violation"

    def test_provider_diagnosis_preserves_retry_metadata(self):
        diagnosis = diagnose_failure(
            {
                "adapter": "claude-code",
                "message": "You've hit your session limit · resets 1:40am (Europe/Rome)",
            }
        )

        assert diagnosis.category == "session_limit"
        assert diagnosis.persistent is True
        assert diagnosis.retry_after_seconds is not None

    def test_network_diagnosis_is_transient_but_persisted_for_cooldown(self):
        diagnosis = diagnose_failure(
            {
                "adapter": "codex",
                "message": "stream disconnected before completion: Connection reset by peer",
            }
        )

        assert diagnosis.category == "network_transient"
        assert diagnosis.persistent is True


class TestStructuredHandoff:
    def test_create_handoff(self):
        handoff = StructuredHandoff(
            work_package_id="wp-1",
            stage="implement",
            summary="Implement the feature",
            repository_diff_summary="10 files changed",
            verification_summary="all tests pass",
            unresolved_findings=["Minor style issue"],
            relevant_decisions=["Use library X"],
        )
        assert handoff.work_package_id == "wp-1"
        assert handoff.stage == "implement"

    def test_as_mapping(self):
        handoff = StructuredHandoff(
            work_package_id="wp-1",
            stage="review",
            summary="Review feature",
        )
        mapping = handoff.as_mapping()
        assert mapping["work_package_id"] == "wp-1"
        assert mapping["stage"] == "review"
        assert mapping["unresolved_findings"] == []

    def test_output_target_and_hard_limit_are_distinct(self):
        handoff = StructuredHandoff(
            work_package_id="wp-1",
            stage="implement",
            summary="Implement feature",
            output_token_budget=100,
            output_token_target=100,
            output_token_hard_limit=250,
        )

        mapping = handoff.as_mapping()
        assert mapping["output_token_target"] == 100
        assert mapping["output_token_hard_limit"] == 250
        assert "output target of 100 tokens" in build_agent_prompt(handoff)


class TestAgentAdapterProtocol:
    def test_concrete_adapter(self):
        adapter = _ConcreteAgentAdapter()
        assert adapter.provider_id == "test-agent"
        assert AgentCapability.IMPLEMENT in adapter.capabilities
        handoff = StructuredHandoff(work_package_id="test", stage="implement", summary="test")
        result = adapter.execute(handoff)
        assert result["handoff"] == "test"


def test_invalid_structured_output_message_is_classified() -> None:
    assert (
        classify_failure({"message": "invalid structured output: missing verdict"})
        == "invalid_output"
    )


def test_semantic_output_hard_limit_message_is_classified() -> None:
    assert (
        classify_failure(
            {"message": "semantic output exceeded its hard token limit: hard_limit=20"}
        )
        == "output_budget_exceeded"
    )


class TestCapabilityWeightedSelection:
    def test_high_complexity_selects_strongest_even_after_weaker_cursor(self):
        slots = [
            _FakeAgent("codex", {AgentCapability.IMPLEMENT}, weight=100),
            _FakeAgent("antigravity", {AgentCapability.IMPLEMENT}, weight=90),
            _FakeAgent("opencode", {AgentCapability.IMPLEMENT}, weight=75),
        ]

        selected = select_agent(
            slots,
            AgentCapability.IMPLEMENT,
            start_after_id="codex",
            task_complexity=90,
            prefer_capable_agents=True,
        )

        assert selected == "codex"

    def test_high_complexity_failover_selects_next_strongest(self):
        slots = [
            _FakeAgent("codex", {AgentCapability.FIX_REVIEW}, weight=100),
            _FakeAgent("claude", {AgentCapability.FIX_REVIEW}, weight=95),
            _FakeAgent("antigravity", {AgentCapability.FIX_REVIEW}, weight=90),
        ]

        selected = select_agent(
            slots,
            AgentCapability.FIX_REVIEW,
            exclude_ids={"codex"},
            task_complexity=90,
            prefer_capable_agents=True,
        )

        assert selected == "claude"

    def test_medium_complexity_uses_strength_band_and_rotates(self):
        slots = [
            _FakeAgent("codex", {AgentCapability.REVIEW}, weight=100),
            _FakeAgent("claude", {AgentCapability.REVIEW}, weight=92),
            _FakeAgent("opencode", {AgentCapability.REVIEW}, weight=60),
        ]

        selected = select_agent(
            slots,
            AgentCapability.REVIEW,
            start_after_id="codex",
            task_complexity=55,
            prefer_capable_agents=True,
            capability_weight_band=10,
        )

        assert selected == "claude"

    def test_low_complexity_preserves_full_circular_queue(self):
        slots = [
            _FakeAgent("codex", {AgentCapability.IMPLEMENT}, weight=100),
            _FakeAgent("opencode", {AgentCapability.IMPLEMENT}, weight=50),
        ]

        selected = select_agent(
            slots,
            AgentCapability.IMPLEMENT,
            start_after_id="codex",
            task_complexity=20,
            prefer_capable_agents=True,
        )

        assert selected == "opencode"

    def test_per_capability_weight_is_used(self):
        slots = [
            _FakeAgent(
                "antigravity",
                {AgentCapability.IMPLEMENT, AgentCapability.REVIEW},
                weight=80,
                capability_weights={AgentCapability.REVIEW: 98},
            ),
            _FakeAgent("claude", {AgentCapability.IMPLEMENT, AgentCapability.REVIEW}, weight=95),
        ]

        assert select_agent(
            slots, AgentCapability.IMPLEMENT, task_complexity=90, prefer_capable_agents=True
        ) == "claude"
        assert select_agent(
            slots, AgentCapability.REVIEW, task_complexity=90, prefer_capable_agents=True
        ) == "antigravity"


def test_absolute_complexity_ceiling_excludes_weak_failover_agent() -> None:
    strong = _FakeAgent("strong", {AgentCapability.IMPLEMENT}, weight=100)
    weak = _FakeAgent("weak", {AgentCapability.IMPLEMENT}, weight=50)
    weak._execraft_max_complexity_by_capability = {AgentCapability.IMPLEMENT: 35}

    assert (
        select_agent(
            [strong, weak],
            AgentCapability.IMPLEMENT,
            exclude_ids={"strong"},
            task_complexity=80,
            prefer_capable_agents=True,
        )
        is None
    )
    assert (
        select_agent(
            [strong, weak],
            AgentCapability.IMPLEMENT,
            exclude_ids={"strong"},
            task_complexity=25,
            prefer_capable_agents=True,
        )
        == "weak"
    )


def test_package_preference_reorders_only_eligible_agents() -> None:
    slots = [
        _FakeAgent("first", {AgentCapability.REVIEW}),
        _FakeAgent("preferred", {AgentCapability.REVIEW}),
    ]

    assert (
        select_agent(
            slots,
            AgentCapability.REVIEW,
            preferred_ids=["preferred", "first"],
        )
        == "preferred"
    )


def test_package_preference_skips_unavailable_and_fails_over_in_rank_order() -> None:
    slots = [
        _FakeAgent(
            "offline",
            {AgentCapability.REVIEW},
            availability=Availability.BUSY,
        ),
        _FakeAgent("preferred", {AgentCapability.REVIEW}),
        _FakeAgent("fallback", {AgentCapability.REVIEW}),
    ]

    assert (
        select_agent(
            slots,
            AgentCapability.REVIEW,
            preferred_ids=["offline", "preferred", "fallback"],
        )
        == "preferred"
    )
    assert (
        select_agent(
            slots,
            AgentCapability.REVIEW,
            preferred_ids=["offline", "preferred", "fallback"],
            start_after_id="preferred",
        )
        == "fallback"
    )


def test_adapter_capabilities_make_read_only_guarantees_explicit():
    from execraft.orchestrate.scheduler import (
        AgentAdapterCapabilities,
        agent_adapter_capabilities,
    )

    class Legacy:
        streaming_interaction = True

    legacy = agent_adapter_capabilities(Legacy())
    assert legacy.read_only_enforcement == "provider_policy"
    assert legacy.streaming is True

    hard = AgentAdapterCapabilities(read_only_enforcement="hard")
    assert hard.structured_output_enforcement == "prompt_only"
    assert hard.satisfies_read_only("provider_policy") is True
    assert hard.satisfies_read_only("hard") is True
    assert AgentAdapterCapabilities(
        read_only_enforcement="advisory"
    ).satisfies_read_only("provider_policy") is False
    assert AgentAdapterCapabilities(
        structured_output=False,
        structured_output_enforcement="native_schema",
    ).structured_output_enforcement == "unsupported"


def test_handoff_attempt_snapshot_is_detached_and_hashable():
    original = StructuredHandoff(
        work_package_id="wp",
        stage="implement",
        summary="Do work",
        workflow_skills=[{"id": "ai-implement", "version": "1"}],
        execution_context={"workspace_digest": "before"},
    )
    attempt = original.for_attempt(2, parent_invocation_id="parent")
    attempt.workflow_skills[0]["version"] = "2"
    attempt.execution_context["workspace_digest"] = "after"

    assert original.workflow_skills[0]["version"] == "1"
    assert original.execution_context["workspace_digest"] == "before"
    assert attempt.attempt == 2
    assert attempt.parent_invocation_id == "parent"
    assert len(attempt.sha256()) == 64
