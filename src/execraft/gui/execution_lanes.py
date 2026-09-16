"""Presentation model for execution lanes in the graph-first workbench.

An execution lane groups scheduler profiles that resolve to the same runtime,
model route, and execution target.  The grouping is deliberately presentation
only: scheduler profile IDs remain the canonical routing identities and lane IDs
must never be persisted into orchestration state.

The helpers in this module accept browser-safe profile rows rather than the
scheduler domain model.  That lets both the task snapshot and the passive runtime
topology endpoint reuse one deterministic projection without introducing a new
orchestration dependency into the GUI layer.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from hashlib import sha256
import re
from typing import Any, Iterable, Mapping, Sequence


_HEALTH_PRECEDENCE: Mapping[str, int] = {
    "available": 0,
    "healthy": 0,
    "unknown": 1,
    "probe_due": 2,
    "cooldown": 3,
    "blocked": 4,
    "unhealthy": 4,
    "failed": 5,
    "unsupported": 6,
    "disabled": 7,
}


@dataclass(frozen=True)
class ExecutionLaneAssignmentView:
    """Compact active-assignment data safe to expose in the Run workbench."""

    package_id: str
    role: str
    status: str = ""
    agent_id: str = ""

    def as_mapping(self) -> dict[str, str]:
        return {
            "package_id": self.package_id,
            "role": self.role,
            "status": self.status,
            "agent_id": self.agent_id,
        }


@dataclass(frozen=True)
class ExecutionLaneView:
    """Secret-free presentation aggregation over one or more agent profiles.

    ``id`` is a deterministic presentation identity only. ``profile_ids`` keep
    the bridge to the existing scheduler model explicit without turning the lane
    into a scheduling authority.
    """

    id: str
    display_name: str
    runtime_id: str
    runtime_kind: str
    model_route_id: str | None = None
    model_display_name: str = ""
    target_id: str | None = None
    target_display_name: str = ""
    roles: tuple[str, ...] = ()
    profile_ids: tuple[str, ...] = ()
    health: str = "unknown"
    availability: str = "unknown"
    active_assignments: tuple[ExecutionLaneAssignmentView, ...] = ()
    diagnostics_summary: str | None = None

    def as_mapping(self) -> dict[str, Any]:
        """Return the browser-facing shape without any credential material."""

        return {
            "id": self.id,
            "display_name": self.display_name,
            "runtime_id": self.runtime_id,
            "runtime_kind": self.runtime_kind,
            "model_route_id": self.model_route_id,
            "model_display_name": self.model_display_name,
            "target_id": self.target_id,
            "target_display_name": self.target_display_name,
            "roles": list(self.roles),
            "profile_ids": list(self.profile_ids),
            "health": self.health,
            "availability": self.availability,
            "active_assignments": [item.as_mapping() for item in self.active_assignments],
            "diagnostics_summary": self.diagnostics_summary,
        }


def _text(value: object) -> str:
    return str(value or "").strip()


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized[:42] or "lane"


def _lane_id(runtime_id: str, model_route_id: str, target_id: str) -> str:
    """Return a presentation identity derived only from the grouping tuple."""

    key = "\x1f".join((runtime_id, model_route_id, target_id))
    digest = sha256(key.encode("utf-8")).hexdigest()[:10]
    readable = "-".join(
        item for item in (runtime_id, model_route_id or "direct", target_id or "local") if item
    )
    return f"lane-{_slug(readable)}-{digest}"


def _roles_for_capabilities(
    capabilities: set[str], execution_roles: Sequence[Mapping[str, Any]]
) -> tuple[str, ...]:
    roles: list[str] = []
    for role in execution_roles:
        role_id = _text(role.get("id"))
        capability = _text(role.get("capability"))
        if role_id and capability in capabilities and role_id not in roles:
            roles.append(role_id)
    return tuple(roles)


def _profile_health(row: Mapping[str, Any]) -> tuple[bool, str, bool]:
    """Return ``(known, status, usable)`` for one profile health projection."""

    raw = row.get("health")
    if not isinstance(raw, Mapping):
        return False, "unknown", False
    status = _text(raw.get("status")) or "unknown"
    if "available" in raw:
        usable = bool(raw.get("available"))
    else:
        usable = status in {"available", "healthy"}
    return True, status, usable


def _aggregate_health(rows: Sequence[Mapping[str, Any]]) -> tuple[str, str, str | None]:
    """Aggregate lane health with deterministic precedence.

    Availability answers whether the lane can currently be selected by product
    policy and health. Health preserves the most severe profile status so a lane
    with one healthy role profile and one cooled-down role profile is visibly
    degraded instead of falsely reported as fully healthy.

    Precedence (worst first): disabled/unsupported, failed, blocked/unhealthy,
    cooldown, probe_due, unknown, available/healthy.
    """

    enabled = [row for row in rows if bool(row.get("enabled", True))]
    if not enabled:
        return "disabled", "disabled", "All profiles in this lane are disabled."

    supported = [
        row
        for row in enabled
        if not isinstance(row.get("support"), Mapping)
        or row.get("support", {}).get("supported") is not False
    ]
    if not supported:
        return (
            "unsupported",
            "unsupported",
            "Product support policy excludes every profile in this lane.",
        )

    states = [_profile_health(row) for row in supported]
    if not any(known for known, _status, _usable in states):
        return "unknown", "unknown", None

    statuses = [status for known, status, _usable in states if known]
    worst = max(statuses, key=lambda item: _HEALTH_PRECEDENCE.get(item, 1))
    usable = sum(1 for known, _status, available in states if known and available)
    known = sum(1 for is_known, _status, _available in states if is_known)
    clean = sum(
        1
        for is_known, status, available in states
        if is_known and available and status in {"available", "healthy"}
    )

    if usable == 0:
        availability = "unavailable"
    elif usable == known and clean == known:
        availability = "ready"
    else:
        availability = "degraded"

    summary = None
    if availability != "ready":
        summary = f"{usable}/{known} health-reported profiles currently available."
    return worst, availability, summary


def _display_fields(rows: Sequence[Mapping[str, Any]]) -> tuple[str, str, str]:
    first = rows[0]
    runtime_id = _text(first.get("runtime_id")) or "runtime"
    runtime_kind = _text(first.get("runtime_kind")) or _text(first.get("adapter"))
    model = _text(first.get("model")) or _text(first.get("model_route_id")) or runtime_id
    target = _text(first.get("target_display_name")) or _text(first.get("target_id"))
    if not target:
        endpoint = first.get("endpoint")
        if isinstance(endpoint, Mapping):
            target = _text(endpoint.get("target_id"))
    display = model if not target else f"{model} · {target}"
    return display, model, target


def _assignment_views(rows: Sequence[Mapping[str, Any]]) -> tuple[ExecutionLaneAssignmentView, ...]:
    result: list[ExecutionLaneAssignmentView] = []
    seen: set[tuple[str, str, str, str]] = set()
    for row in rows:
        agent_id = _text(row.get("id"))
        assignments = row.get("assignments")
        if not isinstance(assignments, Iterable) or isinstance(assignments, (str, bytes, Mapping)):
            continue
        for assignment in assignments:
            if not isinstance(assignment, Mapping):
                continue
            item = ExecutionLaneAssignmentView(
                package_id=_text(assignment.get("package_id")),
                role=_text(assignment.get("stage")) or _text(assignment.get("model_role")),
                status=_text(assignment.get("status")),
                agent_id=_text(assignment.get("agent_id")) or agent_id,
            )
            key = (item.package_id, item.role, item.status, item.agent_id)
            if item.package_id and key not in seen:
                seen.add(key)
                result.append(item)
    return tuple(result)


def build_execution_lane_views(
    profile_rows: Iterable[Mapping[str, Any]],
    execution_roles: Sequence[Mapping[str, Any]],
) -> tuple[ExecutionLaneView, ...]:
    """Group profile rows by effective runtime/model-route/target tuple.

    Input rows may come from the richer task execution-health projection or the
    passive runtime-topology projection. Missing health therefore yields
    ``unknown`` rather than incorrectly marking the lane unavailable.
    """

    groups: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in profile_rows:
        if not isinstance(row, Mapping):
            continue
        runtime_id = _text(row.get("runtime_id"))
        model_route_id = _text(row.get("model_route_id"))
        target_id = _text(row.get("target_id"))
        if not runtime_id:
            continue
        groups[(runtime_id, model_route_id, target_id)].append(row)

    lanes: list[ExecutionLaneView] = []
    for (runtime_id, model_route_id, target_id), rows in groups.items():
        rows = sorted(rows, key=lambda item: (-int(item.get("priority", 0) or 0), _text(item.get("id"))))
        display_name, model_display, target_display = _display_fields(rows)
        role_rows = [
            row
            for row in rows
            if bool(row.get("enabled", True))
            and (
                not isinstance(row.get("support"), Mapping)
                or row.get("support", {}).get("supported") is not False
            )
        ]
        capabilities = {
            _text(capability)
            for row in role_rows
            for capability in (row.get("capabilities") or ())
            if _text(capability)
        }
        roles = _roles_for_capabilities(capabilities, execution_roles)
        profile_ids = tuple(_text(row.get("id")) for row in rows if _text(row.get("id")))
        health, availability, diagnostics = _aggregate_health(rows)
        lanes.append(
            ExecutionLaneView(
                id=_lane_id(runtime_id, model_route_id, target_id),
                display_name=display_name,
                runtime_id=runtime_id,
                runtime_kind=_text(rows[0].get("runtime_kind")) or _text(rows[0].get("adapter")),
                model_route_id=model_route_id or None,
                model_display_name=model_display,
                target_id=target_id or None,
                target_display_name=target_display,
                roles=roles,
                profile_ids=profile_ids,
                health=health,
                availability=availability,
                active_assignments=_assignment_views(rows),
                diagnostics_summary=diagnostics,
            )
        )

    return tuple(
        sorted(
            lanes,
            key=lambda lane: (
                lane.availability not in {"ready", "degraded"},
                lane.display_name.lower(),
                lane.id,
            ),
        )
    )


def execution_lane_mappings(
    profile_rows: Iterable[Mapping[str, Any]],
    execution_roles: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """JSON-ready convenience wrapper used by GUI snapshot presenters."""

    return [lane.as_mapping() for lane in build_execution_lane_views(profile_rows, execution_roles)]


__all__ = [
    "ExecutionLaneAssignmentView",
    "ExecutionLaneView",
    "build_execution_lane_views",
    "execution_lane_mappings",
]
