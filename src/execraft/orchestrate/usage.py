"""Provider-independent token and cost accounting."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .prompt_metrics import measure_prompt_composition


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _integer(value: object) -> int:
    number = _number(value)
    return max(0, int(number)) if number is not None else 0


def _first(mapping: Mapping[str, Any], *keys: str) -> object:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _usage_mapping(result: Mapping[str, Any]) -> dict[str, Any]:
    for key in ("usage", "token_usage", "tokens"):
        value = result.get(key)
        if isinstance(value, Mapping):
            return dict(value)
    metrics = result.get("metrics")
    if isinstance(metrics, Mapping):
        for key in ("usage", "token_usage", "tokens"):
            value = metrics.get(key)
            if isinstance(value, Mapping):
                return dict(value)
    return {}


def _cache_token_buckets(
    raw: Mapping[str, Any],
    *,
    provider: str,
    input_tokens: int,
    reported_input: float | None,
    estimate: int,
) -> tuple[int, int, int, int, int]:
    """Return cached/read/write and provider-neutral uncached/effective input."""

    nested = raw.get("cache") if isinstance(raw.get("cache"), Mapping) else {}
    cached = _integer(
        _first(
            raw,
            "cached_input_tokens",
            "cached_tokens",
            "cache_read_input_tokens",
            "cacheRead",
        )
    )
    created = _integer(
        _first(
            raw,
            "cache_creation_tokens",
            "cache_creation_input_tokens",
            "cache_write_tokens",
            "cacheWrite",
        )
    )
    read = _integer(_first(raw, "cache_read_tokens", "cache_read_input_tokens", "cacheRead"))
    if nested:
        created = created or _integer(_first(nested, "write", "creation", "cache_creation"))
        read = read or _integer(_first(nested, "read", "cache_read"))
    cached = cached or read

    normalized = "cacheRead" in raw or "cacheWrite" in raw
    provider_key = provider.lower().replace("_", "-")
    if normalized or "claude" in provider_key:
        uncached = input_tokens
        effective = input_tokens + created + read if reported_input is not None else estimate
    else:
        if cached and not read:
            read = cached
        uncached = max(0, input_tokens - cached)
        effective = input_tokens if reported_input is not None else estimate
    return cached, created, read, uncached, effective


@dataclass(frozen=True)
class AgentUsage:
    prompt_bytes: int = 0
    estimated_input_tokens: int = 0
    stable_prefix_bytes: int = 0
    stable_prefix_estimated_tokens: int = 0
    skill_instruction_bytes: int = 0
    skill_instruction_estimated_tokens: int = 0
    input_tokens: int = 0
    reported_input_tokens: int | None = None
    effective_input_tokens: int = 0
    uncached_input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    reported_total_tokens: int | None = None
    effective_total_tokens: int = 0
    estimated_cost: float | None = None
    reported_cost: float | None = None
    currency: str = "USD"
    provider: str = ""
    model: str = ""
    capability: str = ""
    package_id: str = ""
    shard: str = ""
    stage: str = ""
    attempt: int = 0
    invocation_id: str = ""
    input_measurement: str = "estimated"
    raw: Mapping[str, Any] | None = None

    def as_mapping(self, *, include_raw: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "prompt_bytes": self.prompt_bytes,
            "estimated_input_tokens": self.estimated_input_tokens,
            "stable_prefix_bytes": self.stable_prefix_bytes,
            "stable_prefix_estimated_tokens": self.stable_prefix_estimated_tokens,
            "skill_instruction_bytes": self.skill_instruction_bytes,
            "skill_instruction_estimated_tokens": self.skill_instruction_estimated_tokens,
            "input_tokens": self.input_tokens,
            "reported_input_tokens": self.reported_input_tokens,
            "effective_input_tokens": self.effective_input_tokens,
            "uncached_input_tokens": self.uncached_input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
            "reported_total_tokens": self.reported_total_tokens,
            "effective_total_tokens": self.effective_total_tokens,
            "estimated_cost": self.estimated_cost,
            "reported_cost": self.reported_cost,
            "currency": self.currency,
            "provider": self.provider,
            "model": self.model,
            "capability": self.capability,
            "package_id": self.package_id,
            "shard": self.shard,
            "stage": self.stage,
            "attempt": self.attempt,
            "invocation_id": self.invocation_id,
            "input_measurement": self.input_measurement,
        }
        if include_raw:
            result["raw"] = dict(self.raw or {})
        return result


def normalize_agent_usage(
    result: Mapping[str, Any] | None,
    *,
    prompt: str,
    provider: str,
    model: str,
    capability: str,
    package_id: str,
    shard: str = "",
    stage: str = "",
    attempt: int = 0,
    invocation_id: str = "",
    workflow_skills: Sequence[Mapping[str, Any]] = (),
) -> AgentUsage:
    """Normalize common Codex, Claude, and OpenCode usage payloads.

    Cached-token fields remain separate. ``total_tokens`` follows a provider
    reported total when present; otherwise it uses estimated prompt input when
    input telemetry is absent and provider-normalized, non-overlapping cache
    buckets when it is present. Unknown costs remain ``None``, never zero.
    """

    raw_result = dict(result or {})
    raw = _usage_mapping(raw_result)
    prompt_metrics = measure_prompt_composition(prompt, workflow_skills=workflow_skills)
    estimate = prompt_metrics.prompt_estimated_tokens

    raw_input = _first(
        raw,
        "input_tokens",
        "prompt_tokens",
        "input",
        "tokens_in",
        "inputTokens",
    )
    reported_input = _number(raw_input)
    input_tokens = _integer(raw_input)
    output_tokens = _integer(
        _first(
            raw,
            "output_tokens",
            "completion_tokens",
            "output",
            "tokens_out",
            "outputTokens",
        )
    )
    reasoning_tokens = _integer(
        _first(
            raw,
            "reasoning_tokens",
            "reasoning_output_tokens",
            "reasoning",
            "thinking_tokens",
            "reasoningTokens",
        )
    )

    raw_total = _first(raw, "total_tokens", "total", "token_count", "totalTokens")
    reported_total_number = _number(raw_total)
    reported_total = _integer(raw_total)
    (
        cached_input_tokens,
        cache_creation_tokens,
        cache_read_tokens,
        uncached_input_tokens,
        effective_input_tokens,
    ) = _cache_token_buckets(
        raw,
        provider=provider,
        input_tokens=input_tokens,
        reported_input=reported_input,
        estimate=estimate,
    )

    if reported_total_number is not None:
        total_tokens = reported_total
    else:
        total_tokens = effective_input_tokens + output_tokens + reasoning_tokens

    cost = _number(
        _first(raw, "cost", "cost_usd", "reported_cost", "total_cost", "total_cost_usd")
    )
    if cost is None:
        cost = _number(
            _first(raw_result, "cost", "cost_usd", "reported_cost", "total_cost", "total_cost_usd")
        )
    currency = str(
        _first(raw, "currency", "cost_currency")
        or _first(raw_result, "currency", "cost_currency")
        or "USD"
    )
    return AgentUsage(
        prompt_bytes=prompt_metrics.prompt_bytes,
        estimated_input_tokens=estimate,
        stable_prefix_bytes=prompt_metrics.stable_prefix_bytes,
        stable_prefix_estimated_tokens=prompt_metrics.stable_prefix_estimated_tokens,
        skill_instruction_bytes=prompt_metrics.skill_instruction_bytes,
        skill_instruction_estimated_tokens=prompt_metrics.skill_instruction_estimated_tokens,
        input_tokens=input_tokens,
        reported_input_tokens=(input_tokens if reported_input is not None else None),
        effective_input_tokens=effective_input_tokens,
        uncached_input_tokens=uncached_input_tokens,
        cached_input_tokens=cached_input_tokens,
        cache_creation_tokens=cache_creation_tokens,
        cache_read_tokens=cache_read_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        total_tokens=total_tokens,
        reported_total_tokens=(reported_total if reported_total_number is not None else None),
        effective_total_tokens=total_tokens,
        reported_cost=(float(cost) if cost is not None else None),
        currency=currency,
        provider=provider,
        model=model,
        capability=capability,
        package_id=package_id,
        shard=shard,
        stage=stage,
        attempt=max(0, int(attempt)),
        invocation_id=invocation_id,
        input_measurement="reported" if reported_input is not None else "estimated",
        raw=raw,
    )
