"""Runtime-neutral bounded delegation policy for OpenClaw sub-agents.

The policy belongs to Execraft, not OpenClaw.  It is intentionally conservative:
sub-agents are disabled globally and per profile until explicitly enabled, are
leaf workers, receive only read-oriented tools, and carry finite concurrency,
time, token, and estimated-cost budgets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
from typing import Any, Mapping


class SubagentPolicyError(ValueError):
    """Raised when a delegation policy is ambiguous or unsafe."""


class SubagentUseCase(str, Enum):
    FOCUSED_SEARCH = "focused_search"
    STATIC_REVIEW = "static_review"
    TEST_FAILURE_CLASSIFICATION = "test_failure_classification"
    DOCUMENTATION_LOOKUP = "documentation_lookup"
    SPECIALIST_ANALYSIS = "specialist_analysis"


_DEFAULT_USE_CASES = tuple(SubagentUseCase)
_DEFAULT_TOOLS = ("read", "grep", "glob")
_FORBIDDEN_TOOLS = frozenset(
    {
        "write",
        "edit",
        "apply_patch",
        "exec",
        "shell",
        "process",
        "gateway",
        "cron",
        "sessions_spawn",
        "sessions_yield",
        "sessions_send",
        "sessions_list",
        "sessions_history",
        "subagents",
    }
)


@dataclass(frozen=True)
class SubagentProfilePolicy:
    """Bounded delegation settings for one Execraft execution profile."""

    enabled: bool = False
    allowed_use_cases: tuple[SubagentUseCase, ...] = _DEFAULT_USE_CASES
    allowed_tools: tuple[str, ...] = _DEFAULT_TOOLS
    child_model_route_id: str = ""
    max_spawn_depth: int = 1
    max_concurrent: int = 2
    max_children_per_parent: int = 2
    run_timeout_seconds: int = 300
    archive_after_minutes: int = 30
    max_input_tokens: int = 12000
    max_output_tokens: int = 3000
    max_estimated_cost_usd: float = 1.0

    def __post_init__(self) -> None:
        if self.max_spawn_depth != 1:
            raise SubagentPolicyError("sub-agent policy requires max_spawn_depth=1")
        if not 1 <= self.max_concurrent <= 4:
            raise SubagentPolicyError("sub-agent max_concurrent must be in [1, 4]")
        if not 1 <= self.max_children_per_parent <= 4:
            raise SubagentPolicyError("sub-agent max_children_per_parent must be in [1, 4]")
        if not 1 <= self.run_timeout_seconds <= 3600:
            raise SubagentPolicyError("sub-agent run_timeout_seconds must be in [1, 3600]")
        if not 1 <= self.archive_after_minutes <= 24 * 60:
            raise SubagentPolicyError("sub-agent archive_after_minutes must be in [1, 1440]")
        if not 256 <= self.max_input_tokens <= 131072:
            raise SubagentPolicyError("sub-agent max_input_tokens must be in [256, 131072]")
        if not 64 <= self.max_output_tokens <= 32768:
            raise SubagentPolicyError("sub-agent max_output_tokens must be in [64, 32768]")
        if not math.isfinite(self.max_estimated_cost_usd) or self.max_estimated_cost_usd <= 0:
            raise SubagentPolicyError("sub-agent max_estimated_cost_usd must be finite and > 0")
        if not self.allowed_use_cases:
            raise SubagentPolicyError("sub-agent allowed_use_cases cannot be empty")
        if not self.allowed_tools:
            raise SubagentPolicyError("sub-agent allowed_tools cannot be empty")
        normalized_tools = tuple(
            tool.strip().lower() for tool in self.allowed_tools if tool.strip()
        )
        if len(normalized_tools) != len(self.allowed_tools):
            raise SubagentPolicyError("sub-agent allowed_tools cannot contain empty values")
        if len(set(normalized_tools)) != len(normalized_tools):
            raise SubagentPolicyError("sub-agent allowed_tools must be unique")
        if tuple(self.allowed_tools) != normalized_tools:
            raise SubagentPolicyError("sub-agent allowed_tools must use normalized lowercase names")
        forbidden = sorted(set(normalized_tools) & _FORBIDDEN_TOOLS)
        if forbidden:
            raise SubagentPolicyError(
                "sub-agent specialist tools must remain read-oriented; forbidden: "
                + ", ".join(forbidden)
            )
        if "*" in normalized_tools:
            raise SubagentPolicyError("sub-agent policy does not permit wildcard tool access")

    def as_mapping(self) -> dict[str, object]:
        result: dict[str, object] = {
            "enabled": self.enabled,
            "use_cases": [item.value for item in self.allowed_use_cases],
            "allowed_tools": list(self.allowed_tools),
            "max_spawn_depth": self.max_spawn_depth,
            "max_concurrent": self.max_concurrent,
            "max_children_per_parent": self.max_children_per_parent,
            "run_timeout_seconds": self.run_timeout_seconds,
            "archive_after_minutes": self.archive_after_minutes,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "max_estimated_cost_usd": self.max_estimated_cost_usd,
        }
        if self.child_model_route_id:
            result["child_model_route"] = self.child_model_route_id
        return result


@dataclass(frozen=True)
class SubagentStrategy:
    """Project-level sub-agent kill switch and per-profile policy registry."""

    enabled: bool = False
    profiles: Mapping[str, SubagentProfilePolicy] = field(default_factory=dict)

    def for_profile(self, profile_id: str) -> SubagentProfilePolicy:
        policy = self.profiles.get(profile_id)
        if policy is None or not self.enabled:
            return SubagentProfilePolicy()
        return policy

    @property
    def active(self) -> bool:
        return self.enabled and any(policy.enabled for policy in self.profiles.values())

    def as_mapping(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "profiles": {
                key: self.profiles[key].as_mapping() for key in sorted(self.profiles)
            },
        }


def parse_subagent_strategy(raw_config: Mapping[str, Any]) -> SubagentStrategy:
    """Parse the co-located ``subagents`` policy without changing schema-v4 topology."""

    raw = raw_config.get("subagents")
    if raw is None:
        return SubagentStrategy()
    if not isinstance(raw, Mapping):
        raise SubagentPolicyError("agents.yaml subagents must be a mapping")
    _reject_unknown(raw, {"enabled", "profiles"}, "subagents")
    enabled = _bool(raw, "enabled", False, "subagents")
    profiles_raw = raw.get("profiles", {})
    if not isinstance(profiles_raw, Mapping):
        raise SubagentPolicyError("agents.yaml subagents.profiles must be a mapping")
    profiles: dict[str, SubagentProfilePolicy] = {}
    for raw_profile_id, raw_policy in profiles_raw.items():
        profile_id = str(raw_profile_id).strip()
        if not profile_id:
            raise SubagentPolicyError("subagent profile id cannot be empty")
        if not isinstance(raw_policy, Mapping):
            raise SubagentPolicyError(f"subagents.profiles.{profile_id} must be a mapping")
        profiles[profile_id] = _parse_profile(raw_policy, profile_id)
    return SubagentStrategy(enabled=enabled, profiles=profiles)


def validate_subagent_strategy(strategy: SubagentStrategy, execution: Any) -> None:
    """Validate references against normalized execution topology.

    Sub-agent delegation supports managed OpenClaw only. External Gateways remain externally
    configured, so Execraft cannot prove that a delegation identity/tool policy
    exists there and therefore fails closed.
    """

    if not strategy.active:
        return
    known = {profile.id: profile for profile in execution.agents}
    for profile_id, policy in strategy.profiles.items():
        if not policy.enabled:
            continue
        profile = known.get(profile_id)
        if profile is None:
            raise SubagentPolicyError(f"unknown sub-agent profile: {profile_id}")
        runtime = execution.runtime(profile.runtime_id)
        kind = str(getattr(getattr(runtime, "kind", ""), "value", getattr(runtime, "kind", "")))
        if kind != "openclaw":
            raise SubagentPolicyError(
                f"sub-agent profile {profile_id!r} must select an OpenClaw runtime"
            )
        options = getattr(runtime, "openclaw", None)
        mode = str(getattr(getattr(options, "mode", ""), "value", getattr(options, "mode", "")))
        if mode != "managed":
            raise SubagentPolicyError(
                f"sub-agent profile {profile_id!r} requires managed OpenClaw; "
                "external Gateway delegation cannot be proven by Execraft"
            )
        if policy.child_model_route_id:
            try:
                execution.model_route(policy.child_model_route_id)
            except KeyError as exc:
                raise SubagentPolicyError(
                    f"sub-agent profile {profile_id!r} references unknown child model route "
                    f"{policy.child_model_route_id!r}"
                ) from exc


def infer_subagent_use_case(
    *, capability: str, stage: str, metadata: Mapping[str, Any]
) -> SubagentUseCase:
    explicit = str(metadata.get("subagent_use_case", "")).strip().lower()
    if explicit:
        try:
            return SubagentUseCase(explicit)
        except ValueError as exc:
            raise SubagentPolicyError(f"unsupported sub-agent use case: {explicit!r}") from exc
    value = f"{capability}:{stage}".lower()
    if "review" in value:
        return SubagentUseCase.STATIC_REVIEW
    if "verify" in value or "test" in value:
        return SubagentUseCase.TEST_FAILURE_CLASSIFICATION
    if "doc" in value:
        return SubagentUseCase.DOCUMENTATION_LOOKUP
    if "implement" in value or "fix" in value:
        return SubagentUseCase.FOCUSED_SEARCH
    return SubagentUseCase.SPECIALIST_ANALYSIS


def request_has_parallelism(request: Any) -> bool:
    """Detect Execraft-owned shard/wave parallelism without importing orchestrator internals."""

    values: list[Mapping[str, Any]] = []
    metadata = getattr(request, "metadata", None)
    if isinstance(metadata, Mapping):
        values.append(metadata)
    handoff = getattr(request, "handoff", None)
    context = getattr(handoff, "execution_context", None)
    if isinstance(context, Mapping):
        values.append(context)
    for mapping in values:
        for nested in _walk_policy_mappings(mapping, max_nodes=32):
            for key in (
                "parallel",
                "parallel_execution",
                "shard_id",
                "shard_index",
                "shard_count",
                "wave",
                "wave_index",
                "wave_size",
            ):
                value = nested.get(key)
                if isinstance(value, bool) and value:
                    return True
                if isinstance(value, int) and value > 1:
                    return True
                if isinstance(value, str):
                    text = value.strip().lower()
                    if text and text not in {"0", "1", "false", "none"}:
                        return True
    return False


def _walk_policy_mappings(value: Mapping[str, Any], *, max_nodes: int):
    pending: list[Any] = [value]
    seen = 0
    while pending and seen < max_nodes:
        current = pending.pop()
        seen += 1
        if isinstance(current, Mapping):
            yield current
            pending.extend(current.values())
        elif isinstance(current, (list, tuple)):
            pending.extend(current)


def _parse_profile(raw: Mapping[str, Any], profile_id: str) -> SubagentProfilePolicy:
    allowed = {
        "enabled",
        "use_cases",
        "allowed_tools",
        "child_model_route",
        "max_spawn_depth",
        "max_concurrent",
        "max_children_per_parent",
        "run_timeout_seconds",
        "archive_after_minutes",
        "max_input_tokens",
        "max_output_tokens",
        "max_estimated_cost_usd",
    }
    _reject_unknown(raw, allowed, f"subagents.profiles.{profile_id}")
    use_cases_raw = raw.get("use_cases", [item.value for item in _DEFAULT_USE_CASES])
    if not isinstance(use_cases_raw, list) or not all(
        isinstance(item, str) for item in use_cases_raw
    ):
        raise SubagentPolicyError(
            f"subagents.profiles.{profile_id}.use_cases must be a string list"
        )
    try:
        use_cases = tuple(SubagentUseCase(item.strip().lower()) for item in use_cases_raw)
    except ValueError as exc:
        raise SubagentPolicyError(
            f"subagents.profiles.{profile_id} has an unsupported use case"
        ) from exc
    tools = _strings(
        raw.get("allowed_tools", list(_DEFAULT_TOOLS)),
        f"subagents.profiles.{profile_id}.allowed_tools",
    )
    return SubagentProfilePolicy(
        enabled=_bool(raw, "enabled", False, f"subagents.profiles.{profile_id}"),
        allowed_use_cases=use_cases,
        allowed_tools=tools,
        child_model_route_id=str(raw.get("child_model_route", "")).strip(),
        max_spawn_depth=_int(raw, "max_spawn_depth", 1, profile_id),
        max_concurrent=_int(raw, "max_concurrent", 2, profile_id),
        max_children_per_parent=_int(raw, "max_children_per_parent", 2, profile_id),
        run_timeout_seconds=_int(raw, "run_timeout_seconds", 300, profile_id),
        archive_after_minutes=_int(raw, "archive_after_minutes", 30, profile_id),
        max_input_tokens=_int(raw, "max_input_tokens", 12000, profile_id),
        max_output_tokens=_int(raw, "max_output_tokens", 3000, profile_id),
        max_estimated_cost_usd=_float(raw, "max_estimated_cost_usd", 1.0, profile_id),
    )


def _reject_unknown(raw: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(str(key) for key in raw if str(key) not in allowed)
    if unknown:
        raise SubagentPolicyError(f"unsupported field for {label}: {', '.join(unknown)}")


def _bool(raw: Mapping[str, Any], key: str, default: bool, label: str) -> bool:
    if key not in raw:
        return default
    value = raw[key]
    if not isinstance(value, bool):
        raise SubagentPolicyError(f"{label}.{key} must be true or false")
    return value


def _int(raw: Mapping[str, Any], key: str, default: int, label: str) -> int:
    if key not in raw:
        return default
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise SubagentPolicyError(f"subagents.profiles.{label}.{key} must be an integer")
    return value


def _float(raw: Mapping[str, Any], key: str, default: float, label: str) -> float:
    if key not in raw:
        return default
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SubagentPolicyError(f"subagents.profiles.{label}.{key} must be numeric")
    return float(value)


def _strings(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SubagentPolicyError(f"{label} must be a string list")
    return tuple(item.strip() for item in value)
