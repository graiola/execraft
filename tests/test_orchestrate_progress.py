"""Tests for bounded, human-readable orchestration progress reporting."""

from io import StringIO

from execraft.cli import build_parser
from execraft.orchestrate.models import TaskExecutionState
from execraft.orchestrate.orchestrator import OrchestrationConfig, ProjectOrchestrator
from execraft.orchestrate.progress import HumanProgressReporter
from execraft.orchestrate.scheduler import (
    AgentAdapter,
    AgentCapability,
    Availability,
    StructuredHandoff,
)


class _SuccessfulAgent(AgentAdapter):
    @property
    def availability(self) -> Availability:
        return Availability.AVAILABLE

    @property
    def provider_id(self) -> str:
        return "agent"

    @property
    def capabilities(self) -> set[AgentCapability]:
        return {
            AgentCapability.IMPLEMENT,
            AgentCapability.REVIEW,
            AgentCapability.FIX_REVIEW,
        }

    def execute(self, handoff: StructuredHandoff) -> dict:
        if handoff.stage in {"review", "final_review"}:
            return {"ok": True, "verdict": "approved", "findings": []}
        return {
            "ok": True,
            "status": "implemented",
            "summary": "done",
        }


def test_reporter_writes_terminal_and_append_only_log(tmp_path) -> None:
    stream = StringIO()
    log_path = tmp_path / "orchestrator.log"
    reporter = HumanProgressReporter(stream=stream, log_path=log_path)

    reporter(
        "verification_command_finished",
        {
            "package_id": "WP10",
            "repository_id": "app",
            "status": "failed",
            "duration_seconds": 2.25,
            "excerpt": "first failure line\nsecond line",
        },
    )

    terminal = stream.getvalue()
    persisted = log_path.read_text(encoding="utf-8")
    assert "WP10 verify [app] FAILED in 2.2s" in terminal
    assert "first failure line second line" in terminal
    assert terminal == persisted


def test_reporter_bounds_failure_details() -> None:
    line = HumanProgressReporter.format_event(
        "human_required",
        {"package_id": "WP10", "reason": "x" * 1000},
    )
    assert line.startswith("WP10 HUMAN_REQUIRED")
    assert len(line) < 300
    assert line.endswith("…")


def test_orchestrator_emits_progress_without_changing_pipeline_semantics(tmp_path) -> None:
    events: list[tuple[str, dict]] = []
    config = OrchestrationConfig(
        state_dir=tmp_path / "state",
        strict_checks=False,
        auto_commit=False,
        require_repository_changes=False,
    )
    orchestrator = ProjectOrchestrator(
        "progress-test",
        config=config,
        progress_callback=lambda event, payload: events.append((event, dict(payload))),
    )
    orchestrator.register_agent(_SuccessfulAgent())
    report = orchestrator.initialize(
        """## Package: Progress\n\n### Acceptance criteria\n- [ ] Emits bounded progress\n"""
    )
    assert not report.has_errors()

    orchestrator.run_pipeline()

    assert orchestrator.state == TaskExecutionState.COMPLETED
    event_types = [event for event, _ in events]
    assert event_types[0] == "pipeline_started"
    assert "package_selected" in event_types
    assert "agent_attempt_started" in event_types
    assert "agent_attempt_finished" in event_types
    attempt_events = [
        payload
        for event, payload in events
        if event in {"agent_attempt_started", "agent_attempt_finished"}
    ]
    assert attempt_events
    assert all(payload.get("stage") for payload in attempt_events)
    sessions = orchestrator._agent_console.sessions("agent")
    assert sessions
    assert all(session["status"] == "completed" for session in sessions)
    assert "package_completed" in event_types
    assert event_types[-1] == "pipeline_finished"


def test_orchestrate_parser_exposes_logging_controls() -> None:
    args = build_parser().parse_args(
        [
            "orchestrate",
            "run",
            "--project",
            "sample",
            "--task-id",
            "sample_task",
            "--quiet",
            "--log-file",
            "/tmp/execraft.log",
        ]
    )
    assert args.quiet is True
    assert str(args.log_file) == "/tmp/execraft.log"


def test_agent_transport_success_is_reported_as_completed_not_approved() -> None:
    line = HumanProgressReporter.format_event(
        "agent_attempt_finished",
        {
            "package_id": "WP10",
            "capability": "review",
            "agent_id": "claude-code",
            "status": "completed",
            "duration_seconds": 603,
        },
    )
    assert "COMPLETED" in line
    assert "PASSED" not in line



def test_reporter_distinguishes_handoff_stage_from_capability() -> None:
    line = HumanProgressReporter.format_event(
        "agent_attempt_started",
        {
            "package_id": "WP17__WP17-S4",
            "stage": "scope_recovery",
            "capability": "fix_review",
            "agent_id": "opencode-local",
            "attempt": 1,
        },
    )

    assert line.startswith("WP17__WP17-S4 scope_recovery agent=opencode-local")
    assert "capability=fix_review" in line


def test_reporter_distinguishes_supervisor_budget_from_provider_attempt() -> None:
    line = HumanProgressReporter.format_event(
        "supervisor_started",
        {
            "package_id": "WP17__WP17-S4",
            "agent_id": "codex",
            "attempt": 2,
            "max_attempts": 3,
        },
    )

    assert line == (
        "WP17__WP17-S4 Supervisor incident attempt=2/3 agent=codex"
    )

def test_orchestrate_parser_exposes_human_required_explain_action() -> None:
    args = build_parser().parse_args(
        [
            "orchestrate",
            "explain",
            "--project",
            "sample",
            "--task-id",
            "sample_task",
        ]
    )
    assert args.action == "explain"


def test_reporter_formats_agent_heartbeat() -> None:
    line = HumanProgressReporter.format_event(
        "agent_heartbeat",
        {
            "package_id": "WP10",
            "stage": "final_review",
            "agent_id": "opencode-go",
            "model": "opencode-go/deepseek-v4-flash",
            "elapsed_seconds": 750,
            "pid": 1234,
            "process_state": "sleeping",
            "process_count": 2,
            "io_active": True,
            "cpu_active": False,
            "read_bytes_delta": 2048,
            "write_bytes_delta": 512,
            "read_chars_delta": 4096,
            "write_chars_delta": 1024,
            "semantic_activity": "Run tests",
            "semantic_target": "tests/test_agent_console.py",
            "progress_state": "working",
            "progress_events": 7,
            "progress_files": 3,
            "progress_commands": 2,
            "progress_completed_tools": 4,
            "transport": "opencode-run-jsonl",
        },
    )

    assert "WP10 final_review agent=opencode-go" in line
    assert "still running elapsed=12m30s" in line
    assert "activity=Run tests" in line
    assert "target=tests/test_agent_console.py" in line
    assert "progress=working" in line
    assert "events=7 files=3 commands=2 tools_done=4" in line
    assert "pid=1234 state=sleeping processes=2" in line
    assert "transport=opencode-run-jsonl" in line
    assert "read:2.0KiB/write:512B" not in line


def test_orchestrate_parser_exposes_heartbeat_interval() -> None:
    args = build_parser().parse_args(
        [
            "orchestrate",
            "run",
            "--project",
            "sample",
            "--task-id",
            "sample_task",
            "--heartbeat-interval",
            "15",
        ]
    )
    assert args.heartbeat_interval == 15.0


def test_orchestrator_wires_heartbeat_aware_agents_to_progress(tmp_path) -> None:
    events: list[tuple[str, dict]] = []

    class HeartbeatAwareAgent(_SuccessfulAgent):
        heartbeat_callback = None
        heartbeat_interval = None

        def configure_heartbeat(self, callback, *, interval_seconds: float) -> None:
            self.heartbeat_callback = callback
            self.heartbeat_interval = interval_seconds

    adapter = HeartbeatAwareAgent()
    orchestrator = ProjectOrchestrator(
        "heartbeat-wiring",
        config=OrchestrationConfig(
            state_dir=tmp_path / "state",
            agent_heartbeat_interval_seconds=17.0,
        ),
        progress_callback=lambda event, payload: events.append((event, dict(payload))),
    )
    orchestrator.register_agent(adapter)

    assert adapter.heartbeat_interval == 17.0
    assert adapter.heartbeat_callback is not None
    adapter.heartbeat_callback(
        {
            "package_id": "WP10",
            "stage": "review",
            "agent_id": "agent",
            "elapsed_seconds": 17,
            "pid": 123,
        }
    )
    assert events[-1][0] == "agent_heartbeat"
    assert events[-1][1]["pid"] == 123


def test_reporter_formats_agent_wait_and_poll_events() -> None:
    waiting = HumanProgressReporter.format_event(
        "agent_waiting",
        {
            "package_id": "M10A",
            "capability": "review",
            "delay_seconds": 300,
            "cycle": 2,
            "candidate_count": 2,
        },
    )
    polling = HumanProgressReporter.format_event(
        "agent_polling",
        {"package_id": "M10A", "capability": "review", "cycle": 2},
    )

    assert "waiting 5m00s before poll" in waiting
    assert "M10A polling review providers cycle=2" == polling


def test_orchestrate_parser_waits_for_agents_by_default() -> None:
    args = build_parser().parse_args(
        [
            "orchestrate",
            "run",
            "--project",
            "sample",
            "--task-id",
            "sample_task",
        ]
    )
    assert args.no_wait_for_agents is False

    one_shot = build_parser().parse_args(
        [
            "orchestrate",
            "run",
            "--project",
            "sample",
            "--task-id",
            "sample_task",
            "--no-wait-for-agents",
        ]
    )
    assert one_shot.no_wait_for_agents is True


def test_pipeline_wait_log_makes_foreground_polling_explicit() -> None:
    line = HumanProgressReporter.format_event(
        "pipeline_finished", {"state": "waiting_for_agent", "error": ""}
    )

    assert line == (
        "RUN suspended state=waiting_for_agent; polling remains active"
    )


def test_reporter_formats_rebalanced_agents_and_requeue() -> None:
    rebalanced = HumanProgressReporter.format_event(
        "agents_rebalanced",
        {
            "package_id": "M10A",
            "implementer": "claude-code",
            "reviewer": "opencode-zen-free",
            "last_fixer": "codex",
            "final_reviewer": "opencode-go",
        },
    )
    requeued = HumanProgressReporter.format_event(
        "package_requeued",
        {
            "package_id": "M10A",
            "stage": "implement",
            "reason": "waiting_for_agent",
        },
    )

    assert "M10A agents rebalanced implement=claude-code" in rebalanced
    assert "review=opencode-zen-free" in rebalanced
    assert "M10A requeued stage=implement" in requeued
    assert "trying disjoint ready work" in requeued


def test_reporter_distinguishes_retry_from_reported_unblock(monkeypatch) -> None:
    import os
    import time

    previous_tz = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "Europe/Rome")
    time.tzset()
    try:
        line = HumanProgressReporter.format_event(
            "agent_waiting",
            {
                "package_id": "WP16",
                "capability": "implement",
                "delay_seconds": 60,
                "cycle": 2,
                "candidate_count": 4,
                "next_retry_agent_id": "opencode-go",
                "next_retry_agent_model": "opencode-go/GLM-5.2",
                "next_retry_reason": "provider_error",
                "next_retry_at": "2026-07-23T14:53:42.865649+00:00",
                "first_reported_unblock_agent_id": "claude-code",
                "first_reported_unblock_agent_model": "",
                "first_reported_unblock_reason": "session_limit",
                "first_reported_unblock_at": "2026-07-23T18:30:00+00:00",
                "unknown_deadline_count": 1,
                "policy_excluded_count": 1,
            },
        )
    finally:
        if previous_tz is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", previous_tz)
        time.tzset()

    assert "next_retry=opencode-go[opencode-go/GLM-5.2]" in line
    assert "at_local=2026-07-23 16:53:42+0200" in line
    assert "reason=provider_error" in line
    assert "first_reported_unblock=claude-code" in line
    assert "at_local=2026-07-23 20:30:00+0200" in line
    assert "reason=session_limit" in line
    assert "unknown_deadlines=1" in line
    assert "complexity_excluded=1" in line


def test_reporter_converts_provider_skip_deadline_to_local_time(monkeypatch) -> None:
    import os
    import time

    previous_tz = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "Europe/Rome")
    time.tzset()
    try:
        line = HumanProgressReporter.format_event(
            "provider_skipped",
            {
                "package_id": "WP16",
                "capability": "implement",
                "agent_id": "claude-code",
                "reason": "session_limit",
                "unavailable_until": "2026-07-23T18:30:00.000035+00:00",
            },
        )
    finally:
        if previous_tz is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", previous_tz)
        time.tzset()

    assert "until_local=2026-07-23 20:30:00+0200" in line


def test_reporter_includes_selected_package_complexity() -> None:
    line = HumanProgressReporter.format_event(
        "package_selected",
        {
            "package_id": "WP16",
            "stage": "final_review",
            "title": "Release gate",
            "complexity": 97,
        },
    )

    assert line == "WP16 selected stage=final_review complexity=97 — Release gate"


def test_reporter_names_providers_excluded_by_complexity() -> None:
    line = HumanProgressReporter.format_event(
        "agent_waiting",
        {
            "package_id": "WP16",
            "capability": "review",
            "delay_seconds": 300,
            "cycle": 1,
            "candidate_count": 1,
            "policy_excluded_count": 2,
            "policy_excluded": [
                {
                    "agent_id": "opencode-ollama-gpu-a-coder",
                    "model": "ollama-gpu-a/qwen3-coder:30b-16k",
                    "task_complexity": 97,
                    "max_complexity": 75,
                },
                {
                    "agent_id": "opencode-ollama-gpu-a",
                    "model": "ollama-gpu-a/qwen3.5:9b",
                    "task_complexity": 97,
                    "max_complexity": 50,
                },
            ],
        },
    )

    assert "complexity_excluded=2" in line
    assert "qwen3-coder:30b-16k]:97>75" in line
    assert "qwen3.5:9b]:97>50" in line


def test_reporter_formats_bounded_agent_wait_expiry() -> None:
    line = HumanProgressReporter.format_event(
        "agent_wait_expired",
        {
            "package_id": "WP16",
            "capability": "review",
            "waited_seconds": 1801,
            "limit_seconds": 1800,
        },
    )

    assert "WP16 review provider wait expired" in line
    assert "limit=30m00s" in line
    assert "HUMAN_REQUIRED" in line


def test_progress_formats_decomposition_and_parallel_wave_events():
    assert "decompose requested" in HumanProgressReporter.format_event(
        "decomposition_requested",
        {"package_id": "WP17", "origin_stage": "implement", "complexity": 90},
    )
    decomposed = HumanProgressReporter.format_event(
        "package_decomposed",
        {
            "package_id": "WP17",
            "agent_id": "qwen",
            "shard_count": 2,
            "shards": ["WP17__a", "WP17__b"],
            "aggregate_stage": "regression_verify",
        },
    )
    assert "WP17__a,WP17__b" in decomposed
    parallel = HumanProgressReporter.format_event(
        "parallel_wave_started",
        {
            "wave_id": "wave-1",
            "packages": ["WP17__a", "WP17__b"],
            "agents": ["deepseek", "qwen"],
        },
    )
    assert "PARALLEL wave=wave-1 started" in parallel


def test_progress_formats_scope_auto_expand_and_reconciliation_events():
    expanded = HumanProgressReporter.format_event(
        "write_scope_auto_expanded",
        {
            "package_id": "WP17__S2",
            "added_paths": ["repo/tests/test_api.py", "repo/docs/verification/wp17.md"],
            "changed_lines": 84,
            "parallel": True,
        },
    )
    reconciled = HumanProgressReporter.format_event(
        "repository_scope_check_reconciled",
        {
            "package_id": "WP17__S2",
            "previous_stage": "completed",
            "next_stage": "completed",
            "automatic": True,
        },
    )

    assert "write scope auto-expanded parallel" in expanded
    assert "files=2" in expanded
    assert "changed_lines=84" in expanded
    assert "stale repository-scope check reconciled" in reconciled
    assert "completed->completed" in reconciled
    assert "automatic=true" in reconciled


def test_reporter_explains_deterministic_review_recovery() -> None:
    queued = HumanProgressReporter.format_event(
        "review_recovery_queued",
        {
            "package_id": "WP17__WP17-S4",
            "cycle": 1,
            "max_cycles": 2,
            "finding_count": 2,
            "excluded_supervisor_id": "codex",
        },
    )
    completed = HumanProgressReporter.format_event(
        "review_recovery_fixer_completed",
        {"package_id": "WP17__WP17-S4", "cycle": 1},
    )

    assert "deterministic review recovery queued cycle=1/2" in queued
    assert "findings=2" in queued
    assert "supervisor_excluded=codex" in queued
    assert "bounded fixer" in queued
    assert "regression verification and independent final review" in completed


def test_pipeline_pause_log_names_scheduled_checkpoint() -> None:
    line = HumanProgressReporter.format_event(
        "pipeline_finished", {"state": "operator_paused", "error": ""}
    )

    assert line == "RUN paused at scheduled Work Package checkpoint"


def test_reporter_formats_work_package_directive_events() -> None:
    applied = HumanProgressReporter.format_event(
        "work_package_directive_applied",
        {
            "package_id": "WP18",
            "kind": "pause_before_start",
            "enabled": True,
            "reason": "operator review",
        },
    )
    reached = HumanProgressReporter.format_event(
        "work_package_pause_before_start_reached",
        {
            "package_id": "WP18",
            "stage": "prepare",
            "reason": "operator review",
        },
    )
    consumed = HumanProgressReporter.format_event(
        "mandatory_decomposition_consumed",
        {"package_id": "WP19", "outcome": "expanded"},
    )

    assert applied == (
        "WP18 future directive pause_before_start enabled — operator review"
    )
    assert reached == (
        "WP18 scheduled pause reached before stage=prepare — operator review"
    )
    assert consumed == "WP19 mandatory decomposition completed outcome=expanded"
