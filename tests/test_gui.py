import json
import re
import subprocess
import threading
import time
from pathlib import Path

import pytest

from execraft.gui.server import (
    DashboardService,
    EndpointProbeCache,
    GuiError,
    OrchestratorProcessController,
    _dashboard_asset,
    _handler_factory,
    _is_client_disconnect,
    serve_dashboard,
)
from execraft.orchestrate import IncidentClass, IncidentStatus
from execraft.orchestrate.models import (
    PlanGraph,
    TaskExecutionState,
    TaskExecutionStateRecord,
    WorkPackage,
    WorkPackageStage,
)
from execraft.orchestrate.journal import EventJournal, default_journal_path


def test_dashboard_refuses_remote_binding_before_starting_server():
    with pytest.raises(GuiError, match="remote binding is disabled"):
        serve_dashboard(
            service=object(),  # type: ignore[arg-type]
            host="0.0.0.0",
            port=0,
            open_browser=False,
        )


def _write_project(root: Path) -> None:
    project = root / "projects" / "demo"
    task = project / "tasks" / "demo-task"
    opencode = project / "opencode"
    task.mkdir(parents=True)
    opencode.mkdir(parents=True)
    (project / "project.yaml").write_text(
        """schema_version: 2
project: demo
description: Demo dashboard project
repositories:
  - id: core
    path: .
    base_branch: main
    workspace_name: core
    required: true
agents_file: projects/demo/agents.yaml
opencode_dir: projects/demo/opencode
resources_file: projects/demo/resources.yaml
verification_file: projects/demo/verification.yaml
""",
        encoding="utf-8",
    )
    (project / "agents.yaml").write_text(
        """schema_version: 2
scheduling:
  interactive_console:
    enabled: true
  parallel_shards:
    enabled: true
    max_workers: 2
providers:
  local:
    adapter: codex
    enabled: true
    provider_id: local-codex
    capabilities: [implement, review]
    priority: 100
  satellite:
    adapter: opencode
    enabled: true
    provider_id: satellite-qwen
    model: satellite/qwen
    capabilities: [review]
    priority: 50
    concurrency_group: satellite-node
    output_silence_timeout_seconds: 900
""",
        encoding="utf-8",
    )
    (opencode / "providers.yaml").write_text(
        """schema_version: 1
endpoints:
  node-a:
    provider_id: satellite
    name: Satellite A
    base_url: http://10.0.0.2:11434/v1
    models:
      qwen: {}
""",
        encoding="utf-8",
    )
    (project / "resources.yaml").write_text("schema_version: 1\npolicy: {}\n", encoding="utf-8")
    (project / "verification.yaml").write_text("schema_version: 1\ncommands: []\n", encoding="utf-8")
    (task / "TASK.yaml").write_text(
        """schema_version: 1
id: demo-task
title: Demo
status: in_progress
git:
  branch_name: task/demo
repositories: []
integration:
  verify: []
""",
        encoding="utf-8",
    )
    (task / "PLAN.graph.yaml").write_text(
        """schema_version: 1
work_packages:
  - id: WP1
    title: Base
    requirements: [R1]
    acceptance_criteria:
      - id: A1
        description: Done
""",
        encoding="utf-8",
    )


def _write_state(state_root: Path) -> None:
    parent = WorkPackage(
        id="WP1",
        title="Parent",
        stage=WorkPackageStage.IMPLEMENT,
        status="running",
        complexity=70,
        requirements=["R1"],
        agent_id="local-codex",
        shard_ids=["WP1__review"],
    )
    shard = WorkPackage(
        id="WP1__review",
        title="Review shard",
        dependencies=[],
        stage=WorkPackageStage.REVIEW,
        status="running",
        complexity=40,
        requirements=["R1"],
        parent_id="WP1",
        shard_key="review",
        execution_mode="review_shard",
        reviewer_id="satellite-qwen",
        parallel_safe=True,
    )
    record = TaskExecutionStateRecord(
        project_id="demo-task",
        state=TaskExecutionState.RUNNING,
        plan_graph=PlanGraph([parent, shard]),
        completed_packages=0,
        total_packages=2,
        scheduler={
            "parallel_wave": {
                "wave_id": "wave-1",
                "package_ids": ["WP1__review"],
                "agents": ["satellite-qwen"],
            }
        },
    )
    path = state_root / "projects" / "demo-task" / "state.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(record.as_mapping()), encoding="utf-8")


def _service(tmp_path: Path, *, agent_command_runner=subprocess.run) -> DashboardService:
    root = tmp_path / "root"
    state_root = tmp_path / "state"
    _write_project(root)
    _write_state(state_root)
    service = DashboardService(
        root=root,
        project_id="demo",
        task_id="demo-task",
        state_root=state_root,
        agent_command_runner=agent_command_runner,
    )
    service.endpoint_cache.probe = lambda endpoints: {
        "node-a": {
            "reachable": True,
            "latency_ms": 7,
            "models": ["qwen"],
            "error": "",
            "checked_at": "2026-07-27T07:00:00+00:00",
        }
    }
    return service


def test_snapshot_exposes_workflow_parallel_assignments_and_satellite(tmp_path):
    service = _service(tmp_path)
    snapshot = service.snapshot()

    assert snapshot["project"]["id"] == "demo"
    assert len(snapshot["packages"]) == 2
    assert snapshot["orchestration"]["scheduler"]["parallel_wave"]["wave_id"] == "wave-1"
    assignment = next(item for item in snapshot["assignments"] if item["package_id"] == "WP1__review")
    assert assignment["agent_id"] == "satellite-qwen"
    assert assignment["parallel"] is True
    assert snapshot["execution_context"] == {
        "package_id": "WP1",
        "stage": "implement",
        "agent_id": "local-codex",
        "status": "running",
        "source": "assignment",
    }
    satellite = next(item for item in snapshot["nodes"] if item["id"] == "node-a")
    assert satellite["reachable"] is True
    assert satellite["agents"] == ["satellite-qwen"]
    agent = next(item for item in snapshot["agents"] if item["id"] == "satellite-qwen")
    assert agent["timeouts"]["first_output_seconds"] == 0
    assert agent["timeouts"]["output_silence_seconds"] == 900
    assert agent["timeouts"]["max_output_bytes"] == 64 * 1024 * 1024
    assert [item["id"] for item in snapshot["execution_roles"]] == [
        "decompose", "implement", "review", "fix_review", "final_review"
    ]
    assert any(item["id"] == "ai-implement" for item in snapshot["skills"])

    service.close()


def test_snapshot_keeps_prepare_stage_visible_without_an_agent_assignment(tmp_path):
    service = _service(tmp_path)
    record = service._load_state()
    assert record is not None
    parent = record.plan_graph.package_by_id("WP1")
    parent.stage = WorkPackageStage.PREPARE
    parent.status = "pending"
    parent.agent_id = ""
    shard = record.plan_graph.package_by_id("WP1__review")
    shard.stage = WorkPackageStage.COMPLETED
    shard.status = "completed"
    record.scheduler = {}
    service.state_path.write_text(
        json.dumps(record.as_mapping(), indent=2), encoding="utf-8"
    )

    snapshot = service.snapshot()

    assert snapshot["assignments"] == []
    assert snapshot["execution_context"] == {
        "package_id": "WP1",
        "stage": "prepare",
        "agent_id": "",
        "status": "pending",
        "source": "package",
    }
    service.close()


def test_snapshot_prefers_live_invocation_over_parent_verification_stage(tmp_path):
    service = _service(tmp_path)
    record = service._load_state()
    assert record is not None
    parent = record.plan_graph.package_by_id("WP1")
    parent.stage = WorkPackageStage.REGRESSION_VERIFY
    parent.status = "running"
    parent.agent_id = ""
    shard = record.plan_graph.package_by_id("WP1__review")
    shard.stage = WorkPackageStage.FINAL_REVIEW
    shard.status = "running"
    shard.final_reviewer_id = ""
    record.scheduler = {}
    service.state_path.write_text(
        json.dumps(record.as_mapping(), indent=2), encoding="utf-8"
    )
    invocation = service.agent_invocations.begin(
        project_id="demo",
        task_id="demo-task",
        package_id="WP1__review",
        stage="final_review",
        capability="review",
        attempt=1,
        agent_id="satellite-qwen",
        adapter="opencode",
        model="satellite/qwen",
        handoff={"work_package_id": "WP1__review", "stage": "final_review"},
    )
    service.process.status = lambda: {
        "owned_running": False,
        "external_running": True,
        "pid": 4242,
        "started_at": "2026-08-04T15:00:00+00:00",
        "last_exit_at": "",
        "last_exit_code": None,
        "last_exit_summary": "",
        "driver_log": "",
        "command": [],
    }

    snapshot = service.snapshot()

    assert snapshot["execution_context"] == {
        "package_id": "WP1__review",
        "stage": "final_review",
        "agent_id": "satellite-qwen",
        "status": "running",
        "source": "invocation",
        "invocation_id": invocation.invocation_id,
        "started_at": invocation.started_at,
        "model": "satellite/qwen",
    }
    assert snapshot["assignments"][0]["package_id"] == "WP1__review"
    assert snapshot["assignments"][0]["agent_id"] == "satellite-qwen"
    assert not any(
        assignment["package_id"] == "WP1"
        for assignment in snapshot["assignments"]
    )
    service.close()


def test_snapshot_exposes_every_parallel_live_invocation(tmp_path):
    service = _service(tmp_path)
    record = service._load_state()
    assert record is not None
    parent = record.plan_graph.package_by_id("WP1")
    parent.stage = WorkPackageStage.IMPLEMENT
    parent.status = "running"
    shard = record.plan_graph.package_by_id("WP1__review")
    shard.stage = WorkPackageStage.FINAL_REVIEW
    shard.status = "running"
    record.scheduler = {
        "parallel_wave": {
            "wave_id": "wave-live",
            "package_ids": ["WP1", "WP1__review"],
            "agents": ["local-codex", "satellite-qwen"],
        }
    }
    service.state_path.write_text(
        json.dumps(record.as_mapping(), indent=2), encoding="utf-8"
    )
    first = service.agent_invocations.begin(
        project_id="demo",
        task_id="demo-task",
        package_id="WP1",
        stage="implement",
        capability="implement",
        attempt=1,
        agent_id="local-codex",
        adapter="codex",
        model="openai/codex",
        handoff={"work_package_id": "WP1", "stage": "implement"},
    )
    second = service.agent_invocations.begin(
        project_id="demo",
        task_id="demo-task",
        package_id="WP1__review",
        stage="final_review",
        capability="review",
        attempt=2,
        agent_id="satellite-qwen",
        adapter="opencode",
        model="satellite/qwen",
        handoff={"work_package_id": "WP1__review", "stage": "final_review"},
    )
    service.process.status = lambda: {
        "owned_running": True,
        "external_running": False,
        "pid": 4242,
        "started_at": "2026-08-04T15:00:00+00:00",
        "last_exit_at": "",
        "last_exit_code": None,
        "last_exit_summary": "",
        "driver_log": "",
        "command": [],
    }

    snapshot = service.snapshot()

    contexts = snapshot["execution_contexts"]
    assert [item["package_id"] for item in contexts] == ["WP1__review", "WP1"]
    assert [item["agent_id"] for item in contexts] == [
        "satellite-qwen",
        "local-codex",
    ]
    assert all(item["source"] == "invocation" for item in contexts)
    assert all(item["parallel"] is True for item in contexts)
    assert {item["invocation_id"] for item in contexts} == {
        first.invocation_id,
        second.invocation_id,
    }
    assert snapshot["execution_context"] == {
        "package_id": "WP1__review",
        "stage": "final_review",
        "agent_id": "satellite-qwen",
        "status": "running",
        "source": "invocation",
        "invocation_id": second.invocation_id,
        "started_at": second.started_at,
        "model": "satellite/qwen",
    }
    service.close()


def test_snapshot_ignores_stale_open_invocation_without_active_driver(tmp_path):
    service = _service(tmp_path)
    service.agent_invocations.begin(
        project_id="demo",
        task_id="demo-task",
        package_id="WP1__review",
        stage="final_review",
        capability="review",
        attempt=1,
        agent_id="satellite-qwen",
        handoff={"work_package_id": "WP1__review", "stage": "final_review"},
    )

    snapshot = service.snapshot()

    assert snapshot["live_invocations"] == []
    assert snapshot["execution_context"]["source"] == "assignment"
    service.close()


def test_execution_trace_endpoint_aggregates_parent_and_shard_history(tmp_path):
    service = _service(tmp_path)
    invocation = service.agent_invocations.begin(
        project_id="demo",
        task_id="demo-task",
        package_id="WP1__review",
        stage="review",
        capability="review",
        attempt=1,
        agent_id="satellite-qwen",
        adapter="opencode",
        model="satellite/qwen",
        handoff={"work_package_id": "WP1__review", "stage": "review"},
    )
    service.agent_invocations.complete(
        invocation.invocation_id,
        duration_seconds=12,
        normalized_result={"verdict": "changes_requested"},
    )
    journal = EventJournal(service.storage_identity.journal_path)
    journal.append(
        "package_stage_transition",
        {
            "package_id": "WP1__review",
            "from_stage": "review",
            "to_stage": "fix_review",
        },
        timestamp="2026-08-04T16:00:00+00:00",
    )

    trace = service.execution_trace("WP1")

    assert trace["package_id"] == "WP1"
    assert trace["scope_package_ids"] == ["WP1", "WP1__review"]
    shard = next(
        lane for lane in trace["lanes"] if lane["package_id"] == "WP1__review"
    )
    provider_node = next(node for node in shard["nodes"] if node["kind"] == "agent")
    assert provider_node["agent_id"] == "satellite-qwen"
    assert provider_node["outcome"] == "changes_requested"
    assert trace["summary"]["review_loops"] == 1
    service.close()


def test_snapshot_hides_paused_work_packages_from_active_assignments(tmp_path):
    service = _service(tmp_path)
    record = service._load_state()
    assert record is not None
    record.plan_graph.package_by_id("WP1__review").operator_paused = True
    service.state_path.write_text(
        json.dumps(record.as_mapping(), indent=2), encoding="utf-8"
    )

    snapshot = service.snapshot()

    assert "WP1__review" not in {
        assignment["package_id"] for assignment in snapshot["assignments"]
    }
    assert any(
        package["id"] == "WP1__review" and package["operator_paused"]
        for package in snapshot["packages"]
    )
    service.close()


def test_run_control_requires_one_resumed_work_package_when_all_are_paused(tmp_path):
    service = _service(tmp_path)
    record = service._load_state()
    assert record is not None
    for package in record.plan_graph.work_packages:
        package.operator_paused = True
    service.state_path.write_text(
        json.dumps(record.as_mapping(), indent=2), encoding="utf-8"
    )

    control = service.snapshot()["run_control"]

    assert control["can_start"] is False
    assert control["label"] == "All Work Packages paused"
    assert "WP1" in control["reason"]
    service.close()


def test_terminal_run_control_is_not_masked_by_work_package_pause(tmp_path):
    service = _service(tmp_path)
    record = service._load_state()
    assert record is not None
    record.state = TaskExecutionState.FAILED
    for package in record.plan_graph.work_packages:
        package.operator_paused = True

    control = service._run_control(record)

    assert control["can_start"] is False
    assert control["label"] != "All Work Packages paused"
    service.close()


def test_config_save_validates_backs_up_and_detects_stale_edits(tmp_path):
    service = _service(tmp_path)
    original = service.read_config("agents")
    updated = original["content"].replace("max_workers: 2", "max_workers: 3")

    saved = service.save_config("agents", updated, original["sha256"])

    assert "max_workers: 3" in saved["content"]
    assert Path(saved["backup"]).is_file()
    assert "max_workers: 2" in Path(saved["backup"]).read_text(encoding="utf-8")
    with pytest.raises(GuiError, match="changed on disk"):
        service.save_config("agents", original["content"], original["sha256"])

    service.close()


def test_config_validation_rejects_invalid_agent_capability(tmp_path):
    service = _service(tmp_path)
    current = service.read_config("agents")["content"]
    invalid = current.replace("capabilities: [implement, review]", "capabilities: [telepathy]")

    result = service.validate_config("agents", invalid)

    assert result["valid"] is False
    assert "unsupported agent capability" in result["error"]
    service.close()


def test_process_controller_builds_normal_cli_command(tmp_path):
    controller = OrchestratorProcessController(
        root=tmp_path,
        project_id="demo",
        task_id="demo-task",
        state_root=tmp_path / "state",
    )

    command = controller.command

    assert command[:3] == [command[0], "-m", "execraft.cli"]
    assert command[3:5] == ["orchestrate", "run"]
    assert "--quiet" in command
    assert command[command.index("--project") + 1] == "demo"
    assert command[command.index("--task-id") + 1] == "demo-task"


def test_process_controller_initializes_plan_through_canonical_cli(tmp_path):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "Task initialized\n", "")

    plan_file = tmp_path / "PLAN.graph.yaml"
    plan_file.write_text("schema_version: 1\nwork_packages: []\n", encoding="utf-8")
    controller = OrchestratorProcessController(
        root=tmp_path,
        project_id="demo",
        task_id="demo-task",
        state_root=tmp_path / "state",
        run_factory=run,
    )

    result = controller.initialize(plan_file)

    command, kwargs = calls[0]
    assert command[3:5] == ["orchestrate", "init"]
    assert command[command.index("--plan-file") + 1] == str(plan_file)
    assert kwargs["cwd"] == tmp_path
    assert result["initialized"] is True
    assert "Task initialized" in result["output"]


def test_uninitialized_dashboard_enables_and_performs_plan_initialization(tmp_path):
    service = _service(tmp_path)
    service.state_path.unlink()
    service.task_lifecycle.summary = lambda: {
        "definition": {"integrity_ok": True},
    }
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "Task initialized\n", "")

    service.process._run_factory = run

    control = service._run_control(None)
    result = service.start_run()

    assert control["can_start"] is True
    assert control["label"] == "Initialize plan"
    assert result["initialized"] is True
    assert calls[0][3:5] == ["orchestrate", "init"]
    service.close()


def test_uninitialized_dashboard_blocks_drifted_definition(tmp_path):
    service = _service(tmp_path)
    service.task_lifecycle.summary = lambda: {
        "definition": {
            "integrity_ok": False,
            "changed_documents": ["PLAN.md"],
            "unexpected_documents": ["PLAN.graph.yaml"],
            "missing_documents": [],
        },
    }

    control = service._run_control(None)

    assert control["can_start"] is False
    assert "Adopt and apply" in control["reason"]
    assert "PLAN.graph.yaml" in control["reason"]
    service.close()


def test_process_controller_start_returns_without_reentrant_lock_deadlock(tmp_path):
    class FakeProcess:
        pid = 4242
        returncode = None

        @staticmethod
        def poll():
            return None

    calls = []

    def popen(command, **kwargs):
        calls.append((command, kwargs))
        return FakeProcess()

    controller = OrchestratorProcessController(
        root=tmp_path,
        project_id="demo",
        task_id="demo-task",
        state_root=tmp_path / "state",
        popen_factory=popen,
    )
    result = {}
    error = []

    def launch():
        try:
            result.update(controller.start())
        except Exception as exc:  # pragma: no cover - asserted below
            error.append(exc)

    thread = threading.Thread(target=launch, daemon=True)
    thread.start()
    thread.join(timeout=1)

    assert not thread.is_alive(), "start() deadlocked while reacquiring its own lock"
    assert not error
    assert result["owned_running"] is True
    assert result["pid"] == 4242
    assert calls


def test_driver_exit_summary_contains_only_current_launch_output(tmp_path):
    class FakeProcess:
        pid = 4242
        returncode = 1

        @staticmethod
        def poll():
            return 1

    def popen(command, **kwargs):
        stream = kwargs["stdout"]
        stream.write(b"current launch failed\n")
        return FakeProcess()

    controller = OrchestratorProcessController(
        root=tmp_path,
        project_id="demo",
        task_id="demo-task",
        state_root=tmp_path / "state",
        popen_factory=popen,
    )
    controller._driver_log.parent.mkdir(parents=True, exist_ok=True)
    controller._driver_log.write_text("old launch output\n", encoding="utf-8")

    status = controller.start()

    assert status["owned_running"] is False
    assert status["last_exit_code"] == 1
    assert "current launch failed" in status["last_exit_summary"]
    assert "old launch output" not in status["last_exit_summary"]


def test_snapshot_marks_old_gui_failure_superseded_by_durable_completion(tmp_path):
    service = _service(tmp_path)
    record = service._load_state()
    assert record is not None
    for package in record.plan_graph.work_packages:
        package.stage = WorkPackageStage.COMPLETED
        package.status = "completed"
    record.state = TaskExecutionState.COMPLETED
    record.completed_packages = record.total_packages
    record.error_message = ""
    service.state_path.write_text(
        json.dumps(record.as_mapping(), indent=2), encoding="utf-8"
    )
    service.process.status = lambda: {
        "owned_running": False,
        "external_running": False,
        "pid": None,
        "started_at": "2026-08-15T08:00:00+00:00",
        "last_exit_at": "2026-08-15T09:00:00+00:00",
        "last_exit_code": 1,
        "last_exit_summary": "Pipeline finished: human_required",
        "driver_log": "gui-driver.log",
        "command": [],
    }

    snapshot = service.snapshot()

    assert snapshot["orchestration"]["state"] == "completed"
    assert snapshot["run"]["last_exit_code"] == 1
    assert snapshot["run"]["last_exit_superseded"] is True
    service.close()


def test_snapshot_marks_human_required_driver_exit_as_expected_control_gate(tmp_path):
    service = _service(tmp_path)
    record = service._load_state()
    assert record is not None
    record.state = TaskExecutionState.HUMAN_REQUIRED
    service.state_path.write_text(
        json.dumps(record.as_mapping(), indent=2), encoding="utf-8"
    )
    service.process.status = lambda: {
        "owned_running": False,
        "external_running": False,
        "pid": None,
        "started_at": "2026-08-28T15:00:00+00:00",
        "last_exit_at": "2026-08-28T15:40:03+00:00",
        "last_exit_code": 1,
        "last_exit_summary": "Pipeline finished: human_required",
        "driver_log": "gui-driver.log",
        "command": [],
    }

    snapshot = service.snapshot()

    assert snapshot["run"]["last_exit_superseded"] is False
    assert snapshot["run"]["last_exit_expected_control_hold"] is True
    service.close()


def test_snapshot_remains_available_when_agent_config_is_broken(tmp_path):
    service = _service(tmp_path)
    agents_path = service.root / "projects" / "demo" / "agents.yaml"
    agents_path.write_text("providers: [broken\n", encoding="utf-8")

    snapshot = service.snapshot()

    assert snapshot["packages"]
    assert snapshot["agents"] == []
    assert snapshot["config_errors"]
    assert "agent registry" in snapshot["config_errors"][0]
    service.close()


def test_human_required_scope_decision_disables_run_with_action_reason(tmp_path):
    service = _service(tmp_path)
    state = json.loads(service.state_path.read_text(encoding="utf-8"))
    state["state"] = "human_required"
    service.state_path.write_text(json.dumps(state), encoding="utf-8")
    EventJournal(default_journal_path(service.state_root, service.task_id)).append(
        "human_intervention_required",
        {
            "reason": "generated shard modified paths outside write_scope",
            "recommended_decision": "Inspect and approve the exact scope expansion.",
        },
    )

    snapshot = service.snapshot()

    assert snapshot["run_control"]["can_start"] is False
    assert snapshot["run_control"]["label"] == "Action required"
    assert "approve the exact scope" in snapshot["run_control"]["reason"]
    with pytest.raises(GuiError, match="approve the exact scope"):
        service.start_run()
    service.close()


def _mark_protected_scope_check(service: DashboardService) -> None:
    record = service._load_state()
    assert record is not None
    record.state = TaskExecutionState.HUMAN_REQUIRED
    service.state_path.write_text(json.dumps(record.as_mapping()), encoding="utf-8")
    EventJournal(default_journal_path(service.state_root, service.task_id)).append(
        "human_intervention_required",
        {
            "package_id": "WP1",
            "stage": "scope",
            "reason": "generated shard modified paths outside write_scope",
            "evidence": ["repository scope check requires operator approval"],
            "recommended_decision": "Inspect and approve the exact scope expansion.",
        },
    )
    incident = service.supervisor_incidents.open_or_reuse(
        fingerprint="protected-scope",
        package_id="WP1",
        stage="scope",
        classification=IncidentClass.WORKSPACE_SCOPE,
        escalation_sequence=1,
        supervisor_agent_id="local-codex",
        workspace_digest="digest",
    )
    incident.summary = (
        "Supervisor cannot autonomously acquire protected paths: "
        "core:.github/workflows/ci.yml, core:.github/workflows/release.yml"
    )
    incident.status = IncidentStatus.EXHAUSTED
    service.supervisor_incidents.save(incident)


def test_protected_scope_check_surfaces_explicit_gui_approval(tmp_path):
    service = _service(tmp_path)
    _mark_protected_scope_check(service)

    control = service.snapshot()["run_control"]

    assert control["can_start"] is False
    assert control["label"] == "Protected scope approval required"
    assert control["scope_approval"] == {
        "available": True,
        "package_id": "WP1",
        "paths": [
            "core:.github/workflows/ci.yml",
            "core:.github/workflows/release.yml",
        ],
        "reason": (
            "Supervisor cannot autonomously acquire protected paths: "
            "core:.github/workflows/ci.yml, core:.github/workflows/release.yml"
        ),
        "incident_id": control["scope_approval"]["incident_id"],
    }
    assert control["scope_approval"]["incident_id"]
    service.close()


def test_protected_scope_preview_uses_authoritative_current_candidates(tmp_path):
    service = _service(tmp_path)
    _mark_protected_scope_check(service)
    service._scope_cli_json = lambda package_id, **_kwargs: {
        "package_id": package_id,
        "stage": "final_review",
        "affected_repositories": ["core"],
        "write_scope": ["core/src"],
        "workspace_scope": {
            "candidates": [
                {
                    "path": "core:.github/workflows/ci.yml",
                    "relationship": "outside_write_scope",
                },
                {
                    "path": "core:.github/workflows/release.yml",
                    "relationship": "outside_write_scope",
                },
            ]
        },
    }

    preview = service.scope_approval_preview("WP1")

    assert preview["candidate_paths"] == [
        "core:.github/workflows/ci.yml",
        "core:.github/workflows/release.yml",
    ]
    assert preview["protected_paths"] == preview["candidate_paths"]
    service.close()


def test_protected_scope_approval_passes_preview_guard_and_resumes(tmp_path):
    service = _service(tmp_path)
    _mark_protected_scope_check(service)
    expected = [
        "core:.github/workflows/ci.yml",
        "core:.github/workflows/release.yml",
    ]
    calls = []

    def fake_scope(package_id, *, accept=False, expected_candidates=None):
        calls.append((package_id, accept, expected_candidates))
        if accept:
            record = service._load_state()
            assert record is not None
            record.state = TaskExecutionState.RUNNING
            service.state_path.write_text(
                json.dumps(record.as_mapping()),
                encoding="utf-8",
            )
        return {
            "package_id": package_id,
            "stage": "regression_verify",
            "workspace_scope": {"candidates": []},
            "added_paths": expected,
        }

    service._scope_cli_json = fake_scope
    starts = []
    service.process.start = lambda **kwargs: starts.append(kwargs) or {
        "owned_running": True,
        "pid": 321,
    }

    result = service.approve_protected_scope(
        "WP1",
        expected_candidates=expected,
    )

    assert result["approved"] is True
    assert calls == [("WP1", True, expected)]
    assert starts == [{}]
    service.close()


def test_stale_protected_scope_check_falls_through_to_resume(tmp_path):
    service = _service(tmp_path)
    _mark_protected_scope_check(service)
    agents_path = service.root / "projects" / "demo" / "agents.yaml"
    agents_path.write_text(
        agents_path.read_text(encoding="utf-8").replace(
            "  interactive_console:\n    enabled: true\n",
            "  interactive_console:\n    enabled: true\n"
            "  scope_recovery:\n    enabled: true\n    auto_resume: true\n",
        ),
        encoding="utf-8",
    )
    service._currently_dirty_scope_paths = lambda _paths: []
    starts = []
    service.process.start = lambda **kwargs: starts.append(kwargs) or {
        "owned_running": True
    }

    control = service.snapshot()["run_control"]
    result = service.start_run()

    assert control["can_start"] is True
    assert control["label"] == "Recover workspace / Resume"
    assert result["owned_running"] is True
    assert starts == [{"no_wait_for_agents": False}]
    service.close()


def test_manual_commit_cannot_bypass_active_protected_scope_check(tmp_path):
    service = _service(tmp_path)
    _mark_protected_scope_check(service)

    with pytest.raises(GuiError, match="approved through the exact-scope action"):
        service.commit_workspace_changes(
            selections={"core": [".github/workflows/ci.yml"]},
            expected_digests={"core": "digest"},
            subject="Bypass protected scope",
            body="",
            reviewed=True,
        )

    service.close()


def test_human_required_scope_decision_offers_bounded_auto_recovery(tmp_path):
    service = _service(tmp_path)
    agents_path = service.root / "projects" / "demo" / "agents.yaml"
    content = agents_path.read_text(encoding="utf-8")
    agents_path.write_text(
        content.replace(
            "  interactive_console:\n    enabled: true\n",
            "  interactive_console:\n    enabled: true\n"
            "  automatic_recovery: true\n"
            "  scope_recovery:\n"
            "    max_resume_attempts: 2\n",
        ),
        encoding="utf-8",
    )
    state = json.loads(service.state_path.read_text(encoding="utf-8"))
    state["state"] = "human_required"
    service.state_path.write_text(json.dumps(state), encoding="utf-8")
    EventJournal(default_journal_path(service.state_root, service.task_id)).append(
        "human_intervention_required",
        {
            "package_id": "WP1",
            "stage": "scope",
            "blocked_requirement": "repository scope or clean-start check failed",
            "evidence": ["generated shard modified paths outside write_scope"],
            "recommended_decision": (
                "restore a clean workspace or amend the declared repository scope"
            ),
        },
    )
    starts = []
    service.process.start = lambda **kwargs: starts.append(kwargs) or {
        "owned_running": True
    }

    snapshot = service.snapshot()
    result = service.start_run()

    assert snapshot["run_control"]["can_start"] is True
    assert snapshot["run_control"]["label"] == "Recover workspace / Resume"
    assert "complete cross-repository delta" in snapshot["run_control"]["reason"]
    assert result["owned_running"] is True
    assert starts == [{"no_wait_for_agents": False}]
    service.close()


def test_completed_scope_check_offers_reconcile_and_resume(tmp_path):
    service = _service(tmp_path)
    state = json.loads(service.state_path.read_text(encoding="utf-8"))
    state["state"] = "human_required"
    for package in state["plan_graph"]["work_packages"]:
        if package["id"] == "WP1":
            package["stage"] = "completed"
            package["status"] = "completed"
    service.state_path.write_text(json.dumps(state), encoding="utf-8")
    EventJournal(default_journal_path(service.state_root, service.task_id)).append(
        "human_intervention_required",
        {
            "package_id": "WP1",
            "stage": "scope",
            "blocked_requirement": "repository scope or clean-start check failed",
            "evidence": ["generated shard modified paths outside write_scope"],
            "recommended_decision": (
                "restore a clean workspace or amend the declared repository scope"
            ),
        },
    )
    starts = []
    service.process.start = lambda **kwargs: starts.append(kwargs) or {
        "owned_running": True
    }

    snapshot = service.snapshot()
    result = service.start_run()

    assert snapshot["run_control"]["can_start"] is True
    assert snapshot["run_control"]["label"] == "Reconcile / Resume"
    assert result["owned_running"] is True
    assert starts == [{"no_wait_for_agents": False}]
    service.close()


def test_config_validation_rejects_invalid_scope_policy(tmp_path):
    service = _service(tmp_path)
    current = service.read_config("agents")["content"]
    invalid = current.replace(
        "parallel_shards:\n    enabled: true",
        "scope_policy:\n    max_files: 0\n  parallel_shards:\n    enabled: true",
    )

    result = service.validate_config("agents", invalid)

    assert result["valid"] is False
    assert "scope policy max_files must be positive" in result["error"]
    service.close()



def test_config_validation_rejects_invalid_scope_recovery_policy(tmp_path):
    service = _service(tmp_path)
    current = service.read_config("agents")["content"]
    invalid = current.replace(
        "parallel_shards:\n    enabled: true",
        "scope_recovery:\n    max_files: 0\n  parallel_shards:\n    enabled: true",
    )

    result = service.validate_config("agents", invalid)

    assert result["valid"] is False
    assert "scope recovery policy max_files must be positive" in result["error"]
    service.close()


def test_config_validation_rejects_invalid_scope_resume_attempts(tmp_path):
    service = _service(tmp_path)
    current = service.read_config("agents")["content"]
    invalid = current.replace(
        "parallel_shards:\n    enabled: true",
        "scope_recovery:\n    enabled: true\n    max_resume_attempts: 0\n"
        "  parallel_shards:\n    enabled: true",
    )

    result = service.validate_config("agents", invalid)

    assert result["valid"] is False
    assert "max_resume_attempts must be positive" in result["error"]
    service.close()

def test_legacy_provider_human_required_state_can_resume(tmp_path):
    service = _service(tmp_path)
    state = json.loads(service.state_path.read_text(encoding="utf-8"))
    state["state"] = "human_required"
    service.state_path.write_text(json.dumps(state), encoding="utf-8")
    EventJournal(default_journal_path(service.state_root, service.task_id)).append(
        "human_intervention_required",
        {
            "reason": "No configured available agent could run this stage.",
            "recommended_decision": "Resolve provider availability and resume.",
        },
    )
    starts = []
    service.process.start = lambda **kwargs: starts.append(kwargs) or {"owned_running": True}

    snapshot = service.snapshot()
    result = service.start_run()

    assert snapshot["run_control"]["can_start"] is True
    assert snapshot["run_control"]["label"] == "Resume execution-agent wait"
    assert result["owned_running"] is True
    assert starts == [{"no_wait_for_agents": False}]
    service.close()


def test_read_log_can_select_gui_driver_output(tmp_path):
    service = _service(tmp_path)
    driver_path = Path(service.process.status()["driver_log"])
    driver_path.parent.mkdir(parents=True, exist_ok=True)
    driver_path.write_text("driver-only failure\n", encoding="utf-8")

    result = service.read_log(source="driver")

    assert result["source"] == "driver"
    assert result["path"] == str(driver_path)
    assert result["content"] == "driver-only failure\n"
    with pytest.raises(GuiError, match="unsupported log source"):
        service.read_log(source="unknown")
    service.close()


def _wait_for_agent_action(service: DashboardService, agent_id: str) -> dict:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        agent = next(item for item in service.snapshot()["agents"] if item["id"] == agent_id)
        action = agent["action"]
        if action.get("status") in {"succeeded", "failed"}:
            return action
        time.sleep(0.01)
    raise AssertionError(f"agent action did not finish for {agent_id}")


def test_reset_health_action_updates_provider_and_snapshot(tmp_path):
    service = _service(tmp_path)
    service.health_store.mark_failure(
        "satellite-qwen",
        reason="quota_exhausted",
        detail="test quota",
    )
    before = next(
        item for item in service.snapshot()["agents"] if item["id"] == "satellite-qwen"
    )
    assert before["health"]["status"] == "blocked"

    started = service.start_agent_action("satellite-qwen", "reset-health")
    action = _wait_for_agent_action(service, "satellite-qwen")
    after = next(
        item for item in service.snapshot()["agents"] if item["id"] == "satellite-qwen"
    )

    assert started["status"] == "running"
    assert action["status"] == "succeeded"
    assert "does not restore quota" in action["stdout"]
    assert after["health"]["status"] == "available"
    service.close()


def test_doctor_action_runs_scoped_live_probe_and_exposes_result(tmp_path):
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='[{"provider_id":"satellite-qwen","smoke_test":"passed"}]',
            stderr="",
        )

    service = _service(tmp_path, agent_command_runner=runner)

    service.start_agent_action("satellite-qwen", "doctor", timeout_seconds=45)
    action = _wait_for_agent_action(service, "satellite-qwen")

    assert action["status"] == "succeeded"
    assert '"smoke_test":"passed"' in action["stdout"]
    command, kwargs = calls[0]
    assert command[3:5] == ["agents", "doctor"]
    assert command[command.index("--agent") + 1] == "satellite-qwen"
    assert "--smoke-test" in command
    assert "--json" in command
    assert command[command.index("--timeout-seconds") + 1] == "45"
    assert kwargs["timeout"] == 75
    service.close()


def test_agent_actions_are_rejected_while_orchestrator_is_active(tmp_path):
    service = _service(tmp_path)
    service.process.status = lambda: {
        "owned_running": False,
        "external_running": True,
        "pid": None,
        "started_at": "",
        "last_exit_code": None,
        "driver_log": "",
        "command": [],
    }

    with pytest.raises(GuiError, match="disabled while an orchestrator driver is active"):
        service.start_agent_action("satellite-qwen", "doctor")

    service.close()


def _limit_satellite_review_complexity(tmp_path: Path, ceiling: int = 75) -> None:
    path = tmp_path / "root" / "projects" / "demo" / "agents.yaml"
    content = path.read_text(encoding="utf-8")
    content = content.replace(
        "    priority: 50\n    concurrency_group: satellite-node\n",
        f"    priority: 50\n    max_complexity_by_capability:\n      review: {ceiling}\n    concurrency_group: satellite-node\n",
    )
    path.write_text(content, encoding="utf-8")


def test_provider_promotion_is_hot_while_orchestrator_is_active(tmp_path):
    service = _service(tmp_path)
    _limit_satellite_review_complexity(tmp_path)
    service.process.status = lambda: {
        "owned_running": False,
        "external_running": True,
        "pid": None,
        "started_at": "",
        "last_exit_code": None,
        "driver_log": "",
        "command": [],
    }

    result = service.set_agent_promotion(
        "satellite-qwen",
        capabilities=["review"],
        promoted_max_complexity=100,
        duration_seconds=4 * 60 * 60,
        fallback_only=True,
        reason="Claude and Codex temporarily unavailable",
    )
    agent = next(
        item for item in service.snapshot()["agents"] if item["id"] == "satellite-qwen"
    )

    assert result["hot_reload"] is True
    assert result["promotions"][0]["base_max_complexity"] == 75
    assert agent["max_complexity"]["review"] == 75
    assert agent["effective_max_complexity"]["review"] == 100
    assert agent["promotions"][0]["fallback_only"] is True
    journal = EventJournal(service.storage_identity.journal_path).read()
    assert any(item.event_type == "provider_promotion_created" for item in journal)
    service.close()


def test_provider_promotion_can_be_scoped_to_one_package(tmp_path):
    service = _service(tmp_path)
    _limit_satellite_review_complexity(tmp_path)

    result = service.set_agent_promotion(
        "satellite-qwen",
        capabilities=["review"],
        promoted_max_complexity=100,
        duration_seconds=3600,
        package_id="WP23",
    )
    agent = next(
        item for item in service.snapshot()["agents"] if item["id"] == "satellite-qwen"
    )

    assert result["promotions"][0]["package_id"] == "WP23"
    # A package-scoped override must not make the provider card advertise a
    # task-wide effective ceiling. The active promotion remains visible with its
    # scope and is applied by the scheduler only for WP23.
    assert agent["effective_max_complexity"]["review"] == 75
    assert agent["promotions"][0]["package_id"] == "WP23"
    service.close()


def test_provider_promotion_revoke_restores_static_ceiling(tmp_path):
    service = _service(tmp_path)
    _limit_satellite_review_complexity(tmp_path)
    service.set_agent_promotion(
        "satellite-qwen",
        capabilities=["review"],
        promoted_max_complexity=100,
        duration_seconds=3600,
    )

    result = service.revoke_agent_promotion(
        "satellite-qwen", capabilities=["review"]
    )
    agent = next(
        item for item in service.snapshot()["agents"] if item["id"] == "satellite-qwen"
    )

    assert [item["capability"] for item in result["revoked"]] == ["review"]
    assert agent["effective_max_complexity"]["review"] == 75
    assert agent["promotions"] == []
    journal = EventJournal(service.storage_identity.journal_path).read()
    assert any(item.event_type == "provider_promotion_revoked" for item in journal)
    service.close()


def test_provider_promotion_validates_capability_ceiling_and_duration(tmp_path):
    service = _service(tmp_path)
    _limit_satellite_review_complexity(tmp_path)

    with pytest.raises(GuiError, match="does not support"):
        service.set_agent_promotion(
            "satellite-qwen", capabilities=["implement"], duration_seconds=3600
        )
    with pytest.raises(GuiError, match="does not exceed"):
        service.set_agent_promotion(
            "satellite-qwen",
            capabilities=["review"],
            promoted_max_complexity=75,
            duration_seconds=3600,
        )

    service.close()


def test_provider_card_exposes_promotion_control_next_to_doctor():
    from execraft.gui.server import _dashboard_html

    maintenance_js = _dashboard_asset("profile-maintenance.js")[0].decode("utf-8")
    index_html = _dashboard_html("test-token")

    doctor_index = maintenance_js.index('data-action="doctor"')
    promote_index = maintenance_js.index('class="btn small promotion-action')
    reset_index = maintenance_js.index('data-action="reset-health"')
    assert doctor_index < promote_index < reset_index
    assert 'id="providerPromotionDialog"' in index_html
    assert 'id="providerPromotionFallbackOnly"' in index_html
    assert 'id="providerPromotionFinalReview"' in index_html
    assert 'id="providerPromotionPackage"' in index_html
    assert "/api/agent/promotion" in maintenance_js


def test_client_disconnect_errors_are_classified_as_benign():
    assert _is_client_disconnect(BrokenPipeError())
    assert _is_client_disconnect(ConnectionResetError())
    assert _is_client_disconnect(ConnectionAbortedError())
    assert not _is_client_disconnect(OSError(5, "I/O error"))


def test_snapshot_disconnect_does_not_attempt_a_second_response():
    class SnapshotService:
        @staticmethod
        def snapshot():
            return {"large": "payload"}

    handler_type = _handler_factory(SnapshotService(), "test-token")
    handler = object.__new__(handler_type)
    handler.path = "/api/snapshot"
    handler.close_connection = False
    writes = []

    def disconnected_json(*args, **kwargs):
        writes.append((args, kwargs))
        raise BrokenPipeError()

    handler._json = disconnected_json

    handler.do_GET()

    assert len(writes) == 1
    assert handler.close_connection is True


def test_error_response_disconnect_is_silently_closed():
    class BrokenService:
        @staticmethod
        def snapshot():
            raise GuiError("snapshot failed")

    handler_type = _handler_factory(BrokenService(), "test-token")
    handler = object.__new__(handler_type)
    handler.path = "/api/snapshot"
    handler.close_connection = False
    responses = []

    def disconnected_json(*args, **kwargs):
        responses.append((args, kwargs))
        raise ConnectionResetError()

    handler._json = disconnected_json

    handler.do_GET()

    assert len(responses) == 1
    assert handler.close_connection is True


def test_main_snapshot_does_not_scan_git_workspace(tmp_path):
    service = _service(tmp_path)

    def unexpected_workspace_scan():
        raise AssertionError("main polling snapshot must not run git status")

    service._workspace_manager = unexpected_workspace_scan

    snapshot = service.snapshot()

    assert snapshot["packages"]
    service.close()


def test_workspace_commit_is_rejected_while_driver_is_active(tmp_path):
    service = _service(tmp_path)
    service.process.status = lambda: {
        "owned_running": False,
        "external_running": True,
        "pid": None,
        "started_at": "",
        "last_exit_code": None,
        "driver_log": "",
        "command": [],
    }

    with pytest.raises(GuiError, match="orchestrator driver is active"):
        service.commit_workspace_changes(
            selections={"core": ["tracked.txt"]},
            expected_digests={"core": "digest"},
            subject="Update tracked behavior",
            body="",
            reviewed=True,
        )

    service.close()



def test_workspace_delete_uses_bounded_manager_action(tmp_path):
    service = _service(tmp_path)
    calls = []

    class Manager:
        @staticmethod
        def delete_untracked(*, selections, expected_digests):
            calls.append((selections, expected_digests))
            return {"deleted_count": 1, "deleted": [{"repository_id": "core", "path": "cache.pyc"}]}

    service._workspace_manager = lambda: Manager()

    result = service.delete_workspace_files(
        selections={"core": ["cache.pyc"]},
        expected_digests={"core": "digest"},
    )

    assert result["deleted_count"] == 1
    assert calls == [({"core": ["cache.pyc"]}, {"core": "digest"})]
    service.close()


def test_workspace_delete_is_rejected_while_driver_is_active(tmp_path):
    service = _service(tmp_path)
    service.process.status = lambda: {
        "owned_running": True,
        "external_running": False,
        "pid": 123,
        "started_at": "",
        "last_exit_code": None,
        "driver_log": "",
        "command": [],
    }

    with pytest.raises(GuiError, match="orchestrator driver is active"):
        service.delete_workspace_files(
            selections={"core": ["cache.pyc"]},
            expected_digests={"core": "digest"},
        )

    service.close()

def test_run_is_rejected_while_workspace_action_is_active(tmp_path):
    service = _service(tmp_path)
    assert service._workspace_action_lock.acquire(blocking=False)
    try:
        with pytest.raises(GuiError, match="workspace action is in progress"):
            service.start_run()
    finally:
        service._workspace_action_lock.release()
    service.close()


def test_workspace_changes_endpoint_uses_dedicated_snapshot():
    calls = []

    class WorkspaceService:
        @staticmethod
        def workspace_snapshot():
            calls.append("snapshot")
            return {"available": True, "repositories": []}

    handler_type = _handler_factory(WorkspaceService(), "test-token")
    handler = object.__new__(handler_type)
    handler.path = "/api/workspace/changes"
    handler.close_connection = False
    responses = []
    handler._json = lambda status, payload: responses.append((status, payload))

    handler.do_GET()

    assert calls == ["snapshot"]
    assert responses[0][1]["available"] is True


def test_update_package_policy_sends_agents_and_skills_atomically(tmp_path, monkeypatch):
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command, 0, stdout="Execution policy updated for WP1\n", stderr=""
        )

    monkeypatch.setattr("execraft.gui.server.subprocess.run", runner)
    service = _service(tmp_path)

    result = service.update_package_policy(
        package_id="WP1",
        agent_preferences={"review": ["satellite-qwen"]},
        skill_preferences={"review": ["ai-review"]},
        agent_preference_binding_roles=["review"],
        apply_to_shards=True,
    )

    command, kwargs = calls[0]
    assert command[3:5] == ["orchestrate", "policy"]
    payload = json.loads(command[command.index("--policy-json") + 1])
    assert payload == {
        "agents": {"review": ["satellite-qwen"]},
        "skills": {"review": ["ai-review"]},
        "binding_agent_roles": ["review"],
    }
    assert "--apply-to-shards" in command
    assert result["skill_preferences"]["review"] == ["ai-review"]
    assert kwargs["capture_output"] is True
    service.close()


def test_update_package_preferences_runs_one_atomic_cli_command(tmp_path, monkeypatch):
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="Agent preferences updated for WP1\n",
            stderr="",
        )

    monkeypatch.setattr("execraft.gui.server.subprocess.run", runner)
    service = _service(tmp_path)

    result = service.update_package_preferences(
        package_id="WP1",
        preferences={
            "implement": ["local-codex"],
            "review": ["satellite-qwen", "local-codex"],
        },
        apply_to_shards=True,
    )

    assert result["returncode"] == 0
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[3:5] == ["orchestrate", "policy"]
    assert command[command.index("--package-id") + 1] == "WP1"
    payload = json.loads(command[command.index("--policy-json") + 1])
    assert payload["agents"]["review"] == ["satellite-qwen", "local-codex"]
    assert payload["skills"] == {}
    assert "--apply-to-shards" in command
    assert kwargs["capture_output"] is True
    service.close()


def test_package_preferences_are_rejected_while_driver_is_active(tmp_path):
    service = _service(tmp_path)
    service.process.status = lambda: {
        "owned_running": False,
        "external_running": True,
        "pid": None,
        "started_at": "",
        "last_exit_code": None,
        "driver_log": "",
        "command": [],
    }

    with pytest.raises(GuiError, match="orchestrator driver is active"):
        service.update_package_preferences(
            package_id="WP1",
            preferences={"review": ["satellite-qwen"]},
        )

    service.close()


def test_gui_assets_contain_workflow_policy_workspace_and_terminal_features():
    root = Path("src/execraft/assets/gui")
    index = (root / "index.html").read_text(encoding="utf-8")
    app = (root / "app.js").read_text(encoding="utf-8")
    workflow = (root / "workflow.js").read_text(encoding="utf-8")
    routing = (root / "workflow-routing.js").read_text(encoding="utf-8")
    viewport = (root / "workflow-viewport.js").read_text(encoding="utf-8")
    console = (root / "agent-console.js").read_text(encoding="utf-8")
    work_package_execution = (root / "work-package-execution.js").read_text(encoding="utf-8")
    execution_trace = (root / "execution-trace-view.js").read_text(encoding="utf-8")
    work_package_presenter = (root / "work-package-presenter.js").read_text(encoding="utf-8")
    profile_maintenance = (root / "profile-maintenance.js").read_text(encoding="utf-8")
    workspace_changes = (root / "workspace-changes-view.js").read_text(encoding="utf-8")
    source = "\n".join([
        index, app, workflow, routing, viewport, console, work_package_execution,
        execution_trace, work_package_presenter, profile_maintenance, workspace_changes,
    ])

    assert "Edit routing" in source
    assert "Advanced profile and skill policy" in source
    assert "/api/package/policy" in source
    assert "skill_preferences" in source
    assert "Apply to direct shards" in source
    assert "draft.applyToShards" in source
    assert "pendingAgents" in source
    assert "hasActiveFormInteraction" in source
    assert "deleteWorkspaceFilesBtn" in source
    assert "/api/workspace/delete" in source
    assert "Delete untracked" in source
    assert "selection never recenters" in index
    assert "Parallel" in workflow
    assert "Shard of" in workflow
    assert "Working now" in workflow
    assert 'data-work-package-action="details"' in workflow
    list_markup = workflow.split("function listItemMarkup", 1)[1].split("/**", 1)[0]
    assert list_markup.count('data-work-package-action="details"') == 1
    assert 'data-work-package-action="select"' in workflow
    assert 'data-work-package-action="execution"' in workflow
    assert 'data-work-package-action="repository-sync"' in workflow
    assert 'data-work-package-action="${paused ? "resume" : "pause"}"' in workflow
    assert "workPackageDirectiveSummary" in workflow
    card_markup = workflow.split("function cardMarkup", 1)[1].split(
        "function markerForAppearance", 1
    )[0]
    assert 'data-work-package-action="decompose"' not in card_markup
    assert 'data-work-package-action="repository-sync"' not in card_markup
    assert 'id="workPackageInspector"' in index
    assert 'id="workPackageOverviewTab"' in index
    assert 'id="workPackageExecutionTab"' in index
    assert 'id="workPackageEvidenceTab"' in index
    assert "openWorkPackageInspector" in app
    assert "setWorkPackageDirective" in app
    assert "workflowCurrentLabel" in index
    assert 'id="workflowList"' in index
    assert 'data-workflow-view="graph" aria-pressed="true"' in index
    assert 'data-workflow-view="list" aria-pressed="false"' in index
    assert 'id="workflowGraphView" class="workflow-graph-view"' in index
    assert 'id="workflowList" class="work-package-list hidden"' in index
    assert 'id="workflowFollowActive"' in index
    assert 'id="workflowLocateBtn"' in index
    assert "WorkflowList" in workflow
    assert "Graph is the primary Run workspace" in workflow
    assert "workingPackageIds" in workflow
    assert "workflowArrowActive" in workflow
    assert "active-terminal" in workflow
    assert 'markerUnits="userSpaceOnUse"' in workflow
    assert 'markerWidth="7"' in workflow
    assert "cardClearance = 5" in workflow
    assert " C ${controlX}" not in workflow
    assert "orthogonalMidpointRoute" in routing
    assert "simplifyOrthogonalPoints" in routing
    assert "orderedPortCoordinates" in routing
    assert "bundleOrthogonalEdges" in routing
    assert "bundleOrthogonalEdges" in workflow
    assert "dependencyEdgesForDisplay" in routing
    assert "dependencyEdgesForDisplay" in workflow
    assert "orderTopologicalColumns" in routing
    assert "orderTopologicalColumns" in workflow
    assert "graph-aware shared trunks" in workflow
    assert "--workflow-route-corridor" not in workflow
    assert "orthogonalEdgePath" in routing
    assert "new WorkflowViewport" in app
    assert 'data-workflow-viewport-action="fit"' in index
    assert 'data-workflow-viewport-action="reset"' in index
    assert 'id="workflowEdgeMode"' in index
    assert 'data-workflow-edge-mode="essential"' in index
    assert 'data-workflow-edge-mode="all"' in index
    assert 'id="workflowEdgeSummary"' in index
    assert 'id="workflowViewport"' in index
    assert 'id="workflowWrap" class="workflow-wrap" tabindex="0"' in index
    assert 'addEventListener("wheel", this.handleWheel' in viewport
    assert 'event.pointerType === "touch"' in viewport
    assert 'event.button !== 0' in viewport
    assert "setPointerCapture" in viewport
    assert "event.ctrlKey || event.metaKey" in viewport
    assert "Math.exp(-this.wheelDelta" in viewport
    assert "this.viewport.scrollBy" in viewport
    assert "ArrowLeft" in viewport
    assert 'src="/assets/execraft-mark.png"' in index
    assert '<div class="product-mark"' not in index
    assert "dependencyClosure" in workflow
    assert "card?.scrollIntoView" not in workflow
    assert "this.wrap.scrollTo" in workflow
    assert 'aria-pressed="${selected ? "true" : "false"}"' in workflow
    assert "vertical wheel scrolls the page" in index
    assert "Shift + wheel pans horizontally" in index
    assert "Ctrl/⌘ + wheel zooms" in index
    assert 'id="workflowEdges"' in index
    gui_css = (root / "gui.css").read_text(encoding="utf-8")
    assert ".workflow-connections { position: absolute; inset: 0; z-index: 0;" in gui_css
    assert ".workflow-board { position: relative; z-index: 1;" in gui_css
    assert "padding: 18px 34px 24px" in gui_css
    assert "stroke-linejoin: miter" in gui_css
    assert ".workflow-edge.bundle-trunk,.workflow-edge.bundle-bus" in gui_css
    assert ".workflow-edge.active-terminal { stroke: #fbbf24; stroke-width: 2.15;" in gui_css
    assert "touch-action: pan-x pan-y" in gui_css
    assert ".execution-trace-flow" in gui_css
    assert ".execution-trace-node.live" in gui_css
    assert ".execution-trace-diagnostics" in gui_css
    assert ".execution-trace-node-flags" in gui_css
    assert 'data-trace-mode="flow"' in source
    assert 'data-trace-mode="attempts"' in source
    assert "executionTraceFlowGroups" in source
    assert "executionTraceDiagnosticsMarkup" in source
    assert "Workflow relationships" in app
    assert "Recent stage evidence" in app
    assert "Execution trace" in app
    assert "/api/package/execution-trace" in source
    assert "executionTraceNodeDetail" in source
    assert "Direct shards" in app
    assert "edge parent" not in workflow
    assert "predecessors.push(item.parent_id)" not in workflow
    assert "New terminal" in index
    assert "Terminal keys" in index
    assert "agentTerminalKeySink" in index
    assert "Focus terminal" in index
    assert 'data-terminal-key="Enter"' in index
    assert "composerPayload" in console
    assert "#drainActionQueue" in console
    assert 'include_events: state.activeView === "activity"' in console
    assert "Open console" in source
    assert ".agent-card[data-agent]" not in app
    assert "terminal_screen_token" in console
    assert "SESSION_REFRESH_MS" in console
    assert "first_output_seconds" in source
    assert "terminal_screen" in console
    assert 'id="agentProgressPanel"' in index
    assert "queued guidance" in console
    assert "conversation-tool-summary" in console
    assert "possibly_stalled" in console
    assert 'data-change-filter="all"' in index
    assert 'data-change-filter="modified"' in index
    assert 'data-change-filter="added"' in index
    assert 'data-change-filter="deleted"' in index
    assert 'id="commitReview"' in index
    assert "changeKind(change)" in workspace_changes



def test_gui_onboarding_uses_progressive_project_and_task_flows():
    root = Path("src/execraft/assets/gui")
    index = (root / "index.html").read_text(encoding="utf-8")
    onboarding = (root / "onboarding-view.js").read_text(encoding="utf-8")
    css = (root / "gui.css").read_text(encoding="utf-8")

    assert 'data-onboarding-mode="source"' in index
    assert 'data-onboarding-mode="descriptor"' in index
    assert 'data-onboarding-mode="greenfield"' in index
    assert 'id="existingSourceOnboarding" class="onboarding-step"' in index
    assert 'id="descriptorOnboarding" class="onboarding-step hidden"' in index
    assert 'id="greenfieldOnboarding" class="onboarding-step hidden"' in index
    assert index.count('>Advanced options</summary>') >= 4
    assert 'id="taskAdvancedOptions" class="task-advanced-panel"' in index
    assert 'id="previewTaskBtn" type="button" class="btn small"' in index
    assert 'id="createTaskBtn" type="button" class="btn primary"' in index
    assert 'id="createTaskBtn" type="button" class="btn primary" disabled' not in index
    assert '#selectOnboardingMode' in onboarding
    assert 'const preview = await this.api("/api/onboarding/start/preview", {' in onboarding
    assert 'if (!preview.can_apply)' in onboarding
    assert '"/api/onboarding/start/apply"' in onboarding
    assert '.onboarding-mode.active' in css
    assert '.onboarding-flow' in css
    assert 'home-launch-grid' not in css
    assert 'onboarding-card' not in css


def test_gui_javascript_literal_element_references_exist_in_html():
    root = Path("src/execraft/assets/gui")
    index = (root / "index.html").read_text(encoding="utf-8")
    scripts = "\n".join(
        (root / name).read_text(encoding="utf-8")
        for name in (
            "app.js",
            "ui-shell.js",
            "run-control.js",
            "log-controller.js",
            "workflow.js",
            "work-package-inspector.js",
            "work-package-execution.js",
            "execution-health-view.js",
            "agent-workforce-view.js",
            "execution-trace-view.js",
            "work-package-presenter.js",
            "profile-maintenance.js",
            "agent-console.js",
            "onboarding-view.js",
            "task-lifecycle.js",
            "context-navigation.js",
        )
    )
    rendered_ids = set(
        re.findall(r'\bid=["\']([A-Za-z][A-Za-z0-9_-]*)["\']', index + scripts)
    )
    javascript_ids = set(
        re.findall(r'(?:this\.)?\$\(["\']([A-Za-z][A-Za-z0-9_-]*)["\']\)', scripts)
    )

    assert javascript_ids <= rendered_ids, sorted(javascript_ids - rendered_ids)


def test_dashboard_assets_are_packaged_from_a_strict_allowlist():
    css, css_type = _dashboard_asset("gui.css")
    workflow, js_type = _dashboard_asset("workflow.js")
    routing, routing_type = _dashboard_asset("workflow-routing.js")
    shell, shell_type = _dashboard_asset("ui-shell.js")
    run_control, run_control_type = _dashboard_asset("run-control.js")
    logs, logs_type = _dashboard_asset("log-controller.js")
    utilities, utilities_type = _dashboard_asset("ui-utils.js")
    context_navigation, context_navigation_type = _dashboard_asset("context-navigation.js")
    task_lifecycle, task_lifecycle_type = _dashboard_asset("task-lifecycle.js")
    viewport, viewport_type = _dashboard_asset("workflow-viewport.js")
    navigation, navigation_type = _dashboard_asset("workbench-navigation.js")
    work_package_inspector, work_package_inspector_type = _dashboard_asset("work-package-inspector.js")
    work_package_execution, work_package_execution_type = _dashboard_asset("work-package-execution.js")
    execution_health, execution_health_type = _dashboard_asset("execution-health-view.js")
    workforce, workforce_type = _dashboard_asset("agent-workforce-view.js")
    execraft_mark, execraft_mark_type = _dashboard_asset("execraft-mark.png")

    assert b"workflow-wrap" in css
    assert css_type == "text/css; charset=utf-8"
    assert b"WorkflowGraph" in workflow
    assert js_type == "text/javascript; charset=utf-8"
    assert b"orthogonalMidpointRoute" in routing
    assert b"dependencyEdgesForDisplay" in routing
    assert routing_type == "text/javascript; charset=utf-8"
    assert b"WorkflowViewport" in viewport
    assert viewport_type == "text/javascript; charset=utf-8"
    assert b"WorkbenchNavigation" in navigation
    assert navigation_type == "text/javascript; charset=utf-8"
    assert b"WorkPackageInspector" in work_package_inspector
    assert work_package_inspector_type == "text/javascript; charset=utf-8"
    assert b"WorkPackageExecutionView" in work_package_execution
    assert work_package_execution_type == "text/javascript; charset=utf-8"
    assert b"ExecutionHealthView" in execution_health
    assert execution_health_type == "text/javascript; charset=utf-8"
    assert b"AgentWorkforceView" in workforce
    assert workforce_type == "text/javascript; charset=utf-8"
    assert b"UiShell" in shell
    assert shell_type == "text/javascript; charset=utf-8"
    assert b"RunControlController" in run_control
    assert run_control_type == "text/javascript; charset=utf-8"
    assert b"LogController" in logs
    assert logs_type == "text/javascript; charset=utf-8"
    assert b"escapeHtml" in utilities
    assert utilities_type == "text/javascript; charset=utf-8"
    assert b"ContextNavigation" in context_navigation
    assert context_navigation_type == "text/javascript; charset=utf-8"
    assert b"TaskLifecycleView" in task_lifecycle
    assert task_lifecycle_type == "text/javascript; charset=utf-8"
    assert execraft_mark.startswith(b"\x89PNG\r\n\x1a\n")
    assert int.from_bytes(execraft_mark[16:20], byteorder="big") == 256
    assert int.from_bytes(execraft_mark[20:24], byteorder="big") == 256
    assert execraft_mark[25] == 6  # PNG RGBA colour type
    assert execraft_mark_type == "image/png"
    with pytest.raises(GuiError, match="unknown dashboard asset"):
        _dashboard_asset("../project.yaml")


def test_gui_workbench_shell_exposes_action_center_and_focused_agent_dock():
    root = Path("src/execraft/assets/gui")
    index = (root / "index.html").read_text(encoding="utf-8")
    css = (root / "gui.css").read_text(encoding="utf-8")
    shell = (root / "ui-shell.js").read_text(encoding="utf-8")
    context_navigation = (root / "context-navigation.js").read_text(encoding="utf-8")
    console = (root / "agent-console.js").read_text(encoding="utf-8")

    assert 'id="actionCenter"' in index
    assert 'id="executionContexts"' in index
    assert 'id="actionDetails"' in index
    assert 'id="supervisorPanel" class="supervisor-notice hidden"' in index
    assert 'id="infrastructureHealth"' in index
    assert 'id="workflowList"' in index
    assert 'id="taskPicker"' in index
    assert 'role="tablist"' in index
    assert 'role="tabpanel"' in index
    assert 'id="commandPalette"' in index
    assert 'class="skip-link"' in index
    assert 'src="/assets/execraft-mark.png"' in index
    assert 'id="mainContent"' in index
    assert 'class="workbench-dock hidden"' in index
    assert 'id="agentAdvancedControls"' in index
    assert 'id="maximizeAgentConsole"' in index
    assert 'id="layoutAgentConsole"' not in index
    assert 'id="collapseAgentConsole"' not in index
    assert 'role="separator"' not in index
    assert "focus-visible" in css
    assert "prefers-reduced-motion" in css
    assert "execraft.gui.preferences.v1" in (root / "app.js").read_text(encoding="utf-8")
    assert "snapshot.execution_context" in shell
    assert "snapshot.execution_contexts" in shell
    assert "liveAgentContexts" in shell
    assert 'agentId || "Unassigned"' in shell
    assert 'this.api("/api/projects")' in context_navigation
    assert '"/api/session/open"' in context_navigation
    assert 'id="projectPicker"' in index
    assert 'id="projectsBreadcrumbBtn"' in index
    assert "execraft.agent-workbench.preferences.v1" not in console
    assert "#bindWorkbenchResize" not in console
    assert "#applyWorkbenchState" in console


def test_gui_refresh_and_event_rendering_are_single_flight_and_visibility_aware():
    root = Path("src/execraft/assets/gui")
    app = (root / "app.js").read_text(encoding="utf-8")
    logs = (root / "log-controller.js").read_text(encoding="utf-8")
    workflow = (root / "workflow.js").read_text(encoding="utf-8")
    archive = (root / "archive-view.js").read_text(encoding="utf-8")
    profile = (root / "profile-maintenance.js").read_text(encoding="utf-8")
    workspace = (root / "workspace-changes-view.js").read_text(encoding="utf-8")

    assert "refreshPromise" in app
    assert "document.hidden" in app
    assert "HIDDEN_REFRESH_MS" in app
    assert "DASHBOARD_REFRESH_MS" in app
    assert "setInterval(refresh" not in app
    assert "this.loading" in workspace
    assert "workspaceLoading" not in app
    assert "workflowRenderKey" in app
    assert 'return value === "list" ? "list" : "graph"' in app
    assert "new WorkbenchNavigation" in app
    assert "scheduleWorkflowNavigation" in app
    assert 'if (!this.healthView.isOpen() || !$("executionProfileMaintenance").open)' in profile
    assert "this.renderKey" in profile
    assert "logs.setActive(selected === \"logs\")" in app
    assert "window.setInterval" not in logs
    assert 'this.board.addEventListener("click"' in workflow
    assert "#handleAction(event)" in workflow
    assert "#handleListAction(event)" in archive
    assert 'data-archive-action="delete"' in archive
    assert '"/api/archive/delete"' in archive


def test_task_catalog_marks_the_dashboard_bound_task(tmp_path):
    service = _service(tmp_path)

    tasks = {item["id"]: item for item in service.list_tasks()}

    assert tasks["demo-task"]["current"] is True
    service.close()


def test_ollama_endpoint_probe_reports_installed_and_loaded_models(monkeypatch):
    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            return json.dumps(self.payload).encode("utf-8")

    calls = []

    def urlopen(request, timeout):
        calls.append(request.full_url)
        if request.full_url.endswith("/v1/models"):
            return Response({"data": [{"id": "qwen3.5:9b"}, {"id": "qwen3-coder:30b-32k"}]})
        if request.full_url.endswith("/api/ps"):
            return Response({"models": [{"name": "qwen3.5:9b"}]})
        raise AssertionError(request.full_url)

    monkeypatch.setattr("execraft.gui.server.urllib.request.urlopen", urlopen)

    class Endpoint:
        endpoint_id = "gpu-node-a"
        provider_id = "ollama-gpu-a"
        base_url = "http://192.0.2.10:11434/v1"
        models_url = "http://192.0.2.10:11434/v1/models"
        connect_timeout_seconds = 5

    endpoint_id, result = EndpointProbeCache._probe_one(Endpoint())

    assert endpoint_id == "gpu-node-a"
    assert result["reachable"] is True
    assert result["models"] == ["qwen3-coder:30b-32k", "qwen3.5:9b"]
    assert result["loaded_models"] == ["qwen3.5:9b"]
    assert result["loaded_models_supported"] is True
    assert calls == [
        "http://192.0.2.10:11434/v1/models",
        "http://192.0.2.10:11434/api/ps",
    ]


def test_agent_console_endpoint_selects_running_session_and_reads_incrementally(tmp_path):
    service = _service(tmp_path)
    payload = {
        "package_id": "WP1__review",
        "stage": "review",
        "capability": "review",
        "agent_id": "satellite-qwen",
        "adapter": "opencode",
        "model": "satellite/qwen",
        "attempt": 1,
    }
    session = service.agent_console.start(payload)
    service.agent_console.append({**payload, "stream": "stdout", "text": "live output\n"})

    result = service.read_agent_console("satellite-qwen")

    assert result["selected_session_id"] == session["session_id"]
    assert result["metadata"]["status"] == "running"
    assert result["events"][0]["text"] == "live output\n"
    service.close()



def test_agent_console_prefers_current_supervisor_session(tmp_path):
    service = _service(tmp_path)
    base = {
        "capability": "supervise",
        "agent_id": "local-codex",
        "adapter": "codex",
        "model": "",
        "attempt": 1,
        "interaction_mode": "conversation",
        "streaming_interaction": True,
        "steering_supported": True,
    }
    target = service.agent_console.start(
        {**base, "package_id": "WP17__WP17-S4", "stage": "supervise"}
    )
    # A newer session for the same provider must not steal the Supervisor button.
    service.agent_console.start(
        {**base, "package_id": "WP18", "stage": "implement", "capability": "implement"}
    )

    result = service.read_agent_console(
        "local-codex",
        preferred_package_id="WP17__WP17-S4",
        preferred_stage="supervise",
    )

    assert result["selected_session_id"] == target["session_id"]
    assert result["metadata"]["stage"] == "supervise"
    service.close()

def test_agent_console_rejects_unknown_provider(tmp_path):
    service = _service(tmp_path)
    with pytest.raises(GuiError, match="unknown configured provider"):
        service.read_agent_console("missing")
    service.close()


def test_agent_console_write_requires_acknowledgement_and_queues_input(tmp_path):
    service = _service(tmp_path)
    payload = {
        "package_id": "WP1__review",
        "stage": "review",
        "capability": "review",
        "agent_id": "satellite-qwen",
        "adapter": "opencode",
        "model": "satellite/qwen",
        "attempt": 1,
        # Endpoint contract for an explicitly PTY-capable standalone session.
        # Orchestrated OpenCode workers intentionally omit this capability.
        "interactive_pty": True,
    }
    session = service.agent_console.start(payload)

    with pytest.raises(GuiError, match="acknowledgement"):
        service.write_agent_console(
            session_id=session["session_id"],
            action="input",
            data="continue\n",
            acknowledged=False,
        )

    accepted = service.write_agent_console(
        session_id=session["session_id"],
        action="input",
        data="continue\n",
        acknowledged=True,
    )
    assert accepted["accepted"] is True
    assert accepted["delivered_immediately"] is False
    assert service.agent_console.consume_control_events(payload)[0]["data"] == "continue\n"
    service.close()


class _FakeManualConsoleManager:
    def __init__(self):
        self.running = False
        self.started: list[str] = []
        self.stopped: list[tuple[str, str]] = []
        self.closed = False

    def any_running(self):
        return self.running

    def status(self, agent_id):
        return {
            "running": self.running,
            "session_id": "manual-session" if self.running else "",
            "pid": 123 if self.running else None,
        }

    def start(self, agent_id):
        self.running = True
        self.started.append(agent_id)
        return {"started": True, "agent_id": agent_id, "session_id": "manual-session"}

    def stop(self, *, agent_id="", session_id=""):
        self.running = False
        self.stopped.append((agent_id, session_id))
        return {"stopped": True, "agent_id": agent_id, "session_id": session_id}

    def dispatch_pending(self, session_id):
        return bool(self.running and session_id == "manual-session")

    def close(self):
        self.closed = True


def test_dashboard_starts_and_stops_standalone_agent_console(tmp_path):
    service = _service(tmp_path)
    service.manual_agent_console.close()
    fake = _FakeManualConsoleManager()
    service.manual_agent_console = fake

    with pytest.raises(GuiError, match="acknowledgement"):
        service.start_manual_agent_console("local-codex", acknowledged=False)

    started = service.start_manual_agent_console(
        "local-codex", acknowledged=True
    )
    assert started["session_id"] == "manual-session"
    assert fake.started == ["local-codex"]

    console = service.read_agent_console("local-codex")
    assert console["manual_console"]["running"] is True
    assert console["manual_console"]["can_start"] is False

    with pytest.raises(GuiError, match="stop standalone"):
        service.start_run()

    stopped = service.stop_manual_agent_console(
        agent_id="local-codex", session_id="manual-session"
    )
    assert stopped["stopped"] is True
    assert fake.stopped == [("local-codex", "manual-session")]
    service.close()
    assert fake.closed is True


def test_dashboard_allows_verified_noop_commit_resume_when_configured(tmp_path):
    service = _service(tmp_path)
    agents_path = service.root / "projects" / "demo" / "agents.yaml"
    agents_path.write_text(
        agents_path.read_text(encoding="utf-8")
        + "\ncommit:\n  mode: automatic\n  allow_verified_noop: true\n",
        encoding="utf-8",
    )
    record = service._load_state()
    assert record is not None
    package = record.plan_graph.package_by_id("WP1")
    package.stage = WorkPackageStage.READY_TO_COMMIT
    record.state = TaskExecutionState.HUMAN_REQUIRED
    service.state_path.write_text(
        json.dumps(record.as_mapping(), indent=2), encoding="utf-8"
    )
    EventJournal(default_journal_path(service.state_root, service.task_id)).append(
        "human_intervention_required",
        {
            "package_id": "WP1",
            "stage": "commit",
            "blocked_requirement": "multi-repository commit transaction failed",
            "evidence": ["no changes detected in affected repositories"],
            "recommended_decision": "repair repository state",
        },
    )

    control = service._run_control(service._load_state())

    assert control["can_start"] is True
    assert control["label"] == "Resume verified no-op"
    service.close()


def test_agent_config_validation_accepts_verified_noop_commit_policy(tmp_path):
    service = _service(tmp_path)
    current = service.read_config("agents")["content"]
    updated = current + "\ncommit:\n  mode: automatic\n  allow_verified_noop: true\n"

    result = service.validate_config("agents", updated)

    assert result == {"valid": True, "error": ""}
    service.close()


def test_agent_config_validation_rejects_non_boolean_verified_noop_flag(tmp_path):
    service = _service(tmp_path)
    current = service.read_config("agents")["content"]
    invalid = current + "\ncommit:\n  mode: automatic\n  allow_verified_noop: 'yes'\n"

    result = service.validate_config("agents", invalid)

    assert result["valid"] is False
    assert "commit.allow_verified_noop must be a boolean" in result["error"]
    service.close()


def test_agent_console_endpoint_can_skip_sessions_and_unchanged_screen(tmp_path):
    service = _service(tmp_path)
    payload = {
        "package_id": "WP1__review",
        "stage": "review",
        "capability": "review",
        "agent_id": "satellite-qwen",
        "adapter": "opencode",
        "model": "ollama/qwen",
        "attempt": 1,
        "interactive_pty": True,
    }
    session = service.agent_console.start(payload)
    service.agent_console.append({**payload, "stream": "terminal", "text": "ready"})

    first = service.read_agent_console("satellite-qwen")
    second = service.read_agent_console(
        "satellite-qwen",
        session_id=session["session_id"],
        offset=first["next_offset"],
        include_sessions=False,
        include_events=False,
        terminal_screen_token=first["terminal_screen_token"],
    )

    assert first["sessions_included"] is True
    assert second["sessions_included"] is False
    assert second["sessions"] == []
    assert second["events"] == []
    assert second["events_included"] is False
    assert second["terminal_screen"] == {}
    assert second["terminal_screen_unchanged"] is True
    service.close()


def test_snapshot_exposes_configured_supervisor_and_waiting_question(tmp_path):
    service = _service(tmp_path)
    agents_path = service.root / "projects" / "demo" / "agents.yaml"
    content = agents_path.read_text(encoding="utf-8")
    content = content.replace(
        "scheduling:\n",
        "supervisor:\n  enabled: true\n  agent: local-codex\n  skill: ai-supervise\n"
        "scheduling:\n",
        1,
    ).replace(
        "capabilities: [implement, review]",
        "capabilities: [implement, review, supervise]",
        1,
    )
    agents_path.write_text(content, encoding="utf-8")
    record = service._load_state()
    assert record is not None
    record.state = TaskExecutionState.WAITING_FOR_HUMAN_DECISION
    service.state_path.write_text(json.dumps(record.as_mapping()), encoding="utf-8")
    incident = service.supervisor_incidents.open_or_reuse(
        fingerprint="gui-incident",
        package_id="WP1",
        stage="final_review",
        classification=IncidentClass.REQUIREMENT_AMBIGUITY,
        escalation_sequence=1,
        supervisor_agent_id="local-codex",
        workspace_digest="digest",
    )
    incident.status = IncidentStatus.WAITING_FOR_HUMAN
    incident.human_question = {
        "question": "Keep the compatibility path until WP2?",
        "context": "The PLAN does not specify the removal tranche.",
        "recommended_option": "keep",
        "options": [
            {"id": "keep", "label": "Keep it", "consequence": "Only evidence changes."},
            {"id": "remove", "label": "Remove it", "consequence": "Code and tests change."},
        ],
    }
    service.supervisor_incidents.save(incident)

    snapshot = service.snapshot()

    assert snapshot["supervisor"]["available"] is True
    assert snapshot["supervisor"]["agent_id"] == "local-codex"
    assert snapshot["supervisor"]["incident"]["status"] == "waiting_for_human"
    assert snapshot["execution_context"] == {
        "package_id": "WP1",
        "stage": "final_review",
        "agent_id": "local-codex",
        "status": "waiting_for_human",
        "source": "supervisor",
    }
    assert snapshot["run_control"]["label"] == "Answer Supervisor"
    assert snapshot["run_control"]["can_start"] is False
    service.close()


def test_argmax_supervisor_question_offers_automatic_transport_retry(tmp_path):
    service = _service(tmp_path)
    agents_path = service.root / "projects" / "demo" / "agents.yaml"
    content = agents_path.read_text(encoding="utf-8")
    content = content.replace(
        "scheduling:\n",
        "supervisor:\n  enabled: true\n  agent: local-codex\n"
        "scheduling:\n",
        1,
    ).replace(
        "capabilities: [implement, review]",
        "capabilities: [implement, review, supervise]",
        1,
    )
    agents_path.write_text(content, encoding="utf-8")
    record = service._load_state()
    assert record is not None
    record.state = TaskExecutionState.WAITING_FOR_HUMAN_DECISION
    service.state_path.write_text(json.dumps(record.as_mapping()), encoding="utf-8")
    incident = service.supervisor_incidents.open_or_reuse(
        fingerprint="argmax-incident",
        package_id="WP1",
        stage="final_review",
        classification=IncidentClass.AGENT_FAILURE,
        escalation_sequence=1,
        supervisor_agent_id="local-codex",
        workspace_digest="digest",
    )
    incident.status = IncidentStatus.WAITING_FOR_HUMAN
    incident.summary = "[Errno 7] Argument list too long: '/venv/bin/python'"
    incident.human_question = {
        "question": "The Supervisor could not safely resolve this incident.",
        "context": incident.summary,
        "options": [
            {"id": "inspect_and_retry", "label": "Retry"},
            {"id": "stop", "label": "Stop"},
        ],
    }
    service.supervisor_incidents.save(incident)

    snapshot = service.snapshot()

    assert snapshot["supervisor"]["auto_resume_transport_failure"] is True
    assert snapshot["run_control"]["can_start"] is True
    assert snapshot["run_control"]["label"] == "Retry Supervisor transport"
    service.close()



def test_lost_supervisor_delegation_offers_automatic_resume(tmp_path):
    service = _service(tmp_path)
    agents_path = service.root / "projects" / "demo" / "agents.yaml"
    content = agents_path.read_text(encoding="utf-8")
    content = content.replace(
        "scheduling:\n",
        "supervisor:\n  enabled: true\n  agent: local-codex\n"
        "scheduling:\n",
        1,
    ).replace(
        "capabilities: [implement, review]",
        "capabilities: [implement, review, supervise]",
        1,
    )
    agents_path.write_text(content, encoding="utf-8")
    record = service._load_state()
    assert record is not None
    record.state = TaskExecutionState.WAITING_FOR_HUMAN_DECISION
    service.state_path.write_text(json.dumps(record.as_mapping()), encoding="utf-8")
    incident = service.supervisor_incidents.open_or_reuse(
        fingerprint="lost-delegation",
        package_id="WP1",
        stage="final_review",
        classification=IncidentClass.AGENT_FAILURE,
        escalation_sequence=1,
        supervisor_agent_id="local-codex",
        workspace_digest="digest",
    )
    incident.status = IncidentStatus.WAITING_FOR_HUMAN
    incident.summary = "A delegated provider failed before producing a result."
    incident.human_question = {
        "question": "How should recovery continue?",
        "context": incident.summary,
        "options": [
            {"id": "retry", "label": "Retry"},
            {"id": "stop", "label": "Stop"},
        ],
    }
    service.supervisor_incidents.save(incident)
    journal = EventJournal(default_journal_path(service.state_root, service.task_id))
    journal.append(
        "supervisor_decision",
        {
            "incident_id": incident.incident_id,
            "package_id": "WP1",
            "decision": "delegate",
        },
    )
    journal.append(
        "supervisor_delegation_started",
        {
            "incident_id": incident.incident_id,
            "package_id": "WP1",
            "index": 1,
            "task": "Repair the final-review evidence.",
            "capability": "fix_review",
            "agent_id": "opencode-zen-free",
            "skills": ["ai-fix-review"],
            "read_only": False,
        },
    )

    snapshot = service.snapshot()

    assert snapshot["supervisor"]["auto_resume_lost_delegation"] is True
    assert snapshot["run_control"]["can_start"] is True
    assert snapshot["run_control"]["label"] == "Resume delegated recovery"
    service.close()

def test_human_required_run_control_offers_supervisor_recovery(tmp_path):
    service = _service(tmp_path)
    agents_path = service.root / "projects" / "demo" / "agents.yaml"
    content = agents_path.read_text(encoding="utf-8")
    content = content.replace(
        "scheduling:\n",
        "supervisor:\n  enabled: true\n  agent: local-codex\n"
        "scheduling:\n",
        1,
    ).replace(
        "capabilities: [implement, review]",
        "capabilities: [implement, review, supervise]",
        1,
    )
    agents_path.write_text(content, encoding="utf-8")
    record = service._load_state()
    assert record is not None
    record.state = TaskExecutionState.HUMAN_REQUIRED
    service.state_path.write_text(json.dumps(record.as_mapping()), encoding="utf-8")
    EventJournal(default_journal_path(service.state_root, service.task_id)).append(
        "human_intervention_required",
        {
            "package_id": "WP1",
            "stage": "final_review",
            "reason": "review/fix cycle budget exhausted",
            "evidence": ["evidence mismatch"],
        },
    )

    control = service.snapshot()["run_control"]

    assert control["can_start"] is True
    assert control["label"] == "Supervisor recover / Resume"
    service.close()


def test_repository_sync_human_action_does_not_offer_supervisor_recovery(tmp_path):
    service = _service(tmp_path)
    agents_path = service.root / "projects" / "demo" / "agents.yaml"
    content = agents_path.read_text(encoding="utf-8")
    content = content.replace(
        "scheduling:\n",
        "supervisor:\n  enabled: true\n  agent: local-codex\n"
        "scheduling:\n",
        1,
    ).replace(
        "capabilities: [implement, review]",
        "capabilities: [implement, review, supervise]",
        1,
    )
    agents_path.write_text(content, encoding="utf-8")
    record = service._load_state()
    assert record is not None
    record.state = TaskExecutionState.HUMAN_REQUIRED
    service.state_path.write_text(json.dumps(record.as_mapping()), encoding="utf-8")
    EventJournal(default_journal_path(service.state_root, service.task_id)).append(
        "human_intervention_required",
        {
            "package_id": "WP1",
            "stage": "repository_sync:prepare",
            "blocked_requirement": (
                "repository-sync workspace path is unavailable for 'retired-repo'"
            ),
            "recommended_decision": "repair the repository selection before resuming",
        },
    )

    control = service.snapshot()["run_control"]

    assert control["can_start"] is False
    assert control["label"] == "Action required"
    assert "repair the repository selection" in control["reason"]
    service.close()


def test_supervisor_console_requires_active_incident() -> None:
    app_js = _dashboard_asset("app.js")[0].decode("utf-8")

    assert "agentId &&\n    active &&\n    incident.incident_id" in app_js


def test_agent_config_validation_restricts_supervisor_to_codex_or_claude(tmp_path):
    service = _service(tmp_path)
    content = service.read_config("agents")["content"]
    invalid = content.replace(
        "scheduling:\n",
        "supervisor:\n  enabled: true\n  agent: satellite-qwen\n"
        "scheduling:\n",
        1,
    ).replace(
        "capabilities: [review]",
        "capabilities: [review, supervise]",
        1,
    )

    result = service.validate_config("agents", invalid)

    assert result["valid"] is False
    assert "antigravity-cli" in result["error"]
    service.close()


def test_answer_supervisor_uses_canonical_cli_and_restarts_driver(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    import execraft.gui.server as gui_server

    service = _service(tmp_path)
    service.process.status = lambda: {
        "owned_running": False,
        "external_running": False,
    }
    starts = []
    service.process.start = lambda: starts.append(True) or {
        "owned_running": True,
    }
    service._load_state = lambda: SimpleNamespace(
        state=TaskExecutionState.SUPERVISING
    )
    commands = []

    def fake_run(command, **kwargs):
        commands.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="Supervisor answer recorded\n",
            stderr="",
        )

    monkeypatch.setattr(gui_server.subprocess, "run", fake_run)

    result = service.answer_supervisor(
        "keep",
        message="Retain compatibility until WP17-S5.",
    )

    assert result["returncode"] == 0
    assert starts == [True]
    command, kwargs = commands[0]
    assert command[-4:] == [
        "--answer",
        "keep",
        "--message",
        "Retain compatibility until WP17-S5.",
    ]
    assert "supervisor" in command
    assert kwargs["cwd"] == service.root
    service.close()


def test_agent_console_endpoint_streams_semantic_events_and_steering(tmp_path):
    service = _service(tmp_path)
    payload = {
        "package_id": "WP1",
        "stage": "supervise",
        "capability": "supervise",
        "agent_id": "local-codex",
        "adapter": "codex",
        "model": "",
        "attempt": 1,
        "interaction_mode": "conversation",
        "streaming_interaction": True,
        "steering_supported": True,
    }
    session = service.agent_console.start(payload)
    service.agent_console.append_interaction(
        {**payload, "kind": "assistant_delta", "item_id": "answer", "text": "Inspecting"}
    )

    first = service.read_agent_console("local-codex", include_events=False)
    accepted = service.write_agent_console(
        session_id=session["session_id"],
        action="steer",
        data="Check the handoff evidence",
        acknowledged=True,
    )
    second = service.read_agent_console(
        "local-codex",
        session_id=session["session_id"],
        include_sessions=False,
        include_events=False,
        interaction_offset=first["interaction_next_offset"],
        since_version=first["version"],
    )

    assert first["metadata"]["interaction"]["mode"] == "conversation"
    assert first["interactions"][0]["text"] == "Inspecting"
    assert accepted["accepted"] is True
    assert second["interactions"][0]["kind"] == "operator"
    assert second["interactions"][0]["text"] == "Check the handoff evidence"
    service.close()


def test_live_agent_workbench_assets_expose_conversation_changes_and_interrupt():
    from execraft.gui.server import _dashboard_html

    html = _dashboard_html("test-token")
    javascript = _dashboard_asset("agent-console.js")[0].decode("utf-8")

    for identifier in (
        "agentConsoleConversationView",
        "agentConsoleChangesView",
        "agentConversationOutput",
        "agentPlanOutput",
        "agentDiffOutput",
        "interruptAgentTurn",
        "agentProgressPanel",
        "agentProgressActivity",
        "agentProgressWarning",
    ):
        assert f'id="{identifier}"' in html
    assert 'action: "steer"' in javascript or '"steer" : "input"' in javascript
    assert 'signal: "interrupt"' in javascript
    assert "LIVE_LONG_POLL_MS" in javascript
    assert "observation stream" in javascript
    assert "queued guidance" in javascript
    assert "Guidance is queued for the provider" in javascript
    assert "#syncViewAvailability" in javascript
    assert "metadata.terminal?.enabled" in javascript


def test_review_exhausted_run_control_prefers_deterministic_fixer(tmp_path):
    service = _service(tmp_path)
    agents_path = service.root / "projects" / "demo" / "agents.yaml"
    content = agents_path.read_text(encoding="utf-8")
    content = content.replace(
        "scheduling:\n",
        "supervisor:\n  enabled: true\n  agent: local-codex\n"
        "scheduling:\n"
        "  recovery_playbooks:\n"
        "    enabled: true\n"
        "    review_exhausted:\n"
        "      enabled: true\n"
        "      max_rescue_cycles: 2\n"
        "      max_findings: 32\n"
        "      prefer_non_supervisor_fixer: true\n"
        "      allow_supervisor_fallback: false\n",
        1,
    ).replace(
        "capabilities: [implement, review]",
        "capabilities: [implement, review, supervise]",
        1,
    )
    agents_path.write_text(content, encoding="utf-8")

    record = service._load_state()
    assert record is not None
    package = record.plan_graph.package_by_id("WP1")
    package.stage = WorkPackageStage.FINAL_REVIEW
    package.status = "running"
    package.review_findings = [
        "HANDOFF.md is missing the durable WP17-S4 section",
        "implementation_summary overstates legacy-route removal",
    ]
    record.state = TaskExecutionState.WAITING_FOR_HUMAN_DECISION
    service.state_path.write_text(json.dumps(record.as_mapping()), encoding="utf-8")
    EventJournal(default_journal_path(service.state_root, service.task_id)).append(
        "human_intervention_required",
        {
            "package_id": "WP1",
            "stage": "final_review",
            "reason": "review/fix cycle budget exhausted",
            "evidence": list(package.review_findings),
        },
    )

    snapshot = service.snapshot()
    control = snapshot["run_control"]
    deterministic = snapshot["supervisor"]["deterministic_recovery"]

    assert control["can_start"] is True
    assert control["label"] == "Repair review findings directly"
    assert deterministic["available"] is True
    assert deterministic["finding_count"] == 2
    assert deterministic["avoid_supervisor_agent"] is True
    service.close()


def test_archive_view_assets_and_endpoints_are_packaged():
    from execraft.gui.server import _dashboard_html

    html = _dashboard_html("test-token")
    archive_js, content_type = _dashboard_asset("archive-view.js")
    app_js = _dashboard_asset("app.js")[0].decode("utf-8")

    assert content_type.startswith("text/javascript")
    assert b"class ArchiveView" in archive_js
    for identifier in (
        "projectArchiveTab",
        "projectArchiveView",
        "archivedTasks",
        "archivedProjects",
        "workPackageInspector",
        "pauseWorkPackageBtn",
    ):
        assert f'id="{identifier}"' in html
    assert "/api/archive" in app_js or b"/api/archive" in archive_js
    assert "/api/package/pause" in app_js


def test_dashboard_package_pause_uses_canonical_cli(tmp_path, monkeypatch):
    service = _service(tmp_path)
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, stdout='{"paused": true}\n', stderr="")

    monkeypatch.setattr("execraft.gui.server.subprocess.run", fake_run)
    result = service.set_package_pause(
        "WP1", paused=True, reason="maintenance", apply_to_shards=True
    )

    command = captured["command"]
    assert command[3:5] == ["orchestrate", "pause"]
    assert command[command.index("--package-id") + 1] == "WP1"
    assert command[command.index("--pause-reason") + 1] == "maintenance"
    assert "--apply-to-shards" in command
    assert result["paused"] is True
    assert result["package_id"] == "WP1"
    service.close()


def test_future_directives_remain_available_while_driver_is_running(
    tmp_path, monkeypatch
):
    service = _service(tmp_path)
    record = service._load_state()
    assert record is not None
    parent = record.plan_graph.package_by_id("WP1")
    parent.stage = WorkPackageStage.PREPARE
    parent.status = "pending"
    parent.agent_id = ""
    parent.execution_mode = "standard"
    service.state_path.write_text(
        json.dumps(record.as_mapping(), indent=2), encoding="utf-8"
    )
    active_status = {
        **service.process.status(),
        "owned_running": True,
        "external_running": False,
    }
    monkeypatch.setattr(service.process, "status", lambda: dict(active_status))

    pause = service.set_work_package_directive(
        "WP1",
        kind="pause_before_start",
        enabled=True,
        reason="operator checkpoint",
    )
    decomposition = service.set_work_package_directive(
        "WP1",
        kind="require_decomposition",
        enabled=True,
        reason="must split before implementation",
    )

    assert pause["queued_while_running"] is True
    assert decomposition["queued_while_running"] is True
    persisted = service._load_state().plan_graph.package_by_id("WP1")
    assert persisted.pause_before_start is False
    assert persisted.decomposition_required is False
    projected = next(
        item for item in service.snapshot()["packages"] if item["id"] == "WP1"
    )
    assert projected["pause_before_start"] is True
    assert projected["decomposition_required"] is True
    assert set(projected["directive_pending_sync"]) == {
        "pause_before_start",
        "require_decomposition",
    }
    service.close()


def test_operator_pause_run_control_offers_explicit_resume(tmp_path):
    service = _service(tmp_path)
    record = service._load_state()
    assert record is not None
    record.state = TaskExecutionState.OPERATOR_PAUSED
    record.waiting = {
        "kind": "pause_before_start",
        "package_id": "WP1",
        "reason": "review checkpoint",
    }

    control = service._run_control(record)

    assert control["can_start"] is True
    assert control["label"] == "Resume WP1"
    assert control["reason"] == "review checkpoint"
    service.close()


def test_planned_pause_execution_context_targets_reached_work_package(tmp_path):
    service = _service(tmp_path)
    record = service._load_state()
    assert record is not None
    package = record.plan_graph.package_by_id("WP1")
    package.stage = WorkPackageStage.PREPARE
    package.status = "pending"
    package.agent_id = ""
    package.pause_before_start_reached_at = "2026-08-04T15:00:00+00:00"
    record.state = TaskExecutionState.OPERATOR_PAUSED
    record.scheduler = {}
    record.waiting = {
        "kind": "pause_before_start",
        "package_id": "WP1",
        "stage": "prepare",
        "reason": "review checkpoint",
        "reached_at": package.pause_before_start_reached_at,
    }
    service.state_path.write_text(
        json.dumps(record.as_mapping(), indent=2), encoding="utf-8"
    )

    snapshot = service.snapshot()

    assert snapshot["execution_context"] == {
        "package_id": "WP1",
        "stage": "prepare",
        "agent_id": "",
        "status": "operator_paused",
        "source": "planned_pause",
    }
    assert snapshot["run_control"]["label"] == "Resume WP1"
    service.close()


def test_final_review_human_hold_offers_explicit_accept_and_continue(tmp_path):
    service = _service(tmp_path)
    record = service._load_state()
    assert record is not None
    package = record.plan_graph.package_by_id("WP1")
    package.stage = WorkPackageStage.FINAL_REVIEW
    package.status = "human_required"
    package.review_findings = [
        "WP1-FR-001 [critical] host-only acceptance evidence is missing"
    ]
    from execraft.orchestrate.models import AcceptanceCriterion
    package.acceptance_criteria = [
        AcceptanceCriterion(id="A1", description="Done", verified=False, evidence="")
    ]
    record.state = TaskExecutionState.HUMAN_REQUIRED
    service.state_path.write_text(json.dumps(record.as_mapping()), encoding="utf-8")
    EventJournal(default_journal_path(service.state_root, service.task_id)).append(
        "human_intervention_required",
        {
            "package_id": "WP1",
            "stage": "final_review",
            "blocked_requirement": "review/fix cycle budget exhausted",
            "evidence": ["real simulator run unavailable in the current sandbox"],
            "impact": "acceptance remains unverified",
        },
    )

    snapshot = service.snapshot()
    offer = snapshot["run_control"]["operator_acceptance"]

    assert offer["available"] is True
    assert offer["package_id"] == "WP1"
    assert offer["unverified_criteria"][0]["id"] == "A1"
    assert "does not mark" in offer["disclaimer"]
    service.close()


def test_operator_acceptance_ui_is_wired_to_stale_safe_api() -> None:
    from execraft.gui.server import _dashboard_html
    html = _dashboard_html("test-token")
    javascript = _dashboard_asset("run-control.js")[0].decode("utf-8")

    assert 'id="acceptRiskBtn"' in html
    assert 'id="operatorAcceptanceDialog"' in html
    assert 'id="operatorAcceptanceAcknowledge"' in html
    assert "/api/operator-acceptance?package_id=" in javascript
    assert '"/api/operator-acceptance/accept"' in javascript
    assert "expected_sequence: preview.sequence" in javascript
    assert "this decision remains explicit in the audit trail" in javascript
