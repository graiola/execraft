"""Tests for the project orchestrator state machine."""

import subprocess
from pathlib import Path

import pytest

from execraft.orchestrate.models import (
    AcceptanceCriterion,
    OrchestrateError,
    PlanGraph,
    TaskExecutionState,
    TaskExecutionStateRecord,
    WorkPackage,
    WorkPackageStage,
)
from execraft.orchestrate.normalizer import NormalizationReport
from execraft.orchestrate.context_budget import TokenBudget
from execraft.orchestrate.orchestrator import (
    OrchestrationConfig,
    ProjectOrchestrator,
    _AgentWaitRequested,
)
from execraft.orchestrate.provider_promotion import ProviderPromotionStore
from execraft.orchestrate.scheduler import (
    AgentAdapter,
    AgentCapability,
    Availability,
    StructuredHandoff,
)
from execraft.orchestrate.verification import (
    CHEAP,
    KnownFailure,
    VerificationCommand,
    VerificationRegistry,
)


def _git(path: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=path, text=True, capture_output=True, check=True
    )
    return completed.stdout.strip()


def _repo(path: Path) -> Path:
    """Create an isolated Git checkout so repository snapshots never fall
    back to the Execraft checkout the suite runs from (which is detached in
    CI pull-request runs, where `git symbolic-ref` cannot resolve a branch).
    """

    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "execraft@example.invalid")
    _git(path, "config", "user.name", "Execraft tests")
    (path / "README.md").write_text("baseline\n")
    _git(path, "add", "README.md")
    _git(path, "commit", "-q", "-m", "baseline")
    return path


class _FakeAgentAdapter(AgentAdapter):
    def __init__(self, provider_id: str, caps: set[AgentCapability], avail: Availability = Availability.AVAILABLE):
        self._id = provider_id
        self._caps = caps
        self._avail = avail
        self.executions: list[StructuredHandoff] = []

    @property
    def availability(self) -> Availability:
        return self._avail

    @property
    def provider_id(self) -> str:
        return self._id

    @property
    def capabilities(self) -> set[AgentCapability]:
        return self._caps

    def execute(self, handoff: StructuredHandoff) -> dict:
        self.executions.append(handoff)
        return {"ok": True, "work_package_id": handoff.work_package_id}


class _PinnedModelAgentAdapter(_FakeAgentAdapter):
    """Adapter with a pinned model that reports whether its config declares it."""

    def __init__(
        self,
        provider_id: str,
        caps: set[AgentCapability],
        *,
        model: str,
        declares_model: bool,
    ):
        super().__init__(provider_id, caps)
        self._model = model
        self._declares_model = declares_model

    @property
    def model(self) -> str:
        return self._model

    def declares_configured_model(self) -> bool:
        return self._declares_model


class _ResettableFakeAgentAdapter(_FakeAgentAdapter):
    def reset_transient_availability(self) -> None:
        if self._avail in {
            Availability.BUSY,
            Availability.COOLDOWN,
            Availability.RATE_LIMITED,
            Availability.QUOTA_EXHAUSTED,
            Availability.SESSION_LIMIT,
            Availability.NETWORK_TRANSIENT,
        }:
            self._avail = Availability.AVAILABLE


class _LongInvalidReviewAdapter(AgentAdapter):
    def __init__(self):
        self._id = "reviewer"

    @property
    def availability(self) -> Availability:
        return Availability.AVAILABLE

    @property
    def provider_id(self) -> str:
        return self._id

    @property
    def capabilities(self) -> set[AgentCapability]:
        return {AgentCapability.REVIEW}

    def execute(self, handoff: StructuredHandoff) -> dict:
        return {
            "ok": True,
            "final_message": "review prose " + ("x" * 6000),
        }


class _StructuredResultAdapter(AgentAdapter):
    def __init__(
        self, provider_id: str, final_message: str, caps: set[AgentCapability]
    ):
        self._id = provider_id
        self._message = final_message
        self._caps = caps
        self.call_count = 0
        self.executions: list[StructuredHandoff] = []

    @property
    def availability(self) -> Availability:
        return Availability.AVAILABLE

    @property
    def provider_id(self) -> str:
        return self._id

    @property
    def capabilities(self) -> set[AgentCapability]:
        return self._caps

    def execute(self, handoff: StructuredHandoff) -> dict:
        self.call_count += 1
        self.executions.append(handoff)
        return {
            "ok": True,
            "work_package_id": handoff.work_package_id,
            "final_message": self._message,
        }


class _FlakyAgentAdapter(AgentAdapter):
    """Raises for the first `fail_times` calls, then delegates to a real
    execution — used to simulate a crashing/unavailable provider that either
    never recovers (fail_times >= budget) or recovers within the retry
    budget."""

    def __init__(
        self,
        provider_id: str,
        caps: set[AgentCapability],
        *,
        fail_times: int = 999,
        error_message: str = "tool crashed",
    ):
        self._id = provider_id
        self._caps = caps
        self._fail_times = fail_times
        self._error_message = error_message
        self.call_count = 0
        self.executions: list[StructuredHandoff] = []

    @property
    def availability(self) -> Availability:
        return Availability.AVAILABLE

    @property
    def provider_id(self) -> str:
        return self._id

    @property
    def capabilities(self) -> set[AgentCapability]:
        return self._caps

    def execute(self, handoff: StructuredHandoff) -> dict:
        self.call_count += 1
        if self.call_count <= self._fail_times:
            raise RuntimeError(self._error_message)
        self.executions.append(handoff)
        return {"ok": True, "work_package_id": handoff.work_package_id}


class _PackageSelectiveAgent(AgentAdapter):
    """Fail selected packages while succeeding on all other work."""

    def __init__(
        self,
        provider_id: str,
        caps: set[AgentCapability],
        *,
        failing_packages: set[str] | None = None,
    ):
        self._id = provider_id
        self._caps = caps
        self._failing_packages = set(failing_packages or set())
        self.executions: list[str] = []

    @property
    def availability(self) -> Availability:
        return Availability.AVAILABLE

    @property
    def provider_id(self) -> str:
        return self._id

    @property
    def capabilities(self) -> set[AgentCapability]:
        return self._caps

    def execute(self, handoff: StructuredHandoff) -> dict:
        self.executions.append(handoff.work_package_id)
        if handoff.work_package_id in self._failing_packages:
            raise RuntimeError(f"simulated provider stall for {handoff.work_package_id}")
        return {"ok": True, "work_package_id": handoff.work_package_id}


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _ScriptedCommandRunner:
    """Records every invocation and returns scripted returncodes in order
    (repeating the last entry once exhausted). A returncode of the string
    "raise" simulates a runner-level environment failure (command not
    found, timeout) instead of a normal nonzero exit. Never touches a real
    subprocess."""

    def __init__(self, returncodes: list[int | str]):
        self._returncodes = list(returncodes)
        self.calls: list[str] = []

    def __call__(self, command: str, *, cwd, timeout):
        self.calls.append(command)
        idx = min(len(self.calls) - 1, len(self._returncodes) - 1)
        outcome = self._returncodes[idx]
        if outcome == "raise":
            raise RuntimeError("simulated environment failure")
        return _FakeCompletedProcess(outcome, stdout=f"ran: {command}")


class _FakeResourceManager:
    """Deterministic stand-in for ResourceManager: never touches real disk
    usage or Docker. `cleanup()` clears the pressure flag by default so a
    test's pipeline run makes forward progress instead of looping forever."""

    def __init__(
        self,
        *,
        needs_cleanup: bool = False,
        pressure_after_cleanup: bool = False,
        can_resume: bool = True,
        clears_after_cleanup: bool = True,
    ):
        self._needs_cleanup = needs_cleanup
        self._pressure_after_cleanup = pressure_after_cleanup
        self._can_resume = can_resume
        self._clears_after_cleanup = clears_after_cleanup
        self.cleanup_calls = 0
        self.inventory_calls = 0

    def inventory(self, workspace_roots=None):
        from execraft.orchestrate.resource import ResourceInventory
        self.inventory_calls += 1
        return ResourceInventory(filesystem_used_percent=92)

    def needs_cleanup(self, inventory=None) -> bool:
        return self._needs_cleanup

    def cleanup(self, workspace_roots=None, dry_run=False) -> list[str]:
        self.cleanup_calls += 1
        if self._clears_after_cleanup:
            self._needs_cleanup = False
        return ["remove stale managed workspace: /fake/workspace"]

    def needs_pause(self, inventory=None) -> bool:
        return self._pressure_after_cleanup

    def can_resume(self, inventory=None) -> bool:
        return self._can_resume


class TestProjectOrchestrator:
    def test_create_orchestrator(self, tmp_path):
        config = OrchestrationConfig(strict_checks=False, auto_commit=False, state_dir=tmp_path / "state")
        orch = ProjectOrchestrator("test-project", config=config)
        assert orch.project_id == "test-project"
        assert orch.state == TaskExecutionState.INITIALIZING

    def test_initialize_with_valid_plan(self, tmp_path):
        config = OrchestrationConfig(strict_checks=False, auto_commit=False, state_dir=tmp_path / "state")
        orch = ProjectOrchestrator("test-project", config=config)
        plan = """## Package: First

### Acceptance criteria
- [ ] Works
"""
        report = orch.initialize(plan)
        assert not report.has_errors()
        assert orch.state == TaskExecutionState.VALIDATING_PLAN
        assert orch._state_record.total_packages == 1

    def test_initialize_with_invalid_plan(self, tmp_path):
        config = OrchestrationConfig(strict_checks=False, auto_commit=False, state_dir=tmp_path / "state")
        orch = ProjectOrchestrator("test-project", config=config)
        plan = """## Package: Broken

Dependencies: does-not-exist

### Acceptance criteria
- [ ] Works
"""
        report = orch.initialize(plan)
        assert report.has_errors()
        assert orch.state == TaskExecutionState.FAILED

    def test_initialize_with_cyclic_plan(self, tmp_path):
        config = OrchestrationConfig(strict_checks=False, auto_commit=False, state_dir=tmp_path / "state")
        orch = ProjectOrchestrator("test-project", config=config)
        plan = """## Package: A

Dependencies: C

### Acceptance criteria
- [ ] Works

## Package: B

Dependencies: A

### Acceptance criteria
- [ ] Works

## Package: C

Dependencies: B

### Acceptance criteria
- [ ] Works
"""
        report = orch.initialize(plan)
        assert len(report.cycles_detected) > 0

    def test_stage_advancement_is_recorded_for_execution_trace(self, tmp_path):
        config = OrchestrationConfig(
            strict_checks=False,
            auto_commit=False,
            state_dir=tmp_path / "state",
        )
        orch = ProjectOrchestrator("test-project", config=config)
        package = WorkPackage(id="WP1", title="Trace me")
        orch._state_record.plan_graph = PlanGraph([package])

        orch._advance_package_stage(package, WorkPackageStage.IMPLEMENT)

        transition = next(
            entry
            for entry in orch._journal.read()
            if entry.event_type == "package_stage_transition"
        )
        assert transition.payload["package_id"] == "WP1"
        assert transition.payload["from_stage"] == "prepare"
        assert transition.payload["to_stage"] == "implement"

    def test_transition_to_valid_state(self, tmp_path):
        config = OrchestrationConfig(strict_checks=False, auto_commit=False, state_dir=tmp_path / "state")
        orch = ProjectOrchestrator("test-project", config=config)
        orch.transition_to(TaskExecutionState.VALIDATING_PLAN)
        assert orch.state == TaskExecutionState.VALIDATING_PLAN

    def test_transition_to_invalid_state(self, tmp_path):
        config = OrchestrationConfig(strict_checks=False, auto_commit=False, state_dir=tmp_path / "state")
        orch = ProjectOrchestrator("test-project", config=config)
        with pytest.raises(Exception):
            orch.transition_to(TaskExecutionState.COMPLETED)

    def test_save_and_load_state(self, tmp_path):
        config = OrchestrationConfig(strict_checks=False, auto_commit=False, state_dir=tmp_path / "state")
        orch1 = ProjectOrchestrator("test-project", config=config)
        orch1.transition_to(TaskExecutionState.VALIDATING_PLAN)

        orch2 = ProjectOrchestrator("test-project", config=config)
        loaded = orch2.load_state()
        assert loaded.state == TaskExecutionState.VALIDATING_PLAN

    def test_operator_pause_is_persisted_and_can_include_direct_shards(self, tmp_path):
        config = OrchestrationConfig(
            strict_checks=False, auto_commit=False, state_dir=tmp_path / "state"
        )
        orch = ProjectOrchestrator("test-project", config=config)
        parent = WorkPackage(id="WP1", title="Parent")
        shard = WorkPackage(id="WP1__a", title="Shard", parent_id="WP1")
        orch._state_record.plan_graph = PlanGraph([parent, shard])

        report = orch.set_package_pause(
            "WP1", paused=True, reason="awaiting operator", apply_to_shards=True
        )

        assert report["affected_packages"] == ["WP1", "WP1__a"]
        assert parent.operator_paused is True
        assert shard.operator_paused is True
        assert orch._state_record.plan_graph.ready_packages() == []
        status = orch.status_report()
        assert [item["id"] for item in status["paused_packages"]] == [
            "WP1",
            "WP1__a",
        ]
        assert status["current_package"] is None
        assert status["next_package"] is None
        assert status["blocked_packages"] == []
        persisted = ProjectOrchestrator("test-project", config=config).load_state()
        assert persisted.plan_graph.package_by_id("WP1").operator_pause_reason == "awaiting operator"

        orch.set_package_pause("WP1", paused=False, apply_to_shards=True)
        assert [item.id for item in orch._state_record.plan_graph.ready_packages()] == ["WP1", "WP1__a"]

    def test_supervisor_does_not_redirect_a_paused_incident(self, tmp_path):
        config = OrchestrationConfig(
            strict_checks=False, auto_commit=False, state_dir=tmp_path / "state"
        )
        orch = ProjectOrchestrator("test-project", config=config)
        paused = WorkPackage(
            id="WP1",
            title="Paused incident",
            stage=WorkPackageStage.REVIEW,
            status="running",
            operator_paused=True,
        )
        unrelated = WorkPackage(
            id="WP2",
            title="Other work",
            stage=WorkPackageStage.IMPLEMENT,
            status="running",
        )
        orch._state_record.plan_graph = PlanGraph([paused, unrelated])

        assert orch._supervisor_package({"package_id": "WP1"}, None) is None

    def test_status_report(self, tmp_path):
        config = OrchestrationConfig(strict_checks=False, auto_commit=False, state_dir=tmp_path / "state")
        orch = ProjectOrchestrator("test-project", config=config)
        report = orch.status_report()
        assert report["project_id"] == "test-project"
        assert report["state"] == "initializing"
        assert report["total_packages"] == 0

    def test_status_report_exposes_current_ready_and_blocked_packages(self, tmp_path):
        config = OrchestrationConfig(
            strict_checks=False,
            auto_commit=False,
            state_dir=tmp_path / "state",
        )
        orch = ProjectOrchestrator("test-project", config=config)
        orch._state_record.plan_graph = PlanGraph(
            work_packages=[
                WorkPackage(
                    id="WP01",
                    title="Foundation",
                    stage=WorkPackageStage.COMPLETED,
                    status="completed",
                    priority=30,
                ),
                WorkPackage(
                    id="WP02",
                    title="Ready work",
                    dependencies=["WP01"],
                    priority=20,
                    verification_profile="focused",
                ),
                WorkPackage(
                    id="WP03",
                    title="Blocked work",
                    dependencies=["WP02"],
                    priority=10,
                ),
                WorkPackage(
                    id="WP04",
                    title="Active work",
                    dependencies=["WP01"],
                    stage=WorkPackageStage.REVIEW,
                    priority=25,
                ),
            ]
        )
        orch._state_record.completed_packages = 1

        report = orch.status_report()

        assert report["completed_package_ids"] == ["WP01"]
        assert report["current_package"]["id"] == "WP04"
        assert report["next_package"]["id"] == "WP02"
        assert [package["id"] for package in report["ready_packages"]] == ["WP02"]
        assert [package["id"] for package in report["blocked_packages"]] == ["WP03"]
        assert report["blocked_packages"][0]["missing_dependencies"] == ["WP02"]

    def test_register_agent(self, tmp_path):
        config = OrchestrationConfig(strict_checks=False, auto_commit=False, state_dir=tmp_path / "state")
        orch = ProjectOrchestrator("test-project", config=config)
        agent = _FakeAgentAdapter("agent-1", {AgentCapability.IMPLEMENT})
        orch.register_agent(agent)
        assert len(orch._agent_slots) == 1

    def test_agent_execution_through_pipeline(self, tmp_path):
        config = OrchestrationConfig(strict_checks=False, auto_commit=False, state_dir=tmp_path / "state")
        orch = ProjectOrchestrator("test-project", config=config)
        implementer = _FakeAgentAdapter("impl-1", {AgentCapability.IMPLEMENT})
        reviewer = _FakeAgentAdapter("rev-1", {AgentCapability.REVIEW})
        orch.register_agent(implementer)
        orch.register_agent(reviewer)

        plan = """## Package: First

### Acceptance criteria
- [ ] Works
"""
        orch.initialize(plan)
        assert orch.state == TaskExecutionState.VALIDATING_PLAN

        orch.run_pipeline()
        report = orch.status_report()
        assert report["state"] == "completed"
        assert report["completed_packages"] == 1
        assert len(implementer.executions) >= 1
        assert len(reviewer.executions) >= 1

    def test_run_pipeline_with_dependencies(self, tmp_path):
        config = OrchestrationConfig(strict_checks=False, auto_commit=False, state_dir=tmp_path / "state")
        orch = ProjectOrchestrator("test-project", config=config)
        agent = _FakeAgentAdapter("agent-1", {AgentCapability.IMPLEMENT, AgentCapability.REVIEW})
        orch.register_agent(agent)

        plan = """## Package: Foundation

### Acceptance criteria
- [ ] Core works

## Package: Features

Dependencies: Foundation

### Acceptance criteria
- [ ] Features work
"""
        orch.initialize(plan)
        assert orch.state == TaskExecutionState.VALIDATING_PLAN

        orch.run_pipeline()
        report = orch.status_report()
        assert report["state"] == "completed"
        assert report["completed_packages"] == 2

    def test_pipeline_with_missing_agent_falls_back(self, tmp_path):
        config = OrchestrationConfig(strict_checks=False, auto_commit=False, state_dir=tmp_path / "state")
        orch = ProjectOrchestrator("test-project", config=config)
        # Only register reviewer, no implementer
        reviewer = _FakeAgentAdapter("rev-1", {AgentCapability.REVIEW})
        orch.register_agent(reviewer)

        plan = """## Package: Solo

### Acceptance criteria
- [ ] Works
"""
        orch.initialize(plan)
        orch.run_pipeline()
        report = orch.status_report()
        # Should complete without agent errors
        assert report["state"] == "completed"

    def test_schedule_prefers_independent_reviewer_when_available(self, tmp_path):
        config = OrchestrationConfig(strict_checks=False, auto_commit=False, state_dir=tmp_path / "state")
        orch = ProjectOrchestrator("test-project", config=config)
        implementer = _FakeAgentAdapter(
            "impl-1", {AgentCapability.IMPLEMENT, AgentCapability.REVIEW}
        )
        reviewer = _FakeAgentAdapter("rev-1", {AgentCapability.REVIEW})
        orch.register_agent(implementer)
        orch.register_agent(reviewer)

        package = WorkPackage(id="pkg", title="Pkg")
        schedule = orch._schedule_agents(package)
        assert schedule.implementer_id == "impl-1"
        assert schedule.reviewer_id == "rev-1"
        assert schedule.reviewer_is_independent() is True
        fallback_events = [
            e for e in orch._journal.read()
            if e.event_type == "reviewer_independence_fallback"
        ]
        assert fallback_events == []

    def test_schedule_falls_back_to_same_provider_review_when_no_alternative(
        self, tmp_path
    ):
        config = OrchestrationConfig(strict_checks=False, auto_commit=False, state_dir=tmp_path / "state")
        orch = ProjectOrchestrator("test-project", config=config)
        solo = _FakeAgentAdapter(
            "solo-1", {AgentCapability.IMPLEMENT, AgentCapability.REVIEW}
        )
        orch.register_agent(solo)

        package = WorkPackage(id="pkg", title="Pkg")
        schedule = orch._schedule_agents(package)
        assert schedule.implementer_id == "solo-1"
        assert schedule.reviewer_id == "solo-1"
        assert schedule.reviewer_is_independent() is False
        fallback_events = [
            e for e in orch._journal.read()
            if e.event_type == "reviewer_independence_fallback"
        ]
        assert len(fallback_events) == 1
        assert fallback_events[0].payload["implementer"] == "solo-1"
        assert fallback_events[0].payload["reviewer"] == "solo-1"

    def test_schedule_does_not_fall_back_when_policy_disallows(self, tmp_path):
        config = OrchestrationConfig(strict_checks=False, auto_commit=False,
            state_dir=tmp_path / "state", allow_same_provider_review=False
        )
        orch = ProjectOrchestrator("test-project", config=config)
        solo = _FakeAgentAdapter(
            "solo-1", {AgentCapability.IMPLEMENT, AgentCapability.REVIEW}
        )
        orch.register_agent(solo)

        package = WorkPackage(id="pkg", title="Pkg")
        schedule = orch._schedule_agents(package)
        assert schedule.implementer_id == "solo-1"
        assert schedule.reviewer_id == ""
        fallback_events = [
            e for e in orch._journal.read()
            if e.event_type == "reviewer_independence_fallback"
        ]
        assert fallback_events == []

    def test_same_provider_assignments_survive_own_fix_cycle(self, tmp_path):
        config = OrchestrationConfig(
            strict_checks=True, auto_commit=False, state_dir=tmp_path / "state"
        )
        orch = ProjectOrchestrator("test-project", config=config)
        solo = _FakeAgentAdapter(
            "solo-1",
            {
                AgentCapability.IMPLEMENT,
                AgentCapability.REVIEW,
                AgentCapability.FIX_REVIEW,
            },
        )
        orch.register_agent(solo)
        package = WorkPackage(
            id="pkg",
            title="Pkg",
            stage=WorkPackageStage.FINAL_REVIEW,
            agent_id="solo-1",
            reviewer_id="solo-1",
            final_reviewer_id="solo-1",
            last_fixer_id="solo-1",
        )

        schedule = orch._ensure_review_assignments(package)

        assert schedule.implementer_id == "solo-1"
        assert schedule.reviewer_id == "solo-1"
        assert schedule.final_reviewer_id == "solo-1"


class TestResourcePressureWiring:
    def _orchestrator(self, tmp_path, resource_manager):
        config = OrchestrationConfig(strict_checks=False, auto_commit=False, state_dir=tmp_path / "state")
        orch = ProjectOrchestrator(
            "test-project", config=config, resource_manager=resource_manager
        )
        agent = _FakeAgentAdapter(
            "solo", {AgentCapability.IMPLEMENT, AgentCapability.REVIEW}
        )
        orch.register_agent(agent)
        orch.initialize("## Package: Only\n\n### Acceptance criteria\n- [ ] Works\n")
        return orch

    def _event_types(self, orch):
        return [e.event_type for e in orch._journal.read()]

    def test_no_resource_manager_is_unaffected(self, tmp_path):
        # Default (resource_manager=None) must behave exactly as before —
        # this host can be near its real disk watermark, and no existing
        # caller opted into pressure handling.
        config = OrchestrationConfig(strict_checks=False, auto_commit=False, state_dir=tmp_path / "state")
        orch = ProjectOrchestrator("test-project", config=config)
        orch.register_agent(
            _FakeAgentAdapter("solo", {AgentCapability.IMPLEMENT, AgentCapability.REVIEW})
        )
        orch.initialize("## Package: Only\n\n### Acceptance criteria\n- [ ] Works\n")
        orch.run_pipeline()
        assert orch.state == TaskExecutionState.COMPLETED

    def test_pipeline_attempts_cleanup_under_pressure_then_continues(self, tmp_path):
        mgr = _FakeResourceManager(needs_cleanup=True, pressure_after_cleanup=False)
        orch = self._orchestrator(tmp_path, mgr)
        orch.run_pipeline()

        assert orch.state == TaskExecutionState.COMPLETED
        assert orch.status_report()["completed_packages"] == 1
        assert mgr.cleanup_calls == 1
        events = self._event_types(orch)
        assert "resource_pressure_detected" in events
        assert "resource_cleanup_performed" in events
        assert "resource_pause" not in events

    def test_pipeline_pauses_when_pressure_persists_after_cleanup(self, tmp_path):
        mgr = _FakeResourceManager(
            needs_cleanup=True, pressure_after_cleanup=True, clears_after_cleanup=False
        )
        orch = self._orchestrator(tmp_path, mgr)
        orch.run_pipeline()

        assert orch.state == TaskExecutionState.PAUSED_LOW_DISK
        assert orch.status_report()["completed_packages"] == 0
        events = self._event_types(orch)
        assert "resource_pause" in events

    def test_run_pipeline_resumes_from_paused_state_when_disk_recovers(self, tmp_path):
        mgr = _FakeResourceManager(
            needs_cleanup=True, pressure_after_cleanup=True, clears_after_cleanup=False
        )
        orch = self._orchestrator(tmp_path, mgr)
        orch.run_pipeline()
        assert orch.state == TaskExecutionState.PAUSED_LOW_DISK

        # Disk pressure has cleared on a later check.
        mgr._needs_cleanup = False
        mgr._can_resume = True
        orch.run_pipeline()

        assert orch.state == TaskExecutionState.COMPLETED
        assert orch.status_report()["completed_packages"] == 1
        assert "resource_resumed" in self._event_types(orch)

    def test_run_pipeline_stays_paused_when_disk_has_not_recovered(self, tmp_path):
        mgr = _FakeResourceManager(
            needs_cleanup=True, pressure_after_cleanup=True, clears_after_cleanup=False
        )
        orch = self._orchestrator(tmp_path, mgr)
        orch.run_pipeline()
        assert orch.state == TaskExecutionState.PAUSED_LOW_DISK

        mgr._can_resume = False
        orch.run_pipeline()

        assert orch.state == TaskExecutionState.PAUSED_LOW_DISK
        assert orch.status_report()["completed_packages"] == 0
        assert "resource_resume_blocked" in self._event_types(orch)


class TestAgentFailoverAndEscalation:
    def _event_types(self, orch):
        return [e.event_type for e in orch._journal.read()]

    def test_live_provider_execution_keeps_project_state_running(self, tmp_path):
        observed_states: list[TaskExecutionState] = []
        config = OrchestrationConfig(
            strict_checks=False,
            auto_commit=False,
            state_dir=tmp_path / "state",
        )
        orch = ProjectOrchestrator("truthful-live-state", config=config)

        class ObservingAdapter(_FakeAgentAdapter):
            def execute(self, handoff: StructuredHandoff) -> dict:
                observed_states.append(orch.state)
                return super().execute(handoff)

        adapter = ObservingAdapter("worker", {AgentCapability.IMPLEMENT})
        orch.register_agent(adapter)
        orch.initialize("## Package: Only\n\n### Acceptance criteria\n- [ ] Works\n")
        orch.transition_to(TaskExecutionState.RUNNING)
        package = orch._state_record.plan_graph.package_by_id("only")
        package.stage = WorkPackageStage.IMPLEMENT

        orch._call_prebuilt_handoff(
            AgentCapability.IMPLEMENT,
            adapter.provider_id,
            package,
            StructuredHandoff(
                work_package_id=package.id,
                stage="implement",
                summary="Implement the package",
            ),
        )

        assert observed_states == [TaskExecutionState.RUNNING]
        assert orch.state == TaskExecutionState.RUNNING

    def test_failover_to_second_agent_recovers_from_a_crash(self, tmp_path):
        config = OrchestrationConfig(strict_checks=False, auto_commit=False, state_dir=tmp_path / "state")
        orch = ProjectOrchestrator("test-project", config=config)
        crashing = _FlakyAgentAdapter(
            "crashing", {AgentCapability.IMPLEMENT}, fail_times=999
        )
        healthy = _FlakyAgentAdapter(
            "healthy", {AgentCapability.IMPLEMENT}, fail_times=0
        )
        reviewer = _FakeAgentAdapter("rev", {AgentCapability.REVIEW})
        orch.register_agent(crashing)
        orch.register_agent(healthy)
        orch.register_agent(reviewer)

        orch.initialize("## Package: Only\n\n### Acceptance criteria\n- [ ] Works\n")
        orch.run_pipeline()

        assert orch.state == TaskExecutionState.COMPLETED
        assert orch.status_report()["completed_packages"] == 1
        assert crashing.call_count == 1
        assert healthy.call_count == 1
        failover_handoff = healthy.executions[0]
        assert failover_handoff.attempt == 2
        assert failover_handoff.parent_invocation_id
        assert failover_handoff.attempt_history[0]["agent_id"] == "crashing"
        assert failover_handoff.attempt_history[0]["classification"] == "tool_failure"
        history = [
            item
            for item in orch.invocation_history(package_id="only", limit=10)
            if item["capability"] == "implement"
        ]
        assert [item["status"] for item in reversed(history)] == ["failed", "completed"]
        assert history[0]["parent_invocation_id"] == history[1]["invocation_id"]
        events = self._event_types(orch)
        assert "agent_failure" in events
        assert "agent_failover" in events
        assert "human_intervention_required" not in events


    def test_invalid_structured_output_fails_over_before_human_escalation(
        self, tmp_path
    ):
        config = OrchestrationConfig(
            strict_checks=True,
            auto_commit=False,
            state_dir=tmp_path / "state",
            max_agent_attempts_per_stage=2,
        )
        orch = ProjectOrchestrator("test-project", config=config)
        implementer = _StructuredResultAdapter(
            "codex",
            '{"ok":true,"verdict":"approved","findings":[],"summary":"clean"}',
            {AgentCapability.REVIEW},
        )
        reviewer = _StructuredResultAdapter(
            "claude-code",
            '{"ok":true,"verdict":"approved","findings":[],"summary":"clean"}',
            {AgentCapability.REVIEW},
        )
        empty = _StructuredResultAdapter(
            "opencode-zen-free", "", {AgentCapability.REVIEW}
        )
        valid = _StructuredResultAdapter(
            "opencode-go",
            '{"ok":true,"verdict":"approved","findings":[],"summary":"clean"}',
            {AgentCapability.REVIEW},
        )
        orch.register_agent(implementer)
        orch.register_agent(reviewer)
        orch.register_agent(empty)
        orch.register_agent(valid)
        package = WorkPackage(
            id="WP10",
            title="Provider routing",
            stage=WorkPackageStage.FINAL_REVIEW,
        )
        handoff = StructuredHandoff(
            work_package_id="WP10",
            stage="final_review",
            summary="Final review",
            expected_output_schema={
                "type": "object",
                "required": ["ok", "verdict", "findings", "summary"],
                "properties": {
                    "ok": {"const": True},
                    "verdict": {"enum": ["approved", "changes_required"]},
                    "findings": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                    },
                    "summary": {"type": "string", "minLength": 1},
                },
            },
        )

        result = orch._execute_agent(
            AgentCapability.REVIEW,
            "opencode-zen-free",
            handoff,
            package,
            excluded_agent_ids={"codex", "claude-code"},
        )

        assert result["_execraft_structured_payload"]["verdict"] == "approved"
        assert implementer.call_count == 0
        assert reviewer.call_count == 0
        assert empty.call_count == 2
        assert valid.call_count == 1
        events = orch._journal.read()
        failures = [event for event in events if event.event_type == "agent_failure"]
        assert failures[0].payload["classification"] == "invalid_output"
        assert any(event.event_type == "agent_failover" for event in events)
        assert not any(
            event.event_type == "human_intervention_required" for event in events
        )
        persisted = [
            event for event in events if event.event_type == "agent_result_persisted"
        ]
        assert [event.payload["agent_id"] for event in persisted] == [
            "opencode-zen-free",
            "opencode-zen-free",
            "opencode-go",
        ]

    def test_structured_output_hard_limit_fails_without_json_truncation(
        self, tmp_path
    ):
        config = OrchestrationConfig(
            strict_checks=True,
            auto_commit=False,
            state_dir=tmp_path / "state",
            max_agent_attempts_per_stage=2,
            token_budgets={
                "review": TokenBudget(16000, 30000, 50, 100),
            },
        )
        orch = ProjectOrchestrator("output-limit", config=config)
        oversized = _StructuredResultAdapter(
            "oversized",
            '{"ok":true,"verdict":"approved","findings":[],"summary":"'
            + ("x" * 6000)
            + '"}',
            {AgentCapability.REVIEW},
        )
        healthy = _StructuredResultAdapter(
            "healthy",
            '{"ok":true,"verdict":"approved","findings":[],"summary":"clean"}',
            {AgentCapability.REVIEW},
        )
        orch.register_agent(oversized)
        orch.register_agent(healthy)
        package = WorkPackage(
            id="WP10",
            title="Bounded review",
            stage=WorkPackageStage.FINAL_REVIEW,
        )
        handoff = StructuredHandoff(
            work_package_id=package.id,
            stage="final_review",
            summary="Final review",
            output_token_target=50,
            output_token_hard_limit=100,
        )

        result = orch._execute_agent(
            AgentCapability.REVIEW,
            oversized.provider_id,
            handoff,
            package,
        )

        assert result["_execraft_structured_payload"]["summary"] == "clean"
        history = list(
            reversed(orch.invocation_history(package_id=package.id, limit=10))
        )
        assert history[0]["failure"]["classification"] == "output_budget_exceeded"
        artifact = history[0]["result_artifact"]
        assert artifact["size_bytes"] > 6000
        assert history[1]["status"] == "completed"

    def test_semantically_invalid_review_fails_before_invocation_completion(
        self, tmp_path
    ):
        config = OrchestrationConfig(
            strict_checks=True,
            auto_commit=False,
            state_dir=tmp_path / "state",
            max_agent_attempts_per_stage=2,
        )
        orch = ProjectOrchestrator("semantic-review", config=config)
        malformed = _StructuredResultAdapter(
            "qwen-malformed",
            '{"verdict":"changes_required","findings":[],"summary":"blockers exist"}',
            {AgentCapability.REVIEW},
        )
        healthy = _StructuredResultAdapter(
            "qwen-healthy",
            '{"verdict":"approved","findings":[],"summary":"clean"}',
            {AgentCapability.REVIEW},
        )
        orch.register_agent(malformed)
        orch.register_agent(healthy)
        package = WorkPackage(
            id="WP17__WP17-S4",
            title="Transport client",
            stage=WorkPackageStage.FINAL_REVIEW,
        )
        # Deliberately supply a legacy/inconsistent schema: every review path
        # must receive the canonical contract rather than trusting the caller.
        handoff = StructuredHandoff(
            work_package_id=package.id,
            stage="final_review",
            summary="Final review",
            expected_output_schema={
                "type": "object",
                "required": ["ok"],
                "properties": {"ok": {"const": True}},
            },
        )

        result = orch._execute_agent(
            AgentCapability.REVIEW,
            malformed.provider_id,
            handoff,
            package,
        )
        verdict, findings = orch._review_result(package, result)

        assert (verdict, findings) == ("approved", [])
        assert malformed.call_count == 2
        assert healthy.call_count == 1
        assert malformed.executions[0].expected_output_schema["required"] == [
            "verdict",
            "findings",
            "observations",
            "summary",
        ]
        repair_context = malformed.executions[1].execution_context
        assert repair_context["format_repair"] is True
        assert repair_context["original_stage"] == "final_review"
        assert repair_context["selected_provider"]["agent_id"] == "qwen-malformed"
        assert malformed.executions[1].requirements == []
        assert malformed.executions[1].attempt_history == []
        assert malformed.executions[1].workflow_skills == []
        assert "previous-invalid-response.txt" in malformed.executions[1].bounded_excerpts
        assert "Do not inspect repositories" in malformed.executions[1].summary
        history = list(reversed(orch.invocation_history(package_id=package.id, limit=10)))
        assert [item["status"] for item in history] == [
            "failed",
            "failed",
            "completed",
        ]
        assert history[0]["failure"]["classification"] == "invalid_output"
        assert "changes_required requires at least one" in history[0]["failure"]["error"]
        events = orch._journal.read()
        assert any(event.event_type == "agent_failover" for event in events)
        assert not any(
            event.event_type == "human_intervention_required" for event in events
        )

    def test_observed_findions_review_reaches_fix_cycle_without_human_escalation(
        self, tmp_path
    ):
        config = OrchestrationConfig(
            strict_checks=True,
            auto_commit=False,
            state_dir=tmp_path / "state",
        )
        orch = ProjectOrchestrator("findions-review", config=config)
        reviewer = _StructuredResultAdapter(
            "qwen-9b",
            (
                '{"ok":true,"verdict":"changes_required",'
                '"findions":["F-1 | HIGH | transport validation missing"],'
                '"findings":[],"summary":"one blocker"}'
            ),
            {AgentCapability.REVIEW},
        )
        orch.register_agent(reviewer)
        package = WorkPackage(
            id="WP17__WP17-S4",
            title="Transport client",
            stage=WorkPackageStage.FINAL_REVIEW,
        )
        handoff = StructuredHandoff(
            work_package_id=package.id,
            stage="final_review",
            summary="Final review",
        )

        result = orch._execute_agent(
            AgentCapability.REVIEW,
            reviewer.provider_id,
            handoff,
            package,
        )
        verdict, findings = orch._review_result(package, result)

        assert verdict == "changes_required"
        assert findings == ["F-1 | HIGH | transport validation missing"]
        assert package.last_review["findings"] == findings
        assert not any(
            event.event_type == "human_intervention_required"
            for event in orch._journal.read()
        )

    def test_semantically_malformed_review_exhaustion_waits_instead_of_escalating(
        self, tmp_path
    ):
        config = OrchestrationConfig(
            strict_checks=True,
            auto_commit=False,
            state_dir=tmp_path / "state",
            max_agent_attempts_per_stage=1,
        )
        orch = ProjectOrchestrator("semantic-wait", config=config)
        malformed = _StructuredResultAdapter(
            "only-reviewer",
            '{"verdict":"changes_required","findings":[],"summary":"blocked"}',
            {AgentCapability.REVIEW},
        )
        orch.register_agent(malformed)
        package = WorkPackage(
            id="WP17__WP17-S4",
            title="Transport client",
            stage=WorkPackageStage.FINAL_REVIEW,
        )
        orch.transition_to(TaskExecutionState.VALIDATING_PLAN)
        orch.transition_to(TaskExecutionState.RUNNING)

        with pytest.raises(_AgentWaitRequested):
            orch._execute_agent(
                AgentCapability.REVIEW,
                malformed.provider_id,
                StructuredHandoff(
                    work_package_id=package.id,
                    stage="final_review",
                    summary="Final review",
                ),
                package,
            )

        assert orch.state == TaskExecutionState.WAITING_FOR_AGENT
        history = orch.invocation_history(package_id=package.id, limit=10)
        assert [item["status"] for item in history] == ["failed", "failed"]
        assert history[0]["failure"]["classification"] == "invalid_output"
        events = orch._journal.read()
        assert any(event.event_type == "agent_wait_scheduled" for event in events)
        assert not any(
            event.event_type in {
                "agent_invocation_completed",
                "human_intervention_required",
            }
            for event in events
        )

    def test_observed_approval_with_findings_is_rejected(
        self, tmp_path
    ):
        config = OrchestrationConfig(
            strict_checks=True,
            auto_commit=False,
            state_dir=tmp_path / "state",
            max_agent_attempts_per_stage=1,
        )
        orch = ProjectOrchestrator("object-review", config=config)
        reviewer = _StructuredResultAdapter(
            "qwen-30b",
            (
                '{"findings":[{"id":"note-1","severity":"low",'
                '"requirement":"constructor is initialized",'
                '"required_fix":"No fix needed."}],'
                '"verdict":{"decision":"approved","reason":"Header is correct."}}'
            ),
            {AgentCapability.REVIEW},
        )
        orch.register_agent(reviewer)
        package = WorkPackage(
            id="WP17__WP17-S4",
            title="Transport client",
            stage=WorkPackageStage.FINAL_REVIEW,
        )

        orch._state_record.state = TaskExecutionState.RUNNING
        with pytest.raises(_AgentWaitRequested):
            orch._execute_agent(
                AgentCapability.REVIEW,
                reviewer.provider_id,
                StructuredHandoff(
                    work_package_id=package.id,
                    stage="final_review",
                    summary="Final review",
                ),
                package,
            )

        assert orch.invocation_history(package_id=package.id)[0]["status"] == "failed"

    def test_structured_contract_can_be_recovered_from_provider_candidate_tail(
        self, tmp_path
    ):
        class _CandidateAdapter(AgentAdapter):
            @property
            def provider_id(self):
                return "opencode"

            @property
            def availability(self):
                return Availability.AVAILABLE

            @property
            def capabilities(self):
                return {AgentCapability.FIX_REVIEW}

            def execute(self, handoff):
                return {
                    "ok": True,
                    "work_package_id": handoff.work_package_id,
                    "final_message": "Finished the repair.",
                    "structured_output_candidates": [
                        '{"ok":true,"status":"fixed","summary":"done"}',
                        "Finished the repair.",
                    ],
                }

        config = OrchestrationConfig(
            strict_checks=True,
            auto_commit=False,
            state_dir=tmp_path / "state",
        )
        orch = ProjectOrchestrator("candidate-tail", config=config)
        adapter = _CandidateAdapter()
        orch.register_agent(adapter)
        package = WorkPackage(id="WP17-S4", title="Repair")
        handoff = StructuredHandoff(
            work_package_id=package.id,
            stage="supervisor_delegate_fix_review",
            summary="Repair",
            expected_output_schema={
                "type": "object",
                "required": ["ok", "status", "summary"],
                "properties": {
                    "ok": {"const": True},
                    "status": {"enum": ["fixed"]},
                    "summary": {"type": "string", "minLength": 1},
                },
            },
        )

        result = orch._execute_agent(
            AgentCapability.FIX_REVIEW,
            adapter.provider_id,
            handoff,
            package,
        )

        assert result["_execraft_structured_payload"]["status"] == "fixed"

    def test_waits_for_agent_after_exhausting_failover_budget(
        self, tmp_path
    ):
        progress: list[tuple[str, dict]] = []
        config = OrchestrationConfig(strict_checks=False, auto_commit=False,
            state_dir=tmp_path / "state", max_agent_attempts_per_stage=2
        )
        orch = ProjectOrchestrator(
            "test-project",
            config=config,
            progress_callback=lambda event, payload: progress.append(
                (event, dict(payload))
            ),
        )
        first = _FlakyAgentAdapter("first", {AgentCapability.IMPLEMENT}, fail_times=999)
        second = _FlakyAgentAdapter("second", {AgentCapability.IMPLEMENT}, fail_times=999)
        reviewer = _FakeAgentAdapter("rev", {AgentCapability.REVIEW})
        orch.register_agent(first)
        orch.register_agent(second)
        orch.register_agent(reviewer)

        orch.initialize("## Package: Only\n\n### Acceptance criteria\n- [ ] Works\n")
        orch.run_pipeline()

        assert orch.state == TaskExecutionState.WAITING_FOR_AGENT
        report = orch.status_report()
        assert report["completed_packages"] == 0
        assert report["human_required"] is None
        assert report["waiting"]["package_id"] == "only"
        assert report["waiting"]["capability"] == "implement"
        assert len(report["waiting"]["attempts"]) == 2
        events = orch._journal.read()
        assert any(e.event_type == "agent_wait_scheduled" for e in events)
        waiting_event = next(
            payload for event, payload in progress if event == "agent_waiting"
        )
        assert waiting_event["next_retry_agent_id"] == "first"
        assert waiting_event["next_retry_at"]
        assert waiting_event["first_reported_unblock_agent_id"] == ""
        assert waiting_event["all_deadlines_known"] is True
        assert not any(
            e.event_type == "human_intervention_required" for e in events
        )
        # Budget of 2 means exactly 2 agents were tried, not all registered
        # implement-capable ones tried indefinitely.
        assert first.call_count == 1
        assert second.call_count == 1

    def test_missing_agent_for_stage_still_completes_without_escalating(
        self, tmp_path
    ):
        # Unchanged legacy behavior: a stage with *no* capability-having
        # agent registered at all is a different situation from one where
        # every registered agent actually failed at runtime, and must not
        # trigger HUMAN_REQUIRED.
        config = OrchestrationConfig(strict_checks=False, auto_commit=False, state_dir=tmp_path / "state")
        orch = ProjectOrchestrator("test-project", config=config)
        reviewer = _FakeAgentAdapter("rev", {AgentCapability.REVIEW})
        orch.register_agent(reviewer)

        orch.initialize("## Package: Only\n\n### Acceptance criteria\n- [ ] Works\n")
        orch.run_pipeline()

        assert orch.state == TaskExecutionState.COMPLETED
        assert "human_intervention_required" not in self._event_types(orch)


class TestRotatingSchedulingAndWorkStealing:
    @staticmethod
    def _graph(*packages: WorkPackage) -> tuple[PlanGraph, NormalizationReport]:
        return (
            PlanGraph(work_packages=list(packages)),
            NormalizationReport(packages_found=len(packages)),
        )

    @staticmethod
    def _package(package_id: str, repository: str) -> WorkPackage:
        return WorkPackage(
            id=package_id,
            title=package_id.title(),
            requirements=["Implement the package"],
            acceptance_criteria=[
                AcceptanceCriterion(id=f"AC-{package_id}", description="Works")
            ],
            affected_repositories=[repository],
        )

    def test_implementers_rotate_across_packages(self, tmp_path):
        config = OrchestrationConfig(
            strict_checks=False,
            auto_commit=False,
            state_dir=tmp_path / "state",
        )
        orch = ProjectOrchestrator(
            "rotation",
            config=config,
            repository_paths={
                "repo-a": _repo(tmp_path / "repo-a"),
                "repo-b": _repo(tmp_path / "repo-b"),
            },
            workspace_root=tmp_path,
        )
        first = _FakeAgentAdapter(
            "codex",
            {AgentCapability.IMPLEMENT, AgentCapability.REVIEW, AgentCapability.FIX_REVIEW},
        )
        second = _FakeAgentAdapter(
            "claude",
            {AgentCapability.IMPLEMENT, AgentCapability.REVIEW, AgentCapability.FIX_REVIEW},
        )
        final = _FakeAgentAdapter("zen", {AgentCapability.REVIEW})
        orch.register_agent(first)
        orch.register_agent(second)
        orch.register_agent(final)
        graph, report = self._graph(
            self._package("first", "repo-a"),
            self._package("second", "repo-b"),
        )
        orch.initialize_graph(graph, report)

        orch.run_pipeline()

        assert orch.state == TaskExecutionState.COMPLETED
        first_package = orch._state_record.plan_graph.package_by_id("first")
        second_package = orch._state_record.plan_graph.package_by_id("second")
        assert first_package.agent_id == "codex"
        assert second_package.agent_id == "claude"
        assert first_package.agent_history["implement"] == ["codex"]
        assert second_package.agent_history["implement"] == ["claude"]
        assert orch.status_report()["scheduler"]["agent_cursor"]["implement"] == "claude"

    def test_failover_records_actual_implementer_and_rebalances_review(self, tmp_path):
        config = OrchestrationConfig(
            strict_checks=False,
            auto_commit=False,
            state_dir=tmp_path / "state",
        )
        orch = ProjectOrchestrator(
            "failover-attribution",
            config=config,
            repository_paths={"repo-a": _repo(tmp_path / "repo-a")},
            workspace_root=tmp_path,
        )
        failing = _FlakyAgentAdapter(
            "codex", {AgentCapability.IMPLEMENT}, fail_times=999
        )
        replacement = _FakeAgentAdapter(
            "claude", {AgentCapability.IMPLEMENT, AgentCapability.REVIEW}
        )
        reviewer = _FakeAgentAdapter("zen", {AgentCapability.REVIEW})
        orch.register_agent(failing)
        orch.register_agent(replacement)
        orch.register_agent(reviewer)
        graph, report = self._graph(self._package("only", "repo-a"))
        orch.initialize_graph(graph, report)

        orch.run_pipeline()

        package = orch._state_record.plan_graph.package_by_id("only")
        assert orch.state == TaskExecutionState.COMPLETED
        assert package.agent_id == "claude"
        assert package.reviewer_id != "claude"
        assert package.agent_history["implement"] == ["claude"]
        assert any(
            event.event_type == "agent_assignments_rebalanced"
            for event in orch._journal.read()
        )

    def test_waiting_package_does_not_block_disjoint_ready_package(self, tmp_path):
        config = OrchestrationConfig(
            strict_checks=False,
            auto_commit=False,
            state_dir=tmp_path / "state",
            max_agent_attempts_per_stage=2,
            allow_cross_package_progress=True,
        )
        orch = ProjectOrchestrator(
            "work-stealing",
            config=config,
            repository_paths={
                "repo-a": _repo(tmp_path / "repo-a"),
                "repo-b": _repo(tmp_path / "repo-b"),
            },
            workspace_root=tmp_path,
        )
        first_worker = _PackageSelectiveAgent(
            "codex",
            {AgentCapability.IMPLEMENT},
            failing_packages={"first"},
        )
        second_worker = _PackageSelectiveAgent(
            "claude",
            {AgentCapability.IMPLEMENT},
            failing_packages={"first"},
        )
        reviewer = _FakeAgentAdapter("reviewer", {AgentCapability.REVIEW})
        orch.register_agent(first_worker)
        orch.register_agent(second_worker)
        orch.register_agent(reviewer)
        graph, report = self._graph(
            self._package("first", "repo-a"),
            self._package("second", "repo-b"),
        )
        orch.initialize_graph(graph, report)

        orch.run_pipeline()

        first_package = orch._state_record.plan_graph.package_by_id("first")
        second_package = orch._state_record.plan_graph.package_by_id("second")
        assert orch.state == TaskExecutionState.WAITING_FOR_AGENT
        assert first_package.stage == WorkPackageStage.IMPLEMENT
        assert second_package.stage == WorkPackageStage.COMPLETED
        assert "second" in first_worker.executions + second_worker.executions
        assert "first" in orch.status_report()["agent_waits"]
        events = orch._journal.read()
        assert any(
            event.event_type == "package_requeued_for_agent"
            and event.payload["package_id"] == "first"
            for event in events
        )

    def test_waiting_package_blocks_overlapping_repository_scope(self, tmp_path):
        config = OrchestrationConfig(
            strict_checks=False,
            auto_commit=False,
            state_dir=tmp_path / "state",
            max_agent_attempts_per_stage=2,
            allow_cross_package_progress=True,
        )
        orch = ProjectOrchestrator("scope-lock", config=config)
        first_worker = _PackageSelectiveAgent(
            "codex",
            {AgentCapability.IMPLEMENT},
            failing_packages={"first"},
        )
        second_worker = _PackageSelectiveAgent(
            "claude",
            {AgentCapability.IMPLEMENT},
            failing_packages={"first"},
        )
        reviewer = _FakeAgentAdapter("reviewer", {AgentCapability.REVIEW})
        orch.register_agent(first_worker)
        orch.register_agent(second_worker)
        orch.register_agent(reviewer)
        graph, report = self._graph(
            self._package("first", "shared-repo"),
            self._package("second", "shared-repo"),
        )
        orch.initialize_graph(graph, report)

        orch.run_pipeline()

        second_package = orch._state_record.plan_graph.package_by_id("second")
        assert orch.state == TaskExecutionState.WAITING_FOR_AGENT
        assert second_package.stage == WorkPackageStage.PREPARE
        assert "second" not in first_worker.executions + second_worker.executions


class TestDeterministicVerificationGating:
    def _event_types(self, orch):
        return [e.event_type for e in orch._journal.read()]

    def _orchestrator(self, tmp_path, *, registry=None, runner=None, config=None, repositories=None):
        cfg = config or OrchestrationConfig(strict_checks=False, auto_commit=False, state_dir=tmp_path / "state")
        orch = ProjectOrchestrator(
            "test-project",
            config=cfg,
            registry=registry,
            command_runner=runner,
            repository_paths=repositories,
            workspace_root=tmp_path if repositories else None,
        )
        orch.register_agent(
            _FakeAgentAdapter("solo", {AgentCapability.IMPLEMENT, AgentCapability.REVIEW})
        )
        return orch

    def test_no_commands_configured_skips_verification(self, tmp_path):
        orch = self._orchestrator(tmp_path)
        orch.initialize("## Package: Only\n\n### Acceptance criteria\n- [ ] Works\n")
        orch.run_pipeline()

        assert orch.state == TaskExecutionState.COMPLETED
        assert "verification_skipped" in self._event_types(orch)

    def test_passing_verification_command_allows_completion(self, tmp_path):
        registry = VerificationRegistry(
            commands=[VerificationCommand(command="pytest -q", profile=CHEAP)]
        )
        runner = _ScriptedCommandRunner([0])
        orch = self._orchestrator(tmp_path, registry=registry, runner=runner)
        orch.initialize("## Package: Only\n\n### Acceptance criteria\n- [ ] Works\n")
        orch.run_pipeline()

        assert orch.state == TaskExecutionState.COMPLETED
        assert orch.status_report()["completed_packages"] == 1
        assert runner.calls == ["pytest -q"]
        run_events = [
            e for e in orch._journal.read() if e.event_type == "verification_command_run"
        ]
        assert len(run_events) == 1
        assert run_events[0].payload["status"] == "passed"

    def test_failing_verification_retries_and_then_succeeds(self, tmp_path):
        registry = VerificationRegistry(
            commands=[VerificationCommand(command="pytest -q", profile=CHEAP)]
        )
        runner = _ScriptedCommandRunner([1, 1, 0])
        orch = self._orchestrator(tmp_path, registry=registry, runner=runner)
        implementer = orch._agent_slots[0]
        orch.initialize("## Package: Only\n\n### Acceptance criteria\n- [ ] Works\n")
        orch.run_pipeline()

        assert orch.state == TaskExecutionState.COMPLETED
        assert orch.status_report()["completed_packages"] == 1
        assert len(runner.calls) == 3
        implement_calls = [
            h for h in implementer.executions if h.stage == "implement"
        ]
        assert len(implement_calls) == 3  # re-implemented on each failed verify
        retry_context = implement_calls[1].execution_context["verification"]
        assert retry_context["status"] == "failed"
        assert retry_context["commands"][0]["command"] == "pytest -q"
        assert "verification-failure.json" in implement_calls[1].bounded_excerpts
        review_calls = [h for h in implementer.executions if h.stage == "review"]
        assert len(review_calls) == 1  # only reviewed once, after verification passed
        failed_events = [
            e for e in orch._journal.read() if e.event_type == "verification_failed"
        ]
        assert [e.payload["attempt"] for e in failed_events] == [1, 2]
        assert "human_intervention_required" not in self._event_types(orch)

    def test_ready_queue_is_recomputed_before_starting_another_package(self, tmp_path):
        registry = VerificationRegistry(
            commands=[VerificationCommand(command="pytest -q", profile=CHEAP)]
        )
        # First package fails once, then passes; the second package passes.
        runner = _ScriptedCommandRunner([1, 0, 0])
        orch = self._orchestrator(tmp_path, registry=registry, runner=runner)
        orch.initialize(
            "## Package: First\n\n### Acceptance criteria\n- [ ] Works\n\n"
            "## Package: Second\n\n### Acceptance criteria\n- [ ] Works\n"
        )

        orch.run_pipeline()

        assert orch.state == TaskExecutionState.COMPLETED
        events = orch._journal.read()
        first_completed = next(
            index
            for index, event in enumerate(events)
            if event.event_type == "package_completed"
            and event.payload["package_id"] == "first"
        )
        second_started = next(
            index
            for index, event in enumerate(events)
            if event.event_type == "package_started"
            and event.payload["package_id"] == "second"
        )
        assert first_completed < second_started

    def test_verification_environment_preserves_host_values_and_overlays_workspace(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HOME", "/tmp/execraft-test-home")
        captured_environment = {}

        def runner(command, *, cwd, timeout, env):
            captured_environment.update(env)
            return _FakeCompletedProcess(0, stdout="ok")

        registry = VerificationRegistry(
            commands=[VerificationCommand(command="verify", profile=CHEAP)]
        )
        config = OrchestrationConfig(
            strict_checks=False, auto_commit=False, state_dir=tmp_path / "state"
        )
        orch = ProjectOrchestrator(
            "test-project",
            config=config,
            registry=registry,
            command_runner=runner,
            verification_environment={"EXECRAFT_REPO_CORE": "/workspace/core"},
        )
        orch.register_agent(
            _FakeAgentAdapter(
                "solo", {AgentCapability.IMPLEMENT, AgentCapability.REVIEW}
            )
        )
        orch.initialize("## Package: Only\n\n### Acceptance criteria\n- [ ] Works\n")

        orch.run_pipeline()

        assert orch.state == TaskExecutionState.COMPLETED
        assert captured_environment["HOME"] == "/tmp/execraft-test-home"
        assert captured_environment["EXECRAFT_REPO_CORE"] == "/workspace/core"

    def test_verification_failure_environment_error_is_not_reported_as_passing(
        self, tmp_path
    ):
        registry = VerificationRegistry(
            commands=[VerificationCommand(command="pytest -q", profile=CHEAP)]
        )
        runner = _ScriptedCommandRunner(["raise", 0])
        orch = self._orchestrator(tmp_path, registry=registry, runner=runner)
        orch.initialize("## Package: Only\n\n### Acceptance criteria\n- [ ] Works\n")
        orch.run_pipeline()

        assert orch.state == TaskExecutionState.COMPLETED
        run_events = [
            e for e in orch._journal.read() if e.event_type == "verification_command_run"
        ]
        assert run_events[0].payload["status"] == "environment_failure"
        assert run_events[0].payload["status"] != "passed"

    def test_verification_exhausts_budget_and_escalates_to_human_required(
        self, tmp_path
    ):
        registry = VerificationRegistry(
            commands=[VerificationCommand(command="pytest -q", profile=CHEAP)]
        )
        runner = _ScriptedCommandRunner([1])  # always fails
        config = OrchestrationConfig(strict_checks=False, auto_commit=False,
            state_dir=tmp_path / "state", max_verification_attempts=2
        )
        orch = self._orchestrator(tmp_path, registry=registry, runner=runner, config=config)
        orch.initialize("## Package: Only\n\n### Acceptance criteria\n- [ ] Works\n")
        orch.run_pipeline()

        assert orch.state == TaskExecutionState.HUMAN_REQUIRED
        assert orch.status_report()["completed_packages"] == 0
        assert len(runner.calls) == 2  # bounded by max_verification_attempts, not infinite
        escalation = next(
            e for e in orch._journal.read() if e.event_type == "human_intervention_required"
        )
        assert escalation.payload["package_id"] == "only"
        assert escalation.payload["stage"] == "verification"
        assert escalation.payload["blocked_requirement"]
        assert escalation.payload["bounded_options"]
        assert escalation.payload["recommended_decision"]

    def test_repository_sync_skips_repository_specific_gates_for_noop_repositories(
        self, tmp_path, monkeypatch
    ):
        from types import SimpleNamespace
        from execraft.orchestrate.models import WorkPackageKind
        from execraft.repository_sync.spec import RepositorySyncSpec

        registry = VerificationRegistry(
            commands=[
                VerificationCommand(command="global-check", profile=CHEAP),
                VerificationCommand(
                    command="core-check", profile=CHEAP, repository_id="core"
                ),
                VerificationCommand(
                    command="worker-check", profile=CHEAP, repository_id="worker"
                ),
            ]
        )
        runner = _ScriptedCommandRunner([0, 0])
        orch = self._orchestrator(tmp_path, registry=registry, runner=runner)
        package = WorkPackage(
            id="WP21-SYNC",
            title="Sync",
            kind=WorkPackageKind.REPOSITORY_SYNC,
            repository_sync=RepositorySyncSpec.from_mapping(
                {"repositories": ["core", "worker"]}
            ),
            affected_repositories=["core", "worker"],
            requirements=["sync"],
            acceptance_criteria=[AcceptanceCriterion(id="A1", description="Done")],
            verification_profile="cheap",
        )
        orch.initialize_graph(PlanGraph([package]), NormalizationReport())
        package.stage = WorkPackageStage.FAST_VERIFY
        package.status = "pending"
        monkeypatch.setattr(orch, "_resolve_repo_path", lambda _repo: tmp_path)

        marked = []
        transaction = SimpleNamespace(
            repositories=[
                SimpleNamespace(repository_id="core", status="merge_ready"),
                SimpleNamespace(repository_id="worker", status="noop"),
            ]
        )
        service = SimpleNamespace(
            transactions=SimpleNamespace(load=lambda _package_id: transaction),
            mark_verified=lambda package_id: marked.append(package_id),
        )
        monkeypatch.setattr(orch, "_repository_sync_service", lambda: service)

        assert orch._run_verification(package) is True
        assert runner.calls == ["global-check", "core-check"]
        assert marked == ["WP21-SYNC"]
        scope = next(
            event
            for event in orch._journal.read()
            if event.event_type == "repository_sync_verification_scope"
        )
        assert scope.payload["skipped_noop_repositories"] == ["worker"]

    def test_repository_sync_regression_failure_routes_to_fix_review(
        self, tmp_path, monkeypatch
    ):
        from types import SimpleNamespace
        from execraft.orchestrate.models import WorkPackageKind
        from execraft.repository_sync.spec import RepositorySyncSpec

        registry = VerificationRegistry(
            commands=[VerificationCommand(command="global-check", profile=CHEAP)]
        )
        runner = _ScriptedCommandRunner([1])
        config = OrchestrationConfig(
            strict_checks=False,
            auto_commit=False,
            state_dir=tmp_path / "state",
            max_verification_attempts=2,
        )
        orch = self._orchestrator(
            tmp_path, registry=registry, runner=runner, config=config
        )
        package = WorkPackage(
            id="WP21-SYNC",
            title="Sync",
            kind=WorkPackageKind.REPOSITORY_SYNC,
            repository_sync=RepositorySyncSpec.from_mapping({"repositories": ["core"]}),
            affected_repositories=["core"],
            requirements=["sync"],
            acceptance_criteria=[AcceptanceCriterion(id="A1", description="Done")],
            verification_profile="cheap",
        )
        orch.initialize_graph(PlanGraph([package]), NormalizationReport())
        package.stage = WorkPackageStage.REGRESSION_VERIFY
        package.status = "pending"
        transaction = SimpleNamespace(
            repositories=[SimpleNamespace(repository_id="core", status="resolved")]
        )
        service = SimpleNamespace(
            transactions=SimpleNamespace(load=lambda _package_id: transaction)
        )
        monkeypatch.setattr(orch, "_repository_sync_service", lambda: service)

        assert orch._run_verification(package) is False
        assert package.stage == WorkPackageStage.FIX_REVIEW
        assert package.review_findings
        assert "global-check" in package.review_findings[0]

    def test_repository_sync_obsolete_verification_incident_resumes_without_supervisor(
        self, tmp_path, monkeypatch
    ):
        from types import SimpleNamespace
        from execraft.orchestrate.models import WorkPackageKind
        from execraft.orchestrate.supervisor import IncidentClass, IncidentStatus, SupervisorIncident
        from execraft.repository_sync.spec import RepositorySyncSpec

        registry = VerificationRegistry(
            commands=[
                VerificationCommand(command="global-check", profile=CHEAP),
                VerificationCommand(
                    command="worker-check", profile=CHEAP, repository_id="worker"
                ),
            ]
        )
        orch = self._orchestrator(tmp_path, registry=registry)
        package = WorkPackage(
            id="WP21-SYNC", title="Sync", kind=WorkPackageKind.REPOSITORY_SYNC,
            repository_sync=RepositorySyncSpec.from_mapping(
                {"repositories": ["core", "worker"]}
            ),
            affected_repositories=["core", "worker"],
            acceptance_criteria=[AcceptanceCriterion(id="A1", description="Done")],
            verification_profile="cheap",
        )
        orch.initialize_graph(PlanGraph([package]), NormalizationReport())
        package.stage = WorkPackageStage.REGRESSION_VERIFY
        package.status = "pending"
        package.verification_attempts = 3
        package.last_verification = {
            "status": "failed",
            "failed_commands": [{"command": "worker-check", "status": "failed"}],
        }
        transaction = SimpleNamespace(
            repositories=[
                SimpleNamespace(repository_id="core", status="resolved"),
                SimpleNamespace(repository_id="worker", status="noop"),
            ]
        )
        service = SimpleNamespace(
            transactions=SimpleNamespace(load=lambda _package_id: transaction)
        )
        monkeypatch.setattr(orch, "_repository_sync_service", lambda: service)
        incident = SupervisorIncident(
            incident_id="incident-1", fingerprint="fingerprint",
            package_id=package.id, stage="verification",
            classification=IncidentClass.TEST_FAILURE, status=IncidentStatus.OPEN,
        )
        orch._supervisor_incidents.save(incident)
        orch.transition_to(TaskExecutionState.RUNNING)
        orch.transition_to(TaskExecutionState.WAITING_FOR_AGENT)

        assert orch._supervisor_coordinator.resume_obsolete_repository_sync_verification()
        assert orch.state == TaskExecutionState.RUNNING
        assert orch._supervisor_incidents.active() is None
        assert package.verification_attempts == 0
        event = next(
            item for item in orch._journal.read()
            if item.event_type == "repository_sync_verification_incident_superseded"
        )
        assert event.payload["skipped_noop_repositories"] == ["worker"]

    def test_repository_sync_fast_verify_repair_uses_verification_handoff_not_conflict_handoff(
        self, tmp_path, monkeypatch
    ):
        from types import SimpleNamespace
        from execraft.orchestrate.models import WorkPackageKind
        from execraft.repository_sync.spec import RepositorySyncSpec

        orch = self._orchestrator(tmp_path)
        package = WorkPackage(
            id="WP21-SYNC",
            title="Sync",
            kind=WorkPackageKind.REPOSITORY_SYNC,
            repository_sync=RepositorySyncSpec.from_mapping({"repositories": ["core"]}),
            affected_repositories=["core"],
            requirements=["sync"],
            acceptance_criteria=[AcceptanceCriterion(id="A1", description="Done")],
            stage=WorkPackageStage.IMPLEMENT,
            agent_id="solo",
            reviewer_id="solo",
            last_verification={
                "status": "failed",
                "failed_commands": [
                    {
                        "command": "core-check",
                        "status": "failed",
                        "relevant_excerpt": "one regression",
                    }
                ],
            },
        )
        orch.initialize_graph(PlanGraph([package]), NormalizationReport())
        package.stage = WorkPackageStage.IMPLEMENT
        package.status = "pending"
        item = SimpleNamespace(
            repository_id="core",
            status="merge_ready",
            conflict_paths=[],
            remote="origin",
            source_branch="master",
            source_commit="a" * 40,
            target_after="",
        )
        item.as_mapping = lambda: {
            "repository_id": item.repository_id,
            "status": item.status,
        }
        transaction = SimpleNamespace(
            transaction_id="sync-test", repositories=[item]
        )
        accepted = transaction
        service = SimpleNamespace(
            transactions=SimpleNamespace(load=lambda _package_id: transaction),
            accept_resolution=lambda _package_id: accepted,
        )
        monkeypatch.setattr(orch, "_repository_sync_service", lambda: service)
        transaction.as_mapping = lambda: {
            "transaction_id": transaction.transaction_id,
            "repositories": [item.as_mapping()],
        }
        verification_handoff = orch._repository_sync_resolution_handoff(package, transaction)
        assert verification_handoff.stage == "repository_sync_verification_repair"
        assert "core-check" in verification_handoff.unresolved_findings[0]
        monkeypatch.setattr(
            orch, "_repository_sync_resolution_handoff",
            lambda _package, _transaction: verification_handoff,
        )
        seen = []
        monkeypatch.setattr(
            orch,
            "_call_prebuilt_handoff",
            lambda _cap, _agent, _package, handoff: seen.append(handoff)
            or {"ok": True},
        )
        monkeypatch.setattr(orch, "_apply_implementation_result", lambda *_args: None)

        orch._process_repository_sync_pre_stages(package)

        assert seen == [verification_handoff]
        assert package.stage == WorkPackageStage.FAST_VERIFY

    def test_commands_deduplicated_across_multiple_affected_repositories(self, tmp_path):
        registry = VerificationRegistry(
            commands=[
                VerificationCommand(command="global-check", profile=CHEAP),
                VerificationCommand(command="repo-a-check", profile=CHEAP, repository_id="repo-a"),
            ]
        )
        runner = _ScriptedCommandRunner([0, 0])
        orch = self._orchestrator(
            tmp_path,
            registry=registry,
            runner=runner,
            repositories={
                "repo-a": _repo(tmp_path / "repo-a"),
                "repo-b": _repo(tmp_path / "repo-b"),
            },
        )
        orch.initialize(
            "## Package: Only\n\nAffected repositories: repo-a, repo-b\n\n"
            "### Acceptance criteria\n- [ ] Works\n"
        )
        orch.run_pipeline()

        assert orch.state == TaskExecutionState.COMPLETED
        # "global-check" applies to both repos but must run once, not twice.
        assert sorted(runner.calls) == ["global-check", "repo-a-check"]

    def test_invalid_structured_output_references_full_agent_artifact(self, tmp_path):
        config = OrchestrationConfig(
            strict_checks=True,
            auto_commit=False,
            state_dir=tmp_path / "state",
        )
        orch = ProjectOrchestrator("test-project", config=config)
        reviewer = _LongInvalidReviewAdapter()
        orch.register_agent(reviewer)
        package = WorkPackage(id="WP10", title="Provider routing", stage=WorkPackageStage.REVIEW)
        handoff = StructuredHandoff(
            work_package_id=package.id,
            stage="review",
            summary="Review WP10",
        )

        orch.transition_to(TaskExecutionState.VALIDATING_PLAN)
        orch.transition_to(TaskExecutionState.RUNNING)
        with pytest.raises(_AgentWaitRequested):
            orch._execute_agent(
                AgentCapability.REVIEW,
                reviewer.provider_id,
                handoff,
                package,
            )

        failure = next(
            entry
            for entry in orch._journal.read()
            if entry.event_type == "agent_failure"
        )
        artifact = failure.payload["artifact"]
        artifact_path = __import__("pathlib").Path(artifact["path"])
        stored = __import__("json").loads(artifact_path.read_text(encoding="utf-8"))
        assert len(stored["result"]["final_message"]) > 6000
        assert artifact["size_bytes"] == artifact_path.stat().st_size
        assert failure.payload["classification"] == "invalid_output"
        assert not any(
            entry.event_type == "human_intervention_required"
            for entry in orch._journal.read()
        )


class TestKnownFailurePolicy:
    def _event_types(self, orch):
        return [e.event_type for e in orch._journal.read()]

    def _orchestrator(self, tmp_path, *, registry, runner, config=None):
        cfg = config or OrchestrationConfig(strict_checks=False, auto_commit=False, state_dir=tmp_path / "state")
        orch = ProjectOrchestrator(
            "test-project", config=cfg, registry=registry, command_runner=runner
        )
        orch.register_agent(
            _FakeAgentAdapter("solo", {AgentCapability.IMPLEMENT, AgentCapability.REVIEW})
        )
        return orch

    def test_environment_blocked_known_failure_does_not_block_completion(self, tmp_path):
        registry = VerificationRegistry(
            commands=[VerificationCommand(command="needs-gpu-test", profile=CHEAP)],
            known_failures=[
                KnownFailure(
                    test_identifier="needs-gpu-test",
                    reason="no GPU available in this environment",
                    environment_blocked=True,
                )
            ],
        )
        runner = _ScriptedCommandRunner([1])  # always fails
        orch = self._orchestrator(tmp_path, registry=registry, runner=runner)
        orch.initialize("## Package: Only\n\n### Acceptance criteria\n- [ ] Works\n")
        orch.run_pipeline()

        assert orch.state == TaskExecutionState.COMPLETED
        assert orch.status_report()["completed_packages"] == 1
        events = self._event_types(orch)
        assert "known_failure_matched" in events
        # Never silently reported as passing: the raw command result is
        # still journaled as failed even though it didn't block completion.
        run_event = next(
            e for e in orch._journal.read() if e.event_type == "verification_command_run"
        )
        assert run_event.payload["status"] == "failed"

    def test_known_failure_without_environment_blocked_still_blocks(self, tmp_path):
        registry = VerificationRegistry(
            commands=[VerificationCommand(command="flaky-test", profile=CHEAP)],
            known_failures=[
                KnownFailure(
                    test_identifier="flaky-test",
                    reason="tracked flaky test, not yet fixed",
                    environment_blocked=False,
                )
            ],
        )
        runner = _ScriptedCommandRunner([1])  # always fails
        config = OrchestrationConfig(strict_checks=False, auto_commit=False,
            state_dir=tmp_path / "state", max_verification_attempts=1
        )
        orch = self._orchestrator(tmp_path, registry=registry, runner=runner, config=config)
        orch.initialize("## Package: Only\n\n### Acceptance criteria\n- [ ] Works\n")
        orch.run_pipeline()

        assert orch.state == TaskExecutionState.HUMAN_REQUIRED
        assert orch.status_report()["completed_packages"] == 0
        assert "known_failure_matched" in self._event_types(orch)

    def test_expired_known_failure_is_not_matched(self, tmp_path):
        registry = VerificationRegistry(
            commands=[VerificationCommand(command="needs-gpu-test", profile=CHEAP)],
            known_failures=[
                KnownFailure(
                    test_identifier="needs-gpu-test",
                    reason="stale entry",
                    environment_blocked=True,
                    expires_at="2020-01-01T00:00:00",
                )
            ],
        )
        runner = _ScriptedCommandRunner([1])  # always fails
        config = OrchestrationConfig(strict_checks=False, auto_commit=False,
            state_dir=tmp_path / "state", max_verification_attempts=1
        )
        orch = self._orchestrator(tmp_path, registry=registry, runner=runner, config=config)
        orch.initialize("## Package: Only\n\n### Acceptance criteria\n- [ ] Works\n")
        orch.run_pipeline()

        # An expired known-failure entry must not exempt the package from
        # blocking, even though it's marked environment_blocked.
        assert orch.state == TaskExecutionState.HUMAN_REQUIRED
        assert "known_failure_matched" not in self._event_types(orch)

    def test_policy_can_disable_the_environment_blocked_exemption(self, tmp_path):
        registry = VerificationRegistry(
            commands=[VerificationCommand(command="needs-gpu-test", profile=CHEAP)],
            known_failures=[
                KnownFailure(
                    test_identifier="needs-gpu-test",
                    reason="no GPU available",
                    environment_blocked=True,
                )
            ],
        )
        runner = _ScriptedCommandRunner([1])  # always fails
        config = OrchestrationConfig(strict_checks=False, auto_commit=False,
            state_dir=tmp_path / "state",
            max_verification_attempts=1,
            allow_known_environment_blocked_failures=False,
        )
        orch = self._orchestrator(tmp_path, registry=registry, runner=runner, config=config)
        orch.initialize("## Package: Only\n\n### Acceptance criteria\n- [ ] Works\n")
        orch.run_pipeline()

        assert orch.state == TaskExecutionState.HUMAN_REQUIRED
        assert "known_failure_matched" in self._event_types(orch)


class TestFinalReviewStage:
    def _event_types(self, orch):
        return [e.event_type for e in orch._journal.read()]

    def test_third_distinct_agent_performs_a_separate_final_review(self, tmp_path):
        config = OrchestrationConfig(strict_checks=False, auto_commit=False, state_dir=tmp_path / "state")
        orch = ProjectOrchestrator("test-project", config=config)
        implementer = _FakeAgentAdapter("impl", {AgentCapability.IMPLEMENT})
        reviewer = _FakeAgentAdapter("rev", {AgentCapability.REVIEW})
        final_reviewer = _FakeAgentAdapter("final-rev", {AgentCapability.REVIEW})
        orch.register_agent(implementer)
        orch.register_agent(reviewer)
        orch.register_agent(final_reviewer)

        orch.initialize("## Package: Only\n\n### Acceptance criteria\n- [ ] Works\n")
        orch.run_pipeline()

        assert orch.state == TaskExecutionState.COMPLETED
        assert orch.status_report()["completed_packages"] == 1
        assert len(implementer.executions) == 1
        assert len(reviewer.executions) == 1
        assert len(final_reviewer.executions) == 1
        assert final_reviewer.executions[0].stage == "final_review"
        assert "final_review_skipped" not in self._event_types(orch)

        package = orch._state_record.plan_graph.package_by_id("only")
        assert package.agent_id == "impl"
        assert package.reviewer_id == "rev"
        assert package.final_reviewer_id == "final-rev"

    def test_primary_reviewer_performs_final_review_without_third_provider(self, tmp_path):
        config = OrchestrationConfig(strict_checks=False, auto_commit=False, state_dir=tmp_path / "state")
        orch = ProjectOrchestrator("test-project", config=config)
        implementer = _FakeAgentAdapter("impl", {AgentCapability.IMPLEMENT})
        reviewer = _FakeAgentAdapter("rev", {AgentCapability.REVIEW})
        orch.register_agent(implementer)
        orch.register_agent(reviewer)

        orch.initialize("## Package: Only\n\n### Acceptance criteria\n- [ ] Works\n")
        orch.run_pipeline()

        assert orch.state == TaskExecutionState.COMPLETED
        assert "final_review_skipped" not in self._event_types(orch)
        assert len(reviewer.executions) == 2
        assert reviewer.executions[-1].stage == "final_review"
        package = orch._state_record.plan_graph.package_by_id("only")
        assert package.final_reviewer_id == "rev"

    def test_final_reviewer_failure_enters_waiting_state(
        self, tmp_path
    ):
        config = OrchestrationConfig(strict_checks=False, auto_commit=False,
            state_dir=tmp_path / "state", max_agent_attempts_per_stage=1
        )
        orch = ProjectOrchestrator("test-project", config=config)
        implementer = _FakeAgentAdapter("impl", {AgentCapability.IMPLEMENT})
        reviewer = _FakeAgentAdapter("rev", {AgentCapability.REVIEW})
        crashing_final = _FlakyAgentAdapter(
            "final-rev", {AgentCapability.REVIEW}, fail_times=999
        )
        orch.register_agent(implementer)
        orch.register_agent(reviewer)
        orch.register_agent(crashing_final)

        orch.initialize("## Package: Only\n\n### Acceptance criteria\n- [ ] Works\n")
        orch.run_pipeline()

        assert orch.state == TaskExecutionState.WAITING_FOR_AGENT
        waiting = orch.status_report()["waiting"]
        assert waiting["package_id"] == "only"
        assert waiting["stage"] == "final_review"
        assert waiting["capability"] == "review"
        assert not any(
            e.event_type == "human_intervention_required"
            for e in orch._journal.read()
        )


class TestMidPackageCrashResume:
    """`_process_package()` used to unconditionally restart every package
    from IMPLEMENT regardless of how far it had actually gotten, even
    though `package.stage` was already being persisted at each step —
    meaning a real process crash mid-package silently redid already-done
    (and, for a real agent, already-paid-for) implement/review work on
    restart. These tests simulate a crash by persisting state exactly as a
    real interrupted process would leave it, then constructing a brand
    new ProjectOrchestrator that only calls load_state() — proving resume
    is driven entirely by durable state, the same standard the daemon
    restart tests already hold run_pipeline() to at the *project* level,
    now applied at the *work-package* level too."""

    def _register_three_agents(self, orch, impl, rev, final):
        orch.register_agent(impl)
        orch.register_agent(rev)
        orch.register_agent(final)

    def test_resume_after_implement_does_not_re_run_the_implementer(self, tmp_path):
        config = OrchestrationConfig(strict_checks=False, auto_commit=False, state_dir=tmp_path / "state")
        first = ProjectOrchestrator("crash-test", config=config)
        impl1 = _FakeAgentAdapter("impl", {AgentCapability.IMPLEMENT})
        rev1 = _FakeAgentAdapter("rev", {AgentCapability.REVIEW})
        final1 = _FakeAgentAdapter("final-rev", {AgentCapability.REVIEW})
        self._register_three_agents(first, impl1, rev1, final1)
        first.initialize("## Package: Only\n\n### Acceptance criteria\n- [ ] Works\n")
        first.transition_to(TaskExecutionState.RUNNING)

        package = first._state_record.plan_graph.package_by_id("only")
        # Simulate a crash exactly after IMPLEMENT finished and its result
        # was durably persisted (package.stage saved as FAST_VERIFY),
        # before FAST_VERIFY itself ran.
        package.stage = WorkPackageStage.FAST_VERIFY
        package.agent_id = "impl"
        package.reviewer_id = "rev"
        package.final_reviewer_id = "final-rev"
        first.transition_to(TaskExecutionState.WAITING_FOR_AGENT)
        first.save_state()

        second = ProjectOrchestrator("crash-test", config=config)
        impl2 = _FakeAgentAdapter("impl", {AgentCapability.IMPLEMENT})
        rev2 = _FakeAgentAdapter("rev", {AgentCapability.REVIEW})
        final2 = _FakeAgentAdapter("final-rev", {AgentCapability.REVIEW})
        self._register_three_agents(second, impl2, rev2, final2)
        second.load_state()
        assert second.state == TaskExecutionState.WAITING_FOR_AGENT

        second.run_pipeline()

        assert second.state == TaskExecutionState.COMPLETED
        assert second.status_report()["completed_packages"] == 1
        assert len(impl2.executions) == 0  # never re-implemented
        assert len(rev2.executions) == 1
        assert len(final2.executions) == 1

    def test_resume_after_review_does_not_re_run_implement_or_review(self, tmp_path):
        config = OrchestrationConfig(strict_checks=False, auto_commit=False, state_dir=tmp_path / "state")
        first = ProjectOrchestrator("crash-test", config=config)
        impl1 = _FakeAgentAdapter("impl", {AgentCapability.IMPLEMENT})
        rev1 = _FakeAgentAdapter("rev", {AgentCapability.REVIEW})
        final1 = _FakeAgentAdapter("final-rev", {AgentCapability.REVIEW})
        self._register_three_agents(first, impl1, rev1, final1)
        first.initialize("## Package: Only\n\n### Acceptance criteria\n- [ ] Works\n")
        first.transition_to(TaskExecutionState.RUNNING)

        package = first._state_record.plan_graph.package_by_id("only")
        # Simulate a crash after both IMPLEMENT and REVIEW completed and
        # were persisted (stage saved as FINAL_REVIEW), before the final
        # review itself ran.
        package.stage = WorkPackageStage.FINAL_REVIEW
        package.agent_id = "impl"
        package.reviewer_id = "rev"
        package.final_reviewer_id = "final-rev"
        first.transition_to(TaskExecutionState.WAITING_FOR_AGENT)
        first.save_state()

        second = ProjectOrchestrator("crash-test", config=config)
        impl2 = _FakeAgentAdapter("impl", {AgentCapability.IMPLEMENT})
        rev2 = _FakeAgentAdapter("rev", {AgentCapability.REVIEW})
        final2 = _FakeAgentAdapter("final-rev", {AgentCapability.REVIEW})
        self._register_three_agents(second, impl2, rev2, final2)
        second.load_state()

        second.run_pipeline()

        assert second.state == TaskExecutionState.COMPLETED
        assert len(impl2.executions) == 0
        assert len(rev2.executions) == 0
        assert len(final2.executions) == 1
        assert final2.executions[0].stage == "final_review"

    def test_resume_from_running_state_also_works(self, tmp_path):
        # Not every crash happens mid-agent-call: one landing between two
        # stages' processing persists plain RUNNING, not WAITING_FOR_AGENT.
        config = OrchestrationConfig(strict_checks=False, auto_commit=False, state_dir=tmp_path / "state")
        first = ProjectOrchestrator("crash-test", config=config)
        impl1 = _FakeAgentAdapter("impl", {AgentCapability.IMPLEMENT})
        rev1 = _FakeAgentAdapter("rev", {AgentCapability.REVIEW})
        self._register_three_agents(first, impl1, rev1, _FakeAgentAdapter("f", set()))
        first.initialize("## Package: Only\n\n### Acceptance criteria\n- [ ] Works\n")
        first.transition_to(TaskExecutionState.RUNNING)

        package = first._state_record.plan_graph.package_by_id("only")
        package.stage = WorkPackageStage.REVIEW
        package.agent_id = "impl"
        package.reviewer_id = "rev"
        first.save_state()  # state left as RUNNING, not WAITING_FOR_AGENT

        second = ProjectOrchestrator("crash-test", config=config)
        impl2 = _FakeAgentAdapter("impl", {AgentCapability.IMPLEMENT})
        rev2 = _FakeAgentAdapter("rev", {AgentCapability.REVIEW})
        second.register_agent(impl2)
        second.register_agent(rev2)
        second.load_state()
        assert second.state == TaskExecutionState.RUNNING

        second.run_pipeline()

        assert second.state == TaskExecutionState.COMPLETED
        assert len(impl2.executions) == 0
        assert len(rev2.executions) == 2
        assert rev2.executions[-1].stage == "final_review"

    def test_resume_after_committed_transaction_does_not_duplicate_the_commit(
        self, tmp_path
    ):
        config = OrchestrationConfig(strict_checks=False, auto_commit=False, state_dir=tmp_path / "state")
        first = ProjectOrchestrator("crash-test", config=config)
        first.register_agent(
            _FakeAgentAdapter("solo", {AgentCapability.IMPLEMENT, AgentCapability.REVIEW})
        )
        first.initialize("## Package: Only\n\n### Acceptance criteria\n- [ ] Works\n")
        first.transition_to(TaskExecutionState.RUNNING)

        package = first._state_record.plan_graph.package_by_id("only")
        # Simulate a crash after the commit transaction was already
        # committed but before the package was marked COMPLETED.
        package.stage = WorkPackageStage.READY_TO_COMMIT
        tx = first._begin_commit_transaction(package)
        first._commit_journal.commit(tx.transaction_id)
        first.save_state()

        second = ProjectOrchestrator("crash-test", config=config)
        second.register_agent(
            _FakeAgentAdapter("solo", {AgentCapability.IMPLEMENT, AgentCapability.REVIEW})
        )
        second.load_state()

        second.run_pipeline()

        assert second.state == TaskExecutionState.COMPLETED
        committed = [
            t for t in second._commit_journal._all() if t.status == "committed"
        ]
        assert len(committed) == 1  # not duplicated
        assert committed[0].transaction_id == tx.transaction_id

    def test_resume_after_interrupted_pending_transaction_marks_it_failed_and_retries(
        self, tmp_path
    ):
        config = OrchestrationConfig(strict_checks=False, auto_commit=False, state_dir=tmp_path / "state")
        first = ProjectOrchestrator("crash-test", config=config)
        first.register_agent(
            _FakeAgentAdapter("solo", {AgentCapability.IMPLEMENT, AgentCapability.REVIEW})
        )
        first.initialize("## Package: Only\n\n### Acceptance criteria\n- [ ] Works\n")
        first.transition_to(TaskExecutionState.RUNNING)

        package = first._state_record.plan_graph.package_by_id("only")
        # Simulate a crash between _begin_commit_transaction() and
        # _commit_journal.commit(): a transaction exists but was
        # never marked committed.
        package.stage = WorkPackageStage.READY_TO_COMMIT
        abandoned_tx = first._begin_commit_transaction(package)
        first.save_state()

        second = ProjectOrchestrator("crash-test", config=config)
        second.register_agent(
            _FakeAgentAdapter("solo", {AgentCapability.IMPLEMENT, AgentCapability.REVIEW})
        )
        second.load_state()

        second.run_pipeline()

        assert second.state == TaskExecutionState.COMPLETED
        all_tx = second._commit_journal._all()
        abandoned = next(
            t for t in all_tx if t.transaction_id == abandoned_tx.transaction_id
        )
        assert abandoned.status == "failed"
        committed = [t for t in all_tx if t.status == "committed"]
        assert len(committed) == 1
        assert committed[0].transaction_id != abandoned_tx.transaction_id


class TestHumanRequiredDiagnostics:
    def test_status_report_joins_escalation_with_agent_artifact(self, tmp_path):
        config = OrchestrationConfig(
            strict_checks=True,
            auto_commit=False,
            state_dir=tmp_path / "state",
        )
        orch = ProjectOrchestrator("sample_task", config=config)
        orch._state_record.state = TaskExecutionState.HUMAN_REQUIRED
        orch._state_record.plan_graph = PlanGraph(
            work_packages=[
                WorkPackage(
                    id="WP10",
                    title="Runtime-neutral execution",
                    stage=WorkPackageStage.FINAL_REVIEW,
                )
            ]
        )
        artifact = {
            "path": str(tmp_path / "result.json"),
            "sha256": "abc123",
            "size_bytes": 4321,
        }
        orch._journal.append(
            "agent_result_persisted",
            {
                "package_id": "WP10",
                "stage": "final_review",
                "capability": "review",
                "agent_id": "opencode-zen-free",
                "adapter": "opencode",
                "model": "opencode/deepseek-v4-flash-free",
                "artifact": artifact,
            },
        )
        orch._journal.append(
            "human_intervention_required",
            {
                "package_id": "WP10",
                "stage": "review",
                "blocked_requirement": "agent returned invalid structured output",
                "evidence": ["structured contract was not satisfied"],
                "agent_output_artifact": artifact,
                "recommended_decision": "inspect the artifact and resume",
            },
        )

        context = orch.status_report()["human_required"]

        assert context["package_id"] == "WP10"
        assert context["package_title"] == "Runtime-neutral execution"
        assert context["stage"] == "final_review"
        assert context["reason"] == "agent returned invalid structured output"
        assert context["agent"] == {
            "id": "opencode-zen-free",
            "adapter": "opencode",
            "model": "opencode/deepseek-v4-flash-free",
            "capability": "review",
        }
        assert context["artifact"] == artifact
        assert context["recommended_decision"] == "inspect the artifact and resume"

    def test_status_report_omits_stale_escalation_after_resume(self, tmp_path):
        config = OrchestrationConfig(
            strict_checks=False,
            auto_commit=False,
            state_dir=tmp_path / "state",
        )
        orch = ProjectOrchestrator("task", config=config)
        orch._journal.append(
            "human_intervention_required",
            {"blocked_requirement": "old failure"},
        )
        orch._state_record.state = TaskExecutionState.RUNNING

        assert orch.status_report()["human_required"] is None


class _TypedFailureAdapter(AgentAdapter):
    def __init__(self, provider_id: str):
        self._id = provider_id
        self.call_count = 0

    @property
    def availability(self) -> Availability:
        return Availability.AVAILABLE

    @property
    def provider_id(self) -> str:
        return self._id

    @property
    def capabilities(self) -> set[AgentCapability]:
        return {AgentCapability.REVIEW}

    def execute(self, handoff: StructuredHandoff) -> dict:
        from execraft.orchestrate.scheduler import AgentExecutionError

        self.call_count += 1
        raise AgentExecutionError(
            "monthly quota reached",
            classification="quota_exhausted",
            retry_after_seconds=3600,
            persistent=True,
            artifact_payload={"stderr": "monthly quota reached"},
        )


class _DatedQuotaFailureAdapter(AgentAdapter):
    def __init__(self, provider_id: str):
        self._id = provider_id
        self.call_count = 0

    @property
    def availability(self) -> Availability:
        return Availability.AVAILABLE

    @property
    def provider_id(self) -> str:
        return self._id

    @property
    def capabilities(self) -> set[AgentCapability]:
        return {AgentCapability.IMPLEMENT}

    def execute(self, handoff: StructuredHandoff) -> dict:
        from execraft.orchestrate.scheduler import AgentExecutionError

        self.call_count += 1
        raise AgentExecutionError(
            "usage limit until next week",
            classification="quota_exhausted",
            retry_after_seconds=6 * 24 * 3600,
            persistent=True,
        )


def test_provider_quota_is_persisted_and_skipped_on_next_attempt(tmp_path):
    primary = _TypedFailureAdapter("opencode-go")
    fallback = _FakeAgentAdapter("fallback", {AgentCapability.REVIEW})
    orch = ProjectOrchestrator(
        "provider-health",
        config=OrchestrationConfig(
            state_dir=tmp_path / "state",
            strict_checks=False,
            max_agent_attempts_per_stage=3,
        ),
    )
    orch.register_agent(primary)
    orch.register_agent(fallback)
    package = WorkPackage(id="WP1", title="Review", affected_repositories=[])
    handoff = StructuredHandoff(
        work_package_id="WP1", stage="review", summary="review"
    )

    result = orch._execute_agent(AgentCapability.REVIEW, "opencode-go", handoff, package)

    assert result["ok"] is True
    assert primary.call_count == 1
    assert len(fallback.executions) == 1
    health = orch.agent_status()[0]["provider_health"]
    assert health["status"] == "cooldown"
    assert health["reason"] == "quota_exhausted"
    assert health["unavailable_until"]

    # A subsequent request that still names the persisted primary agent skips
    # it without spending another provider call and immediately uses fallback.
    result = orch._execute_agent(AgentCapability.REVIEW, "opencode-go", handoff, package)
    assert result["ok"] is True
    assert primary.call_count == 1
    assert len(fallback.executions) == 2


def test_known_quota_deadline_uses_long_poll_without_relaunching_cli(tmp_path):
    primary = _DatedQuotaFailureAdapter("codex")
    config = OrchestrationConfig(
        state_dir=tmp_path / "state",
        strict_checks=True,
        auto_commit=False,
        require_verification=False,
        require_acceptance_evidence=False,
        require_repository_changes=False,
        agent_wait_poll_max_seconds=300,
        agent_known_deadline_poll_max_seconds=1800,
    )
    orch = ProjectOrchestrator("dated-quota", config=config)
    orch.register_agent(primary)
    orch.initialize("## Package: Only\n\n### Acceptance criteria\n- [ ] Works\n")
    package = orch._state_record.plan_graph.package_by_id("only")
    package.stage = WorkPackageStage.IMPLEMENT
    orch.save_state()

    orch.run_pipeline()
    first_wait = orch.status_report()["waiting"]

    assert primary.call_count == 1
    assert first_wait["poll_after_seconds"] == pytest.approx(1800)
    assert first_wait["candidates"][0]["reason"] == "quota_exhausted"
    assert first_wait["candidates"][0]["available_at"]

    # A scheduler poll before the deadline consults provider health and does
    # not spend another Codex invocation.
    orch.run_pipeline()
    assert primary.call_count == 1


class _NonPersistentPermissionFailureAdapter(AgentAdapter):
    def __init__(self, provider_id: str):
        self._id = provider_id
        self.call_count = 0

    @property
    def availability(self) -> Availability:
        return Availability.AVAILABLE

    @property
    def provider_id(self) -> str:
        return self._id

    @property
    def capabilities(self) -> set[AgentCapability]:
        return {AgentCapability.REVIEW}

    def execute(self, handoff: StructuredHandoff) -> dict:
        from execraft.orchestrate.scheduler import AgentExecutionError

        self.call_count += 1
        raise AgentExecutionError(
            "opencode auto-rejected permission todowrite for *",
            classification="permission_required",
            persistent=False,
        )


def test_non_persistent_permission_failure_fails_over_without_blocking_provider(tmp_path):
    primary = _NonPersistentPermissionFailureAdapter("opencode-zen-free")
    fallback = _FakeAgentAdapter("fallback", {AgentCapability.REVIEW})
    orch = ProjectOrchestrator(
        "provider-health-permission",
        config=OrchestrationConfig(
            state_dir=tmp_path / "state",
            strict_checks=False,
            max_agent_attempts_per_stage=3,
        ),
    )
    orch.register_agent(primary)
    orch.register_agent(fallback)
    package = WorkPackage(id="WP1", title="Review", affected_repositories=[])
    handoff = StructuredHandoff(
        work_package_id="WP1", stage="review", summary="review"
    )

    result = orch._execute_agent(
        AgentCapability.REVIEW, "opencode-zen-free", handoff, package
    )

    assert result["ok"] is True
    assert primary.call_count == 1
    assert len(fallback.executions) == 1
    health = next(
        item["provider_health"]
        for item in orch.agent_status()
        if item["provider_id"] == "opencode-zen-free"
    )
    assert health["status"] == "available"


def test_strict_orchestrator_without_compatible_agent_waits_instead_of_escalating(tmp_path):
    config = OrchestrationConfig(
        strict_checks=True,
        auto_commit=False,
        state_dir=tmp_path / "state",
        require_verification=False,
        require_acceptance_evidence=False,
        require_repository_changes=False,
    )
    orch = ProjectOrchestrator("missing-agent-wait", config=config)
    orch.initialize("## Package: Only\n\n### Acceptance criteria\n- [ ] Works\n")
    package = orch._state_record.plan_graph.package_by_id("only")
    package.stage = WorkPackageStage.IMPLEMENT
    orch.save_state()

    orch.run_pipeline()

    report = orch.status_report()
    assert orch.state == TaskExecutionState.WAITING_FOR_AGENT
    assert report["human_required"] is None
    assert report["waiting"]["capability"] == "implement"
    assert report["waiting"]["candidates"][0]["reason"] == "no_compatible_agent_configured"


def test_project_state_wait_metadata_round_trips(tmp_path):
    state = TaskExecutionStateRecord(
        project_id="wait-roundtrip",
        state=TaskExecutionState.WAITING_FOR_AGENT,
        waiting={
            "package_id": "M10A",
            "stage": "final_review",
            "next_check_at": "2026-07-22T18:00:00+00:00",
        },
    )

    restored = TaskExecutionStateRecord.from_mapping(state.as_mapping())

    assert restored.state == TaskExecutionState.WAITING_FOR_AGENT
    assert restored.waiting == state.waiting


def test_unavailable_adapter_uses_bounded_poll_instead_of_busy_loop(tmp_path):
    adapter = _FakeAgentAdapter(
        "offline",
        {AgentCapability.IMPLEMENT},
        avail=Availability.BUSY,
    )
    config = OrchestrationConfig(
        strict_checks=True,
        auto_commit=False,
        state_dir=tmp_path / "state",
        require_verification=False,
        require_acceptance_evidence=False,
        require_repository_changes=False,
        agent_wait_poll_max_seconds=240.0,
    )
    orch = ProjectOrchestrator("offline-agent-wait", config=config)
    orch.register_agent(adapter)
    orch.initialize("## Package: Only\n\n### Acceptance criteria\n- [ ] Works\n")
    package = orch._state_record.plan_graph.package_by_id("only")
    package.stage = WorkPackageStage.IMPLEMENT
    orch.save_state()

    orch.run_pipeline()

    waiting = orch.status_report()["waiting"]
    assert waiting["poll_after_seconds"] == pytest.approx(240.0)
    assert waiting["candidates"][0]["reason"] == "adapter_busy"


class TestFixerFallbackPolicy:
    def test_reserved_final_reviewer_can_fix_when_primary_reviewer_stays_independent(
        self, tmp_path
    ):
        config = OrchestrationConfig(
            strict_checks=False,
            auto_commit=False,
            state_dir=tmp_path / "state",
        )
        orch = ProjectOrchestrator("test-project", config=config)
        orch.register_agent(
            _FakeAgentAdapter(
                "codex",
                {AgentCapability.IMPLEMENT, AgentCapability.FIX_REVIEW},
                Availability.QUOTA_EXHAUSTED,
            )
        )
        orch.register_agent(
            _FakeAgentAdapter(
                "opencode-zen-free",
                {AgentCapability.REVIEW},
            )
        )
        orch.register_agent(
            _FakeAgentAdapter(
                "claude-code",
                {AgentCapability.REVIEW, AgentCapability.FIX_REVIEW},
            )
        )
        package = WorkPackage(
            id="WP14",
            title="UI integration",
            stage=WorkPackageStage.FIX_REVIEW,
            agent_id="codex",
            reviewer_id="opencode-zen-free",
            final_reviewer_id="claude-code",
        )

        selection = orch._select_fixer(package)

        assert selection.agent_id == "claude-code"
        assert selection.policy == "release_final_reviewer_for_fix"
        assert selection.excluded_agent_ids == frozenset({"opencode-zen-free"})

    def test_reviewer_can_fix_as_last_resort_with_independent_final_reviewer(
        self, tmp_path
    ):
        config = OrchestrationConfig(
            strict_checks=False,
            auto_commit=False,
            state_dir=tmp_path / "state",
        )
        orch = ProjectOrchestrator("test-project", config=config)
        codex = _FakeAgentAdapter(
            "codex",
            {AgentCapability.IMPLEMENT, AgentCapability.FIX_REVIEW},
            Availability.DISABLED,
        )
        claude = _ResettableFakeAgentAdapter(
            "claude-code",
            {AgentCapability.REVIEW, AgentCapability.FIX_REVIEW},
        )
        zen = _FakeAgentAdapter("opencode-zen-free", {AgentCapability.REVIEW})
        orch.register_agent(codex)
        orch.register_agent(claude)
        orch.register_agent(zen)

        package = WorkPackage(
            id="WP11",
            title="Planner",
            stage=WorkPackageStage.FIX_REVIEW,
            agent_id="codex",
            reviewer_id="claude-code",
            final_reviewer_id="opencode-zen-free",
        )

        selection = orch._select_fixer(package)

        assert selection.agent_id == "claude-code"
        assert selection.policy == "reviewer_self_fix_with_independent_final"
        assert selection.excluded_agent_ids == frozenset({"opencode-zen-free"})
        events = [
            entry
            for entry in orch._journal.read()
            if entry.event_type == "fixer_independence_relaxed"
        ]
        assert len(events) == 1
        assert events[0].payload["agent_id"] == "claude-code"

    def test_transient_adapter_status_is_released_after_health_recovers(
        self, tmp_path
    ):
        config = OrchestrationConfig(
            strict_checks=False,
            auto_commit=False,
            state_dir=tmp_path / "state",
        )
        orch = ProjectOrchestrator("test-project", config=config)
        claude = _ResettableFakeAgentAdapter(
            "claude-code",
            {AgentCapability.FIX_REVIEW},
            Availability.SESSION_LIMIT,
        )
        orch.register_agent(claude)
        orch._provider_health.mark_available("claude-code")

        selected = orch._select_agent_for_capability(AgentCapability.FIX_REVIEW)

        assert selected == "claude-code"
        assert claude.availability == Availability.AVAILABLE

    def test_reviewer_self_fix_is_allowed_in_single_provider_mode(
        self, tmp_path
    ):
        config = OrchestrationConfig(
            strict_checks=False,
            auto_commit=False,
            state_dir=tmp_path / "state",
        )
        orch = ProjectOrchestrator("test-project", config=config)
        orch.register_agent(
            _FakeAgentAdapter(
                "codex",
                {AgentCapability.IMPLEMENT, AgentCapability.FIX_REVIEW},
                Availability.DISABLED,
            )
        )
        orch.register_agent(
            _FakeAgentAdapter(
                "claude-code",
                {AgentCapability.REVIEW, AgentCapability.FIX_REVIEW},
            )
        )
        package = WorkPackage(
            id="WP11",
            title="Planner",
            stage=WorkPackageStage.FIX_REVIEW,
            agent_id="codex",
            reviewer_id="claude-code",
        )

        selection = orch._select_fixer(package)

        assert selection.agent_id == "claude-code"
        assert selection.policy == "reuse_provider_for_fix_review"

    def test_register_agent_repairs_legacy_invalid_model_without_pinned_model(
        self, tmp_path
    ):
        config = OrchestrationConfig(
            strict_checks=False,
            auto_commit=False,
            state_dir=tmp_path / "state",
        )
        orch = ProjectOrchestrator("test-project", config=config)
        orch._provider_health.mark_failure(
            "codex",
            reason="invalid_model",
            detail="legacy false positive",
            persistent=True,
        )
        assert not orch._provider_health.get("codex").is_available

        orch.register_agent(
            _FakeAgentAdapter(
                "codex",
                {AgentCapability.IMPLEMENT, AgentCapability.FIX_REVIEW},
            )
        )

        assert orch._provider_health.get("codex").is_available
        events = [
            entry
            for entry in orch._journal.read()
            if entry.event_type == "provider_health_repaired"
        ]
        assert len(events) == 1

    def test_register_agent_repairs_model_block_once_config_declares_model(
        self, tmp_path
    ):
        config = OrchestrationConfig(
            strict_checks=False,
            auto_commit=False,
            state_dir=tmp_path / "state",
        )
        orch = ProjectOrchestrator("test-project", config=config)
        orch._provider_health.mark_failure(
            "opencode-ollama-local-coder",
            reason="invalid_model",
            detail="configured model is unavailable",
            persistent=True,
        )
        adapter = _PinnedModelAgentAdapter(
            "opencode-ollama-local-coder",
            {AgentCapability.IMPLEMENT},
            model="ollama-local/qwen3-coder:30b-32k",
            declares_model=True,
        )

        orch.register_agent(adapter)

        assert orch._provider_health.get("opencode-ollama-local-coder").is_available

    def test_register_agent_keeps_model_block_when_config_lacks_model(
        self, tmp_path
    ):
        config = OrchestrationConfig(
            strict_checks=False,
            auto_commit=False,
            state_dir=tmp_path / "state",
        )
        orch = ProjectOrchestrator("test-project", config=config)
        orch._provider_health.mark_failure(
            "opencode-ollama-local-coder",
            reason="invalid_model",
            detail="configured model is unavailable",
            persistent=True,
        )
        adapter = _PinnedModelAgentAdapter(
            "opencode-ollama-local-coder",
            {AgentCapability.IMPLEMENT},
            model="ollama-local/qwen3-coder:30b-32k",
            declares_model=False,
        )

        orch.register_agent(adapter)

        health = orch._provider_health.get("opencode-ollama-local-coder")
        assert not health.is_available
        assert health.reason == "invalid_model"


def test_fix_review_failover_relaxes_from_blocked_implementer_to_reviewer(
    tmp_path,
):
    config = OrchestrationConfig(
        strict_checks=False,
        auto_commit=False,
        state_dir=tmp_path / "state",
        max_agent_attempts_per_stage=3,
    )
    orch = ProjectOrchestrator("test-project", config=config)
    codex = _FlakyAgentAdapter(
        "codex",
        {AgentCapability.IMPLEMENT, AgentCapability.FIX_REVIEW},
        fail_times=999,
        error_message="simulated codex stall",
    )
    claude = _FakeAgentAdapter(
        "claude-code",
        {AgentCapability.REVIEW, AgentCapability.FIX_REVIEW},
    )
    zen = _FakeAgentAdapter("opencode-zen-free", {AgentCapability.REVIEW})
    orch.register_agent(codex)
    orch.register_agent(claude)
    orch.register_agent(zen)
    package = WorkPackage(
        id="WP11",
        title="Planner",
        stage=WorkPackageStage.FIX_REVIEW,
        agent_id="codex",
        reviewer_id="claude-code",
        final_reviewer_id="opencode-zen-free",
        review_findings=["fix this"],
    )
    selection = orch._select_fixer(package)
    assert selection.agent_id == "codex"
    assert selection.policy == "independent"

    result = orch._execute_agent(
        AgentCapability.FIX_REVIEW,
        selection.agent_id,
        StructuredHandoff(
            work_package_id="WP11",
            stage="fix_review",
            summary="Fix findings",
        ),
        package,
        excluded_agent_ids=set(selection.excluded_agent_ids),
        fallback_exclusion_tiers=[
            (policy, set(excluded))
            for policy, excluded in selection.fallback_tiers
        ],
    )

    assert result["_execraft_executed_by"] == "claude-code"
    assert codex.call_count == 1
    assert len(claude.executions) == 1
    relaxed = [
        entry
        for entry in orch._journal.read()
        if entry.event_type == "fixer_independence_relaxed"
    ]
    assert relaxed[-1].payload["policy"] == "reviewer_self_fix_with_independent_final"
    failovers = [
        entry
        for entry in orch._journal.read()
        if entry.event_type == "agent_failover"
    ]
    assert failovers[-1].payload["from_agent"] == "codex"
    assert failovers[-1].payload["to_agent"] == "claude-code"


def test_high_complexity_package_uses_strongest_available_agents(tmp_path):
    config = OrchestrationConfig(state_dir=tmp_path / "state")
    config.prefer_capable_agents = True
    orchestrator = ProjectOrchestrator("weighted", config=config)
    weak = _FakeAgentAdapter(
        "weak", {AgentCapability.IMPLEMENT, AgentCapability.REVIEW, AgentCapability.FIX_REVIEW}
    )
    strong = _FakeAgentAdapter(
        "strong", {AgentCapability.IMPLEMENT, AgentCapability.REVIEW, AgentCapability.FIX_REVIEW}
    )
    reviewer = _FakeAgentAdapter("reviewer", {AgentCapability.REVIEW})
    weak._execraft_capability_weight = 55
    strong._execraft_capability_weight = 100
    reviewer._execraft_capability_weight = 90
    orchestrator.register_agent(weak)
    orchestrator.register_agent(strong)
    orchestrator.register_agent(reviewer)
    orchestrator._set_rotation_cursor(AgentCapability.IMPLEMENT, "strong")

    package = WorkPackage(id="hard", title="Hard", complexity=95)
    schedule = orchestrator._schedule_agents(package)

    assert schedule.implementer_id == "strong"
    assert schedule.reviewer_id == "reviewer"



def test_fallback_only_promotion_preserves_normally_eligible_provider(tmp_path):
    config = OrchestrationConfig(state_dir=tmp_path / "state")
    config.prefer_capable_agents = True
    orchestrator = ProjectOrchestrator("promotion-fallback", config=config)
    promoted = _FakeAgentAdapter("qwen", {AgentCapability.REVIEW})
    normal = _FakeAgentAdapter("claude", {AgentCapability.REVIEW})
    promoted._execraft_capability_weight = 100
    promoted._execraft_max_complexity_by_capability = {AgentCapability.REVIEW: 75}
    normal._execraft_capability_weight = 90
    normal._execraft_max_complexity_by_capability = {AgentCapability.REVIEW: 100}
    orchestrator.register_agent(promoted)
    orchestrator.register_agent(normal)
    package = WorkPackage(id="WP23", title="Runtime acceptance", complexity=97)
    orchestrator._provider_promotions.promote(
        "qwen",
        "review",
        base_max_complexity=75,
        promoted_max_complexity=100,
        duration_seconds=3600,
        fallback_only=True,
    )

    selected = orchestrator._select_agent_for_capability(
        AgentCapability.REVIEW, package=package
    )

    assert selected == "claude"


def test_fallback_only_promotion_unlocks_provider_after_normal_pool_is_unavailable(tmp_path):
    config = OrchestrationConfig(state_dir=tmp_path / "state")
    config.prefer_capable_agents = True
    orchestrator = ProjectOrchestrator("promotion-unblock", config=config)
    promoted = _FakeAgentAdapter("qwen", {AgentCapability.REVIEW})
    blocked = _FakeAgentAdapter(
        "claude", {AgentCapability.REVIEW}, Availability.QUOTA_EXHAUSTED
    )
    promoted._execraft_max_complexity_by_capability = {AgentCapability.REVIEW: 75}
    blocked._execraft_max_complexity_by_capability = {AgentCapability.REVIEW: 100}
    orchestrator.register_agent(promoted)
    orchestrator.register_agent(blocked)
    package = WorkPackage(id="WP23", title="Runtime acceptance", complexity=97)

    # Simulate a GUI/CLI process writing the task-local promotion after this
    # orchestrator was already constructed and waiting for an eligible agent.
    external_store = ProviderPromotionStore(
        orchestrator._state_dir / "provider-promotions.json"
    )
    external_store.promote(
        "qwen",
        "review",
        base_max_complexity=75,
        promoted_max_complexity=100,
        duration_seconds=3600,
        fallback_only=True,
        reason="premium providers blocked",
    )

    selected = orchestrator._select_agent_for_capability(
        AgentCapability.REVIEW, package=package
    )

    assert selected == "qwen"
    status = next(
        item for item in orchestrator.agent_status() if item["provider_id"] == "qwen"
    )
    assert status["max_complexity_by_capability"]["review"] == 75
    assert status["effective_max_complexity_by_capability"]["review"] == 100


def test_immediate_promotion_does_not_activate_other_fallback_only_promotions(tmp_path):
    config = OrchestrationConfig(state_dir=tmp_path / "state")
    config.prefer_capable_agents = True
    orchestrator = ProjectOrchestrator("promotion-isolation", config=config)
    fallback = _FakeAgentAdapter("fallback", {AgentCapability.REVIEW})
    immediate = _FakeAgentAdapter("immediate", {AgentCapability.REVIEW})
    fallback._execraft_capability_weight = 100
    immediate._execraft_capability_weight = 80
    for adapter in (fallback, immediate):
        adapter._execraft_max_complexity_by_capability = {AgentCapability.REVIEW: 75}
        orchestrator.register_agent(adapter)
    package = WorkPackage(id="WP23", title="Runtime acceptance", complexity=97)
    orchestrator._provider_promotions.promote(
        "fallback",
        "review",
        base_max_complexity=75,
        promoted_max_complexity=100,
        duration_seconds=3600,
        fallback_only=True,
    )
    orchestrator._provider_promotions.promote(
        "immediate",
        "review",
        base_max_complexity=75,
        promoted_max_complexity=100,
        duration_seconds=3600,
        fallback_only=False,
    )

    selected = orchestrator._select_agent_for_capability(
        AgentCapability.REVIEW, package=package
    )

    assert selected == "immediate"


def test_review_promotion_requires_explicit_opt_in_for_final_review(tmp_path):
    config = OrchestrationConfig(state_dir=tmp_path / "state")
    orchestrator = ProjectOrchestrator("promotion-final-review", config=config)
    qwen = _FakeAgentAdapter("qwen", {AgentCapability.REVIEW})
    qwen._execraft_max_complexity_by_capability = {AgentCapability.REVIEW: 75}
    orchestrator.register_agent(qwen)
    package = WorkPackage(
        id="WP23",
        title="Runtime acceptance",
        complexity=97,
        stage=WorkPackageStage.FINAL_REVIEW,
    )
    orchestrator._provider_promotions.promote(
        "qwen",
        "review",
        base_max_complexity=75,
        promoted_max_complexity=100,
        duration_seconds=3600,
        fallback_only=True,
        allow_final_review=False,
    )

    assert (
        orchestrator._select_agent_for_capability(
            AgentCapability.REVIEW, package=package
        )
        is None
    )

    orchestrator._provider_promotions.promote(
        "qwen",
        "review",
        base_max_complexity=75,
        promoted_max_complexity=100,
        duration_seconds=3600,
        fallback_only=True,
        allow_final_review=True,
    )
    assert (
        orchestrator._select_agent_for_capability(
            AgentCapability.REVIEW, package=package
        )
        == "qwen"
    )


def test_promoted_execution_is_audited_without_changing_static_weight(tmp_path):
    config = OrchestrationConfig(
        state_dir=tmp_path / "state", strict_checks=False, auto_commit=False
    )
    orchestrator = ProjectOrchestrator("promotion-audit", config=config)
    qwen = _FakeAgentAdapter("qwen", {AgentCapability.REVIEW})
    qwen._execraft_capability_weight = 65
    qwen._execraft_max_complexity_by_capability = {AgentCapability.REVIEW: 75}
    orchestrator.register_agent(qwen)
    package = WorkPackage(id="WP23", title="Runtime acceptance", complexity=97)
    orchestrator._provider_promotions.promote(
        "qwen",
        "review",
        base_max_complexity=75,
        promoted_max_complexity=100,
        duration_seconds=3600,
        fallback_only=True,
        reason="temporary quota incident",
    )

    result = orchestrator._execute_agent(
        AgentCapability.REVIEW,
        "qwen",
        StructuredHandoff(
            work_package_id="WP23", stage="review", summary="Review runtime framework"
        ),
        package,
    )

    assert result["_execraft_executed_by"] == "qwen"
    assert qwen._execraft_capability_weight == 65
    events = [
        item for item in orchestrator._journal.read()
        if item.event_type == "provider_promotion_used"
    ]
    assert events
    assert events[-1].payload["base_max_complexity"] == 75
    assert events[-1].payload["promoted_max_complexity"] == 100
    assert events[-1].payload["task_complexity"] == 97


def test_stale_over_complexity_assignment_is_skipped_before_execution(tmp_path):
    config = OrchestrationConfig(
        state_dir=tmp_path / "state",
        strict_checks=False,
        auto_commit=False,
    )
    orchestrator = ProjectOrchestrator("stale-complexity", config=config)
    weak = _FakeAgentAdapter("weak", {AgentCapability.REVIEW})
    strong = _FakeAgentAdapter("strong", {AgentCapability.REVIEW})
    weak._execraft_capability_weight = 75
    weak._execraft_max_complexity_by_capability = {AgentCapability.REVIEW: 85}
    strong._execraft_capability_weight = 95
    strong._execraft_max_complexity_by_capability = {AgentCapability.REVIEW: 100}
    orchestrator.register_agent(weak)
    orchestrator.register_agent(strong)
    package = WorkPackage(id="WP16", title="Release gate", complexity=97)

    result = orchestrator._execute_agent(
        AgentCapability.REVIEW,
        "weak",
        StructuredHandoff(
            work_package_id="WP16",
            stage="final_review",
            summary="Final review",
        ),
        package,
    )

    assert result["_execraft_executed_by"] == "strong"
    assert weak.executions == []
    assert len(strong.executions) == 1
    skipped = [
        event
        for event in orchestrator._journal.read()
        if event.event_type == "agent_failover"
    ]
    assert skipped[-1].payload["from_agent"] == "weak"
    assert skipped[-1].payload["to_agent"] == "strong"


def test_persisted_final_reviewer_is_rebalanced_when_complexity_policy_changes(tmp_path):
    config = OrchestrationConfig(state_dir=tmp_path / "state")
    orchestrator = ProjectOrchestrator("rebalance-complexity", config=config)
    implementer = _FakeAgentAdapter("impl", {AgentCapability.IMPLEMENT})
    reviewer = _FakeAgentAdapter("reviewer", {AgentCapability.REVIEW})
    weak_final = _FakeAgentAdapter("opencode-go", {AgentCapability.REVIEW})
    strong_final = _FakeAgentAdapter("claude-code", {AgentCapability.REVIEW})
    weak_final._execraft_capability_weight = 75
    weak_final._execraft_max_complexity_by_capability = {AgentCapability.REVIEW: 85}
    strong_final._execraft_capability_weight = 95
    strong_final._execraft_max_complexity_by_capability = {AgentCapability.REVIEW: 100}
    orchestrator.register_agent(implementer)
    orchestrator.register_agent(reviewer)
    orchestrator.register_agent(weak_final)
    orchestrator.register_agent(strong_final)
    package = WorkPackage(
        id="WP16",
        title="Release gate",
        complexity=97,
        stage=WorkPackageStage.FINAL_REVIEW,
        agent_id="impl",
        reviewer_id="reviewer",
        final_reviewer_id="opencode-go",
    )

    schedule = orchestrator._ensure_review_assignments(package)

    assert schedule.final_reviewer_id == "claude-code"
    assert package.final_reviewer_id == "claude-code"


def test_blocking_provider_wait_escalates_after_configured_limit(tmp_path):
    import time

    config = OrchestrationConfig(
        strict_checks=False,
        auto_commit=False,
        state_dir=tmp_path / "state",
        max_agent_attempts_per_stage=1,
        blocking_agent_wait_max_seconds=0.01,
    )
    orchestrator = ProjectOrchestrator("bounded-wait", config=config)
    implementer = _FakeAgentAdapter("impl", {AgentCapability.IMPLEMENT})
    reviewer = _FakeAgentAdapter("reviewer", {AgentCapability.REVIEW})
    final = _FlakyAgentAdapter(
        "final", {AgentCapability.REVIEW}, fail_times=999,
        error_message="Unexpected server error. Check server logs for details.",
    )
    orchestrator.register_agent(implementer)
    orchestrator.register_agent(reviewer)
    orchestrator.register_agent(final)
    orchestrator.initialize("## Package: Only\n\n### Acceptance criteria\n- [ ] Works\n")

    orchestrator.run_pipeline()
    assert orchestrator.state == TaskExecutionState.WAITING_FOR_AGENT

    time.sleep(0.02)
    orchestrator.run_pipeline()

    assert orchestrator.state == TaskExecutionState.HUMAN_REQUIRED
    assert any(
        event.event_type == "agent_wait_expired"
        for event in orchestrator._journal.read()
    )

def test_low_complexity_package_keeps_round_robin_fairness(tmp_path):
    config = OrchestrationConfig(state_dir=tmp_path / "state")
    config.prefer_capable_agents = True
    orchestrator = ProjectOrchestrator("weighted-low", config=config)
    strong = _FakeAgentAdapter("strong", {AgentCapability.IMPLEMENT})
    weak = _FakeAgentAdapter("weak", {AgentCapability.IMPLEMENT})
    strong._execraft_capability_weight = 100
    weak._execraft_capability_weight = 50
    orchestrator.register_agent(strong)
    orchestrator.register_agent(weak)
    orchestrator._set_rotation_cursor(AgentCapability.IMPLEMENT, "strong")

    selected = orchestrator._select_agent_for_capability(
        AgentCapability.IMPLEMENT,
        package=WorkPackage(id="easy", title="Easy", complexity=20),
    )

    assert selected == "weak"


def test_configured_capability_ignores_temporary_provider_unavailability(tmp_path):
    orchestrator = ProjectOrchestrator(
        "capability-preflight",
        config=OrchestrationConfig(state_dir=tmp_path),
    )
    orchestrator.register_agent(
        _FakeAgentAdapter(
            "temporarily-blocked",
            {AgentCapability.IMPLEMENT, AgentCapability.FIX_REVIEW},
            Availability.QUOTA_EXHAUSTED,
        )
    )

    assert orchestrator.has_configured_capability(AgentCapability.IMPLEMENT)
    assert orchestrator.has_configured_capability(AgentCapability.FIX_REVIEW)
    assert not orchestrator.has_available_capability(AgentCapability.IMPLEMENT)
    assert not orchestrator.has_configured_capability(AgentCapability.REVIEW)


def test_package_agent_preferences_are_soft_and_persisted(tmp_path):
    config = OrchestrationConfig(
        strict_checks=False,
        auto_commit=False,
        state_dir=tmp_path / "state",
        rotate_agents=True,
    )
    orchestrator = ProjectOrchestrator("preferences", config=config)
    package = WorkPackage(id="WP01", title="Preferred Work Package")
    orchestrator._state_record.plan_graph = PlanGraph(work_packages=[package])
    first = _FakeAgentAdapter(
        "first", {AgentCapability.IMPLEMENT, AgentCapability.REVIEW}
    )
    preferred = _FakeAgentAdapter(
        "preferred", {AgentCapability.IMPLEMENT, AgentCapability.REVIEW}
    )
    orchestrator.register_agent(first)
    orchestrator.register_agent(preferred)

    report = orchestrator.set_agent_preferences(
        "WP01",
        {
            "implement": ["preferred", "first"],
            "final_review": ["first"],
        },
    )

    assert report["preferences"]["implement"] == ["preferred", "first"]
    assert package.agent_preferences["final_review"] == ["first"]
    assert (
        orchestrator._select_agent_for_capability(
            AgentCapability.IMPLEMENT,
            package=package,
        )
        == "preferred"
    )

    reloaded = ProjectOrchestrator("preferences", config=config)
    reloaded.load_state()
    assert (
        reloaded._state_record.plan_graph.package_by_id("WP01").agent_preferences
        == package.agent_preferences
    )


def test_explicit_final_review_preferences_are_a_binding_pool(tmp_path):
    config = OrchestrationConfig(
        strict_checks=False,
        auto_commit=False,
        state_dir=tmp_path / "state",
    )
    orchestrator = ProjectOrchestrator("final-review-pool", config=config)
    package = WorkPackage(
        id="WP01",
        title="Release gate",
        stage=WorkPackageStage.FINAL_REVIEW,
        agent_preferences={"final_review": ["preferred"]},
    )
    orchestrator._state_record.plan_graph = PlanGraph(work_packages=[package])
    preferred = _FakeAgentAdapter(
        "preferred",
        {AgentCapability.REVIEW},
        Availability.BUSY,
    )
    unlisted = _FakeAgentAdapter("unlisted", {AgentCapability.REVIEW})
    orchestrator.register_agent(preferred)
    orchestrator.register_agent(unlisted)

    assert (
        orchestrator._select_agent_for_capability(
            AgentCapability.REVIEW,
            package=package,
            preference_role="final_review",
        )
        is None
    )
    assert orchestrator._binding_role_exclusions(
        package, "final_review"
    ) == {"unlisted"}
    orchestrator._state_record.state = TaskExecutionState.RUNNING
    with pytest.raises(_AgentWaitRequested):
        orchestrator._wait_for_agent_availability(
            package,
            AgentCapability.REVIEW,
            [],
        )
    assert [
        item["agent_id"]
        for item in orchestrator.status_report()["waiting"]["candidates"]
    ] == ["preferred"]


def test_missing_binding_preferences_migrate_to_default_pool(tmp_path):
    orchestrator = ProjectOrchestrator(
        "stale-final-review-pool",
        config=OrchestrationConfig(
            strict_checks=False,
            auto_commit=False,
            state_dir=tmp_path / "state",
        ),
    )
    package = WorkPackage(
        id="WP01",
        title="Release gate",
        stage=WorkPackageStage.FINAL_REVIEW,
        agent_preferences={"final_review": ["removed-provider"]},
    )
    orchestrator._state_record.plan_graph = PlanGraph(work_packages=[package])
    available = _FakeAgentAdapter("available", {AgentCapability.REVIEW})
    orchestrator.register_agent(available)

    assert orchestrator._binding_role_exclusions(package, "final_review") == set()
    assert package.agent_preferences["final_review"] == []
    migration = [
        event
        for event in orchestrator._journal.read()
        if event.event_type == "agent_preferences_migrated"
    ][-1]
    assert migration.payload["removed_agent_ids"] == ["removed-provider"]
    assert migration.payload["fallback"] == "default_pool"


def test_explicit_fix_review_preferences_are_a_binding_pool(tmp_path):
    orchestrator = ProjectOrchestrator(
        "fix-review-pool",
        config=OrchestrationConfig(
            strict_checks=False,
            auto_commit=False,
            state_dir=tmp_path / "state",
        ),
    )
    package = WorkPackage(
        id="WP01",
        title="Review remediation",
        stage=WorkPackageStage.FIX_REVIEW,
        agent_preferences={"fix_review": ["fixer"]},
    )
    orchestrator._state_record.plan_graph = PlanGraph(work_packages=[package])
    orchestrator.register_agent(
        _FakeAgentAdapter("fixer", {AgentCapability.FIX_REVIEW})
    )
    orchestrator.register_agent(
        _FakeAgentAdapter("unlisted", {AgentCapability.FIX_REVIEW})
    )

    assert (
        orchestrator._select_agent_for_capability(
            AgentCapability.FIX_REVIEW,
            package=package,
            preference_role="fix_review",
        )
        == "fixer"
    )
    assert orchestrator._binding_role_exclusions(
        package, "fix_review"
    ) == {"unlisted"}


def test_package_agent_preference_rejects_wrong_capability(tmp_path):
    orchestrator = ProjectOrchestrator(
        "preferences-invalid",
        config=OrchestrationConfig(
            strict_checks=False,
            auto_commit=False,
            state_dir=tmp_path / "state",
        ),
    )
    orchestrator._state_record.plan_graph = PlanGraph(
        work_packages=[WorkPackage(id="WP01", title="Work Package")]
    )
    orchestrator.register_agent(
        _FakeAgentAdapter("review-only", {AgentCapability.REVIEW})
    )

    with pytest.raises(Exception, match="does not support role 'implement'"):
        orchestrator.set_agent_preferences(
            "WP01", {"implement": ["review-only"]}
        )


def test_package_execution_policy_persists_agents_and_skills_and_inherits_to_shards(tmp_path):
    config = OrchestrationConfig(
        strict_checks=False,
        auto_commit=False,
        state_dir=tmp_path / "state",
    )
    orchestrator = ProjectOrchestrator("execution-policy", config=config)
    parent = WorkPackage(id="WP1", title="Work Package")
    shard = WorkPackage(id="WP1__S1", title="Shard", parent_id="WP1")
    orchestrator._state_record.plan_graph = PlanGraph(work_packages=[parent, shard])
    coder = _FakeAgentAdapter(
        "coder",
        {
            AgentCapability.DECOMPOSE,
            AgentCapability.IMPLEMENT,
            AgentCapability.REVIEW,
            AgentCapability.FIX_REVIEW,
        },
    )
    reviewer = _FakeAgentAdapter("reviewer", {AgentCapability.REVIEW})
    orchestrator.register_agent(coder)
    orchestrator.register_agent(reviewer)

    report = orchestrator.set_execution_policy(
        "WP1",
        agent_preferences={
            "implement": ["coder"],
            "review": ["reviewer", "coder"],
        },
        skill_preferences={
            "implement": ["ai-implement"],
            "review": ["ai-review"],
        },
        apply_to_shards=True,
    )

    assert report["agent_preferences"]["review"] == ["reviewer", "coder"]
    assert report["skill_preferences"]["implement"] == ["ai-implement"]
    assert shard.agent_preferences == parent.agent_preferences
    assert shard.skill_preferences == parent.skill_preferences

    reloaded = ProjectOrchestrator("execution-policy", config=config)
    reloaded.load_state()
    restored = reloaded._state_record.plan_graph.package_by_id("WP1")
    assert restored.agent_preferences == parent.agent_preferences
    assert restored.skill_preferences == parent.skill_preferences


def test_package_execution_policy_rejects_incompatible_skill(tmp_path):
    orchestrator = ProjectOrchestrator(
        "skill-policy-invalid",
        config=OrchestrationConfig(
            strict_checks=False,
            auto_commit=False,
            state_dir=tmp_path / "state",
        ),
    )
    orchestrator._state_record.plan_graph = PlanGraph(
        work_packages=[WorkPackage(id="WP1", title="Work Package")]
    )
    orchestrator.register_agent(
        _FakeAgentAdapter("coder", {AgentCapability.IMPLEMENT})
    )

    with pytest.raises(Exception, match="does not support role 'implement'"):
        orchestrator.set_execution_policy(
            "WP1",
            agent_preferences={"implement": ["coder"]},
            skill_preferences={"implement": ["ai-review"]},
        )


def test_effective_role_skill_is_embedded_in_standard_handoff(tmp_path):
    orchestrator = ProjectOrchestrator(
        "skill-handoff",
        config=OrchestrationConfig(
            strict_checks=False,
            auto_commit=False,
            state_dir=tmp_path / "state",
        ),
    )
    package = WorkPackage(
        id="WP1",
        title="Work Package",
        stage=WorkPackageStage.IMPLEMENT,
    )
    orchestrator._state_record.plan_graph = PlanGraph(work_packages=[package])
    orchestrator._state_record.state = TaskExecutionState.RUNNING
    adapter = _FakeAgentAdapter("coder", {AgentCapability.IMPLEMENT})
    orchestrator.register_agent(adapter)

    orchestrator._call_agent(
        AgentCapability.IMPLEMENT,
        "coder",
        package,
        summary="Implement Work Package",
    )

    assert [item["id"] for item in adapter.executions[0].workflow_skills] == [
        "ai-implement"
    ]
    assert "Run `execraft task status" in adapter.executions[0].workflow_skills[0][
        "instructions"
    ]


def test_read_only_stage_skips_provider_without_required_isolation(tmp_path):
    from execraft.orchestrate.scheduler import AgentAdapterCapabilities

    class IsolationAdapter(_FakeAgentAdapter):
        def __init__(self, provider_id, caps, level):
            super().__init__(provider_id, caps)
            self._level = level

        @property
        def execution_capabilities(self):
            return AgentAdapterCapabilities(read_only_enforcement=self._level)

    config = OrchestrationConfig(
        strict_checks=False,
        auto_commit=False,
        state_dir=tmp_path / "state",
        minimum_read_only_enforcement="hard",
    )
    orch = ProjectOrchestrator("isolation", config=config)
    implementer = IsolationAdapter("impl", {AgentCapability.IMPLEMENT}, "hard")
    advisory = IsolationAdapter("review-advisory", {AgentCapability.REVIEW}, "advisory")
    hard = IsolationAdapter("review-hard", {AgentCapability.REVIEW}, "hard")
    hard_final = IsolationAdapter("review-hard-final", {AgentCapability.REVIEW}, "hard")
    orch.register_agent(implementer)
    orch.register_agent(advisory)
    orch.register_agent(hard)
    orch.register_agent(hard_final)
    orch.initialize("## Package: Only\n\n### Acceptance criteria\n- [ ] Works\n")

    orch.run_pipeline()

    assert orch.state == TaskExecutionState.COMPLETED
    assert advisory.executions == []
    assert len(hard.executions) + len(hard_final.executions) == 2
    review_records = [
        item
        for item in orch.invocation_history(package_id="only")
        if item["capability"] == "review"
    ]
    assert review_records[0]["isolation"]["read_only_enforcement"] == "hard"


def test_post_completion_projection_failure_does_not_poison_provider(tmp_path, monkeypatch):
    config = OrchestrationConfig(
        strict_checks=False,
        auto_commit=False,
        state_dir=tmp_path / "state",
    )
    orchestrator = ProjectOrchestrator("projection-failure", config=config)
    package = WorkPackage(
        id="WP1",
        title="Work Package",
        stage=WorkPackageStage.IMPLEMENT,
    )
    orchestrator._state_record.plan_graph = PlanGraph(work_packages=[package])
    orchestrator._state_record.state = TaskExecutionState.RUNNING
    adapter = _FakeAgentAdapter("coder", {AgentCapability.IMPLEMENT})
    orchestrator.register_agent(adapter)

    def fail_projection(_package, _attempt):
        raise OSError("state projection unavailable")

    monkeypatch.setattr(orchestrator, "_append_agent_attempt", fail_projection)

    with pytest.raises(OrchestrateError, match="invocation completed"):
        orchestrator._call_agent(
            AgentCapability.IMPLEMENT,
            "coder",
            package,
            summary="Implement Work Package",
        )

    records = orchestrator.invocation_history(package_id="WP1")
    assert len(records) == 1
    assert records[0]["status"] == "completed"
    assert orchestrator._provider_health.get("coder").is_available


def test_console_registration_separates_native_controls_from_pty(tmp_path):
    from execraft.orchestrate.agent_console import InteractiveTerminalPolicy
    from execraft.orchestrate.scheduler import AgentAdapterCapabilities

    class ConsoleAwareAdapter(_FakeAgentAdapter):
        def __init__(self, provider_id: str, execution: AgentAdapterCapabilities):
            super().__init__(provider_id, {AgentCapability.IMPLEMENT})
            self._execution = execution
            self.control_callbacks = []
            self.terminal_callbacks = []

        @property
        def execution_capabilities(self):
            return self._execution

        def configure_operator_controls(self, callback):
            self.control_callbacks.append(callback)

        def configure_terminal(self, callback):
            self.terminal_callbacks.append(callback)

    config = OrchestrationConfig(
        strict_checks=False,
        auto_commit=False,
        state_dir=tmp_path / "state",
        interactive_terminal_policy=InteractiveTerminalPolicy(enabled=True),
    )
    orchestrator = ProjectOrchestrator("console-transports", config=config)
    opencode = ConsoleAwareAdapter(
        "opencode",
        AgentAdapterCapabilities(
            streaming=True,
            semantic_streaming=True,
            provider_native_steering=False,
            interactive_pty=False,
        ),
    )
    codex = ConsoleAwareAdapter(
        "codex",
        AgentAdapterCapabilities(
            streaming=True,
            semantic_streaming=True,
            provider_native_steering=True,
            interactive_pty=False,
        ),
    )

    orchestrator.register_agent(opencode)
    orchestrator.register_agent(codex)

    assert opencode.control_callbacks == []
    assert opencode.terminal_callbacks == []
    assert len(codex.control_callbacks) == 1
    assert codex.terminal_callbacks == []
    assert orchestrator._agent_metadata(opencode)["interactive_pty"] is False
    assert orchestrator._agent_metadata(opencode)["streaming_interaction"] is True


def test_future_pause_stops_before_assignment_and_next_run_resumes(tmp_path, monkeypatch):
    from execraft.orchestrate.directives import PAUSE_BEFORE_START

    config = OrchestrationConfig(
        strict_checks=False,
        auto_commit=False,
        require_verification=False,
        require_repository_changes=False,
        state_dir=tmp_path / "state",
    )
    orch = ProjectOrchestrator("test-project", config=config)
    package = WorkPackage(
        id="WP2",
        title="Future Work Package",
        requirements=["R1"],
        acceptance_criteria=[AcceptanceCriterion(id="A1", description="Done")],
    )
    orch.initialize_graph(PlanGraph([package]), NormalizationReport())
    orch.transition_to(TaskExecutionState.RUNNING)
    orch._work_package_directives.enqueue(
        package_id="WP2",
        kind=PAUSE_BEFORE_START,
        enabled=True,
        reason="operator checkpoint",
        requested_by="test",
    )
    processed: list[str] = []

    def complete(selected: WorkPackage) -> None:
        processed.append(selected.id)
        selected.stage = WorkPackageStage.COMPLETED
        selected.status = "completed"

    monkeypatch.setattr(orch, "_process_package", complete)

    orch.run_pipeline()

    assert orch.state == TaskExecutionState.OPERATOR_PAUSED
    assert processed == []
    assert orch._state_record.waiting["kind"] == PAUSE_BEFORE_START
    assert orch._state_record.waiting["package_id"] == "WP2"
    assert package.pause_before_start is False
    assert package.pause_before_start_reached_at
    assert orch._work_package_directives.pending() == []

    orch.run_pipeline()

    assert processed == ["WP2"]
    assert orch.state == TaskExecutionState.COMPLETED
    assert orch._state_record.waiting == {}
    assert package.pause_before_start_reached_at == ""


def _sync_card_manifest():
    from execraft.workspace.task_git import RepositorySpec, TaskManifest

    return TaskManifest(
        schema_version=2,
        id="demo",
        project="test-project",
        title="Demo",
        status="in_progress",
        branch_name="task/demo",
        repositories=[
            RepositorySpec(
                id="core",
                base_branch="master",
                task_branch="task/demo",
                mutability="task_owned",
            )
        ],
    )


def test_pause_and_sync_before_waits_for_dependency_safe_boundary(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from execraft.orchestrate.directives import PAUSE_FOR_REPOSITORY_SYNC

    config = OrchestrationConfig(
        strict_checks=False,
        auto_commit=False,
        require_verification=False,
        require_repository_changes=False,
        state_dir=tmp_path / "state",
    )
    orch = ProjectOrchestrator("test-project", config=config)
    first = WorkPackage(
        id="WP1",
        title="First",
        affected_repositories=["core"],
        requirements=["R1"],
        acceptance_criteria=[AcceptanceCriterion(id="A1", description="Done")],
    )
    target = WorkPackage(
        id="WP2",
        title="Target",
        dependencies=["WP1"],
        affected_repositories=["core"],
        requirements=["R2"],
        acceptance_criteria=[AcceptanceCriterion(id="A2", description="Done")],
    )
    orch.initialize_graph(PlanGraph([first, target]), NormalizationReport())
    orch.transition_to(TaskExecutionState.RUNNING)
    monkeypatch.setattr(
        orch,
        "_repository_sync_service",
        lambda: SimpleNamespace(manifest=_sync_card_manifest()),
    )
    orch._work_package_directives.enqueue(
        package_id="WP2",
        kind=PAUSE_FOR_REPOSITORY_SYNC,
        enabled=True,
        reason="sync before WP2",
        parameters={
            "mode": "before",
            "repositories": ["core"],
            "source_branches": {"core": "master"},
            "remote": "origin",
            "conflict_policy": "ai_resolve",
            "auto_resume": True,
        },
    )
    processed = []

    def complete(package):
        processed.append(package.id)
        package.stage = WorkPackageStage.COMPLETED
        package.status = "completed"

    monkeypatch.setattr(orch, "_process_package", complete)
    orch.run_pipeline()

    assert processed == ["WP1"]
    assert orch.state == TaskExecutionState.OPERATOR_PAUSED
    assert orch._state_record.waiting["kind"] == PAUSE_FOR_REPOSITORY_SYNC
    assert orch._state_record.waiting["package_id"] == "WP2"
    assert orch._state_record.waiting["repository_sync"]["mode"] == "before"
    assert orch._work_package_directives.pending() == []


def test_pause_and_sync_replaces_existing_operator_pause_boundary(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from execraft.orchestrate.directives import (
        PAUSE_BEFORE_START,
        PAUSE_FOR_REPOSITORY_SYNC,
    )

    config = OrchestrationConfig(
        strict_checks=False,
        auto_commit=False,
        require_verification=False,
        require_repository_changes=False,
        state_dir=tmp_path / "state",
    )
    orch = ProjectOrchestrator("test-project", config=config)
    target = WorkPackage(
        id="WP20",
        title="Target",
        affected_repositories=["core"],
        requirements=["R20"],
        acceptance_criteria=[AcceptanceCriterion(id="A20", description="Done")],
    )
    orch.initialize_graph(PlanGraph([target]), NormalizationReport())
    orch.transition_to(TaskExecutionState.RUNNING)
    orch.transition_to(TaskExecutionState.OPERATOR_PAUSED)
    orch._state_record.waiting = {
        "kind": PAUSE_BEFORE_START,
        "package_id": "WP20",
        "reached_at": "2026-08-08T10:00:00+00:00",
    }
    orch.save_state(reason="legacy_pause_before_start")
    monkeypatch.setattr(
        orch,
        "_repository_sync_service",
        lambda: SimpleNamespace(manifest=_sync_card_manifest()),
    )
    command = orch._work_package_directives.enqueue(
        package_id="WP20",
        kind=PAUSE_FOR_REPOSITORY_SYNC,
        enabled=True,
        reason="sync before WP20",
        parameters={
            "mode": "before",
            "repositories": ["core"],
            "source_branches": {"core": "master"},
            "remote": "origin",
            "conflict_policy": "ai_resolve",
            "auto_resume": True,
        },
    )

    orch.run_pipeline()

    assert orch.state == TaskExecutionState.OPERATOR_PAUSED
    assert orch._state_record.waiting["kind"] == PAUSE_FOR_REPOSITORY_SYNC
    assert orch._state_record.waiting["command_id"] == command.id
    assert orch._work_package_directives.pending() == []
    stored = json.loads(orch._work_package_directives.path.read_text(encoding="utf-8"))
    commands = {item["id"]: item for item in stored["commands"]}
    assert commands[command.id]["status"] == "applied"


def test_pause_and_sync_after_finishes_selected_package_before_pausing(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from execraft.orchestrate.directives import PAUSE_FOR_REPOSITORY_SYNC

    config = OrchestrationConfig(
        strict_checks=False,
        auto_commit=False,
        require_verification=False,
        require_repository_changes=False,
        state_dir=tmp_path / "state",
    )
    orch = ProjectOrchestrator("test-project", config=config)
    current = WorkPackage(
        id="WP20",
        title="Current",
        affected_repositories=["core"],
        requirements=["R1"],
        acceptance_criteria=[AcceptanceCriterion(id="A1", description="Done")],
    )
    downstream = WorkPackage(
        id="WP21",
        title="Downstream",
        dependencies=["WP20"],
        affected_repositories=["core"],
        requirements=["R2"],
        acceptance_criteria=[AcceptanceCriterion(id="A2", description="Done")],
    )
    orch.initialize_graph(PlanGraph([current, downstream]), NormalizationReport())
    orch.transition_to(TaskExecutionState.RUNNING)
    monkeypatch.setattr(
        orch,
        "_repository_sync_service",
        lambda: SimpleNamespace(manifest=_sync_card_manifest()),
    )
    orch._work_package_directives.enqueue(
        package_id="WP20",
        kind=PAUSE_FOR_REPOSITORY_SYNC,
        enabled=True,
        reason="pause and sync",
        parameters={
            "mode": "after",
            "repositories": ["core"],
            "source_branches": {},
            "remote": "origin",
            "conflict_policy": "ai_resolve",
            "auto_resume": False,
        },
    )
    processed = []

    def complete(package):
        processed.append(package.id)
        package.stage = WorkPackageStage.COMPLETED
        package.status = "completed"

    monkeypatch.setattr(orch, "_process_package", complete)
    orch.run_pipeline()

    assert processed == ["WP20"]
    assert orch.state == TaskExecutionState.OPERATOR_PAUSED
    assert orch._state_record.waiting["package_id"] == "WP20"
    assert orch._state_record.waiting["repository_sync"]["mode"] == "after"


def test_post_sync_hold_stops_after_package_and_next_run_acknowledges(tmp_path, monkeypatch):
    from execraft.orchestrate.models import WorkPackageKind
    from execraft.repository_sync.spec import RepositorySyncSpec
    config = OrchestrationConfig(
        strict_checks=False,
        auto_commit=False,
        require_verification=False,
        require_repository_changes=False,
        state_dir=tmp_path / "state",
    )
    orch = ProjectOrchestrator("test-project", config=config)
    package = WorkPackage(
        id="WP20-SYNC",
        title="Sync",
        kind=WorkPackageKind.REPOSITORY_SYNC,
        repository_sync=RepositorySyncSpec.from_mapping({"repositories": ["core"]}),
        affected_repositories=["core"],
        requirements=["sync"],
        acceptance_criteria=[AcceptanceCriterion(id="A1", description="Done")],
    )
    orch.initialize_graph(PlanGraph([package]), NormalizationReport())
    orch.transition_to(TaskExecutionState.RUNNING)
    orch.schedule_pause_after_completion("WP20-SYNC", reason="inspect sync")
    repeated_schedule = orch.schedule_pause_after_completion(
        "WP20-SYNC", reason="inspect sync again"
    )
    assert repeated_schedule["already_scheduled"] is True

    def complete(selected):
        selected.stage = WorkPackageStage.COMPLETED
        selected.status = "completed"

    monkeypatch.setattr(orch, "_process_package", complete)
    orch.run_pipeline()

    assert orch.state == TaskExecutionState.OPERATOR_PAUSED
    assert orch._state_record.waiting["kind"] == "pause_after_completion"
    assert orch._state_record.waiting["package_id"] == "WP20-SYNC"
    assert package.pause_after_completion is False
    assert package.pause_after_completion_reached_at

    orch.run_pipeline()

    assert orch.state == TaskExecutionState.COMPLETED
    assert orch._state_record.waiting == {}
    assert package.pause_after_completion_reached_at == ""


def test_repository_sync_boundary_acknowledgement_is_identity_checked_and_idempotent(tmp_path):
    config = OrchestrationConfig(
        strict_checks=False,
        auto_commit=False,
        require_verification=False,
        require_repository_changes=False,
        state_dir=tmp_path / "state",
    )
    orch = ProjectOrchestrator("test-project", config=config)
    package = WorkPackage(
        id="WP20",
        title="Work",
        requirements=["work"],
        acceptance_criteria=[AcceptanceCriterion(id="A1", description="Done")],
    )
    orch.initialize_graph(PlanGraph([package]), NormalizationReport())
    orch.transition_to(TaskExecutionState.RUNNING)
    orch.transition_to(TaskExecutionState.OPERATOR_PAUSED)
    orch._state_record.waiting = {
        "kind": "pause_for_repository_sync",
        "package_id": "WP20",
        "command_id": "command-20",
        "reached_at": "2026-08-08T10:00:00+00:00",
    }
    orch.save_state(reason="test_sync_boundary")

    with pytest.raises(OrchestrateError, match="command identity changed"):
        orch.acknowledge_repository_sync_boundary(
            command_id="wrong-command",
            sync_package_id="WP20-SYNC",
        )

    result = orch.acknowledge_repository_sync_boundary(
        command_id="command-20",
        sync_package_id="WP20-SYNC",
    )
    assert result["already_acknowledged"] is False
    assert orch.state == TaskExecutionState.RUNNING
    assert orch._state_record.waiting == {}

    repeated = orch.acknowledge_repository_sync_boundary(
        command_id="command-20",
        sync_package_id="WP20-SYNC",
    )
    assert repeated["already_acknowledged"] is True


def test_repository_sync_boundary_recovers_legacy_replan_without_waiting(tmp_path):
    from execraft.orchestrate.models import WorkPackageKind
    from execraft.repository_sync.spec import RepositorySyncSpec

    config = OrchestrationConfig(
        strict_checks=False,
        auto_commit=False,
        require_verification=False,
        require_repository_changes=False,
        state_dir=tmp_path / "state",
    )
    orch = ProjectOrchestrator("test-project", config=config)
    sync_package = WorkPackage(
        id="WP20-SYNC",
        title="Sync",
        kind=WorkPackageKind.REPOSITORY_SYNC,
        repository_sync=RepositorySyncSpec.from_mapping({"repositories": ["core"]}),
        affected_repositories=["core"],
        requirements=["sync"],
        acceptance_criteria=[AcceptanceCriterion(id="A1", description="Done")],
    )
    orch.initialize_graph(PlanGraph([sync_package]), NormalizationReport())
    orch.transition_to(TaskExecutionState.RUNNING)
    orch.transition_to(TaskExecutionState.OPERATOR_PAUSED)
    assert orch._state_record.waiting == {}

    result = orch.acknowledge_repository_sync_boundary(
        command_id="legacy-command",
        sync_package_id="WP20-SYNC",
    )

    assert result["recovered_missing_ownership"] is True
    assert orch.state == TaskExecutionState.RUNNING


class TestOrchestratorFacadeStructure:
    """Structural regressions for the remaining intentional façade surface."""

    def test_project_orchestrator_defines_no_duplicate_methods(self):
        import ast
        import inspect

        source = inspect.getsource(ProjectOrchestrator)
        tree = ast.parse(source)
        class_node = tree.body[0]
        assert isinstance(class_node, ast.ClassDef)
        names = [
            node.name
            for node in class_node.body
            if isinstance(node, ast.FunctionDef)
        ]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        assert duplicates == [], (
            "ProjectOrchestrator must define every method exactly once; "
            f"shadowed duplicates found: {duplicates}"
        )

    def test_scope_recovery_facade_delegates_to_coordinator(self, tmp_path):
        from unittest.mock import MagicMock

        orch = ProjectOrchestrator(
            "test-project", config=OrchestrationConfig(state_dir=tmp_path / "state")
        )
        coordinator = orch._scope_recovery_coordinator
        delegations = [
            ("declared_write_scope_report", "declared_write_scope_report", ("WP01",)),
            ("reconcile_resolved_scope_check", "reconcile_resolved_scope_check", ()),
            ("approve_declared_write_scope", "approve_declared_write_scope", ("WP01",)),
        ]
        for method, coordinator_method, args in delegations:
            mock = MagicMock()
            setattr(coordinator, coordinator_method, mock)
            getattr(orch, method)(*args)
            mock.assert_called_once()

    def test_supervisor_facade_delegates_to_coordinator(self, tmp_path):
        from unittest.mock import MagicMock

        config = OrchestrationConfig(state_dir=tmp_path / "state")
        orch = ProjectOrchestrator("test-project", config=config)
        coordinator = orch._supervisor_coordinator

        delegations = [
            (
                "can_auto_resume_supervisor_transport_failure",
                "can_auto_resume_supervisor_transport_failure",
                (),
            ),
        ]
        for method, coordinator_method, args in delegations:
            mock = MagicMock()
            setattr(coordinator, coordinator_method, mock)
            getattr(orch, method)(*args)
            assert mock.call_count == 1, (
                f"{method} must delegate exactly once to the coordinator"
            )

        lost_mock = MagicMock()
        orch._supervisor_coordinator.can_auto_resume_lost_supervisor_delegation = lost_mock
        orch.can_auto_resume_lost_supervisor_delegation()
        assert lost_mock.call_count == 1
