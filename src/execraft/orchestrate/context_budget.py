"""Deterministic context planning and provider prompt budgets.

The orchestration core deals in semantic context blocks rather than concatenating
all available history.  This module is deliberately provider-neutral: it uses a
conservative UTF-8 based estimator when no model tokenizer is available and
records every inclusion/exclusion decision for inspection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Iterable, Mapping


class ContextBudgetError(RuntimeError):
    """Raised when mandatory prompt context exceeds a configured hard limit."""


@dataclass(frozen=True)
class TokenBudget:
    """Input/output targets and hard limits for one capability."""

    input_target: int
    input_hard_limit: int
    output_target: int
    output_hard_limit: int

    def __post_init__(self) -> None:
        values = (
            self.input_target,
            self.input_hard_limit,
            self.output_target,
            self.output_hard_limit,
        )
        if any(int(value) <= 0 for value in values):
            raise ValueError("token budget values must be positive")
        if self.input_target > self.input_hard_limit:
            raise ValueError("input_target cannot exceed input_hard_limit")
        if self.output_target > self.output_hard_limit:
            raise ValueError("output_target cannot exceed output_hard_limit")

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any] | None,
        *,
        default: "TokenBudget",
    ) -> "TokenBudget":
        raw = dict(value or {})
        try:
            return cls(
                input_target=int(raw.get("input_target", default.input_target)),
                input_hard_limit=int(
                    raw.get("input_hard_limit", default.input_hard_limit)
                ),
                output_target=int(raw.get("output_target", default.output_target)),
                output_hard_limit=int(
                    raw.get("output_hard_limit", default.output_hard_limit)
                ),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid token budget: {exc}") from exc

    def as_mapping(self) -> dict[str, int]:
        return {
            "input_target": self.input_target,
            "input_hard_limit": self.input_hard_limit,
            "output_target": self.output_target,
            "output_hard_limit": self.output_hard_limit,
        }


DEFAULT_TOKEN_BUDGETS: dict[str, TokenBudget] = {
    "plan": TokenBudget(8000, 16000, 1800, 4000),
    "brief": TokenBudget(6000, 12000, 1500, 3000),
    "decompose": TokenBudget(6000, 12000, 1500, 3000),
    "implement": TokenBudget(12000, 24000, 1500, 4000),
    "fix_review": TokenBudget(10000, 20000, 1000, 3000),
    "review": TokenBudget(16000, 30000, 1500, 4000),
    "verify": TokenBudget(10000, 20000, 1200, 3000),
    "supervise": TokenBudget(24000, 48000, 3000, 6000),
    "close": TokenBudget(8000, 16000, 1500, 3000),
}




def truncate_utf8(
    value: object,
    maximum_bytes: int,
    *,
    suffix: str = "\n...[truncated]",
) -> str:
    """Bound text by encoded size without splitting a UTF-8 code point."""

    text = str(value or "")
    data = text.encode("utf-8")
    if len(data) <= maximum_bytes:
        return text
    if maximum_bytes <= 0:
        return ""
    suffix_bytes = suffix.encode("utf-8")
    if len(suffix_bytes) >= maximum_bytes:
        return data[:maximum_bytes].decode("utf-8", errors="ignore")
    available = maximum_bytes - len(suffix_bytes)
    return data[:available].decode("utf-8", errors="ignore") + suffix

def estimate_tokens(value: object) -> int:
    """Return a conservative, deterministic token estimate.

    Four UTF-8 bytes per token is a common approximation for English prose, but
    source code, paths, and structured data often tokenize less efficiently.
    Dividing by 3.5 and adding a small framing allowance intentionally errs on
    the safe side without requiring provider SDK dependencies.
    """

    data = str(value or "").encode("utf-8")
    if not data:
        return 0
    return max(1, math.ceil(len(data) / 3.5) + 2)


def truncate_to_tokens(
    value: object,
    maximum_tokens: int,
    *,
    from_end: bool = False,
    suffix: str = "\n...[bounded by Execraft]",
) -> str:
    """Bound text without splitting a UTF-8 code point.

    Very small limits cannot accommodate the estimator's framing allowance.
    In that case the only valid bounded value is the empty string.  When the
    truncation marker would consume the whole byte budget, preserve bounded
    source text instead of returning only the marker.
    """

    text = str(value or "")
    if estimate_tokens(text) <= maximum_tokens:
        return text
    if maximum_tokens <= 0:
        return ""
    # ``estimate_tokens`` adds two framing tokens. Reserve them here so a
    # bounded block never causes the planner to overshoot its target. Limits
    # of one or two tokens cannot contain any non-empty value under that
    # estimator.
    byte_budget = int(maximum_tokens - 2) * 7 // 2
    if byte_budget <= 0:
        return ""
    suffix_bytes = suffix.encode("utf-8")
    bounded_suffix = suffix if len(suffix_bytes) < byte_budget else ""
    payload_budget = byte_budget - len(bounded_suffix.encode("utf-8"))
    encoded = text.encode("utf-8")
    if from_end:
        payload = encoded[-payload_budget:] if payload_budget else b""
        return bounded_suffix + payload.decode("utf-8", errors="ignore")
    payload = encoded[:payload_budget]
    return payload.decode("utf-8", errors="ignore") + bounded_suffix


@dataclass(frozen=True)
class ContextBlock:
    """One independently selectable unit of prompt context."""

    id: str
    type: str
    source: str
    content: str
    scope: str = ""
    priority: int = 50
    required: bool = False
    deduplication_key: str = ""
    truncation_policy: str = "drop"  # drop, head, tail
    minimum_tokens: int = 64
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def estimated_tokens(self) -> int:
        return estimate_tokens(self.content)

    @property
    def key(self) -> str:
        return self.deduplication_key or f"{self.type}:{self.source}:{self.id}"

    def as_mapping(self, *, include_content: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "type": self.type,
            "source": self.source,
            "scope": self.scope,
            "priority": self.priority,
            "required": self.required,
            "deduplication_key": self.key,
            "truncation_policy": self.truncation_policy,
            "estimated_tokens": self.estimated_tokens,
            "content_bytes": len(self.content.encode("utf-8")),
            "metadata": dict(self.metadata),
        }
        if include_content:
            result["content"] = self.content
        return result


@dataclass(frozen=True)
class PlannedContext:
    included: tuple[ContextBlock, ...]
    excluded: tuple[dict[str, Any], ...]
    deduplicated: tuple[dict[str, Any], ...]
    estimated_tokens: int
    target_tokens: int
    hard_limit_tokens: int

    def as_mapping(self) -> dict[str, Any]:
        return {
            "estimator": "utf8-bytes/3.5+framing",
            "estimated_tokens": self.estimated_tokens,
            "target_tokens": self.target_tokens,
            "hard_limit_tokens": self.hard_limit_tokens,
            "included": [item.as_mapping() for item in self.included],
            "excluded": [dict(item) for item in self.excluded],
            "deduplicated": [dict(item) for item in self.deduplicated],
        }


class ContextPlanner:
    """Deduplicate, rank, and budget context blocks deterministically."""

    def __init__(self, budget: TokenBudget) -> None:
        self.budget = budget

    def plan(
        self,
        blocks: Iterable[ContextBlock],
        *,
        reserved_tokens: int = 0,
    ) -> PlannedContext:
        unique: dict[str, ContextBlock] = {}
        deduplicated: list[dict[str, Any]] = []
        for block in blocks:
            existing = unique.get(block.key)
            if existing is None:
                unique[block.key] = block
                continue
            winner = self._preferred(existing, block)
            loser = block if winner is existing else existing
            unique[block.key] = winner
            deduplicated.append(
                {
                    **loser.as_mapping(),
                    "reason": f"duplicate of {winner.id}",
                }
            )

        ordered = sorted(
            unique.values(),
            key=lambda item: (
                not item.required,
                -int(item.priority),
                item.type,
                item.id,
            ),
        )
        included: list[ContextBlock] = []
        excluded: list[dict[str, Any]] = []
        used = max(0, int(reserved_tokens))

        for block in ordered:
            block_tokens = block.estimated_tokens
            if used + block_tokens <= self.budget.input_target:
                included.append(block)
                used += block_tokens
                continue

            if block.required:
                if used + block_tokens > self.budget.input_hard_limit:
                    raise ContextBudgetError(
                        "mandatory context exceeds input hard limit: "
                        f"block={block.id} used={used} block_tokens={block_tokens} "
                        f"hard_limit={self.budget.input_hard_limit}"
                    )
                included.append(block)
                used += block_tokens
                continue

            remaining = self.budget.input_target - used
            if (
                block.truncation_policy in {"head", "tail"}
                and remaining >= max(1, block.minimum_tokens)
            ):
                bounded = truncate_to_tokens(
                    block.content,
                    remaining,
                    from_end=block.truncation_policy == "tail",
                )
                included.append(
                    ContextBlock(
                        id=block.id,
                        type=block.type,
                        source=block.source,
                        content=bounded,
                        scope=block.scope,
                        priority=block.priority,
                        required=block.required,
                        deduplication_key=block.deduplication_key,
                        truncation_policy="drop",
                        minimum_tokens=block.minimum_tokens,
                        metadata={**dict(block.metadata), "truncated": True},
                    )
                )
                used += estimate_tokens(bounded)
                excluded.append(
                    {
                        **block.as_mapping(),
                        "reason": "partially included to target budget",
                    }
                )
                continue

            excluded.append(
                {
                    **block.as_mapping(),
                    "reason": "target token budget exhausted",
                }
            )

        if used > self.budget.input_hard_limit:
            raise ContextBudgetError(
                f"planned context uses {used} estimated tokens; hard limit is "
                f"{self.budget.input_hard_limit}"
            )
        return PlannedContext(
            included=tuple(included),
            excluded=tuple(excluded),
            deduplicated=tuple(deduplicated),
            estimated_tokens=used,
            target_tokens=self.budget.input_target,
            hard_limit_tokens=self.budget.input_hard_limit,
        )

    @staticmethod
    def _preferred(left: ContextBlock, right: ContextBlock) -> ContextBlock:
        return max(
            (left, right),
            key=lambda item: (
                item.required,
                item.priority,
                -item.estimated_tokens,
                item.id,
            ),
        )


def budgets_from_mapping(
    value: Mapping[str, Any] | None,
) -> dict[str, TokenBudget]:
    """Overlay user configuration on the safe capability defaults."""

    raw = dict(value or {})
    result: dict[str, TokenBudget] = {}
    for capability, default in DEFAULT_TOKEN_BUDGETS.items():
        item = raw.get(capability)
        if item is not None and not isinstance(item, Mapping):
            raise ValueError(f"token budget for {capability!r} must be a mapping")
        result[capability] = TokenBudget.from_mapping(item, default=default)
    unknown = sorted(set(raw) - set(DEFAULT_TOKEN_BUDGETS))
    if unknown:
        raise ValueError("unknown token budget capabilities: " + ", ".join(unknown))
    return result
