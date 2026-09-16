"""Pure dashboard projections shared by the GUI service.

The HTTP/service boundary in :mod:`execraft.gui.server` should compose durable
state and runtime services, not also own the transformations used to render the
Run workbench.  These helpers deliberately accept already-loaded domain objects
and plain mappings so they remain independently testable and cannot become a
second orchestration model.
"""

from __future__ import annotations

from typing import Any, Mapping

from execraft.orchestrate.directives import (
    PAUSE_BEFORE_START,
    PAUSE_FOR_REPOSITORY_SYNC,
    REQUIRE_DECOMPOSITION,
)
from execraft.orchestrate.models import (
    TaskExecutionState,
    TaskExecutionStateRecord,
    WorkPackage,
    WorkPackageStage,
)


def package_with_pending_directives(
    package: WorkPackage,
    pending: Mapping[str, Mapping[str, Any]],
    sync_summary: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Overlay unconsumed sidecar requests for immediate dashboard feedback."""

    row = package.as_mapping()
    commands = pending.get(package.id, {})
    pending_kinds: list[str] = []
    pause = commands.get(PAUSE_BEFORE_START)
    if pause is not None:
        row["pause_before_start"] = bool(pause.enabled)
        row["pause_before_start_reason"] = pause.reason if pause.enabled else ""
        row["pause_before_start_requested_at"] = (
            pause.requested_at if pause.enabled else ""
        )
        row["pause_before_start_reached_at"] = ""
        pending_kinds.append(PAUSE_BEFORE_START)
    sync_request = commands.get(PAUSE_FOR_REPOSITORY_SYNC)
    if sync_request is not None:
        row["repository_sync_requested"] = bool(sync_request.enabled)
        row["repository_sync_request"] = dict(sync_request.parameters)
        row["repository_sync_request_id"] = sync_request.id
        pending_kinds.append(PAUSE_FOR_REPOSITORY_SYNC)
    else:
        row["repository_sync_requested"] = False
        row["repository_sync_request"] = {}
        row["repository_sync_request_id"] = ""
    row["upstream_sync_summary"] = dict(sync_summary or {})
    decomposition = commands.get(REQUIRE_DECOMPOSITION)
    if decomposition is not None:
        row["decomposition_required"] = bool(decomposition.enabled)
        row["decomposition_required_reason"] = (
            decomposition.reason if decomposition.enabled else ""
        )
        row["decomposition_required_at"] = (
            decomposition.requested_at if decomposition.enabled else ""
        )
        pending_kinds.append(REQUIRE_DECOMPOSITION)
    row["directive_pending_sync"] = pending_kinds
    return row


def active_assignments(
    packages: list[WorkPackage],
    scheduler: Mapping[str, Any],
    *,
    live_invocations: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Project actual agent-owned work without deterministic-stage noise.

    Open invocation rows override persisted role fields because failover
    selection is durable before successful-agent attribution is updated.
    """

    agent_stages = {
        WorkPackageStage.DECOMPOSE,
        WorkPackageStage.IMPLEMENT,
        WorkPackageStage.REVIEW,
        WorkPackageStage.FIX_REVIEW,
        WorkPackageStage.FINAL_REVIEW,
    }
    wave = scheduler.get("parallel_wave") if isinstance(scheduler, Mapping) else None
    wave_agents: dict[str, str] = {}
    if isinstance(wave, Mapping):
        package_ids = [str(item) for item in wave.get("package_ids", [])]
        agent_ids = [str(item) for item in wave.get("agents", [])]
        wave_agents = dict(zip(package_ids, agent_ids))

    packages_by_id = {package.id: package for package in packages}
    assignments: list[dict[str, Any]] = []
    emitted: set[str] = set()
    for invocation in live_invocations or []:
        package_id = str(invocation.get("package_id", "")).strip()
        if not package_id or package_id in emitted:
            continue
        package = packages_by_id.get(package_id)
        stage_value = str(invocation.get("stage", "")).strip() or (
            package.stage.value if package is not None else ""
        )
        try:
            stage = WorkPackageStage(stage_value)
        except ValueError:
            stage = package.stage if package is not None else WorkPackageStage.IMPLEMENT
        assignments.append(
            {
                "package_id": package_id,
                "package_title": package.title if package is not None else package_id,
                "stage": stage_value,
                "status": "running",
                "agent_id": str(invocation.get("agent_id", "")).strip(),
                "model_role": role_for_stage(stage),
                "parent_id": package.parent_id if package is not None else "",
                "parallel": package_id in wave_agents,
                "source": "invocation",
                "invocation_id": str(invocation.get("invocation_id", "")).strip(),
                "started_at": str(invocation.get("started_at", "")).strip(),
                "model": str(invocation.get("model", "")).strip(),
            }
        )
        emitted.add(package_id)

    for package in packages:
        if (
            package.id in emitted
            or package.operator_paused
            or package.stage not in agent_stages
        ):
            continue
        agent_id = wave_agents.get(package.id, "") or agent_for_stage(package)
        assignments.append(
            {
                "package_id": package.id,
                "package_title": package.title,
                "stage": package.stage.value,
                "status": package.status,
                "agent_id": agent_id,
                "model_role": role_for_stage(package.stage),
                "parent_id": package.parent_id,
                "parallel": package.id in wave_agents,
                "source": "state",
            }
        )
    return assignments


def execution_contexts(
    *,
    record: TaskExecutionStateRecord | None,
    assignments: list[dict[str, Any]],
    supervisor: Mapping[str, Any],
    run_control: Mapping[str, Any],
    live_invocations: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Resolve every execution owner currently relevant to the Action Center."""

    packages = record.plan_graph.work_packages if record is not None else []
    packages_by_id = {package.id: package for package in packages}
    wave = record.scheduler.get("parallel_wave") if record is not None else None
    parallel_ids = {
        str(item)
        for item in (wave.get("package_ids", []) if isinstance(wave, Mapping) else [])
        if str(item).strip()
    }
    contexts = _supervisor_context(supervisor, packages_by_id)
    contexts.extend(
        _invocation_contexts(live_invocations or [], packages_by_id, parallel_ids)
    )
    if contexts:
        return contexts
    return _fallback_execution_contexts(
        record=record,
        assignments=assignments,
        run_control=run_control,
        packages=packages,
        packages_by_id=packages_by_id,
    )


def _supervisor_context(
    supervisor: Mapping[str, Any],
    packages_by_id: Mapping[str, WorkPackage],
) -> list[dict[str, Any]]:
    incident = supervisor.get("incident") or {}
    incident_status = str(incident.get("status", "")).strip()
    if not (
        incident.get("incident_id")
        and incident_status not in {"idle", "resolved", "completed", "stopped"}
    ):
        return []
    package_id = str(incident.get("package_id", "")).strip()
    package = packages_by_id.get(package_id)
    return [
        {
            "package_id": package_id,
            "package_title": package.title if package is not None else package_id,
            "parent_id": package.parent_id if package is not None else "",
            "stage": str(
                incident.get("stage") or incident_status or "supervise"
            ).strip(),
            "agent_id": str(
                supervisor.get("agent_id")
                or supervisor.get("configured_agent")
                or incident.get("supervisor_agent_id")
                or ""
            ).strip(),
            "status": incident_status or "active",
            "source": "supervisor",
            "kind": "supervisor",
            "parallel": False,
            "started_at": str(incident.get("opened_at", "")).strip(),
        }
    ]


def _invocation_contexts(
    live_invocations: list[dict[str, Any]],
    packages_by_id: Mapping[str, WorkPackage],
    parallel_ids: set[str],
) -> list[dict[str, Any]]:
    contexts: list[dict[str, Any]] = []
    emitted: set[str] = set()
    for live in live_invocations:
        invocation_id = str(live.get("invocation_id", "")).strip()
        package_id = str(live.get("package_id", "")).strip()
        identity = invocation_id or "|".join(
            [
                package_id,
                str(live.get("stage", "")).strip(),
                str(live.get("agent_id", "")).strip(),
                str(live.get("started_at", "")).strip(),
            ]
        )
        if not package_id or identity in emitted:
            continue
        emitted.add(identity)
        package = packages_by_id.get(package_id)
        contexts.append(
            {
                "package_id": package_id,
                "package_title": package.title if package is not None else package_id,
                "parent_id": package.parent_id if package is not None else "",
                "stage": str(live.get("stage", "")).strip()
                or (package.stage.value if package is not None else ""),
                "agent_id": str(live.get("agent_id", "")).strip(),
                "status": "running",
                "source": "invocation",
                "kind": "agent",
                "parallel": package_id in parallel_ids,
                "invocation_id": invocation_id,
                "started_at": str(live.get("started_at", "")).strip(),
                "model": str(live.get("model", "")).strip(),
            }
        )
    return contexts


def _fallback_execution_contexts(
    *,
    record: TaskExecutionStateRecord | None,
    assignments: list[dict[str, Any]],
    run_control: Mapping[str, Any],
    packages: list[WorkPackage],
    packages_by_id: Mapping[str, WorkPackage],
) -> list[dict[str, Any]]:
    waiting = dict(record.waiting or {}) if record is not None else {}
    if (
        record is not None
        and record.state == TaskExecutionState.OPERATOR_PAUSED
        and waiting.get("kind") == PAUSE_BEFORE_START
    ):
        package_id = str(waiting.get("package_id", "")).strip()
        package = packages_by_id.get(package_id)
        return [
            {
                "package_id": package_id,
                "package_title": package.title if package is not None else package_id,
                "parent_id": package.parent_id if package is not None else "",
                "stage": str(waiting.get("stage", "")).strip()
                or (package.stage.value if package is not None else "prepare"),
                "agent_id": agent_for_stage(package) if package is not None else "",
                "status": TaskExecutionState.OPERATOR_PAUSED.value,
                "source": "planned_pause",
                "kind": "package",
                "parallel": False,
            }
        ]
    if assignments:
        return [_assignment_context(active) for active in assignments]
    return [_package_or_orchestration_context(record, run_control, packages, packages_by_id)]


def _assignment_context(active: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "package_id": str(active.get("package_id", "")).strip(),
        "package_title": str(active.get("package_title", "")).strip(),
        "parent_id": str(active.get("parent_id", "")).strip(),
        "stage": str(active.get("stage", "")).strip(),
        "agent_id": str(active.get("agent_id", "")).strip(),
        "status": str(active.get("status", "")).strip(),
        "source": (
            "invocation"
            if str(active.get("source", "")).strip() == "invocation"
            else "assignment"
        ),
        "kind": "agent",
        "parallel": bool(active.get("parallel")),
        "invocation_id": str(active.get("invocation_id", "")).strip(),
        "started_at": str(active.get("started_at", "")).strip(),
        "model": str(active.get("model", "")).strip(),
    }


def _package_or_orchestration_context(
    record: TaskExecutionStateRecord | None,
    run_control: Mapping[str, Any],
    packages: list[WorkPackage],
    packages_by_id: Mapping[str, WorkPackage],
) -> dict[str, Any]:
    action = run_control.get("human_action") or {}
    action_package_id = str(action.get("package_id", "")).strip()
    package = packages_by_id.get(action_package_id)
    if package is None:
        incomplete = [
            item for item in packages if item.stage != WorkPackageStage.COMPLETED
        ]
        package = next(
            (item for item in incomplete if not item.operator_paused),
            incomplete[0] if incomplete else None,
        )
    if package is not None:
        action_stage = (
            str(action.get("stage", "")).strip()
            if package.id == action_package_id
            else ""
        )
        return {
            "package_id": package.id,
            "package_title": package.title,
            "parent_id": package.parent_id,
            "stage": action_stage or package.stage.value,
            "agent_id": agent_for_stage(package),
            "status": package.status,
            "source": "human_action" if action_stage or action_package_id else "package",
            "kind": "package",
            "parallel": False,
        }
    state = record.state.value if record is not None else "not_initialized"
    return {
        "package_id": action_package_id,
        "package_title": action_package_id,
        "parent_id": "",
        "stage": str(action.get("stage", "")).strip() or state,
        "agent_id": str(action.get("agent_id", "")).strip(),
        "status": state,
        "source": "human_action" if action else "orchestration",
        "kind": "package",
        "parallel": False,
    }


def primary_execution_context(contexts: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the backwards-compatible singular execution-context projection."""

    if not contexts:
        return {}
    context = contexts[0]
    result = {
        key: context.get(key, "")
        for key in ("package_id", "stage", "agent_id", "status", "source")
    }
    if context.get("source") == "invocation":
        result.update(
            {
                key: context[key]
                for key in ("invocation_id", "started_at", "model")
                if context.get(key, "") != ""
            }
        )
    return result


def agent_for_stage(package: WorkPackage) -> str:
    """Resolve the profile that owns the package's current agent stage."""

    if package.stage == WorkPackageStage.DECOMPOSE:
        return package.decomposition_agent_id
    if package.stage in {WorkPackageStage.REVIEW, WorkPackageStage.FINAL_REVIEW}:
        return package.final_reviewer_id or package.reviewer_id
    if package.stage == WorkPackageStage.FIX_REVIEW:
        return package.last_fixer_id
    return package.agent_id


def role_for_stage(stage: WorkPackageStage) -> str:
    """Map an orchestration stage to the presentation model-role name."""

    if stage == WorkPackageStage.DECOMPOSE:
        return "planner"
    if stage in {WorkPackageStage.REVIEW, WorkPackageStage.FINAL_REVIEW}:
        return "reviewer"
    if stage == WorkPackageStage.FIX_REVIEW:
        return "fixer"
    return "implementer"


def agent_nodes(
    agents: list[dict[str, Any]],
    registry: Any,
    endpoint_status: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Group browser-safe profile rows by execution location."""

    grouped: dict[str, dict[str, Any]] = {
        "local": {
            "id": "local",
            "name": "Development PC",
            "kind": "local",
            "url": "",
            "reachable": True,
            "models": [],
            "agents": [],
        }
    }
    for endpoint in registry.endpoints:
        probe = endpoint_status.get(endpoint.endpoint_id, {})
        grouped[endpoint.endpoint_id] = {
            "id": endpoint.endpoint_id,
            "name": endpoint.name,
            "kind": endpoint.kind,  # compatibility API-family field
            "target_kind": getattr(endpoint.target_kind, "value", endpoint.target_kind),
            "provider_family": endpoint.provider_family,
            "provider_alias": endpoint.provider_id,
            "api_family": endpoint.kind,
            "concurrency_group": endpoint.concurrency_group,
            "url": endpoint.base_url,
            "reachable": probe.get("reachable"),
            "latency_ms": probe.get("latency_ms"),
            "models": probe.get("models", list(endpoint.models)),
            "loaded_models": probe.get("loaded_models", []),
            "loaded_models_supported": probe.get("loaded_models_supported", False),
            "loaded_models_error": probe.get("loaded_models_error", ""),
            "error": probe.get("error", ""),
            "agents": [],
        }
    for agent in agents:
        endpoint = dict(agent.get("endpoint") or {})
        endpoint_id = str(endpoint.get("id") or "local")
        if endpoint_id not in grouped:
            grouped[endpoint_id] = {
                "id": endpoint_id,
                "name": endpoint_id,
                "kind": str(
                    endpoint.get("api_family")
                    or endpoint.get("target_kind")
                    or "execution"
                ),
                "target_kind": str(endpoint.get("target_kind") or "local"),
                "provider_family": str(endpoint.get("provider_family") or ""),
                "provider_alias": str(endpoint.get("provider_alias") or ""),
                "api_family": str(endpoint.get("api_family") or ""),
                "concurrency_group": str(
                    endpoint.get("concurrency_group") or endpoint_id
                ),
                "url": str(endpoint.get("url") or ""),
                "reachable": endpoint.get("reachable"),
                "latency_ms": endpoint.get("latency_ms"),
                "models": list(endpoint.get("models") or []),
                "loaded_models": list(endpoint.get("loaded_models") or []),
                "loaded_models_supported": bool(
                    endpoint.get("loaded_models_supported", False)
                ),
                "loaded_models_error": str(endpoint.get("loaded_models_error") or ""),
                "error": str(endpoint.get("error") or ""),
                "agents": [],
            }
        grouped[endpoint_id]["agents"].append(agent["id"])
    return list(grouped.values())
