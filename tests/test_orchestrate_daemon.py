"""Tests for the daemon-style retry/backoff driver."""

import subprocess
from pathlib import Path

import pytest

from execraft.orchestrate import (
    AcceptanceCriterion,
    ScopeRecoveryPolicy,
    WorkPackage,
    WorkPackageStage,
    normalize_work_packages,
)
from execraft.orchestrate.daemon import DaemonConfig, run_until_terminal
from execraft.orchestrate.models import OrchestrateError, TaskExecutionState
from execraft.orchestrate.orchestrator import OrchestrationConfig, ProjectOrchestrator
from execraft.orchestrate.resource import ResourceInventory
from execraft.orchestrate.scheduler import (
    AgentAdapter,
    AgentCapability,
    AgentExecutionError,
    Availability,
    StructuredHandoff,
)


class _FakeAgentAdapter(AgentAdapter):
    def __init__(self, provider_id: str, caps: set[AgentCapability]):
        self._id = provider_id
        self._caps = caps

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
        return {"ok": True}


class _RecordingSleep:
    def __init__(self):
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class _PressureThenResumeResourceManager:
    """Pauses the project on the first pressure check, then allows resume
    only after a configured number of `can_resume()` probes — simulating
    disk pressure that clears over time without any real sleeping or disk
    access."""

    def __init__(self, *, resumable_after_calls: int):
        self._cleanup_done = False
        self._resume_calls = 0
        self._resumable_after_calls = resumable_after_calls

    def inventory(self, workspace_roots=None) -> ResourceInventory:
        return ResourceInventory(filesystem_used_percent=92)

    def needs_cleanup(self, inventory=None) -> bool:
        return not self._cleanup_done

    def cleanup(self, workspace_roots=None, dry_run=False) -> list[str]:
        self._cleanup_done = True
        return ["remove stale managed workspace: /fake"]

    def needs_pause(self, inventory=None) -> bool:
        return True

    def can_resume(self, inventory=None) -> bool:
        self._resume_calls += 1
        return self._resume_calls > self._resumable_after_calls


def _initialized_orchestrator(tmp_path, *, resource_manager=None):
    config = OrchestrationConfig(strict_checks=False, auto_commit=False, state_dir=tmp_path / "state")
    orch = ProjectOrchestrator(
        "daemon-test", config=config, resource_manager=resource_manager
    )
    orch.register_agent(
        _FakeAgentAdapter("solo", {AgentCapability.IMPLEMENT, AgentCapability.REVIEW})
    )
    orch.initialize("## Package: Only\n\n### Acceptance criteria\n- [ ] Works\n")
    return orch


class TestRunUntilTerminal:
    def test_completes_on_first_attempt_without_sleeping(self, tmp_path):
        orch = _initialized_orchestrator(tmp_path)
        sleeper = _RecordingSleep()

        result = run_until_terminal(orch, sleep=sleeper)

        assert result.final_state == TaskExecutionState.COMPLETED
        assert result.attempts == 1
        assert result.exhausted is False
        assert sleeper.calls == []

    def test_returns_immediately_when_already_terminal(self, tmp_path):
        orch = _initialized_orchestrator(tmp_path)
        orch.run_pipeline()
        assert orch.state == TaskExecutionState.COMPLETED
        sleeper = _RecordingSleep()

        result = run_until_terminal(orch, sleep=sleeper)

        assert result.attempts == 0
        assert sleeper.calls == []

    def test_stops_without_retrying_on_human_required(self, tmp_path):
        config = OrchestrationConfig(strict_checks=False, auto_commit=False, state_dir=tmp_path / "state")
        orch = ProjectOrchestrator("daemon-human", config=config)
        orch.transition_to(TaskExecutionState.VALIDATING_PLAN)
        orch.transition_to(TaskExecutionState.HUMAN_REQUIRED)
        sleeper = _RecordingSleep()

        result = run_until_terminal(orch, sleep=sleeper)

        assert result.final_state == TaskExecutionState.HUMAN_REQUIRED
        assert result.attempts == 0
        assert sleeper.calls == []

    def test_stops_without_polling_while_supervisor_waits_for_human(self, tmp_path):
        config = OrchestrationConfig(
            strict_checks=False,
            auto_commit=False,
            state_dir=tmp_path / "state",
        )
        orch = ProjectOrchestrator("daemon-supervisor-question", config=config)
        orch.transition_to(TaskExecutionState.VALIDATING_PLAN)
        orch.transition_to(TaskExecutionState.HUMAN_REQUIRED)
        orch.transition_to(TaskExecutionState.SUPERVISING)
        orch.transition_to(TaskExecutionState.WAITING_FOR_HUMAN_DECISION)
        sleeper = _RecordingSleep()

        result = run_until_terminal(orch, sleep=sleeper)

        assert result.final_state == TaskExecutionState.WAITING_FOR_HUMAN_DECISION
        assert result.attempts == 0
        assert sleeper.calls == []

    def test_resumes_with_exponential_backoff_once_pressure_clears(self, tmp_path):
        mgr = _PressureThenResumeResourceManager(resumable_after_calls=1)
        orch = _initialized_orchestrator(tmp_path, resource_manager=mgr)
        sleeper = _RecordingSleep()
        daemon_config = DaemonConfig(
            initial_backoff_seconds=1.0, backoff_multiplier=2.0
        )

        result = run_until_terminal(orch, config=daemon_config, sleep=sleeper)

        assert result.final_state == TaskExecutionState.COMPLETED
        assert orch.status_report()["completed_packages"] == 1
        # attempt 1: pauses; attempt 2: still blocked; attempt 3: resumes+completes.
        assert result.attempts == 3
        assert sleeper.calls == [1.0, 2.0]

    def test_stops_after_max_attempts_when_never_resolves(self, tmp_path):
        mgr = _PressureThenResumeResourceManager(resumable_after_calls=100)
        orch = _initialized_orchestrator(tmp_path, resource_manager=mgr)
        sleeper = _RecordingSleep()
        daemon_config = DaemonConfig(
            max_attempts=3, initial_backoff_seconds=1.0, backoff_multiplier=1.0
        )

        result = run_until_terminal(orch, config=daemon_config, sleep=sleeper)

        assert result.exhausted is True
        assert result.attempts == 3
        assert result.final_state == TaskExecutionState.PAUSED_LOW_DISK
        assert orch.status_report()["completed_packages"] == 0
        assert sleeper.calls == [1.0, 1.0, 1.0]

    def test_backoff_never_exceeds_configured_maximum(self, tmp_path):
        mgr = _PressureThenResumeResourceManager(resumable_after_calls=5)
        orch = _initialized_orchestrator(tmp_path, resource_manager=mgr)
        sleeper = _RecordingSleep()
        daemon_config = DaemonConfig(
            initial_backoff_seconds=10.0,
            backoff_multiplier=10.0,
            max_backoff_seconds=25.0,
        )

        run_until_terminal(orch, config=daemon_config, sleep=sleeper)

        assert all(delay <= 25.0 for delay in sleeper.calls)
        assert sleeper.calls[-1] == 25.0

    def test_daemon_survives_being_reconstructed_between_attempts(self, tmp_path):
        """Simulates a process restart: a fresh ProjectOrchestrator instance
        loading persisted state must resume exactly where the last one left
        off, since run_until_terminal() relies only on durable state."""
        mgr = _PressureThenResumeResourceManager(resumable_after_calls=0)
        config = OrchestrationConfig(strict_checks=False, auto_commit=False, state_dir=tmp_path / "state")
        first = ProjectOrchestrator(
            "daemon-restart", config=config, resource_manager=mgr
        )
        first.register_agent(
            _FakeAgentAdapter("solo", {AgentCapability.IMPLEMENT, AgentCapability.REVIEW})
        )
        first.initialize("## Package: Only\n\n### Acceptance criteria\n- [ ] Works\n")
        first.run_pipeline()
        assert first.state == TaskExecutionState.PAUSED_LOW_DISK

        second = ProjectOrchestrator(
            "daemon-restart", config=config, resource_manager=mgr
        )
        second.register_agent(
            _FakeAgentAdapter("solo", {AgentCapability.IMPLEMENT, AgentCapability.REVIEW})
        )
        second.load_state()
        assert second.state == TaskExecutionState.PAUSED_LOW_DISK

        sleeper = _RecordingSleep()
        result = run_until_terminal(second, sleep=sleeper)
        assert result.final_state == TaskExecutionState.COMPLETED

class _RecoveringAgent(AgentAdapter):
    def __init__(self):
        self.call_count = 0

    @property
    def availability(self) -> Availability:
        return Availability.AVAILABLE

    @property
    def provider_id(self) -> str:
        return "recovering"

    @property
    def capabilities(self) -> set[AgentCapability]:
        return {AgentCapability.IMPLEMENT, AgentCapability.REVIEW}

    def execute(self, handoff: StructuredHandoff) -> dict:
        self.call_count += 1
        if self.call_count == 1:
            raise RuntimeError("temporary provider outage")
        return {"ok": True}


def test_daemon_polls_waiting_agent_and_resumes_automatically(tmp_path):
    config = OrchestrationConfig(
        strict_checks=False,
        auto_commit=False,
        state_dir=tmp_path / "state",
        max_agent_attempts_per_stage=1,
        agent_retry_initial_seconds=12.0,
    )
    orch = ProjectOrchestrator("daemon-agent-wait", config=config)
    adapter = _RecoveringAgent()
    orch.register_agent(adapter)
    orch.initialize("## Package: Only\n\n### Acceptance criteria\n- [ ] Works\n")
    sleeper = _RecordingSleep()

    result = run_until_terminal(
        orch,
        config=DaemonConfig(max_backoff_seconds=60.0),
        sleep=sleeper,
    )

    assert result.final_state == TaskExecutionState.COMPLETED
    assert adapter.call_count >= 2
    # The persisted retry deadline is created before the daemon loop starts, so
    # setup/serialization time is correctly subtracted from the configured 12s.
    # Assert the remaining-delay contract rather than a wall-clock-sensitive
    # approximation of the original interval.
    assert len(sleeper.calls) == 1
    assert 0 < sleeper.calls[0] <= 12.0
    assert orch.status_report()["waiting"] == {}


def test_waiting_state_survives_process_reconstruction(tmp_path):
    config = OrchestrationConfig(
        strict_checks=False,
        auto_commit=False,
        state_dir=tmp_path / "state",
        max_agent_attempts_per_stage=1,
    )
    first = ProjectOrchestrator("daemon-agent-restart", config=config)
    first.register_agent(
        _RecoveringAgent()
    )
    first.initialize("## Package: Only\n\n### Acceptance criteria\n- [ ] Works\n")
    first.run_pipeline()
    assert first.state == TaskExecutionState.WAITING_FOR_AGENT
    persisted_wait = dict(first.status_report()["waiting"])

    second = ProjectOrchestrator("daemon-agent-restart", config=config)
    second.load_state()

    assert second.state == TaskExecutionState.WAITING_FOR_AGENT
    assert second.status_report()["waiting"] == persisted_wait


class _PersistentFailureThenSuccess(AgentAdapter):
    def __init__(self):
        self.call_count = 0

    @property
    def availability(self) -> Availability:
        return Availability.AVAILABLE

    @property
    def provider_id(self) -> str:
        return "persistent"

    @property
    def capabilities(self) -> set[AgentCapability]:
        return {AgentCapability.IMPLEMENT, AgentCapability.REVIEW}

    def execute(self, handoff: StructuredHandoff) -> dict:
        self.call_count += 1
        if self.call_count == 1:
            raise AgentExecutionError(
                "monthly quota exhausted",
                classification="quota_exhausted",
                retry_after_seconds=3600,
                persistent=True,
            )
        return {"ok": True}


def test_health_reset_wakes_persistently_blocked_provider_on_next_poll(tmp_path):
    config = OrchestrationConfig(
        strict_checks=False,
        auto_commit=False,
        state_dir=tmp_path / "state",
        max_agent_attempts_per_stage=1,
    )
    orch = ProjectOrchestrator("daemon-provider-reset", config=config)
    adapter = _PersistentFailureThenSuccess()
    orch.register_agent(adapter)
    orch.initialize("## Package: Only\n\n### Acceptance criteria\n- [ ] Works\n")
    sleeps: list[float] = []

    def reset_during_wait(seconds: float) -> None:
        sleeps.append(seconds)
        orch._provider_health.reset("persistent")

    result = run_until_terminal(
        orch,
        config=DaemonConfig(max_backoff_seconds=300.0),
        sleep=reset_during_wait,
    )

    assert result.final_state == TaskExecutionState.COMPLETED
    assert adapter.call_count >= 2
    assert sleeps == [pytest.approx(300.0, abs=0.2)]


def test_legacy_agent_human_required_is_auto_migrated_to_waiting(tmp_path):
    config = OrchestrationConfig(
        strict_checks=False,
        auto_commit=False,
        state_dir=tmp_path / "state",
        max_agent_attempts_per_stage=1,
    )
    orch = ProjectOrchestrator("daemon-legacy-agent", config=config)
    adapter = _RecoveringAgent()
    orch.register_agent(adapter)
    orch.initialize("## Package: Only\n\n### Acceptance criteria\n- [ ] Works\n")
    package = orch._state_record.plan_graph.package_by_id("only")
    orch._escalate_to_human(
        package,
        AgentCapability.IMPLEMENT,
        [{"agent_id": "recovering", "classification": "timeout", "error": "down"}],
    )
    assert orch.state == TaskExecutionState.HUMAN_REQUIRED
    assert orch.can_auto_resume_agent_wait() is True

    result = run_until_terminal(orch, sleep=lambda _seconds: None)

    assert result.final_state == TaskExecutionState.COMPLETED


def test_driver_lock_prevents_two_polling_loops(tmp_path):
    orch = _initialized_orchestrator(tmp_path)

    with orch.exclusive_driver_lock():
        with pytest.raises(OrchestrateError, match="active run/daemon driver"):
            run_until_terminal(orch, sleep=lambda _seconds: None)


def test_observer_construction_does_not_recover_a_live_invocation(tmp_path):
    config = OrchestrationConfig(
        strict_checks=False,
        auto_commit=False,
        state_dir=tmp_path / "state",
    )
    driver = ProjectOrchestrator("live-driver", config=config)

    with driver.exclusive_driver_lock(), driver._exclusive_run_lock():
        invocation = driver._agent_invocations.begin(
            project_id="live-driver",
            package_id="only",
            stage="review",
            capability="review",
            attempt=1,
            agent_id="reviewer",
            handoff={"work_package_id": "only", "stage": "review"},
        )

        observer = ProjectOrchestrator("live-driver", config=config)

        assert (
            observer._agent_invocations.get(invocation.invocation_id).status
            == "running"
        )
        with pytest.raises(OrchestrateError, match="already being orchestrated"):
            observer.run_pipeline()
        assert (
            observer._agent_invocations.get(invocation.invocation_id).status
            == "running"
        )


def test_execution_lock_recovers_invocations_left_by_a_dead_driver(tmp_path):
    config = OrchestrationConfig(
        strict_checks=False,
        auto_commit=False,
        state_dir=tmp_path / "state",
    )
    crashed = ProjectOrchestrator("dead-driver", config=config)
    invocation = crashed._agent_invocations.begin(
        project_id="dead-driver",
        package_id="only",
        stage="review",
        capability="review",
        attempt=1,
        agent_id="reviewer",
        handoff={"work_package_id": "only", "stage": "review"},
    )

    restarted = ProjectOrchestrator("dead-driver", config=config)
    assert (
        restarted._agent_invocations.get(invocation.invocation_id).status
        == "running"
    )

    with restarted.exclusive_driver_lock():
        recovered = restarted._agent_invocations.get(invocation.invocation_id)

    assert recovered.status == "failed"
    assert recovered.failure["classification"] == "interrupted"


def _git(path: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=path,
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout.strip()


def _clean_repository(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "execraft@example.invalid")
    _git(path, "config", "user.name", "Execraft tests")
    (path / "README.md").write_text("baseline\n")
    _git(path, "add", "README.md")
    _git(path, "commit", "-q", "-m", "baseline")
    return path


def test_verified_noop_human_required_enters_pipeline_instead_of_stopping(tmp_path):
    repo = _clean_repository(tmp_path / "repo")
    package = WorkPackage(
        id="noop",
        title="Verified no-op",
        requirements=["Preserve the already-satisfied behavior"],
        acceptance_criteria=[
            AcceptanceCriterion(
                id="works",
                description="Behavior remains verified",
                verified=True,
                evidence="verification and final review already passed",
            )
        ],
        affected_repositories=["repo"],
        stage=WorkPackageStage.READY_TO_COMMIT,
        status="running",
    )
    graph, report = normalize_work_packages([package])
    orch = ProjectOrchestrator(
        "daemon-verified-noop",
        config=OrchestrationConfig(
            state_dir=tmp_path / "state",
            allow_verified_noop_commits=True,
        ),
        repository_paths={"repo": repo},
        workspace_root=tmp_path,
    )
    orch.initialize_graph(graph, report)
    orch.transition_to(TaskExecutionState.RUNNING)
    package = orch._state_record.plan_graph.package_by_id("noop")
    orch._escalate_commit_failure(
        package, "no changes detected in affected repositories"
    )

    result = run_until_terminal(orch, sleep=lambda _seconds: None)

    assert result.final_state == TaskExecutionState.COMPLETED
    assert result.attempts == 1
    events = [entry.event_type for entry in orch._journal.read()]
    assert "verified_noop_commit_check_reconciled" in events
    assert "verified_noop_commit" in events


def test_persisted_clean_start_cache_check_enters_pipeline_and_completes(tmp_path):
    repo = _clean_repository(tmp_path / "repo")
    package = WorkPackage(
        id="cache-cleanup",
        title="Cache cleanup",
        requirements=["Complete the package"],
        acceptance_criteria=[
            AcceptanceCriterion(
                id="works",
                description="Works",
                verified=True,
                evidence="fixture pre-verified",
            )
        ],
        affected_repositories=["repo"],
        stage=WorkPackageStage.PREPARE,
    )
    graph, report = normalize_work_packages([package])
    orch = ProjectOrchestrator(
        "daemon-scope-cache",
        config=OrchestrationConfig(
            state_dir=tmp_path / "state",
            strict_checks=True,
            require_structured_agent_output=False,
            require_verification=False,
            require_acceptance_evidence=False,
            require_repository_changes=False,
            auto_commit=False,
            scope_recovery_policy=ScopeRecoveryPolicy(
                enabled=True,
                auto_resume=True,
            ),
        ),
        repository_paths={"repo": repo},
        workspace_root=tmp_path,
    )
    orch.register_agent(
        _FakeAgentAdapter(
            "solo",
            {AgentCapability.IMPLEMENT, AgentCapability.REVIEW},
        )
    )
    orch.register_agent(
        _FakeAgentAdapter(
            "independent-reviewer",
            {AgentCapability.REVIEW},
        )
    )
    orch.initialize_graph(graph, report)
    orch.transition_to(TaskExecutionState.RUNNING)
    package = orch._state_record.plan_graph.package_by_id("cache-cleanup")
    cache = repo / "tests" / "__pycache__" / "generated.pyc"
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"\0cache")
    orch._scope_recovery_coordinator.escalate_scope_failure(
        package,
        "workspace must be clean before a new package starts; "
        "repo: tests/__pycache__/generated.pyc",
    )

    result = run_until_terminal(orch, sleep=lambda _seconds: None)

    assert result.final_state == TaskExecutionState.COMPLETED
    assert result.attempts == 1
    assert not cache.exists()
    events = [entry.event_type for entry in orch._journal.read()]
    assert "scope_recovery_artifacts_removed" in events
    assert "repository_scope_check_reconciled" in events


def test_operator_pause_stops_current_daemon_but_explicit_new_run_resumes(
    tmp_path, monkeypatch
):
    config = OrchestrationConfig(
        strict_checks=False, auto_commit=False, state_dir=tmp_path / "state"
    )
    orch = ProjectOrchestrator("daemon-planned-pause", config=config)
    orch.transition_to(TaskExecutionState.VALIDATING_PLAN)
    calls: list[str] = []

    def reach_pause() -> None:
        calls.append("pause")
        orch.transition_to(TaskExecutionState.RUNNING)
        orch.transition_to(TaskExecutionState.OPERATOR_PAUSED)

    monkeypatch.setattr(orch, "run_pipeline", reach_pause)
    first = run_until_terminal(orch, sleep=_RecordingSleep())

    assert first.final_state == TaskExecutionState.OPERATOR_PAUSED
    assert first.attempts == 1
    assert calls == ["pause"]

    def resume_and_complete() -> None:
        calls.append("resume")
        orch.transition_to(TaskExecutionState.RUNNING)
        orch.transition_to(TaskExecutionState.FINAL_VALIDATION)
        orch.transition_to(TaskExecutionState.COMPLETED)

    monkeypatch.setattr(orch, "run_pipeline", resume_and_complete)
    second = run_until_terminal(orch, sleep=_RecordingSleep())

    assert second.final_state == TaskExecutionState.COMPLETED
    assert second.attempts == 1
    assert calls == ["pause", "resume"]
