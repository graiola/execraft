"""Pure definition transformation for inserting repository-sync Work Packages.

The helper does not touch orchestration state. It produces a normal definition
``TaskDefinitionInput`` so the existing versioned replanning transaction owns
history, impact analysis, approval, and publication.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

import yaml

from execraft.onboarding.task_definition import TaskDefinitionInput
from execraft.plan_contract import canonicalize_legacy_plan_graph_mapping
from execraft.workspace.task_git import TaskManifest

from .selection import RepositorySyncSelectionError, validate_sync_repository_selection as _validate_sync_repository_selection
from .spec import RepositorySyncSpec, RepositorySyncSpecError


class RepositorySyncPlanningError(ValueError):
    """Raised when a sync-before graph transformation is invalid."""


def _load_declarative_graph(plan_graph_yaml: str) -> tuple[dict[str, object], tuple[str, ...]]:
    """Parse and compatibility-project a historical hybrid plan graph.

    Repository synchronization is often first used on long-lived tasks that predate the current definition format
    and therefore still carry runtime fields inside PLAN.graph.yaml. Strip only
    the runtime-owned legacy fields before creating the candidate. Unknown
    fields are intentionally preserved so the normal definition validator can still
    reject unsupported semantics.
    """

    try:
        loaded = yaml.safe_load(plan_graph_yaml) or {}
    except yaml.YAMLError as exc:
        raise RepositorySyncPlanningError(f"PLAN.graph.yaml is invalid YAML: {exc}") from exc
    if not isinstance(loaded, Mapping):
        raise RepositorySyncPlanningError("PLAN.graph.yaml must contain a mapping")
    projection = canonicalize_legacy_plan_graph_mapping(loaded)
    return projection.graph, projection.legacy_completed_package_ids


def validate_sync_repository_selection(
    manifest: TaskManifest, repositories: Sequence[str]
) -> tuple[str, ...]:
    """Backward-compatible planning-level selection validator."""
    try:
        return _validate_sync_repository_selection(manifest, repositories)
    except RepositorySyncSelectionError as exc:
        raise RepositorySyncPlanningError(str(exc)) from exc


@dataclass(frozen=True)
class RepositorySyncInsertion:
    definition: TaskDefinitionInput
    sync_package_id: str
    target_package_id: str
    repositories: tuple[str, ...]


def _next_sync_package_id(existing_ids: set[str], target_id: str, explicit: str) -> str:
    requested = str(explicit).strip()
    if requested:
        if requested in existing_ids:
            raise RepositorySyncPlanningError(f"sync package ID already exists: {requested}")
        if requested == target_id:
            raise RepositorySyncPlanningError("sync package ID must differ from the target package ID")
        return requested
    base = f"{target_id}-SYNC"
    if base not in existing_ids:
        return base
    index = 2
    while f"{base}-{index}" in existing_ids:
        index += 1
    return f"{base}-{index}"


def _sync_package_mapping(
    *,
    package_id: str,
    target_id: str,
    dependencies: Sequence[str],
    selected: Sequence[str],
    branches: Mapping[str, str],
    remote: str,
    conflict_policy: str,
    verification_profile: str,
) -> tuple[dict[str, object], RepositorySyncSpec]:
    spec_mapping = {
        "strategy": "merge",
        "remote": str(remote).strip() or "origin",
        "conflict_policy": str(conflict_policy).strip() or "ai_resolve",
        "require_independent_review": True,
        "repositories": {
            repository_id: (
                {"source_branch": branches[repository_id]}
                if branches.get(repository_id)
                else {}
            )
            for repository_id in selected
        },
    }
    try:
        sync_spec = RepositorySyncSpec.from_mapping(spec_mapping)
    except RepositorySyncSpecError as exc:
        raise RepositorySyncPlanningError(str(exc)) from exc
    return (
        {
            "id": package_id,
            "title": f"Synchronize upstream repositories around {target_id}",
            "kind": "repository_sync",
            "dependencies": [str(item) for item in dependencies],
            "requirements": [
                "Fetch and pin the selected authoritative upstream branches before mutating task worktrees.",
                "Merge the pinned upstream commits into the task branches without rebasing or rewriting task history.",
                "Resolve merge and compatibility conflicts within the declared repository scope.",
                "Preserve durable synchronization evidence and leave no unresolved Git operation.",
            ],
            "acceptance_criteria": [
                {
                    "id": "upstream_integrated",
                    "description": "Every selected pinned upstream commit is an ancestor of the resulting task branch.",
                },
                {
                    "id": "conflicts_resolved",
                    "description": "No unresolved merge conflict or unrelated Git operation remains.",
                },
                {
                    "id": "verification_passed",
                    "description": "Configured post-synchronization verification and independent review pass.",
                },
            ],
            "affected_repositories": list(selected),
            "repository_sync": sync_spec.as_mapping(),
            "risk": "high",
            "verification_profile": str(verification_profile).strip() or "integration",
            "parallel_safe": False,
        },
        sync_spec,
    )


def build_sync_before_definition(
    *,
    brief_markdown: str,
    plan_markdown: str,
    plan_graph_yaml: str,
    before_package_id: str,
    repositories: Sequence[str],
    source_branches: Mapping[str, str] | None = None,
    remote: str = "origin",
    conflict_policy: str = "ai_resolve",
    sync_package_id: str = "",
    verification_profile: str = "integration",
) -> RepositorySyncInsertion:
    """Insert one synchronization node immediately before *before_package_id*.

    The original target dependencies are transferred to the sync node and the
    target is rewired to depend solely on the sync node.  Downstream packages do
    not need modification because the target package identity stays stable.
    """

    target_id = str(before_package_id).strip()
    if not target_id:
        raise RepositorySyncPlanningError("--before package ID cannot be empty")
    selected = tuple(dict.fromkeys(str(item).strip() for item in repositories if str(item).strip()))
    if not selected:
        raise RepositorySyncPlanningError("at least one repository must be selected for synchronization")
    branches = {str(k).strip(): str(v).strip() for k, v in (source_branches or {}).items()}
    unknown_overrides = sorted(set(branches) - set(selected))
    if unknown_overrides:
        raise RepositorySyncPlanningError(
            "source-branch overrides reference repositories outside the selected set: "
            + ", ".join(unknown_overrides)
        )
    requested_package_id = str(sync_package_id).strip()

    raw, _legacy_completed = _load_declarative_graph(plan_graph_yaml)
    packages = raw.get("work_packages")
    if not isinstance(packages, list):
        raise RepositorySyncPlanningError("PLAN.graph.yaml work_packages must be a list")

    target_index = -1
    target: dict[str, object] | None = None
    existing_ids: set[str] = set()
    for index, candidate in enumerate(packages):
        if not isinstance(candidate, dict):
            raise RepositorySyncPlanningError("every work package must be a mapping")
        candidate_id = str(candidate.get("id", "")).strip()
        if candidate_id:
            existing_ids.add(candidate_id)
        if candidate_id == target_id:
            target_index = index
            target = candidate
    if target is None:
        raise RepositorySyncPlanningError(f"target work package does not exist: {target_id}")
    package_id = _next_sync_package_id(existing_ids, target_id, requested_package_id)

    target_repositories = {
        str(item).strip() for item in (target.get("affected_repositories") or []) if str(item).strip()
    }
    # Synchronizing repositories outside the target is valid, but require an
    # explicit selection (already provided) and retain the fact in PLAN text.
    original_dependencies = [
        str(item).strip() for item in (target.get("dependencies") or []) if str(item).strip()
    ]
    sync_package, sync_spec = _sync_package_mapping(
        package_id=package_id,
        target_id=target_id,
        dependencies=original_dependencies,
        selected=selected,
        branches=branches,
        remote=remote,
        conflict_policy=conflict_policy,
        verification_profile=verification_profile,
    )
    target["dependencies"] = [package_id]
    packages.insert(target_index, sync_package)

    source_lines = []
    for repository_id in selected:
        source = branches.get(repository_id) or "TASK.yaml base_branch"
        source_lines.append(f"- `{repository_id}`: `{sync_spec.target(repository_id).remote}/{source}`")
    note = "\n".join(
        [
            "",
            f"## Work Package: {package_id} — Repository synchronization before {target_id}",
            "",
            "This orchestration-owned Work Package periodically converges long-lived Task branches with authoritative upstream history.",
            "It uses merge commits, never automatic rebase/history rewriting, and invokes AI only when deterministic merge/verification requires semantic repair.",
            "",
            "Repositories:",
            *source_lines,
            "",
            f"The Work Package inherits the previous dependencies of `{target_id}`; `{target_id}` now depends on `{package_id}`.",
            (
                "The synchronized set includes repositories not directly declared by the target Work Package: "
                + ", ".join(sorted(set(selected) - target_repositories))
                if set(selected) - target_repositories
                else "The synchronized set is contained in the target Work Package repository scope."
            ),
            "",
        ]
    )
    updated_plan = plan_markdown.rstrip() + "\n" + note
    updated_graph = yaml.safe_dump(raw, sort_keys=False, width=1000)
    definition = TaskDefinitionInput.from_contents(
        brief_markdown=brief_markdown,
        plan_markdown=updated_plan,
        plan_graph_yaml=updated_graph,
        brief_source="repository_sync:preserved",
        plan_source="repository_sync:sync-before",
        plan_graph_source="repository_sync:sync-before",
    )
    return RepositorySyncInsertion(
        definition=definition,
        sync_package_id=package_id,
        target_package_id=target_id,
        repositories=selected,
    )


def build_sync_after_definition(
    *,
    brief_markdown: str,
    plan_markdown: str,
    plan_graph_yaml: str,
    after_package_id: str,
    repositories: Sequence[str],
    source_branches: Mapping[str, str] | None = None,
    remote: str = "origin",
    conflict_policy: str = "ai_resolve",
    sync_package_id: str = "",
    verification_profile: str = "integration",
    completed_package_ids: Sequence[str] | None = None,
) -> RepositorySyncInsertion:
    """Insert a synchronization barrier immediately after a started package.

    Direct incomplete dependents are rewired through the new sync package.
    Completed package contracts remain untouched, which keeps definition history
    immutability intact. Independent graph branches remain independent, while
    repository-sync packages are scheduler-prioritized once ready.
    """

    target_id = str(after_package_id).strip()
    if not target_id:
        raise RepositorySyncPlanningError("after package ID cannot be empty")
    selected = tuple(dict.fromkeys(str(item).strip() for item in repositories if str(item).strip()))
    if not selected:
        raise RepositorySyncPlanningError("at least one repository must be selected for synchronization")
    branches = {str(k).strip(): str(v).strip() for k, v in (source_branches or {}).items()}
    unknown_overrides = sorted(set(branches) - set(selected))
    if unknown_overrides:
        raise RepositorySyncPlanningError(
            "source-branch overrides reference repositories outside the selected set: "
            + ", ".join(unknown_overrides)
        )
    raw, legacy_completed = _load_declarative_graph(plan_graph_yaml)
    packages = raw.get("work_packages")
    if not isinstance(packages, list):
        raise RepositorySyncPlanningError("PLAN.graph.yaml work_packages must be a list")
    completed = (
        {str(item).strip() for item in completed_package_ids if str(item).strip()}
        if completed_package_ids is not None
        else set(legacy_completed)
    )

    target_index = -1
    target: dict[str, object] | None = None
    existing_ids: set[str] = set()
    for index, candidate in enumerate(packages):
        if not isinstance(candidate, dict):
            raise RepositorySyncPlanningError("every work package must be a mapping")
        candidate_id = str(candidate.get("id", "")).strip()
        if candidate_id:
            existing_ids.add(candidate_id)
        if candidate_id == target_id:
            target_index = index
            target = candidate
    if target is None:
        raise RepositorySyncPlanningError(f"target work package does not exist: {target_id}")
    package_id = _next_sync_package_id(existing_ids, target_id, sync_package_id)
    sync_package, sync_spec = _sync_package_mapping(
        package_id=package_id,
        target_id=target_id,
        dependencies=[target_id],
        selected=selected,
        branches=branches,
        remote=remote,
        conflict_policy=conflict_policy,
        verification_profile=verification_profile,
    )

    rewired: list[str] = []
    for candidate in packages:
        if not isinstance(candidate, dict) or candidate is target:
            continue
        candidate_id = str(candidate.get("id", "")).strip()
        dependencies = [str(item).strip() for item in (candidate.get("dependencies") or []) if str(item).strip()]
        if target_id not in dependencies:
            continue
        # A completed dependent is historical evidence and cannot be rewritten.
        # Completion is runtime state, not a declarative PLAN.graph field.
        if candidate_id in completed:
            continue
        candidate["dependencies"] = [package_id if item == target_id else item for item in dependencies]
        rewired.append(candidate_id)
    packages.insert(target_index + 1, sync_package)

    source_lines = []
    for repository_id in selected:
        source = branches.get(repository_id) or "TASK.yaml base_branch"
        source_lines.append(f"- `{repository_id}`: `{sync_spec.target(repository_id).remote}/{source}`")
    note = "\n".join(
        [
            "",
            f"## Work Package: {package_id} — Repository synchronization after {target_id}",
            "",
            "This orchestration-owned Work Package creates a safe convergence point after a started Work Package completes.",
            "It uses merge commits, never automatic rebase/history rewriting, and invokes AI only for semantic conflict repair.",
            "",
            "Repositories:",
            *source_lines,
            "",
            f"`{package_id}` depends on `{target_id}`.",
            (
                "Rewired direct dependents through the synchronization barrier: "
                + ", ".join(f"`{item}`" for item in rewired)
                if rewired
                else "No direct dependent required rewiring; the synchronization Work Package remains an explicit trailing barrier."
            ),
            "",
        ]
    )
    updated_plan = plan_markdown.rstrip() + "\n" + note
    updated_graph = yaml.safe_dump(raw, sort_keys=False, width=1000)
    definition = TaskDefinitionInput.from_contents(
        brief_markdown=brief_markdown,
        plan_markdown=updated_plan,
        plan_graph_yaml=updated_graph,
        brief_source="repository_sync:preserved",
        plan_source="repository_sync:sync-after",
        plan_graph_source="repository_sync:sync-after",
    )
    return RepositorySyncInsertion(
        definition=definition,
        sync_package_id=package_id,
        target_package_id=target_id,
        repositories=selected,
    )


def build_final_sync_definition(
    *,
    brief_markdown: str,
    plan_markdown: str,
    plan_graph_yaml: str,
    repositories: Sequence[str],
    source_branches: Mapping[str, str] | None = None,
    remote: str = "origin",
    conflict_policy: str = "ai_resolve",
    sync_package_id: str = "",
    verification_profile: str = "integration",
) -> RepositorySyncInsertion:
    """Append a synchronization barrier after every terminal graph branch.

    This is the completion-time counterpart to ``build_sync_before_definition``.
    Depending on every terminal node makes the new Work Package a true final Check
    even when the plan completed through multiple independent branches.
    """

    selected = tuple(
        dict.fromkeys(str(item).strip() for item in repositories if str(item).strip())
    )
    if not selected:
        raise RepositorySyncPlanningError(
            "at least one repository must be selected for synchronization"
        )
    branches = {
        str(key).strip(): str(value).strip()
        for key, value in (source_branches or {}).items()
    }
    unknown_overrides = sorted(set(branches) - set(selected))
    if unknown_overrides:
        raise RepositorySyncPlanningError(
            "source-branch overrides reference repositories outside the selected set: "
            + ", ".join(unknown_overrides)
        )
    raw, _legacy_completed = _load_declarative_graph(plan_graph_yaml)
    packages = raw.get("work_packages")
    if not isinstance(packages, list) or not packages:
        raise RepositorySyncPlanningError(
            "final synchronization requires at least one existing work package"
        )
    existing_ids: set[str] = set()
    dependency_ids: set[str] = set()
    for candidate in packages:
        if not isinstance(candidate, dict):
            raise RepositorySyncPlanningError("every work package must be a mapping")
        candidate_id = str(candidate.get("id", "")).strip()
        if not candidate_id:
            raise RepositorySyncPlanningError("every work package must have an ID")
        existing_ids.add(candidate_id)
        dependency_ids.update(
            str(item).strip()
            for item in (candidate.get("dependencies") or [])
            if str(item).strip()
        )
    terminal_ids = tuple(sorted(existing_ids - dependency_ids))
    if not terminal_ids:
        raise RepositorySyncPlanningError(
            "cannot identify terminal work packages for final synchronization"
        )
    target_id = terminal_ids[-1]
    package_id = _next_sync_package_id(existing_ids, "FINAL", sync_package_id)
    sync_package, sync_spec = _sync_package_mapping(
        package_id=package_id,
        target_id="final completion",
        dependencies=terminal_ids,
        selected=selected,
        branches=branches,
        remote=remote,
        conflict_policy=conflict_policy,
        verification_profile=verification_profile,
    )
    packages.append(sync_package)
    source_lines = []
    for repository_id in selected:
        source = branches.get(repository_id) or "TASK.yaml base_branch"
        source_lines.append(
            f"- `{repository_id}`: `{sync_spec.target(repository_id).remote}/{source}`"
        )
    dependencies = ", ".join(f"`{item}`" for item in terminal_ids)
    note = "\n".join(
        [
            "",
            f"## Work Package: {package_id} — Final repository synchronization",
            "",
            "This operator-approved final synchronization Check converges Task branches with authoritative upstream history before archival and cleanup.",
            "It uses merge commits, never automatic rebase/history rewriting, and requires post-synchronization verification and independent review.",
            "",
            "Repositories:",
            *source_lines,
            "",
            f"`{package_id}` depends on every terminal package: {dependencies}.",
            "",
        ]
    )
    definition = TaskDefinitionInput.from_contents(
        brief_markdown=brief_markdown,
        plan_markdown=plan_markdown.rstrip() + "\n" + note,
        plan_graph_yaml=yaml.safe_dump(raw, sort_keys=False, width=1000),
        brief_source="repository_sync:preserved",
        plan_source="repository_sync:final-sync",
        plan_graph_source="repository_sync:final-sync",
    )
    return RepositorySyncInsertion(
        definition=definition,
        sync_package_id=package_id,
        target_package_id=target_id,
        repositories=selected,
    )


__all__ = [
    "RepositorySyncInsertion",
    "RepositorySyncPlanningError",
    "build_final_sync_definition",
    "build_sync_after_definition",
    "build_sync_before_definition",
    "validate_sync_repository_selection",
]
