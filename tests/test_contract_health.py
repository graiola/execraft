from __future__ import annotations

from execraft.orchestrate.contract_health import ContractHealthStore, schema_sha256


def test_contract_failures_are_scoped_to_exact_tuple(tmp_path):
    store = ContractHealthStore(tmp_path / "contract-health.json")
    review_hash = schema_sha256({"type": "object", "required": ["verdict"]})
    fix_hash = schema_sha256({"type": "object", "required": ["status"]})

    first = store.mark_failure(
        "codex", "gpt", "review", review_hash, detail="missing verdict"
    )
    second = store.mark_failure(
        "codex", "gpt", "review", review_hash, detail="missing verdict"
    )

    assert first.is_available
    assert not second.is_available
    assert store.get("codex", "gpt", "review", fix_hash).is_available
    assert store.get("codex", "gpt", "implement", review_hash).is_available
    assert store.get("claude-code", "gpt", "review", review_hash).is_available


def test_contract_success_clears_only_matching_strike(tmp_path):
    store = ContractHealthStore(tmp_path / "contract-health.json")
    schema_hash = schema_sha256({"type": "object"})
    store.mark_failure("codex", "gpt", "review", schema_hash)
    store.mark_failure("claude-code", "sonnet", "review", schema_hash)

    store.mark_available("codex", "gpt", "review", schema_hash)

    assert (
        store.get("codex", "gpt", "review", schema_hash).consecutive_failures
        == 0
    )
    assert (
        store.get("codex", "gpt", "review", schema_hash).successful_results
        == 1
    )
    assert (
        store.get(
            "claude-code", "sonnet", "review", schema_hash
        ).consecutive_failures
        == 1
    )
