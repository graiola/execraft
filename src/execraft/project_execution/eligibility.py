"""Structured Project Task eligibility diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .models import (
    GateState,
    ProjectExecutionDefinition,
    ProjectExecutionError,
)
from .policy import TaskFailureBehavior
from .runtime_repository import ProjectExecutionRuntimeState
from .task_port import TaskExecutionPort, TaskOutcome


class EligibilityReasonKind(str, Enum):
    TASK_MISSING = "task_missing"
    TASK_ARCHIVED = "task_archived"
    TASK_NOT_EXECUTABLE = "task_not_executable"
    PHASE_ENTRY_GATE_UNSATISFIED = "phase_entry_gate_unsatisfied"
    PREDECESSOR_UNSATISFIED = "predecessor_unsatisfied"
    GATE_UNSATISFIED = "gate_unsatisfied"
    TASK_ALREADY_RUNNING = "task_already_running"
    TASK_TERMINAL = "task_terminal"
    PROJECT_HELD = "project_held"
    PHASE_CANCELLED = "phase_cancelled"
    TASK_FAILURE_POLICY = "task_failure_policy"


@dataclass(frozen=True)
class EligibilityReason:
    kind: EligibilityReasonKind
    message: str
    task_id: str = ""
    gate_id: str = ""
    phase_id: str = ""

    def as_mapping(self) -> dict[str, str]:
        return {
            key: value
            for key, value in (
                ("kind", self.kind.value),
                ("message", self.message),
                ("task_id", self.task_id),
                ("gate_id", self.gate_id),
                ("phase_id", self.phase_id),
            )
            if value
        }


@dataclass(frozen=True)
class EligibilityResult:
    eligible: bool
    reasons: tuple[EligibilityReason, ...] = ()

    def as_mapping(self) -> dict[str, object]:
        return {
            "eligible": self.eligible,
            "reasons": [reason.as_mapping() for reason in self.reasons],
        }


def gate_satisfied(runtime: ProjectExecutionRuntimeState, gate_id: str) -> bool:
    """Return whether a Gate currently authorizes downstream execution."""

    row = runtime.gates.get(gate_id, {})
    state = str(row.get("state", ""))
    current_fingerprint = str(row.get("input_fingerprint", ""))
    if not current_fingerprint:
        return False

    if state == GateState.WAIVED.value:
        waiver = row.get("waiver") or {}
        return (
            isinstance(waiver, dict)
            and waiver.get("input_fingerprint") == current_fingerprint
        )

    evaluation = row.get("evaluation") or {}
    if not isinstance(evaluation, dict):
        return False
    return (
        state == GateState.PASSED.value
        and evaluation.get("input_fingerprint") == current_fingerprint
    )


def evaluate_task_eligibility(
    task_id: str,
    *,
    definition: ProjectExecutionDefinition,
    runtime: ProjectExecutionRuntimeState,
    task_port: TaskExecutionPort,
) -> EligibilityResult:
    """Evaluate one Task and return every blocking reason, not a bare boolean."""

    project_task = definition.task_index.get(task_id)
    if project_task is None:
        raise ProjectExecutionError(
            f"Task {task_id!r} is not referenced by Project Execution"
        )

    summary = task_port.describe(task_id)
    reasons: list[EligibilityReason] = []

    if not summary.exists:
        reasons.append(
            EligibilityReason(
                EligibilityReasonKind.TASK_MISSING,
                f"canonical Task {task_id} does not exist",
                task_id=task_id,
            )
        )
    elif not summary.active:
        reasons.append(
            EligibilityReason(
                EligibilityReasonKind.TASK_ARCHIVED,
                f"Task {task_id} is archived or inactive",
                task_id=task_id,
            )
        )

    if summary.exists and not summary.executable:
        reasons.append(
            EligibilityReason(
                EligibilityReasonKind.TASK_NOT_EXECUTABLE,
                f"Task {task_id} has no executable PLAN.graph.yaml",
                task_id=task_id,
            )
        )

    if runtime.held:
        reasons.append(
            EligibilityReason(
                EligibilityReasonKind.PROJECT_HELD,
                runtime.hold_reason or "Project Execution is held",
            )
        )

    if project_task.phase in runtime.cancelled_phases:
        reasons.append(
            EligibilityReason(
                EligibilityReasonKind.PHASE_CANCELLED,
                f"Phase {project_task.phase} is cancelled",
                phase_id=project_task.phase,
            )
        )

    phase = definition.phase_index[project_task.phase]
    for gate_id in phase.entry_gates:
        if gate_satisfied(runtime, gate_id):
            continue
        reasons.append(
            EligibilityReason(
                EligibilityReasonKind.PHASE_ENTRY_GATE_UNSATISFIED,
                f"Phase entry Gate {gate_id} is not satisfied",
                gate_id=gate_id,
                phase_id=phase.id,
            )
        )

    for predecessor in project_task.requires.tasks:
        if task_port.outcome(predecessor) == TaskOutcome.COMPLETED:
            continue
        reasons.append(
            EligibilityReason(
                EligibilityReasonKind.PREDECESSOR_UNSATISFIED,
                f"predecessor Task {predecessor} has not completed",
                task_id=predecessor,
            )
        )

    for gate_id in project_task.requires.gates:
        if gate_satisfied(runtime, gate_id):
            continue
        reasons.append(
            EligibilityReason(
                EligibilityReasonKind.GATE_UNSATISFIED,
                f"required Gate {gate_id} is not satisfied",
                gate_id=gate_id,
            )
        )

    if summary.outcome == TaskOutcome.RUNNING:
        reasons.append(
            EligibilityReason(
                EligibilityReasonKind.TASK_ALREADY_RUNNING,
                f"Task {task_id} is already running",
                task_id=task_id,
            )
        )
    elif summary.outcome in {
        TaskOutcome.COMPLETED,
        TaskOutcome.FAILED,
        TaskOutcome.CANCELLED,
    }:
        reasons.append(
            EligibilityReason(
                EligibilityReasonKind.TASK_TERMINAL,
                f"Task {task_id} is terminal ({summary.outcome.value})",
                task_id=task_id,
            )
        )

    if (
        definition.policy.task_failure_behavior != TaskFailureBehavior.CONTINUE
        and _required_task_failed(definition, task_port)
    ):
        reasons.append(
            EligibilityReason(
                EligibilityReasonKind.TASK_FAILURE_POLICY,
                "a required Task failed and Project Execution is configured "
                f"to {definition.policy.task_failure_behavior.value}",
            )
        )

    return EligibilityResult(eligible=not reasons, reasons=tuple(reasons))


def _required_task_failed(
    definition: ProjectExecutionDefinition,
    task_port: TaskExecutionPort,
) -> bool:
    return any(
        project_task.required
        and task_port.outcome(project_task.task_id) == TaskOutcome.FAILED
        for project_task in definition.tasks
    )
