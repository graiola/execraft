from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

from execraft.orchestrate.provider_health import ProviderHealthStore


def test_quota_cooldown_is_persisted_and_reloaded(tmp_path):
    path = tmp_path / "provider-health.json"
    store = ProviderHealthStore(path)

    health = store.mark_failure(
        "opencode-go",
        reason="quota_exhausted",
        detail="monthly limit",
        retry_after_seconds=3600,
    )

    assert health.status == "cooldown"
    assert not health.is_available
    reloaded = ProviderHealthStore(path).get("opencode-go")
    assert reloaded.reason == "quota_exhausted"
    assert reloaded.consecutive_failures == 1
    assert path.stat().st_mode & 0o777 == 0o600


def test_expired_cooldown_becomes_probe_due_until_success(tmp_path):
    path = tmp_path / "provider-health.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "providers": {
                    "opencode-go": {
                        "provider_id": "opencode-go",
                        "status": "cooldown",
                        "reason": "rate_limited",
                        "unavailable_until": (
                            datetime.now(timezone.utc) - timedelta(seconds=1)
                        ).isoformat(),
                        "last_failure_at": "",
                        "consecutive_failures": 2,
                        "detail": "",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    health = ProviderHealthStore(path).get("opencode-go")

    assert health.status == "probe_due"
    assert health.is_available
    assert health.consecutive_failures == 2

    store = ProviderHealthStore(path)
    store.mark_available("opencode-go")
    assert store.get("opencode-go").status == "available"


def test_auth_failure_blocks_until_manual_reset(tmp_path):
    store = ProviderHealthStore(tmp_path / "provider-health.json")
    health = store.mark_failure(
        "claude-code",
        reason="authentication_required",
        detail="login required",
    )
    assert health.status == "blocked"
    assert health.unavailable_until == ""
    assert not health.is_available

    store.reset("claude-code")
    assert store.get("claude-code").is_available


def test_invalid_output_does_not_contaminate_endpoint_health(tmp_path):
    store = ProviderHealthStore(tmp_path / "provider-health.json")
    health = store.mark_failure(
        "opencode-zen-free",
        reason="invalid_output",
        detail="missing verdict",
        persistent=False,
    )
    assert health.status == "available"
    assert health.reason == ""
    assert health.is_available


def test_legacy_invalid_output_cooldown_is_migrated_on_read(tmp_path):
    path = tmp_path / "provider-health.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "providers": {
                    "codex": {
                        "provider_id": "codex",
                        "status": "cooldown",
                        "reason": "invalid_output",
                        "unavailable_until": (
                            datetime.now(timezone.utc) + timedelta(hours=1)
                        ).isoformat(),
                        "consecutive_failures": 4,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    health = ProviderHealthStore(path).get("codex")

    assert health.is_available
    assert health.reason == ""
    assert ProviderHealthStore(path).get("codex").consecutive_failures == 0



def test_output_silence_uses_short_progressive_cooldown(tmp_path):
    store = ProviderHealthStore(tmp_path / "provider-health.json")

    first = store.mark_failure(
        "satellite-qwen",
        reason="output_silence",
        detail="no stdout or stderr for 900s",
        persistent=True,
    )
    first_delay = (
        datetime.fromisoformat(first.unavailable_until)
        - datetime.fromisoformat(first.last_failure_at)
    ).total_seconds()

    second = store.mark_failure(
        "satellite-qwen",
        reason="output_silence",
        detail="no stdout or stderr for 900s",
        persistent=True,
    )
    second_delay = (
        datetime.fromisoformat(second.unavailable_until)
        - datetime.fromisoformat(second.last_failure_at)
    ).total_seconds()

    assert first.status == "cooldown"
    assert first.reason == "output_silence"
    assert 299 <= first_delay <= 301
    assert 599 <= second_delay <= 601
    assert second.consecutive_failures == 2

def test_non_persistent_permission_failure_does_not_block_provider(tmp_path):
    store = ProviderHealthStore(tmp_path / "provider-health.json")

    health = store.mark_failure(
        "opencode-zen-free",
        reason="permission_required",
        detail="todowrite was auto-rejected",
        persistent=False,
    )

    assert health.status == "available"
    assert health.is_available
    assert store.get("opencode-zen-free").is_available


def test_generic_provider_error_uses_progressive_cooldown(tmp_path):
    store = ProviderHealthStore(tmp_path / "provider-health.json")

    first = store.mark_failure(
        "opencode-go",
        reason="provider_error",
        detail="Unexpected server error",
        persistent=False,
    )
    first_delay = (
        datetime.fromisoformat(first.unavailable_until)
        - datetime.fromisoformat(first.last_failure_at)
    ).total_seconds()

    second = store.mark_failure(
        "opencode-go",
        reason="provider_error",
        detail="Unexpected server error",
        persistent=False,
    )
    second_delay = (
        datetime.fromisoformat(second.unavailable_until)
        - datetime.fromisoformat(second.last_failure_at)
    ).total_seconds()

    assert first.status == "cooldown"
    assert 299 <= first_delay <= 301
    assert 899 <= second_delay <= 901
    assert second.consecutive_failures == 2


def test_session_limit_uses_exact_persisted_deadline(tmp_path):
    store = ProviderHealthStore(tmp_path / "provider-health.json")

    health = store.mark_failure(
        "claude-code",
        reason="session_limit",
        detail="resets at 01:40",
        retry_after_seconds=7200,
        persistent=True,
    )

    assert health.status == "cooldown"
    assert health.reason == "session_limit"
    assert health.unavailable_until
    assert not health.is_available


def test_network_failures_use_bounded_exponential_cooldown(tmp_path):
    store = ProviderHealthStore(tmp_path / "provider-health.json")

    first = store.mark_failure(
        "codex",
        reason="network_transient",
        detail="dns lookup failed",
        persistent=True,
    )
    first_delay = (
        datetime.fromisoformat(first.unavailable_until)
        - datetime.fromisoformat(first.last_failure_at)
    ).total_seconds()

    second = store.mark_failure(
        "codex",
        reason="network_transient",
        detail="connection reset by peer",
        persistent=True,
    )
    second_delay = (
        datetime.fromisoformat(second.unavailable_until)
        - datetime.fromisoformat(second.last_failure_at)
    ).total_seconds()

    assert first_delay == 60
    assert second_delay == 120
    assert second.consecutive_failures == 2


def test_operator_can_set_exact_provider_cooldown(tmp_path):
    store = ProviderHealthStore(tmp_path / "provider-health.json")
    deadline = datetime.now(timezone.utc) + timedelta(days=2)

    health = store.set_cooldown(
        "opencode-go",
        unavailable_until=deadline,
        reason="session_limit",
        detail="Reset shown in provider UI",
    )

    assert health.status == "cooldown"
    assert health.reason == "session_limit"
    assert not health.is_available
    persisted = store.get("opencode-go")
    assert persisted.unavailable_until == deadline.isoformat()
    assert persisted.detail == "Reset shown in provider UI"


def test_operator_cooldown_rejects_naive_deadline(tmp_path):
    store = ProviderHealthStore(tmp_path / "provider-health.json")

    try:
        store.set_cooldown(
            "opencode-go",
            unavailable_until=datetime.now() + timedelta(days=1),
        )
    except ValueError as exc:
        assert "timezone" in str(exc)
    else:
        raise AssertionError("expected a timezone validation error")
