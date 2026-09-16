"""Smoke tests for the Execraft CLI."""

import json
import subprocess
import sys
import os
from pathlib import Path

import yaml

from execraft.agents.config import parse_agent_configs, project_native_agent_configs

REPO_ROOT = Path(__file__).resolve().parent.parent
CLI = [sys.executable, "-m", "execraft.cli"]
CLI_ENV = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}


def test_projects_list():
    result = subprocess.run([*CLI, "projects", "list"], capture_output=True, text=True, env=CLI_ENV)
    assert result.returncode == 0
    assert "sample" in result.stdout


def _isolated_task_environment(tmp_path: Path) -> tuple[dict[str, str], Path]:
    control_root = tmp_path / "control"
    dossier = control_root / "projects" / "sample" / "tasks" / "demo-task"
    dossier.mkdir(parents=True)
    (dossier / "TASK.yaml").write_text(
        """schema_version: 2
id: demo-task
project: sample
title: Demo task
status: in_progress
git:
  branch_name: task/demo-task
repositories:
  - id: sample
    base_branch: main
    task_branch: task/demo-task
    role: component
    required: true
integration:
  verify: []
""",
        encoding="utf-8",
    )
    return {**CLI_ENV, "EXECRAFT_CONTROL_ROOT": str(control_root)}, dossier


def test_task_status(tmp_path):
    environment, _ = _isolated_task_environment(tmp_path)
    result = subprocess.run(
        [*CLI, "task", "status", "demo-task"], capture_output=True, text=True, env=environment
    )
    assert result.returncode == 0
    assert "Task: demo-task" in result.stdout
    assert "Control-plane Git:" in result.stdout
    assert "Runtime status:" in result.stdout
    assert "Status:" in result.stdout
    assert "(task lifecycle)" in result.stdout


def test_project_validate():
    result = subprocess.run(
        [*CLI, "project", "validate", "sample"], capture_output=True, text=True, env=CLI_ENV
    )
    assert result.returncode == 0
    assert "Project sample is valid" in result.stdout


def test_required_cutover_commands_are_exposed():
    result = subprocess.run([*CLI, "--help"], capture_output=True, text=True, env=CLI_ENV)
    for command in ("init", "start", "new", "project", "task", "workspace", "code", "agents", "doctor", "gui", "browser"):
        assert command in result.stdout

    workspace = subprocess.run(
        [*CLI, "workspace", "--help"], capture_output=True, text=True, env=CLI_ENV
    )
    for action in ("start", "status", "sync", "run", "verify", "refresh", "destroy"):
        assert action in workspace.stdout


def test_help_exits_zero():
    result = subprocess.run([*CLI, "--help"], capture_output=True, text=True, env=CLI_ENV)
    assert result.returncode == 0
    assert "usage:" in result.stdout


def test_no_args_exits_one():
    result = subprocess.run([*CLI], capture_output=True, text=True, env=CLI_ENV)
    # No args should show help and exit 1
    assert result.returncode == 1


def test_agents_status_reports_persisted_provider_health(tmp_path):
    from execraft.orchestrate.provider_health import ProviderHealthStore

    store = ProviderHealthStore(tmp_path / "provider-health.json")
    store.mark_failure(
        "opencode-go",
        reason="quota_exhausted",
        detail="monthly limit",
        retry_after_seconds=3600,
    )
    result = subprocess.run(
        [
            *CLI,
            "agents",
            "status",
            "--project",
            "sample",
            "--state-dir",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        env=CLI_ENV,
    )
    assert result.returncode == 1
    assert "opencode-go" in result.stdout
    assert "quota_exhausted" in result.stdout
    assert "available again:" in result.stdout


def test_agents_reset_health_clears_local_record(tmp_path):
    from execraft.orchestrate.provider_health import ProviderHealthStore

    store = ProviderHealthStore(tmp_path / "provider-health.json")
    store.mark_failure(
        "opencode-go",
        reason="quota_exhausted",
        retry_after_seconds=3600,
    )
    result = subprocess.run(
        [
            *CLI,
            "agents",
            "reset-health",
            "--project",
            "sample",
            "--agent",
            "opencode-go",
            "--state-dir",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        env=CLI_ENV,
    )
    assert result.returncode == 0
    assert ProviderHealthStore(tmp_path / "provider-health.json").get(
        "opencode-go"
    ).is_available


def test_agents_set_cooldown_persists_operator_deadline(tmp_path):
    from datetime import datetime, timedelta, timezone
    from execraft.orchestrate.provider_health import ProviderHealthStore

    deadline = datetime.now(timezone.utc) + timedelta(days=2)
    result = subprocess.run(
        [
            *CLI,
            "agents",
            "set-cooldown",
            "--project",
            "sample",
            "--agent",
            "opencode-go",
            "--until",
            deadline.isoformat(),
            "--reason",
            "session_limit",
            "--detail",
            "Reset shown in provider UI",
            "--state-dir",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        env=CLI_ENV,
    )

    assert result.returncode == 0, result.stderr
    assert "Execution-agent cooldown set: opencode-go" in result.stdout
    health = ProviderHealthStore(tmp_path / "provider-health.json").get(
        "opencode-go"
    )
    assert health.reason == "session_limit"
    assert health.detail == "Reset shown in provider UI"
    assert not health.is_available


def test_agents_can_promote_list_and_revoke_provider_for_one_task(tmp_path):
    from execraft.orchestrate.identity import resolve_storage_identity
    from execraft.orchestrate.provider_promotion import ProviderPromotionStore

    common = [
        *CLI,
        "agents",
        "promote",
        "--project",
        "sample",
        "--task-id",
        "feature_refactor",
        "--agent",
        "opencode-ollama-gpu-a-coder",
        "--capability",
        "review",
        "--max-complexity",
        "100",
        "--for",
        "4h",
        "--package-id",
        "WP23",
        "--reason",
        "premium provider quota unavailable",
        "--state-dir",
        str(tmp_path),
    ]
    promoted = subprocess.run(common, capture_output=True, text=True, env=CLI_ENV)

    assert promoted.returncode == 0, promoted.stderr
    assert "75->100" in promoted.stdout
    identity = resolve_storage_identity(
        tmp_path, project_id="sample", task_id="feature_refactor"
    )
    store = ProviderPromotionStore(identity.state_dir / "provider-promotions.json")
    records = store.list(provider_id="opencode-ollama-gpu-a-coder")
    assert len(records) == 1
    assert records[0].capability == "review"
    assert records[0].package_id == "WP23"
    assert records[0].fallback_only is True

    listed = subprocess.run(
        [
            *CLI,
            "agents",
            "promotions",
            "--project",
            "sample",
            "--task-id",
            "feature_refactor",
            "--agent",
            "opencode-ollama-gpu-a-coder",
            "--state-dir",
            str(tmp_path),
            "--json",
        ],
        capture_output=True,
        text=True,
        env=CLI_ENV,
    )
    assert listed.returncode == 0, listed.stderr
    assert json.loads(listed.stdout)[0]["promoted_max_complexity"] == 100

    revoked = subprocess.run(
        [
            *CLI,
            "agents",
            "revoke-promotion",
            "--project",
            "sample",
            "--task-id",
            "feature_refactor",
            "--agent",
            "opencode-ollama-gpu-a-coder",
            "--capability",
            "review",
            "--state-dir",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        env=CLI_ENV,
    )
    assert revoked.returncode == 0, revoked.stderr
    assert store.list(provider_id="opencode-ollama-gpu-a-coder") == []


def test_agents_status_can_filter_to_one_provider(tmp_path):
    result = subprocess.run(
        [
            *CLI,
            "agents",
            "status",
            "--project",
            "sample",
            "--agent",
            "opencode-ollama-gpu-a-coder",
            "--state-dir",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        env=CLI_ENV,
    )

    assert result.returncode == 0, result.stderr
    assert "opencode-ollama-gpu-a-coder" in result.stdout
    assert "qwen3-coder:30b-32k" in result.stdout
    assert "first output timeout: 180s" in result.stdout
    assert "opencode-ollama-gpu-a\n" not in result.stdout
    assert "codex\n" not in result.stdout


def test_agents_doctor_can_inspect_an_explicit_provider(tmp_path):
    raw = yaml.safe_load(
        (REPO_ROOT / "projects" / "sample" / "agents.yaml").read_text(
            encoding="utf-8"
        )
    )
    configured = {
        item.provider_id: item
        for item in project_native_agent_configs(raw)[0]
    }["opencode-go"]
    opencode = tmp_path / "opencode"
    opencode.write_text(
        f"#!/bin/sh\nprintf '%s\\n' '{configured.model}'\n",
        encoding="utf-8",
    )
    opencode.chmod(0o755)
    environment = {
        **CLI_ENV,
        "PATH": f"{tmp_path}{os.pathsep}{CLI_ENV.get('PATH', '')}",
    }
    result = subprocess.run(
        [
            *CLI,
            "agents",
            "doctor",
            "--project",
            "sample",
            "--agent",
            "opencode-go",
            "--state-dir",
            str(tmp_path),
            "--json",
        ],
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    diagnostics = __import__("json").loads(result.stdout)
    assert diagnostics[0]["provider_id"] == "opencode-go"
    assert diagnostics[0]["model"] == configured.model
    assert diagnostics[0]["model_available"] is True


def test_task_sync_status_without_orchestrator_state(tmp_path):
    environment, dossier = _isolated_task_environment(tmp_path)
    state_dir = tmp_path / "state"
    result = subprocess.run(
        [
            *CLI,
            "task",
            "sync-status",
            "demo-task",
            "--state-dir",
            str(state_dir),
        ],
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    assert "Synchronized runtime status: not initialized" in result.stdout
    runtime = dossier / "RUNTIME_STATUS.md"
    assert runtime.is_file()
    assert "No local orchestration state exists yet" in runtime.read_text()


def test_orchestrate_help_exposes_manual_decomposition():
    result = subprocess.run(
        [*CLI, "orchestrate", "--help"], capture_output=True, text=True, env=CLI_ENV
    )
    assert result.returncode == 0
    assert "decompose" in result.stdout


def test_orchestrate_help_exposes_invocation_trace():
    result = subprocess.run(
        [*CLI, "orchestrate", "--help"],
        capture_output=True,
        text=True,
        env=CLI_ENV,
    )

    assert result.returncode == 0
    assert "trace" in result.stdout
    assert "--include-handoff" in result.stdout
    assert "scope" in result.stdout
    assert "supervisor" in result.stdout
    assert "--package-id" in result.stdout
    assert "--accept-scope" in result.stdout


def test_orchestrate_supervisor_status_renders_plain_language_question(
    monkeypatch, capsys
):
    from types import SimpleNamespace

    import execraft.cli as cli

    class FakeOrchestrator:
        state = SimpleNamespace(value="waiting_for_human_decision")

        def load_state(self):
            return None

        def supervisor_status_report(self):
            return {
                "enabled": True,
                "available": True,
                "configured_agent": "codex",
                "agent_id": "codex",
                "policy": {"max_attempts_per_incident": 3},
                "incident": {
                    "incident_id": "incident-1",
                    "package_id": "WP17__WP17-S4",
                    "classification": "requirement_ambiguity",
                    "status": "waiting_for_human",
                    "attempts": 1,
                    "summary": "The PLAN does not determine the compatibility cutover.",
                    "human_question": {
                        "question": "Should the legacy route remain until WP17-S5?",
                        "context": "Removing it now changes the approved migration order.",
                        "options": [
                            {
                                "id": "keep",
                                "label": "Keep it until WP17-S5",
                                "consequence": "Only evidence and summaries are corrected.",
                            },
                            {
                                "id": "remove",
                                "label": "Remove it in WP17-S4",
                                "consequence": "The Supervisor updates code and PLAN scope.",
                            },
                        ],
                    },
                },
            }

    monkeypatch.setattr(
        cli,
        "_build_orchestrator",
        lambda _args: (FakeOrchestrator(), "sample_task", [], None),
    )
    args = SimpleNamespace(
        action="supervisor",
        project_id="sample",
        answer=None,
        message="",
    )

    assert cli.cmd_orchestrate(args) == 0
    output = capsys.readouterr().out
    assert "Resolved agent: codex" in output
    assert "Should the legacy route remain until WP17-S5?" in output
    assert "keep: Keep it until WP17-S5" in output
    assert "--answer <option-id>" in output


def test_supervisor_terminal_pause_renders_digest_and_possible_solutions(capsys):
    import execraft.cli as cli

    class FakeOrchestrator:
        def supervisor_status_report(self):
            return {
                "incident": {
                    "package_id": "WP17__WP17-S4",
                    "classification": "requirement_ambiguity",
                    "summary": "Two autonomous repair campaigns remain rejected.",
                    "actions_taken": [
                        "Compared the review findings with the PLAN.",
                        "Confirmed that the remaining choice changes package scope.",
                    ],
                    "human_question": {
                        "question": "Implement the full transport now or defer it?",
                        "context": "The current UI still routes Edge control through Core.",
                        "recommended_option": "implement_now",
                        "options": [
                            {
                                "id": "implement_now",
                                "label": "Implement it now",
                                "consequence": "Repair, verify, and review again.",
                            },
                            {
                                "id": "defer",
                                "label": "Defer it",
                                "consequence": "Replan the package acceptance criteria.",
                            },
                        ],
                    },
                }
            }

    assert cli._print_supervisor_human_decision(
        FakeOrchestrator(),
        project_id="sample",
        task_id="sample_task",
    )
    output = capsys.readouterr().out
    assert "Supervisor digest — human decision required" in output
    assert "Two autonomous repair campaigns remain rejected" in output
    assert "Possible solutions:" in output
    assert "implement_now: Implement it now (recommended)" in output
    assert "Repair, verify, and review again" in output
    assert "--answer <option-id>" in output


def test_orchestrate_supervisor_answer_is_recorded_and_resume_is_explicit(
    monkeypatch, capsys
):
    from types import SimpleNamespace

    import execraft.cli as cli

    class FakeOrchestrator:
        state = SimpleNamespace(value="waiting_for_human_decision")

        def __init__(self):
            self.submitted = None

        def load_state(self):
            return None

        def submit_supervisor_answer(self, option_id, *, message):
            self.submitted = (option_id, message)
            self.state.value = "supervising"
            return {"incident_id": "incident-1"}

    orchestrator = FakeOrchestrator()
    monkeypatch.setattr(
        cli,
        "_build_orchestrator",
        lambda _args: (orchestrator, "sample_task", [], None),
    )
    args = SimpleNamespace(
        action="supervisor",
        project_id="sample",
        answer="keep",
        message="Preserve compatibility until the next tranche.",
    )

    assert cli.cmd_orchestrate(args) == 0
    assert orchestrator.submitted == (
        "keep",
        "Preserve compatibility until the next tranche.",
    )
    output = capsys.readouterr().out
    assert "Supervisor answer recorded for incident incident-1" in output
    assert "execraft orchestrate run --project sample" in output


def test_commit_policy_enables_explicit_automatic_mode():
    from execraft.cli_config import apply_commit_policy
    from execraft.orchestrate import OrchestrationConfig

    config = OrchestrationConfig(auto_commit=False)
    apply_commit_policy(config, {"commit": {"mode": "automatic"}})

    assert config.auto_commit is True


def test_commit_policy_absence_preserves_embedding_override():
    from execraft.cli_config import apply_commit_policy
    from execraft.orchestrate import OrchestrationConfig

    config = OrchestrationConfig(auto_commit=False)
    apply_commit_policy(config, {})

    assert config.auto_commit is False


def test_commit_policy_rejects_unknown_mode():
    import pytest

    from execraft.cli_config import apply_commit_policy
    from execraft.orchestrate import OrchestrationConfig
    from execraft.workspace.task_git import TaskGitError

    with pytest.raises(TaskGitError, match="supported modes: automatic"):
        apply_commit_policy(
            OrchestrationConfig(), {"commit": {"mode": "approval"}}
        )


def test_commit_policy_requires_mapping():
    import pytest

    from execraft.cli_config import apply_commit_policy
    from execraft.orchestrate import OrchestrationConfig
    from execraft.workspace.task_git import TaskGitError

    with pytest.raises(TaskGitError, match="commit policy must be a mapping"):
        apply_commit_policy(OrchestrationConfig(), {"commit": "automatic"})


def test_commit_policy_enables_verified_noop_transactions():
    from execraft.cli_config import apply_commit_policy
    from execraft.orchestrate import OrchestrationConfig

    config = OrchestrationConfig()
    apply_commit_policy(
        config,
        {"commit": {"mode": "automatic", "allow_verified_noop": True}},
    )

    assert config.auto_commit is True
    assert config.allow_verified_noop_commits is True


def test_commit_policy_requires_boolean_verified_noop_flag():
    import pytest

    from execraft.cli_config import apply_commit_policy
    from execraft.orchestrate import OrchestrationConfig
    from execraft.workspace.task_git import TaskGitError

    with pytest.raises(TaskGitError, match="allow_verified_noop must be a boolean"):
        apply_commit_policy(
            OrchestrationConfig(),
            {"commit": {"mode": "automatic", "allow_verified_noop": "yes"}},
        )


def test_orchestrate_run_allows_reconcilable_verified_noop_human_required(monkeypatch):
    from types import SimpleNamespace

    import execraft.cli as cli

    class FakeOrchestrator:
        def __init__(self):
            self.state = SimpleNamespace(value="human_required")
            self.run_called = False

        def load_state(self):
            return None

        def can_auto_resume_agent_wait(self):
            return False

        def can_auto_reconcile_scope_check(self):
            return False

        def can_auto_reconcile_verified_noop_commit(self):
            return True

        def can_auto_resume_human_required(self):
            return True

        def run_pipeline(self):
            self.run_called = True
            self.state.value = "completed"

        def status_report(self):
            return {
                "state": self.state.value,
                "waiting": None,
                "human_required": None,
            }

    orchestrator = FakeOrchestrator()
    monkeypatch.setattr(
        cli,
        "_build_orchestrator",
        lambda _args: (orchestrator, "sample_task", [], None),
    )
    args = SimpleNamespace(
        action="run",
        project_id="sample",
        quiet=True,
        no_wait_for_agents=True,
    )

    assert cli.cmd_orchestrate(args) == 0
    assert orchestrator.run_called is True


def test_orchestrate_run_treats_supervisor_human_decision_as_clean_handoff(
    monkeypatch, capsys
):
    from types import SimpleNamespace

    import execraft.cli as cli

    class FakeOrchestrator:
        def __init__(self):
            self.state = SimpleNamespace(value="running")

        def load_state(self):
            return None

        def run_pipeline(self):
            self.state.value = "waiting_for_human_decision"

        def status_report(self):
            return {
                "state": self.state.value,
                "waiting": None,
                "human_required": None,
            }

        def supervisor_status_report(self):
            return {
                "incident": {
                    "package_id": "WP17__WP17-S4",
                    "classification": "review_exhausted",
                    "human_question": {
                        "question": "Inspect and retry?",
                        "context": "The bounded attempt budget was exhausted.",
                        "recommended_option": "inspect_and_retry",
                        "options": [
                            {
                                "id": "inspect_and_retry",
                                "label": "Inspect and retry",
                                "consequence": "Start a fresh bounded attempt.",
                            },
                            {
                                "id": "stop",
                                "label": "Stop",
                                "consequence": "Leave the task paused.",
                            },
                        ],
                    },
                }
            }

    monkeypatch.setattr(
        cli,
        "_build_orchestrator",
        lambda _args: (FakeOrchestrator(), "sample_task", [], None),
    )
    args = SimpleNamespace(
        action="run",
        project_id="sample",
        quiet=True,
        no_wait_for_agents=True,
    )

    assert cli.cmd_orchestrate(args) == 0
    assert "Supervisor digest — human decision required" in capsys.readouterr().out


def test_orchestrate_daemon_allows_reconcilable_verified_noop_human_required(
    monkeypatch,
):
    from types import SimpleNamespace

    import execraft.cli as cli

    class FakeOrchestrator:
        def __init__(self):
            self.state = SimpleNamespace(value="human_required")

        def load_state(self):
            return None

        def can_auto_resume_agent_wait(self):
            return False

        def can_auto_reconcile_scope_check(self):
            return False

        def can_auto_reconcile_verified_noop_commit(self):
            return True

        def can_auto_resume_human_required(self):
            return True

        def status_report(self):
            return {
                "state": self.state.value,
                "human_required": None,
            }

    orchestrator = FakeOrchestrator()
    monkeypatch.setattr(
        cli,
        "_build_orchestrator",
        lambda _args: (orchestrator, "sample_task", [], None),
    )

    def fake_run_until_terminal(_orchestrator, *, config):
        del config
        _orchestrator.state.value = "completed"
        return SimpleNamespace(attempts=1, exhausted=False)

    monkeypatch.setattr(cli, "run_until_terminal", fake_run_until_terminal)
    args = SimpleNamespace(
        action="daemon",
        project_id="sample",
        quiet=True,
        max_attempts=1,
        initial_backoff_seconds=0.0,
        max_backoff_seconds=0.0,
        backoff_multiplier=1.0,
    )

    assert cli.cmd_orchestrate(args) == 0


def test_scope_report_surfaces_cross_repository_automatic_recovery(
    monkeypatch, capsys
):
    from types import SimpleNamespace

    import execraft.cli as cli

    class FakeOrchestrator:
        state = SimpleNamespace(value="human_required")

        def load_state(self):
            return None

        def declared_write_scope_report(self, package_id):
            assert package_id == "WP17__WP17-S4"
            return {
                "package_id": package_id,
                "stage": "ready_to_commit",
                "parent_id": "WP17",
                "write_scope": ["frontend/sample"],
                "violations": [],
                "assessments": [],
                "workspace_scope": {
                    "candidates": [
                        {
                            "path": "worker_a:apps/uav_navigation/CMakeLists.txt",
                            "relationship": "undeclared_repository",
                        }
                    ]
                },
                "auto_recovery": {
                    "can_recover": True,
                    "mode": "agent",
                    "reason": "the complete out-of-scope workspace delta can be recovered",
                    "candidate_repositories": ["worker_a"],
                    "attempts": 0,
                    "max_attempts": 2,
                },
                "reconciliation": {
                    "can_reconcile": False,
                    "reason": "repositories outside the package scope are still dirty",
                },
            }

    monkeypatch.setattr(
        cli,
        "_build_orchestrator",
        lambda _args: (FakeOrchestrator(), "sample_task", [], None),
    )
    args = SimpleNamespace(
        action="scope",
        project_id="sample",
        package_id="WP17__WP17-S4",
        accept_scope=False,
        reconcile_scope=False,
    )

    assert cli.cmd_orchestrate(args) == 0
    output = capsys.readouterr().out
    assert "Violations: 0" in output
    assert "Unowned workspace changes: 1" in output
    assert "worker_a:apps/uav_navigation/CMakeLists.txt" in output
    assert "Automatic recovery: available" in output
    assert "Candidate repositories:" in output
    assert "Run / Resume will invoke automatic workspace recovery" in output


def test_scope_json_acceptance_forwards_previewed_candidate_guard(
    monkeypatch, capsys
):
    import json
    from types import SimpleNamespace

    import execraft.cli as cli

    calls = []

    class FakeOrchestrator:
        state = SimpleNamespace(value="human_required")

        def load_state(self):
            return None

        def approve_declared_write_scope(
            self, package_id, *, expected_candidates=None
        ):
            calls.append((package_id, expected_candidates))
            return {
                "package_id": package_id,
                "added_paths": ["repo/.github/workflows/ci.yml"],
                "previous_stage": "final_review",
                "next_stage": "regression_verify",
            }

    monkeypatch.setattr(
        cli,
        "_build_orchestrator",
        lambda _args: (FakeOrchestrator(), "sample_task", [], None),
    )
    args = SimpleNamespace(
        action="scope",
        project_id="sample",
        package_id="WP22__WP22-S4",
        accept_scope=True,
        reconcile_scope=False,
        expected_scope_json='["repo:.github/workflows/ci.yml"]',
        json=True,
    )

    assert cli.cmd_orchestrate(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["next_stage"] == "regression_verify"
    assert calls == [
        ("WP22__WP22-S4", ["repo:.github/workflows/ci.yml"])
    ]


def test_orchestrate_supervisor_status_shows_pending_delegation_auto_resume(
    monkeypatch, capsys
):
    from types import SimpleNamespace

    import execraft.cli as cli

    class FakeOrchestrator:
        state = SimpleNamespace(value="waiting_for_human_decision")

        def load_state(self):
            return None

        def supervisor_status_report(self):
            return {
                "enabled": True,
                "available": True,
                "configured_agent": "codex",
                "agent_id": "codex",
                "policy": {"max_attempts_per_incident": 3},
                "auto_resume_lost_delegation": True,
                "pending_delegation_count": 1,
                "pending_delegation_source": "persisted_queue",
                "stale_human_question": True,
                "incident": {
                    "incident_id": "incident-1",
                    "package_id": "WP17__WP17-S4",
                    "classification": "agent_failure",
                    "status": "delegating",
                    "attempts": 2,
                    "summary": "A delegated repair is pending failover.",
                    "human_question": {
                        "question": "How should recovery continue?",
                        "options": [{"id": "retry", "label": "Retry"}],
                    },
                },
            }

    monkeypatch.setattr(
        cli,
        "_build_orchestrator",
        lambda _args: (FakeOrchestrator(), "sample_task", [], None),
    )
    args = SimpleNamespace(
        action="supervisor",
        project_id="sample",
        answer=None,
        message="",
    )

    assert cli.cmd_orchestrate(args) == 0
    output = capsys.readouterr().out
    assert "Automatic resume: available" in output
    assert "1 pending delegation" in output
    assert "source=persisted_queue" in output
    assert "Stale human decision" in output
    assert "Human decision requested:" not in output
    assert "execraft orchestrate run --project sample" in output


def test_task_delete_supports_dry_run_and_explicit_yes(tmp_path):
    environment, dossier = _isolated_task_environment(tmp_path)
    state = tmp_path / "state"
    environment.update(
        {
            "EXECRAFT_STATE_HOME": str(state),
            "EXECRAFT_CONFIG_HOME": str(tmp_path / "config"),
        }
    )
    dry_run = subprocess.run(
        [
            *CLI,
            "task",
            "delete",
            "demo-task",
            "--project",
            "sample",
            "--dry-run",
            "--json",
        ],
        capture_output=True,
        text=True,
        env=environment,
    )
    assert dry_run.returncode == 0, dry_run.stderr
    assert json.loads(dry_run.stdout)["dry_run"] is True
    assert dossier.is_dir()

    rejected = subprocess.run(
        [*CLI, "task", "delete", "demo-task", "--project", "sample"],
        capture_output=True,
        text=True,
        env=environment,
    )
    assert rejected.returncode == 2
    assert "requires --yes" in rejected.stderr
    assert dossier.is_dir()

    deleted = subprocess.run(
        [
            *CLI,
            "task",
            "delete",
            "demo-task",
            "--project",
            "sample",
            "--yes",
        ],
        capture_output=True,
        text=True,
        env=environment,
    )
    assert deleted.returncode == 0, deleted.stderr
    assert not dossier.exists()


def test_project_delete_removes_internal_descriptor_but_preserves_source(tmp_path):
    control = tmp_path / "control"
    project = control / "projects" / "alpha"
    (project / "tasks").mkdir(parents=True)
    (project / "project.yaml").write_text(
        "\n".join(
            (
                "schema_version: 2",
                "project: alpha",
                "path_base: project_directory",
                "repositories:",
                "  - id: component",
                "    path: .",
                "    workspace_name: component",
                "",
            )
        ),
        encoding="utf-8",
    )
    source = tmp_path / "source"
    source.mkdir()
    environment = {
        **CLI_ENV,
        "EXECRAFT_CONTROL_ROOT": str(control),
        "EXECRAFT_STATE_HOME": str(tmp_path / "state"),
        "EXECRAFT_CONFIG_HOME": str(tmp_path / "config"),
    }

    result = subprocess.run(
        [*CLI, "project", "delete", "alpha", "--yes"],
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    assert not project.exists()
    assert source.is_dir()


def test_execution_policy_report_uses_canonical_role_inventory(capsys):
    from execraft.cli_orchestration_output import _print_execution_policy_report

    _print_execution_policy_report(
        "WP1",
        {"agent_preferences": {"implement": ["codex"]}, "skill_preferences": {}},
        apply_to_shards=False,
    )

    output = capsys.readouterr().out
    assert "Execution policy updated for WP1" in output
    assert "implement: agents=codex" in output
