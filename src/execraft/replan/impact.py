"""Pure impact analysis and state reconciliation for task replanning.

This module contains no filesystem or provider side effects. Keeping contract
comparison separate from publication makes replanning decisions deterministic,
unit-testable, and reusable by future GUI/API surfaces.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Sequence

from execraft.orchestrate.directives import PAUSE_FOR_REPOSITORY_SYNC
from execraft.orchestrate.models import (
    PlanGraph,
    TaskExecutionState,
    TaskExecutionStateRecord,
    WorkPackage,
    WorkPackageStage,
    utc_now,
)

from .models import PackageImpact, ReplanImpact

def analyze_impact(
    state: TaskExecutionStateRecord,
    candidate_graph: PlanGraph,
    mapping: Mapping[str, str],
    *,
    current_revision: int,
    historical_package_ids: Sequence[str],
) -> ReplanImpact:
    """Compare declarative candidate work with durable runtime state.

    Runtime decomposition expands ``state.plan_graph`` with generated shard
    packages and mutates parent dependencies. Those details are execution
    history, not authored task semantics, so shard descendants follow their
    parent and are never required to appear in a candidate graph.
    """

    runtime_by_id = {item.id: item for item in state.plan_graph.work_packages}
    old_by_id = {
        item.id: item for item in state.plan_graph.work_packages if not item.parent_id
    }
    generated_by_parent: dict[str, list[WorkPackage]] = {}
    for item in state.plan_graph.work_packages:
        if item.parent_id:
            generated_by_parent.setdefault(item.parent_id, []).append(item)
    generated_ids = {
        child.id for children in generated_by_parent.values() for child in children
    }
    new_by_id = {item.id: item for item in candidate_graph.work_packages}
    historical_ids = {
        str(item).strip()
        for item in historical_package_ids
        if str(item).strip()
    }
    classifications: list[PackageImpact] = []
    blockers: list[str] = []
    warnings: list[str] = []
    clean_required: list[str] = []
    completed: list[str] = []
    active: list[str] = []
    pending: list[str] = []
    parent_classification: dict[str, str] = {}

    for child in sorted(
        (item for item in state.plan_graph.work_packages if item.parent_id),
        key=lambda item: item.id,
    ):
        if child.parent_id not in old_by_id:
            blockers.append(
                f"runtime-generated package {child.id} references missing parent "
                f"{child.parent_id}; repair orchestration state before replanning"
            )

    collisions = sorted(set(new_by_id) & generated_ids)
    if collisions:
        blockers.append(
            "candidate reuses runtime-generated package IDs: " + ", ".join(collisions)
        )
    retired_ids = historical_ids - set(runtime_by_id)
    historical_collisions = sorted(set(new_by_id) & retired_ids)
    if historical_collisions:
        blockers.append(
            "candidate reuses retired/historical package IDs: "
            + ", ".join(historical_collisions)
        )

    for package_id, old in old_by_id.items():
        new = new_by_id.get(package_id)
        child_ids = {item.id for item in generated_by_parent.get(package_id, [])}
        changed = (
            _changed_contract_fields(
                old,
                new,
                generated_dependency_ids=child_ids,
            )
            if new is not None
            else ("removed",)
        )
        if old.stage == WorkPackageStage.COMPLETED:
            completed.append(package_id)
            if new is None:
                blockers.append(
                    f"completed package {package_id} cannot be removed; retain it and add remediation work"
                )
                classification = "completed_removed"
                classifications.append(
                    PackageImpact(
                        package_id,
                        classification,
                        blockers[-1],
                        changed_fields=changed,
                    )
                )
            elif changed:
                blockers.append(
                    f"completed package {package_id} changed semantic contract "
                    f"({', '.join(changed)}); keep it unchanged and add a remediation "
                    "package with a new ID"
                )
                classification = "completed_changed"
                classifications.append(
                    PackageImpact(
                        package_id,
                        classification,
                        blockers[-1],
                        changed_fields=changed,
                    )
                )
            else:
                classification = "completed_preserved"
                classifications.append(
                    PackageImpact(
                        package_id,
                        classification,
                        "completed state and evidence will be preserved",
                    )
                )
            parent_classification[package_id] = classification
            continue

        if _is_active_package(old):
            active.append(package_id)
            if new is not None and not changed:
                classification = "active_preserved"
                classifications.append(
                    PackageImpact(
                        package_id,
                        classification,
                        "started package is unchanged and keeps runtime/evidence state",
                    )
                )
                parent_classification[package_id] = classification
                continue
            replacement = str(mapping.get(package_id, "")).strip()
            if new is not None:
                blockers.append(
                    f"started package {package_id} cannot change in place; remove the old "
                    "ID and map it to a new replacement package"
                )
                classification = "active_changed_in_place"
                classifications.append(
                    PackageImpact(
                        package_id,
                        classification,
                        blockers[-1],
                        replacement_id=replacement,
                        changed_fields=changed,
                    )
                )
                parent_classification[package_id] = classification
                continue
            if not replacement:
                blockers.append(
                    f"started package {package_id} was removed without an explicit replacement mapping"
                )
                classification = "active_removed"
                classifications.append(
                    PackageImpact(
                        package_id,
                        classification,
                        blockers[-1],
                        changed_fields=changed,
                    )
                )
                parent_classification[package_id] = classification
                continue
            if replacement not in new_by_id:
                blockers.append(
                    f"replacement {replacement} for started package {package_id} is not "
                    "present in the candidate graph"
                )
                classification = "active_bad_replacement"
                classifications.append(
                    PackageImpact(
                        package_id,
                        classification,
                        blockers[-1],
                        replacement_id=replacement,
                        changed_fields=changed,
                    )
                )
                parent_classification[package_id] = classification
                continue
            if replacement in runtime_by_id:
                blockers.append(
                    f"replacement {replacement} for started package {package_id} must use "
                    "a genuinely new package ID"
                )
                classification = "active_existing_replacement"
                classifications.append(
                    PackageImpact(
                        package_id,
                        classification,
                        blockers[-1],
                        replacement_id=replacement,
                        changed_fields=changed,
                    )
                )
                parent_classification[package_id] = classification
                continue
            clean_required.append(package_id)
            classification = "active_superseded"
            classifications.append(
                PackageImpact(
                    package_id,
                    classification,
                    "started package and generated descendants remain in the revision "
                    "snapshot and are replaced after workspace safety checks",
                    replacement_id=replacement,
                    changed_fields=changed,
                )
            )
            parent_classification[package_id] = classification
            continue

        pending.append(package_id)
        if new is None:
            classification = "pending_removed"
            classifications.append(
                PackageImpact(
                    package_id,
                    classification,
                    "unstarted package will be removed",
                )
            )
        elif changed:
            classification = "pending_changed"
            classifications.append(
                PackageImpact(
                    package_id,
                    classification,
                    "unstarted package will be replaced by candidate definition",
                    changed_fields=changed,
                )
            )
        else:
            classification = "pending_preserved"
            classifications.append(
                PackageImpact(
                    package_id,
                    classification,
                    "unstarted package is unchanged",
                )
            )
        parent_classification[package_id] = classification

    for parent_id, children in sorted(generated_by_parent.items()):
        disposition = parent_classification.get(parent_id, "")
        preserve = disposition in {"completed_preserved", "active_preserved"}
        retire = disposition == "active_superseded"
        for child in sorted(children, key=lambda item: item.id):
            if preserve:
                classifications.append(
                    PackageImpact(
                        child.id,
                        "generated_preserved",
                        f"runtime-generated shard remains attached to unchanged parent {parent_id}",
                    )
                )
            elif retire:
                classifications.append(
                    PackageImpact(
                        child.id,
                        "generated_retired",
                        f"runtime-generated shard is retired with superseded parent {parent_id}",
                    )
                )

    authored_ids = set(old_by_id)
    added = tuple(sorted(set(new_by_id) - authored_ids))
    removed = tuple(sorted(authored_ids - set(new_by_id)))
    for old_id, new_id in mapping.items():
        if old_id not in old_by_id:
            blockers.append(f"package mapping references unknown old package {old_id}")
        elif old_id not in active:
            blockers.append(f"package mapping may only supersede a started package: {old_id}")
        if new_id not in new_by_id:
            blockers.append(f"package mapping references missing replacement package {new_id}")
        if new_id in runtime_by_id:
            blockers.append(f"package mapping replacement must use a new package ID: {new_id}")
        if old_id == new_id:
            blockers.append(f"package mapping for {old_id} must use a new package ID")
    if not state.plan_graph.work_packages:
        warnings.append(
            "orchestration state has no packages; candidate becomes the initial runtime graph"
        )
    return ReplanImpact(
        current_revision=current_revision,
        candidate_revision=current_revision + 1,
        packages=tuple(classifications),
        added_packages=added,
        removed_packages=removed,
        completed_packages=tuple(sorted(completed)),
        active_packages=tuple(sorted(active)),
        pending_packages=tuple(sorted(pending)),
        requires_clean_workspace=tuple(sorted(set(clean_required))),
        blockers=tuple(dict.fromkeys(blockers)),
        warnings=tuple(dict.fromkeys(warnings)),
    )

def reconcile_state(
    state: TaskExecutionStateRecord,
    graph: PlanGraph,
    impact: ReplanImpact,
    mapping: Mapping[str, str],
) -> TaskExecutionStateRecord:
    old_by_id = {item.id: item for item in state.plan_graph.work_packages}
    generated_by_parent: dict[str, list[WorkPackage]] = {}
    for item in state.plan_graph.work_packages:
        if item.parent_id:
            generated_by_parent.setdefault(item.parent_id, []).append(item)
    preserved_ids = {
        item.package_id
        for item in impact.packages
        if item.classification in {"completed_preserved", "active_preserved"}
    }
    packages: list[WorkPackage] = []
    for candidate in graph.work_packages:
        if candidate.id in preserved_ids:
            packages.append(deepcopy(old_by_id[candidate.id]))
            packages.extend(
                deepcopy(item)
                for item in generated_by_parent.get(candidate.id, [])
            )
        else:
            packages.append(deepcopy(candidate))
    migrated = deepcopy(state)
    migrated.plan_graph = PlanGraph(work_packages=packages)
    migrated.total_packages = len(packages)
    migrated.completed_packages = sum(
        1 for item in packages if item.stage == WorkPackageStage.COMPLETED
    )
    # A card-triggered repository-sync replan is performed while the
    # orchestrator is deliberately stopped at an operator-owned boundary.
    # Keep that ownership record until the coordinator has durably published
    # the sync package and explicitly acknowledges it.  Other transient waits
    # remain invalid across a structural replan and are cleared as before.
    migrated.waiting = (
        deepcopy(state.waiting)
        if (state.waiting or {}).get("kind") == PAUSE_FOR_REPOSITORY_SYNC
        else {}
    )
    migrated.agent_waits = {}
    scheduler = dict(migrated.scheduler or {})
    for key in ("active_assignments", "assignments", "parallel_wave"):
        scheduler.pop(key, None)
    scheduler["last_replan"] = {
        "revision": impact.candidate_revision,
        "at": utc_now(),
        "superseded": {
            old: new
            for old, new in mapping.items()
            if old in impact.active_packages
        },
    }
    migrated.scheduler = scheduler
    migrated.error_message = ""
    if migrated.state not in {
        TaskExecutionState.INITIALIZING,
        TaskExecutionState.VALIDATING_PLAN,
        TaskExecutionState.OPERATOR_PAUSED,
    }:
        migrated.state = TaskExecutionState.RUNNING
    migrated.last_transition_at = utc_now()
    return migrated


def _is_active_package(package: WorkPackage) -> bool:
    return (
        package.stage not in {WorkPackageStage.PREPARE, WorkPackageStage.COMPLETED}
        or package.status == "running"
        or bool(package.last_invocation_id)
        or bool(package.last_implementation)
        or bool(package.last_verification)
        or bool(package.last_review)
    )


def _contract_mapping(
    package: WorkPackage,
    *,
    generated_dependency_ids: set[str] | None = None,
) -> dict[str, Any]:
    generated = generated_dependency_ids or set()
    return {
        "id": package.id,
        "title": package.title,
        "dependencies": [item for item in package.dependencies if item not in generated],
        "requirements": list(package.requirements),
        "acceptance_criteria": [
            {"id": item.id, "description": item.description}
            for item in package.acceptance_criteria
        ],
        "affected_repositories": list(package.affected_repositories),
        "risk": package.risk,
        "priority": package.priority,
        "complexity": package.complexity,
        "verification_profile": package.verification_profile,
        "agent_preferences": deepcopy(package.agent_preferences),
        "skill_preferences": deepcopy(package.skill_preferences),
        "parallel_safe": package.parallel_safe,
        "read_scope": list(package.read_scope),
        "write_scope": list(package.write_scope),
        "conflict_keys": list(package.conflict_keys),
    }


def _changed_contract_fields(
    old: WorkPackage,
    new: WorkPackage | None,
    *,
    generated_dependency_ids: set[str] | None = None,
) -> tuple[str, ...]:
    if new is None:
        return ("removed",)
    old_map = _contract_mapping(old, generated_dependency_ids=generated_dependency_ids)
    new_map = _contract_mapping(new)
    return tuple(sorted(key for key in old_map if old_map[key] != new_map[key]))


def impact_from_mapping(raw: Mapping[str, Any]) -> ReplanImpact:
    return ReplanImpact(
        current_revision=int(raw.get("current_revision", 1)),
        candidate_revision=int(raw.get("candidate_revision", 2)),
        packages=tuple(
            PackageImpact(
                package_id=str(item.get("package_id", "")),
                classification=str(item.get("classification", "")),
                summary=str(item.get("summary", "")),
                replacement_id=str(item.get("replacement_id", "")),
                changed_fields=tuple(str(value) for value in item.get("changed_fields", [])),
            )
            for item in raw.get("packages", [])
            if isinstance(item, Mapping)
        ),
        added_packages=tuple(str(item) for item in raw.get("added_packages", [])),
        removed_packages=tuple(str(item) for item in raw.get("removed_packages", [])),
        completed_packages=tuple(str(item) for item in raw.get("completed_packages", [])),
        active_packages=tuple(str(item) for item in raw.get("active_packages", [])),
        pending_packages=tuple(str(item) for item in raw.get("pending_packages", [])),
        requires_clean_workspace=tuple(str(item) for item in raw.get("requires_clean_workspace", [])),
        blockers=tuple(str(item) for item in raw.get("blockers", [])),
        warnings=tuple(str(item) for item in raw.get("warnings", [])),
    )


def render_impact_report(
    impact: ReplanImpact,
    consistency_mode: str,
    consistency_summary: str,
) -> str:
    lines = [
        f"# Replan impact — revision {impact.candidate_revision}",
        "",
        f"- Applicable: **{'yes' if impact.applicable else 'no'}**",
        f"- Consistency: `{consistency_mode}` — {consistency_summary}",
        f"- Added packages: {', '.join(impact.added_packages) or 'none'}",
        f"- Removed packages: {', '.join(impact.removed_packages) or 'none'}",
        "",
        "## Package migration",
        "",
        "| Package | Classification | Replacement | Changed fields |",
        "|---|---|---|---|",
    ]
    for item in impact.packages:
        lines.append(
            f"| `{item.package_id}` | `{item.classification}` | "
            f"`{item.replacement_id or '—'}` | {', '.join(item.changed_fields) or '—'} |"
        )
    if impact.blockers:
        lines.extend(["", "## Blockers", ""])
        lines.extend(f"- {item}" for item in impact.blockers)
    if impact.warnings:
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {item}" for item in impact.warnings)
    lines.append("")
    return "\n".join(lines)



__all__ = [
    "analyze_impact",
    "impact_from_mapping",
    "reconcile_state",
    "render_impact_report",
]
