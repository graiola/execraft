"""Review assignment and independence-relaxation policy.

Independent providers are preferred for review checks.  Ordinary work may
relax that preference when a project intentionally runs with a single capable
provider, while repository-sync packages that explicitly require independent
review remain fail-closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .models import WorkPackage, WorkPackageKind
from .scheduler import AgentCapability, AgentSchedule


@dataclass(frozen=True)
class FixerSelection:
    """Selected fixer and progressively relaxed failover policy tiers."""

    agent_id: str = ""
    excluded_agent_ids: frozenset[str] = frozenset()
    policy: str = "none"
    fallback_tiers: tuple[tuple[str, frozenset[str]], ...] = ()


class ReviewPolicyHost(Protocol):
    """Small host surface required to maintain persisted review assignments."""

    config: Any
    _journal: Any

    def _select_agent_for_capability(
        self,
        capability: AgentCapability,
        *,
        package: WorkPackage | None = None,
        exclude_ids: set[str] | None = None,
        preference_role: str = "",
        **kwargs: Any,
    ) -> str | None: ...

    def _agent_ineligibility_reason(
        self,
        agent_id: str,
        capability: AgentCapability,
        package: WorkPackage,
        *,
        excluded_ids: set[str] | None = None,
    ) -> str: ...

    def _binding_role_exclusions(
        self, package: WorkPackage | None, role: str
    ) -> set[str]: ...
    def save_state(self, *, reason: str = "state_update") -> Any: ...
    def _emit_progress(self, event_type: str, **payload: Any) -> None: ...


def requires_independent_review(package: WorkPackage) -> bool:
    """Return whether the package explicitly forbids same-provider review."""

    return bool(
        package.kind == WorkPackageKind.REPOSITORY_SYNC
        and package.repository_sync is not None
        and package.repository_sync.require_independent_review
    )


def same_provider_review_allowed(config: Any, package: WorkPackage) -> bool:
    """Return whether review independence may degrade for this package."""

    return bool(config.allow_same_provider_review) and not requires_independent_review(
        package
    )


def primary_review_fallback_tiers(
    config: Any, package: WorkPackage
) -> list[tuple[str, set[str]]]:
    """Execution fallback that permits the implementer to review as last resort."""

    if not same_provider_review_allowed(config, package):
        return []
    return [("reuse_implementer_for_review", set())]


def final_review_fallback_tiers(
    config: Any,
    package: WorkPackage,
    schedule: AgentSchedule,
) -> list[tuple[str, set[str]]]:
    """Progressively relax final-review independence without skipping the check."""

    if not same_provider_review_allowed(config, package):
        return []
    implementer = schedule.implementer_id
    last_fixer = package.last_fixer_id
    return [
        (
            "reuse_primary_reviewer_for_final_review",
            {value for value in (implementer, last_fixer) if value},
        ),
        (
            "reuse_fixer_for_final_review",
            {value for value in (implementer,) if value},
        ),
        ("reuse_provider_for_final_review", set()),
    ]


def _eligible_for_final_review(
    host: ReviewPolicyHost,
    package: WorkPackage,
    agent_id: str,
) -> bool:
    if not agent_id:
        return False
    binding_exclusions = host._binding_role_exclusions(package, "final_review")
    return not host._agent_ineligibility_reason(
        agent_id,
        AgentCapability.REVIEW,
        package,
        excluded_ids=binding_exclusions,
    )


def _relaxed_final_reviewer(
    host: ReviewPolicyHost,
    package: WorkPackage,
    *,
    implementer: str,
    reviewer: str,
) -> str:
    """Choose the least-relaxed eligible reviewer for a degraded final check."""

    for candidate in (reviewer, package.last_fixer_id, implementer):
        if _eligible_for_final_review(host, package, candidate):
            return candidate
    return (
        host._select_agent_for_capability(
            AgentCapability.REVIEW,
            package=package,
            exclude_ids=set(),
            preference_role="final_review",
        )
        or ""
    )


def schedule_agents(host: ReviewPolicyHost, package: WorkPackage) -> AgentSchedule:
    """Schedule implementation plus primary/final review with graceful degradation."""

    implementer = (
        host._select_agent_for_capability(AgentCapability.IMPLEMENT, package=package)
        or ""
    )
    reviewer = host._select_agent_for_capability(
        AgentCapability.REVIEW,
        package=package,
        exclude_ids={implementer} if implementer else set(),
    ) or ""
    reviewer_relaxed = False
    if (
        not reviewer
        and implementer
        and same_provider_review_allowed(host.config, package)
    ):
        reviewer = host._select_agent_for_capability(
            AgentCapability.REVIEW, package=package, exclude_ids=set()
        ) or ""
        reviewer_relaxed = bool(reviewer)

    final_excluded = {
        value for value in (implementer, reviewer, package.last_fixer_id) if value
    }
    final_reviewer = host._select_agent_for_capability(
        AgentCapability.REVIEW,
        package=package,
        exclude_ids=final_excluded,
        preference_role="final_review",
    ) or ""
    final_relaxed = False
    if (
        not final_reviewer
        and reviewer
        and same_provider_review_allowed(host.config, package)
    ):
        final_reviewer = _relaxed_final_reviewer(
            host, package, implementer=implementer, reviewer=reviewer
        )
        final_relaxed = bool(final_reviewer)

    schedule = AgentSchedule(implementer, reviewer, final_reviewer)
    if reviewer_relaxed:
        host._journal.append(
            "reviewer_independence_fallback",
            {
                "package_id": package.id,
                "implementer": implementer,
                "reviewer": reviewer,
            },
        )
    if final_relaxed:
        host._journal.append(
            "final_reviewer_independence_fallback",
            {
                "package_id": package.id,
                "implementer": implementer,
                "reviewer": reviewer,
                "final_reviewer": final_reviewer,
            },
        )
    return schedule


def ensure_review_assignments(
    host: ReviewPolicyHost, package: WorkPackage
) -> AgentSchedule:
    """Repair persisted review assignments after failover or policy changes."""

    changed = False
    reviewer_excluded = {
        value for value in (package.agent_id, package.last_fixer_id) if value
    }
    reviewer = package.reviewer_id
    if host._agent_ineligibility_reason(
        reviewer,
        AgentCapability.REVIEW,
        package,
        excluded_ids=reviewer_excluded,
    ):
        reviewer = host._select_agent_for_capability(
            AgentCapability.REVIEW,
            package=package,
            exclude_ids=reviewer_excluded,
        ) or ""
        if (
            not reviewer
            and package.agent_id
            and same_provider_review_allowed(host.config, package)
        ):
            reviewer = host._select_agent_for_capability(
                AgentCapability.REVIEW,
                package=package,
                exclude_ids={package.last_fixer_id} if package.last_fixer_id else set(),
            ) or ""
            if not reviewer:
                reviewer = host._select_agent_for_capability(
                    AgentCapability.REVIEW, package=package, exclude_ids=set()
                ) or ""
        if reviewer != package.reviewer_id:
            package.reviewer_id = reviewer
            changed = True

    final_excluded = {
        value
        for value in (package.agent_id, package.reviewer_id, package.last_fixer_id)
        if value
    }
    final = package.final_reviewer_id
    if host._agent_ineligibility_reason(
        final,
        AgentCapability.REVIEW,
        package,
        excluded_ids=final_excluded,
    ):
        final = host._select_agent_for_capability(
            AgentCapability.REVIEW,
            package=package,
            exclude_ids=final_excluded,
            preference_role="final_review",
        ) or ""
        if (
            not final
            and package.reviewer_id
            and same_provider_review_allowed(host.config, package)
        ):
            final = _relaxed_final_reviewer(
                host,
                package,
                implementer=package.agent_id,
                reviewer=package.reviewer_id,
            )
        if final != package.final_reviewer_id:
            package.final_reviewer_id = final
            changed = True

    if changed:
        host.save_state()
        payload = {
            "package_id": package.id,
            "implementer": package.agent_id,
            "reviewer": package.reviewer_id,
            "final_reviewer": package.final_reviewer_id,
            "last_fixer": package.last_fixer_id,
        }
        host._journal.append("agent_assignments_rebalanced", payload)
        host._emit_progress("agents_rebalanced", **payload)
    return AgentSchedule(
        package.agent_id,
        package.reviewer_id,
        package.final_reviewer_id,
    )
