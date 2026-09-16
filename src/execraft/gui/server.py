"""Dependency-free local web dashboard for the Execraft orchestrator.

The dashboard is intentionally a projection and control surface over the existing
CLI/state model.  It never owns a second orchestration implementation: run/stop
operations launch the normal ``execraft orchestrate run`` command, while all workflow
and agent data is read from the same durable files used by the CLI.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Mapping

from execraft.gui.errors import GuiError
from execraft.network import is_loopback_host
from execraft.gui.dashboard_presenter import (
    active_assignments,
    agent_nodes,
    execution_contexts as build_execution_contexts,
    package_with_pending_directives,
    primary_execution_context as build_primary_execution_context,
)
from execraft.gui.execution_health import build_execution_health_rows
from execraft.gui.execution_lanes import execution_lane_mappings
from execraft.persistence import (
    FileLock,
    LockBusyError,
    LockLevel,
    LockUnavailableError,
    atomic_write_text,
    file_lock_is_held,
)
from execraft.gui.final_sync import FinalSyncDashboardMixin, reconcile_run_status
from execraft.gui.protected_scope import ProtectedScopeDashboardMixin
from execraft.gui.provider_promotions import ProviderPromotionDashboardMixin
from execraft.gui.runtime_topology import (
    RuntimeTopologyDashboardMixin,
    review_capable_candidates,
)
from execraft.gui.task_lifecycle import TaskLifecycleController
from execraft.gui.routes import GuiApiRouter, RouteNotFound
from execraft.gui.routes.payload import string_list_mapping as _string_list_mapping
from urllib.parse import parse_qs, urlparse

import yaml

from execraft.control_plane import xdg_state_home
from execraft.agents import (
    AgentConfigError,
    OpenCodeProviderRegistry,
    OpenCodeRegistryError,
    load_opencode_provider_registry,
    project_native_agent_configs,
    parse_execution_config,
)
from execraft.agents.execution_compat import project_native_profile_legacy_config
from execraft.runtime.native import build_native_runtime
from execraft.runtime_config import RuntimeKind

from execraft.orchestrate import (
    AgentCapability,
    IncidentClass,
    IncidentStatus,
    SupervisorConfigError,
    SupervisorIncidentStore,
    SupervisorPolicy,
    classify_incident,
    is_recoverable_prompt_transport_failure,
    recovery_playbook_policy_from_scheduling,
    unfinished_supervisor_delegations,
    load_plan_graph_file,
)
from execraft.orchestrate.agent_console import (
    AgentConsoleStore,
    InteractiveTerminalPolicy,
)
from execraft.orchestrate.directives import (
    PAUSE_FOR_REPOSITORY_SYNC,
    REQUIRE_DECOMPOSITION,
    WORK_PACKAGE_DIRECTIVE_KINDS,
    WorkPackageDirectiveQueue,
)
from execraft.orchestrate.execution_policy import execution_role_metadata
from execraft.orchestrate.execution_trace import build_execution_trace, trace_scope_packages
from execraft.skills import SkillCatalog, SkillCatalogError
from execraft.targets.health import probe_model_endpoint
from execraft.orchestrate.identity import resolve_storage_identity
from execraft.orchestrate.journal import EventJournal
from execraft.orchestrate.invocations import AgentInvocationStore
from execraft.orchestrate.models import (
    OrchestrateError,
    TaskExecutionState,
    TaskExecutionStateRecord,
    WorkPackageStage,
)
from execraft.orchestrate.operator_action import (
    is_auto_resumable_agent_wait,
    is_completed_repository_scope_action,
    is_repository_scope_action,
)
from execraft.orchestrate.scope_policy import (
    ScopePolicy,
    scope_recovery_policy_from_scheduling,
)
from execraft.orchestrate.package_finalization import (
    workspace_finalization_policy_from_scheduling,
)
from execraft.orchestrate.provider_health import ProviderHealthStore
from execraft.project import (
    ProjectError,
    load_project,
    load_registered_project,
)
from execraft.workspace.task_git import TaskGitError, TaskManifest
from execraft.workspace.workspace_git import load_workspace
from execraft.gui.workspace_changes import (
    WorkspaceChangeError,
    WorkspaceChangeManager,
)
from execraft.gui.operator_acceptance import OperatorAcceptanceDashboardMixin
from execraft.gui.downloads import BinaryDownload
from execraft.gui.http_transport import is_client_disconnect as _is_client_disconnect
from execraft.gui.manual_agent_console import (
    ManualAgentConsoleError,
    ManualAgentConsoleManager,
)

@dataclass(frozen=True)
class ConfigFileSpec:
    key: str
    label: str
    path: Path
    validator: Callable[[str], None]
    requires_idle: bool = False

class EndpointProbeCache:
    """Short-lived endpoint health cache so UI polling does not hammer satellites."""

    def __init__(self, ttl_seconds: float = 12.0) -> None:
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self._lock = threading.Lock()
        self._expires_at = 0.0
        self._result: dict[str, dict[str, Any]] = {}

    def probe(self, endpoints: list[Any]) -> dict[str, dict[str, Any]]:
        now = time.monotonic()
        with self._lock:
            if now < self._expires_at:
                return {key: dict(value) for key, value in self._result.items()}
        with ThreadPoolExecutor(max_workers=max(1, min(4, len(endpoints)))) as executor:
            rows = list(executor.map(self._probe_one, endpoints)) if endpoints else []
        result = {row[0]: row[1] for row in rows}
        with self._lock:
            self._result = result
            self._expires_at = time.monotonic() + self.ttl_seconds
        return {key: dict(value) for key, value in result.items()}

    @staticmethod
    def _probe_one(endpoint: Any) -> tuple[str, dict[str, Any]]:
        generic = (
            endpoint.as_model_endpoint()
            if hasattr(endpoint, "as_model_endpoint")
            else endpoint
        )
        probe = probe_model_endpoint(
            generic,
            user_agent="execraft-gui/0.1",
            max_response_bytes=2_000_000,
            timeout_seconds=min(3, generic.connect_timeout_seconds),
            probe_loaded_models=True,
            opener=urllib.request.urlopen,
        )
        return endpoint.endpoint_id, {
            "reachable": probe.reachable,
            "latency_ms": probe.latency_ms,
            "models": sorted(probe.models),
            "loaded_models": sorted(probe.loaded_models),
            "loaded_models_supported": probe.loaded_models_supported,
            "loaded_models_error": probe.loaded_models_error,
            "error": probe.error,
            "checked_at": _utc_now(),
        }

class OrchestratorProcessController:
    """Own at most one CLI driver process started from this dashboard."""

    def __init__(
        self,
        *,
        root: Path,
        project_id: str,
        task_id: str,
        state_root: Path,
        popen_factory: Callable[..., subprocess.Popen[Any]] = subprocess.Popen,
        run_factory: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.root = root.resolve()
        self.project_id = project_id
        self.task_id = task_id
        self.state_root = state_root.resolve()
        self._popen_factory = popen_factory
        self._run_factory = run_factory
        self._process: subprocess.Popen[Any] | None = None
        self._lock = threading.Lock()
        self._started_at = ""
        self._last_exit_at = ""
        self._last_exit_code: int | None = None
        self._last_exit_summary = ""
        self._log_start_offset = 0
        self.storage_identity = resolve_storage_identity(
            self.state_root,
            project_id=self.project_id,
            task_id=self.task_id,
        )
        self._driver_log = self.storage_identity.state_dir / "gui-driver.log"

    @property
    def command(self) -> list[str]:
        return [
            sys.executable,
            "-m",
            "execraft.cli",
            "orchestrate",
            "run",
            "--project",
            self.project_id,
            "--task-id",
            self.task_id,
            "--state-dir",
            str(self.state_root),
            "--quiet",
        ]

    def initialize(self, plan_file: Path) -> dict[str, Any]:
        """Initialize durable orchestration state through the canonical CLI path."""

        plan_file = plan_file.expanduser().resolve()
        if not plan_file.is_file():
            raise GuiError(f"plan graph not found: {plan_file}")
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                raise GuiError("cannot initialize while the dashboard orchestrator is running")
            if self._driver_lock_held():
                raise GuiError("cannot initialize while an external orchestrator driver is active")
            command = [
                sys.executable,
                "-m",
                "execraft.cli",
                "orchestrate",
                "init",
                "--project",
                self.project_id,
                "--task-id",
                self.task_id,
                "--state-dir",
                str(self.state_root),
                "--plan-file",
                str(plan_file),
            ]
            env = os.environ.copy()
            source_tree = self.root / "src"
            if source_tree.is_dir():
                existing = env.get("PYTHONPATH", "")
                source_path = str(source_tree)
                env["PYTHONPATH"] = (
                    source_path
                    if not existing
                    else source_path + os.pathsep + existing
                )
            env["EXECRAFT_CONTROL_ROOT"] = str(self.root)
            try:
                completed = self._run_factory(
                    command,
                    cwd=self.root,
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=120,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise GuiError(f"cannot initialize orchestration: {exc}") from exc
        output = "\n".join(
            part.strip() for part in (completed.stdout, completed.stderr) if part.strip()
        )
        if completed.returncode != 0:
            raise GuiError(
                "orchestration initialization failed"
                + (f": {output}" if output else f" with exit code {completed.returncode}")
            )
        return {
            "initialized": True,
            "output": output,
            **self.status(),
        }

    def start(self, *, no_wait_for_agents: bool = False) -> dict[str, Any]:
        """Start the normal foreground orchestrator driver.

        Do not call :meth:`status` while holding ``self._lock``.  ``status``
        acquires the same non-reentrant lock, which previously deadlocked every
        successful GUI launch after ``Popen`` returned.  The process was really
        created, but the HTTP request never completed and the control center
        appeared unresponsive.
        """

        with self._lock:
            if self._process is not None and self._process.poll() is None:
                raise GuiError("the dashboard already owns a running orchestrator")
            if self._driver_lock_held():
                raise GuiError(
                    "an orchestrate run/daemon driver is already active outside this dashboard"
                )
            self._driver_log.parent.mkdir(parents=True, exist_ok=True)
            command = list(self.command)
            if no_wait_for_agents:
                command.append("--no-wait-for-agents")
            env = os.environ.copy()
            source_tree = self.root / "src"
            if source_tree.is_dir():
                existing = env.get("PYTHONPATH", "")
                source_path = str(source_tree)
                env["PYTHONPATH"] = (
                    source_path
                    if not existing
                    else source_path + os.pathsep + existing
                )
            env["EXECRAFT_CONTROL_ROOT"] = str(self.root)
            self._log_start_offset = (
                self._driver_log.stat().st_size if self._driver_log.is_file() else 0
            )
            log_handle = self._driver_log.open("ab", buffering=0)
            try:
                self._process = self._popen_factory(
                    command,
                    cwd=self.root,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    start_new_session=(os.name != "nt"),
                )
            except Exception:
                log_handle.close()
                raise
            # Popen owns the duplicated descriptor after process creation.
            log_handle.close()
            self._started_at = _utc_now()
            self._last_exit_at = ""
            self._last_exit_code = None
            self._last_exit_summary = ""

        # Important: status() acquires self._lock and therefore must run only
        # after the launch critical section has been released.
        return self.status()

    def stop(self, timeout_seconds: float = 8.0) -> dict[str, Any]:
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                raise GuiError(
                    "no dashboard-owned orchestrator is running; external drivers are not terminated"
                )
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGTERM)
            else:  # pragma: no cover
                process.terminate()
        try:
            process.wait(timeout=max(0.1, timeout_seconds))
        except subprocess.TimeoutExpired:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:  # pragma: no cover
                process.kill()
            process.wait(timeout=3)
        with self._lock:
            self._last_exit_code = process.returncode
        return self.status()

    def close(self) -> None:
        with self._lock:
            process = self._process
        if process is not None and process.poll() is None:
            try:
                self.stop(timeout_seconds=3)
            except (GuiError, OSError, subprocess.SubprocessError):
                pass

    def status(self) -> dict[str, Any]:
        with self._lock:
            process = self._process
            if process is not None:
                code = process.poll()
                if code is not None:
                    self._record_exit_locked(code)
            else:
                code = None
            owned_running = process is not None and code is None
            pid = process.pid if owned_running else None
            return {
                "owned_running": owned_running,
                "external_running": self._driver_lock_held() and not owned_running,
                "pid": pid,
                "started_at": self._started_at,
                "last_exit_at": self._last_exit_at,
                "last_exit_code": self._last_exit_code,
                "last_exit_summary": self._last_exit_summary,
                "driver_log": str(self._driver_log),
                "command": list(self.command),
            }

    def _record_exit_locked(self, code: int) -> None:
        """Persist one concise process-exit diagnostic while holding the lock."""

        if self._last_exit_code == code and self._last_exit_at:
            return
        self._last_exit_code = code
        self._last_exit_at = _utc_now()
        self._last_exit_summary = self._read_driver_log_tail(
            start_offset=self._log_start_offset,
            limit=12_000,
        )

    def _read_driver_log_tail(self, *, start_offset: int = 0, limit: int = 12_000) -> str:
        if not self._driver_log.is_file():
            return ""
        try:
            size = self._driver_log.stat().st_size
            offset = max(start_offset, size - max(1, int(limit)))
            with self._driver_log.open("rb") as handle:
                handle.seek(offset)
                data = handle.read()
        except OSError:
            return ""
        text = data.decode("utf-8", errors="replace").strip()
        if offset > start_offset and text:
            text = "…\n" + text
        return text

    def _driver_lock_held(self) -> bool:
        path = self.storage_identity.state_dir / "orchestrator-driver.lock"
        try:
            return file_lock_is_held(path)
        except LockUnavailableError:  # pragma: no cover - non-POSIX status fallback
            return False

class DashboardService(RuntimeTopologyDashboardMixin, ProviderPromotionDashboardMixin, ProtectedScopeDashboardMixin, OperatorAcceptanceDashboardMixin, FinalSyncDashboardMixin):
    """Read model state and expose safe dashboard operations."""

    def __init__(
        self,
        *,
        root: Path,
        project_id: str,
        task_id: str,
        state_root: Path | None = None,
        process_controller: OrchestratorProcessController | None = None,
        agent_command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        agent_builder: Callable[..., Any] = build_native_runtime,
        manual_console_manager: Any | None = None,
    ) -> None:
        self.root = root.expanduser().resolve()
        self.project_id = project_id
        self.task_id = task_id
        self.state_root = (
            state_root.expanduser().resolve()
            if state_root is not None
            else xdg_state_home()
        )
        try:
            self.project = load_registered_project(self.root, project_id)
        except (ProjectError, FileNotFoundError) as exc:
            raise GuiError(str(exc)) from exc
        self.task_dir = self.project.directory / "tasks" / task_id
        if not self.task_dir.is_dir():
            raise GuiError(f"task dossier not found: {self.task_dir}")
        skills_dir = self.project.configured_path("skills_dir") or self.project.directory / "skills"
        try:
            self.skill_catalog = SkillCatalog.load(
                project_skills_dir=skills_dir
            )
        except SkillCatalogError as exc:
            raise GuiError(f"invalid workflow skill catalog: {exc}") from exc
        self.storage_identity = resolve_storage_identity(
            self.state_root,
            project_id=self.project_id,
            task_id=self.task_id,
        )
        self.state_dir = self.storage_identity.state_dir
        self.state_path = self.state_dir / "state.json"
        self.log_path = self.state_dir / "orchestrator.log"
        agents_path = self._project_path(
            "agents_file", self.project.directory / "agents.yaml"
        )
        agents_raw = yaml.safe_load(agents_path.read_text(encoding="utf-8")) or {}
        scheduling = agents_raw.get("scheduling") or {}
        if not isinstance(scheduling, dict):
            raise GuiError("scheduling must be a YAML mapping")
        try:
            terminal_policy = InteractiveTerminalPolicy.from_mapping(
                scheduling.get("interactive_console") or {}
            )
        except ValueError as exc:
            raise GuiError(f"invalid scheduling.interactive_console: {exc}") from exc
        self.agent_console = AgentConsoleStore(
            self.state_dir / "agent-console",
            terminal_policy=terminal_policy,
        )
        self.agent_invocations = AgentInvocationStore(
            self.state_dir / "agent-invocations.sqlite3"
        )
        self.supervisor_incidents = SupervisorIncidentStore(
            self.state_dir / "supervisor-incidents.json"
        )
        self.work_package_directives = WorkPackageDirectiveQueue(
            self.state_dir / "work-package-directives.json"
        )
        self.health_store = ProviderHealthStore(self.state_root / "provider-health.json")
        self._init_provider_promotions()
        self.endpoint_cache = EndpointProbeCache()
        self.process = process_controller or OrchestratorProcessController(
            root=self.root,
            project_id=project_id,
            task_id=task_id,
            state_root=self.state_root,
        )
        self.manual_agent_console = manual_console_manager or ManualAgentConsoleManager(
            store=self.agent_console,
            provider_loader=self._agent_configs,
            workdir_loader=self._manual_console_workdir,
            environment_loader=self._command_environment,
        )
        self.task_lifecycle = TaskLifecycleController(
            control_root=self.root,
            state_root=self.state_root,
            project=self.project,
            task_id=self.task_id,
            task_dir=self.task_dir,
        )
        self._config_lock = threading.Lock()
        self._agent_command_runner = agent_command_runner
        self._agent_builder = agent_builder
        self._workspace_action_lock = threading.Lock()
        self._agent_action_lock = threading.Lock()
        self._agent_actions: dict[str, dict[str, Any]] = {}
        self._agent_action_executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="execraft-gui-agent"
        )

    def close(self) -> None:
        self.manual_agent_console.close()
        self.process.close()
        self._agent_action_executor.shutdown(wait=False, cancel_futures=True)

    def snapshot(self) -> dict[str, Any]:
        record = self._load_state()
        run_status = reconcile_run_status(self.process.status(), record)
        packages = record.plan_graph.work_packages if record is not None else []
        pending_directives = self.work_package_directives.effective_pending()
        sync_card_summaries = self.task_lifecycle.repository_sync_card_summaries()
        package_rows = [
            package_with_pending_directives(
                package,
                pending_directives,
                sync_card_summaries.get(package.id),
            )
            for package in packages
        ]
        config_errors: list[str] = []
        try:
            registry = self._opencode_registry()
        except (OpenCodeRegistryError, OSError, ValueError) as exc:
            registry = OpenCodeProviderRegistry()
            config_errors.append(f"model registry: {exc}")
        endpoint_status = self.endpoint_cache.probe(list(registry.endpoints))
        live_invocations = self._running_agent_invocations(run_status)
        assignments = active_assignments(
            packages,
            record.scheduler if record else {},
            live_invocations=live_invocations,
        )
        try:
            agents = self._agent_rows(assignments, registry, endpoint_status)
        except (AgentConfigError, OSError, yaml.YAMLError, ValueError) as exc:
            agents = []
            config_errors.append(f"agent registry: {exc}")
        project_repositories = [
            {
                "id": repository.id,
                "role": repository.role,
                "required": repository.required,
                "workspace_name": repository.workspace_name,
            }
            for repository in self.project.repositories
        ]
        execution_roles = execution_role_metadata()
        execution_lanes = execution_lane_mappings(agents, execution_roles)
        supervisor = self._supervisor_snapshot(agents)
        run_control = self._run_control(record, supervisor)
        execution_contexts = build_execution_contexts(
            record=record,
            assignments=assignments,
            supervisor=supervisor,
            run_control=run_control,
            live_invocations=live_invocations,
        )
        execution_context = build_primary_execution_context(execution_contexts)
        state = record.as_mapping() if record is not None else None
        try:
            token_usage = self.agent_invocations.usage_summary(self.project_id)
        except (OSError, ValueError):
            token_usage = {}
        return {
            "generated_at": _utc_now(),
            "project": {
                "id": self.project.id,
                "description": self.project.description,
                "task_id": self.task_id,
                "repositories": project_repositories,
            },
            "orchestration": state,
            "packages": package_rows,
            "assignments": assignments,
            "live_invocations": live_invocations,
            "agents": agents,
            "execution_lanes": execution_lanes,
            "nodes": agent_nodes(agents, registry, endpoint_status),
            "execution_roles": execution_roles,
            "skills": self.skill_catalog.as_mappings(),
            "run": run_status,
            "supervisor": supervisor,
            "run_control": run_control,
            "execution_context": execution_context,
            "execution_contexts": execution_contexts,
            "token_usage": token_usage,
            "task_lifecycle": self.task_lifecycle.summary(),
            "paths": {
                "state": str(self.state_path),
                "log": str(self.log_path),
                "driver_log": str(run_status["driver_log"]),
                "task": str(self.task_dir),
            },
            "configs": [self._config_metadata(spec) for spec in self.config_specs()],
            "config_errors": config_errors,
        }

    def task_lifecycle_snapshot(self) -> dict[str, Any]:
        """Return the lazy task-definition/replan/completion workbench model."""

        return self.task_lifecycle.snapshot()

    def task_replan_candidate(self, **kwargs: Any) -> dict[str, Any]:
        """Stage a replan candidate through the canonical definition service."""

        return self.task_lifecycle.create_candidate(**kwargs)

    def task_generate_plan(self, *, provider_id: str = "") -> dict[str, Any]:
        """Stage a semantic plan and graph generated from the accepted brief."""

        return self.task_lifecycle.generate_plan_from_brief(provider_id=provider_id)

    def task_replan_candidate_view(self, candidate_id: str) -> dict[str, Any]:
        return self.task_lifecycle.candidate(candidate_id)

    def task_replan_apply(self, candidate_id: str) -> dict[str, Any]:
        return self.task_lifecycle.apply_candidate(candidate_id)

    def task_replan_recover(self) -> dict[str, Any]:
        return self.task_lifecycle.recover_replan()

    def task_complete(self, *, dry_run: bool = False) -> dict[str, Any]:
        return self.task_lifecycle.complete(dry_run=dry_run)

    def task_repository_sync(self, *, refresh: bool = False) -> dict[str, Any]:
        return self.task_lifecycle.repository_sync_snapshot(refresh=refresh)

    def task_repository_sync_options(
        self, package_id: str, *, refresh: bool = False
    ) -> dict[str, Any]:
        result = self.task_lifecycle.repository_sync_options(
            package_id, refresh=refresh
        )
        pending = (
            self.work_package_directives.effective_pending()
            .get(str(package_id).strip(), {})
            .get(PAUSE_FOR_REPOSITORY_SYNC)
        )
        if pending is None:
            return result
        parameters = dict(pending.parameters)
        selected = {str(item) for item in parameters.get("repositories", [])}
        overrides = parameters.get("source_branches", {})
        if not isinstance(overrides, Mapping):
            overrides = {}
        for repository in result.get("repositories", []):
            repository_id = str(repository.get("id", ""))
            repository["selected"] = repository_id in selected
            repository["selected_source_branch"] = str(
                overrides.get(repository_id)
                or repository.get("configured_base_branch", "")
            )
        result["pending_request"] = {
            **parameters,
            "id": pending.id,
            "requested_at": pending.requested_at,
            "reason": pending.reason,
        }
        return result

    def task_repository_sync_preview(
        self,
        *,
        package_id: str,
        repositories: list[str],
        source_branches: Mapping[str, str],
        remote: str = "origin",
    ) -> dict[str, Any]:
        return self.task_lifecycle.repository_sync_preview_selection(
            package_id=package_id,
            repositories=repositories,
            source_branches=source_branches,
            remote=remote,
        )

    def task_repository_sync_request(
        self,
        *,
        package_id: str,
        repositories: list[str],
        source_branches: Mapping[str, str],
        remote: str = "origin",
        conflict_policy: str = "ai_resolve",
        sync_package_id: str = "",
        auto_resume: bool = True,
    ) -> dict[str, Any]:
        """Queue one card-triggered synchronization at a safe package boundary."""

        request = self.task_lifecycle.prepare_repository_sync_card_request(
            package_id=package_id,
            repositories=repositories,
            source_branches=source_branches,
            remote=remote,
            conflict_policy=conflict_policy,
            sync_package_id=sync_package_id,
            auto_resume=auto_resume,
        )
        command = self.work_package_directives.enqueue(
            package_id=request.package_id,
            kind=PAUSE_FOR_REPOSITORY_SYNC,
            enabled=True,
            reason=(
                "Pause & Sync requested from Work Package card; synchronize at the next "
                "safe Work Package boundary"
            ),
            requested_by="dashboard",
            parameters=request.as_parameters(),
        )
        run_status = self.process.status()
        return {
            **command.as_mapping(),
            "mode": request.mode,
            "auto_resume": request.auto_resume,
            "queued_while_running": bool(
                run_status["external_running"] or run_status["owned_running"]
            ),
            "run": run_status,
        }

    def task_repository_sync_before(self, **kwargs: Any) -> dict[str, Any]:
        return self.task_lifecycle.create_repository_sync_before(**kwargs)

    def task_repository_sync_rollback(self, package_id: str) -> dict[str, Any]:
        return self._task_repository_sync_cli_action(
            package_id, flag="--rollback", action="roll back repository synchronization"
        )

    def task_repository_sync_accept_resolution(
        self, package_id: str
    ) -> dict[str, Any]:
        return self._task_repository_sync_cli_action(
            package_id,
            flag="--accept-resolution",
            action="accept repository synchronization resolution",
        )

    def _task_repository_sync_cli_action(
        self, package_id: str, *, flag: str, action: str
    ) -> dict[str, Any]:
        """Run a sync recovery action through the canonical orchestrator CLI."""

        package = str(package_id).strip()
        if not package:
            raise GuiError("package_id is required")
        run_status = self.process.status()
        if run_status["external_running"] or run_status["owned_running"]:
            raise GuiError(f"stop the orchestrator before attempting to {action}")
        command = [
            sys.executable,
            "-m",
            "execraft.cli",
            "orchestrate",
            "sync",
            "--project",
            self.project_id,
            "--task-id",
            self.task_id,
            "--state-dir",
            str(self.state_root),
            "--package-id",
            package,
            flag,
        ]
        with self._exclusive_workspace_action(action):
            result = subprocess.run(
                command,
                cwd=self.root,
                env=self._command_environment(),
                text=True,
                capture_output=True,
                check=False,
            )
        if result.returncode != 0:
            raise GuiError(
                result.stderr.strip()
                or result.stdout.strip()
                or f"{action} failed"
            )
        try:
            payload = json.loads(result.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise GuiError(f"{action} returned invalid JSON") from exc
        if not isinstance(payload, Mapping):
            raise GuiError(f"{action} returned an invalid payload")
        return {
            "repository_sync_action": dict(payload),
            "lifecycle": self.task_lifecycle.snapshot(),
        }

    def execution_trace(self, package_id: str) -> dict[str, Any]:
        """Return the durable stage-attempt graph for one Work Package.

        Traces are loaded lazily by the Work Package dialog so normal dashboard
        polling remains lightweight. Parent Work Packages include recursively
        generated shards as separate swimlanes; selecting a shard stays focused
        on that package.
        """

        package_id = str(package_id).strip()
        if not package_id:
            raise GuiError("package_id is required")
        record = self._load_state()
        if record is None:
            raise GuiError("orchestration state is not initialized")
        packages = record.plan_graph.work_packages
        try:
            selected = record.plan_graph.package_by_id(package_id)
        except OrchestrateError as exc:
            raise GuiError(str(exc)) from exc
        scope = trace_scope_packages(selected, packages)
        invocations = self.agent_invocations.list_for_packages(
            self.project_id,
            [package.id for package in scope],
            limit=5000,
            newest_first=False,
        )
        warnings: list[str] = []
        try:
            entries = EventJournal(self.storage_identity.journal_path).read()
        except (OSError, OrchestrateError, ValueError) as exc:
            entries = []
            warnings.append(f"event journal unavailable: {exc}")
        return build_execution_trace(
            selected=selected,
            packages=packages,
            invocations=invocations,
            journal_entries=entries,
            warnings=warnings,
        )

    def read_agent_console(
        self,
        agent_id: str,
        *,
        session_id: str = "",
        preferred_package_id: str = "",
        preferred_stage: str = "",
        offset: int = 0,
        limit: int = 128_000,
        include_sessions: bool = True,
        include_events: bool = True,
        interaction_offset: int = 0,
        include_interactions: bool = True,
        since_version: int = -1,
        wait_seconds: float = 0.0,
        terminal_screen_token: str = "",
    ) -> dict[str, Any]:
        """Return session metadata and an incremental read-only output stream."""

        agent_id = str(agent_id).strip()
        configured = {item.provider_id for item in self._agent_configs()}
        if agent_id not in configured:
            raise GuiError(f"unknown configured provider: {agent_id}")
        selected = str(session_id).strip()
        sessions = (
            self.agent_console.sessions(agent_id)
            if include_sessions or not selected
            else []
        )
        if not selected and sessions:
            preferred_package_id = str(preferred_package_id).strip()
            preferred_stage = str(preferred_stage).strip()

            def matches_preference(item: Mapping[str, Any]) -> bool:
                return (
                    (not preferred_package_id or item.get("package_id") == preferred_package_id)
                    and (not preferred_stage or item.get("stage") == preferred_stage)
                )

            preferred = [item for item in sessions if matches_preference(item)]
            preferred_running = next(
                (item for item in preferred if item.get("status") == "running"), None
            )
            any_running = next(
                (item for item in sessions if item.get("status") == "running"), None
            )
            selected = str(
                (preferred_running or (preferred[0] if preferred else None) or any_running or sessions[0]).get(
                    "session_id", ""
                )
            )
        if not selected:
            return {
                "agent_id": agent_id,
                "sessions": sessions,
                "selected_session_id": "",
                "metadata": {},
                "events": [],
                "events_included": bool(include_events),
                "interactions": [],
                "interactions_included": bool(include_interactions),
                "interaction_offset": 0,
                "interaction_next_offset": 0,
                "interaction_size_bytes": 0,
                "interaction_reset": False,
                "version": 0,
                "offset": 0,
                "next_offset": 0,
                "size_bytes": 0,
                "reset": False,
                "manual_console": self._manual_console_status(agent_id),
                "sessions_included": True,
                "terminal_screen_token": "",
                "terminal_screen_unchanged": False,
            }
        try:
            result = self.agent_console.read_events(
                selected,
                offset=offset,
                limit=limit,
                terminal_screen_token=terminal_screen_token,
                include_events=include_events,
                interaction_offset=interaction_offset,
                include_interactions=include_interactions,
                since_version=since_version,
                wait_seconds=wait_seconds,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise GuiError(str(exc)) from exc
        if result["metadata"].get("agent_id") != agent_id:
            raise GuiError("agent console session does not belong to the requested provider")
        return {
            "agent_id": agent_id,
            "sessions": sessions,
            "selected_session_id": selected,
            "manual_console": self._manual_console_status(agent_id),
            "sessions_included": bool(include_sessions or not session_id),
            **result,
        }

    def start_manual_agent_console(
        self, agent_id: str, *, acknowledged: bool = False
    ) -> dict[str, Any]:
        """Start a configured provider CLI in a standalone task-workspace PTY."""

        if not acknowledged:
            raise GuiError(
                "starting a standalone agent console requires explicit operator acknowledgement"
            )
        if not self._workspace_action_lock.acquire(blocking=False):
            raise GuiError(
                "cannot start a standalone agent console while another workspace action is in progress"
            )
        try:
            run_status = self.process.status()
            if run_status["owned_running"] or run_status["external_running"]:
                raise GuiError(
                    "stop the orchestrator before starting a standalone agent console"
                )
            try:
                return self.manual_agent_console.start(agent_id)
            except (ManualAgentConsoleError, OSError, subprocess.SubprocessError) as exc:
                raise GuiError(str(exc)) from exc
        finally:
            self._workspace_action_lock.release()

    def stop_manual_agent_console(
        self, *, agent_id: str = "", session_id: str = ""
    ) -> dict[str, Any]:
        """Stop one dashboard-owned standalone provider PTY."""

        try:
            return self.manual_agent_console.stop(
                agent_id=agent_id, session_id=session_id
            )
        except (ManualAgentConsoleError, OSError, subprocess.SubprocessError) as exc:
            raise GuiError(str(exc)) from exc

    def write_agent_console(
        self,
        *,
        session_id: str,
        action: str,
        data: str = "",
        rows: int | None = None,
        columns: int | None = None,
        signal_name: str = "",
        acknowledged: bool = False,
    ) -> dict[str, Any]:
        """Queue one acknowledged operator control for an active agent session."""

        if not acknowledged:
            raise GuiError(
                "interactive agent control requires explicit operator acknowledgement"
            )
        try:
            result = self.agent_console.queue_control_event(
                session_id,
                action=action,
                data=data,
                rows=rows,
                columns=columns,
                signal_name=signal_name,
            )
            result["delivered_immediately"] = bool(
                self.manual_agent_console.dispatch_pending(session_id)
            )
            return result
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise GuiError(str(exc)) from exc

    def read_agent_artifact(self, path: str) -> dict[str, Any]:
        """Read a bounded artifact preview from the current task state root."""

        try:
            return self.agent_console.read_artifact(path)
        except (OSError, ValueError) as exc:
            raise GuiError(str(exc)) from exc

    def list_tasks(self) -> list[dict[str, Any]]:
        """Return active tasks only, enriched with their durable runtime state."""

        tasks_root = self.project.directory / "tasks"
        rows: list[dict[str, Any]] = []
        for directory in sorted(tasks_root.iterdir()) if tasks_root.is_dir() else []:
            manifest_path = directory / "TASK.yaml"
            if not directory.is_dir() or not manifest_path.is_file():
                continue
            try:
                manifest = (
                    yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
                )
            except (OSError, yaml.YAMLError):
                manifest = {}
            identity = resolve_storage_identity(
                self.state_root,
                project_id=self.project_id,
                task_id=directory.name,
                create=False,
            )
            state_path = identity.state_dir / "state.json"
            runtime_state = ""
            if state_path.is_file():
                try:
                    raw_state = json.loads(state_path.read_text(encoding="utf-8"))
                    if isinstance(raw_state, Mapping):
                        runtime_state = str(raw_state.get("state", ""))
                except (OSError, ValueError):
                    runtime_state = "unreadable"
            rows.append(
                {
                    "id": directory.name,
                    "title": str(manifest.get("title", "")).strip() or directory.name,
                    "status": str(manifest.get("status", "")).strip() or "active",
                    "current": directory.name == self.task_id,
                    "has_state": state_path.is_file(),
                    "state": runtime_state,
                    "state_modified_at": (
                        datetime.fromtimestamp(
                            state_path.stat().st_mtime, timezone.utc
                        ).isoformat()
                        if state_path.is_file()
                        else ""
                    ),
                }
            )
        return rows

    def set_work_package_directive(
        self,
        package_id: str,
        *,
        kind: str,
        enabled: bool,
        reason: str = "",
    ) -> dict[str, Any]:
        """Queue a future Work Package directive without racing the active driver."""

        package_id = str(package_id).strip()
        kind = str(kind).strip()
        if not package_id:
            raise GuiError("package_id is required")
        if kind not in WORK_PACKAGE_DIRECTIVE_KINDS:
            raise GuiError(f"unsupported Work Package directive: {kind!r}")
        record = self._load_state()
        if record is None:
            raise GuiError("orchestration state is not initialized")
        try:
            package = record.plan_graph.package_by_id(package_id)
        except OrchestrateError as exc:
            raise GuiError(str(exc)) from exc
        if package.stage == WorkPackageStage.COMPLETED:
            raise GuiError("completed Work Packages cannot be changed")
        if enabled and (
            package.stage != WorkPackageStage.PREPARE or package.status != "pending"
        ):
            raise GuiError(
                "future Work Package directives require an unstarted queued Work Package"
            )
        if kind == REQUIRE_DECOMPOSITION:
            if package.parent_id or package.execution_mode != "standard":
                raise GuiError(
                    "mandatory decomposition is available only for top-level standard Work Packages"
                )
            if package.decomposition_status or package.stage == WorkPackageStage.DECOMPOSE:
                raise GuiError("decomposition has already started for this Work Package")
        command = self.work_package_directives.enqueue(
            package_id=package_id,
            kind=kind,
            enabled=bool(enabled),
            reason=str(reason).strip(),
            requested_by="dashboard",
        )
        run_status = self.process.status()
        return {
            **command.as_mapping(),
            "queued_while_running": bool(
                run_status["external_running"] or run_status["owned_running"]
            ),
        }

    def set_package_pause(
        self,
        package_id: str,
        *,
        paused: bool,
        reason: str = "",
        apply_to_shards: bool = False,
    ) -> dict[str, Any]:
        """Set or clear a Work Package scheduling hold through the canonical CLI."""

        package_id = str(package_id).strip()
        if not package_id:
            raise GuiError("package_id is required")
        run_status = self.process.status()
        if run_status["external_running"] or run_status["owned_running"]:
            raise GuiError("stop the orchestrator before changing a Work Package pause")
        command = [
            sys.executable,
            "-m",
            "execraft.cli",
            "orchestrate",
            "pause",
            "--project",
            self.project_id,
            "--task-id",
            self.task_id,
            "--state-dir",
            str(self.state_root),
            "--package-id",
            package_id,
        ]
        if not paused:
            command.append("--resume")
        if str(reason).strip():
            command.extend(["--pause-reason", str(reason).strip()])
        if apply_to_shards:
            command.append("--apply-to-shards")
        with self._exclusive_workspace_action("change Work Package pause"):
            result = subprocess.run(
                command,
                cwd=self.root,
                env=self._command_environment(),
                text=True,
                capture_output=True,
                check=False,
            )
        if result.returncode != 0:
            raise GuiError(
                result.stderr.strip()
                or result.stdout.strip()
                or "changing Work Package pause failed"
            )
        return {
            "package_id": package_id,
            "paused": bool(paused),
            "stdout": result.stdout,
            "stderr": result.stderr,
        }

    def read_log(
        self,
        offset: int = 0,
        limit: int = 256_000,
        *,
        source: str = "orchestrator",
    ) -> dict[str, Any]:
        if offset < 0:
            raise GuiError("log offset cannot be negative")
        limit = max(1, min(1_000_000, int(limit)))
        paths = {
            "orchestrator": self.log_path,
            "driver": Path(self.process.status()["driver_log"]),
        }
        if source not in paths:
            raise GuiError(f"unsupported log source: {source}")
        path = paths[source]
        if not path.is_file():
            return {
                "source": source,
                "path": str(path),
                "offset": 0,
                "next_offset": 0,
                "content": "",
                "truncated": False,
            }
        size = path.stat().st_size
        if offset > size:
            offset = 0
        with path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read(limit)
        return {
            "source": source,
            "path": str(path),
            "offset": offset,
            "next_offset": offset + len(data),
            "content": data.decode("utf-8", errors="replace"),
            "truncated": offset + len(data) < size,
        }

    def workspace_snapshot(self) -> dict[str, Any]:
        status = self.process.status()
        return self._workspace_snapshot(
            driver_active=bool(status["owned_running"] or status["external_running"])
        )

    def workspace_diff(self, repository_id: str, path: str) -> dict[str, Any]:
        try:
            return self._workspace_manager().file_diff(repository_id, path)
        except (TaskGitError, WorkspaceChangeError, OSError) as exc:
            raise GuiError(str(exc)) from exc

    def generate_commit_message(
        self,
        *,
        selections: Mapping[str, list[str]],
        expected_digests: Mapping[str, str],
        agent_id: str,
    ) -> dict[str, Any]:
        with self._exclusive_workspace_action("generate an AI commit message"):
            provider = next(
                (item for item in self._agent_configs() if item.provider_id == agent_id),
                None,
            )
            if provider is None or not provider.enabled:
                raise GuiError(f"unknown or disabled commit-message agent: {agent_id}")
            health = self.health_store.get(provider.provider_id)
            if not health.is_available:
                detail = health.reason or health.status
                raise GuiError(
                    f"agent {provider.provider_id} is unavailable for commit-message generation: {detail}"
                )
            try:
                return self._workspace_manager().generate_commit_message(
                    selections=selections,
                    expected_digests=expected_digests,
                    provider=provider,
                )
            except (TaskGitError, WorkspaceChangeError, OSError, ValueError) as exc:
                raise GuiError(str(exc)) from exc

    def delete_workspace_files(
        self,
        *,
        selections: Mapping[str, list[str]],
        expected_digests: Mapping[str, str],
    ) -> dict[str, Any]:
        with self._exclusive_workspace_action("delete untracked workspace files"):
            try:
                return self._workspace_manager().delete_untracked(
                    selections=selections,
                    expected_digests=expected_digests,
                )
            except (TaskGitError, WorkspaceChangeError, OSError) as exc:
                raise GuiError(str(exc)) from exc

    def commit_workspace_changes(
        self,
        *,
        selections: Mapping[str, list[str]],
        expected_digests: Mapping[str, str],
        subject: str,
        body: str,
        reviewed: bool,
    ) -> dict[str, Any]:
        with self._exclusive_workspace_action("commit workspace changes"):
            self.reject_protected_scope_commit_bypass(selections)
            try:
                return self._workspace_manager().commit(
                    selections=selections,
                    expected_digests=expected_digests,
                    subject=subject,
                    body=body,
                    reviewed=reviewed,
                )
            except (TaskGitError, WorkspaceChangeError, OSError) as exc:
                raise GuiError(str(exc)) from exc

    def _workspace_snapshot(self, *, driver_active: bool) -> dict[str, Any]:
        try:
            snapshot = self._workspace_manager().snapshot(driver_active=driver_active)
        except (TaskGitError, WorkspaceChangeError, OSError) as exc:
            return {
                "available": False,
                "repositories": [],
                "dirty_repository_count": 0,
                "changed_file_count": 0,
                "driver_active": driver_active,
                "commit_allowed": False,
                "reason": str(exc),
                "review_agents": [],
            }
        try:
            # Runtime-neutral: a reviewer on any configured runtime is selectable.
            review_agents = review_capable_candidates(self, self.project_id)
        except (GuiError, AgentConfigError, OSError, yaml.YAMLError, ValueError):
            review_agents = []
        snapshot["review_agents"] = review_agents
        return snapshot

    def _workspace_manager(self) -> WorkspaceChangeManager:
        workspace = load_workspace(self.root, self.task_id)
        raw = _parse_yaml_mapping(
            (self.task_dir / "TASK.yaml").read_text(encoding="utf-8"),
            label="TASK.yaml",
        )
        manifest = TaskManifest.from_mapping(raw)
        expected_branches = {
            item.id: item.task_branch for item in manifest.repositories
        }
        return WorkspaceChangeManager(
            workspace=workspace,
            expected_branches=expected_branches,
            state_dir=self.state_dir,
            agent_builder=self._agent_builder,
            skill_catalog=self.skill_catalog,
        )

    @contextmanager
    def _exclusive_workspace_action(self, action: str):
        """Serialize GUI Git actions and exclude foreground/daemon drivers."""

        if not self._workspace_action_lock.acquire(blocking=False):
            raise GuiError("another workspace or run action is already in progress")
        try:
            status = self.process.status()
            if status["owned_running"] or status["external_running"]:
                raise GuiError(f"cannot {action} while an orchestrator driver is active")
            lock_path = self.state_dir / "orchestrator-driver.lock"
            try:
                with FileLock(lock_path, level=LockLevel.DRIVER, timeout=0.0):
                    yield
            except LockBusyError as exc:
                raise GuiError(
                    f"cannot {action} while an orchestrator driver is active"
                ) from exc
            except LockUnavailableError:
                # Keep the historical non-POSIX fallback: the in-process action
                # mutex still serializes GUI mutations when advisory locks are absent.
                yield
        finally:
            self._workspace_action_lock.release()

    def config_specs(self) -> list[ConfigFileSpec]:
        project_dir = self.project.directory
        opencode_dir = self.project.configured_path("opencode_dir") or project_dir / "opencode"
        candidates = [
            ConfigFileSpec(
                "agents",
                "Agents & scheduling",
                self._project_path("agents_file", project_dir / "agents.yaml"),
                self._validate_agents,
            ),
            ConfigFileSpec(
                "providers",
                "OpenCode endpoints & satellite models",
                opencode_dir / "providers.yaml",
                self._validate_providers,
            ),
            ConfigFileSpec(
                "resources",
                "Resource policy",
                self._project_path("resources_file", project_dir / "resources.yaml"),
                _validate_yaml_mapping,
            ),
            ConfigFileSpec(
                "verification",
                "Verification registry",
                self._project_path(
                    "verification_file", project_dir / "verification.yaml"
                ),
                _validate_yaml_mapping,
            ),
            ConfigFileSpec(
                "project",
                "Project descriptor",
                project_dir / "project.yaml",
                self._validate_project,
                requires_idle=True,
            ),
            ConfigFileSpec(
                "task",
                "Task manifest",
                self.task_dir / "TASK.yaml",
                self._validate_task,
                requires_idle=True,
            ),
        ]
        plan = self.task_dir / "PLAN.graph.yaml"
        if plan.is_file():
            candidates.append(
                ConfigFileSpec(
                    "plan",
                    "Executable plan graph",
                    plan,
                    self._validate_plan,
                    requires_idle=True,
                )
            )
        return [item for item in candidates if item.path.is_file()]

    def read_config(self, key: str) -> dict[str, Any]:
        spec = self._config_spec(key)
        content = spec.path.read_text(encoding="utf-8")
        return {
            **self._config_metadata(spec),
            "content": content,
            "sha256": _sha256_text(content),
        }

    def validate_config(self, key: str, content: str) -> dict[str, Any]:
        spec = self._config_spec(key)
        try:
            spec.validator(content)
        except Exception as exc:
            return {"valid": False, "error": str(exc)}
        return {"valid": True, "error": ""}

    def save_config(self, key: str, content: str, expected_sha256: str) -> dict[str, Any]:
        spec = self._config_spec(key)
        run_status = self.process.status()
        if spec.requires_idle and (run_status["external_running"] or run_status["owned_running"]):
            raise GuiError(f"{spec.label} cannot be changed while an orchestrator driver is active")
        spec.validator(content)
        with self._config_lock:
            current = spec.path.read_text(encoding="utf-8")
            current_sha = _sha256_text(current)
            if not expected_sha256:
                raise GuiError("a file digest is required before saving")
            if current_sha != expected_sha256:
                raise GuiError(
                    "the file changed on disk after it was opened; reload before saving"
                )
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            relative = spec.path.relative_to(self.root)
            backup = self.state_root / "config-backups" / timestamp / relative
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(spec.path, backup)
            atomic_write_text(spec.path, content)
        return {
            **self.read_config(key),
            "backup": str(backup),
            "message": "saved; running orchestrators keep their current in-memory configuration",
        }

    def start_run(self, *, no_wait_for_agents: bool = False) -> dict[str, Any]:
        if not self._workspace_action_lock.acquire(blocking=False):
            raise GuiError("cannot start orchestration while a workspace action is in progress")
        try:
            if self.manual_agent_console.any_running():
                raise GuiError(
                    "stop standalone agent consoles before starting orchestration"
                )
            record = self._load_state()
            control = self._run_control(record, self._supervisor_snapshot([]))
            if not control["can_start"]:
                raise GuiError(str(control["reason"]))
            if record is None:
                return self.process.initialize(self.task_dir / "PLAN.graph.yaml")
            return self.process.start(no_wait_for_agents=no_wait_for_agents)
        finally:
            self._workspace_action_lock.release()

    def stop_run(self) -> dict[str, Any]:
        return self.process.stop()

    def _run_control(
        self,
        record: TaskExecutionStateRecord | None,
        supervisor: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Describe whether Run / Resume is valid before spawning the CLI.

        The CLI remains authoritative and performs the same checks again.  This
        projection prevents the dashboard from offering a button that is known
        to fail immediately, especially for terminal states and human decisions
        that require an explicit operator action before resuming.
        """

        if record is None:
            lifecycle = self.task_lifecycle.summary()
            definition = lifecycle.get("definition") or {}
            plan_file = self.task_dir / "PLAN.graph.yaml"
            if not bool(definition.get("integrity_ok")):
                changed = [
                    *definition.get("changed_documents", []),
                    *definition.get("unexpected_documents", []),
                    *definition.get("missing_documents", []),
                ]
                detail = ", ".join(dict.fromkeys(str(item) for item in changed))
                return {
                    "can_start": False,
                    "label": "Initialize plan",
                    "reason": (
                        "Adopt and apply the current task definition before initialization"
                        + (f": {detail}" if detail else ".")
                    ),
                    "human_action": {},
                }
            if not plan_file.is_file():
                return {
                    "can_start": False,
                    "label": "Initialize plan",
                    "reason": f"Accepted plan graph is missing: {plan_file}",
                    "human_action": {},
                }
            return {
                "can_start": True,
                "label": "Initialize plan",
                "reason": "Create durable orchestration state from the accepted plan graph.",
                "human_action": {},
            }

        state = record.state.value
        supervisor = dict(supervisor or {})
        incomplete = [
            package
            for package in record.plan_graph.work_packages
            if package.stage != WorkPackageStage.COMPLETED
        ]
        paused = [package for package in incomplete if package.operator_paused]
        if state == "operator_paused":
            waiting = dict(record.waiting or {})
            package_id = str(waiting.get("package_id", "")).strip()
            reason = str(waiting.get("reason", "")).strip()
            return {
                "can_start": True,
                "label": (
                    f"Resume {package_id}" if package_id else "Resume planned pause"
                ),
                "reason": reason,
                "human_action": {},
            }
        resumable_states = {
            "validating_plan",
            "running",
            "waiting_for_agent",
            "supervising",
        }
        if (
            state in resumable_states
            and incomplete
            and len(paused) == len(incomplete)
        ):
            names = ", ".join(package.id for package in paused[:5])
            suffix = "…" if len(paused) > 5 else ""
            return {
                "can_start": False,
                "label": "All Work Packages paused",
                "reason": (
                    "Resume at least one Work Package before starting the orchestrator. "
                    f"Paused: {names}{suffix}"
                ),
                "human_action": {},
            }
        if state in resumable_states:
            label = (
                "Run"
                if state == "validating_plan"
                else "Resume supervision"
                if state == "supervising"
                else "Run / Resume"
            )
            return {
                "can_start": True,
                "label": label,
                "reason": "",
                "human_action": {},
            }

        if state == "waiting_for_human_decision":
            incident = supervisor.get("incident") or {}
            question = incident.get("human_question") or {}
            transport_recoverable = bool(
                supervisor.get("auto_resume_transport_failure")
            )
            delegation_recoverable = bool(
                supervisor.get("auto_resume_lost_delegation")
            )
            deterministic = supervisor.get("deterministic_recovery") or {}
            deterministic_recoverable = bool(deterministic.get("available"))
            auto_resume = (
                deterministic_recoverable
                or transport_recoverable
                or delegation_recoverable
            )
            return {
                "can_start": auto_resume,
                "label": (
                    "Repair review findings directly"
                    if deterministic_recoverable
                    else "Resume delegated recovery"
                    if delegation_recoverable
                    else "Retry Supervisor transport"
                    if transport_recoverable
                    else "Answer Supervisor"
                ),
                "reason": (
                    "The final reviewer already recorded exact blocking findings. "
                    "Run will bypass broad supervision, queue a bounded fix_review "
                    "campaign, prefer a non-Supervisor provider, and rerun verification "
                    "plus independent final review."
                    if deterministic_recoverable
                    else "A previous execution-agent wait lost the in-memory delegation queue. "
                    "The journaled delegated task can now resume on the next healthy agent."
                    if delegation_recoverable
                    else "The previous Supervisor launch exceeded the operating-system "
                    "argument limit. The repaired stdin transport can retry it automatically."
                    if transport_recoverable
                    else str(
                        question.get("question")
                        or "The Supervisor needs an operator decision before it can continue."
                    )
                ),
                "human_action": {},
                "supervisor_question": question,
            }

        if state == "human_required":
            action = self._latest_human_action()
            protected_control = self.protected_scope_run_control(action)
            if protected_control:
                return protected_control
            operator_acceptance = self._operator_acceptance_offer(action)
            auto_resume = is_auto_resumable_agent_wait(action)
            packages = {
                package.id: package for package in record.plan_graph.work_packages
            }
            stale_scope = is_completed_repository_scope_action(action, packages)
            recoverable_scope = (
                is_repository_scope_action(action)
                and self._automatic_workspace_recovery_enabled()
            )
            verified_noop = self._is_verified_noop_commit_action(action)
            reason = str(action.get("reason", "")).strip()
            recommended = str(action.get("recommended_decision", "")).strip()
            if auto_resume:
                return {
                    "can_start": True,
                    "label": "Resume execution-agent wait",
                    "reason": reason,
                    "human_action": action,
                }
            if stale_scope:
                return {
                    "can_start": True,
                    "label": "Reconcile / Resume",
                    "reason": (
                        "The recorded scope check points to a completed package. "
                        "Run will re-check workspace cleanliness and clear it only "
                        "when the original condition is gone."
                    ),
                    "human_action": action,
                }
            if recoverable_scope:
                return {
                    "can_start": True,
                    "label": "Recover workspace / Resume",
                    "reason": (
                        "Automatic workspace recovery is enabled. Run removes safe "
                        "untracked artifacts, asks a reviewer/fixer to repair or retain "
                        "the complete cross-repository delta, then revalidates and "
                        "commits through the orchestrator."
                    ),
                    "human_action": action,
                }
            if verified_noop:
                return {
                    "can_start": True,
                    "label": "Resume verified no-op",
                    "reason": (
                        "The Work Package passed its checks and scope recovery left the "
                        "affected repositories clean. Run will record an explicit "
                        "no-op transaction and continue."
                    ),
                    "human_action": action,
                }
            deterministic = supervisor.get("deterministic_recovery") or {}
            if deterministic.get("available"):
                return {
                    "can_start": True,
                    "label": "Repair review findings directly",
                    "reason": (
                        "The final reviewer already supplied exact blocking findings. "
                        "Run will skip model-driven supervision, queue a bounded direct "
                        "fixer, and repeat verification and independent final review."
                    ),
                    "human_action": action,
                    "operator_acceptance": operator_acceptance,
                }
            policy = supervisor.get("policy") or {}
            trigger_classes = {
                str(item)
                for item in policy.get("trigger_classes", [])
                if str(item).strip()
            }
            classification = classify_incident(action).value if action else ""
            if (
                action.get("supervisor_eligible") is not False
                and supervisor.get("enabled")
                and supervisor.get("available")
                and classification in trigger_classes
            ):
                return {
                    "can_start": True,
                    "label": "Supervisor recover / Resume",
                    "reason": (
                        "The configured Supervisor will diagnose this incident, "
                        "repair or delegate the required work, verify progress, and "
                        "ask a plain-language question only if a product or external "
                        "decision cannot be inferred safely."
                    ),
                    "human_action": action,
                    "operator_acceptance": operator_acceptance,
                }
            detail = recommended or reason or (
                "Resolve the recorded human-required decision before resuming."
            )
            return {
                "can_start": False,
                "label": "Action required",
                "reason": detail,
                "human_action": action,
                "operator_acceptance": operator_acceptance,
            }

        messages = {
            "completed": "The orchestration is already completed.",
            "failed": "The orchestration is failed and cannot be resumed directly.",
            "cancelled": "The orchestration is cancelled and cannot be resumed directly.",
            "paused_low_disk": "Free disk space and recover the project state before resuming.",
            "waiting_for_environment": "Resolve the environment wait before resuming.",
            "resource_maintenance": "Resource maintenance is in progress.",
            "recovering": "Recovery is already in progress.",
            "final_validation": "Final validation is already in progress.",
            "initializing": "Finish orchestration initialization before running.",
        }
        return {
            "can_start": False,
            "label": "Run",
            "reason": messages.get(state, f"Cannot run from orchestration state {state}."),
            "human_action": {},
        }

    def _is_verified_noop_commit_action(self, action: Mapping[str, Any]) -> bool:
        if not self._verified_noop_commit_enabled():
            return False
        if str(action.get("stage", "")).strip() != "commit":
            return False
        evidence = " ".join(str(item) for item in action.get("evidence", [])).lower()
        return "no changes detected in affected repositories" in evidence

    def _verified_noop_commit_enabled(self) -> bool:
        agents_path = self._project_path(
            "agents_file", self.project.directory / "agents.yaml"
        )
        try:
            raw = yaml.safe_load(agents_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return False
        commit = raw.get("commit") or {}
        return isinstance(commit, Mapping) and commit.get("allow_verified_noop") is True

    def _automatic_workspace_recovery_enabled(self) -> bool:
        """Return whether unattended workspace recovery is enabled."""

        agents_path = self._project_path(
            "agents_file", self.project.directory / "agents.yaml"
        )
        try:
            raw = yaml.safe_load(agents_path.read_text(encoding="utf-8")) or {}
            scheduling = raw.get("scheduling") or {}
            policy = scope_recovery_policy_from_scheduling(scheduling)
        except (OSError, yaml.YAMLError, ValueError, AttributeError):
            return False
        return policy.enabled and policy.auto_resume

    def _recovery_playbook_policy(self):
        agents_path = self._project_path(
            "agents_file", self.project.directory / "agents.yaml"
        )
        try:
            raw = yaml.safe_load(agents_path.read_text(encoding="utf-8")) or {}
            scheduling = raw.get("scheduling") or {}
            return recovery_playbook_policy_from_scheduling(scheduling)
        except (OSError, yaml.YAMLError, ValueError, AttributeError):
            return recovery_playbook_policy_from_scheduling({
                "recovery_playbooks": {"enabled": False}
            })

    def _review_recovery_snapshot(
        self,
        record: TaskExecutionStateRecord | None,
        supervisor: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Project deterministic exhausted-review recovery into the GUI."""

        if record is None:
            return {"available": False}
        policy = self._recovery_playbook_policy()
        review_policy = policy.review_exhausted
        if not policy.enabled or not review_policy.enabled:
            return {"available": False, "policy": review_policy.as_mapping()}
        supervisor = dict(supervisor or {})
        incident = supervisor.get("incident") or {}
        action = self._latest_human_action()
        classification = str(incident.get("classification", ""))
        if not classification and action:
            classification = classify_incident(action).value
        if classification != IncidentClass.REVIEW_EXHAUSTED.value:
            return {"available": False, "policy": review_policy.as_mapping()}
        package_id = str(
            incident.get("package_id") or action.get("package_id") or ""
        ).strip()
        try:
            package = record.plan_graph.package_by_id(package_id)
        except OrchestrateError:
            return {"available": False, "policy": review_policy.as_mapping()}
        findings = list(package.review_findings)
        if not findings:
            raw = action.get("evidence") or []
            findings = [str(item) for item in raw] if isinstance(raw, list) else []
        available = bool(
            record.state
            in {
                TaskExecutionState.HUMAN_REQUIRED,
                TaskExecutionState.SUPERVISING,
                TaskExecutionState.WAITING_FOR_AGENT,
                TaskExecutionState.WAITING_FOR_HUMAN_DECISION,
            }
            and package.stage
            in {
                WorkPackageStage.REVIEW,
                WorkPackageStage.FINAL_REVIEW,
                WorkPackageStage.FULL_VERIFY,
                WorkPackageStage.FIX_REVIEW,
            }
            and findings
            and len(findings) <= review_policy.max_findings
            and package.review_recovery_cycles < review_policy.max_rescue_cycles
        )
        return {
            "available": available,
            "kind": "review_exhausted",
            "package_id": package.id,
            "finding_count": min(len(findings), review_policy.max_findings),
            "cycle": package.review_recovery_cycles + 1,
            "max_cycles": review_policy.max_rescue_cycles,
            "avoid_supervisor_agent": review_policy.prefer_non_supervisor_fixer,
            "allow_supervisor_fallback": review_policy.allow_supervisor_fallback,
            "policy": review_policy.as_mapping(),
        }

    @staticmethod
    def _validated_supervisor_policy(
        raw: Mapping[str, Any],
        *,
        providers: list[Any] | None = None,
    ) -> SupervisorPolicy:
        try:
            policy = SupervisorPolicy.from_mapping(raw.get("supervisor"))
        except SupervisorConfigError as exc:
            raise GuiError(f"invalid supervisor policy: {exc}") from exc
        if providers is not None:
            available = providers
        elif int(raw.get("schema_version", 1)) >= 4:
            # Supervisor execution is still a Native-only compatibility surface,
            # but unrelated OpenClaw profiles must not make a mixed v4 project
            # invalid. Project only the Native supervisor candidates.
            execution = parse_execution_config(dict(raw), include_disabled=True)
            available = [
                project_native_profile_legacy_config(execution, profile)
                for profile in execution.agents
                if execution.runtime(profile.runtime_id).kind == RuntimeKind.NATIVE
            ]
        else:
            available = project_native_agent_configs(dict(raw))[0]
        if not policy.enabled or not policy.agent_ids:
            return policy
        for agent_id in policy.agent_ids:
            selected = next(
                (item for item in available if item.provider_id == agent_id),
                None,
            )
            if selected is None:
                raise GuiError(
                    f"supervisor.agents references an unknown Native agent: {agent_id}"
                )
            if selected.adapter not in {
                "codex",
                "claude",
                "claude-code",
                "antigravity",
                "antigravity-cli",
            }:
                raise GuiError(
                    "supervisor.agents must use codex, claude-code, or antigravity-cli adapters"
                )
            if AgentCapability.SUPERVISE not in selected.capabilities:
                raise GuiError(
                    "every supervisor.agents entry must declare the supervise "
                    f"capability: {agent_id}"
                )
        return policy

    def _validate_supervisor_skill(self, policy: SupervisorPolicy) -> None:
        if not policy.enabled:
            return
        try:
            self.skill_catalog.materialize("supervise", [policy.skill_id])
        except SkillCatalogError as exc:
            raise GuiError(f"invalid supervisor skill policy: {exc}") from exc

    def _supervisor_policy(self) -> SupervisorPolicy:
        agents_path = self._project_path(
            "agents_file", self.project.directory / "agents.yaml"
        )
        try:
            raw = yaml.safe_load(agents_path.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, Mapping):
                raise GuiError("agent registry must be a YAML mapping")
            policy = self._validated_supervisor_policy(raw)
            self._validate_supervisor_skill(policy)
            return policy
        except (OSError, yaml.YAMLError, SupervisorConfigError, AgentConfigError) as exc:
            raise GuiError(f"invalid supervisor policy: {exc}") from exc

    def _supervisor_snapshot(
        self, agents: list[Mapping[str, Any]]
    ) -> dict[str, Any]:
        """Return a durable GUI projection without starting a second runtime."""

        try:
            policy = self._supervisor_policy()
        except GuiError as exc:
            return {
                "enabled": False,
                "available": False,
                "configured_agent": "",
                "agent_id": "",
                "policy": {},
                "incident": {},
                "error": str(exc),
            }
        rows = list(agents)
        if not rows:
            try:
                rows = [
                    {
                        "id": item.provider_id,
                        "adapter": item.adapter,
                        "enabled": item.enabled,
                        "capabilities": sorted(
                            capability.value for capability in item.capabilities
                        ),
                    }
                    for item in self._agent_configs()
                ]
            except (AgentConfigError, OSError, yaml.YAMLError):
                rows = []
        candidates = [
            row
            for row in rows
            if row.get("enabled")
            and str(row.get("adapter", "")).lower() in {
                "codex", "claude", "claude-code"
            }
            and "supervise" in (row.get("capabilities") or [])
        ]
        selected = next(
            (row for row in candidates if row.get("id") == policy.agent_id),
            candidates[0] if candidates and not policy.agent_id else None,
        )
        record = None
        try:
            incident = self.supervisor_incidents.active()
            record = self._load_state()
            if (
                incident is None
                and record is not None
                and record.state == TaskExecutionState.WAITING_FOR_HUMAN_DECISION
            ):
                recent = self.supervisor_incidents.recent(1)
                if recent and recent[0].status != IncidentStatus.RESOLVED:
                    incident = recent[0]
        except (OSError, ValueError):
            incident = None
        incident_mapping = incident.as_mapping() if incident is not None else {}
        transport_evidence = "\n".join(
            (
                str(incident_mapping.get("summary", "")),
                json.dumps(
                    incident_mapping.get("human_question") or {},
                    ensure_ascii=False,
                ),
            )
        )
        unfinished_delegations: list[dict[str, Any]] = []
        persisted_delegations: list[dict[str, Any]] = []
        if incident is not None:
            start = min(
                max(0, incident.pending_delegation_index),
                len(incident.pending_delegations),
            )
            persisted_delegations = [
                dict(item)
                for item in incident.pending_delegations[start:]
                if isinstance(item, Mapping)
            ]
            try:
                journal = EventJournal(self.storage_identity.journal_path)
                unfinished_delegations = unfinished_supervisor_delegations(
                    journal.read(), incident.incident_id
                )
            except (OSError, OrchestrateError, ValueError):
                unfinished_delegations = []
        auto_resume_lost_delegation = bool(
            policy.enabled
            and selected
            and (persisted_delegations or unfinished_delegations)
        )
        deterministic_recovery = self._review_recovery_snapshot(
            record, {"incident": incident_mapping}
        )
        return {
            "enabled": policy.enabled,
            "available": bool(policy.enabled and selected),
            "configured_agent": policy.agent_id,
            "agent_id": str(selected.get("id", "")) if selected else "",
            "policy": policy.as_mapping(),
            "incident": incident_mapping,
            "auto_resume_transport_failure": bool(
                policy.enabled
                and selected
                and is_recoverable_prompt_transport_failure(transport_evidence)
            ),
            "auto_resume_lost_delegation": auto_resume_lost_delegation,
            "stale_human_question": bool(
                auto_resume_lost_delegation
                and incident_mapping.get("human_question")
            ),
            "pending_delegation_count": len(
                persisted_delegations or unfinished_delegations
            ),
            "pending_delegation_source": (
                "persisted_queue"
                if persisted_delegations
                else "journal"
                if unfinished_delegations
                else ""
            ),
            "deterministic_recovery": deterministic_recovery,
            "error": (
                ""
                if not policy.enabled or selected
                else "No enabled Codex or Claude provider has the supervise capability."
            ),
        }

    def answer_supervisor(self, option_id: str, *, message: str = "") -> dict[str, Any]:
        """Record an operator decision and resume the canonical driver."""

        option_id = str(option_id).strip()
        if not option_id:
            raise GuiError("a Supervisor decision option is required")
        if not self._workspace_action_lock.acquire(blocking=False):
            raise GuiError("another workspace action is already in progress")
        try:
            run_status = self.process.status()
            if run_status["owned_running"] or run_status["external_running"]:
                raise GuiError("stop the orchestrator before answering the Supervisor")
            if self.manual_agent_console.any_running():
                raise GuiError(
                    "stop standalone agent consoles before resuming supervision"
                )
            command = [
                sys.executable,
                "-m",
                "execraft.cli",
                "orchestrate",
                "supervisor",
                "--project",
                self.project_id,
                "--task-id",
                self.task_id,
                "--answer",
                option_id,
            ]
            guidance = str(message).strip()
            if guidance:
                command.extend(["--message", guidance])
            result = subprocess.run(
                command,
                cwd=self.root,
                env=self._command_environment(),
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                raise GuiError(
                    result.stderr.strip()
                    or result.stdout.strip()
                    or "recording the Supervisor answer failed"
                )
            record = self._load_state()
            driver = {}
            if record is not None and record.state.value == "supervising":
                driver = self.process.start()
            return {
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "supervisor": self._supervisor_snapshot([]),
                "run": driver,
            }
        finally:
            self._workspace_action_lock.release()

    def _manual_console_status(self, agent_id: str) -> dict[str, Any]:
        status = dict(self.manual_agent_console.status(agent_id))
        try:
            config = next(
                item for item in self._agent_configs() if item.provider_id == agent_id
            )
        except (StopIteration, AgentConfigError, OSError, yaml.YAMLError):
            config = None
        run_status = self.process.status()
        enabled = bool(
            config is not None
            and config.enabled
            and self.agent_console.terminal_policy.enabled
        )
        can_start = enabled and not status.get("running") and not (
            run_status["owned_running"] or run_status["external_running"]
        )
        reason = ""
        if not enabled:
            reason = "provider or interactive console policy is disabled"
        elif status.get("running"):
            reason = "standalone console is already running"
        elif run_status["owned_running"] or run_status["external_running"]:
            reason = "orchestrator is running"
        return {
            **status,
            "enabled": enabled,
            "can_start": can_start,
            "reason": reason,
        }

    def _manual_console_workdir(self) -> Path:
        try:
            record = load_workspace(self.root, self.task_id)
        except TaskGitError as exc:
            raise ManualAgentConsoleError(
                f"task workspace is not ready: {exc}"
            ) from exc
        return Path(record.workspace_root).resolve()

    def start_agent_action(
        self,
        agent_id: str,
        action: str,
        *,
        timeout_seconds: int = 180,
    ) -> dict[str, Any]:
        """Start one bounded provider maintenance action in the background.

        Live probes can consume quota and local GPU capacity, while health resets
        affect scheduler eligibility.  Keep them out of an active orchestration run
        so the operator cannot race the scheduler from the dashboard.
        """

        agent_id = str(agent_id).strip()
        action = str(action).strip()
        if action not in {"doctor", "reset-health"}:
            raise GuiError(f"unsupported agent action: {action}")
        if not agent_id:
            raise GuiError("agent_id is required")
        configs = {item.provider_id: item for item in self._agent_configs()}
        config = configs.get(agent_id)
        if config is None:
            raise GuiError(f"unknown configured provider: {agent_id}")
        if not config.enabled:
            raise GuiError(f"configured provider is disabled: {agent_id}")
        run_status = self.process.status()
        if run_status["external_running"] or run_status["owned_running"]:
            raise GuiError(
                "agent health actions are disabled while an orchestrator driver is active"
            )
        timeout_seconds = max(10, min(900, int(timeout_seconds)))
        with self._agent_action_lock:
            current = self._agent_actions.get(agent_id) or {}
            if current.get("status") == "running":
                raise GuiError(f"an agent action is already running for {agent_id}")
            state = {
                "agent_id": agent_id,
                "action": action,
                "status": "running",
                "started_at": _utc_now(),
                "finished_at": "",
                "returncode": None,
                "stdout": "",
                "stderr": "",
                "error": "",
            }
            self._agent_actions[agent_id] = state
        self._agent_action_executor.submit(
            self._execute_agent_action, agent_id, action, timeout_seconds
        )
        return dict(state)

    def decompose(self, package_id: str) -> dict[str, Any]:
        package_id = str(package_id).strip()
        if not package_id:
            raise GuiError("package_id is required")
        run_status = self.process.status()
        if run_status["external_running"] or run_status["owned_running"]:
            raise GuiError("cannot decompose while an orchestrator driver is active")
        command = [
            sys.executable,
            "-m",
            "execraft.cli",
            "orchestrate",
            "decompose",
            "--project",
            self.project_id,
            "--task-id",
            self.task_id,
            "--state-dir",
            str(self.state_root),
            "--package-id",
            package_id,
        ]
        env = self._command_environment()
        result = subprocess.run(
            command,
            cwd=self.root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode not in {0, 1}:
            raise GuiError(result.stderr.strip() or result.stdout.strip() or "decomposition failed")
        return {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }

    def update_package_policy(
        self,
        *,
        package_id: str,
        agent_preferences: Mapping[str, list[str]],
        skill_preferences: Mapping[str, list[str]],
        agent_preference_binding_roles: list[str] | tuple[str, ...] | None = None,
        apply_to_shards: bool = False,
    ) -> dict[str, Any]:
        """Persist one package execution policy through the canonical CLI path."""

        package_id = str(package_id).strip()
        if not package_id:
            raise GuiError("package_id is required")
        normalized_agents = _string_list_mapping(
            agent_preferences,
            mapping_label="agent-preferences",
            item_label="agent preferences",
        )
        normalized_skills = _string_list_mapping(
            skill_preferences,
            mapping_label="skill-preferences",
            item_label="skill preferences",
        )
        policy = {"agents": normalized_agents, "skills": normalized_skills}
        if agent_preference_binding_roles is not None:
            policy["binding_agent_roles"] = [
                str(item) for item in agent_preference_binding_roles if str(item)
            ]
        with self._exclusive_workspace_action("update Work Package execution policy"):
            command = [
                sys.executable,
                "-m",
                "execraft.cli",
                "orchestrate",
                "policy",
                "--project",
                self.project_id,
                "--task-id",
                self.task_id,
                "--state-dir",
                str(self.state_root),
                "--package-id",
                package_id,
                "--policy-json",
                json.dumps(policy, separators=(",", ":")),
            ]
            if apply_to_shards:
                command.append("--apply-to-shards")
            result = subprocess.run(
                command,
                cwd=self.root,
                env=self._command_environment(),
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                raise GuiError(
                    result.stderr.strip()
                    or result.stdout.strip()
                    or "updating execution policy failed"
                )
        return {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "package_id": package_id,
            "agent_preferences": normalized_agents,
            "agent_preference_binding_roles": list(
                agent_preference_binding_roles or ()
            ),
            "skill_preferences": normalized_skills,
            "apply_to_shards": bool(apply_to_shards),
        }

    def update_package_preferences(
        self,
        *,
        package_id: str,
        preferences: Mapping[str, list[str]],
        apply_to_shards: bool = False,
    ) -> dict[str, Any]:
        """Backward-compatible GUI service wrapper for agent-only clients."""

        record = self._load_state()
        skill_preferences: Mapping[str, list[str]] = {}
        binding_roles: tuple[str, ...] | list[str] = ()
        if record is not None:
            try:
                package = record.plan_graph.package_by_id(package_id)
                skill_preferences = package.skill_preferences
                binding_roles = package.agent_preference_binding_roles
            except OrchestrateError:
                pass
        return self.update_package_policy(
            package_id=package_id,
            agent_preferences=preferences,
            skill_preferences=skill_preferences,
            agent_preference_binding_roles=binding_roles,
            apply_to_shards=apply_to_shards,
        )

    def _execute_agent_action(
        self, agent_id: str, action: str, timeout_seconds: int
    ) -> None:
        returncode = 0
        stdout = ""
        stderr = ""
        error = ""
        try:
            if action == "reset-health":
                self.health_store.reset(agent_id)
                stdout = (
                    f"Reset execution-agent health: {agent_id}\n"
                    "This does not restore quota or credentials; run Doctor test "
                    "after resolving the cause."
                )
            else:
                command = [
                    sys.executable,
                    "-m",
                    "execraft.cli",
                    "agents",
                    "doctor",
                    "--project",
                    self.project_id,
                    "--agent",
                    agent_id,
                    "--smoke-test",
                    "--json",
                    "--timeout-seconds",
                    str(timeout_seconds),
                    "--state-dir",
                    str(self.state_root),
                ]
                env = self._command_environment()
                result = self._agent_command_runner(
                    command,
                    cwd=self.root,
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=timeout_seconds + 30,
                )
                returncode = int(result.returncode)
                stdout = result.stdout or ""
                stderr = result.stderr or ""
                if returncode != 0:
                    error = (stderr.strip() or stdout.strip() or "doctor test failed")
        except subprocess.TimeoutExpired as exc:
            returncode = 124
            stdout = _coerce_process_text(exc.stdout)
            stderr = _coerce_process_text(exc.stderr)
            error = f"doctor test timed out after {timeout_seconds}s"
        except Exception as exc:  # Keep the dashboard alive after provider/CLI faults.
            returncode = 1
            error = str(exc)
        with self._agent_action_lock:
            self._agent_actions[agent_id] = {
                "agent_id": agent_id,
                "action": action,
                "status": "succeeded" if returncode == 0 else "failed",
                "started_at": (self._agent_actions.get(agent_id) or {}).get(
                    "started_at", ""
                ),
                "finished_at": _utc_now(),
                "returncode": returncode,
                "stdout": stdout[-32_000:],
                "stderr": stderr[-16_000:],
                "error": error[-4_000:],
            }

    def _agent_action_state(self, agent_id: str) -> dict[str, Any]:
        with self._agent_action_lock:
            return dict(self._agent_actions.get(agent_id) or {})

    def _agent_configs(self) -> list[Any]:
        agents_path = self._project_path(
            "agents_file", self.project.directory / "agents.yaml"
        )
        raw = yaml.safe_load(agents_path.read_text(encoding="utf-8")) or {}
        # Legacy Native-only surfaces speak provider vocabulary internally. A project may
        # also configure non-Native runtimes, which the runtime topology model
        # describes instead; projecting per profile keeps these working rather
        # than failing closed on the whole file.
        return project_native_agent_configs(raw)[0]

    def _command_environment(self) -> dict[str, str]:
        env = os.environ.copy()
        env["EXECRAFT_CONTROL_ROOT"] = str(self.root)
        source_tree = self.root / "src"
        if source_tree.is_dir():
            existing = env.get("PYTHONPATH", "")
            source_path = str(source_tree)
            env["PYTHONPATH"] = (
                source_path if not existing else source_path + os.pathsep + existing
            )
        return env

    def _load_state(self) -> TaskExecutionStateRecord | None:
        if not self.state_path.is_file():
            return None
        for attempt in range(3):
            try:
                raw = json.loads(self.state_path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise GuiError(f"orchestrator state is not a JSON object: {self.state_path}")
                return TaskExecutionStateRecord.from_mapping(raw)
            except (json.JSONDecodeError, OSError):
                if attempt == 2:
                    raise
                time.sleep(0.03)
        return None

    def _running_agent_invocations(
        self, run_status: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        """Return exact live provider ownership while a driver is active.

        Work-package state is persisted at deterministic stage boundaries and can
        legitimately lag a provider process that has just been selected.  The
        invocation ledger is written immediately before adapter execution, so it
        is the authoritative source for the Action Center during a live attempt.
        Ignore open ledger rows when no driver owns the task; startup recovery will
        close any rows left behind by an interrupted process.
        """

        if not (
            bool(run_status.get("owned_running"))
            or bool(run_status.get("external_running"))
        ):
            return []
        try:
            records = self.agent_invocations.list_running(limit=50)
        except (OSError, ValueError):
            return []
        return [record.as_mapping(include_handoff=False) for record in records]

    def _agent_rows(
        self,
        assignments: list[dict[str, Any]],
        registry: Any,
        endpoint_status: Mapping[str, Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        agents_path = self._project_path(
            "agents_file", self.project.directory / "agents.yaml"
        )
        return build_execution_health_rows(
            agents_path=agents_path,
            registry=registry,
            endpoint_status=endpoint_status,
            assignments=assignments,
            health_store=self.health_store,
            promotion_fields=self._provider_promotion_fields,
            action_state=self._agent_action_state,
        )

    def _opencode_registry(self) -> Any:
        opencode_dir = self.project.configured_path("opencode_dir") or self.project.directory / "opencode"
        return load_opencode_provider_registry(opencode_dir / "providers.yaml")

    def _project_path(self, key: str, fallback: Path) -> Path:
        return self.project.configured_path(key) or fallback.resolve()

    def _config_spec(self, key: str) -> ConfigFileSpec:
        for spec in self.config_specs():
            if spec.key == key:
                return spec
        raise GuiError(f"unknown or unavailable configuration file: {key}")

    def _config_metadata(self, spec: ConfigFileSpec) -> dict[str, Any]:
        stat = spec.path.stat()
        return {
            "key": spec.key,
            "label": spec.label,
            "path": str(spec.path),
            "relative_path": str(spec.path.relative_to(self.root)),
            "requires_idle": spec.requires_idle,
            "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            "size": stat.st_size,
        }

    def _validate_agents(self, content: str) -> None:
        raw = _parse_yaml_mapping(content, label="agent registry")
        try:
            if int(raw.get("schema_version", 1)) >= 4:
                execution = parse_execution_config(raw, include_disabled=True)
                providers = [
                    project_native_profile_legacy_config(execution, profile)
                    for profile in execution.agents
                    if execution.runtime(profile.runtime_id).kind == RuntimeKind.NATIVE
                ]
            else:
                providers = project_native_agent_configs(raw)[0]
        except (AgentConfigError, TypeError, ValueError) as exc:
            raise GuiError(f"invalid execution configuration: {exc}") from exc
        supervisor_policy = self._validated_supervisor_policy(raw, providers=providers)
        self._validate_supervisor_skill(supervisor_policy)
        commit = raw.get("commit") or {}
        if not isinstance(commit, dict):
            raise GuiError("commit must be a YAML mapping")
        mode = commit.get("mode", "automatic")
        if not isinstance(mode, str) or mode.strip().lower() != "automatic":
            raise GuiError("commit.mode must be 'automatic'")
        allow_verified_noop = commit.get("allow_verified_noop", False)
        if not isinstance(allow_verified_noop, bool):
            raise GuiError("commit.allow_verified_noop must be a boolean")
        scheduling = raw.get("scheduling") or {}
        if not isinstance(scheduling, dict):
            raise GuiError("scheduling must be a YAML mapping")
        parallel = scheduling.get("parallel_shards") or {}
        if not isinstance(parallel, dict):
            raise GuiError("scheduling.parallel_shards must be a YAML mapping")
        if "max_workers" in parallel and not 1 <= int(parallel["max_workers"]) <= 16:
            raise GuiError("parallel_shards.max_workers must be between 1 and 16")
        decomposition = scheduling.get("decomposition") or {}
        if not isinstance(decomposition, dict):
            raise GuiError("scheduling.decomposition must be a YAML mapping")
        scope_policy = scheduling.get("scope_policy") or {}
        if not isinstance(scope_policy, dict):
            raise GuiError("scheduling.scope_policy must be a YAML mapping")
        try:
            ScopePolicy.from_mapping(scope_policy)
        except ValueError as exc:
            raise GuiError(f"invalid scheduling.scope_policy: {exc}") from exc
        try:
            scope_recovery_policy_from_scheduling(scheduling)
        except ValueError as exc:
            raise GuiError(f"invalid scheduling recovery policy: {exc}") from exc
        try:
            workspace_finalization_policy_from_scheduling(scheduling)
        except ValueError as exc:
            raise GuiError(
                f"invalid scheduling workspace finalization policy: {exc}"
            ) from exc
        interactive_console = scheduling.get("interactive_console") or {}
        if not isinstance(interactive_console, dict):
            raise GuiError("scheduling.interactive_console must be a YAML mapping")
        try:
            InteractiveTerminalPolicy.from_mapping(interactive_console)
        except ValueError as exc:
            raise GuiError(
                f"invalid scheduling.interactive_console: {exc}"
            ) from exc

    def _validate_providers(self, content: str) -> None:
        with tempfile.TemporaryDirectory(prefix="execraft-gui-provider-") as directory:
            path = Path(directory) / "providers.yaml"
            path.write_text(content, encoding="utf-8")
            load_opencode_provider_registry(path)

    def _validate_project(self, content: str) -> None:
        with tempfile.TemporaryDirectory(
            prefix=".execraft-gui-project-", dir=self.project.directory.parent
        ) as directory:
            temp_project = Path(directory)
            (temp_project / "project.yaml").write_text(content, encoding="utf-8")
            load_project(temp_project)

    def _validate_task(self, content: str) -> None:
        raw = _parse_yaml_mapping(content, label="TASK.yaml")
        manifest = TaskManifest.from_mapping(raw)
        if manifest.id != self.task_id:
            raise GuiError(
                f"task manifest ID mismatch: expected {self.task_id!r}, got {manifest.id!r}"
            )

    def _validate_plan(self, content: str) -> None:
        with tempfile.TemporaryDirectory(prefix="execraft-gui-plan-") as directory:
            path = Path(directory) / "PLAN.graph.yaml"
            path.write_text(content, encoding="utf-8")
            graph, report = load_plan_graph_file(path)
            if report.has_errors():
                raise GuiError(report.summary())
            findings = graph.validate_completeness()
            if findings:
                raise GuiError("; ".join(findings))

def serve_dashboard(
    *,
    service: Any,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> None:
    """Serve the dashboard until interrupted."""

    if not is_loopback_host(host):
        raise GuiError(
            "dashboard remote binding is disabled until every endpoint has "
            "external authentication"
        )
    token = secrets.token_urlsafe(32)
    handler = _handler_factory(service, token)
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    url = f"http://{host}:{server.server_address[1]}/"
    print(f"Execraft GUI: {url}")
    if getattr(service, "task_id", ""):
        print(f"Project: {service.project_id}  Task: {service.task_id}")
    elif getattr(service, "project_id", ""):
        print(f"Project home: {service.project_id}")
    else:
        print("Project home: no active task")
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        service.close()

def _handler_factory(service: Any, token: str) -> type[BaseHTTPRequestHandler]:
    router = GuiApiRouter(service)

    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "execraft-gui/0.2"

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/":
                    self._html(_dashboard_html(token))
                    return
                if parsed.path.startswith("/assets/"):
                    name = parsed.path.removeprefix("/assets/")
                    content, content_type = _dashboard_asset(name)
                    self._asset(content, content_type)
                    return
                if parsed.path in router.protected_get_paths and not self._authorized():
                    return
                try:
                    result = router.get(parsed.path, parse_qs(parsed.query))
                except RouteNotFound:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return
                if isinstance(result, BinaryDownload):
                    self._asset(result.content, result.content_type, filename=result.filename)
                else:
                    self._json(HTTPStatus.OK, result)
            except Exception as exc:
                self._handle_request_error(HTTPStatus.BAD_REQUEST, exc)

        def do_POST(self) -> None:  # noqa: N802
            try:
                if not self._authorized():
                    return
                parsed = urlparse(self.path)
                payload = self._payload()
                try:
                    result = router.post(parsed.path, payload)
                except RouteNotFound:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return
                self._json(HTTPStatus.OK, result)
            except Exception as exc:
                self._handle_request_error(HTTPStatus.CONFLICT, exc)

        def handle_one_request(self) -> None:
            try:
                super().handle_one_request()
            except OSError as exc:
                if not _is_client_disconnect(exc):
                    raise
                self.close_connection = True

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _handle_request_error(self, status: HTTPStatus, exc: Exception) -> None:
            if _is_client_disconnect(exc):
                self.close_connection = True
                return
            try:
                self._json(status, {"error": str(exc)})
            except OSError as write_exc:
                if not _is_client_disconnect(write_exc):
                    raise
                self.close_connection = True

        def _authorized(self) -> bool:
            candidate = self.headers.get("X-Execraft-Token", "")
            if not secrets.compare_digest(candidate, token):
                self._json(HTTPStatus.FORBIDDEN, {"error": "invalid dashboard token"})
                return False
            return True

        def _payload(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 5_000_000:
                raise GuiError("request body is too large")
            raw = self.rfile.read(length) if length else b"{}"
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise GuiError("request body must be a JSON object")
            return value

        def _html(self, content: str) -> None:
            data = content.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self._security_headers("text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _asset(self, content: bytes, content_type: str, *, filename: str = "") -> None:
            self.send_response(HTTPStatus.OK)
            self._security_headers(content_type)
            if filename:
                safe = filename.replace('"', "").replace("\r", "").replace("\n", "")
                self.send_header("Content-Disposition", f'attachment; filename="{safe}"')
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def _json(self, status: HTTPStatus, value: Any) -> None:
            data = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self._security_headers("application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _security_headers(self, content_type: str) -> None:
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                "script-src 'self'; img-src 'self' data:; "
                "connect-src 'self'; frame-ancestors 'none'",
            )

    return DashboardHandler


def _dashboard_html(token: str) -> str:
    package = resources.files("execraft") / "assets" / "gui" / "index.html"
    return package.read_text(encoding="utf-8").replace("__EXECRAFT_TOKEN__", token)


def _dashboard_asset(name: str) -> tuple[bytes, str]:
    """Return one packaged dashboard asset from a strict allowlist."""
    content_types = {
        "gui.css": "text/css; charset=utf-8",
        "app.js": "text/javascript; charset=utf-8",
        "log-controller.js": "text/javascript; charset=utf-8",
        "run-control.js": "text/javascript; charset=utf-8",
        "runtime-control.js": "text/javascript; charset=utf-8",
        "ui-shell.js": "text/javascript; charset=utf-8",
        "workflow.js": "text/javascript; charset=utf-8",
        "workflow-routing.js": "text/javascript; charset=utf-8",
        "workbench-navigation.js": "text/javascript; charset=utf-8",
        "work-package-inspector.js": "text/javascript; charset=utf-8",
        "work-package-execution.js": "text/javascript; charset=utf-8",
        "execution-health-view.js": "text/javascript; charset=utf-8",
        "agent-workforce-view.js": "text/javascript; charset=utf-8",
        "profile-maintenance.js": "text/javascript; charset=utf-8",
        "work-package-presenter.js": "text/javascript; charset=utf-8",
        "execution-trace-view.js": "text/javascript; charset=utf-8",
        "workspace-changes-view.js": "text/javascript; charset=utf-8",
        "workflow-viewport.js": "text/javascript; charset=utf-8",
        "agent-console.js": "text/javascript; charset=utf-8",
        "archive-view.js": "text/javascript; charset=utf-8",
        "context-navigation.js": "text/javascript; charset=utf-8",
        "onboarding-view.js": "text/javascript; charset=utf-8",
        "project-execution-view.js": "text/javascript; charset=utf-8",
        "roadmap-view.js": "text/javascript; charset=utf-8",
        "roadmap-interactions.js": "text/javascript; charset=utf-8",
        "task-lifecycle.js": "text/javascript; charset=utf-8",
        "ui-utils.js": "text/javascript; charset=utf-8",
        "execraft-mark.png": "image/png",
    }
    content_type = content_types.get(str(name))
    if content_type is None:
        raise GuiError("unknown dashboard asset")
    package = resources.files("execraft") / "assets" / "gui" / str(name)
    return package.read_bytes(), content_type


def _parse_yaml_mapping(content: str, *, label: str) -> dict[str, Any]:
    try:
        value = yaml.safe_load(content) or {}
    except yaml.YAMLError as exc:
        raise GuiError(f"invalid YAML in {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise GuiError(f"{label} must contain a YAML mapping")
    return value


def _validate_yaml_mapping(content: str) -> None:
    _parse_yaml_mapping(content, label="configuration")


def _sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _coerce_process_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
