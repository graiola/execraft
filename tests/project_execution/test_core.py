from __future__ import annotations

from dataclasses import replace
import json

import pytest

from execraft.persistence.locks import FileLock, LockHierarchyError, LockLevel
from execraft.project_execution.eligibility import evaluate_task_eligibility
from execraft.project_execution.engine import ProjectExecutionEngine
from execraft.project_execution.events import ProjectEventJournal
from execraft.project_execution.gates import GateEvaluationService
from execraft.project_execution.milestones import MilestoneAchievementService
from execraft.project_execution.models import (
    DeliveryPolicy,
    ExecutionMode,
    GateCriterion,
    MilestoneRequirements,
    PhaseState,
    ProjectExecutionConflictError,
    ProjectExecutionDefinition,
    ProjectExecutionError,
    ProjectExecutionPolicy,
    ProjectGate,
    ProjectMilestone,
    ProjectPhase,
    ProjectTask,
    TaskRequirements,
)
from execraft.project_execution.repository import ProjectExecutionRepository
from execraft.project_execution.runtime_repository import (
    ProjectExecutionRuntimeState,
    ProjectRuntimeRepository,
)
from execraft.project_execution.service import ProjectExecutionService
from execraft.project_execution.task_port import (
    TaskActionResult,
    TaskEvidence,
    TaskExecutionSummary,
    TaskOutcome,
    TaskStartResult,
    TaskVerificationSummary,
)
from execraft.project_execution.validation import validate_definition


class FakePort:
    def __init__(
        self,
        outcomes=None,
        verification=None,
        *,
        revisions: bool = True,
        fail_start_after_side_effect: bool = False,
    ) -> None:
        self.outcomes = dict(outcomes or {})
        self.verification = dict(verification or {})
        self.started: list[str] = []
        self.revisions = revisions
        self.fail_start_after_side_effect = fail_start_after_side_effect

    def describe(self, task_id: str) -> TaskExecutionSummary:
        outcome = self.outcomes.get(task_id, TaskOutcome.NOT_STARTED)
        return TaskExecutionSummary(
            task_id=task_id,
            exists=True,
            active=True,
            executable=True,
            outcome=outcome,
            execution_state=outcome.value,
            title=task_id,
        )

    def start(self, task_id: str) -> TaskStartResult:
        self.started.append(task_id)
        self.outcomes[task_id] = TaskOutcome.RUNNING
        if self.fail_start_after_side_effect:
            raise RuntimeError("lost response after Task start")
        return TaskStartResult(True)

    def pause(self, task_id: str) -> TaskActionResult:
        return TaskActionResult(True)

    def resume(self, task_id: str) -> TaskActionResult:
        return TaskActionResult(True)

    def outcome(self, task_id: str) -> TaskOutcome:
        return self.outcomes.get(task_id, TaskOutcome.NOT_STARTED)

    def evidence(self, task_id: str) -> TaskEvidence:
        outcome = self.outcome(task_id)
        verification = self.verification.get(task_id, "passed")
        return TaskEvidence(
            task_id=task_id,
            outcome=outcome,
            task_digest=f"sha256:task-{task_id}",
            plan_digest=f"sha256:plan-{task_id}",
            verification=TaskVerificationSummary(
                verification,
                1 if verification == "passed" else 0,
                1 if verification == "failed" else 0,
                1,
            ),
            repository_revisions=(
                {"repo": f"commit-{task_id}"} if self.revisions else {}
            ),
            artifacts=(f"artifact-{task_id}",),
        )


def definition(mode: ExecutionMode = ExecutionMode.ASSISTED):
    return ProjectExecutionDefinition(
        project="sample",
        mode=mode,
        phases=(
            ProjectPhase(
                "phase",
                "Phase",
                tasks=("a", "b"),
                exit_gates=("accepted",),
                milestones=("mvp",),
            ),
        ),
        tasks=(
            ProjectTask("a", "phase"),
            ProjectTask(
                "b",
                "phase",
                requires=TaskRequirements(
                    tasks=("a",),
                    gates=("accepted",),
                ),
            ),
        ),
        gates=(
            ProjectGate(
                "accepted",
                "Accepted",
                criteria=(
                    GateCriterion("task_completion", task_id="a"),
                    GateCriterion("human_approval"),
                ),
            ),
        ),
        milestones=(
            ProjectMilestone(
                "mvp",
                "MVP",
                requires=MilestoneRequirements(
                    tasks=("a",),
                    gates=("accepted",),
                ),
                delivery_policy=DeliveryPolicy.CANDIDATE,
            ),
        ),
    )


def _engine(tmp_path, definition_value, port):
    definitions = ProjectExecutionRepository(tmp_path / "project")
    definitions.project_directory.mkdir()
    definitions.create(definition_value)
    runtime = ProjectRuntimeRepository(tmp_path / "state", "sample")
    journal = ProjectEventJournal(tmp_path / "state", "sample")
    return (
        ProjectExecutionEngine(
            definition_repository=definitions,
            runtime_repository=runtime,
            task_port=port,
            journal=journal,
        ),
        definitions,
        runtime,
        journal,
    )


def test_validation_rejects_cycles_missing_refs_and_entry_self_dependency():
    cyclic = ProjectExecutionDefinition(
        project="sample",
        phases=(ProjectPhase("p", "P", tasks=("a", "b")),),
        tasks=(
            ProjectTask("a", "p", requires=TaskRequirements(tasks=("b",))),
            ProjectTask("b", "p", requires=TaskRequirements(tasks=("a",))),
        ),
    )
    with pytest.raises(ProjectExecutionError, match="acyclic"):
        validate_definition(cyclic)

    self_blocking = ProjectExecutionDefinition(
        project="sample",
        phases=(ProjectPhase("p", "P", tasks=("a",), entry_gates=("g",)),),
        tasks=(ProjectTask("a", "p"),),
        gates=(
            ProjectGate(
                "g",
                "G",
                criteria=(GateCriterion("task_completion", task_id="a"),),
            ),
        ),
    )
    with pytest.raises(ProjectExecutionError, match="entry Gate"):
        validate_definition(self_blocking)

    with pytest.raises(ProjectExecutionError, match="unknown project gate evaluator"):
        GateCriterion("python_expression")


def test_task_requirements_cannot_turn_milestones_into_gates():
    with pytest.raises(ProjectExecutionError, match="unsupported task prerequisite"):
        TaskRequirements.from_mapping({"milestones": ["m1"]})


def test_gate_human_approval_is_bound_to_current_evidence_fingerprint():
    project = definition()
    port = FakePort({"a": TaskOutcome.COMPLETED})
    runtime = ProjectExecutionRuntimeState("sample")
    service = GateEvaluationService()
    gate = project.gate_index["accepted"]

    first = service.evaluate(
        gate,
        definition=project,
        runtime=runtime,
        task_port=port,
    )
    assert first["state"] == "awaiting_decision"
    first_fingerprint = first["input_fingerprint"]

    service.decide(
        "accepted",
        runtime=runtime,
        actor="operator",
        decision="approved",
    )
    assert service.evaluate(
        gate,
        definition=project,
        runtime=runtime,
        task_port=port,
    )["state"] == "passed"

    port.outcomes["a"] = TaskOutcome.FAILED
    changed = service.evaluate(
        gate,
        definition=project,
        runtime=runtime,
        task_port=port,
    )
    assert changed["input_fingerprint"] != first_fingerprint
    assert changed["state"] == "failed"

    # Restoring byte-equivalent evidence restores the exact prior approval.
    port.outcomes["a"] = TaskOutcome.COMPLETED
    assert service.evaluate(
        gate,
        definition=project,
        runtime=runtime,
        task_port=port,
    )["state"] == "passed"


def test_gate_control_definition_change_invalidates_human_approval():
    project = definition()
    port = FakePort({"a": TaskOutcome.COMPLETED})
    runtime = ProjectExecutionRuntimeState("sample")
    service = GateEvaluationService()
    original = project.gate_index["accepted"]

    service.evaluate(
        original,
        definition=project,
        runtime=runtime,
        task_port=port,
    )
    service.decide(
        original.id,
        runtime=runtime,
        actor="operator",
        decision="approved",
    )
    original_fingerprint = runtime.gates[original.id]["input_fingerprint"]

    changed_gate = replace(
        original,
        criteria=(
            GateCriterion("task_completion", task_id="a"),
            GateCriterion("task_artifact", task_id="a", artifact_id="artifact-a"),
            GateCriterion("human_approval"),
        ),
    )
    changed_project = replace(project, gates=(changed_gate,))
    row = service.evaluate(
        changed_gate,
        definition=changed_project,
        runtime=runtime,
        task_port=port,
    )
    assert row["input_fingerprint"] != original_fingerprint
    assert row["state"] == "awaiting_decision"


def test_waiver_is_distinct_from_pass_and_stales_with_evidence():
    project = definition()
    port = FakePort({"a": TaskOutcome.COMPLETED})
    runtime = ProjectExecutionRuntimeState("sample")
    service = GateEvaluationService()
    gate = project.gate_index["accepted"]

    service.evaluate(gate, definition=project, runtime=runtime, task_port=port)
    service.waive(
        "accepted",
        runtime=runtime,
        actor="lead",
        reason="accepted risk",
    )
    assert service.evaluate(
        gate,
        definition=project,
        runtime=runtime,
        task_port=port,
    )["state"] == "waived"

    port.outcomes["a"] = TaskOutcome.FAILED
    assert service.evaluate(
        gate,
        definition=project,
        runtime=runtime,
        task_port=port,
    )["state"] == "failed"


def test_structured_eligibility_and_phase_projection():
    project = definition()
    port = FakePort({"a": TaskOutcome.NOT_STARTED})
    runtime = ProjectExecutionRuntimeState("sample")

    blocked = evaluate_task_eligibility(
        "b",
        definition=project,
        runtime=runtime,
        task_port=port,
    )
    assert not blocked.eligible
    assert {reason.kind.value for reason in blocked.reasons} >= {
        "predecessor_unsatisfied",
        "gate_unsatisfied",
    }

    from execraft.project_execution.phases import project_phase

    projected = project_phase(
        project.phases[0],
        definition=project,
        runtime=runtime,
        task_port=port,
        eligibility={
            "a": evaluate_task_eligibility(
                "a",
                definition=project,
                runtime=runtime,
                task_port=port,
            ),
            "b": blocked,
        },
    )
    assert projected.state == PhaseState.READY


def test_optional_task_does_not_block_phase_completion():
    project = ProjectExecutionDefinition(
        project="sample",
        phases=(ProjectPhase("p", "P", tasks=("required", "optional")),),
        tasks=(
            ProjectTask("required", "p"),
            ProjectTask("optional", "p", required=False),
        ),
    )
    port = FakePort(
        {
            "required": TaskOutcome.COMPLETED,
            "optional": TaskOutcome.NOT_STARTED,
        }
    )
    runtime = ProjectExecutionRuntimeState("sample")
    eligibility = {
        task.task_id: evaluate_task_eligibility(
            task.task_id,
            definition=project,
            runtime=runtime,
            task_port=port,
        )
        for task in project.tasks
    }

    from execraft.project_execution.phases import project_phase

    assert project_phase(
        project.phases[0],
        definition=project,
        runtime=runtime,
        task_port=port,
        eligibility=eligibility,
    ).state == PhaseState.COMPLETE


def test_milestone_achievement_is_immutable_and_requires_candidate_revisions():
    project = definition()
    port = FakePort({"a": TaskOutcome.COMPLETED})
    runtime = ProjectExecutionRuntimeState("sample")
    gates = GateEvaluationService()
    gate = project.gate_index["accepted"]

    gates.evaluate(gate, definition=project, runtime=runtime, task_port=port)
    gates.decide(
        "accepted",
        runtime=runtime,
        actor="op",
        decision="approved",
    )
    gates.evaluate(gate, definition=project, runtime=runtime, task_port=port)

    service = MilestoneAchievementService()
    result = service.achieve_if_ready(
        project.milestone_index["mvp"],
        definition=project,
        runtime=runtime,
        task_port=port,
    )
    assert result and result.complete
    baseline = json.loads(json.dumps(runtime.milestone_achievements["mvp"]))

    port.outcomes["a"] = TaskOutcome.FAILED
    assert service.achieve_if_ready(
        project.milestone_index["mvp"],
        definition=project,
        runtime=runtime,
        task_port=port,
    ) is None
    assert runtime.milestone_achievements["mvp"] == baseline

    no_revisions = FakePort({"a": TaskOutcome.COMPLETED}, revisions=False)
    runtime_without_revisions = ProjectExecutionRuntimeState("sample")
    gates.evaluate(
        gate,
        definition=project,
        runtime=runtime_without_revisions,
        task_port=no_revisions,
    )
    gates.waive(
        "accepted",
        runtime=runtime_without_revisions,
        actor="op",
        reason="test",
    )
    incomplete = service.achieve_if_ready(
        project.milestone_index["mvp"],
        definition=project,
        runtime=runtime_without_revisions,
        task_port=no_revisions,
    )
    assert incomplete and not incomplete.complete
    assert any(
        issue.kind == "missing_repository_revision"
        for issue in incomplete.issues
    )


def test_repository_revision_conflict_and_symlink_rejection(tmp_path):
    repository = ProjectExecutionRepository(tmp_path)
    created = repository.create(ProjectExecutionDefinition(project="sample"))
    assert created.revision == 1

    saved = repository.save(created, expected_revision=1)
    assert saved.revision == 2
    with pytest.raises(ProjectExecutionConflictError):
        repository.save(saved, expected_revision=1)

    repository.path.unlink()
    target = tmp_path / "other"
    target.write_text("x", encoding="utf-8")
    repository.path.symlink_to(target)
    with pytest.raises(ProjectExecutionError):
        repository.load()


def test_service_refuses_to_delete_gate_or_milestone_needed_by_history(tmp_path):
    definitions = ProjectExecutionRepository(tmp_path / "project")
    definitions.project_directory.mkdir()
    definitions.create(definition())
    runtime_repository = ProjectRuntimeRepository(tmp_path / "state", "sample")
    runtime = runtime_repository.load()
    runtime.gates["accepted"] = {
        "state": "passed",
        "input_fingerprint": "sha256:gate",
        "evaluation": {"input_fingerprint": "sha256:gate"},
    }
    runtime.milestone_achievements["mvp"] = {
        "gates": {"accepted": {"input_fingerprint": "sha256:gate"}}
    }
    runtime_repository.save(runtime)
    service = ProjectExecutionService(
        definitions,
        runtime_repository=runtime_repository,
    )

    current = service.get()
    # Remove definition references first; runtime history must still block identity deletion.
    phase = replace(current.phases[0], exit_gates=(), milestones=())
    task_b = replace(current.task_index["b"], requires=TaskRequirements(tasks=("a",)))
    milestone = replace(current.milestone_index["mvp"], requires=MilestoneRequirements(tasks=("a",)))
    current = definitions.save(
        replace(current, phases=(phase,), tasks=(current.task_index["a"], task_b), milestones=(milestone,)),
        expected_revision=current.revision,
    )

    with pytest.raises(ProjectExecutionError, match="history|baseline"):
        service.delete_gate("accepted", expected_revision=current.revision)
    with pytest.raises(ProjectExecutionError, match="immutable achievement"):
        service.delete_milestone("mvp", expected_revision=current.revision)


def test_engine_observe_never_starts_and_assisted_start_is_intent_backed(tmp_path):
    project = replace(definition(), mode=ExecutionMode.OBSERVE)
    port = FakePort({"a": TaskOutcome.NOT_STARTED})
    engine, definitions, runtime, _journal = _engine(tmp_path, project, port)

    engine.reconcile()
    assert port.started == []
    with pytest.raises(ProjectExecutionError, match="Observe"):
        engine.start_task("a")

    current = definitions.load()
    definitions.save(
        replace(current, mode=ExecutionMode.ASSISTED),
        expected_revision=current.revision,
    )
    result = engine.start_task("a")
    assert result.accepted
    assert port.started == ["a"]
    intents = list(runtime.load().intents.values())
    assert intents and intents[-1]["status"] == "resolved"


def test_start_error_after_task_commit_is_reconciled_without_duplicate(tmp_path):
    project = ProjectExecutionDefinition(
        project="sample",
        mode=ExecutionMode.ASSISTED,
        phases=(ProjectPhase("p", "P", tasks=("a",)),),
        tasks=(ProjectTask("a", "p"),),
    )
    port = FakePort(fail_start_after_side_effect=True)
    engine, _definitions, runtime, _journal = _engine(tmp_path, project, port)

    result = engine.start_task("a")
    assert result.accepted and result.already_started
    assert port.started == ["a"]
    intent = next(iter(runtime.load().intents.values()))
    assert intent["status"] == "resolved"
    assert intent["observed_outcome"] == "running"


def test_automatic_mode_reconcile_remains_observational(tmp_path):
    project = ProjectExecutionDefinition(
        project="sample",
        mode=ExecutionMode.AUTOMATIC,
        policy=ProjectExecutionPolicy(maximum_parallel_tasks=1),
        phases=(ProjectPhase("p", "P", tasks=("a", "b")),),
        tasks=(ProjectTask("a", "p"), ProjectTask("b", "p")),
    )
    port = FakePort()
    engine, _definitions, _runtime, _journal = _engine(tmp_path, project, port)

    snapshot = engine.reconcile()
    assert port.started == []
    assert {
        task_id
        for task_id, result in snapshot.eligibility.items()
        if result.eligible
    } == {"a", "b"}


def test_project_executor_lock_can_nest_task_driver_but_not_inverse(tmp_path):
    with FileLock(tmp_path / "project.lock", level=LockLevel.PROJECT_EXECUTOR):
        with FileLock(tmp_path / "driver.lock", level=LockLevel.DRIVER):
            pass

    with FileLock(tmp_path / "driver-2.lock", level=LockLevel.DRIVER):
        with pytest.raises(LockHierarchyError, match="order violation"):
            FileLock(
                tmp_path / "project-2.lock",
                level=LockLevel.PROJECT_EXECUTOR,
            ).acquire()


def test_project_journal_tolerates_crash_truncated_tail(tmp_path):
    journal = ProjectEventJournal(tmp_path, "sample")
    journal.append("project_execution_started", {"mode": "observe"})
    with journal.path.open("ab") as handle:
        handle.write(b'{"type":"project_task_ready"')
    assert [row["type"] for row in journal.read()] == ["project_execution_started"]
