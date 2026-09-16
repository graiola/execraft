"""HTTP-independent routes for the canonical Project Execution workspace."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from execraft.gui.errors import GuiError

from .onboarding import _MISSING, _query_value
from .payload import payload_bool, payload_int


def _string(payload: Mapping[str, Any], key: str, *, default: str = "") -> str:
    value = payload.get(key, default)
    if not isinstance(value, str):
        raise GuiError(f"{key} must be a JSON string")
    return value


def _mapping(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        raise GuiError(f"{key} must be a JSON object")
    return value



def _asset_metadata(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Decode canonical asset metadata without coercing browser values."""

    value = _mapping(payload, "metadata")
    allowed = {"title", "description", "schedule"}
    unknown = set(value) - allowed
    if unknown:
        names = ", ".join(sorted(map(str, unknown)))
        raise GuiError(f"unsupported asset metadata field(s): {names}")
    for key in ("title", "description"):
        if key in value and not isinstance(value[key], str):
            raise GuiError(f"metadata.{key} must be a JSON string")
    schedule = value.get("schedule")
    if schedule is not None:
        if not isinstance(schedule, Mapping):
            raise GuiError("metadata.schedule must be a JSON object")
        unknown_schedule = set(schedule) - {"start", "target"}
        if unknown_schedule:
            names = ", ".join(sorted(map(str, unknown_schedule)))
            raise GuiError(f"unsupported metadata.schedule field(s): {names}")
        for key in ("start", "target"):
            if key in schedule and not isinstance(schedule[key], str):
                raise GuiError(f"metadata.schedule.{key} must be a JSON string")
    return value



def _task_metadata(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Decode project-level Task metadata without accepting Task-domain copies."""

    value = _mapping(payload, "metadata")
    allowed = {"phase", "required", "requires"}
    unknown = set(value) - allowed
    if unknown:
        names = ", ".join(sorted(map(str, unknown)))
        raise GuiError(f"unsupported project Task metadata field(s): {names}")
    if "phase" in value and not isinstance(value["phase"], str):
        raise GuiError("metadata.phase must be a JSON string")
    if "required" in value and not isinstance(value["required"], bool):
        raise GuiError("metadata.required must be a JSON boolean")
    requires = value.get("requires")
    if requires is not None:
        if not isinstance(requires, Mapping):
            raise GuiError("metadata.requires must be a JSON object")
        unknown_requires = set(requires) - {"tasks", "gates"}
        if unknown_requires:
            names = ", ".join(sorted(map(str, unknown_requires)))
            raise GuiError(f"unsupported metadata.requires field(s): {names}")
        for key in ("tasks", "gates"):
            refs = requires.get(key, [])
            if not isinstance(refs, list) or any(not isinstance(item, str) for item in refs):
                raise GuiError(f"metadata.requires.{key} must be a JSON string array")
    return value

def _acknowledged(payload: Mapping[str, Any], operation: str) -> None:
    if not payload_bool(payload, "acknowledged", default=False):
        raise GuiError(f"{operation} requires explicit acknowledgement")


_POST_PATHS = frozenset(
    {
        "/api/project-execution/initialize",
        "/api/project-execution/mode",
        "/api/project-execution/policy",
        "/api/project-execution/reconcile",
        "/api/project-execution/coordination/resolve",
        "/api/project-execution/automatic/cycle",
        "/api/project-execution/pause",
        "/api/project-execution/resume",
        "/api/project-execution/task/start",
        "/api/project-execution/gate/decide",
        "/api/project-execution/gate/waive",
        "/api/project-execution/phase/upsert",
        "/api/project-execution/phase/metadata",
        "/api/project-execution/phase/delete",
        "/api/project-execution/gate/upsert",
        "/api/project-execution/gate/metadata",
        "/api/project-execution/gate/delete",
        "/api/project-execution/milestone/upsert",
        "/api/project-execution/milestone/metadata",
        "/api/project-execution/milestone/delete",
        "/api/project-execution/task/assign",
        "/api/project-execution/task/remove",
    }
)


def dispatch_get(service: Any, path: str, query: Mapping[str, list[str]]) -> Any:
    if path == "/api/project-execution/status":
        return service.project_execution_workspace.snapshot(
            _query_value(query, "project_id")
        )
    if path == "/api/project-execution/coordination":
        return service.project_execution_workspace.coordination_status(
            _query_value(query, "project_id")
        )
    if path == "/api/project-execution/coordination/history":
        raw_limit = _query_value(query, "limit", "20")
        try:
            limit = int(raw_limit)
        except ValueError as exc:
            raise GuiError("coordination history limit must be an integer") from exc
        if not 1 <= limit <= 100:
            raise GuiError("coordination history limit must be between 1 and 100")
        return service.project_execution_workspace.coordination_history(
            _query_value(query, "project_id"),
            limit=limit,
        )
    return _MISSING


def dispatch_post(service: Any, path: str, payload: Mapping[str, Any]) -> Any:
    # Route groups are consulted in sequence.  Do not decode project payloads or
    # touch the Project Execution service for paths owned by another domain;
    # lightweight route tests and embedders are allowed to compose only the
    # services they actually expose.
    if path not in _POST_PATHS:
        return _MISSING

    project_id = _string(payload, "project_id")
    workspace = service.project_execution_workspace

    if path == "/api/project-execution/initialize":
        _acknowledged(payload, "initializing Project Execution")
        return workspace.initialize(project_id, mode=_string(payload, "mode", default="assisted"))
    if path == "/api/project-execution/mode":
        _acknowledged(payload, "changing Project Execution mode")
        return workspace.set_mode(
            project_id,
            mode=_string(payload, "mode"),
            expected_revision=payload_int(payload, "expected_revision", default=0),
        )
    if path == "/api/project-execution/policy":
        _acknowledged(payload, "changing Automatic execution policy")
        return workspace.set_policy(
            project_id,
            _mapping(payload, "policy"),
            expected_revision=payload_int(payload, "expected_revision", default=0),
        )
    if path == "/api/project-execution/reconcile":
        return workspace.snapshot(project_id)
    if path == "/api/project-execution/coordination/resolve":
        _acknowledged(payload, "resolving canonical Roadmap coordination")
        return workspace.resolve_coordination(
            project_id,
            action=_string(payload, "action"),
        )
    if path == "/api/project-execution/automatic/cycle":
        _acknowledged(payload, "running an Automatic execution cycle")
        return workspace.automatic_cycle(project_id)
    if path == "/api/project-execution/pause":
        _acknowledged(payload, "pausing Project Execution")
        return workspace.pause(project_id, reason=_string(payload, "reason"))
    if path == "/api/project-execution/resume":
        _acknowledged(payload, "resuming Project Execution")
        return workspace.resume(project_id)
    if path == "/api/project-execution/task/start":
        _acknowledged(payload, "starting a Project Task")
        return workspace.start_task(
            project_id,
            _string(payload, "task_id"),
            retry_uncertain=payload_bool(payload, "retry_uncertain", default=False),
        )
    if path == "/api/project-execution/gate/decide":
        _acknowledged(payload, "recording a Gate decision")
        return workspace.decide_gate(
            project_id,
            _string(payload, "gate_id"),
            actor=_string(payload, "actor"),
            decision=_string(payload, "decision"),
            reason=_string(payload, "reason"),
        )
    if path == "/api/project-execution/gate/waive":
        _acknowledged(payload, "waiving a Gate")
        return workspace.waive_gate(
            project_id,
            _string(payload, "gate_id"),
            actor=_string(payload, "actor"),
            reason=_string(payload, "reason"),
        )
    if path == "/api/project-execution/phase/upsert":
        return workspace.upsert_phase(
            project_id,
            _mapping(payload, "phase"),
            expected_revision=payload_int(payload, "expected_revision", default=0),
        )
    if path == "/api/project-execution/phase/metadata":
        return workspace.update_phase_metadata(
            project_id,
            _string(payload, "phase_id"),
            _asset_metadata(payload),
            expected_revision=payload_int(payload, "expected_revision", default=0),
        )
    if path == "/api/project-execution/phase/delete":
        return workspace.delete_phase(
            project_id,
            _string(payload, "phase_id"),
            expected_revision=payload_int(payload, "expected_revision", default=0),
        )
    if path == "/api/project-execution/gate/upsert":
        return workspace.upsert_gate(
            project_id,
            _mapping(payload, "gate"),
            expected_revision=payload_int(payload, "expected_revision", default=0),
        )
    if path == "/api/project-execution/gate/metadata":
        return workspace.update_gate_metadata(
            project_id,
            _string(payload, "gate_id"),
            _asset_metadata(payload),
            expected_revision=payload_int(payload, "expected_revision", default=0),
        )
    if path == "/api/project-execution/gate/delete":
        return workspace.delete_gate(
            project_id,
            _string(payload, "gate_id"),
            expected_revision=payload_int(payload, "expected_revision", default=0),
        )
    if path == "/api/project-execution/milestone/upsert":
        return workspace.upsert_milestone(
            project_id,
            _mapping(payload, "milestone"),
            expected_revision=payload_int(payload, "expected_revision", default=0),
        )
    if path == "/api/project-execution/milestone/metadata":
        return workspace.update_milestone_metadata(
            project_id,
            _string(payload, "milestone_id"),
            _asset_metadata(payload),
            expected_revision=payload_int(payload, "expected_revision", default=0),
        )
    if path == "/api/project-execution/milestone/delete":
        return workspace.delete_milestone(
            project_id,
            _string(payload, "milestone_id"),
            expected_revision=payload_int(payload, "expected_revision", default=0),
        )
    if path == "/api/project-execution/task/assign":
        return workspace.assign_task(
            project_id,
            _string(payload, "task_id"),
            _task_metadata(payload),
            expected_revision=payload_int(payload, "expected_revision", default=0),
        )
    if path == "/api/project-execution/task/remove":
        return workspace.remove_task(
            project_id,
            _string(payload, "task_id"),
            expected_revision=payload_int(payload, "expected_revision", default=0),
        )
    return _MISSING


__all__ = ["dispatch_get", "dispatch_post"]
