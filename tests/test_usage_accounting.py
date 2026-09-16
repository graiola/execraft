from __future__ import annotations

from pathlib import Path

from execraft.orchestrate.invocations import AgentInvocationStore
from execraft.orchestrate.usage import normalize_agent_usage


def test_normalizes_claude_cache_tokens_without_double_counting() -> None:
    usage = normalize_agent_usage(
        {
            "usage": {
                "input_tokens": 100,
                "cache_creation_input_tokens": 20,
                "cache_read_input_tokens": 300,
                "output_tokens": 50,
                "cost_usd": 0.25,
            }
        },
        prompt="prompt",
        provider="claude-code",
        model="claude",
        capability="review",
        package_id="WP10",
    )

    assert usage.total_tokens == 470
    assert usage.cached_input_tokens == 300
    assert usage.cache_creation_tokens == 20
    assert usage.reported_cost == 0.25
    assert usage.input_measurement == "reported"


def test_normalizes_codex_reported_total_and_persists_usage_summary(tmp_path: Path) -> None:
    store = AgentInvocationStore(tmp_path / "invocations.sqlite3")
    record = store.begin(
        project_id="task",
        package_id="WP10",
        stage="implement",
        capability="implement",
        attempt=1,
        agent_id="codex",
        adapter="codex-cli",
        model="gpt",
        handoff={
            "context_manifest": [
                {"type": "capsule", "estimated_tokens": 120},
                {"type": "workspace", "estimated_tokens": 80},
            ]
        },
    )
    usage = normalize_agent_usage(
        {
            "usage": {
                "input_tokens": 1000,
                "cached_input_tokens": 700,
                "output_tokens": 100,
                "reasoning_tokens": 25,
                "total_tokens": 1125,
            }
        },
        prompt="x" * 100,
        provider="codex-cli",
        model="gpt",
        capability="implement",
        package_id="WP10",
        invocation_id=record.invocation_id,
    ).as_mapping()
    store.complete(record.invocation_id, duration_seconds=1.0, usage=usage)

    summary = store.usage_summary("task", package_id="WP10")

    assert summary["totals"]["total_tokens"] == 1125
    assert summary["by_provider"]["codex-cli"]["cached_input_tokens"] == 700
    assert summary["by_capability"]["implement"]["invocations"] == 1
    assert summary["context_block_tokens"] == {"capsule": 120, "workspace": 80}


def test_partial_usage_uses_estimated_input_in_effective_total() -> None:
    usage = normalize_agent_usage(
        {"usage": {"output_tokens": 10}},
        prompt="large prompt " * 400,
        provider="provider-with-partial-telemetry",
        model="model",
        capability="implement",
        package_id="WP10",
    )

    assert usage.reported_input_tokens is None
    assert usage.effective_input_tokens == usage.estimated_input_tokens
    assert usage.total_tokens == usage.estimated_input_tokens + 10
    assert usage.effective_total_tokens == usage.total_tokens
    assert usage.input_measurement == "estimated"


def test_codex_cache_buckets_are_normalized_without_overlap(tmp_path: Path) -> None:
    store = AgentInvocationStore(tmp_path / "invocations.sqlite3")
    record = store.begin(
        project_id="task",
        package_id="WP10",
        stage="implement",
        capability="implement",
        attempt=1,
        agent_id="codex",
        adapter="codex",
        model="gpt",
        handoff={},
    )
    usage = normalize_agent_usage(
        {"usage": {"input_tokens": 1000, "cached_input_tokens": 700}},
        prompt="prompt",
        provider="codex",
        model="gpt",
        capability="implement",
        package_id="WP10",
    ).as_mapping()
    store.complete(record.invocation_id, duration_seconds=1.0, usage=usage)

    summary = store.usage_summary("task", package_id="WP10")
    assert summary["totals"]["uncached_input_tokens"] == 300
    assert summary["totals"]["cache_read_tokens"] == 700
    assert summary["cache"]["hit_rate"] == 0.7


def test_unavailable_cost_is_not_reported_as_zero() -> None:
    usage = normalize_agent_usage(
        {"usage": {"input_tokens": 2, "output_tokens": 1}},
        prompt="prompt",
        provider="codex",
        model="unknown-pricing",
        capability="review",
        package_id="WP10",
    )

    assert usage.reported_cost is None
    assert usage.estimated_cost is None


def _record_usage(
    store: AgentInvocationStore,
    raw: dict,
    *,
    package_id: str = "WP10",
    provider: str = "claude-code",
) -> None:
    record = store.begin(
        project_id="task",
        package_id=package_id,
        stage="review",
        capability="review",
        attempt=1,
        agent_id=provider,
        adapter=provider,
        model="claude",
        handoff={},
    )
    usage = normalize_agent_usage(
        {"usage": raw},
        prompt="prompt",
        provider=provider,
        model="claude",
        capability="review",
        package_id=package_id,
        invocation_id=record.invocation_id,
    ).as_mapping()
    store.complete(record.invocation_id, duration_seconds=1.0, usage=usage)


def test_usage_summary_reports_cache_write_and_read_separately(tmp_path: Path) -> None:
    """A healthy cache must be distinguishable from one that only ever writes."""

    store = AgentInvocationStore(tmp_path / "invocations.sqlite3")
    _record_usage(
        store,
        {
            "input_tokens": 100,
            "cache_creation_input_tokens": 200,
            "cache_read_input_tokens": 700,
            "output_tokens": 50,
        },
    )

    summary = store.usage_summary("task", package_id="WP10")

    assert summary["totals"]["cache_creation_tokens"] == 200
    assert summary["totals"]["cache_read_tokens"] == 700
    cache = summary["cache"]
    assert cache["status"] == "reported"
    assert cache["hit_rate"] == 0.7
    assert cache["reuse_ratio"] == 3.5


def test_usage_summary_flags_a_cache_that_only_writes(tmp_path: Path) -> None:
    """Writes without reads are the signature of a prefix invalidated per call."""

    store = AgentInvocationStore(tmp_path / "invocations.sqlite3")
    _record_usage(
        store,
        {"input_tokens": 100, "cache_creation_input_tokens": 900, "output_tokens": 10},
    )

    cache = store.usage_summary("task", package_id="WP10")["cache"]

    assert cache["status"] == "reported"
    assert cache["hit_rate"] == 0.0
    assert cache["reuse_ratio"] == 0.0


def test_usage_summary_distinguishes_providers_without_cache_telemetry(
    tmp_path: Path,
) -> None:
    """No cache buckets at all must not look like a broken cache."""

    store = AgentInvocationStore(tmp_path / "invocations.sqlite3")
    _record_usage(store, {"input_tokens": 0, "output_tokens": 0})

    assert store.usage_summary("task", package_id="WP10")["cache"]["status"] == "unreported"


def test_aggregated_cost_preserves_unknown_state(tmp_path: Path) -> None:
    """Aggregate reported_cost must remain None when all invocations have unavailable costs."""
    store = AgentInvocationStore(tmp_path / "invocations.sqlite3")
    _record_usage(store, {"input_tokens": 100, "output_tokens": 50, "reported_cost": None})
    _record_usage(store, {"input_tokens": 200, "output_tokens": 75, "reported_cost": None})

    summary = store.usage_summary("task", package_id="WP10")
    assert summary["totals"]["reported_cost"] is None
    for bucket in summary["by_provider"].values():
        assert bucket.get("reported_cost") is None


def test_aggregated_cost_preserves_partial_reporting(tmp_path: Path) -> None:
    """Mixed reported/unreported costs must aggregate only the reported subset."""
    store = AgentInvocationStore(tmp_path / "invocations.sqlite3")
    _record_usage(
        store,
        {"input_tokens": 100, "output_tokens": 50, "reported_cost": 0.15},
        provider="claude",
    )
    _record_usage(
        store,
        {"input_tokens": 200, "output_tokens": 75, "reported_cost": None},
        provider="claude",
    )
    _record_usage(
        store,
        {"input_tokens": 150, "output_tokens": 60, "reported_cost": 0.10},
        provider="codex",
    )

    summary = store.usage_summary("task", package_id="WP10")
    assert summary["totals"]["reported_cost"] == 0.25
    assert summary["by_provider"]["claude"]["reported_cost"] == 0.15
    assert summary["by_provider"]["codex"]["reported_cost"] == 0.10


def test_effective_input_equals_reported_when_telemetry_present() -> None:
    """Effective input uses reported values when available, not estimates."""
    usage = normalize_agent_usage(
        {"usage": {"input_tokens": 500, "output_tokens": 100}},
        prompt="x" * 10000,
        provider="claude",
        model="claude",
        capability="implement",
        package_id="WP10",
    )
    assert usage.reported_input_tokens == 500
    assert usage.effective_input_tokens == 500
    assert usage.estimated_input_tokens != 500


def test_effective_total_matches_reported_total_when_available() -> None:
    """Effective total honors provider-reported totals over computed sums."""
    usage = normalize_agent_usage(
        {
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "reasoning_tokens": 25,
                "total_tokens": 200,
            }
        },
        prompt="prompt",
        provider="codex",
        model="gpt",
        capability="review",
        package_id="WP10",
    )
    assert usage.reported_total_tokens == 200
    assert usage.total_tokens == 200
    assert usage.effective_total_tokens == 200


def test_effective_total_uses_computed_sum_when_no_reported_total() -> None:
    """Effective total computes from buckets when provider gives no total."""
    usage = normalize_agent_usage(
        {
            "usage": {
                "input_tokens": 100,
                "cache_creation_input_tokens": 50,
                "cache_read_input_tokens": 200,
                "output_tokens": 75,
                "reasoning_tokens": 10,
            }
        },
        prompt="prompt",
        provider="claude",
        model="claude",
        capability="implement",
        package_id="WP10",
    )
    assert usage.reported_total_tokens is None
    assert usage.total_tokens == 100 + 50 + 200 + 75 + 10
    assert usage.effective_total_tokens == 435


def test_cache_read_and_creation_are_not_double_counted_in_total() -> None:
    """Cache tokens are additive with input, not included in input."""
    usage = normalize_agent_usage(
        {
            "usage": {
                "input_tokens": 100,
                "cache_creation_input_tokens": 30,
                "cache_read_input_tokens": 400,
                "output_tokens": 50,
            }
        },
        prompt="prompt",
        provider="claude",
        model="claude",
        capability="review",
        package_id="WP10",
    )
    assert usage.total_tokens == 100 + 30 + 400 + 50
    assert usage.total_tokens == 580


def test_codex_overlapping_cache_buckets_are_normalized(tmp_path: Path) -> None:
    """OpenAI includes cache reads in input_tokens; normalize to non-overlapping."""
    usage = normalize_agent_usage(
        {"usage": {"input_tokens": 1000, "cached_input_tokens": 700}},
        prompt="prompt",
        provider="codex",
        model="gpt",
        capability="implement",
        package_id="WP10",
    )
    assert usage.input_tokens == 1000
    assert usage.cache_read_tokens == 700
    assert usage.uncached_input_tokens == 300
    assert usage.effective_input_tokens == 1000


def test_claude_non_overlapping_cache_buckets_preserved() -> None:
    """Claude reports cache tokens separately; preserve that shape."""
    usage = normalize_agent_usage(
        {
            "usage": {
                "input_tokens": 200,
                "cache_creation_input_tokens": 50,
                "cache_read_input_tokens": 600,
            }
        },
        prompt="prompt",
        provider="claude",
        model="claude",
        capability="review",
        package_id="WP10",
    )
    assert usage.input_tokens == 200
    assert usage.uncached_input_tokens == 200
    assert usage.cache_creation_tokens == 50
    assert usage.cache_read_tokens == 600
    assert usage.effective_input_tokens == 200 + 50 + 600


def test_zero_cost_is_reported_not_treated_as_unavailable() -> None:
    """Explicit zero cost must not be confused with unreported cost."""
    usage = normalize_agent_usage(
        {"usage": {"input_tokens": 1, "output_tokens": 1, "cost_usd": 0.0}},
        prompt="prompt",
        provider="mock",
        model="free",
        capability="test",
        package_id="WP10",
    )
    assert usage.reported_cost == 0.0


def test_estimated_cost_remains_none_when_unavailable() -> None:
    """Estimated cost is None when we cannot compute it."""
    usage = normalize_agent_usage(
        {"usage": {"input_tokens": 100, "output_tokens": 50}},
        prompt="prompt",
        provider="unknown",
        model="unknown",
        capability="implement",
        package_id="WP10",
    )
    assert usage.estimated_cost is None
    assert usage.reported_cost is None


def test_cache_efficiency_unreported_when_no_cache_buckets() -> None:
    """No cache telemetry must report 'unreported', not zero hit rate."""
    from execraft.orchestrate.invocations import _cache_efficiency

    totals = {
        "uncached_input_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
    }
    efficiency = _cache_efficiency(totals)
    assert efficiency["status"] == "unreported"
    assert efficiency["hit_rate"] == 0.0
    assert efficiency["reuse_ratio"] == 0.0


def test_cache_efficiency_uncached_when_only_uncached_present() -> None:
    """Only uncached tokens means cache is not in use."""
    from execraft.orchestrate.invocations import _cache_efficiency

    totals = {
        "uncached_input_tokens": 500,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
    }
    efficiency = _cache_efficiency(totals)
    assert efficiency["status"] == "uncached"
    assert efficiency["hit_rate"] == 0.0


def test_cache_efficiency_reported_when_any_cache_bucket_nonzero() -> None:
    """Any cache activity means status is 'reported'."""
    from execraft.orchestrate.invocations import _cache_efficiency

    totals = {
        "uncached_input_tokens": 100,
        "cache_creation_tokens": 50,
        "cache_read_tokens": 0,
    }
    efficiency = _cache_efficiency(totals)
    assert efficiency["status"] == "reported"


def test_cache_hit_rate_computed_from_non_overlapping_buckets() -> None:
    """Hit rate is read/(read + creation + uncached)."""
    from execraft.orchestrate.invocations import _cache_efficiency

    totals = {
        "uncached_input_tokens": 100,
        "cache_creation_tokens": 200,
        "cache_read_tokens": 700,
    }
    efficiency = _cache_efficiency(totals)
    assert efficiency["hit_rate"] == 0.7
    assert efficiency["cache_read_tokens"] == 700
    assert efficiency["uncached_input_tokens"] == 100


def test_cache_reuse_ratio_is_read_over_write() -> None:
    """Reuse ratio below 1.0 means writing more than reading."""
    from execraft.orchestrate.invocations import _cache_efficiency

    totals = {
        "uncached_input_tokens": 0,
        "cache_creation_tokens": 200,
        "cache_read_tokens": 700,
    }
    efficiency = _cache_efficiency(totals)
    assert efficiency["reuse_ratio"] == 3.5


def test_cache_reuse_ratio_zero_when_no_creation() -> None:
    """Reuse ratio is zero when no writes occurred."""
    from execraft.orchestrate.invocations import _cache_efficiency

    totals = {
        "uncached_input_tokens": 100,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 500,
    }
    efficiency = _cache_efficiency(totals)
    assert efficiency["reuse_ratio"] == 0.0


def test_claude_adapter_total_cost_usd_normalization_nonzero() -> None:
    """Claude adapter returns total_cost_usd; normalize_agent_usage must recognize it."""
    usage = normalize_agent_usage(
        {"total_cost_usd": 0.0160664, "usage": {"input_tokens": 1000, "output_tokens": 100}},
        prompt="prompt",
        provider="claude-code",
        model="claude",
        capability="implement",
        package_id="WP01",
    )
    assert usage.reported_cost == 0.0160664


def test_claude_adapter_total_cost_usd_normalization_zero() -> None:
    """Explicit zero total_cost_usd must be preserved as reported cost."""
    usage = normalize_agent_usage(
        {"total_cost_usd": 0.0, "usage": {"input_tokens": 10, "output_tokens": 5}},
        prompt="prompt",
        provider="claude-code",
        model="claude",
        capability="review",
        package_id="WP01",
    )
    assert usage.reported_cost == 0.0


def test_prompt_composition_metrics_measure_stable_prefix_and_skill_body() -> None:
    skill = {
        "id": "ai-review",
        "instructions": "Review deterministically and return structured findings.",
    }
    usage = normalize_agent_usage(
        {"usage": {"input_tokens": 50, "output_tokens": 5}},
        prompt=(
            "EXECRAFT ORCHESTRATION CONTRACT\n"
            "Review deterministically and return structured findings.\n"
            "Handoff ID: abc\nTask: review"
        ),
        provider="claude-code",
        model="claude",
        capability="review",
        package_id="M0",
        workflow_skills=[skill],
    )

    assert usage.stable_prefix_bytes > 0
    assert usage.stable_prefix_bytes < usage.prompt_bytes
    assert usage.stable_prefix_estimated_tokens > 0
    assert usage.skill_instruction_bytes == len(skill["instructions"].encode("utf-8"))
    assert usage.skill_instruction_estimated_tokens > 0


def test_usage_summary_exposes_prompt_composition_stage_model_and_attempt_cost(
    tmp_path: Path,
) -> None:
    store = AgentInvocationStore(tmp_path / "invocations.sqlite3")
    for attempt, status in ((1, "failed"), (2, "completed")):
        record = store.begin(
            project_id="task",
            package_id="M0",
            stage="review",
            capability="review",
            attempt=attempt,
            agent_id="claude",
            adapter="claude-code",
            model="claude-sonnet",
            handoff={},
        )
        usage = normalize_agent_usage(
            {"usage": {"input_tokens": 100, "output_tokens": 10}},
            prompt="stable prefix\nReview.\nHandoff ID: dynamic",
            provider="claude-code",
            model="claude-sonnet",
            capability="review",
            package_id="M0",
            workflow_skills=[{"instructions": "Review."}],
        ).as_mapping()
        if status == "failed":
            store.fail(
                record.invocation_id,
                duration_seconds=1.0,
                failure={"classification": "invalid_output"},
                usage=usage,
            )
        else:
            store.complete(record.invocation_id, duration_seconds=1.0, usage=usage)

    summary = store.usage_summary("task", package_id="M0")

    assert summary["by_model"]["claude-sonnet"]["invocations"] == 2
    assert summary["by_stage"]["review"]["invocations"] == 2
    assert summary["attempt_cost"]["failed"]["effective_input_tokens"] == 100
    assert summary["attempt_cost"]["retries"]["effective_input_tokens"] == 100
    assert summary["prompt_composition"]["skill_instruction_bytes"] == 14
    assert summary["prompt_composition"]["stable_prefix_bytes"] > 0


def test_prompt_composition_counts_only_skill_bodies_embedded_in_prompt() -> None:
    selected = [{"id": "ai-review", "instructions": "Lazy review instructions."}]

    usage = normalize_agent_usage(
        {"usage": {"input_tokens": 10, "output_tokens": 1}},
        prompt="EXECRAFT ORCHESTRATION CONTRACT\nSkill: ai-review\nHandoff ID: abc",
        provider="openclaw",
        model="test-model",
        capability="review",
        package_id="M0",
        workflow_skills=selected,
    )

    assert usage.skill_instruction_bytes == 0
    assert usage.skill_instruction_estimated_tokens == 0
