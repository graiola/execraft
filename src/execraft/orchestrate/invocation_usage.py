"""Usage aggregation for persisted agent invocations.

Keeping aggregation outside the SQLite persistence class makes the invocation
store responsible only for durable records and migrations. It also provides a
single place to add runtime/model/target dimensions without expanding the
already stateful persistence code.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .runtime_optimization_usage import summarize_runtime_optimization


_TOKEN_KEYS = (
    "input_tokens",
    "effective_input_tokens",
    "uncached_input_tokens",
    "estimated_input_tokens",
    "stable_prefix_bytes",
    "stable_prefix_estimated_tokens",
    "skill_instruction_bytes",
    "skill_instruction_estimated_tokens",
    "cached_input_tokens",
    "cache_creation_tokens",
    "cache_read_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
    "effective_total_tokens",
    "prompt_bytes",
)


def cache_efficiency(totals: Mapping[str, Any]) -> dict[str, Any]:
    created = int(totals.get("cache_creation_tokens", 0) or 0)
    read = int(totals.get("cache_read_tokens", 0) or 0)
    uncached = int(totals.get("uncached_input_tokens", 0) or 0)
    billable_prefix = created + read + uncached
    if not billable_prefix:
        return {"status": "unreported", "hit_rate": 0.0, "reuse_ratio": 0.0}
    return {
        "status": "reported" if (created or read) else "uncached",
        "hit_rate": round(read / billable_prefix, 4),
        "reuse_ratio": round(read / created, 4) if created else 0.0,
        "cache_creation_tokens": created,
        "cache_read_tokens": read,
        "uncached_input_tokens": uncached,
    }


def _new_totals(invocations: int = 0) -> dict[str, Any]:
    result: dict[str, Any] = {key: 0 for key in _TOKEN_KEYS}
    result["invocations"] = invocations
    result["reported_cost"] = None
    return result


def _accumulate(bucket: dict[str, Any], usage: Mapping[str, Any]) -> None:
    bucket["invocations"] = int(bucket.get("invocations", 0)) + 1
    for key in _TOKEN_KEYS:
        value = usage.get(key, 0)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            bucket[key] = int(bucket.get(key, 0)) + int(value)
    cost = usage.get("reported_cost")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        current = bucket.get("reported_cost")
        bucket["reported_cost"] = float(cost) if current is None else float(current) + float(cost)


def _group_value(record: Any, usage: Mapping[str, Any], dimension: str) -> str:
    if dimension == "candidate":
        return str(getattr(record, "candidate_id", "") or getattr(record, "agent_id", "") or "unknown")
    if dimension == "runtime":
        return str(getattr(record, "runtime_id", "") or "legacy-native")
    if dimension == "runtime_backend":
        return str(getattr(record, "runtime_backend", "") or getattr(record, "adapter", "") or "unknown")
    if dimension == "model_route":
        return str(getattr(record, "model_route_id", "") or "unrouted")
    if dimension == "target":
        return str(getattr(record, "target_id", "") or "unresolved")
    if dimension == "provider":
        return str(
            getattr(record, "model_provider", "")
            or usage.get("provider")
            or getattr(record, "adapter", "")
            or getattr(record, "agent_id", "")
            or "unknown"
        )
    if dimension == "model":
        return str(
            getattr(record, "model_name", "")
            or usage.get("model")
            or getattr(record, "model", "")
            or "unknown"
        )
    if dimension == "capability":
        return str(getattr(record, "capability", "") or "unknown")
    if dimension == "stage":
        return str(getattr(record, "stage", "") or "unknown")
    if dimension == "status":
        return str(getattr(record, "status", "") or "unknown")
    raise ValueError(f"unsupported usage dimension: {dimension}")


def summarize_invocation_usage(records: Sequence[Any]) -> dict[str, Any]:
    """Aggregate normalized usage across execution dimensions."""

    totals = _new_totals()
    groups: dict[str, dict[str, dict[str, Any]]] = {
        dimension: {}
        for dimension in (
            "candidate",
            "runtime",
            "runtime_backend",
            "model_route",
            "target",
            "provider",
            "model",
            "capability",
            "stage",
            "status",
        )
    }
    failed_attempts: dict[str, Any] = {}
    retry_attempts: dict[str, Any] = {}
    prompt_sizes: list[int] = []
    block_totals: dict[str, int] = {}

    for record in records:
        usage = record.usage if isinstance(record.usage, Mapping) else {}
        _accumulate(totals, usage)
        for dimension, buckets in groups.items():
            key = _group_value(record, usage, dimension)
            _accumulate(buckets.setdefault(key, {}), usage)
        if getattr(record, "status", "") == "failed":
            _accumulate(failed_attempts, usage)
        if int(getattr(record, "attempt", 1)) > 1:
            _accumulate(retry_attempts, usage)
        prompt_bytes = usage.get("prompt_bytes")
        if isinstance(prompt_bytes, (int, float)) and not isinstance(prompt_bytes, bool):
            prompt_sizes.append(int(prompt_bytes))
        manifest = getattr(record, "handoff", {}).get("context_manifest", [])
        if isinstance(manifest, list):
            for item in manifest:
                if not isinstance(item, Mapping):
                    continue
                block_type = str(item.get("type") or item.get("id") or "unknown")
                tokens = item.get("estimated_tokens", 0)
                if isinstance(tokens, (int, float)) and not isinstance(tokens, bool):
                    block_totals[block_type] = block_totals.get(block_type, 0) + int(tokens)

    sorted_sizes = sorted(prompt_sizes)

    def percentile(percent: float) -> int:
        if not sorted_sizes:
            return 0
        index = min(
            len(sorted_sizes) - 1,
            max(0, int(round((len(sorted_sizes) - 1) * percent))),
        )
        return sorted_sizes[index]

    result: dict[str, Any] = {
        "totals": totals,
        "cache": cache_efficiency(totals),
        "attempt_cost": {"failed": failed_attempts, "retries": retry_attempts},
        "prompt_composition": {
            "stable_prefix_bytes": totals["stable_prefix_bytes"],
            "stable_prefix_estimated_tokens": totals["stable_prefix_estimated_tokens"],
            "skill_instruction_bytes": totals["skill_instruction_bytes"],
            "skill_instruction_estimated_tokens": totals["skill_instruction_estimated_tokens"],
        },
        "prompt_bytes": {
            "average": int(sum(sorted_sizes) / len(sorted_sizes)) if sorted_sizes else 0,
            "p50": percentile(0.50),
            "p90": percentile(0.90),
            "p99": percentile(0.99),
            "maximum": max(sorted_sizes) if sorted_sizes else 0,
        },
        "context_block_tokens": dict(
            sorted(block_totals.items(), key=lambda item: (-item[1], item[0]))
        ),
        "runtime_optimization": summarize_runtime_optimization(records),
    }
    for dimension, buckets in groups.items():
        result[f"by_{dimension}"] = {key: buckets[key] for key in sorted(buckets)}
    return result
