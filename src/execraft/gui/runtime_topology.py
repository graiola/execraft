"""GUI application service for runtime/model/target controls.

Passive inventory never contacts model endpoints or OpenClaw.  Network/process
operations are isolated behind the explicit diagnostics action.  Package routing
continues to persist through the canonical execution-policy CLI path.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Mapping

import yaml

from execraft.agents.config import parse_execution_config
from execraft.model_registry import ModelEndpoint, ModelRegistryError, ModelRouteRegistry, load_model_route_registry
from execraft.project import load_registered_project
from execraft.runtime.config_migration import (
    RuntimeConfigMigrationError,
    apply_agents_v4_migration,
    normalize_execution_for_operator,
    preview_agents_v4_migration,
)
from execraft.runtime.execution_setup import (
    ExecutionSetupError,
    apply_execution_configuration,
    preview_model_route_configuration,
)
from execraft.runtime.openclaw_setup import (
    OpenClawSetupError,
    apply_openclaw_configuration,
    inspect_openclaw_host,
    install_openclaw,
    openclaw_install_command,
    preview_openclaw_configuration,
)
from execraft.runtime.operator_control import compile_runtime_preference_update
from execraft.runtime.security_diagnostics import diagnose_openclaw_security
from execraft.runtime.topology import build_runtime_topology
from execraft.runtime_config import OpenClawMode, RuntimeKind
from execraft.orchestrate.execution_policy import execution_role_metadata
from execraft.targets.config import ExecutionTargetKind

from .errors import GuiError
from .execution_lanes import execution_lane_mappings


def _load_topology_registry(path: Path) -> tuple[ModelRouteRegistry, tuple[str, ...]]:
    """Load the optional Native/OpenCode registry without hiding schema-v4 topology.

    The canonical execution topology is self-contained in schema v4.  A broken
    compatibility registry is therefore reported as a warning here and as its
    own readiness dimension; it must not make Runtime / Model / Target inventory
    disappear for otherwise valid Native/OpenClaw projects.
    """

    try:
        return load_model_route_registry(path), ()
    except ModelRegistryError as exc:
        return ModelRouteRegistry(), (f"Model endpoint registry is invalid: {exc}",)


def _project_execution(service: Any, project_id: str = "") -> tuple[Any, ModelRouteRegistry, tuple[str, ...]]:
    project_id = str(project_id or getattr(service, "project_id", "")).strip()
    if not project_id:
        raise GuiError("select a project before inspecting runtime topology")
    project = load_registered_project(Path(service.root), project_id)
    agents_path = project.configured_path("agents_file") or project.directory / "agents.yaml"
    try:
        raw = yaml.safe_load(agents_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise GuiError(f"cannot load agent configuration: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise GuiError("agent configuration must be a YAML mapping")
    opencode_dir = project.configured_path("opencode_dir") or project.directory / "opencode"
    registry, registry_warnings = _load_topology_registry(opencode_dir / "providers.yaml")
    execution, warnings = normalize_execution_for_operator(raw, model_registry=registry)
    return execution, registry, (*warnings, *registry_warnings)



def _project_paths(service: Any, project_id: str = "") -> tuple[Any, Path, Path]:
    selected = str(project_id or getattr(service, "project_id", "")).strip()
    if not selected:
        raise GuiError("select a project before configuring execution")
    project = load_registered_project(Path(service.root), selected)
    agents_path = project.configured_path("agents_file") or project.directory / "agents.yaml"
    opencode_dir = project.configured_path("opencode_dir") or project.directory / "opencode"
    return project, agents_path, opencode_dir / "providers.yaml"


def runtime_setup_snapshot(service: Any, project_id: str = "") -> dict[str, Any]:
    """Return passive setup/migration state without contacting any Gateway."""

    _project, agents_path, registry_path = _project_paths(service, project_id)
    try:
        raw = yaml.safe_load(agents_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise GuiError(f"cannot load agent configuration: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise GuiError("agent configuration must be a YAML mapping")
    try:
        source_schema = int(raw.get("schema_version", 1))
    except (TypeError, ValueError) as exc:
        raise GuiError("agents schema_version must be an integer") from exc
    registry, registry_warnings = _load_topology_registry(registry_path)
    migration: dict[str, Any] = {
        "required": source_schema < 4,
        "source_schema_version": source_schema,
        "target_schema_version": 4,
    }
    if source_schema < 4:
        try:
            migration.update(preview_agents_v4_migration(agents_path, model_registry=registry).as_mapping())
        except RuntimeConfigMigrationError as exc:
            migration["error"] = str(exc)
    execution, warnings = normalize_execution_for_operator(raw, model_registry=registry)
    openclaw_runtimes: list[dict[str, Any]] = []
    for runtime in execution.runtimes:
        if runtime.kind != RuntimeKind.OPENCLAW or runtime.openclaw is None:
            continue
        security = diagnose_openclaw_security(execution, runtime.id)
        openclaw_runtimes.append(
            {
                "id": runtime.id,
                "mode": runtime.openclaw.mode.value,
                "gateway": runtime.openclaw.gateway,
                "executable": runtime.openclaw.executable or "openclaw",
                "auth_kind": runtime.openclaw.auth_kind,
                "authentication_configured": bool(runtime.openclaw.auth_ref.strip()),
                "version_policy": runtime.openclaw.version_policy.value,
                "security": {
                    "enforcement": security.enforcement,
                    "configured_secure": security.configured_secure,
                    "sandbox_owner": security.sandbox_owner,
                    "workspace_binding": security.workspace_binding,
                    "live_enforcement_evidence": security.live_enforcement_evidence,
                    "warnings": list(security.warnings),
                },
            }
        )

    openclaw_profiles = []
    for profile in execution.agents:
        runtime = execution.runtime(profile.runtime_id)
        if runtime.kind != RuntimeKind.OPENCLAW:
            continue
        openclaw_profiles.append(
            {
                "id": profile.id,
                "runtime_id": runtime.id,
                "model_route_id": profile.model_route_id,
                "target_id": profile.target_id,
                "capabilities": sorted(item.value for item in profile.capabilities),
                "sandbox_enabled": bool(profile.policy.sandbox_enabled),
                "sandbox": profile.policy.sandbox,
            }
        )
    return {
        "project_id": str(project_id or getattr(service, "project_id", "")),
        "agents_path": str(agents_path),
        "schema_version": source_schema,
        "migration": migration,
        "host": inspect_openclaw_host().as_mapping(),
        "openclaw_runtimes": openclaw_runtimes,
        "openclaw_profiles": openclaw_profiles,
        "warnings": [*warnings, *registry_warnings],
    }


def preview_runtime_migration(service: Any, project_id: str = "") -> dict[str, Any]:
    _project, agents_path, registry_path = _project_paths(service, project_id)
    try:
        preview = preview_agents_v4_migration(
            agents_path, model_registry=load_model_route_registry(registry_path)
        )
    except (RuntimeConfigMigrationError, OSError, ValueError) as exc:
        raise GuiError(str(exc)) from exc
    return preview.as_mapping()


def apply_runtime_migration(
    service: Any, *, project_id: str = "", expected_sha256: str
) -> dict[str, Any]:
    _project, agents_path, registry_path = _project_paths(service, project_id)
    try:
        preview = preview_agents_v4_migration(
            agents_path, model_registry=load_model_route_registry(registry_path)
        )
        if preview.source_sha256 != expected_sha256:
            raise GuiError("execution configuration changed after migration preview; refresh first")
        backup = apply_agents_v4_migration(agents_path, preview)
    except (RuntimeConfigMigrationError, OSError, ValueError) as exc:
        raise GuiError(str(exc)) from exc
    return {
        "applied": preview.changed,
        "backup": str(backup) if preview.changed else "",
        "setup": runtime_setup_snapshot(service, project_id),
        "topology": runtime_topology_snapshot(service, project_id),
    }



def _execution_preview_from_payload(service: Any, payload: Mapping[str, Any]) -> Any:
    project_id = str(payload.get("project_id", ""))
    _project, agents_path, _registry_path = _project_paths(service, project_id)
    try:
        return preview_model_route_configuration(
            agents_path,
            profile_id=str(payload.get("profile_id", "")),
            route_id=str(payload.get("route_id", "")),
            provider=str(payload.get("provider", "")),
            model=str(payload.get("model", "")),
            target_id=str(payload.get("target_id", "")),
            target_kind=str(payload.get("target_kind", "local")),
            endpoint=str(payload.get("endpoint", "")),
            credential_ref=str(payload.get("credential_ref", "")),
            api_family=str(payload.get("api_family", "")),
        )
    except (ExecutionSetupError, OSError, TypeError, ValueError) as exc:
        raise GuiError(str(exc)) from exc


def preview_execution_setup(service: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Preview a runtime-neutral model-route/location edit."""

    return _execution_preview_from_payload(service, payload).as_mapping()


def apply_execution_setup(service: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    project_id = str(payload.get("project_id", ""))
    _project, agents_path, _registry_path = _project_paths(service, project_id)
    preview = _execution_preview_from_payload(service, payload)
    if preview.source_sha256 != str(payload.get("expected_sha256", "")):
        raise GuiError("execution configuration changed after preview; refresh first")
    try:
        backup = apply_execution_configuration(agents_path, preview)
    except (ExecutionSetupError, OSError) as exc:
        raise GuiError(str(exc)) from exc
    return {
        "applied": preview.changed,
        "backup": str(backup) if backup else "",
        "setup": runtime_setup_snapshot(service, project_id),
        "topology": runtime_topology_snapshot(service, project_id),
    }

def _openclaw_preview_from_payload(service: Any, payload: Mapping[str, Any]) -> Any:
    project_id = str(payload.get("project_id", ""))
    _project, agents_path, _registry_path = _project_paths(service, project_id)
    raw_caps = payload.get("capabilities", ["implement", "review", "fix_review"])
    if not isinstance(raw_caps, list):
        raise GuiError("capabilities must be a JSON list")
    try:
        return preview_openclaw_configuration(
            agents_path,
            runtime_id=str(payload.get("runtime_id", "openclaw-local")),
            profile_id=str(payload.get("profile_id", "openclaw-local")),
            mode=str(payload.get("mode", "managed")),
            gateway=str(payload.get("gateway", "")),
            executable=str(payload.get("executable", "openclaw")),
            auth_kind=str(payload.get("auth_kind", "token")),
            auth_ref=str(payload.get("auth_ref", "")),
            model_route_id=str(payload.get("model_route_id", "")),
            target_id=str(payload.get("target_id", "")),
            capabilities=[str(item) for item in raw_caps],
            priority=int(payload.get("priority", 50)),
            max_complexity=int(payload.get("max_complexity", 70)),
            route_id=str(payload.get("route_id", "")),
            provider=str(payload.get("provider", "")),
            model=str(payload.get("model", "")),
            endpoint=str(payload.get("endpoint", "")),
            target_kind=str(payload.get("target_kind", "local")),
        )
    except (OpenClawSetupError, TypeError, ValueError) as exc:
        raise GuiError(str(exc)) from exc


def preview_openclaw_setup(service: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    return _openclaw_preview_from_payload(service, payload).as_mapping()


def apply_openclaw_setup(service: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    project_id = str(payload.get("project_id", ""))
    _project, agents_path, _registry_path = _project_paths(service, project_id)
    preview = _openclaw_preview_from_payload(service, payload)
    if preview.source_sha256 != str(payload.get("expected_sha256", "")):
        raise GuiError("execution configuration changed after OpenClaw preview; refresh first")
    try:
        backup = apply_openclaw_configuration(agents_path, preview)
    except (OpenClawSetupError, OSError) as exc:
        raise GuiError(str(exc)) from exc
    return {
        "applied": preview.changed,
        "backup": str(backup) if backup else "",
        "setup": runtime_setup_snapshot(service, project_id),
        "topology": runtime_topology_snapshot(service, project_id),
    }


def preview_openclaw_install(
    service: Any, *, prefix: str = "~/.local", version: str = ""
) -> dict[str, Any]:
    del service
    try:
        command = openclaw_install_command(Path(prefix), version=version)
    except OpenClawSetupError as exc:
        raise GuiError(str(exc)) from exc
    return {"command": list(command), "host": inspect_openclaw_host(Path(prefix)).as_mapping()}


def apply_openclaw_install(
    service: Any, *, prefix: str = "~/.local", version: str = ""
) -> dict[str, Any]:
    del service
    try:
        return install_openclaw(Path(prefix), version=version)
    except OpenClawSetupError as exc:
        raise GuiError(str(exc)) from exc

def runtime_topology_snapshot(service: Any, project_id: str = "") -> dict[str, Any]:
    execution, _registry, warnings = _project_execution(service, project_id)
    payload = build_runtime_topology(execution)
    payload["execution_lanes"] = execution_lane_mappings(
        payload.get("profiles", []), execution_role_metadata()
    )
    payload["warnings"] = list(warnings)
    snapshot = service.snapshot()
    if snapshot.get("mode") == "task":
        payload["packages"] = [
            {
                "id": str(item.get("id", "")),
                "title": str(item.get("title", "")),
                "stage": str(item.get("stage", "")),
                "status": str(item.get("status", "")),
                "agent_preferences": dict(item.get("agent_preferences") or {}),
            }
            for item in snapshot.get("packages", [])
        ]
        payload["run"] = dict(snapshot.get("run") or {})
        payload["task_id"] = str(snapshot.get("project", {}).get("task_id", ""))
    else:
        payload["packages"] = []
        payload["run"] = {"owned_running": False, "external_running": False}
        payload["task_id"] = ""
    payload["project_id"] = str(project_id or getattr(service, "project_id", ""))
    return payload


def _diagnostic_model_endpoint(route: Any, target: Any, registry: ModelRouteRegistry) -> ModelEndpoint | None:
    """Resolve an explicit diagnostic endpoint from canonical v4 data first.

    The legacy OpenCode registry remains a compatibility source, but new GUI
    model routes may be fully described by ``model_routes`` +
    ``execution_targets`` and must be diagnosable without duplicating an
    OpenCode provider entry.
    """

    native_ref = route.reference_for_native_adapter("opencode")
    endpoint = registry.endpoint_for_model(native_ref)
    if endpoint is not None:
        return endpoint
    base_url = str(route.endpoint or (target.endpoint if target is not None else "")).strip()
    if not base_url:
        return None
    target_id = target.id if target is not None else (route.default_target or route.id)
    target_kind = target.kind if target is not None else ExecutionTargetKind.LOCAL
    return ModelEndpoint(
        endpoint_id=f"route:{route.id}",
        provider_family=route.provider,
        provider_alias=route.provider_alias or route.provider,
        name=route.id,
        base_url=base_url,
        base_url_source="schema-v4 model route",
        models={route.model: {}},
        target_id=target_id,
        target_kind=target_kind,
        concurrency_group=(target.concurrency_group if target is not None else target_id),
        api_family=route.api_family or "openai-compatible",
    )


def diagnose_runtime_layers(
    service: Any,
    *,
    project_id: str = "",
    runtime_id: str = "",
    model_route_id: str = "",
    target_id: str = "",
) -> dict[str, Any]:
    """Probe only explicitly selected layers and keep the result JSON-safe."""

    execution, registry, _warnings = _project_execution(service, project_id)
    result: dict[str, Any] = {}
    target = execution.target(target_id) if target_id else None

    if runtime_id:
        runtime = execution.runtime(runtime_id)
        if runtime.kind == RuntimeKind.NATIVE:
            binary = str(getattr(runtime, "binary", "") or getattr(runtime, "adapter", ""))
            result["runtime"] = {
                "id": runtime.id,
                "kind": runtime.kind.value,
                "healthy": bool(binary and (Path(binary).is_file() or shutil.which(binary))),
                "binary": binary,
            }
        else:
            if runtime.openclaw is None:
                raise GuiError(f"runtime {runtime.id!r} has no OpenClaw options")
            if target is not None and target.kind == ExecutionTargetKind.REMOTE_RUNTIME:
                raise GuiError(
                    "remote full-runtime targets are experimental-disabled; "
                    "use a local or inference_endpoint target"
                )
            from execraft.runtime.openclaw_projection import project_openclaw_config
            from execraft.runtime.openclaw_service import OpenClawGatewayService

            projection = project_openclaw_config(execution, runtime.id)
            gateway = OpenClawGatewayService(
                runtime,
                state_root=Path(service.state_root),
                config_payload=projection.config,
                credential_env_refs=projection.credential_refs,
            )
            try:
                gateway_result = (
                    gateway.start()
                    if runtime.openclaw.mode == OpenClawMode.MANAGED
                    else gateway.probe()
                )
            finally:
                gateway.stop()
            row = gateway_result.as_mapping()
            row["security"] = diagnose_openclaw_security(
                execution, runtime.id
            ).as_mapping()
            result["runtime"] = row

    if model_route_id:
        route = execution.model_route(model_route_id)
        row: dict[str, Any] = {
            "id": route.id,
            "provider": route.provider,
            "model": route.model,
            "endpoint": route.endpoint,
            "credential_configured": bool(str(route.credential_ref).strip()),
            "healthy": None,
        }
        endpoint = _diagnostic_model_endpoint(route, target, registry)
        if endpoint is not None:
            from execraft.targets.health import probe_model_endpoint

            probe = probe_model_endpoint(endpoint, timeout_seconds=min(3, endpoint.connect_timeout_seconds))
            row.update(
                {
                    "healthy": probe.reachable and (not route.model or route.model in probe.models),
                    "reachable": probe.reachable,
                    "latency_ms": probe.latency_ms,
                    "models": sorted(probe.models),
                    "error": probe.error,
                }
            )
        result["model_route"] = row

    if target is not None:
        result["execution_target"] = {
            "id": target.id,
            "kind": target.kind.value,
            "endpoint": target.endpoint,
            "workspace_transport": target.workspace_transport,
            "max_concurrency": target.max_concurrency,
            "environment": target.environment.as_mapping() if target.environment else {},
            "healthy": result.get("runtime", {}).get("healthy") if target.kind == ExecutionTargetKind.REMOTE_RUNTIME else result.get("model_route", {}).get("reachable"),
        }
    return result


class RuntimeTopologyDashboardMixin:
    """Task-bound mutation surface consumed by the shared runtime routes."""

    def preview_runtime_selection(self, **selection: Any) -> dict[str, Any]:
        execution, _registry, _warnings = _project_execution(self)
        record = self._load_state()
        if record is None:
            raise GuiError("orchestration state is not initialized")
        package_id = str(selection.get("package_id", "")).strip()
        try:
            package = record.plan_graph.package_by_id(package_id)
        except Exception as exc:
            raise GuiError(str(exc)) from exc
        run = self.process.status()
        active = bool(run["owned_running"] or run["external_running"])
        update = compile_runtime_preference_update(
            execution,
            existing_preferences=package.agent_preferences,
            existing_binding_roles=package.agent_preference_binding_roles,
            role=str(selection.get("role", "implement")),
            mode=str(selection.get("mode", "automatic")),
            runtime_id=str(selection.get("runtime_id", "")),
            model_route_id=str(selection.get("model_route_id", "")),
            target_id=str(selection.get("target_id", "")),
            invocation_active=active,
        )
        payload = update.as_mapping()
        payload.update(
            {
                "package_id": package_id,
                "apply_to_shards": bool(selection.get("apply_to_shards", False)),
                "run": run,
                "can_apply_now": not active,
                "can_cancel_and_switch": bool(run["owned_running"]),
                "external_driver_blocks_switch": bool(run["external_running"]),
            }
        )
        return payload

    def apply_runtime_selection(self, *, cancel_and_switch: bool = False, **selection: Any) -> dict[str, Any]:
        preview = self.preview_runtime_selection(**selection)
        run = preview["run"]
        restarted = False
        if run["external_running"]:
            raise GuiError(
                "an externally owned orchestrator is active; wait for it to become idle before changing runtime routing"
            )
        if run["owned_running"]:
            if not cancel_and_switch:
                raise GuiError(
                    "the dashboard orchestrator is active; use Cancel & switch to restart at a safe invocation boundary"
                )
            self.process.stop()
        record = self._load_state()
        if record is None:
            raise GuiError("orchestration state is not initialized")
        package = record.plan_graph.package_by_id(str(preview["package_id"]))
        result = self.update_package_policy(
            package_id=str(preview["package_id"]),
            agent_preferences=preview["agent_preferences"],
            skill_preferences=package.skill_preferences,
            agent_preference_binding_roles=preview["agent_preference_binding_roles"],
            apply_to_shards=bool(preview["apply_to_shards"]),
        )
        if run["owned_running"] and cancel_and_switch:
            self.process.start()
            restarted = True
        return {"selection": preview, "policy": result, "restarted": restarted}


def review_capable_candidates(service: Any, project_id: str = "") -> list[dict[str, Any]]:
    """List every review-capable candidate, whatever runtime it runs on.

    The operator's reviewer picker predates the runtime model and was built from
    the Native provider projection, which silently omits candidates on any other
    runtime. Reading the normalized execution config instead keeps the picker
    honest: an OpenClaw reviewer is selectable exactly like a Native one, and
    each row says which runtime it would actually run on.
    """

    execution, _registry, _warnings = _project_execution(service, project_id)
    health_store = getattr(service, "health_store", None)
    rows: list[dict[str, Any]] = []
    for profile in execution.agents:
        if not profile.enabled:
            continue
        if "review" not in {item.value for item in profile.capabilities}:
            continue
        runtime = execution.runtime(profile.runtime_id)
        route = (
            execution.model_route(profile.model_route_id)
            if profile.model_route_id
            else None
        )
        health = health_store.get(profile.candidate_id) if health_store else None
        rows.append(
            {
                "id": profile.candidate_id,
                "name": profile.name or profile.candidate_id,
                "model": route.model if route else "",
                "adapter": runtime.adapter or runtime.kind.value,
                "runtime_id": runtime.id,
                "runtime_kind": runtime.kind.value,
                "available": bool(health.is_available) if health else True,
                "health": health.status if health else "unknown",
                "reason": health.reason if health else "",
            }
        )
    return rows
