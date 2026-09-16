"""Provider failover coordinator that handles provider progression, retry, repair,
exclusion relaxation, and failover logic."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping

from execraft.execution_identity import execution_identity_of

from .agent_attempt import AgentAttemptRunner, AgentAttemptResult
from .contract_health import ContractHealthStore, schema_sha256
from .context_budget import truncate_utf8
from .provider_health import ProviderHealthStore
from .scheduler import (
    AgentAdapter,
    AgentCapability,
    StructuredHandoff,
)

logger = logging.getLogger(__name__)

_ATTEMPT_HISTORY_ERROR_MAX_BYTES = 2 * 1024



@dataclass(frozen=True)
class FailoverOutcome:
    """Result of the provider progression loop.

    ``result`` is None when every candidate was exhausted without a durable
    success; the caller then schedules a provider wait instead of escalating.
    """

    result: dict[str, Any] | None
    attempts: list[dict[str, Any]]
    candidate_id: str
    policy_excluded: frozenset[str]


@dataclass
class ProviderFailoverStrategy:
    """Callbacks that bind the coordinator to an orchestrator façade.

    The coordinator owns provider progression, the bounded format-repair retry,
    failover, and exclusion relaxation. Everything orchestrator-specific
    (adapter lookup, eligibility mapping, prompt rendering, durable side
    effects) is injected here so the coordinator stays independently testable.
    """

    adapter: Callable[[str], AgentAdapter | None]
    metadata: Callable[[str], dict[str, Any]]
    eligibility: Callable[
        [str, AgentCapability, frozenset[str], str, bool, str],
        dict[str, Any] | None,
    ]
    prepare_attempt: Callable[[], tuple[str, Callable[[], str]]]
    render_prompt: Callable[[StructuredHandoff], str]
    build_repair_handoff: Callable[
        [StructuredHandoff, str, str, str, str], StructuredHandoff
    ]
    parent_invocation_id: Callable[[], str]
    select_candidate: Callable[
        [AgentCapability, set[str], str, str], str | None
    ]
    health_excluded_ids: Callable[[], set[str]]
    candidate_eligible: Callable[[str, AgentCapability, frozenset[str]], str]
    on_provider_skipped: Callable[[str, Mapping[str, Any]], None]
    on_relaxed: Callable[[str, str, frozenset[str]], None]
    on_transition: Callable[[str, str, str], None]


class ProviderFailoverCoordinator:
    """Execute provider attempts with progression, retry, repair, and failover.

    ``AgentAttemptRunner`` executes one provider attempt. This coordinator
    decides which provider runs next: it applies the bounded format-repair
    retry after a single invalid structured output, falls back through ordered
    candidates, relaxes stage-independence exclusion tiers, and transitions
    across providers until the stage durably succeeds or the runnable pool is
    exhausted.
    """

    def __init__(
        self,
        *,
        attempt_runner: AgentAttemptRunner,
        contract_health: ContractHealthStore,
        provider_health: ProviderHealthStore,
        max_attempts_per_stage: int,
        strict_checks: bool,
        require_structured_output: bool,
        strategy: ProviderFailoverStrategy,
    ):
        self._attempt_runner = attempt_runner
        self._contract_health = contract_health
        self._provider_health = provider_health
        self._max_attempts_per_stage = max_attempts_per_stage
        self._strict_checks = strict_checks
        self._require_structured_output = require_structured_output
        self._strategy = strategy

    def execute_with_failover(
        self,
        *,
        handoff: StructuredHandoff,
        capability: AgentCapability,
        package_id: str,
        stage: str,
        shard_key: str,
        candidate_id: str,
        policy_excluded: set[str],
        relaxation_tiers: list[tuple[str, set[str]]],
        ordered_fallbacks: list[str],
        preference_role: str,
    ) -> FailoverOutcome:
        """Run provider progression, retry, repair, and failover for one stage."""
        tried: set[str] = set()
        attempts: list[dict[str, Any]] = []
        execution_attempts = 0
        format_retry_allowance = 0
        format_repair_source = ""
        format_repair_error = ""
        format_repair_session = ""
        max_attempts = max(1, self._max_attempts_per_stage)
        contract_schema_hash = (
            schema_sha256(handoff.expected_output_schema)
            if handoff.expected_output_schema
            else ""
        )
        policy_excluded = set(policy_excluded)
        relaxation_tiers = [
            (str(policy), set(tier_excluded)) for policy, tier_excluded in relaxation_tiers
        ]

        while (
            candidate_id
            and execution_attempts < max_attempts + format_retry_allowance
        ):
            retry_same_candidate = False
            adapter = self._strategy.adapter(candidate_id)
            if adapter is not None:
                candidate_id = execution_identity_of(adapter).candidate_id
            tried.add(candidate_id)
            metadata = self._strategy.metadata(candidate_id)

            eligibility_payload = self._strategy.eligibility(
                candidate_id,
                capability,
                frozenset(policy_excluded),
                contract_schema_hash,
                handoff.read_only,
                handoff.required_isolation,
            )
            if eligibility_payload is not None:
                attempts.append(
                    {
                        "agent_id": candidate_id,
                        "error": str(eligibility_payload.get("error", "")),
                        "classification": str(
                            eligibility_payload.get("classification", "")
                        ),
                        "retry_after_seconds": eligibility_payload.get(
                            "retry_after_seconds"
                        ),
                    }
                )
                self._strategy.on_provider_skipped(
                    candidate_id, eligibility_payload
                )
            else:
                execution_attempts += 1
                attempt_number = execution_attempts
                parent_invocation_id = self._strategy.parent_invocation_id()
                attempt_handoff = handoff.for_attempt(
                    attempt_number,
                    parent_invocation_id=parent_invocation_id,
                    attempt_history=attempts,
                )
                if format_repair_source:
                    attempt_handoff = self._strategy.build_repair_handoff(
                        attempt_handoff,
                        format_repair_source,
                        format_repair_error,
                        format_repair_session,
                        candidate_id,
                    )
                    format_repair_source = ""
                    format_repair_error = ""
                    format_repair_session = ""
                attempt_context = dict(attempt_handoff.execution_context)
                attempt_context["selected_provider"] = {
                    "agent_id": candidate_id,
                    "adapter": metadata["adapter"],
                    "model": metadata["model"],
                    "execution_capabilities": metadata["execution_capabilities"],
                }
                attempt_handoff = replace(
                    attempt_handoff, execution_context=attempt_context
                )
                rendered_prompt = self._strategy.render_prompt(attempt_handoff)
                workspace_before_digest, after_digest_fn = (
                    self._strategy.prepare_attempt()
                )
                attempt_result: AgentAttemptResult = (
                    self._attempt_runner.execute_attempt(
                        adapter=adapter,
                        handoff=attempt_handoff,
                        capability=capability,
                        package_id=package_id,
                        stage=stage,
                        shard_key=shard_key,
                        attempt_number=attempt_number,
                        parent_invocation_id=parent_invocation_id,
                        triggering_event_id=attempt_handoff.triggering_event_id,
                        workspace_before_digest=workspace_before_digest,
                        workspace_after_digest_fn=after_digest_fn,
                        rendered_prompt=rendered_prompt,
                        metadata=metadata,
                        strict_checks=self._strict_checks,
                        require_structured_output=self._require_structured_output,
                    )
                )
                if attempt_result.success:
                    return FailoverOutcome(
                        result=attempt_result.result,
                        attempts=attempts,
                        candidate_id=candidate_id,
                        policy_excluded=frozenset(policy_excluded),
                    )

                classification = attempt_result.classification
                if classification == "invalid_output" and contract_schema_hash:
                    contract_health = attempt_result.contract_health
                    if contract_health is None:
                        contract_health = self._contract_health.get(
                            candidate_id,
                            metadata["model"],
                            capability.value,
                            contract_schema_hash,
                        )
                    retry_same_candidate = (
                        contract_health.consecutive_failures == 1
                        and execution_attempts < max_attempts + 1
                    )
                    if retry_same_candidate:
                        format_retry_allowance = 1
                        raw_result = attempt_result.raw_result
                        if isinstance(raw_result, Mapping):
                            candidate_text = raw_result.get("final_message")
                            if isinstance(candidate_text, str) and candidate_text.strip():
                                format_repair_source = candidate_text.strip()
                                format_repair_error = str(
                                    attempt_result.invocation.failure.get("error", "")
                                )
                                format_repair_session = str(
                                    raw_result.get("session_id", "") or ""
                                ).strip()

                attempts.append(
                    {
                        "invocation_id": attempt_result.invocation.invocation_id,
                        "parent_invocation_id": parent_invocation_id,
                        "agent_id": candidate_id,
                        "model": metadata["model"],
                        "error": truncate_utf8(
                            attempt_result.invocation.failure.get("error", ""),
                            _ATTEMPT_HISTORY_ERROR_MAX_BYTES,
                        ),
                        "classification": classification,
                        "retry_after_seconds": attempt_result.retry_after_seconds,
                        "artifact": (
                            attempt_result.artifact.as_mapping()
                            if attempt_result.artifact is not None
                            else {}
                        ),
                        "handoff_sha256": attempt_result.invocation.handoff_sha256,
                    }
                )

            if retry_same_candidate:
                next_id = candidate_id
            else:
                format_repair_source = ""
                format_repair_error = ""
                format_repair_session = ""
                next_id = ""
            next_excluded = (
                set(tried) | policy_excluded | self._strategy.health_excluded_ids()
            )
            next_id = next_id or self._first_eligible_fallback(
                ordered_fallbacks, next_excluded, capability
            )
            next_id = next_id or (
                self._strategy.select_candidate(
                    capability,
                    next_excluded,
                    candidate_id,
                    preference_role,
                )
                or ""
            )
            while not next_id and relaxation_tiers:
                policy, relaxed_excluded = relaxation_tiers.pop(0)
                newly_allowed = set(policy_excluded) - set(relaxed_excluded)
                policy_excluded = set(relaxed_excluded)
                next_id = (
                    self._strategy.select_candidate(
                        capability,
                        (set(tried) - newly_allowed)
                        | policy_excluded
                        | self._strategy.health_excluded_ids(),
                        candidate_id,
                        preference_role,
                    )
                    or ""
                )
                if next_id:
                    self._strategy.on_relaxed(
                        policy, next_id, frozenset(policy_excluded)
                    )
            if next_id:
                transition_event = (
                    "agent_contract_retry"
                    if next_id == candidate_id
                    else "agent_failover"
                )
                self._strategy.on_transition(
                    transition_event, candidate_id, next_id
                )
            candidate_id = next_id or ""

        return FailoverOutcome(
            result=None,
            attempts=attempts,
            candidate_id=candidate_id,
            policy_excluded=frozenset(policy_excluded),
        )

    def _first_eligible_fallback(
        self,
        ordered_fallbacks: list[str],
        next_excluded: set[str],
        capability: AgentCapability,
    ) -> str:
        """Return the first ordered fallback that is still runnable, else ""."""
        for fallback_id in ordered_fallbacks:
            if fallback_id in next_excluded:
                continue
            fallback_adapter = self._strategy.adapter(fallback_id)
            if fallback_adapter is None:
                continue
            if capability not in fallback_adapter.capabilities:
                continue
            if self._strategy.candidate_eligible(
                fallback_id, capability, frozenset()
            ):
                continue
            return fallback_id
        return ""
