"""Pure planning for durable provider-availability waits.

The orchestrator owns persistence, journaling, state transitions, and human
escalation.  This module owns the deterministic calculation of wait cycles,
provider candidates, retry deadlines, and progress metadata.  Keeping that
calculation side-effect free makes scheduling policy independently testable and
prevents time arithmetic from being duplicated across foreground and daemon
execution paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class AgentWaitConfig:
    """Timing policy used to build one provider wait schedule."""

    retry_initial_seconds: float
    retry_max_seconds: float
    poll_max_seconds: float
    known_deadline_poll_max_seconds: float
    blocking_wait_max_seconds: float


@dataclass(frozen=True)
class AgentWaitAdapter:
    """Provider state required by the wait planner."""

    agent_id: str
    model: str
    availability: str
    health_available: bool
    health_reason: str
    unavailable_until: str
    max_complexity: int


@dataclass(frozen=True)
class AgentWaitAttempt:
    """Normalized result of a provider attempt in the current stage."""

    agent_id: str
    classification: str
    error: str
    retry_after_seconds: float | None = None

    def as_mapping(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "classification": self.classification,
            "error": self.error[:1000],
        }


@dataclass(frozen=True)
class AgentWaitPlan:
    """One deterministic durable wait schedule."""

    package_id: str
    stage: str
    capability: str
    since: str
    cycle: int
    waited_seconds: float
    expired: bool
    poll_after_seconds: float
    next_check_at: str
    candidates: tuple[dict[str, Any], ...]
    policy_excluded: tuple[dict[str, Any], ...]
    attempts: tuple[dict[str, Any], ...]
    all_deadlines_known: bool
    next_retry: Mapping[str, Any]
    first_reported_unblock: Mapping[str, Any]

    def as_wait_mapping(self) -> dict[str, Any]:
        return {
            "package_id": self.package_id,
            "stage": self.stage,
            "capability": self.capability,
            "since": self.since,
            "cycle": self.cycle,
            "next_check_at": self.next_check_at,
            "poll_after_seconds": self.poll_after_seconds,
            "candidates": [dict(item) for item in self.candidates],
            "policy_excluded": [dict(item) for item in self.policy_excluded],
            "attempts": [dict(item) for item in self.attempts],
        }


def parse_deadline(value: str) -> datetime | None:
    """Parse an ISO timestamp and normalize naive values to UTC."""

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def seconds_until(value: str, *, now: datetime | None = None) -> float | None:
    """Return non-negative seconds until ``value`` or ``None`` when invalid."""

    deadline = parse_deadline(value)
    if deadline is None:
        return None
    current = now or datetime.now(timezone.utc)
    return max(0.0, (deadline - current).total_seconds())


def seconds_since(value: str, *, now: datetime | None = None) -> float | None:
    """Return non-negative seconds since ``value`` or ``None`` when invalid."""

    started = parse_deadline(value)
    if started is None:
        return None
    current = now or datetime.now(timezone.utc)
    return max(0.0, (current - started).total_seconds())


def _deadline_key(item: Mapping[str, Any]) -> tuple[datetime, str]:
    deadline = parse_deadline(str(item.get("available_at", "")))
    return (
        deadline or datetime.max.replace(tzinfo=timezone.utc),
        str(item.get("agent_id", "")),
    )


def _earliest_candidate(
    candidates: Sequence[Mapping[str, Any]],
    kinds: set[str],
) -> Mapping[str, Any]:
    return min(
        (
            item
            for item in candidates
            if str(item.get("deadline_kind", "")) in kinds
        ),
        key=_deadline_key,
        default={},
    )


def build_agent_wait_plan(
    *,
    package_id: str,
    stage: str,
    capability: str,
    previous: Mapping[str, Any],
    attempts: Sequence[AgentWaitAttempt],
    adapters: Sequence[AgentWaitAdapter],
    excluded_agent_ids: set[str],
    task_complexity: int,
    config: AgentWaitConfig,
    now: datetime | None = None,
) -> AgentWaitPlan:
    """Build the next wait schedule without mutating orchestrator state."""

    current = now or datetime.now(timezone.utc)
    same_wait = (
        previous.get("package_id") == package_id
        and previous.get("stage") == stage
        and previous.get("capability") == capability
    )
    cycle = int(previous.get("cycle", 0)) + 1 if same_wait else 1
    since = str(previous.get("since", "")) if same_wait else current.isoformat()
    waited_seconds = seconds_since(since, now=current) or 0.0
    wait_limit = max(0.0, float(config.blocking_wait_max_seconds))
    expired = bool(same_wait and wait_limit and waited_seconds >= wait_limit)

    bounded_exponent = min(60, max(0, cycle - 1))
    backoff = min(
        config.retry_initial_seconds * (2.0**bounded_exponent),
        config.retry_max_seconds,
    )
    attempted_by_id = {item.agent_id: item for item in attempts if item.agent_id}
    candidates: list[dict[str, Any]] = []
    policy_excluded: list[dict[str, Any]] = []
    delays: list[float] = []

    for adapter in adapters:
        if adapter.agent_id in excluded_agent_ids:
            continue
        if task_complexity > adapter.max_complexity:
            policy_excluded.append(
                {
                    "agent_id": adapter.agent_id,
                    "model": adapter.model,
                    "reason": "complexity_limit",
                    "task_complexity": task_complexity,
                    "max_complexity": adapter.max_complexity,
                }
            )
            continue

        attempt = attempted_by_id.get(adapter.agent_id)
        # Durable provider health is authoritative when it carries a cooldown
        # deadline. The adapter-local availability flag describes only this
        # in-memory instance's last observation and commonly remains
        # SESSION_LIMIT/QUOTA_EXHAUSTED immediately after a failure. Checking it
        # first discarded an exact persisted reset timestamp and made the wait
        # planner report an "unknown" deadline until the next process restart.
        if not adapter.health_available:
            health_delay = seconds_until(adapter.unavailable_until, now=current)
            delay = (
                health_delay if health_delay is not None else config.poll_max_seconds
            )
            reason = adapter.health_reason or "provider_unavailable"
            available_at = adapter.unavailable_until
            deadline_kind = "health_unblock" if available_at else "unknown"
        elif adapter.availability != "available":
            delay = config.poll_max_seconds
            reason = f"adapter_{adapter.availability}"
            available_at = ""
            deadline_kind = "unknown"
        elif attempt is not None:
            requested = attempt.retry_after_seconds
            delay = max(backoff, requested) if requested is not None else backoff
            reason = attempt.classification or "retry_backoff"
            available_at = (current + timedelta(seconds=delay)).isoformat()
            deadline_kind = (
                "provider_retry_after" if requested is not None else "orchestrator_backoff"
            )
        else:
            delay = 1.0
            reason = "eligible_on_next_poll"
            available_at = (current + timedelta(seconds=delay)).isoformat()
            deadline_kind = "orchestrator_probe"

        delays.append(max(1.0, float(delay)))
        candidates.append(
            {
                "agent_id": adapter.agent_id,
                "model": adapter.model,
                "reason": reason,
                "available_at": available_at,
                "deadline_kind": deadline_kind,
            }
        )

    if not candidates and not policy_excluded:
        delays.append(config.poll_max_seconds)
        candidates.append(
            {
                "agent_id": "",
                "model": "",
                "reason": "no_compatible_agent_configured",
                "available_at": "",
                "deadline_kind": "unknown",
            }
        )

    concrete = [item for item in candidates if item.get("agent_id")]
    all_deadlines_known = bool(concrete) and all(item.get("available_at") for item in concrete)
    poll_cap = (
        config.known_deadline_poll_max_seconds
        if all_deadlines_known
        else config.poll_max_seconds
    )
    if delays:
        delay = min(min(delays), poll_cap)
    else:
        delay = poll_cap
    poll_after_seconds = max(1.0, float(delay))
    next_check_at = (current + timedelta(seconds=poll_after_seconds)).isoformat()

    dated = [item for item in concrete if parse_deadline(str(item.get("available_at", "")))]
    return AgentWaitPlan(
        package_id=package_id,
        stage=stage,
        capability=capability,
        since=since,
        cycle=cycle,
        waited_seconds=waited_seconds,
        expired=expired,
        poll_after_seconds=poll_after_seconds,
        next_check_at=next_check_at,
        candidates=tuple(candidates),
        policy_excluded=tuple(policy_excluded),
        attempts=tuple(item.as_mapping() for item in attempts),
        all_deadlines_known=all_deadlines_known,
        next_retry=_earliest_candidate(
            dated,
            {"orchestrator_backoff", "orchestrator_probe"},
        ),
        first_reported_unblock=_earliest_candidate(
            dated,
            {"provider_retry_after", "health_unblock"},
        ),
    )


__all__ = [
    "AgentWaitAdapter",
    "AgentWaitAttempt",
    "AgentWaitConfig",
    "AgentWaitPlan",
    "build_agent_wait_plan",
    "parse_deadline",
    "seconds_since",
    "seconds_until",
]
