"""Project Executor coordinating Tasks without entering Work Package orchestration."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any
import uuid

from execraft.persistence.locks import FileLock, LockLevel

from .automatic import AutomaticExecutionPlanner, AutomaticStartPlan
from .eligibility import EligibilityResult, evaluate_task_eligibility
from .events import ProjectEventJournal
from .gates import GateEvaluationService
from .graph import topological_order
from .milestones import MilestoneAchievementService
from .models import (
    ExecutionMode,
    ProjectExecutionDefinition,
    ProjectExecutionError,
)
from .phases import PhaseProjection, project_phase
from .recovery import reconcile_start_intents
from .repository import ProjectExecutionRepository
from .policy import TaskFailureBehavior
from .runtime_repository import (
    ProjectExecutionRuntimeState,
    ProjectRuntimeRepository,
)
from .task_port import TaskExecutionPort, TaskOutcome, TaskStartResult

_RESOLVED_INTENT_STATES = {"resolved", "failed", "superseded"}
_PHASE_EVENT = {
    "ready": "project_phase_ready",
    "active": "project_phase_activated",
    "complete": "project_phase_completed",
}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def definition_digest(definition: ProjectExecutionDefinition) -> str:
    """Return the canonical digest used to bind runtime to its definition."""

    encoded = json.dumps(
        definition.as_mapping(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return "sha256:" + sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ProjectExecutionSnapshot:
    """One reconciled read model returned by the Project Executor."""

    definition_revision: int
    definition_digest: str
    mode: str
    held: bool
    gates: dict[str, dict[str, Any]]
    milestones: dict[str, dict[str, Any]]
    phases: dict[str, PhaseProjection]
    eligibility: dict[str, EligibilityResult]
    task_outcomes: dict[str, str]


@dataclass(frozen=True)
class AutomaticExecutionCycle:
    """Durable result of one opt-in Automatic scheduling cycle."""

    started_tasks: tuple[str, ...]
    start_failures: tuple[dict[str, str], ...]
    plan: AutomaticStartPlan
    snapshot: ProjectExecutionSnapshot

    def as_mapping(self) -> dict[str, Any]:
        return {
            "started_tasks": list(self.started_tasks),
            "start_failures": [dict(row) for row in self.start_failures],
            "plan": self.plan.as_mapping(),
        }


class ProjectExecutionEngine:
    """Coordinate canonical Tasks while respecting Project-level control policy.

    ``reconcile`` remains observational in every mode so status/read paths never
    acquire the authority to start software-producing Tasks.  Assisted starts
    use :meth:`start_task`; Automatic mode progresses only through the explicit
    :meth:`automatic_cycle` driver operation.
    """

    def __init__(
        self,
        *,
        definition_repository: ProjectExecutionRepository,
        runtime_repository: ProjectRuntimeRepository,
        task_port: TaskExecutionPort,
        journal: ProjectEventJournal | None = None,
    ) -> None:
        self.definitions = definition_repository
        self.runtime_repository = runtime_repository
        self.task_port = task_port
        self.journal = journal
        self.gates = GateEvaluationService()
        self.milestones = MilestoneAchievementService()
        self.automatic_planner = AutomaticExecutionPlanner()
        self.lock_path = runtime_repository.directory / "executor.lock"

    def reconcile(self) -> ProjectExecutionSnapshot:
        """Recompute durable project state without starting any canonical Task."""

        with self._executor_lock():
            definition = self.definitions.load()
            runtime = self.runtime_repository.load()
            self._sync_definition(runtime, definition)
            reconcile_start_intents(runtime, self.task_port, self.journal)

            self._evaluate_gates(definition, runtime)
            self._evaluate_milestones(definition, runtime)
            eligibility = self._eligibility(definition, runtime)
            self._record_ready_tasks(runtime, eligibility)
            self._observe_tasks(definition, runtime)
            phases = self._project_phases(definition, runtime, eligibility)
            self._record_phase_transitions(runtime, phases)
            runtime.executor["last_reconciled_at"] = _now()

            # Even a logically unchanged reconcile refreshes durable observation
            # metadata and is therefore intentionally persisted.
            self.runtime_repository.save(runtime)
            return self._snapshot(definition, runtime, phases, eligibility)

    def start_task(
        self,
        task_id: str,
        *,
        retry_uncertain: bool = False,
    ) -> TaskStartResult:
        """Start one currently eligible Task with a recoverable intent record."""

        with self._executor_lock():
            definition = self.definitions.load()
            runtime = self.runtime_repository.load()
            if definition.mode == ExecutionMode.OBSERVE:
                raise ProjectExecutionError("Observe mode cannot start Tasks")
            if definition.mode == ExecutionMode.AUTOMATIC:
                raise ProjectExecutionError(
                    "Automatic mode starts Tasks only through an automatic cycle"
                )

            self._sync_definition(runtime, definition)
            self._evaluate_gates(definition, runtime)
            if task_id not in definition.task_index:
                raise ProjectExecutionError(
                    f"Task {task_id!r} is not part of Project Execution"
                )

            eligibility = evaluate_task_eligibility(
                task_id,
                definition=definition,
                runtime=runtime,
                task_port=self.task_port,
            )
            if not eligibility.eligible:
                details = "; ".join(
                    reason.message for reason in eligibility.reasons
                )
                raise ProjectExecutionError(f"Task is not eligible: {details}")

            pending = self._pending_start_intent(runtime, task_id)
            if pending is not None and not retry_uncertain:
                raise ProjectExecutionError(
                    f"Task {task_id} has an unresolved start intent; "
                    "reconcile or explicitly retry it"
                )
            return self._start_with_intent(task_id, runtime, retry_of=pending)

    def automatic_cycle(self) -> AutomaticExecutionCycle:
        """Run one bounded Automatic scheduling cycle.

        The cycle is explicit and side-effecting.  It evaluates current evidence,
        applies failure policy, plans against running/uncertain Task capacity, and
        then starts only the selected canonical Tasks through ``TaskExecutionPort``.
        It never enters Work Package scheduling.
        """

        with self._executor_lock():
            definition = self.definitions.load()
            if definition.mode != ExecutionMode.AUTOMATIC:
                raise ProjectExecutionError(
                    "automatic cycles require Project Execution mode=automatic"
                )
            runtime = self.runtime_repository.load()
            self._sync_definition(runtime, definition)
            reconcile_start_intents(runtime, self.task_port, self.journal)
            self._evaluate_gates(definition, runtime)
            self._evaluate_milestones(definition, runtime)
            self._observe_tasks(definition, runtime)
            self._apply_failure_policy(definition, runtime)

            eligibility = self._eligibility(definition, runtime)
            self._record_ready_tasks(runtime, eligibility)
            phases = self._project_phases(definition, runtime, eligibility)
            self._record_phase_transitions(runtime, phases)
            plan = self.automatic_planner.plan(
                definition=definition,
                runtime=runtime,
                eligibility=eligibility,
                phases=phases,
            )

            started: list[str] = []
            failures: list[dict[str, str]] = []
            cycle_id = f"automatic-{uuid.uuid4().hex[:12]}"
            self._append_event(
                "project_automatic_cycle_started",
                {"cycle_id": cycle_id, "planned_tasks": list(plan.task_ids)},
            )

            if not runtime.held:
                for task_id in plan.task_ids:
                    try:
                        result = self._start_with_intent(task_id, runtime)
                    except Exception as exc:
                        failure = {"task_id": task_id, "error": str(exc)}
                        failures.append(failure)
                        self._append_event(
                            "project_automatic_task_start_failed",
                            {"cycle_id": cycle_id, **failure},
                        )
                        # An ambiguous failure may have crossed the Task durable
                        # start boundary. Stop this cycle and let reconciliation
                        # reserve its intent before considering more capacity.
                        break
                    if result.accepted or result.already_started:
                        started.append(task_id)
                        continue
                    failure = {
                        "task_id": task_id,
                        "error": result.message or "Task start was rejected",
                    }
                    failures.append(failure)
                    self._append_event(
                        "project_automatic_task_start_failed",
                        {"cycle_id": cycle_id, **failure},
                    )
                    # A systematic adapter/configuration rejection is unlikely to
                    # improve for later Tasks during the same cycle.
                    break

            # Refresh projections after side effects so the returned snapshot and
            # persisted executor metadata describe the state callers can observe.
            self._evaluate_gates(definition, runtime)
            self._evaluate_milestones(definition, runtime)
            self._observe_tasks(definition, runtime)
            eligibility = self._eligibility(definition, runtime)
            self._record_ready_tasks(runtime, eligibility)
            phases = self._project_phases(definition, runtime, eligibility)
            self._record_phase_transitions(runtime, phases)
            completed_at = _now()
            runtime.executor["automatic"] = {
                "last_cycle_id": cycle_id,
                "last_cycle_at": completed_at,
                "started_tasks": list(started),
                "start_failures": failures,
                "plan": plan.as_mapping(),
            }
            runtime.executor["last_reconciled_at"] = completed_at
            self.runtime_repository.save(runtime)
            self._append_event(
                "project_automatic_cycle_completed",
                {
                    "cycle_id": cycle_id,
                    "started_tasks": list(started),
                    "failure_count": len(failures),
                },
            )
            snapshot = self._snapshot(definition, runtime, phases, eligibility)
            return AutomaticExecutionCycle(
                started_tasks=tuple(started),
                start_failures=tuple(failures),
                plan=plan,
                snapshot=snapshot,
            )

    def decide_gate(
        self,
        gate_id: str,
        *,
        actor: str,
        decision: str,
        reason: str = "",
    ) -> dict[str, Any]:
        """Persist one human Gate decision under the Project Executor lock."""

        with self._executor_lock():
            definition = self.definitions.load()
            runtime = self.runtime_repository.load()
            self._sync_definition(runtime, definition)
            self._evaluate_gates(definition, runtime)
            if gate_id not in definition.gate_index:
                raise ProjectExecutionError(f"Gate {gate_id!r} is not defined")
            record = self.gates.decide(
                gate_id,
                runtime=runtime,
                actor=actor,
                decision=decision,
                reason=reason,
                journal=self.journal,
            )
            # Re-evaluate immediately so callers receive durable Gate state that
            # reflects the decision rather than an intermediate audit record.
            self.gates.evaluate(
                definition.gate_index[gate_id],
                definition=definition,
                runtime=runtime,
                task_port=self.task_port,
                journal=self.journal,
            )
            self.runtime_repository.save(runtime)
            return record

    def waive_gate(
        self,
        gate_id: str,
        *,
        actor: str,
        reason: str,
    ) -> dict[str, Any]:
        """Persist an explicit evidence-bound Gate waiver."""

        with self._executor_lock():
            definition = self.definitions.load()
            runtime = self.runtime_repository.load()
            self._sync_definition(runtime, definition)
            self._evaluate_gates(definition, runtime)
            if gate_id not in definition.gate_index:
                raise ProjectExecutionError(f"Gate {gate_id!r} is not defined")
            waiver = self.gates.waive(
                gate_id,
                runtime=runtime,
                actor=actor,
                reason=reason,
                journal=self.journal,
            )
            self.runtime_repository.save(runtime)
            return waiver

    def pause(self, reason: str = "") -> None:
        with self._executor_lock():
            runtime = self.runtime_repository.load()
            runtime.held = True
            runtime.hold_reason = reason.strip() or "operator hold"
            self.runtime_repository.save(runtime)
            self._append_event(
                "project_execution_paused",
                {"reason": runtime.hold_reason},
            )

    def resume(self) -> None:
        with self._executor_lock():
            runtime = self.runtime_repository.load()
            runtime.held = False
            runtime.hold_reason = ""
            self.runtime_repository.save(runtime)
            self._append_event(
                "project_execution_started",
                {"mode": runtime.mode},
            )

    def _apply_failure_policy(
        self,
        definition: ProjectExecutionDefinition,
        runtime: ProjectExecutionRuntimeState,
    ) -> None:
        """Apply required-Task failure behavior without touching running Tasks."""

        failed = [
            task.task_id
            for task in definition.tasks
            if task.required
            and str((runtime.observed_tasks.get(task.task_id) or {}).get("outcome", ""))
            == TaskOutcome.FAILED.value
        ]
        if not failed or definition.policy.task_failure_behavior != TaskFailureBehavior.HOLD:
            return
        if runtime.held:
            return
        runtime.held = True
        runtime.hold_reason = (
            "automatic failure policy hold; required Task(s) failed: "
            + ", ".join(sorted(failed))
        )
        self._append_event(
            "project_execution_paused",
            {
                "reason": runtime.hold_reason,
                "source": "task_failure_policy",
                "task_ids": sorted(failed),
            },
        )

    def _evaluate_gates(
        self,
        definition: ProjectExecutionDefinition,
        runtime: ProjectExecutionRuntimeState,
    ) -> None:
        dependencies = {
            gate.id: tuple(
                criterion.gate_id
                for criterion in gate.criteria
                if criterion.gate_id
            )
            for gate in definition.gates
        }
        for gate_id in topological_order(
            dependencies,
            label="ProjectGate dependency graph",
        ):
            self.gates.evaluate(
                definition.gate_index[gate_id],
                definition=definition,
                runtime=runtime,
                task_port=self.task_port,
                journal=self.journal,
            )

    def _evaluate_milestones(
        self,
        definition: ProjectExecutionDefinition,
        runtime: ProjectExecutionRuntimeState,
    ) -> None:
        dependencies = {
            milestone.id: milestone.requires.milestones
            for milestone in definition.milestones
        }
        for milestone_id in topological_order(
            dependencies,
            label="ProjectMilestone dependency graph",
        ):
            self.milestones.achieve_if_ready(
                definition.milestone_index[milestone_id],
                definition=definition,
                runtime=runtime,
                task_port=self.task_port,
                journal=self.journal,
            )

    def _eligibility(
        self,
        definition: ProjectExecutionDefinition,
        runtime: ProjectExecutionRuntimeState,
    ) -> dict[str, EligibilityResult]:
        return {
            task.task_id: evaluate_task_eligibility(
                task.task_id,
                definition=definition,
                runtime=runtime,
                task_port=self.task_port,
            )
            for task in definition.tasks
        }

    def _record_ready_tasks(
        self,
        runtime: ProjectExecutionRuntimeState,
        eligibility: dict[str, EligibilityResult],
    ) -> None:
        previous = set(runtime.executor.get("ready_tasks", []))
        ready = {
            task_id for task_id, result in eligibility.items() if result.eligible
        }
        for task_id in sorted(ready - previous):
            self._append_event("project_task_ready", {"task_id": task_id})
        runtime.executor["ready_tasks"] = sorted(ready)

    def _observe_tasks(
        self,
        definition: ProjectExecutionDefinition,
        runtime: ProjectExecutionRuntimeState,
    ) -> None:
        previous = runtime.observed_tasks
        observed: dict[str, dict[str, Any]] = {}
        for task in definition.tasks:
            summary = self.task_port.describe(task.task_id)
            observed[task.task_id] = {
                "outcome": summary.outcome.value,
                "execution_state": summary.execution_state,
            }
            prior_outcome = str((previous.get(task.task_id) or {}).get("outcome", ""))
            if prior_outcome == summary.outcome.value:
                continue
            if summary.outcome == TaskOutcome.COMPLETED:
                self._append_event(
                    "project_task_completed",
                    {"task_id": task.task_id},
                )
            elif summary.outcome == TaskOutcome.FAILED:
                self._append_event(
                    "project_task_failed",
                    {"task_id": task.task_id},
                )
        runtime.observed_tasks = observed

    def _project_phases(
        self,
        definition: ProjectExecutionDefinition,
        runtime: ProjectExecutionRuntimeState,
        eligibility: dict[str, EligibilityResult],
    ) -> dict[str, PhaseProjection]:
        return {
            phase.id: project_phase(
                phase,
                definition=definition,
                runtime=runtime,
                task_port=self.task_port,
                eligibility=eligibility,
            )
            for phase in definition.phases
        }

    def _record_phase_transitions(
        self,
        runtime: ProjectExecutionRuntimeState,
        phases: dict[str, PhaseProjection],
    ) -> None:
        old_states = runtime.executor.get("phase_states", {})
        if not isinstance(old_states, dict):
            old_states = {}
        new_states = {
            phase_id: {
                "state": projection.state.value,
                "health": projection.health.value,
            }
            for phase_id, projection in phases.items()
        }
        for phase_id, row in new_states.items():
            previous = old_states.get(phase_id) or {}
            previous_state = (
                previous.get("state") if isinstance(previous, dict) else None
            )
            if previous_state == row["state"]:
                continue
            event_type = _PHASE_EVENT.get(row["state"])
            if event_type:
                self._append_event(event_type, {"phase_id": phase_id})
        runtime.executor["phase_states"] = new_states

    def _start_with_intent(
        self,
        task_id: str,
        runtime: ProjectExecutionRuntimeState,
        *,
        retry_of: dict[str, Any] | None = None,
    ) -> TaskStartResult:
        intent_id = f"start-{task_id}-{uuid.uuid4().hex[:12]}"
        intent: dict[str, Any] = {
            "id": intent_id,
            "kind": "start_task",
            "task_id": task_id,
            "status": "pending",
            "created_at": _now(),
        }
        if retry_of is not None:
            intent["retry_of"] = retry_of.get("id", "")
            retry_of["status"] = "superseded"
            retry_of["superseded_by"] = intent_id

        runtime.intents[intent_id] = intent
        self.runtime_repository.save(runtime)
        self._append_event(
            "project_task_start_requested",
            {"task_id": task_id, "intent_id": intent_id},
        )

        try:
            result = self.task_port.start(task_id)
        except Exception as exc:
            result = self._reconcile_start_exception(
                task_id,
                intent,
                runtime,
                exc,
            )
        else:
            self._resolve_start_result(task_id, intent, result)

        self.runtime_repository.save(runtime)
        if intent["status"] == "resolved":
            self._append_event(
                "project_task_started",
                {
                    "task_id": task_id,
                    "intent_id": intent_id,
                    "observed_outcome": intent.get("observed_outcome", ""),
                },
            )
        return result

    def _reconcile_start_exception(
        self,
        task_id: str,
        intent: dict[str, Any],
        runtime: ProjectExecutionRuntimeState,
        error: Exception,
    ) -> TaskStartResult:
        # The Task action may have crossed its durable commit point before the
        # call failed. Observation, never blind replay, decides the outcome.
        outcome = self.task_port.outcome(task_id)
        if outcome != TaskOutcome.NOT_STARTED:
            intent.update(
                {
                    "status": "resolved",
                    "resolved_at": _now(),
                    "observed_outcome": outcome.value,
                    "action_error": str(error),
                }
            )
            return TaskStartResult(
                True,
                already_started=True,
                message=f"Task start outcome recovered after error: {error}",
            )

        intent.update(
            {
                "status": "uncertain",
                "action_error": str(error),
                "reconciled_at": _now(),
            }
        )
        self.runtime_repository.save(runtime)
        raise error

    def _resolve_start_result(
        self,
        task_id: str,
        intent: dict[str, Any],
        result: TaskStartResult,
    ) -> None:
        observed = self.task_port.outcome(task_id)
        if observed != TaskOutcome.NOT_STARTED:
            intent.update(
                {
                    "status": "resolved",
                    "resolved_at": _now(),
                    "observed_outcome": observed.value,
                }
            )
            return
        if result.accepted or result.already_started:
            # The action was acknowledged, but the canonical Task durable state
            # has not caught up yet. Keep the intent unresolved so Automatic
            # capacity remains reserved and no later cycle can duplicate it.
            intent.update(
                {
                    "status": "pending",
                    "action_acknowledged_at": _now(),
                    "message": result.message,
                }
            )
            return
        intent.update(
            {
                "status": "failed",
                "resolved_at": _now(),
                "message": result.message,
            }
        )

    @staticmethod
    def _pending_start_intent(
        runtime: ProjectExecutionRuntimeState,
        task_id: str,
    ) -> dict[str, Any] | None:
        return next(
            (
                value
                for value in runtime.intents.values()
                if value.get("kind") == "start_task"
                and value.get("task_id") == task_id
                and value.get("status") not in _RESOLVED_INTENT_STATES
            ),
            None,
        )

    def _sync_definition(
        self,
        runtime: ProjectExecutionRuntimeState,
        definition: ProjectExecutionDefinition,
    ) -> None:
        digest = definition_digest(definition)
        changed = (
            runtime.definition_revision != definition.revision
            or runtime.definition_digest != digest
            or runtime.mode != definition.mode.value
        )
        if not changed:
            return

        previous_digest = runtime.definition_digest
        runtime.definition_revision = definition.revision
        runtime.definition_digest = digest
        runtime.mode = definition.mode.value
        self._append_event(
            "project_definition_changed",
            {
                "definition_revision": definition.revision,
                "definition_digest": digest,
                "previous_digest": previous_digest,
            },
        )

    def _append_event(self, event_type: str, data: dict[str, Any]) -> None:
        if self.journal is not None:
            self.journal.append(event_type, data)

    def _executor_lock(self) -> FileLock:
        return FileLock(
            self.lock_path,
            level=LockLevel.PROJECT_EXECUTOR,
        )

    @staticmethod
    def _snapshot(
        definition: ProjectExecutionDefinition,
        runtime: ProjectExecutionRuntimeState,
        phases: dict[str, PhaseProjection],
        eligibility: dict[str, EligibilityResult],
    ) -> ProjectExecutionSnapshot:
        return ProjectExecutionSnapshot(
            definition_revision=definition.revision,
            definition_digest=runtime.definition_digest,
            mode=definition.mode.value,
            held=runtime.held,
            gates={key: dict(value) for key, value in runtime.gates.items()},
            milestones={
                key: dict(value)
                for key, value in runtime.milestone_achievements.items()
            },
            phases=phases,
            eligibility=eligibility,
            task_outcomes={
                task_id: str(row.get("outcome", "unknown"))
                for task_id, row in runtime.observed_tasks.items()
            },
        )
