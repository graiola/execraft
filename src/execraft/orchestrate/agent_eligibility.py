"""Pure classification of scheduler candidate ineligibility.

The orchestrator determines *why* a candidate is ineligible because that
requires live adapter, package and policy state.  This module only converts that
reason into the stable failover/wait payload consumed by coordination code.
Keeping message/classification policy here removes a deeply nested branch from
``ProjectOrchestrator._execute_agent`` without moving state-machine ownership.
"""

from __future__ import annotations

from typing import Any, Callable


def ineligibility_payload(
    reason: str,
    *,
    provider_unavailable_until: str = "",
    contract_unavailable_until: str = "",
    seconds_until: Callable[[str], float | None],
) -> dict[str, Any]:
    """Return the stable failover payload for one canonical ineligibility reason."""

    unavailable_until = ""
    retry_after = None
    if reason.startswith("complexity_limit:"):
        classification = "complexity_limit"
        detail = reason.split(":", 1)[1]
        error = f"task complexity exceeds provider limit: {detail}"
    elif reason.startswith("read_only_isolation:"):
        classification = "policy_excluded"
        detail = reason.split(":", 1)[1]
        error = f"provider cannot satisfy required read-only isolation: {detail}"
    elif reason == "adapter_not_found":
        classification = "configuration_error"
        error = "registered agent adapter not found"
    elif reason == "policy_excluded":
        classification = "policy_excluded"
        error = "provider excluded by stage independence policy"
    elif reason == "promotion_fallback_not_needed":
        classification = "policy_excluded"
        error = (
            "fallback-only promotion is dormant while a normally eligible "
            "provider is available"
        )
    elif reason == "role_pool_excluded":
        classification = "policy_excluded"
        error = "provider excluded by binding final-review pool"
    elif reason == "contract_invalid_output":
        classification = "invalid_output"
        error = (
            "provider/model is quarantined for this capability "
            "and structured-output schema"
        )
        unavailable_until = contract_unavailable_until
        retry_after = seconds_until(unavailable_until)
    elif reason.startswith("adapter_"):
        classification = reason
        error = f"provider adapter unavailable: {reason}"
    else:
        classification = reason or "provider_unavailable"
        unavailable_until = provider_unavailable_until
        retry_after = seconds_until(unavailable_until)
        error = (
            f"provider unavailable: {classification}"
            + (f" until {unavailable_until}" if unavailable_until else "")
        )
    return {
        "reason": reason,
        "classification": classification,
        "error": error,
        "unavailable_until": unavailable_until,
        "retry_after_seconds": retry_after,
    }
