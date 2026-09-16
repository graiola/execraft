"""Aggregate runtime continuation/cache/compaction diagnostics."""

from __future__ import annotations

from collections import Counter
from typing import Any, Mapping, Sequence


def summarize_runtime_optimization(records: Sequence[Any]) -> dict[str, Any]:
    decisions: Counter[str] = Counter()
    cache_retention: Counter[str] = Counter()
    pruning_modes: Counter[str] = Counter()
    pressure_samples: list[float] = []
    totals = {
        "openclaw_invocations": 0,
        "session_reused": 0,
        "cold_reconstructions": 0,
        "authoritative_guards": 0,
        "compaction_attempts": 0,
        "compaction_successes": 0,
        "compaction_tokens_before": 0,
        "compaction_tokens_after": 0,
        "compaction_tokens_reclaimed": 0,
        "observed_compaction_count_delta": 0,
        "delta_prompt_bytes": 0,
        "prompt_bytes": 0,
        "prompt_estimated_tokens": 0,
        "cold_equivalent_prompt_bytes": 0,
        "cold_equivalent_prompt_estimated_tokens": 0,
        "repeated_context_avoided_bytes": 0,
        "repeated_context_avoided_estimated_tokens": 0,
        "skill_prompt_savings_bytes": 0,
        "skill_prompt_savings_estimated_tokens": 0,
        "skill_instruction_bytes": 0,
        "provider_input_tokens": 0,
        "provider_uncached_input_tokens": 0,
        "provider_cached_input_tokens": 0,
        "provider_cache_read_tokens": 0,
        "failed_attempts": 0,
        "retry_attempts": 0,
        "session_telemetry_degraded": 0,
    }

    for record in records:
        metadata = getattr(record, "runtime_metadata", {})
        if not isinstance(metadata, Mapping) or str(
            getattr(record, "runtime_backend", "")
        ) != "gateway":
            continue
        totals["openclaw_invocations"] += 1
        totals["session_reused"] += int(metadata.get("session_reused") is True)
        totals["cold_reconstructions"] += int(
            metadata.get("cold_reconstruction") is True
        )
        totals["authoritative_guards"] += int(
            metadata.get("authoritative_guard") is True
        )
        totals["session_telemetry_degraded"] += int(
            metadata.get("session_telemetry_degraded") is True
        )
        totals["delta_prompt_bytes"] += _integer(metadata.get("delta_prompt_bytes"))
        for key in (
            "prompt_bytes",
            "prompt_estimated_tokens",
            "cold_equivalent_prompt_bytes",
            "cold_equivalent_prompt_estimated_tokens",
            "repeated_context_avoided_bytes",
            "repeated_context_avoided_estimated_tokens",
            "skill_prompt_savings_bytes",
            "skill_prompt_savings_estimated_tokens",
            "skill_instruction_bytes",
        ):
            totals[key] += _integer(metadata.get(key))
        usage = getattr(record, "usage", {})
        usage = usage if isinstance(usage, Mapping) else {}
        totals["provider_input_tokens"] += _integer(usage.get("input_tokens"))
        totals["provider_uncached_input_tokens"] += _integer(
            usage.get("uncached_input_tokens")
        )
        totals["provider_cached_input_tokens"] += _integer(
            usage.get("cached_input_tokens")
        )
        totals["provider_cache_read_tokens"] += _integer(usage.get("cache_read_tokens"))
        totals["failed_attempts"] += int(str(getattr(record, "status", "")) == "failed")
        totals["retry_attempts"] += int(_integer(getattr(record, "attempt", 1)) > 1)
        decision = str(metadata.get("continuation_decision", "")).strip()
        if decision:
            decisions[decision] += 1
        retention = str(metadata.get("cache_retention", "")).strip()
        if retention:
            cache_retention[retention] += 1
        pruning = str(metadata.get("context_pruning_mode", "")).strip()
        if pruning:
            pruning_modes[pruning] += 1

        compaction = metadata.get("compaction")
        if isinstance(compaction, Mapping):
            totals["compaction_attempts"] += int(compaction.get("attempted") is True)
            totals["compaction_successes"] += int(compaction.get("compacted") is True)
            before = _integer(compaction.get("tokens_before"))
            after = _integer(compaction.get("tokens_after"))
            totals["compaction_tokens_before"] += before
            totals["compaction_tokens_after"] += after
            totals["compaction_tokens_reclaimed"] += max(0, before - after)

        pre = metadata.get("pre_session")
        post = metadata.get("post_session")
        if isinstance(pre, Mapping):
            pressure = pre.get("context_pressure")
            if isinstance(pressure, (int, float)) and not isinstance(pressure, bool):
                pressure_samples.append(float(pressure))
        if isinstance(pre, Mapping) and isinstance(post, Mapping):
            totals["observed_compaction_count_delta"] += max(
                0,
                _integer(post.get("compaction_count"))
                - _integer(pre.get("compaction_count")),
            )

    byte_denominator = totals["cold_equivalent_prompt_bytes"]
    token_denominator = totals["cold_equivalent_prompt_estimated_tokens"]
    return {
        **totals,
        "context_reduction": {
            "bytes_percent": _percentage(
                totals["repeated_context_avoided_bytes"], byte_denominator
            ),
            "estimated_tokens_percent": _percentage(
                totals["repeated_context_avoided_estimated_tokens"], token_denominator
            ),
        },
        "continuation_decisions": dict(sorted(decisions.items())),
        "cache_retention": dict(sorted(cache_retention.items())),
        "context_pruning_modes": dict(sorted(pruning_modes.items())),
        "context_pressure": {
            "samples": len(pressure_samples),
            "average": round(sum(pressure_samples) / len(pressure_samples), 4)
            if pressure_samples
            else 0.0,
            "maximum": round(max(pressure_samples), 4) if pressure_samples else 0.0,
        },
    }


def _integer(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _percentage(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(max(0, numerator) / denominator * 100.0, 2)
