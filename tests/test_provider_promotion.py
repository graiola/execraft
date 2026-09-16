from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from execraft.orchestrate.provider_promotion import (
    ProviderPromotionStore,
    parse_promotion_duration,
)


def test_promotion_store_persists_task_scoped_override_atomically(tmp_path):
    path = tmp_path / "provider-promotions.json"
    store = ProviderPromotionStore(path)
    now = datetime(2026, 8, 26, 11, 30, tzinfo=timezone.utc)

    created = store.promote(
        "local-qwen",
        "review",
        base_max_complexity=75,
        promoted_max_complexity=100,
        duration_seconds=4 * 60 * 60,
        package_id="WP23",
        fallback_only=True,
        reason="top-tier quota unavailable",
        source="test",
        now=now,
    )

    reloaded = ProviderPromotionStore(path).applicable(
        "local-qwen",
        "review",
        package_id="WP23",
        stage="review",
        now=now + timedelta(minutes=1),
    )

    assert reloaded == created
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    payload = json.loads(path.read_text(encoding="utf-8"))
    persisted = next(iter(payload["promotions"].values()))
    assert "remaining_seconds" not in persisted
    assert "active" not in persisted


def test_promotion_scope_expiry_and_final_review_guard(tmp_path):
    store = ProviderPromotionStore(tmp_path / "promotions.json")
    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    store.promote(
        "qwen",
        "review",
        base_max_complexity=75,
        promoted_max_complexity=97,
        duration_seconds=60,
        package_id="WP23",
        allow_final_review=False,
        now=now,
    )

    assert store.applicable(
        "qwen", "review", package_id="WP22", stage="review", now=now
    ) is None
    assert store.applicable(
        "qwen", "review", package_id="WP23", stage="final_review", now=now
    ) is None
    assert store.applicable(
        "qwen",
        "review",
        package_id="WP23",
        stage="review",
        now=now + timedelta(seconds=61),
    ) is None


def test_package_specific_promotion_beats_task_wide_record(tmp_path):
    store = ProviderPromotionStore(tmp_path / "promotions.json")
    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    store.promote(
        "qwen",
        "review",
        base_max_complexity=75,
        promoted_max_complexity=90,
        duration_seconds=3600,
        now=now,
    )
    store.promote(
        "qwen",
        "review",
        base_max_complexity=75,
        promoted_max_complexity=97,
        duration_seconds=1800,
        package_id="WP23",
        now=now,
    )

    selected = store.applicable(
        "qwen", "review", package_id="WP23", stage="review", now=now
    )
    assert selected is not None
    assert selected.promoted_max_complexity == 97
    assert selected.package_id == "WP23"


def test_store_hot_reload_observes_external_atomic_update(tmp_path):
    path = tmp_path / "promotions.json"
    reader = ProviderPromotionStore(path)
    writer = ProviderPromotionStore(path)
    assert reader.list() == []

    writer.promote(
        "qwen",
        "review",
        base_max_complexity=75,
        promoted_max_complexity=100,
        duration_seconds=3600,
    )

    assert [item.provider_id for item in reader.list()] == ["qwen"]


def test_revoke_removes_only_requested_capabilities(tmp_path):
    store = ProviderPromotionStore(tmp_path / "promotions.json")
    for capability in ("review", "implement"):
        store.promote(
            "qwen",
            capability,
            base_max_complexity=50,
            promoted_max_complexity=100,
            duration_seconds=3600,
        )

    removed = store.revoke("qwen", capabilities=["review"])

    assert [item.capability for item in removed] == ["review"]
    assert [item.capability for item in store.list()] == ["implement"]


@pytest.mark.parametrize(
    ("value", "seconds"),
    [("30m", 1800), ("4h", 14400), ("1d", 86400), ("45s", 45)],
)
def test_parse_promotion_duration(value, seconds):
    assert parse_promotion_duration(value) == seconds


@pytest.mark.parametrize("value", ["", "4", "0h", "8d", "banana"])
def test_parse_promotion_duration_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        parse_promotion_duration(value)
