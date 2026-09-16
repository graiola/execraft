"""Declarative PLAN.graph contract helpers.

`PLAN.graph.yaml` is task-definition input. Runtime orchestration state belongs
in the state/evidence stores and must never be published back into a new
versioned task definition. Older Execraft tasks predate that separation and may
still contain fields such as ``stage``, ``status``, ``verified`` and
``evidence`` in their authored graph.

This module centralizes two intentionally different operations:

* strict validation of a declarative graph; and
* compatibility projection of *known* legacy runtime fields out of an older
  hybrid graph.

The compatibility projection is conservative: it removes only fields that are
owned by runtime orchestration. Unknown fields are preserved so strict
validation still rejects accidental or unsupported task semantics instead of
silently discarding them.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping


class DeclarativePlanGraphError(ValueError):
    """Raised when a plan graph contains non-declarative package state."""


# Keep this set aligned with the fields the definition service versions as authored
# task semantics. Runtime defaults (stage/status/etc.) are supplied by
# WorkPackage deserialization when the graph enters orchestration state.
DECLARATIVE_PACKAGE_FIELDS = frozenset(
    {
        "id",
        "title",
        "dependencies",
        "requirements",
        "acceptance_criteria",
        "affected_repositories",
        "kind",
        "repository_sync",
        "risk",
        "priority",
        "complexity",
        "verification_profile",
        "agent_preferences",
        "skill_preferences",
        "parallel_safe",
        "read_scope",
        "write_scope",
        "conflict_keys",
    }
)

DECLARATIVE_ACCEPTANCE_CRITERION_FIELDS = frozenset({"id", "description"})

# Runtime-owned WorkPackage fields that appeared in historical PLAN.graph.yaml
# files or can be emitted by older planners. These are safe to remove from a
# compatibility projection because none participate in the declarative
# contract comparison used by replanning.
LEGACY_RUNTIME_PACKAGE_FIELDS = frozenset(
    {
        "stage",
        "status",
        "agent_id",
        "reviewer_id",
        "final_reviewer_id",
        "last_fixer_id",
        "agent_history",
        "verification_attempts",
        "review_cycles",
        "review_findings",
        "review_recovery_cycles",
        "review_recovery_fingerprint",
        "review_recovery_origin_stage",
        "implementation_summary",
        "last_implementation",
        "last_verification",
        "last_review",
        "last_agent_attempts",
        "last_invocation_id",
        "execution_mode",
        "parent_id",
        "shard_key",
        "generated_by",
        "decomposition_status",
        "decomposition_origin_stage",
        "decomposition_reason",
        "decomposition_agent_id",
        "decomposition_plan_hash",
        "shard_ids",
        "operator_paused",
        "operator_pause_reason",
        "operator_paused_at",
        "pause_before_start",
        "pause_before_start_reason",
        "pause_before_start_requested_at",
        "pause_before_start_reached_at",
        "pause_after_completion",
        "pause_after_completion_reason",
        "pause_after_completion_requested_at",
        "pause_after_completion_reached_at",
        "decomposition_required",
        "decomposition_required_reason",
        "decomposition_required_at",
        "decomposition_required_consumed_at",
    }
)

LEGACY_RUNTIME_ACCEPTANCE_CRITERION_FIELDS = frozenset({"verified", "evidence"})


@dataclass(frozen=True)
class PlanGraphCanonicalization:
    """Result of projecting a legacy hybrid graph to declarative form."""

    graph: dict[str, Any]
    removed_fields: tuple[str, ...] = ()
    legacy_completed_package_ids: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.removed_fields)


def validate_declarative_plan_graph_mapping(raw: Mapping[str, Any]) -> None:
    """Reject package/runtime fields that do not belong in task definition.

    Error wording deliberately matches the task-definition validator so CLI/GUI
    diagnostics and existing regression contracts remain stable.
    """

    packages = raw.get("work_packages") or raw.get("packages") or []
    if not isinstance(packages, list):
        return
    for index, package in enumerate(packages, start=1):
        if not isinstance(package, Mapping):
            continue
        runtime_fields = sorted(
            set(str(key) for key in package) - DECLARATIVE_PACKAGE_FIELDS
        )
        if runtime_fields:
            package_id = str(package.get("id", index))
            raise DeclarativePlanGraphError(
                f"candidate package {package_id} contains non-declarative/runtime fields: "
                + ", ".join(runtime_fields)
            )
        criteria = package.get("acceptance_criteria") or []
        if not isinstance(criteria, list):
            continue
        for criterion in criteria:
            if not isinstance(criterion, Mapping):
                continue
            forbidden = sorted(
                set(str(key) for key in criterion)
                - DECLARATIVE_ACCEPTANCE_CRITERION_FIELDS
            )
            if forbidden:
                package_id = str(package.get("id", index))
                raise DeclarativePlanGraphError(
                    f"candidate package {package_id} acceptance criteria contain "
                    "runtime/evidence fields: " + ", ".join(forbidden)
                )


def canonicalize_legacy_plan_graph_mapping(
    raw: Mapping[str, Any],
) -> PlanGraphCanonicalization:
    """Remove only known historical runtime fields from a copied graph.

    This helper exists for migration/compatibility boundaries such as
    repository synchronization of tasks created before the current definition format. It is *not* a
    generic sanitizer: unsupported/unknown fields are preserved and therefore
    remain visible to :func:`validate_declarative_plan_graph_mapping`.
    """

    graph = deepcopy(dict(raw))
    removed: list[str] = []
    legacy_completed: list[str] = []

    package_keys = [key for key in ("work_packages", "packages") if key in graph]
    for package_key in package_keys:
        packages = graph.get(package_key)
        if not isinstance(packages, list):
            continue
        normalized_packages: list[Any] = []
        for index, original in enumerate(packages, start=1):
            if not isinstance(original, Mapping):
                normalized_packages.append(original)
                continue
            package = dict(original)
            package_id = str(package.get("id", index)).strip() or str(index)
            stage = str(package.get("stage", "")).strip().lower()
            status = str(package.get("status", "")).strip().lower()
            if stage == "completed" or status == "completed":
                legacy_completed.append(package_id)

            for field in LEGACY_RUNTIME_PACKAGE_FIELDS:
                if field in package:
                    package.pop(field, None)
                    removed.append(f"{package_id}.{field}")

            criteria = package.get("acceptance_criteria")
            if isinstance(criteria, list):
                normalized_criteria: list[Any] = []
                for criterion_index, original_criterion in enumerate(criteria, start=1):
                    if not isinstance(original_criterion, Mapping):
                        normalized_criteria.append(original_criterion)
                        continue
                    criterion = dict(original_criterion)
                    criterion_id = str(
                        criterion.get("id", criterion_index)
                    ).strip() or str(criterion_index)
                    for field in LEGACY_RUNTIME_ACCEPTANCE_CRITERION_FIELDS:
                        if field in criterion:
                            criterion.pop(field, None)
                            removed.append(
                                f"{package_id}.acceptance_criteria[{criterion_id}].{field}"
                            )
                    normalized_criteria.append(criterion)
                package["acceptance_criteria"] = normalized_criteria
            normalized_packages.append(package)
        graph[package_key] = normalized_packages

    return PlanGraphCanonicalization(
        graph=graph,
        removed_fields=tuple(removed),
        legacy_completed_package_ids=tuple(dict.fromkeys(legacy_completed)),
    )


__all__ = [
    "DECLARATIVE_ACCEPTANCE_CRITERION_FIELDS",
    "DECLARATIVE_PACKAGE_FIELDS",
    "DeclarativePlanGraphError",
    "LEGACY_RUNTIME_ACCEPTANCE_CRITERION_FIELDS",
    "LEGACY_RUNTIME_PACKAGE_FIELDS",
    "PlanGraphCanonicalization",
    "canonicalize_legacy_plan_graph_mapping",
    "validate_declarative_plan_graph_mapping",
]
