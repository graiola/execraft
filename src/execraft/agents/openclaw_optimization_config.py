"""Parse OpenClaw optimization policy from schema-v4 runtime config."""

from __future__ import annotations

from typing import Any, Mapping

from execraft.runtime_config import OpenClawOptimizationOptions


_KEYS = frozenset(
    {
        "cache_retention",
        "context_pruning_mode",
        "context_pruning_ttl",
        "proactive_compaction",
        "compaction_mode",
        "compaction_timeout_seconds",
        "context_pressure_ratio",
        "max_context_tokens",
        "max_delta_prompt_bytes",
        "max_session_reuse_turns",
        "max_session_age_seconds",
        "post_compaction_guard",
    }
)


def parse_openclaw_optimization(
    raw: object, *, runtime_id: str
) -> OpenClawOptimizationOptions:
    """Return validated optimization policy for one OpenClaw runtime."""

    if raw is None:
        return OpenClawOptimizationOptions()
    if not isinstance(raw, Mapping):
        raise ValueError(
            f"OpenClaw runtime {runtime_id!r} optimization must be a mapping"
        )
    unknown = sorted(str(key) for key in raw if str(key) not in _KEYS)
    if unknown:
        raise ValueError(
            f"OpenClaw runtime {runtime_id!r} optimization has unknown fields: "
            + ", ".join(unknown)
        )
    defaults = OpenClawOptimizationOptions()
    return OpenClawOptimizationOptions(
        cache_retention=_text(raw, "cache_retention", defaults.cache_retention),
        context_pruning_mode=_text(
            raw, "context_pruning_mode", defaults.context_pruning_mode
        ),
        context_pruning_ttl=_text(
            raw, "context_pruning_ttl", defaults.context_pruning_ttl
        ),
        proactive_compaction=_boolean(
            raw, "proactive_compaction", defaults.proactive_compaction
        ),
        compaction_mode=_text(raw, "compaction_mode", defaults.compaction_mode),
        compaction_timeout_seconds=_integer(
            raw,
            "compaction_timeout_seconds",
            defaults.compaction_timeout_seconds,
            minimum=1,
        ),
        context_pressure_ratio=_ratio(
            raw, "context_pressure_ratio", defaults.context_pressure_ratio
        ),
        max_context_tokens=_integer(
            raw, "max_context_tokens", defaults.max_context_tokens, minimum=0
        ),
        max_delta_prompt_bytes=_integer(
            raw,
            "max_delta_prompt_bytes",
            defaults.max_delta_prompt_bytes,
            minimum=1,
        ),
        max_session_reuse_turns=_integer(
            raw,
            "max_session_reuse_turns",
            defaults.max_session_reuse_turns,
            minimum=1,
        ),
        max_session_age_seconds=_integer(
            raw,
            "max_session_age_seconds",
            defaults.max_session_age_seconds,
            minimum=1,
        ),
        post_compaction_guard=_boolean(
            raw, "post_compaction_guard", defaults.post_compaction_guard
        ),
    )


def _text(raw: Mapping[str, Any], key: str, default: str) -> str:
    if key not in raw:
        return default
    value = str(raw[key]).strip().lower()
    if not value:
        raise ValueError(f"OpenClaw optimization {key} cannot be empty")
    return value


def _boolean(raw: Mapping[str, Any], key: str, default: bool) -> bool:
    if key not in raw:
        return default
    value = raw[key]
    if not isinstance(value, bool):
        raise ValueError(f"OpenClaw optimization {key} must be true or false")
    return value


def _integer(raw: Mapping[str, Any], key: str, default: int, *, minimum: int) -> int:
    if key not in raw:
        return default
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"OpenClaw optimization {key} must be an integer >= {minimum}")
    return value


def _ratio(raw: Mapping[str, Any], key: str, default: float) -> float:
    if key not in raw:
        return default
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"OpenClaw optimization {key} must be numeric")
    ratio = float(value)
    if not 0.0 < ratio <= 1.0:
        raise ValueError(f"OpenClaw optimization {key} must be in (0, 1]")
    return ratio
