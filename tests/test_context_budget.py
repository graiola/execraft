from __future__ import annotations

import pytest

from execraft.orchestrate.context_budget import (
    ContextBlock,
    ContextBudgetError,
    ContextPlanner,
    TokenBudget,
    budgets_from_mapping,
    estimate_tokens,
    truncate_to_tokens,
)


def _budget(*, target: int = 100, hard: int = 200) -> TokenBudget:
    return TokenBudget(target, hard, 20, 40)


def test_context_planner_deduplicates_and_prefers_required_block() -> None:
    optional = ContextBlock(
        id="old",
        type="evidence",
        source="test",
        content="old",
        priority=90,
        deduplication_key="same",
    )
    required = ContextBlock(
        id="new",
        type="evidence",
        source="test",
        content="authoritative",
        priority=10,
        required=True,
        deduplication_key="same",
    )

    plan = ContextPlanner(_budget()).plan([optional, required])

    assert [item.id for item in plan.included] == ["new"]
    assert plan.deduplicated[0]["id"] == "old"
    assert plan.deduplicated[0]["reason"] == "duplicate of new"


def test_context_planner_bounds_optional_text_at_target() -> None:
    block = ContextBlock(
        id="log.txt",
        type="log",
        source="test",
        content="x" * 4000,
        truncation_policy="tail",
        minimum_tokens=20,
    )

    plan = ContextPlanner(_budget(target=80, hard=160)).plan(
        [block], reserved_tokens=20
    )

    assert len(plan.included) == 1
    assert plan.included[0].metadata["truncated"] is True
    assert plan.estimated_tokens <= 80
    assert plan.excluded[0]["reason"] == "partially included to target budget"


def test_context_planner_rejects_mandatory_overflow() -> None:
    block = ContextBlock(
        id="requirements.json",
        type="contract",
        source="test",
        content="x" * 4000,
        required=True,
    )

    with pytest.raises(ContextBudgetError, match="mandatory context"):
        ContextPlanner(_budget(target=20, hard=40)).plan([block])


def test_budget_configuration_overlays_defaults_and_rejects_unknown_keys() -> None:
    budgets = budgets_from_mapping(
        {"implement": {"input_target": 321, "input_hard_limit": 654}}
    )

    assert budgets["implement"].input_target == 321
    assert budgets["implement"].input_hard_limit == 654
    assert budgets["review"].input_target > 0
    with pytest.raises(ValueError, match="unknown token budget"):
        budgets_from_mapping({"made_up": {}})


def test_token_estimator_is_deterministic_and_conservative() -> None:
    text = "alpha beta gamma" * 100
    assert estimate_tokens(text) == estimate_tokens(text)
    assert estimate_tokens(text) > len(text.encode("utf-8")) // 4


@pytest.mark.parametrize("maximum_tokens", range(17))
@pytest.mark.parametrize("from_end", [False, True])
def test_truncation_never_exceeds_tiny_token_limits(
    maximum_tokens: int, from_end: bool
) -> None:
    bounded = truncate_to_tokens(
        "alpha βeta 🌍 " * 20,
        maximum_tokens,
        from_end=from_end,
    )

    assert estimate_tokens(bounded) <= maximum_tokens


def test_tiny_truncation_preserves_requested_source_edge() -> None:
    assert truncate_to_tokens("abcdefgh", 2) == ""
    assert truncate_to_tokens("abcdefgh", 3) == "abc"
    assert truncate_to_tokens("abcdefgh", 3, from_end=True) == "fgh"
