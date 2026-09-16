"""PLAN §M08B exit-criteria test: a synthetic multi-work-package project
driven end to end through dependency-ordered scheduling, agent failure and
cross-provider failover, resource-pressure pause/resume, a daemon retry
loop, and a simulated process restart — asserting the whole chain behaves
as one flow and lands on COMPLETED with no duplicate commits or agent
effects.

Deterministic verification-registry gating (fast/targeted/full verify
command execution and pass/fail-based commit blocking) is wired through
`_run_verification()` in `_process_package()` and tested separately in
`test_orchestrate_orchestrator.py` (known-failure matching, retry budget,
environment-blocked bypass, escalation on exhaustion). This file covers
the cross-cutting lifecycle that those focused tests intentionally isolate
away from.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from execraft.orchestrate.daemon import DaemonConfig, run_until_terminal
from execraft.orchestrate.models import TaskExecutionState
from execraft.orchestrate.orchestrator import OrchestrationConfig, ProjectOrchestrator
from execraft.orchestrate.resource import ResourceInventory
from execraft.orchestrate.scheduler import AgentAdapter, AgentCapability, Availability, StructuredHandoff
from execraft.orchestrate.verification import CHEAP, VerificationCommand, VerificationRegistry


class _RecordingSleep:
    def __init__(self):
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class _FlakyAgentAdapter(AgentAdapter):
    """Fails its first `fail_times` calls, then succeeds — simulates a
    provider that crashes/rate-limits and later recovers."""

    def __init__(self, provider_id: str, caps: set[AgentCapability], *, fail_times: int = 0):
        self._id = provider_id
        self._caps = caps
        self._fail_times = fail_times
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
            raise RuntimeError("provider unavailable: transient tool crash")
        self.executions.append(handoff)
        return {"ok": True, "work_package_id": handoff.work_package_id}


class _MidRunPressureResourceManager:
    """Disk pressure appears only after `pressure_after_checks` inventory
    checks (so the first work package completes before pressure hits), is
    not resolved by cleanup alone, and only clears after `resumable_after`
    resume probes — modelling real disk pressure that a human/automation
    resolves out of band while the daemon keeps polling."""

    def __init__(self, *, pressure_after_checks: int, resumable_after: int):
        self._checks = 0
        self._pressure_after_checks = pressure_after_checks
        self._cleanup_done = False
        self._resume_calls = 0
        self._resumable_after = resumable_after
        self.cleanup_calls = 0

    def inventory(self, workspace_roots=None) -> ResourceInventory:
        return ResourceInventory(filesystem_used_percent=91)

    def needs_cleanup(self, inventory=None) -> bool:
        self._checks += 1
        if self._checks <= self._pressure_after_checks:
            return False
        return not self._cleanup_done

    def cleanup(self, workspace_roots=None, dry_run=False) -> list[str]:
        self.cleanup_calls += 1
        self._cleanup_done = True
        return ["remove stale managed workspace: /fake/workspace"]

    def needs_pause(self, inventory=None) -> bool:
        # Cleanup alone never fully resolves pressure in this scenario;
        # only can_resume() (probed by the daemon's resume attempts) does.
        return True

    def can_resume(self, inventory=None) -> bool:
        self._resume_calls += 1
        return self._resume_calls > self._resumable_after


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


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


class _ScriptedCommandRunner:
    """Records every invocation and returns scripted returncodes in order
    (repeating the last entry once exhausted). Never touches a real
    subprocess."""

    def __init__(self, returncodes: list[int]):
        self._returncodes = list(returncodes)
        self.calls: list[str] = []

    def __call__(self, command: str, *, cwd, timeout):
        self.calls.append(command)
        idx = min(len(self.calls) - 1, len(self._returncodes) - 1)
        return _FakeCompletedProcess(self._returncodes[idx], stdout=f"ran: {command}")


_PLAN = """## Package: Foundation

### Acceptance criteria
- [ ] Core scaffolding exists

## Package: Features

Dependencies: Foundation

Affected repositories: repo-a, repo-b

### Acceptance criteria
- [ ] Features build on the foundation

## Package: Polish

Dependencies: Features

### Acceptance criteria
- [ ] Rough edges are cleaned up
"""


class TestSyntheticMultiPackageProjectEndToEnd:
    def _register_agents(self, orch, flaky_impl, backup_impl):
        orch.register_agent(flaky_impl)
        orch.register_agent(backup_impl)
        orch.register_agent(
            _FlakyAgentAdapter("reviewer", {AgentCapability.REVIEW})
        )

    def test_full_chain_completes_with_failover_pressure_and_restart(self, tmp_path):
        state_dir = tmp_path / "state"
        resource_manager = _MidRunPressureResourceManager(
            pressure_after_checks=1, resumable_after=1
        )
        # `flaky_impl` fails exactly once: package #1's implement stage
        # fails over to `backup_impl`, but by package #2 the "provider" has
        # recovered on its own (modelling a transient crash that clears)
        # and needs no failover at all.
        flaky_impl = _FlakyAgentAdapter(
            "flaky-impl", {AgentCapability.IMPLEMENT}, fail_times=1
        )
        backup_impl = _FlakyAgentAdapter(
            "backup-impl", {AgentCapability.IMPLEMENT}, fail_times=0
        )

        config = OrchestrationConfig(strict_checks=False, auto_commit=False, state_dir=state_dir)
        repositories = {
            "repo-a": _repo(tmp_path / "repo-a"),
            "repo-b": _repo(tmp_path / "repo-b"),
        }
        first = ProjectOrchestrator(
            "e2e-project",
            config=config,
            resource_manager=resource_manager,
            repository_paths=repositories,
            workspace_root=tmp_path,
        )
        self._register_agents(first, flaky_impl, backup_impl)
        report = first.initialize(_PLAN)
        assert not report.has_errors()
        assert first.state == TaskExecutionState.VALIDATING_PLAN

        sleeper = _RecordingSleep()
        daemon_config = DaemonConfig(initial_backoff_seconds=1.0, backoff_multiplier=2.0)

        result = run_until_terminal(first, config=daemon_config, sleep=sleeper)

        # First daemon attempt: Foundation completes (with failover), then
        # pressure hits before Features -> PAUSED_LOW_DISK, not terminal.
        # Second attempt: still blocked (can_resume() call #1 is False).
        # Third attempt: resumes and runs Features + Polish to completion.
        assert result.attempts == 3
        assert sleeper.calls == [1.0, 2.0]
        assert result.final_state == TaskExecutionState.COMPLETED

        status = first.status_report()
        assert status["completed_packages"] == 3
        assert status["pending_packages"] == 0

        # Provider unavailability + cross-provider failover on package #1,
        # followed by deterministic rotation across the recovered workers.
        assert flaky_impl.call_count == 2
        assert backup_impl.call_count == 2
        assert len(flaky_impl.executions) + len(backup_impl.executions) == 3
        events = [e.event_type for e in first._journal.read()]
        assert "agent_failure" in events
        assert "agent_failover" in events
        assert "human_intervention_required" not in events

        # Resource-pressure pause/recovery.
        assert "resource_pressure_detected" in events
        assert "resource_pause" in events
        assert "resource_resume_blocked" in events
        assert "resource_resumed" in events
        assert resource_manager.cleanup_calls == 1

        # Multi-repository commit: exactly one committed transaction per
        # package, none duplicated across the pause/resume/restart cycle.
        committed = [
            tx for tx in first._commit_journal._all() if tx.status == "committed"
        ]
        assert sorted(tx.work_package_id for tx in committed) == [
            "features",
            "foundation",
            "polish",
        ]
        features_tx = next(tx for tx in committed if tx.work_package_id == "features")
        assert sorted(s.repository_id for s in features_tx.post_snapshots) == [
            "repo-a",
            "repo-b",
        ]

        # --- Process restart: a fresh orchestrator instance picks up where
        # the last one left off, purely from durable state, and produces no
        # further effects since the project is already COMPLETED.
        restarted = ProjectOrchestrator(
            "e2e-project",
            config=config,
            resource_manager=resource_manager,
            repository_paths=repositories,
            workspace_root=tmp_path,
        )
        self._register_agents(restarted, flaky_impl, backup_impl)
        restarted.load_state()
        assert restarted.state == TaskExecutionState.COMPLETED

        restart_sleeper = _RecordingSleep()
        restart_result = run_until_terminal(restarted, sleep=restart_sleeper)
        assert restart_result.attempts == 0
        assert restart_result.final_state == TaskExecutionState.COMPLETED
        assert restart_sleeper.calls == []
        # No package was re-implemented, re-reviewed, or re-committed.
        assert flaky_impl.call_count == 2
        assert backup_impl.call_count == 2
        recommitted = [
            tx for tx in restarted._commit_journal._all() if tx.status == "committed"
        ]
        assert len(recommitted) == 3


class TestVerificationFailureCaughtAndFixedBeforeCommit:
    """Closes the gap the previous slice's docstring called out explicitly:
    deterministic verification is now real, so the synthetic project must
    also demonstrate a verification failure being caught and retried to a
    fix before commit, combined with agent failover, in one flow."""

    def test_two_packages_with_a_verification_retry_and_an_agent_failover(self, tmp_path):
        config = OrchestrationConfig(strict_checks=False, auto_commit=False, state_dir=tmp_path / "state")
        registry = VerificationRegistry(
            commands=[VerificationCommand(command="run-tests", profile=CHEAP)]
        )
        # Alpha's implementer crashes once (failover to a backup agent);
        # Alpha's first verification attempt then fails and must be
        # retried before it can commit. Beta needs neither: by the time
        # its stages run, the implementer has already recovered and
        # verification passes on the first try.
        runner = _ScriptedCommandRunner([1, 0, 0])
        flaky_impl = _FlakyAgentAdapter(
            "flaky-impl", {AgentCapability.IMPLEMENT}, fail_times=1
        )
        backup_impl = _FlakyAgentAdapter(
            "backup-impl", {AgentCapability.IMPLEMENT}, fail_times=0
        )
        reviewer = _FlakyAgentAdapter("reviewer", {AgentCapability.REVIEW})

        orch = ProjectOrchestrator(
            "verify-retry-project",
            config=config,
            registry=registry,
            command_runner=runner,
        )
        orch.register_agent(flaky_impl)
        orch.register_agent(backup_impl)
        orch.register_agent(reviewer)

        plan = """## Package: Alpha

### Acceptance criteria
- [ ] Alpha works

## Package: Beta

### Acceptance criteria
- [ ] Beta works
"""
        report = orch.initialize(plan)
        assert not report.has_errors()

        orch.run_pipeline()

        assert orch.state == TaskExecutionState.COMPLETED
        assert orch.status_report()["completed_packages"] == 2

        assert flaky_impl.call_count == 2
        assert backup_impl.call_count == 2
        assert len(flaky_impl.executions) + len(backup_impl.executions) == 3
        assert len(runner.calls) == 3  # Alpha fails once, then Alpha + Beta each pass once

        events = [e.event_type for e in orch._journal.read()]
        assert "agent_failure" in events
        assert "agent_failover" in events
        verify_failed = [
            e for e in orch._journal.read() if e.event_type == "verification_failed"
        ]
        assert len(verify_failed) == 1
        assert verify_failed[0].payload["package_id"] == "alpha"
        assert verify_failed[0].payload["attempt"] == 1
        assert "human_intervention_required" not in events

        committed = [
            tx for tx in orch._commit_journal._all() if tx.status == "committed"
        ]
        assert sorted(tx.work_package_id for tx in committed) == ["alpha", "beta"]
