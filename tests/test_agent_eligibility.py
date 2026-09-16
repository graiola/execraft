from __future__ import annotations

import pytest

from execraft.orchestrate.agent_eligibility import ineligibility_payload


def _seconds(value: str):
    return 42.0 if value else None


@pytest.mark.parametrize(
    ("reason", "classification", "message"),
    [
        ("adapter_not_found", "configuration_error", "registered agent adapter not found"),
        ("policy_excluded", "policy_excluded", "stage independence policy"),
        ("promotion_fallback_not_needed", "policy_excluded", "fallback-only promotion"),
        ("role_pool_excluded", "policy_excluded", "binding final-review pool"),
        ("adapter_disabled", "adapter_disabled", "provider adapter unavailable"),
        ("complexity_limit:80>60", "complexity_limit", "80>60"),
        ("read_only_isolation:none<filesystem", "policy_excluded", "none<filesystem"),
    ],
)
def test_static_ineligibility_classification(reason, classification, message):
    payload = ineligibility_payload(reason, seconds_until=_seconds)
    assert payload["classification"] == classification
    assert message in payload["error"]
    assert payload["unavailable_until"] == ""
    assert payload["retry_after_seconds"] is None


def test_contract_quarantine_uses_contract_deadline():
    payload = ineligibility_payload(
        "contract_invalid_output",
        provider_unavailable_until="provider-deadline",
        contract_unavailable_until="contract-deadline",
        seconds_until=_seconds,
    )
    assert payload["classification"] == "invalid_output"
    assert payload["unavailable_until"] == "contract-deadline"
    assert payload["retry_after_seconds"] == 42.0


def test_generic_provider_unavailability_keeps_health_deadline():
    payload = ineligibility_payload(
        "cooldown",
        provider_unavailable_until="provider-deadline",
        seconds_until=_seconds,
    )
    assert payload == {
        "reason": "cooldown",
        "classification": "cooldown",
        "error": "provider unavailable: cooldown until provider-deadline",
        "unavailable_until": "provider-deadline",
        "retry_after_seconds": 42.0,
    }
