from __future__ import annotations

from dataclasses import replace

import pytest

from execraft.project_execution.engine import ProjectExecutionEngine
from execraft.project_execution.events import ProjectEventJournal
from execraft.project_execution.models import (
    ExecutionMode,
    GateCriterion,
    ProjectExecutionDefinition,
    ProjectExecutionError,
    ProjectExecutionPolicy,
    ProjectGate,
    ProjectPhase,
    ProjectTask,
    TaskFailureBehavior,
    TaskRequirements,
)
from execraft.project_execution.repository import ProjectExecutionRepository
from execraft.project_execution.runner import AutomaticRunnerConfig, run_automatic
from execraft.project_execution.runtime_repository import ProjectRuntimeRepository
from execraft.project_execution.task_port import (
    TaskActionResult,
    TaskEvidence,
    TaskExecutionSummary,
    TaskOutcome,
    TaskStartResult,
    TaskVerificationSummary,
)


class FakePort:
    def __init__(self, outcomes=None, *, fail_without_side_effect: str = "") -> None:
        self.outcomes = dict(outcomes or {})
        self.started: list[str] = []
        self.paused: list[str] = []
        self.fail_without_side_effect = fail_without_side_effect

    def describe(self, task_id: str) -> TaskExecutionSummary:
        outcome = self.outcomes.get(task_id, TaskOutcome.NOT_STARTED)
        return TaskExecutionSummary(
            task_id=task_id,
            exists=True,
            active=True,
            executable=True,
            execution_state=outcome.value,
            outcome=outcome,
            title=task_id,
        )

    def start(self, task_id: str) -> TaskStartResult:
        self.started.append(task_id)
        if self.fail_without_side_effect == task_id:
            raise RuntimeError("launch transport failed")
        self.outcomes[task_id] = TaskOutcome.RUNNING
        return TaskStartResult(True, message=f"started {task_id}")

    def pause(self, task_id: str) -> TaskActionResult:
        self.paused.append(task_id)
        return TaskActionResult(True)

    def resume(self, task_id: str) -> TaskActionResult:
        return TaskActionResult(True)

    def outcome(self, task_id: str) -> TaskOutcome:
        return self.outcomes.get(task_id, TaskOutcome.NOT_STARTED)

    def evidence(self, task_id: str) -> TaskEvidence:
        return TaskEvidence(
            task_id=task_id,
            outcome=self.outcome(task_id),
            task_digest=f"sha256:task-{task_id}",
            plan_digest=f"sha256:plan-{task_id}",
            verification=TaskVerificationSummary("passed", 1, 0, 1),
        )


def _definition(
    *,
    policy: ProjectExecutionPolicy | None = None,
    tasks: tuple[ProjectTask, ...] | None = None,
    phases: tuple[ProjectPhase, ...] | None = None,
    gates: tuple[ProjectGate, ...] = (),
) -> ProjectExecutionDefinition:
    tasks = tasks or (
        ProjectTask("a", "p1"),
        ProjectTask("b", "p1"),
        ProjectTask("c", "p2"),
    )
    phases = phases or (
        ProjectPhase("p1", "Phase 1", tasks=("a", "b")),
        ProjectPhase("p2", "Phase 2", tasks=("c",)),
    )
    return ProjectExecutionDefinition(
        project="sample",
        mode=ExecutionMode.AUTOMATIC,
        policy=policy or ProjectExecutionPolicy(),
        phases=phases,
        tasks=tasks,
        gates=gates,
    )


def _engine(tmp_path, definition: ProjectExecutionDefinition, port: FakePort):
    definitions = ProjectExecutionRepository(tmp_path / "project")
    definitions.project_directory.mkdir(parents=True)
    definitions.create(definition)
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


def test_policy_serializes_new_vocabulary_and_reads_p10_legacy_flag():
    legacy_stop = ProjectExecutionPolicy.from_mapping(
        {"maximum_parallel_tasks": 3, "stop_on_task_failure": True}
    )
    assert legacy_stop.task_failure_behavior == TaskFailureBehavior.STOP_NEW
    assert legacy_stop.maximum_parallel_tasks_per_phase == 3
    assert "stop_on_task_failure" not in legacy_stop.as_mapping()

    legacy_continue = ProjectExecutionPolicy.from_mapping(
        {"stop_on_task_failure": False}
    )
    assert legacy_continue.task_failure_behavior == TaskFailureBehavior.CONTINUE

    with pytest.raises(ProjectExecutionError, match="conflicts"):
        ProjectExecutionPolicy.from_mapping(
            {
                "task_failure_behavior": "continue",
                "stop_on_task_failure": True,
            }
        )


def test_reconcile_remains_observational_in_automatic_mode(tmp_path):
    port = FakePort()
    engine, *_ = _engine(tmp_path, _definition(), port)

    snapshot = engine.reconcile()

    assert port.started == []
    assert snapshot.mode == "automatic"
    assert {task_id for task_id, row in snapshot.eligibility.items() if row.eligible} == {
        "a",
        "b",
        "c",
    }
    with pytest.raises(ProjectExecutionError, match="automatic cycle"):
        engine.start_task("a")


def test_automatic_cycle_enforces_global_and_per_phase_limits(tmp_path):
    policy = ProjectExecutionPolicy(
        maximum_parallel_tasks=3,
        maximum_parallel_tasks_per_phase=2,
        maximum_active_phases=2,
    )
    port = FakePort()
    engine, *_ = _engine(tmp_path, _definition(policy=policy), port)

    cycle = engine.automatic_cycle()

    assert cycle.started_tasks == ("a", "b", "c")
    assert set(port.started) == {"a", "b", "c"}
    assert cycle.plan.capacity.global_limit == 3


def test_automatic_cycle_limits_simultaneously_active_phases(tmp_path):
    policy = ProjectExecutionPolicy(
        maximum_parallel_tasks=3,
        maximum_parallel_tasks_per_phase=3,
        maximum_active_phases=1,
    )
    port = FakePort()
    engine, *_ = _engine(tmp_path, _definition(policy=policy), port)

    cycle = engine.automatic_cycle()

    assert cycle.started_tasks == ("a", "b")
    assert cycle.plan.policy_blocked["c"] == "maximum_active_phases would be exceeded"


def test_existing_running_tasks_consume_automatic_capacity(tmp_path):
    policy = ProjectExecutionPolicy(
        maximum_parallel_tasks=2,
        maximum_parallel_tasks_per_phase=2,
        maximum_active_phases=2,
    )
    port = FakePort({"a": TaskOutcome.RUNNING})
    engine, *_ = _engine(tmp_path, _definition(policy=policy), port)

    cycle = engine.automatic_cycle()

    assert cycle.started_tasks == ("b",)
    assert cycle.plan.capacity.occupied_global == 1
    assert port.started == ["b"]


def test_uncertain_start_intent_reserves_capacity_and_is_not_replayed(tmp_path):
    policy = ProjectExecutionPolicy(
        maximum_parallel_tasks=1,
        maximum_parallel_tasks_per_phase=1,
        maximum_active_phases=1,
    )
    port = FakePort(fail_without_side_effect="a")
    engine, _definitions, runtime_repo, _journal = _engine(
        tmp_path, _definition(policy=policy), port
    )

    first = engine.automatic_cycle()
    assert first.started_tasks == ()
    assert first.start_failures[0]["task_id"] == "a"
    state = runtime_repo.load()
    intent = next(iter(state.intents.values()))
    assert intent["status"] == "uncertain"

    port.fail_without_side_effect = ""
    second = engine.automatic_cycle()
    assert port.started == ["a"]
    assert second.started_tasks == ()
    assert second.plan.reserved_tasks == ("a",)
    assert second.plan.capacity.available_global == 0


def test_human_gate_is_a_hard_automatic_boundary_until_decision(tmp_path):
    gate = ProjectGate(
        "approve",
        "Approve",
        criteria=(GateCriterion("human_approval"),),
    )
    tasks = (ProjectTask("a", "p1", requires=TaskRequirements(gates=("approve",))),)
    phases = (ProjectPhase("p1", "Phase 1", tasks=("a",)),)
    port = FakePort()
    engine, *_ = _engine(
        tmp_path,
        _definition(tasks=tasks, phases=phases, gates=(gate,)),
        port,
    )

    before = engine.automatic_cycle()
    assert before.started_tasks == ()
    assert before.snapshot.gates["approve"]["state"] == "awaiting_decision"

    engine.decide_gate("approve", actor="operator", decision="approved")
    after = engine.automatic_cycle()
    assert after.started_tasks == ("a",)


def test_stop_new_failure_policy_blocks_unrelated_new_tasks(tmp_path):
    port = FakePort({"a": TaskOutcome.FAILED})
    engine, *_ = _engine(
        tmp_path,
        _definition(
            policy=ProjectExecutionPolicy(
                maximum_parallel_tasks=2,
                maximum_parallel_tasks_per_phase=2,
                maximum_active_phases=2,
                task_failure_behavior=TaskFailureBehavior.STOP_NEW,
            )
        ),
        port,
    )

    cycle = engine.automatic_cycle()

    assert cycle.started_tasks == ()
    assert port.paused == []
    assert any(
        reason.kind.value == "task_failure_policy"
        for reason in cycle.snapshot.eligibility["c"].reasons
    )


def test_continue_failure_policy_allows_unrelated_tasks_only(tmp_path):
    tasks = (
        ProjectTask("a", "p1"),
        ProjectTask("b", "p1", requires=TaskRequirements(tasks=("a",))),
        ProjectTask("c", "p2"),
    )
    port = FakePort({"a": TaskOutcome.FAILED})
    engine, *_ = _engine(
        tmp_path,
        _definition(
            tasks=tasks,
            policy=ProjectExecutionPolicy(
                maximum_parallel_tasks=2,
                maximum_parallel_tasks_per_phase=2,
                maximum_active_phases=2,
                task_failure_behavior=TaskFailureBehavior.CONTINUE,
            ),
        ),
        port,
    )

    cycle = engine.automatic_cycle()

    assert cycle.started_tasks == ("c",)
    assert any(
        reason.kind.value == "predecessor_unsatisfied"
        for reason in cycle.snapshot.eligibility["b"].reasons
    )


def test_hold_failure_policy_holds_project_but_never_pauses_running_task(tmp_path):
    port = FakePort({"a": TaskOutcome.FAILED, "b": TaskOutcome.RUNNING})
    engine, _definitions, runtime_repo, journal = _engine(
        tmp_path,
        _definition(
            policy=ProjectExecutionPolicy(
                maximum_parallel_tasks=3,
                maximum_parallel_tasks_per_phase=3,
                maximum_active_phases=2,
                task_failure_behavior=TaskFailureBehavior.HOLD,
            )
        ),
        port,
    )

    cycle = engine.automatic_cycle()

    assert cycle.snapshot.held is True
    assert runtime_repo.load().hold_reason.startswith("automatic failure policy hold")
    assert port.paused == []
    pause_events = [row for row in journal.read() if row["type"] == "project_execution_paused"]
    assert pause_events[-1]["source"] == "task_failure_policy"


def test_foreground_runner_progresses_after_running_task_completes(tmp_path):
    tasks = (
        ProjectTask("a", "p1"),
        ProjectTask("b", "p1", requires=TaskRequirements(tasks=("a",))),
    )
    phases = (ProjectPhase("p1", "Phase 1", tasks=("a", "b")),)
    port = FakePort()
    engine, *_ = _engine(
        tmp_path,
        _definition(
            tasks=tasks,
            phases=phases,
            policy=ProjectExecutionPolicy(
                maximum_parallel_tasks=1,
                maximum_parallel_tasks_per_phase=1,
                maximum_active_phases=1,
            ),
        ),
        port,
    )

    sleep_calls = 0

    def complete_between_cycles(_seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if port.outcomes.get("a") == TaskOutcome.RUNNING:
            port.outcomes["a"] = TaskOutcome.COMPLETED
        elif port.outcomes.get("b") == TaskOutcome.RUNNING:
            port.outcomes["b"] = TaskOutcome.COMPLETED

    result = run_automatic(
        engine,
        config=AutomaticRunnerConfig(poll_seconds=0, max_cycles=5),
        sleep=complete_between_cycles,
    )

    assert result.tasks_terminal is True
    assert result.tasks_successful is True
    assert result.cycles == 3
    assert port.started == ["a", "b"]
    assert sleep_calls == 2


def test_policy_rejects_unbounded_or_unknown_automatic_configuration():
    with pytest.raises(ProjectExecutionError, match="maximum_parallel_tasks"):
        ProjectExecutionPolicy(maximum_parallel_tasks=0)
    with pytest.raises(ProjectExecutionError, match="maximum_active_phases"):
        ProjectExecutionPolicy(maximum_active_phases=True)
    with pytest.raises(ProjectExecutionError, match="unsupported Project Execution policy"):
        ProjectExecutionPolicy.from_mapping({"agent_parallelism": 4})


def test_foreground_runner_reports_terminal_failure_without_calling_it_success(tmp_path):
    tasks = (ProjectTask("a", "p1"),)
    phases = (ProjectPhase("p1", "Phase 1", tasks=("a",)),)
    port = FakePort({"a": TaskOutcome.FAILED})
    engine, *_ = _engine(
        tmp_path,
        _definition(
            tasks=tasks,
            phases=phases,
            policy=ProjectExecutionPolicy(
                task_failure_behavior=TaskFailureBehavior.CONTINUE,
            ),
        ),
        port,
    )

    result = run_automatic(
        engine,
        config=AutomaticRunnerConfig(poll_seconds=0, max_cycles=1),
        sleep=lambda _seconds: None,
    )

    assert result.tasks_terminal is True
    assert result.tasks_successful is False
    assert result.held is False


def test_foreground_runner_stops_quiescent_at_human_gate(tmp_path):
    gate = ProjectGate(
        "approval",
        "Approval",
        criteria=(GateCriterion("human_approval"),),
    )
    tasks = (
        ProjectTask(
            "a",
            "p1",
            requires=TaskRequirements(gates=("approval",)),
        ),
    )
    phases = (ProjectPhase("p1", "Phase 1", tasks=("a",)),)
    port = FakePort()
    engine, *_ = _engine(
        tmp_path,
        _definition(tasks=tasks, phases=phases, gates=(gate,)),
        port,
    )

    result = run_automatic(
        engine,
        config=AutomaticRunnerConfig(poll_seconds=0, max_cycles=3),
        sleep=lambda _seconds: None,
    )

    assert result.quiescent is True
    assert result.tasks_terminal is False
    assert result.last_cycle.snapshot.gates["approval"]["state"] == "awaiting_decision"
    assert port.started == []


def test_concurrent_automatic_cycles_cannot_duplicate_start(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    entered = threading.Event()
    release = threading.Event()

    class BlockingPort(FakePort):
        def start(self, task_id: str) -> TaskStartResult:
            self.started.append(task_id)
            self.outcomes[task_id] = TaskOutcome.RUNNING
            entered.set()
            assert release.wait(timeout=5)
            return TaskStartResult(True, message=f"started {task_id}")

    tasks = (ProjectTask("a", "p1"),)
    phases = (ProjectPhase("p1", "Phase 1", tasks=("a",)),)
    port = BlockingPort()
    engine1, definitions, runtime, journal = _engine(
        tmp_path,
        _definition(tasks=tasks, phases=phases),
        port,
    )
    engine2 = ProjectExecutionEngine(
        definition_repository=definitions,
        runtime_repository=runtime,
        task_port=port,
        journal=journal,
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(engine1.automatic_cycle)
        assert entered.wait(timeout=5)
        second = pool.submit(engine2.automatic_cycle)
        release.set()
        first_result = first.result(timeout=5)
        second_result = second.result(timeout=5)

    assert first_result.started_tasks == ("a",)
    assert second_result.started_tasks == ()
    assert port.started == ["a"]


def test_acknowledged_start_without_observable_task_state_reserves_capacity(tmp_path):
    class AcknowledgingPort(FakePort):
        def start(self, task_id: str) -> TaskStartResult:
            self.started.append(task_id)
            return TaskStartResult(True, message="launch accepted asynchronously")

    policy = ProjectExecutionPolicy(
        maximum_parallel_tasks=1,
        maximum_parallel_tasks_per_phase=1,
        maximum_active_phases=1,
    )
    port = AcknowledgingPort()
    engine, _definitions, runtime_repo, _journal = _engine(
        tmp_path,
        _definition(policy=policy),
        port,
    )

    first = engine.automatic_cycle()
    assert first.started_tasks == ("a",)
    intent = next(iter(runtime_repo.load().intents.values()))
    assert intent["status"] == "pending"
    assert intent["action_acknowledged_at"]

    second = engine.automatic_cycle()
    assert port.started == ["a"]
    assert second.started_tasks == ()
    assert second.plan.reserved_tasks == ("a",)
    intent = next(iter(runtime_repo.load().intents.values()))
    assert intent["status"] == "uncertain"
