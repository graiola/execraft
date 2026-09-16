"""Immutable, provider-neutral Project Milestone baseline construction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .models import DeliveryPolicy, ProjectExecutionDefinition, ProjectMilestone
from .runtime_repository import ProjectExecutionRuntimeState
from .task_port import TaskEvidence, TaskExecutionPort, TaskOutcome


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class BaselineIssue:
    kind: str
    message: str
    subject_id: str = ""

    def as_mapping(self) -> dict[str, str]:
        return {
            key: value
            for key, value in (
                ("kind", self.kind),
                ("message", self.message),
                ("subject_id", self.subject_id),
            )
            if value
        }


@dataclass(frozen=True)
class BaselineBuildResult:
    complete: bool
    baseline: dict[str, Any]
    issues: tuple[BaselineIssue, ...] = ()


class ProjectBaselineBuilder:
    """Collect reproducibility evidence through Project-domain interfaces only."""

    def build(
        self,
        milestone: ProjectMilestone,
        *,
        definition: ProjectExecutionDefinition,
        runtime: ProjectExecutionRuntimeState,
        task_port: TaskExecutionPort,
    ) -> BaselineBuildResult:
        issues: list[BaselineIssue] = []
        task_rows: dict[str, dict[str, Any]] = {}
        repositories: dict[str, str] = {}
        artifacts: set[str] = set()

        for task_id in milestone.requires.tasks:
            evidence = task_port.evidence(task_id)
            issues.extend(self._task_issues(milestone, evidence))
            task_rows[task_id] = self._task_row(evidence)
            self._merge_repositories(
                repositories,
                evidence,
                issues=issues,
            )
            artifacts.update(evidence.artifacts)

        gate_rows: dict[str, dict[str, Any]] = {}
        for gate_id in milestone.requires.gates:
            row = runtime.gates.get(gate_id, {})
            fingerprint = str(row.get("input_fingerprint", ""))
            gate_rows[gate_id] = {
                "state": row.get("state", "waiting"),
                "evaluation_revision": row.get("evaluation_revision", 0),
                "input_fingerprint": fingerprint,
            }
            if not fingerprint:
                issues.append(
                    BaselineIssue(
                        "missing_gate_fingerprint",
                        f"Gate {gate_id} has no evidence fingerprint",
                        gate_id,
                    )
                )

        baseline = {
            "achieved_at": _now(),
            "definition_revision": definition.revision,
            "tasks": task_rows,
            "gates": gate_rows,
            "repositories": repositories,
            "artifacts": sorted(artifacts),
            "delivery": {"policy": milestone.delivery_policy.value},
        }
        return BaselineBuildResult(
            complete=not issues,
            baseline=baseline,
            issues=tuple(issues),
        )

    @staticmethod
    def _task_issues(
        milestone: ProjectMilestone,
        evidence: TaskEvidence,
    ) -> list[BaselineIssue]:
        issues: list[BaselineIssue] = []
        task_id = evidence.task_id
        if evidence.outcome != TaskOutcome.COMPLETED:
            issues.append(
                BaselineIssue(
                    "task_not_completed",
                    f"Task {task_id} is {evidence.outcome.value}",
                    task_id,
                )
            )
        if not evidence.task_digest:
            issues.append(
                BaselineIssue(
                    "missing_task_digest",
                    f"Task {task_id} has no TASK.yaml digest",
                    task_id,
                )
            )
        if not evidence.plan_digest:
            issues.append(
                BaselineIssue(
                    "missing_plan_digest",
                    f"Task {task_id} has no PLAN.graph.yaml digest",
                    task_id,
                )
            )
        if (
            milestone.delivery_policy == DeliveryPolicy.CANDIDATE
            and not evidence.repository_revisions
        ):
            issues.append(
                BaselineIssue(
                    "missing_repository_revision",
                    f"Task {task_id} has no reproducible repository revision",
                    task_id,
                )
            )
        return issues

    @staticmethod
    def _task_row(evidence: TaskEvidence) -> dict[str, Any]:
        return {
            "outcome": evidence.outcome.value,
            "task_digest": evidence.task_digest,
            "plan_digest": evidence.plan_digest,
            "verification": {
                "outcome": evidence.verification.outcome,
                "passed": evidence.verification.passed,
                "failed": evidence.verification.failed,
                "total": evidence.verification.total,
            },
        }

    @staticmethod
    def _merge_repositories(
        repositories: dict[str, str],
        evidence: TaskEvidence,
        *,
        issues: list[BaselineIssue],
    ) -> None:
        for repository_id, commit in evidence.repository_revisions.items():
            prior = repositories.get(repository_id)
            if prior and prior != commit:
                issues.append(
                    BaselineIssue(
                        "repository_revision_conflict",
                        f"repository {repository_id} has conflicting revisions "
                        f"{prior} and {commit}",
                        repository_id,
                    )
                )
            repositories[repository_id] = commit
