"""Automatic work-package decomposition and safe parallel-wave planning.

The planner is model-driven, but the resulting graph is not trusted. This
module validates requirement/evidence coverage, repository boundaries,
dependency references, and concurrency conflicts before generated shards are
inserted into durable orchestration state.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Iterable, Mapping, Sequence

from .models import AcceptanceCriterion, OrchestrateError, WorkPackage, WorkPackageStage
from .scheduler import AgentCapability, StructuredHandoff


_SHARD_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}$")
_WRITE_CAPABILITIES = frozenset({AgentCapability.IMPLEMENT, AgentCapability.FIX_REVIEW})


class ShardPlanError(OrchestrateError):
    """Raised when a model-created decomposition plan is unsafe or incomplete."""


@dataclass(frozen=True)
class DecompositionPolicy:
    enabled: bool = True
    complexity_threshold: int = 60
    target_shard_complexity: int = 40
    maximum_shard_complexity: int = 55
    maximum_shards: int = 8
    trigger_when_no_eligible_provider: bool = True


@dataclass(frozen=True)
class ParallelShardPolicy:
    enabled: bool = True
    max_workers: int = 2
    stages: frozenset[AgentCapability] = frozenset(
        {
            AgentCapability.IMPLEMENT,
            AgentCapability.REVIEW,
            AgentCapability.FIX_REVIEW,
        }
    )
    require_disjoint_repositories_for_writes: bool = True


@dataclass(frozen=True)
class ParallelShardCandidate:
    package: WorkPackage
    capability: AgentCapability
    agent_id: str
    handoff: StructuredHandoff
    read_only: bool
    repository_scope: frozenset[str]
    conflict_keys: frozenset[str]
    concurrency_group: str
    source_repository_id: str = ""
    source_repository_path: str = ""
    isolation_path: str = ""
    source_fingerprint: str = ""
    baseline_commit: str = ""
    invocation_id: str = ""
    parent_invocation_id: str = ""
    handoff_sha256: str = ""
    workspace_before_digest: str = ""


@dataclass(frozen=True)
class ValidatedDecomposition:
    decision: str
    reason: str
    shards: tuple[WorkPackage, ...] = ()
    plan_hash: str = ""


def decomposition_output_schema(maximum_shards: int) -> dict[str, Any]:
    """Return the provider-neutral schema for a decomposition proposal."""

    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["ok", "decision", "reason", "shards"],
        "properties": {
            "ok": {"const": True},
            "decision": {"enum": ["shard", "keep_atomic"]},
            "reason": {"type": "string", "minLength": 1},
            "shards": {
                "type": "array",
                "maxItems": int(maximum_shards),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "id",
                        "title",
                        "complexity",
                        "affected_repositories",
                        "requirement_indexes",
                        "acceptance_criterion_ids",
                        "finding_indexes",
                        "depends_on",
                        "read_scope",
                        "write_scope",
                        "conflict_keys",
                        "parallel_safe",
                    ],
                    "properties": {
                        "id": {"type": "string", "minLength": 1},
                        "title": {"type": "string", "minLength": 1},
                        "complexity": {"type": "integer"},
                        "affected_repositories": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1},
                        },
                        "requirement_indexes": {
                            "type": "array",
                            "items": {"type": "integer"},
                        },
                        "acceptance_criterion_ids": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1},
                        },
                        "finding_indexes": {
                            "type": "array",
                            "items": {"type": "integer"},
                        },
                        "depends_on": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1},
                        },
                        "read_scope": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1},
                        },
                        "write_scope": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1},
                        },
                        "conflict_keys": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1},
                        },
                        "parallel_safe": {"type": "boolean"},
                    },
                },
            },
        },
    }


def build_decomposition_handoff(
    package: WorkPackage,
    *,
    policy: DecompositionPolicy,
    working_directory: str = "",
) -> StructuredHandoff:
    origin_stage = package.decomposition_origin_stage or package.stage.value
    requirement_lines = [
        f"{index}: {value}" for index, value in enumerate(package.requirements, start=1)
    ]
    criterion_lines = [
        f"{criterion.id}: {criterion.description}"
        for criterion in package.acceptance_criteria
    ]
    finding_lines = [
        f"{index}: {value}"
        for index, value in enumerate(package.review_findings, start=1)
    ]
    relevant = [
        f"Decompose the {origin_stage} stage, not the entire project.",
        f"Create 2..{policy.maximum_shards} bounded shards only when useful.",
        f"Target complexity is {policy.target_shard_complexity}; no shard may exceed "
        f"{policy.maximum_shard_complexity}.",
        "Every parent requirement and acceptance criterion must be assigned to at least one shard.",
        "Use only repositories declared by the parent package.",
        "Mark parallel_safe=true only for genuinely independent work.",
        "Write-capable shards should normally target one repository; use depends_on for ordering.",
    ]
    if finding_lines:
        relevant.append("Every unresolved finding must be assigned to at least one shard.")
    return StructuredHandoff(
        work_package_id=package.id,
        stage="decompose",
        summary=(
            f"Decide whether to keep this package atomic or produce a safe shard plan: "
            f"{package.title}"
        ),
        relevant_decisions=relevant,
        bounded_excerpts={
            "numbered_requirements": "\n".join(requirement_lines),
            "acceptance_criteria": "\n".join(criterion_lines),
            "numbered_findings": "\n".join(finding_lines),
        },
        requirements=list(package.requirements),
        acceptance_criteria=[
            {"id": item.id, "description": item.description}
            for item in package.acceptance_criteria
        ],
        expected_output_schema=decomposition_output_schema(policy.maximum_shards),
        working_directory=working_directory,
        read_only=True,
    )


def validate_decomposition_payload(
    parent: WorkPackage,
    payload: Mapping[str, Any],
    *,
    policy: DecompositionPolicy,
    generated_by: str,
) -> ValidatedDecomposition:
    decision = str(payload.get("decision", "")).strip().lower()
    reason = str(payload.get("reason", "")).strip()
    if decision not in {"shard", "keep_atomic"}:
        raise ShardPlanError("decomposition decision must be shard or keep_atomic")
    if not reason:
        raise ShardPlanError("decomposition reason is required")
    if decision == "keep_atomic":
        return ValidatedDecomposition(decision=decision, reason=reason)

    raw_shards = payload.get("shards")
    if not isinstance(raw_shards, list):
        raise ShardPlanError("decomposition shards must be a list")
    if not 2 <= len(raw_shards) <= policy.maximum_shards:
        raise ShardPlanError(
            f"decomposition must contain 2..{policy.maximum_shards} shards"
        )

    parent_repositories = set(parent.affected_repositories)
    criterion_by_id = {item.id: item for item in parent.acceptance_criteria}
    requirement_coverage: set[int] = set()
    criterion_coverage: set[str] = set()
    finding_coverage: set[int] = set()
    local_ids: set[str] = set()
    normalized: list[dict[str, Any]] = []

    for index, raw in enumerate(raw_shards, start=1):
        if not isinstance(raw, Mapping):
            raise ShardPlanError(f"shard {index} must be a mapping")
        local_id = str(raw.get("id", "")).strip()
        if not _SHARD_ID.fullmatch(local_id):
            raise ShardPlanError(
                f"shard {index} id must match {_SHARD_ID.pattern!r}"
            )
        if local_id in local_ids:
            raise ShardPlanError(f"duplicate shard id: {local_id}")
        local_ids.add(local_id)

        title = str(raw.get("title", "")).strip()
        if not title:
            raise ShardPlanError(f"shard {local_id} requires a title")
        try:
            complexity = int(raw.get("complexity", 0))
        except (TypeError, ValueError) as exc:
            raise ShardPlanError(f"shard {local_id} complexity must be an integer") from exc
        if not 1 <= complexity <= policy.maximum_shard_complexity:
            raise ShardPlanError(
                f"shard {local_id} complexity {complexity} exceeds allowed "
                f"1..{policy.maximum_shard_complexity}"
            )

        repositories = _string_list(raw.get("affected_repositories"))
        if not repositories:
            raise ShardPlanError(f"shard {local_id} requires affected_repositories")
        unknown_repositories = set(repositories) - parent_repositories
        if unknown_repositories:
            raise ShardPlanError(
                f"shard {local_id} references repositories outside the parent: "
                + ", ".join(sorted(unknown_repositories))
            )

        requirement_indexes = _integer_set(raw.get("requirement_indexes"), label="requirement_indexes")
        invalid_requirements = sorted(
            value for value in requirement_indexes if value < 1 or value > len(parent.requirements)
        )
        if invalid_requirements:
            raise ShardPlanError(
                f"shard {local_id} has invalid requirement indexes: {invalid_requirements}"
            )
        criterion_ids = set(_string_list(raw.get("acceptance_criterion_ids")))
        invalid_criteria = sorted(criterion_ids - set(criterion_by_id))
        if invalid_criteria:
            raise ShardPlanError(
                f"shard {local_id} has unknown acceptance criteria: {invalid_criteria}"
            )
        finding_indexes = _integer_set(raw.get("finding_indexes"), label="finding_indexes")
        invalid_findings = sorted(
            value for value in finding_indexes if value < 1 or value > len(parent.review_findings)
        )
        if invalid_findings:
            raise ShardPlanError(
                f"shard {local_id} has invalid finding indexes: {invalid_findings}"
            )

        requirement_coverage.update(requirement_indexes)
        criterion_coverage.update(criterion_ids)
        finding_coverage.update(finding_indexes)
        normalized.append(
            {
                "id": local_id,
                "title": title,
                "complexity": complexity,
                "repositories": repositories,
                "requirement_indexes": requirement_indexes,
                "criterion_ids": criterion_ids,
                "finding_indexes": finding_indexes,
                "depends_on": _string_list(raw.get("depends_on")),
                "read_scope": _scope_list(raw.get("read_scope"), label="read_scope"),
                "write_scope": _scope_list(raw.get("write_scope"), label="write_scope"),
                "conflict_keys": _string_list(raw.get("conflict_keys")),
                "parallel_safe": bool(raw.get("parallel_safe", False)),
            }
        )

    missing_requirements = sorted(set(range(1, len(parent.requirements) + 1)) - requirement_coverage)
    missing_criteria = sorted(set(criterion_by_id) - criterion_coverage)
    missing_findings = sorted(
        set(range(1, len(parent.review_findings) + 1)) - finding_coverage
    )
    if missing_requirements:
        raise ShardPlanError(
            f"decomposition does not cover parent requirement indexes: {missing_requirements}"
        )
    if missing_criteria:
        raise ShardPlanError(
            f"decomposition does not cover parent acceptance criteria: {missing_criteria}"
        )
    if parent.review_findings and missing_findings:
        raise ShardPlanError(
            f"decomposition does not cover unresolved finding indexes: {missing_findings}"
        )

    for item in normalized:
        unknown_dependencies = set(item["depends_on"]) - local_ids
        if unknown_dependencies:
            raise ShardPlanError(
                f"shard {item['id']} depends on unknown shards: "
                + ", ".join(sorted(unknown_dependencies))
            )
        if item["id"] in item["depends_on"]:
            raise ShardPlanError(f"shard {item['id']} cannot depend on itself")
    _validate_local_acyclic(normalized)

    origin_stage = parent.decomposition_origin_stage or parent.stage.value
    children: list[WorkPackage] = []
    parent_dependencies = list(parent.dependencies)
    for order, item in enumerate(normalized):
        full_id = f"{parent.id}__{item['id']}"
        dependencies = list(parent_dependencies)
        dependencies.extend(f"{parent.id}__{dep}" for dep in item["depends_on"])
        requirements = [
            parent.requirements[index - 1]
            for index in sorted(item["requirement_indexes"])
        ]
        criteria = [
            AcceptanceCriterion(
                id=criterion_id,
                description=criterion_by_id[criterion_id].description,
            )
            for criterion_id in sorted(item["criterion_ids"])
        ]
        findings = [
            parent.review_findings[index - 1]
            for index in sorted(item["finding_indexes"])
        ]
        stage, mode, read_only = _child_execution(origin_stage)
        write_scope = list(item["write_scope"])
        if not read_only and not write_scope:
            write_scope = [f"{repository}/**" for repository in item["repositories"]]
        parallel_safe = bool(item["parallel_safe"])
        if not read_only and len(item["repositories"]) != 1:
            parallel_safe = False
        child = WorkPackage(
            id=full_id,
            title=item["title"],
            dependencies=list(dict.fromkeys(dependencies)),
            requirements=requirements,
            acceptance_criteria=criteria,
            affected_repositories=list(item["repositories"]),
            stage=stage,
            status="pending",
            risk=_risk_for_complexity(item["complexity"]),
            priority=parent.priority + max(0, len(normalized) - order),
            complexity=item["complexity"],
            verification_profile=parent.verification_profile,
            agent_preferences={
                role: list(values)
                for role, values in (parent.agent_preferences or {}).items()
            },
            agent_preference_binding_roles=list(
                parent.agent_preference_binding_roles
            ),
            skill_preferences={
                role: list(values)
                for role, values in (parent.skill_preferences or {}).items()
            },
            review_findings=findings,
            execution_mode=mode,
            parent_id=parent.id,
            shard_key=item["id"],
            generated_by=generated_by,
            parallel_safe=parallel_safe,
            read_scope=list(item["read_scope"]),
            write_scope=write_scope,
            conflict_keys=list(item["conflict_keys"]),
        )
        children.append(child)

    canonical = {
        "parent_id": parent.id,
        "origin_stage": origin_stage,
        "reason": reason,
        "shards": [child.as_mapping() for child in children],
    }
    plan_hash = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return ValidatedDecomposition(
        decision=decision,
        reason=reason,
        shards=tuple(children),
        plan_hash=plan_hash,
    )


def capability_for_parallel_stage(package: WorkPackage) -> AgentCapability | None:
    if package.stage == WorkPackageStage.IMPLEMENT:
        return AgentCapability.IMPLEMENT
    if package.stage == WorkPackageStage.REVIEW:
        return AgentCapability.REVIEW
    if package.stage == WorkPackageStage.FIX_REVIEW:
        return AgentCapability.FIX_REVIEW
    return None


def choose_parallel_candidates(
    candidates: Sequence[ParallelShardCandidate],
    *,
    policy: ParallelShardPolicy,
) -> list[ParallelShardCandidate]:
    """Choose a deterministic, conflict-free wave with distinct providers."""

    selected: list[ParallelShardCandidate] = []
    used_agents: set[str] = set()
    used_groups: set[str] = set()
    for candidate in candidates:
        if len(selected) >= policy.max_workers:
            break
        if candidate.agent_id in used_agents:
            continue
        if candidate.concurrency_group in used_groups:
            continue
        if candidate.capability not in policy.stages:
            continue
        if any(_parallel_conflict(candidate, current, policy=policy) for current in selected):
            continue
        selected.append(candidate)
        used_agents.add(candidate.agent_id)
        used_groups.add(candidate.concurrency_group)
    return selected


def _parallel_conflict(
    left: ParallelShardCandidate,
    right: ParallelShardCandidate,
    *,
    policy: ParallelShardPolicy,
) -> bool:
    if left.conflict_keys & right.conflict_keys:
        return True
    repositories_overlap = bool(left.repository_scope & right.repository_scope)
    if not repositories_overlap:
        return False
    if left.read_only and right.read_only:
        return False
    if policy.require_disjoint_repositories_for_writes:
        return True
    return False


def _child_execution(origin_stage: str) -> tuple[WorkPackageStage, str, bool]:
    if origin_stage in {WorkPackageStage.REVIEW.value, WorkPackageStage.FINAL_REVIEW.value}:
        return WorkPackageStage.REVIEW, "review_shard", True
    if origin_stage == WorkPackageStage.FIX_REVIEW.value:
        return WorkPackageStage.FIX_REVIEW, "fix_shard", False
    return WorkPackageStage.PREPARE, "standard_shard", False


def _risk_for_complexity(value: int) -> str:
    if value >= 80:
        return "critical"
    if value >= 60:
        return "high"
    if value >= 35:
        return "medium"
    return "low"


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ShardPlanError("expected a list")
    return [str(item).strip() for item in value if str(item).strip()]


def _scope_list(value: Any, *, label: str) -> list[str]:
    scopes = _string_list(value)
    for scope in scopes:
        normalized = scope.replace("\\", "/")
        parts = [part for part in normalized.split("/") if part]
        if normalized.startswith("/") or ".." in parts:
            raise ShardPlanError(
                f"{label} entries must be relative and cannot traverse parents: {scope!r}"
            )
    return scopes


def _integer_set(value: Any, *, label: str) -> set[int]:
    if value is None:
        return set()
    if not isinstance(value, list):
        raise ShardPlanError(f"{label} must be a list")
    result: set[int] = set()
    for item in value:
        if isinstance(item, bool):
            raise ShardPlanError(f"{label} must contain integers")
        try:
            result.add(int(item))
        except (TypeError, ValueError) as exc:
            raise ShardPlanError(f"{label} must contain integers") from exc
    return result


def _validate_local_acyclic(shards: Iterable[Mapping[str, Any]]) -> None:
    dependencies = {
        str(item["id"]): set(str(dep) for dep in item["depends_on"])
        for item in shards
    }
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(shard_id: str) -> None:
        if shard_id in visiting:
            raise ShardPlanError(f"cycle detected in shard dependencies at {shard_id}")
        if shard_id in visited:
            return
        visiting.add(shard_id)
        for dependency in dependencies[shard_id]:
            visit(dependency)
        visiting.remove(shard_id)
        visited.add(shard_id)

    for shard_id in dependencies:
        visit(shard_id)
