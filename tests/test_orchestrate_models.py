"""Tests for orchestrate domain models — state machine, validation, work packages."""

import pytest

from execraft.orchestrate.models import (
    AcceptanceCriterion,
    OrchestrateError,
    PlanGraph,
    TaskExecutionState,
    TaskExecutionStateRecord,
    WorkPackage,
    WorkPackageStage,
    validate_task_execution_transition,
    validate_stage_transition,
)


class TestTaskExecutionStateTransitions:
    def test_valid_initializing_to_validating_plan(self):
        validate_task_execution_transition(TaskExecutionState.INITIALIZING, TaskExecutionState.VALIDATING_PLAN)

    def test_valid_initializing_to_failed(self):
        validate_task_execution_transition(TaskExecutionState.INITIALIZING, TaskExecutionState.FAILED)

    def test_valid_initializing_to_cancelled(self):
        validate_task_execution_transition(TaskExecutionState.INITIALIZING, TaskExecutionState.CANCELLED)

    def test_invalid_initializing_to_completed(self):
        with pytest.raises(OrchestrateError, match="invalid task execution state transition"):
            validate_task_execution_transition(TaskExecutionState.INITIALIZING, TaskExecutionState.COMPLETED)

    def test_valid_running_to_waiting_for_agent(self):
        validate_task_execution_transition(TaskExecutionState.RUNNING, TaskExecutionState.WAITING_FOR_AGENT)

    def test_valid_running_to_paused_low_disk(self):
        validate_task_execution_transition(TaskExecutionState.RUNNING, TaskExecutionState.PAUSED_LOW_DISK)

    def test_valid_paused_to_recovering(self):
        validate_task_execution_transition(TaskExecutionState.PAUSED_LOW_DISK, TaskExecutionState.RECOVERING)

    def test_valid_human_required_to_running(self):
        validate_task_execution_transition(TaskExecutionState.HUMAN_REQUIRED, TaskExecutionState.RUNNING)

    def test_valid_human_required_to_supervising(self):
        validate_task_execution_transition(
            TaskExecutionState.HUMAN_REQUIRED,
            TaskExecutionState.SUPERVISING,
        )

    def test_supervisor_can_wait_for_and_resume_human_decision(self):
        validate_task_execution_transition(
            TaskExecutionState.SUPERVISING,
            TaskExecutionState.WAITING_FOR_HUMAN_DECISION,
        )
        validate_task_execution_transition(
            TaskExecutionState.WAITING_FOR_HUMAN_DECISION,
            TaskExecutionState.SUPERVISING,
        )

    def test_supervisor_can_wait_for_and_resume_delegated_agent(self):
        validate_task_execution_transition(
            TaskExecutionState.SUPERVISING,
            TaskExecutionState.WAITING_FOR_AGENT,
        )
        validate_task_execution_transition(
            TaskExecutionState.WAITING_FOR_AGENT,
            TaskExecutionState.SUPERVISING,
        )

    def test_valid_human_required_to_cancelled(self):
        validate_task_execution_transition(TaskExecutionState.HUMAN_REQUIRED, TaskExecutionState.CANCELLED)

    def test_terminal_states_accept_no_transitions(self):
        for terminal in (TaskExecutionState.COMPLETED, TaskExecutionState.FAILED, TaskExecutionState.CANCELLED):
            for other in TaskExecutionState:
                if other != terminal:
                    with pytest.raises(OrchestrateError):
                        validate_task_execution_transition(terminal, other)

    def test_validating_plan_to_human_required(self):
        validate_task_execution_transition(TaskExecutionState.VALIDATING_PLAN, TaskExecutionState.HUMAN_REQUIRED)

    def test_final_validation_to_completed(self):
        validate_task_execution_transition(TaskExecutionState.FINAL_VALIDATION, TaskExecutionState.COMPLETED)

    def test_final_validation_back_to_running(self):
        validate_task_execution_transition(TaskExecutionState.FINAL_VALIDATION, TaskExecutionState.RUNNING)


class TestWorkPackageStageTransitions:
    def test_prepare_to_implement(self):
        validate_stage_transition(WorkPackageStage.PREPARE, WorkPackageStage.IMPLEMENT)

    def test_implement_to_fast_verify(self):
        validate_stage_transition(WorkPackageStage.IMPLEMENT, WorkPackageStage.FAST_VERIFY)

    def test_implement_to_review(self):
        validate_stage_transition(WorkPackageStage.IMPLEMENT, WorkPackageStage.REVIEW)

    def test_review_to_fix_review(self):
        validate_stage_transition(WorkPackageStage.REVIEW, WorkPackageStage.FIX_REVIEW)

    def test_review_to_final_review(self):
        validate_stage_transition(WorkPackageStage.REVIEW, WorkPackageStage.FINAL_REVIEW)

    def test_final_review_to_ready_to_commit(self):
        validate_stage_transition(WorkPackageStage.FINAL_REVIEW, WorkPackageStage.READY_TO_COMMIT)

    def test_ready_to_commit_to_completed(self):
        validate_stage_transition(WorkPackageStage.READY_TO_COMMIT, WorkPackageStage.COMPLETED)

    def test_prepare_to_completed(self):
        validate_stage_transition(WorkPackageStage.PREPARE, WorkPackageStage.COMPLETED)

    def test_invalid_implement_to_completed(self):
        with pytest.raises(OrchestrateError):
            validate_stage_transition(WorkPackageStage.IMPLEMENT, WorkPackageStage.COMPLETED)

    def test_completed_accepts_no_transitions(self):
        with pytest.raises(OrchestrateError):
            validate_stage_transition(WorkPackageStage.COMPLETED, WorkPackageStage.PREPARE)

    def test_review_back_to_implement(self):
        validate_stage_transition(WorkPackageStage.REVIEW, WorkPackageStage.IMPLEMENT)

    def test_full_verify_to_ready_to_commit(self):
        validate_stage_transition(WorkPackageStage.FULL_VERIFY, WorkPackageStage.READY_TO_COMMIT)

    def test_fix_review_to_regression_verify(self):
        validate_stage_transition(WorkPackageStage.FIX_REVIEW, WorkPackageStage.REGRESSION_VERIFY)

    def test_regression_back_to_review(self):
        validate_stage_transition(WorkPackageStage.REGRESSION_VERIFY, WorkPackageStage.REVIEW)


class TestWorkPackage:
    def test_create_basic_package(self):
        wp = WorkPackage(id="test-1", title="Test package")
        assert wp.id == "test-1"
        assert wp.stage == WorkPackageStage.PREPARE
        assert wp.status == "pending"
        assert wp.dependencies == []

    def test_package_with_acceptance_criteria(self):
        criteria = [
            AcceptanceCriterion(id="ac1", description="Must pass tests"),
            AcceptanceCriterion(id="ac2", description="Must be documented"),
        ]
        wp = WorkPackage(
            id="wp-1",
            title="Implementation",
            acceptance_criteria=criteria,
        )
        assert len(wp.acceptance_criteria) == 2
        assert wp.acceptance_criteria[0].verified is False

    def test_as_mapping_roundtrip(self):
        original = WorkPackage(
            id="wp-1",
            title="Test",
            dependencies=["wp-0"],
            requirements=["req1"],
            acceptance_criteria=[
                AcceptanceCriterion(id="ac1", description="Must work", verified=True, evidence="Tests pass")
            ],
            affected_repositories=["repo-a"],
            stage=WorkPackageStage.REVIEW,
            status="active",
            risk="high",
            priority=5,
            verification_profile="full",
            agent_id="agent-1",
            reviewer_id="agent-2",
            agent_preferences={
                "implement": ["agent-1", "agent-3"],
                "final_review": ["agent-4"],
            },
            skill_preferences={
                "implement": ["ai-implement"],
                "final_review": ["ai-review"],
            },
            review_recovery_cycles=1,
            review_recovery_fingerprint="abc123",
            review_recovery_origin_stage="final_review",
            operator_paused=True,
            operator_pause_reason="dependency unavailable",
            operator_paused_at="2026-07-31T12:00:00+00:00",
        )
        mapping = original.as_mapping()
        restored = WorkPackage.from_mapping(mapping)
        assert restored.id == original.id
        assert restored.title == original.title
        assert restored.dependencies == original.dependencies
        assert restored.stage == original.stage
        assert restored.acceptance_criteria[0].verified is True
        assert restored.acceptance_criteria[0].evidence == "Tests pass"
        assert restored.agent_id == "agent-1"
        assert restored.agent_preferences == original.agent_preferences
        assert restored.skill_preferences == original.skill_preferences
        assert restored.review_recovery_cycles == 1
        assert restored.review_recovery_fingerprint == "abc123"
        assert restored.review_recovery_origin_stage == "final_review"
        assert restored.operator_paused is True
        assert restored.operator_pause_reason == "dependency unavailable"
        assert restored.operator_paused_at == "2026-07-31T12:00:00+00:00"

    def test_empty_package_from_mapping(self):
        with pytest.raises(KeyError):
            WorkPackage.from_mapping({})


class TestPlanGraph:
    def test_empty_graph_ready(self):
        graph = PlanGraph(work_packages=[])
        assert graph.ready_packages() == []

    def test_ready_packages_with_no_deps(self):
        packages = [
            WorkPackage(id="wp-1", title="First"),
            WorkPackage(id="wp-2", title="Second"),
        ]
        graph = PlanGraph(work_packages=packages)
        ready = graph.ready_packages()
        assert len(ready) == 2

    def test_ready_packages_excludes_operator_paused_work_packages(self):
        packages = [
            WorkPackage(id="wp-1", title="Paused", operator_paused=True),
            WorkPackage(id="wp-2", title="Ready"),
        ]
        graph = PlanGraph(work_packages=packages)

        assert [item.id for item in graph.ready_packages()] == ["wp-2"]

    def test_ready_packages_honors_dependencies(self):
        packages = [
            WorkPackage(id="wp-1", title="First", dependencies=[]),
            WorkPackage(id="wp-2", title="Second", dependencies=["wp-1"]),
        ]
        graph = PlanGraph(work_packages=packages)
        ready = graph.ready_packages()
        assert len(ready) == 1
        assert ready[0].id == "wp-1"

    def test_dependency_ready_after_completion(self):
        packages = [
            WorkPackage(id="wp-1", title="First", stage=WorkPackageStage.COMPLETED),
            WorkPackage(id="wp-2", title="Second", dependencies=["wp-1"]),
        ]
        graph = PlanGraph(work_packages=packages)
        assert graph.dependency_ready("wp-2") is True

    def test_dependency_not_ready(self):
        packages = [
            WorkPackage(id="wp-1", title="First", stage=WorkPackageStage.REVIEW),
            WorkPackage(id="wp-2", title="Second", dependencies=["wp-1"]),
        ]
        graph = PlanGraph(work_packages=packages)
        assert graph.dependency_ready("wp-2") is False

    def test_validate_acyclic_passes(self):
        packages = [
            WorkPackage(id="wp-1", title="First"),
            WorkPackage(id="wp-2", title="Second", dependencies=["wp-1"]),
            WorkPackage(id="wp-3", title="Third", dependencies=["wp-2"]),
        ]
        graph = PlanGraph(work_packages=packages)
        graph.validate_acyclic()

    def test_validate_acyclic_detects_cycle(self):
        packages = [
            WorkPackage(id="wp-1", title="First", dependencies=["wp-3"]),
            WorkPackage(id="wp-2", title="Second", dependencies=["wp-1"]),
            WorkPackage(id="wp-3", title="Third", dependencies=["wp-2"]),
        ]
        graph = PlanGraph(work_packages=packages)
        with pytest.raises(OrchestrateError, match="cycle"):
            graph.validate_acyclic()

    def test_self_cycle(self):
        packages = [
            WorkPackage(id="wp-1", title="First", dependencies=["wp-1"]),
        ]
        graph = PlanGraph(work_packages=packages)
        with pytest.raises(OrchestrateError, match="cycle"):
            graph.validate_acyclic()

    def test_validate_completeness_finds_missing_deps(self):
        packages = [
            WorkPackage(id="wp-1", title="First", dependencies=["wp-missing"]),
        ]
        graph = PlanGraph(work_packages=packages)
        findings = graph.validate_completeness()
        assert any("missing" in f for f in findings)

    def test_validate_completeness_finds_missing_ac(self):
        packages = [
            WorkPackage(id="wp-1", title="First"),
        ]
        graph = PlanGraph(work_packages=packages)
        findings = graph.validate_completeness()
        assert any("acceptance criteria" in f for f in findings)

    def test_ready_packages_sorted_by_priority(self):
        packages = [
            WorkPackage(id="wp-1", title="Low", priority=1),
            WorkPackage(id="wp-2", title="High", priority=10),
        ]
        graph = PlanGraph(work_packages=packages)
        ready = graph.ready_packages()
        assert ready[0].id == "wp-2"

    def test_package_by_id(self):
        packages = [WorkPackage(id="wp-1", title="Test")]
        graph = PlanGraph(work_packages=packages)
        assert graph.package_by_id("wp-1").title == "Test"

    def test_package_by_id_not_found(self):
        graph = PlanGraph()
        with pytest.raises(OrchestrateError, match="not found"):
            graph.package_by_id("nonexistent")


class TestTaskExecutionStateRecord:
    def test_create_default_record(self):
        record = TaskExecutionStateRecord(project_id="test-project")
        assert record.project_id == "test-project"
        assert record.state == TaskExecutionState.INITIALIZING
        assert record.completed_packages == 0

    def test_as_mapping_roundtrip(self):
        packages = [WorkPackage(id="wp-1", title="First")]
        original = TaskExecutionStateRecord(
            schema_version=1,
            project_id="test-p",
            state=TaskExecutionState.RUNNING,
            plan_graph=PlanGraph(work_packages=packages),
            started_at="2026-01-01T00:00:00",
            completed_packages=0,
            total_packages=1,
        )
        mapping = original.as_mapping()
        restored = TaskExecutionStateRecord.from_mapping(mapping)
        assert restored.project_id == original.project_id
        assert restored.state == original.state
        assert restored.total_packages == 1
        assert restored.plan_graph.work_packages[0].id == "wp-1"


def test_work_package_explicit_and_inferred_complexity():
    explicit = WorkPackage(id="explicit", title="Explicit", complexity=88)
    inferred = WorkPackage(
        id="inferred",
        title="Inferred",
        risk="critical",
        affected_repositories=["a", "b", "c"],
        requirements=["r1", "r2"],
        acceptance_criteria=[AcceptanceCriterion(id="ac", description="done")],
        verification_profile="full",
    )

    assert explicit.complexity_score() == 88
    assert inferred.complexity_score() == 100
    assert explicit.as_mapping()["computed_complexity"] == 88


def test_invalid_explicit_complexity_is_rejected():
    with pytest.raises(OrchestrateError, match="complexity"):
        WorkPackage(id="bad", title="Bad", complexity=101)


def test_work_package_rejects_invalid_persisted_execution_policy():
    data = WorkPackage(id="WP1", title="Work Package").as_mapping()
    data["agent_preferences"] = {"unknown_role": ["agent"]}

    with pytest.raises(OrchestrateError, match="unsupported execution role"):
        WorkPackage.from_mapping(data)


def test_direct_work_package_construction_validates_execution_policy():
    with pytest.raises(OrchestrateError, match="unsupported execution role"):
        WorkPackage(
            id="M-invalid",
            title="Invalid policy",
            agent_preferences={"unknown": ["codex"]},
        )

    source = ["codex"]
    package = WorkPackage(
        id="M-copy",
        title="Defensive policy copy",
        agent_preferences={"review": source},
    )
    source.append("claude-code")
    assert package.agent_preferences == {"review": ["codex"]}


def test_operator_pause_state_can_resume_running():
    validate_task_execution_transition(TaskExecutionState.RUNNING, TaskExecutionState.OPERATOR_PAUSED)
    validate_task_execution_transition(TaskExecutionState.OPERATOR_PAUSED, TaskExecutionState.RUNNING)


def test_future_work_package_directives_round_trip():
    package = WorkPackage(
        id="WP9",
        title="Future",
        pause_before_start=True,
        pause_before_start_reason="operator checkpoint",
        pause_before_start_requested_at="2026-08-04T12:00:00+00:00",
        decomposition_required=True,
        decomposition_required_reason="must split",
        decomposition_required_at="2026-08-04T12:01:00+00:00",
    )

    restored = WorkPackage.from_mapping(package.as_mapping())

    assert restored.pause_before_start is True
    assert restored.pause_before_start_reason == "operator checkpoint"
    assert restored.decomposition_required is True
    assert restored.decomposition_required_reason == "must split"


def test_ready_repository_sync_packages_are_prioritized_over_development_work() -> None:
    sync = WorkPackage.from_mapping(
        {
            "id": "WP20-SYNC",
            "title": "Sync",
            "kind": "repository_sync",
            "affected_repositories": ["core"],
            "repository_sync": {"repositories": ["core"]},
            "requirements": ["sync"],
            "acceptance_criteria": [{"id": "sync_done", "description": "done"}],
            "priority": 1,
        }
    )
    development = WorkPackage(
        id="OTHER",
        title="Other",
        requirements=["work"],
        acceptance_criteria=[AcceptanceCriterion(id="other_done", description="done")],
        priority=1000,
    )

    ready = PlanGraph([development, sync]).ready_packages()

    assert [item.id for item in ready] == ["WP20-SYNC", "OTHER"]
