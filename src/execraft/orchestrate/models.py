"""Domain models for the task execution state machine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from execraft.repository_sync.spec import RepositorySyncSpec
from execraft.orchestrate.execution_policy import normalize_binding_roles


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _normalized_role_preferences(
    value: object, *, label: str
) -> dict[str, list[str]]:
    """Validate persisted package policy without silently dropping bad data."""

    from .execution_policy import ExecutionPolicyError, normalize_role_mapping

    if value is None:
        return {}
    try:
        return normalize_role_mapping(value, label=label)  # type: ignore[arg-type]
    except ExecutionPolicyError as exc:
        raise OrchestrateError(str(exc)) from exc


class TaskExecutionState(str, Enum):
    INITIALIZING = "initializing"
    VALIDATING_PLAN = "validating_plan"
    RUNNING = "running"
    WAITING_FOR_AGENT = "waiting_for_agent"
    WAITING_FOR_ENVIRONMENT = "waiting_for_environment"
    RESOURCE_MAINTENANCE = "resource_maintenance"
    PAUSED_LOW_DISK = "paused_low_disk"
    OPERATOR_PAUSED = "operator_paused"
    RECOVERING = "recovering"
    FINAL_VALIDATION = "final_validation"
    HUMAN_REQUIRED = "human_required"
    SUPERVISING = "supervising"
    WAITING_FOR_HUMAN_DECISION = "waiting_for_human_decision"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkPackageKind(str, Enum):
    DEVELOPMENT = "development"
    REPOSITORY_SYNC = "repository_sync"


class WorkPackageStage(str, Enum):
    PREPARE = "prepare"
    DECOMPOSE = "decompose"
    IMPLEMENT = "implement"
    FAST_VERIFY = "fast_verify"
    TARGETED_VERIFY = "targeted_verify"
    REVIEW = "review"
    FIX_REVIEW = "fix_review"
    REGRESSION_VERIFY = "regression_verify"
    FINAL_REVIEW = "final_review"
    FULL_VERIFY = "full_verify"
    READY_TO_COMMIT = "ready_to_commit"
    COMPLETED = "completed"


_VALID_TASK_EXECUTION_TRANSITIONS: dict[TaskExecutionState, set[TaskExecutionState]] = {
    TaskExecutionState.INITIALIZING: {
        TaskExecutionState.VALIDATING_PLAN, TaskExecutionState.FAILED, TaskExecutionState.CANCELLED,
    },
    TaskExecutionState.VALIDATING_PLAN: {
        TaskExecutionState.RUNNING, TaskExecutionState.HUMAN_REQUIRED,
        TaskExecutionState.FAILED, TaskExecutionState.CANCELLED,
    },
    TaskExecutionState.RUNNING: {
        TaskExecutionState.WAITING_FOR_AGENT, TaskExecutionState.WAITING_FOR_ENVIRONMENT,
        TaskExecutionState.RESOURCE_MAINTENANCE, TaskExecutionState.PAUSED_LOW_DISK,
        TaskExecutionState.OPERATOR_PAUSED, TaskExecutionState.FINAL_VALIDATION,
        TaskExecutionState.HUMAN_REQUIRED,
        TaskExecutionState.SUPERVISING, TaskExecutionState.FAILED, TaskExecutionState.CANCELLED,
    },
    TaskExecutionState.WAITING_FOR_AGENT: {
        TaskExecutionState.RUNNING, TaskExecutionState.SUPERVISING, TaskExecutionState.HUMAN_REQUIRED,
        TaskExecutionState.FAILED, TaskExecutionState.CANCELLED,
    },
    TaskExecutionState.WAITING_FOR_ENVIRONMENT: {
        TaskExecutionState.RUNNING, TaskExecutionState.HUMAN_REQUIRED,
        TaskExecutionState.FAILED, TaskExecutionState.CANCELLED,
    },
    TaskExecutionState.RESOURCE_MAINTENANCE: {
        TaskExecutionState.RUNNING, TaskExecutionState.PAUSED_LOW_DISK,
        TaskExecutionState.FAILED, TaskExecutionState.CANCELLED,
    },
    TaskExecutionState.PAUSED_LOW_DISK: {
        TaskExecutionState.RECOVERING, TaskExecutionState.RESOURCE_MAINTENANCE,
        TaskExecutionState.FAILED, TaskExecutionState.CANCELLED,
    },
    TaskExecutionState.OPERATOR_PAUSED: {
        TaskExecutionState.RUNNING, TaskExecutionState.FAILED, TaskExecutionState.CANCELLED,
    },
    TaskExecutionState.RECOVERING: {
        TaskExecutionState.RUNNING, TaskExecutionState.WAITING_FOR_ENVIRONMENT,
        TaskExecutionState.FAILED, TaskExecutionState.CANCELLED,
    },
    TaskExecutionState.FINAL_VALIDATION: {
        TaskExecutionState.COMPLETED, TaskExecutionState.HUMAN_REQUIRED,
        TaskExecutionState.RUNNING, TaskExecutionState.FAILED, TaskExecutionState.CANCELLED,
    },
    TaskExecutionState.HUMAN_REQUIRED: {
        TaskExecutionState.RUNNING, TaskExecutionState.SUPERVISING,
        TaskExecutionState.CANCELLED, TaskExecutionState.FAILED,
    },
    TaskExecutionState.SUPERVISING: {
        TaskExecutionState.RUNNING, TaskExecutionState.WAITING_FOR_AGENT,
        TaskExecutionState.WAITING_FOR_HUMAN_DECISION, TaskExecutionState.HUMAN_REQUIRED,
        TaskExecutionState.FAILED, TaskExecutionState.CANCELLED,
    },
    TaskExecutionState.WAITING_FOR_HUMAN_DECISION: {
        TaskExecutionState.SUPERVISING, TaskExecutionState.HUMAN_REQUIRED,
        TaskExecutionState.CANCELLED, TaskExecutionState.FAILED,
    },
    TaskExecutionState.COMPLETED: set(),
    TaskExecutionState.FAILED: set(),
    TaskExecutionState.CANCELLED: set(),
}


def validate_task_execution_transition(from_state: TaskExecutionState, to_state: TaskExecutionState) -> None:
    allowed = _VALID_TASK_EXECUTION_TRANSITIONS.get(from_state, set())
    if to_state not in allowed:
        raise OrchestrateError(
            f"invalid task execution state transition: {from_state.value} -> {to_state.value}"
        )


_VALID_STAGE_TRANSITIONS: dict[WorkPackageStage, set[WorkPackageStage]] = {
    WorkPackageStage.PREPARE: {
        WorkPackageStage.DECOMPOSE, WorkPackageStage.IMPLEMENT,
        WorkPackageStage.FAST_VERIFY, WorkPackageStage.REVIEW,
        WorkPackageStage.COMPLETED,
    },
    WorkPackageStage.DECOMPOSE: {
        WorkPackageStage.IMPLEMENT, WorkPackageStage.REVIEW,
        WorkPackageStage.FIX_REVIEW, WorkPackageStage.REGRESSION_VERIFY,
        WorkPackageStage.FINAL_REVIEW, WorkPackageStage.COMPLETED,
    },
    WorkPackageStage.IMPLEMENT: {
        WorkPackageStage.DECOMPOSE, WorkPackageStage.FAST_VERIFY,
        WorkPackageStage.TARGETED_VERIFY, WorkPackageStage.REVIEW,
        WorkPackageStage.FIX_REVIEW,
    },
    WorkPackageStage.FAST_VERIFY: {
        WorkPackageStage.TARGETED_VERIFY, WorkPackageStage.REVIEW,
        WorkPackageStage.FIX_REVIEW, WorkPackageStage.IMPLEMENT,
    },
    WorkPackageStage.TARGETED_VERIFY: {
        WorkPackageStage.REVIEW, WorkPackageStage.FIX_REVIEW, WorkPackageStage.IMPLEMENT,
    },
    WorkPackageStage.REVIEW: {
        WorkPackageStage.DECOMPOSE, WorkPackageStage.FIX_REVIEW,
        WorkPackageStage.FINAL_REVIEW, WorkPackageStage.REGRESSION_VERIFY,
        WorkPackageStage.IMPLEMENT, WorkPackageStage.COMPLETED,
    },
    WorkPackageStage.FIX_REVIEW: {
        WorkPackageStage.DECOMPOSE, WorkPackageStage.REGRESSION_VERIFY,
        WorkPackageStage.REVIEW, WorkPackageStage.FINAL_REVIEW,
        WorkPackageStage.IMPLEMENT,
    },
    WorkPackageStage.REGRESSION_VERIFY: {
        WorkPackageStage.FINAL_REVIEW, WorkPackageStage.REVIEW,
        WorkPackageStage.FIX_REVIEW, WorkPackageStage.IMPLEMENT,
    },
    WorkPackageStage.FINAL_REVIEW: {
        WorkPackageStage.DECOMPOSE, WorkPackageStage.READY_TO_COMMIT,
        WorkPackageStage.FIX_REVIEW, WorkPackageStage.FULL_VERIFY,
        WorkPackageStage.REVIEW,
    },
    WorkPackageStage.FULL_VERIFY: {
        WorkPackageStage.READY_TO_COMMIT, WorkPackageStage.FIX_REVIEW,
        WorkPackageStage.FINAL_REVIEW,
    },
    WorkPackageStage.READY_TO_COMMIT: {
        WorkPackageStage.COMPLETED, WorkPackageStage.FULL_VERIFY,
    },
    WorkPackageStage.COMPLETED: set(),
}


def validate_stage_transition(from_stage: WorkPackageStage, to_stage: WorkPackageStage) -> None:
    allowed = _VALID_STAGE_TRANSITIONS.get(from_stage, set())
    if to_stage not in allowed:
        raise OrchestrateError(
            f"invalid work-package stage transition: {from_stage.value} -> {to_stage.value}"
        )


class OrchestrateError(RuntimeError):
    """Raised on invalid orchestration operations."""


class _AgentWaitRequested(Exception):
    """Raised internally after a durable waiting_for_agent state is saved."""


class _StageEscalated(Exception):
    """Raised internally after a non-recoverable check enters HUMAN_REQUIRED."""



@dataclass
class AcceptanceCriterion:
    id: str
    description: str
    verified: bool = False
    evidence: str = ""


@dataclass
class WorkPackage:
    id: str
    title: str
    dependencies: list[str] = field(default_factory=list)
    requirements: list[str] = field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterion] = field(default_factory=list)
    affected_repositories: list[str] = field(default_factory=list)
    kind: WorkPackageKind = WorkPackageKind.DEVELOPMENT
    repository_sync: RepositorySyncSpec | None = None
    stage: WorkPackageStage = WorkPackageStage.PREPARE
    status: str = "pending"
    risk: str = "medium"
    priority: int = 0
    complexity: int = 0
    verification_profile: str = "targeted"
    agent_id: str = ""
    reviewer_id: str = ""
    final_reviewer_id: str = ""
    last_fixer_id: str = ""
    # Operator-defined soft scheduling preferences. Keys are orchestration
    # roles (decompose, implement, review, fix_review, final_review) and values
    # are ranked provider IDs. Preferences never bypass health, capability,
    # complexity, independence, or concurrency policy; the scheduler falls
    # back to its normal deterministic ring when none is currently eligible.
    agent_preferences: dict[str, list[str]] = field(default_factory=dict)
    # Roles listed here treat the ranked preference pool as binding. This is
    # used by Force routing; ordinary preferences remain soft hints.
    agent_preference_binding_roles: list[str] = field(default_factory=list)
    # Ranked workflow skill IDs per execution role. Empty/missing roles inherit
    # the canonical role defaults from execution_policy.py.
    skill_preferences: dict[str, list[str]] = field(default_factory=dict)
    agent_history: dict[str, list[str]] = field(default_factory=dict)
    verification_attempts: int = 0
    review_cycles: int = 0
    review_findings: list[str] = field(default_factory=list)
    # Deterministic rescue campaigns are separate from ordinary review cycles.
    # They reopen an exhausted review/fix loop without asking the broad
    # Supervisor to rediscover findings that are already durable and exact.
    review_recovery_cycles: int = 0
    review_recovery_fingerprint: str = ""
    review_recovery_origin_stage: str = ""
    implementation_summary: str = ""
    # Compact, durable causal context for the next stage. Full provider output
    # remains in the artifact and invocation stores; these bounded summaries
    # survive restart and prevent review/retry handoffs from becoming generic.
    last_implementation: dict[str, Any] = field(default_factory=dict)
    last_verification: dict[str, Any] = field(default_factory=dict)
    # Explicit operator authorization for an exact, package-scoped set of
    # pre-existing verification failures.  The raw command remains failed;
    # this record only affects the package-level blocking decision while its
    # fingerprints continue to match.
    verification_baseline_acceptance: dict[str, Any] = field(default_factory=dict)
    # Explicit, package-scoped operator disposition for a late review or
    # acceptance check that the operator chooses to defer. Unlike verification
    # baseline acceptance this does not rewrite any check as passed: criterion
    # ``verified`` flags and reviewer findings remain truthful, while this
    # durable record authorizes finalization of the exact active escalation.
    operator_risk_acceptance: dict[str, Any] = field(default_factory=dict)
    last_review: dict[str, Any] = field(default_factory=dict)
    last_agent_attempts: list[dict[str, Any]] = field(default_factory=list)
    last_invocation_id: str = ""
    execution_mode: str = "standard"
    parent_id: str = ""
    shard_key: str = ""
    generated_by: str = ""
    parallel_safe: bool = False
    read_scope: list[str] = field(default_factory=list)
    write_scope: list[str] = field(default_factory=list)
    conflict_keys: list[str] = field(default_factory=list)
    decomposition_status: str = ""
    decomposition_origin_stage: str = ""
    decomposition_reason: str = ""
    decomposition_agent_id: str = ""
    decomposition_plan_hash: str = ""
    shard_ids: list[str] = field(default_factory=list)
    # Operator-controlled scheduling hold. A paused package remains fully
    # inspectable and preserves its current stage/status, but is omitted from
    # the ready queue until explicitly resumed.
    operator_paused: bool = False
    operator_pause_reason: str = ""
    operator_paused_at: str = ""
    # One-shot scheduling check configured before the Work Package becomes ready.
    # The orchestrator stops before assigning an agent, then the next explicit
    # run acknowledges the check and continues.
    pause_before_start: bool = False
    pause_before_start_reason: str = ""
    pause_before_start_requested_at: str = ""
    pause_before_start_reached_at: str = ""
    # Runtime-only one-shot hold used by control-plane flows such as
    # Pause & Sync. It is intentionally not accepted in declarative PLAN
    # revisions; the orchestrator clears it when the operator resumes.
    pause_after_completion: bool = False
    pause_after_completion_reason: str = ""
    pause_after_completion_requested_at: str = ""
    pause_after_completion_reached_at: str = ""
    # Mandatory decomposition bypasses automatic complexity heuristics but not
    # deterministic validation. It remains set until the decomposition result
    # is durably recorded as atomic or expanded.
    decomposition_required: bool = False
    decomposition_required_reason: str = ""
    decomposition_required_at: str = ""
    decomposition_required_consumed_at: str = ""

    def __post_init__(self) -> None:
        if not 0 <= int(self.complexity) <= 100:
            raise OrchestrateError(
                f"work package {self.id!r} complexity must be between 0 and 100"
            )
        if int(self.review_recovery_cycles) < 0:
            raise OrchestrateError(
                f"work package {self.id!r} review_recovery_cycles cannot be negative"
            )
        if not isinstance(self.kind, WorkPackageKind):
            try:
                self.kind = WorkPackageKind(str(self.kind))
            except ValueError as exc:
                raise OrchestrateError(
                    f"work package {self.id!r} has unsupported kind {self.kind!r}"
                ) from exc
        if self.kind == WorkPackageKind.REPOSITORY_SYNC:
            if self.repository_sync is None:
                raise OrchestrateError(
                    f"repository-sync package {self.id!r} requires repository_sync configuration"
                )
            selected = tuple(self.repository_sync.repository_ids)
            affected = tuple(self.affected_repositories)
            if set(selected) != set(affected) or len(selected) != len(affected):
                raise OrchestrateError(
                    f"repository-sync package {self.id!r} affected_repositories must exactly "
                    "match repository_sync repositories"
                )
            if self.execution_mode != "standard":
                raise OrchestrateError(
                    f"repository-sync package {self.id!r} must use execution_mode='standard'"
                )
            if self.parallel_safe or self.parent_id or self.shard_key:
                raise OrchestrateError(
                    f"repository-sync package {self.id!r} cannot be a parallel shard"
                )
            if self.decomposition_required or self.decomposition_status:
                raise OrchestrateError(
                    f"repository-sync package {self.id!r} cannot request decomposition"
                )
        elif self.repository_sync is not None:
            raise OrchestrateError(
                f"work package {self.id!r} declares repository_sync but kind is not repository_sync"
            )
        # Direct programmatic construction must obey the same durable policy
        # contract as plan/state deserialization. Normalize copies here so
        # callers cannot retain aliased mutable lists or bypass role validation.
        self.agent_preferences = _normalized_role_preferences(
            self.agent_preferences, label="agent preferences"
        )
        self.agent_preference_binding_roles = normalize_binding_roles(
            self.agent_preference_binding_roles
        )
        self.skill_preferences = _normalized_role_preferences(
            self.skill_preferences, label="skill preferences"
        )

    def complexity_score(self) -> int:
        """Return an explicit or deterministically inferred 1..100 score.

        Existing plans need no migration: risk, repository breadth, contract
        size, and verification depth provide a conservative automatic score.
        ``complexity`` may override the inference when a plan author has better
        domain knowledge.
        """

        if self.complexity:
            return max(1, min(100, int(self.complexity)))
        risk_base = {
            "low": 25,
            "medium": 50,
            "high": 75,
            "critical": 90,
        }.get(self.risk.strip().lower(), 50)
        repository_bonus = min(10, max(0, len(self.affected_repositories) - 1) * 3)
        contract_bonus = min(
            10,
            len(self.requirements) + len(self.acceptance_criteria),
        )
        verification_bonus = {
            "cheap": 0,
            "focused": 2,
            "targeted": 3,
            "integration": 6,
            "full": 10,
        }.get(self.verification_profile.strip().lower(), 3)
        return max(
            1,
            min(100, risk_base + repository_bonus + contract_bonus + verification_bonus),
        )

    def as_mapping(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "dependencies": list(self.dependencies),
            "requirements": list(self.requirements),
            "acceptance_criteria": [
                {"id": ac.id, "description": ac.description,
                 "verified": ac.verified, "evidence": ac.evidence}
                for ac in self.acceptance_criteria
            ],
            "affected_repositories": list(self.affected_repositories),
            "kind": self.kind.value,
            "repository_sync": (
                self.repository_sync.as_mapping() if self.repository_sync is not None else {}
            ),
            "stage": self.stage.value,
            "status": self.status,
            "risk": self.risk,
            "priority": self.priority,
            "complexity": self.complexity,
            "computed_complexity": self.complexity_score(),
            "verification_profile": self.verification_profile,
            "agent_id": self.agent_id,
            "reviewer_id": self.reviewer_id,
            "final_reviewer_id": self.final_reviewer_id,
            "last_fixer_id": self.last_fixer_id,
            "agent_preferences": {
                str(role): [str(item) for item in providers]
                for role, providers in self.agent_preferences.items()
            },
            "agent_preference_binding_roles": list(self.agent_preference_binding_roles),
            "skill_preferences": {
                str(role): [str(item) for item in skills]
                for role, skills in self.skill_preferences.items()
            },
            "agent_history": {
                str(capability): [str(item) for item in providers]
                for capability, providers in self.agent_history.items()
            },
            "verification_attempts": self.verification_attempts,
            "review_cycles": self.review_cycles,
            "review_findings": list(self.review_findings),
            "review_recovery_cycles": self.review_recovery_cycles,
            "review_recovery_fingerprint": self.review_recovery_fingerprint,
            "review_recovery_origin_stage": self.review_recovery_origin_stage,
            "implementation_summary": self.implementation_summary,
            "last_implementation": dict(self.last_implementation),
            "last_verification": dict(self.last_verification),
            "verification_baseline_acceptance": dict(
                self.verification_baseline_acceptance
            ),
            "operator_risk_acceptance": dict(self.operator_risk_acceptance),
            "last_review": dict(self.last_review),
            "last_agent_attempts": [dict(item) for item in self.last_agent_attempts],
            "last_invocation_id": self.last_invocation_id,
            "execution_mode": self.execution_mode,
            "parent_id": self.parent_id,
            "shard_key": self.shard_key,
            "generated_by": self.generated_by,
            "parallel_safe": self.parallel_safe,
            "read_scope": list(self.read_scope),
            "write_scope": list(self.write_scope),
            "conflict_keys": list(self.conflict_keys),
            "decomposition_status": self.decomposition_status,
            "decomposition_origin_stage": self.decomposition_origin_stage,
            "decomposition_reason": self.decomposition_reason,
            "decomposition_agent_id": self.decomposition_agent_id,
            "decomposition_plan_hash": self.decomposition_plan_hash,
            "shard_ids": list(self.shard_ids),
            "operator_paused": self.operator_paused,
            "operator_pause_reason": self.operator_pause_reason,
            "operator_paused_at": self.operator_paused_at,
            "pause_before_start": self.pause_before_start,
            "pause_before_start_reason": self.pause_before_start_reason,
            "pause_before_start_requested_at": self.pause_before_start_requested_at,
            "pause_before_start_reached_at": self.pause_before_start_reached_at,
            "pause_after_completion": self.pause_after_completion,
            "pause_after_completion_reason": self.pause_after_completion_reason,
            "pause_after_completion_requested_at": self.pause_after_completion_requested_at,
            "pause_after_completion_reached_at": self.pause_after_completion_reached_at,
            "decomposition_required": self.decomposition_required,
            "decomposition_required_reason": self.decomposition_required_reason,
            "decomposition_required_at": self.decomposition_required_at,
            "decomposition_required_consumed_at": (
                self.decomposition_required_consumed_at
            ),
        }

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "WorkPackage":
        return cls(
            id=str(data["id"]),
            title=str(data.get("title", "")),
            dependencies=list(data.get("dependencies", [])),
            requirements=list(data.get("requirements", [])),
            acceptance_criteria=[
                AcceptanceCriterion(
                    id=ac["id"],
                    description=ac.get("description", ""),
                    verified=ac.get("verified", False),
                    evidence=ac.get("evidence", ""),
                )
                for ac in data.get("acceptance_criteria", [])
            ],
            affected_repositories=list(data.get("affected_repositories", [])),
            kind=WorkPackageKind(str(data.get("kind", "development"))),
            repository_sync=(
                RepositorySyncSpec.from_mapping(data.get("repository_sync"))
                if str(data.get("kind", "development")) == WorkPackageKind.REPOSITORY_SYNC.value
                else None
            ),
            stage=WorkPackageStage(data.get("stage", "prepare")),
            status=str(data.get("status", "pending")),
            risk=str(data.get("risk", "medium")),
            priority=int(data.get("priority", 0)),
            complexity=int(data.get("complexity", 0)),
            verification_profile=str(data.get("verification_profile", "targeted")),
            agent_id=str(data.get("agent_id", "")),
            reviewer_id=str(data.get("reviewer_id", "")),
            final_reviewer_id=str(data.get("final_reviewer_id", "")),
            last_fixer_id=str(data.get("last_fixer_id", "")),
            agent_preferences=_normalized_role_preferences(
                data.get("agent_preferences"), label="agent preferences"
            ),
            agent_preference_binding_roles=normalize_binding_roles(
                data.get("agent_preference_binding_roles")
            ),
            skill_preferences=_normalized_role_preferences(
                data.get("skill_preferences"), label="skill preferences"
            ),
            agent_history={
                str(capability): [str(item) for item in providers]
                for capability, providers in (data.get("agent_history") or {}).items()
                if isinstance(providers, list)
            },
            verification_attempts=int(data.get("verification_attempts", 0)),
            review_cycles=int(data.get("review_cycles", 0)),
            review_findings=[str(item) for item in data.get("review_findings", [])],
            review_recovery_cycles=max(
                0, int(data.get("review_recovery_cycles", 0))
            ),
            review_recovery_fingerprint=str(
                data.get("review_recovery_fingerprint", "")
            ),
            review_recovery_origin_stage=str(
                data.get("review_recovery_origin_stage", "")
            ),
            implementation_summary=str(data.get("implementation_summary", "")),
            last_implementation=dict(data.get("last_implementation") or {}),
            last_verification=dict(data.get("last_verification") or {}),
            verification_baseline_acceptance=dict(
                data.get("verification_baseline_acceptance") or {}
            ),
            operator_risk_acceptance=dict(
                data.get("operator_risk_acceptance") or {}
            ),
            last_review=dict(data.get("last_review") or {}),
            last_agent_attempts=[
                dict(item)
                for item in (data.get("last_agent_attempts") or [])
                if isinstance(item, dict)
            ][-12:],
            last_invocation_id=str(data.get("last_invocation_id", "")),
            execution_mode=str(data.get("execution_mode", "standard")),
            parent_id=str(data.get("parent_id", "")),
            shard_key=str(data.get("shard_key", "")),
            generated_by=str(data.get("generated_by", "")),
            parallel_safe=bool(data.get("parallel_safe", False)),
            read_scope=[str(item) for item in data.get("read_scope", [])],
            write_scope=[str(item) for item in data.get("write_scope", [])],
            conflict_keys=[str(item) for item in data.get("conflict_keys", [])],
            decomposition_status=str(data.get("decomposition_status", "")),
            decomposition_origin_stage=str(data.get("decomposition_origin_stage", "")),
            decomposition_reason=str(data.get("decomposition_reason", "")),
            decomposition_agent_id=str(data.get("decomposition_agent_id", "")),
            decomposition_plan_hash=str(data.get("decomposition_plan_hash", "")),
            shard_ids=[str(item) for item in data.get("shard_ids", [])],
            operator_paused=bool(data.get("operator_paused", False)),
            operator_pause_reason=str(data.get("operator_pause_reason", "")),
            operator_paused_at=str(data.get("operator_paused_at", "")),
            pause_before_start=bool(data.get("pause_before_start", False)),
            pause_before_start_reason=str(
                data.get("pause_before_start_reason", "")
            ),
            pause_before_start_requested_at=str(
                data.get("pause_before_start_requested_at", "")
            ),
            pause_before_start_reached_at=str(
                data.get("pause_before_start_reached_at", "")
            ),
            pause_after_completion=bool(data.get("pause_after_completion", False)),
            pause_after_completion_reason=str(
                data.get("pause_after_completion_reason", "")
            ),
            pause_after_completion_requested_at=str(
                data.get("pause_after_completion_requested_at", "")
            ),
            pause_after_completion_reached_at=str(
                data.get("pause_after_completion_reached_at", "")
            ),
            decomposition_required=bool(
                data.get("decomposition_required", False)
            ),
            decomposition_required_reason=str(
                data.get("decomposition_required_reason", "")
            ),
            decomposition_required_at=str(
                data.get("decomposition_required_at", "")
            ),
            decomposition_required_consumed_at=str(
                data.get("decomposition_required_consumed_at", "")
            ),
        )


@dataclass
class PlanGraph:
    work_packages: list[WorkPackage] = field(default_factory=list)

    def package_by_id(self, package_id: str) -> WorkPackage:
        for package in self.work_packages:
            if package.id == package_id:
                return package
        raise OrchestrateError(f"work package not found: {package_id}")

    def dependency_ready(self, package_id: str) -> bool:
        package = self.package_by_id(package_id)
        for dep_id in package.dependencies:
            dep = self.package_by_id(dep_id)
            if dep.stage != WorkPackageStage.COMPLETED:
                return False
        return True

    def ready_packages(self) -> list[WorkPackage]:
        return sorted(
            [
                package
                for package in self.work_packages
                if package.status == "pending"
                and not package.operator_paused
                and self.dependency_ready(package.id)
                and package.stage != WorkPackageStage.COMPLETED
            ],
            key=lambda package: (
                0 if package.kind == WorkPackageKind.REPOSITORY_SYNC else 1,
                -package.priority,
                package.id,
            ),
        )

    def validate_acyclic(self) -> None:
        visited: set[str] = set()
        recursion_stack: set[str] = set()

        def _visit(package_id: str) -> None:
            if package_id in recursion_stack:
                raise OrchestrateError(
                    f"cycle detected in work-package graph involving {package_id}"
                )
            if package_id in visited:
                return
            recursion_stack.add(package_id)
            visited.add(package_id)
            package = self.package_by_id(package_id)
            for dep_id in package.dependencies:
                self.package_by_id(dep_id)
                _visit(dep_id)
            recursion_stack.remove(package_id)

        for package in self.work_packages:
            _visit(package.id)

    def validate_completeness(self) -> list[str]:
        findings: list[str] = []
        ids = {p.id for p in self.work_packages}
        seen_ids: set[str] = set()
        for package in self.work_packages:
            if package.id in seen_ids:
                # Two distinct packages deriving (or being given) the same
                # ID would otherwise silently collide: PlanGraph.
                # package_by_id() only ever returns the *first* match, so a
                # dependent referencing this ID would silently resolve
                # against the wrong package instead of failing loudly.
                findings.append(f"duplicate work package id: {package.id}")
            seen_ids.add(package.id)
            for dep_id in package.dependencies:
                if dep_id not in ids:
                    findings.append(
                        f"package {package.id} depends on missing package {dep_id}"
                    )
            if not package.acceptance_criteria:
                findings.append(f"package {package.id} has no acceptance criteria")
            if not package.requirements:
                findings.append(f"package {package.id} has no requirements")
        return findings


@dataclass
class TaskExecutionStateRecord:
    schema_version: int = 1
    project_id: str = ""
    state: TaskExecutionState = TaskExecutionState.INITIALIZING
    plan_graph: PlanGraph = field(default_factory=PlanGraph)
    started_at: str = ""
    last_transition_at: str = ""
    completed_packages: int = 0
    total_packages: int = 0
    error_message: str = ""
    waiting: dict[str, Any] = field(default_factory=dict)
    agent_waits: dict[str, dict[str, Any]] = field(default_factory=dict)
    scheduler: dict[str, Any] = field(default_factory=dict)

    def as_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "state": self.state.value,
            "plan_graph": {
                "work_packages": [p.as_mapping() for p in self.plan_graph.work_packages],
            },
            "started_at": self.started_at,
            "last_transition_at": self.last_transition_at,
            "completed_packages": self.completed_packages,
            "total_packages": self.total_packages,
            "error_message": self.error_message,
            "waiting": dict(self.waiting),
            "agent_waits": {
                str(package_id): dict(waiting)
                for package_id, waiting in self.agent_waits.items()
            },
            "scheduler": dict(self.scheduler),
        }

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "TaskExecutionStateRecord":
        graph_data = data.get("plan_graph", {})
        packages = [WorkPackage.from_mapping(p) for p in graph_data.get("work_packages", [])]
        waiting = dict(data.get("waiting") or {})
        project_state = TaskExecutionState(data.get("state", "initializing"))
        agent_waits = {
            str(package_id): dict(value)
            for package_id, value in (data.get("agent_waits") or {}).items()
            if isinstance(value, dict)
        }
        # Schema generations before ``agent_waits`` persisted only the active
        # provider wait in ``waiting``.  Migrate that compatibility shape only
        # when the Task execution state proves it is an agent-availability wait.
        # Operator pause/sync boundaries also carry package IDs and must never
        # be copied into the agent scheduler.
        if (
            waiting.get("package_id")
            and not agent_waits
            and project_state == TaskExecutionState.WAITING_FOR_AGENT
        ):
            agent_waits[str(waiting["package_id"])] = dict(waiting)
        return cls(
            schema_version=int(data.get("schema_version", 1)),
            project_id=str(data.get("project_id", "")),
            state=project_state,
            plan_graph=PlanGraph(work_packages=packages),
            started_at=str(data.get("started_at", "")),
            last_transition_at=str(data.get("last_transition_at", "")),
            completed_packages=int(data.get("completed_packages", 0)),
            total_packages=int(data.get("total_packages", 0)),
            error_message=str(data.get("error_message", "")),
            waiting=waiting,
            agent_waits=agent_waits,
            scheduler=dict(data.get("scheduler") or {}),
        )
