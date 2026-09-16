"""Build durable, human-readable execution traces for Work Package inspection.

The orchestrator already keeps two complementary append-only histories:

* :mod:`execraft.orchestrate.invocations` records exact provider attempts, and
* :mod:`execraft.orchestrate.journal` records scheduler, operator, verification,
  failover, and stage-transition events.

This module joins those histories into a bounded UI projection.  It deliberately
never mutates orchestration state and remains useful for partially historical
runs created before structured stage-transition events were introduced.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from .event_compat import canonical_task_event_type, task_event_work_package_id
from .invocations import AgentInvocationRecord
from .journal import JournalEntry
from .models import WorkPackage, WorkPackageStage


_PROVIDER_STAGES = frozenset(
    {
        WorkPackageStage.DECOMPOSE.value,
        WorkPackageStage.IMPLEMENT.value,
        WorkPackageStage.REVIEW.value,
        WorkPackageStage.FIX_REVIEW.value,
        WorkPackageStage.FINAL_REVIEW.value,
    }
)
_DETERMINISTIC_STAGES = frozenset(
    {
        WorkPackageStage.PREPARE.value,
        WorkPackageStage.FAST_VERIFY.value,
        WorkPackageStage.TARGETED_VERIFY.value,
        WorkPackageStage.REGRESSION_VERIFY.value,
        WorkPackageStage.FULL_VERIFY.value,
        WorkPackageStage.READY_TO_COMMIT.value,
    }
)
_TRACE_EVENT_TYPES = frozenset(
    {
        "agent_failover",
        "agent_contract_retry",
        "agent_wait_scheduled",
        "agent_availability_recovered",
        "package_operator_pause_changed",
        "work_package_pause_before_start_reached",
        "mandatory_decomposition_consumed",
        "package_decomposition_requested",
        "review_recovery_playbook_queued",
        "supervisor_incident_resolved",
        "human_intervention_required",
        "repository_scope_check_reconciled",
        "write_scope_expansion_approved",
    }
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _seconds_between(start: object, end: object) -> float:
    left = _parse_timestamp(start)
    right = _parse_timestamp(end)
    if left is None or right is None:
        return 0.0
    return max(0.0, (right - left).total_seconds())


def _event_package_id(entry: JournalEntry) -> str:
    return task_event_work_package_id(entry.payload)


def _display_stage(stage: str) -> str:
    return str(stage or "").replace("_", " ").strip().title()


def _transition_reason(from_stage: str, to_stage: str) -> str:
    if from_stage == to_stage:
        return "Stage resumed"
    if to_stage == WorkPackageStage.FIX_REVIEW.value:
        if from_stage in {
            WorkPackageStage.REVIEW.value,
            WorkPackageStage.FINAL_REVIEW.value,
        }:
            return "Changes requested"
        return "Verification failed"
    if from_stage == WorkPackageStage.FIX_REVIEW.value:
        return "Fix completed"
    if to_stage == WorkPackageStage.REGRESSION_VERIFY.value:
        return "Regression required"
    if to_stage == WorkPackageStage.FINAL_REVIEW.value:
        return "Ready for final review"
    if to_stage == WorkPackageStage.FULL_VERIFY.value:
        return "Final review passed"
    if to_stage == WorkPackageStage.READY_TO_COMMIT.value:
        return "Quality checks passed"
    if to_stage == WorkPackageStage.COMPLETED.value:
        return "Completed"
    if to_stage == WorkPackageStage.DECOMPOSE.value:
        return "Decomposition required"
    if from_stage == WorkPackageStage.IMPLEMENT.value:
        return "Implementation completed"
    if "verify" in from_stage:
        return "Verification passed"
    if from_stage in {
        WorkPackageStage.REVIEW.value,
        WorkPackageStage.FINAL_REVIEW.value,
    }:
        return "Review passed"
    return f"{_display_stage(from_stage)} completed"


def _transition_outcome(from_stage: str, to_stage: str) -> str:
    if to_stage == WorkPackageStage.FIX_REVIEW.value:
        return "changes_requested" if "review" in from_stage else "failed"
    if to_stage == WorkPackageStage.IMPLEMENT.value and "verify" in from_stage:
        return "failed"
    if to_stage == WorkPackageStage.COMPLETED.value:
        return "completed"
    if "verify" in from_stage or "review" in from_stage:
        return "passed"
    return "completed"


def _result_outcome(record: AgentInvocationRecord) -> str:
    if record.status == "running":
        return "running"
    if record.status != "completed":
        return str(record.failure.get("classification") or record.status or "failed")
    result = record.normalized_result
    for key in ("verdict", "outcome", "decision", "status"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower().replace(" ", "_")
    if record.validation_errors:
        return "validation_failed"
    return "completed"


def _metrics_from_invocation(record: AgentInvocationRecord) -> dict[str, Any]:
    """Expose normalized usage first, then legacy provider metrics."""

    metrics: dict[str, Any] = {}
    usage = record.usage if isinstance(record.usage, Mapping) else {}
    normalized_usage = {
        "tokens": usage.get("total_tokens"),
        "input_tokens": usage.get("input_tokens") or usage.get("estimated_input_tokens"),
        "cached_tokens": usage.get("cached_input_tokens") or usage.get("cache_read_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "reasoning_tokens": usage.get("reasoning_tokens"),
        "prompt_bytes": usage.get("prompt_bytes"),
        "cost": usage.get("reported_cost") or usage.get("estimated_cost"),
        "currency": usage.get("currency"),
    }
    for key, value in normalized_usage.items():
        if value not in (None, "", 0, 0.0):
            metrics[key] = value

    candidates: list[Mapping[str, Any]] = []
    for value in (record.normalized_result, record.result_artifact, record.isolation):
        if isinstance(value, Mapping):
            candidates.append(value)
            nested = value.get("metrics")
            if isinstance(nested, Mapping):
                candidates.append(nested)
    aliases = {
        "events": ("events", "event_count"),
        "files": ("files", "files_changed", "file_count"),
        "commands": ("commands", "command_count"),
        "tools": ("tools_done", "tools", "tool_count"),
        "tokens": ("tokens", "total_tokens", "token_count"),
    }
    for label, keys in aliases.items():
        if label in metrics:
            continue
        for mapping in candidates:
            value = next((mapping.get(key) for key in keys if key in mapping), None)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                metrics[label] = value
                break
            if isinstance(value, list):
                metrics[label] = len(value)
                break
    return metrics


def _annotation(entry: JournalEntry) -> dict[str, Any]:
    payload = dict(entry.payload)
    event_type = canonical_task_event_type(entry.event_type)
    labels = {
        "agent_failover": "Provider fallback",
        "agent_contract_retry": "Structured-output retry",
        "agent_wait_scheduled": "Waiting for an eligible agent",
        "agent_availability_recovered": "Agent availability recovered",
        "package_operator_pause_changed": "Operator pause changed",
        "work_package_pause_before_start_reached": "Planned pause reached",
        "mandatory_decomposition_consumed": "Mandatory decomposition consumed",
        "package_decomposition_requested": "Decomposition requested",
        "review_recovery_playbook_queued": "Review recovery queued",
        "supervisor_incident_resolved": "Supervisor recovery resolved",
        "human_intervention_required": "Human intervention required",
        "repository_scope_check_reconciled": "Repository scope reconciled",
        "write_scope_expansion_approved": "Write scope expanded",
    }
    detail = ""
    if event_type == "agent_failover":
        detail = " → ".join(
            item
            for item in (
                str(payload.get("from_agent", "")).strip(),
                str(payload.get("to_agent", "")).strip(),
            )
            if item
        )
    elif event_type == "agent_contract_retry":
        detail = str(payload.get("to_agent") or payload.get("agent_id") or "").strip()
    elif event_type == "package_operator_pause_changed":
        detail = "Paused" if payload.get("paused") else "Resumed"
    else:
        detail = str(
            payload.get("reason")
            or payload.get("summary")
            or payload.get("outcome")
            or payload.get("classification")
            or ""
        ).strip()
    return {
        "id": f"journal-{entry.sequence}",
        "event_type": event_type,
        "label": labels.get(event_type, _display_stage(event_type)),
        "detail": detail,
        "timestamp": entry.timestamp,
        "payload": payload,
    }


@dataclass(frozen=True)
class _Transition:
    sequence: int
    package_id: str
    from_stage: str
    to_stage: str
    timestamp: str
    reason: str
    payload: dict[str, Any]


def _stage_transitions(
    entries: Iterable[JournalEntry], package_ids: set[str]
) -> list[_Transition]:
    transitions: list[_Transition] = []
    for entry in entries:
        if canonical_task_event_type(entry.event_type) != "package_stage_transition":
            continue
        package_id = _event_package_id(entry)
        if package_id not in package_ids:
            continue
        payload = dict(entry.payload)
        from_stage = str(payload.get("from_stage", "")).strip()
        to_stage = str(payload.get("to_stage", "")).strip()
        if not from_stage or not to_stage or from_stage == to_stage:
            continue
        raw_reason = str(payload.get("reason", "")).strip()
        reason_aliases = {
            "package_completed": "Completed",
            "supervisor_recovery": "Supervisor recovery",
        }
        reason = (
            reason_aliases.get(raw_reason)
            or (raw_reason.replace("_", " ").strip().title() if raw_reason else "")
            or _transition_reason(from_stage, to_stage)
        )
        transitions.append(
            _Transition(
                sequence=entry.sequence,
                package_id=package_id,
                from_stage=from_stage,
                to_stage=to_stage,
                timestamp=entry.timestamp,
                reason=reason,
                payload=payload,
            )
        )
    return sorted(transitions, key=lambda item: (item.timestamp, item.sequence))


def trace_scope_packages(
    selected: WorkPackage, packages: Sequence[WorkPackage]
) -> list[WorkPackage]:
    by_parent: dict[str, list[WorkPackage]] = {}
    for package in packages:
        if package.parent_id:
            by_parent.setdefault(package.parent_id, []).append(package)
    result: list[WorkPackage] = []
    pending = [selected]
    emitted: set[str] = set()
    while pending:
        package = pending.pop(0)
        if package.id in emitted:
            continue
        emitted.add(package.id)
        result.append(package)
        children = sorted(
            by_parent.get(package.id, []),
            key=lambda item: (item.priority, item.shard_key, item.id),
        )
        pending.extend(children)
    return result


def _first_package_timestamp(
    package_id: str,
    invocations: Sequence[AgentInvocationRecord],
    entries: Sequence[JournalEntry],
) -> str:
    values = [
        record.started_at
        for record in invocations
        if record.package_id == package_id and record.started_at
    ]
    values.extend(
        entry.timestamp
        for entry in entries
        if _event_package_id(entry) == package_id
        and canonical_task_event_type(entry.event_type)
        in {
            "package_started",
            "package_decomposition_requested",
            "work_package_pause_before_start_reached",
        }
    )
    parsed = [(value, _parse_timestamp(value)) for value in values]
    valid = [(value, stamp) for value, stamp in parsed if stamp is not None]
    return min(valid, key=lambda item: item[1])[0] if valid else ""


def _find_transition_after(
    transitions: Sequence[_Transition],
    package_id: str,
    stage: str,
    timestamp: str,
) -> _Transition | None:
    start = _parse_timestamp(timestamp)
    candidates = [
        transition
        for transition in transitions
        if transition.package_id == package_id and transition.from_stage == stage
    ]
    if start is None:
        return candidates[0] if candidates else None
    for transition in candidates:
        stamp = _parse_timestamp(transition.timestamp)
        if stamp is not None and stamp >= start:
            return transition
    return None


def _invocation_node(
    record: AgentInvocationRecord,
    transitions: Sequence[_Transition],
    *,
    now: str,
) -> dict[str, Any]:
    transition = (
        _find_transition_after(
            transitions, record.package_id, record.stage, record.started_at
        )
        if record.status == "completed"
        else None
    )
    completed_at = record.completed_at or (now if record.status == "running" else "")
    duration = (
        _seconds_between(record.started_at, now)
        if record.status == "running"
        else max(
            record.duration_seconds,
            _seconds_between(record.started_at, completed_at),
        )
    )
    outcome = _result_outcome(record)
    if record.status == "completed" and transition is not None:
        outcome = _transition_outcome(transition.from_stage, transition.to_stage)
    artifact_path = str(record.result_artifact.get("path", "")).strip()
    return {
        "id": f"invocation-{record.invocation_id}",
        "kind": "agent",
        "package_id": record.package_id,
        "stage": record.stage,
        "stage_label": _display_stage(record.stage),
        "status": record.status,
        "outcome": outcome,
        "attempt": record.attempt,
        "agent_id": record.agent_id,
        "adapter": record.adapter,
        "model": record.model,
        "started_at": record.started_at,
        "completed_at": completed_at,
        "duration_seconds": round(duration, 3),
        "invocation_id": record.invocation_id,
        "parent_invocation_id": record.parent_invocation_id,
        "capability": record.capability,
        "transition_to": transition.to_stage if transition else "",
        "transition_reason": transition.reason if transition else "",
        "metrics": _metrics_from_invocation(record),
        "skills": [dict(item) for item in record.skills],
        "validation_errors": list(record.validation_errors),
        "failure": dict(record.failure),
        "artifact_path": artifact_path,
        "artifact": dict(record.result_artifact),
        "live": record.status == "running",
    }


def _deterministic_nodes(
    package: WorkPackage,
    transitions: Sequence[_Transition],
    invocations: Sequence[AgentInvocationRecord],
    entries: Sequence[JournalEntry],
    *,
    now: str,
) -> list[dict[str, Any]]:
    package_transitions = [
        item for item in transitions if item.package_id == package.id
    ]
    nodes: list[dict[str, Any]] = []
    entered_at: dict[str, str] = {}
    first_timestamp = _first_package_timestamp(package.id, invocations, entries)
    if first_timestamp:
        entered_at[WorkPackageStage.PREPARE.value] = first_timestamp

    for transition in package_transitions:
        transition_stamp = _parse_timestamp(transition.timestamp)
        start = entered_at.get(transition.from_stage, "")
        if not start:
            previous: list[_Transition] = []
            for item in package_transitions:
                item_stamp = _parse_timestamp(item.timestamp)
                if (
                    item.to_stage == transition.from_stage
                    and item_stamp is not None
                    and transition_stamp is not None
                    and item_stamp <= transition_stamp
                ):
                    previous.append(item)
            if previous:
                start = previous[-1].timestamp
        if not start:
            start = transition.timestamp

        start_stamp = _parse_timestamp(start)
        provider_attempt = any(
            record.package_id == package.id
            and record.stage == transition.from_stage
            and (record_stamp := _parse_timestamp(record.started_at)) is not None
            and start_stamp is not None
            and transition_stamp is not None
            and start_stamp <= record_stamp <= transition_stamp
            for record in invocations
        )
        include = transition.from_stage in _DETERMINISTIC_STAGES or (
            transition.from_stage in _PROVIDER_STAGES and not provider_attempt
        )
        duration = _seconds_between(start, transition.timestamp)
        if include and (
            duration > 0
            or transition.from_stage != WorkPackageStage.PREPARE.value
        ):
            nodes.append(
                {
                    "id": f"transition-{package.id}-{transition.sequence}",
                    "kind": "deterministic",
                    "package_id": package.id,
                    "stage": transition.from_stage,
                    "stage_label": _display_stage(transition.from_stage),
                    "status": "completed",
                    "outcome": _transition_outcome(
                        transition.from_stage, transition.to_stage
                    ),
                    "attempt": 1,
                    "agent_id": "",
                    "adapter": "",
                    "model": "",
                    "started_at": start,
                    "completed_at": transition.timestamp,
                    "duration_seconds": round(duration, 3),
                    "invocation_id": "",
                    "parent_invocation_id": "",
                    "capability": "deterministic",
                    "transition_to": transition.to_stage,
                    "transition_reason": transition.reason,
                    "metrics": {},
                    "skills": [],
                    "validation_errors": [],
                    "failure": {},
                    "artifact_path": "",
                    "artifact": {},
                    "live": False,
                }
            )
        entered_at[transition.to_stage] = transition.timestamp

    active_stage = package.stage.value
    has_active_invocation = any(
        record.package_id == package.id
        and record.stage == active_stage
        and record.status == "running"
        for record in invocations
    )
    if (
        active_stage in _DETERMINISTIC_STAGES
        and active_stage != WorkPackageStage.COMPLETED.value
        and not has_active_invocation
    ):
        started_at = entered_at.get(active_stage, "")
        if started_at:
            nodes.append(
                {
                    "id": f"active-{package.id}-{active_stage}",
                    "kind": "deterministic",
                    "package_id": package.id,
                    "stage": active_stage,
                    "stage_label": _display_stage(active_stage),
                    "status": "running" if package.status != "blocked" else "blocked",
                    "outcome": "running" if package.status != "blocked" else "blocked",
                    "attempt": 1,
                    "agent_id": "",
                    "adapter": "",
                    "model": "",
                    "started_at": started_at,
                    "completed_at": now,
                    "duration_seconds": round(_seconds_between(started_at, now), 3),
                    "invocation_id": "",
                    "parent_invocation_id": "",
                    "capability": "deterministic",
                    "transition_to": "",
                    "transition_reason": "",
                    "metrics": {},
                    "skills": [],
                    "validation_errors": [],
                    "failure": {},
                    "artifact_path": "",
                    "artifact": {},
                    "live": package.status not in {"blocked", "completed"},
                }
            )
    return nodes


def _incoming_reason(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    annotations: Sequence[Mapping[str, Any]],
) -> str:
    previous_end = _parse_timestamp(
        previous.get("completed_at") or previous.get("started_at")
    )
    current_start = _parse_timestamp(current.get("started_at"))
    relevant: list[Mapping[str, Any]] = []
    if previous_end is not None and current_start is not None:
        relevant = [
            item
            for item in annotations
            if (stamp := _parse_timestamp(item.get("timestamp"))) is not None
            and previous_end <= stamp <= current_start
        ]
    event_types = {str(item.get("event_type", "")) for item in relevant}
    reason = str(previous.get("transition_reason", "")).strip()
    if not reason and previous.get("stage") != current.get("stage"):
        reason = _transition_reason(
            str(previous.get("stage", "")), str(current.get("stage", ""))
        )
    if "agent_failover" in event_types:
        return f"{reason} · fallback" if reason else "Provider fallback"
    if "agent_contract_retry" in event_types:
        return f"{reason} · retry" if reason else "Retry structured output"
    if previous.get("stage") == current.get("stage"):
        return "Retry"
    return reason


def _interval_union_seconds(nodes: Sequence[Mapping[str, Any]]) -> float:
    intervals: list[tuple[datetime, datetime]] = []
    for node in nodes:
        start = _parse_timestamp(node.get("started_at"))
        end = _parse_timestamp(node.get("completed_at"))
        if start is not None and end is not None and end >= start:
            intervals.append((start, end))
    if not intervals:
        return 0.0
    intervals.sort(key=lambda item: item[0])
    merged: list[list[datetime]] = []
    for start, end in intervals:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        elif end > merged[-1][1]:
            merged[-1][1] = end
    return sum((end - start).total_seconds() for start, end in merged)


def build_execution_trace(
    *,
    selected: WorkPackage,
    packages: Sequence[WorkPackage],
    invocations: Sequence[AgentInvocationRecord],
    journal_entries: Sequence[JournalEntry],
    now: str | None = None,
    warnings: Sequence[str] = (),
) -> dict[str, Any]:
    """Return a bounded trace projection for one Work Package.

    Selecting a parent Work Package includes all recursively generated shards as
    separate swimlanes. Selecting a shard remains focused on that package.
    """

    generated_at = now or _now_iso()
    scope = trace_scope_packages(selected, packages)
    scope_ids = {package.id for package in scope}
    scoped_invocations = [
        record for record in invocations if record.package_id in scope_ids
    ]
    scoped_entries = [
        entry for entry in journal_entries if _event_package_id(entry) in scope_ids
    ]
    transitions = _stage_transitions(scoped_entries, scope_ids)
    annotations_by_package: dict[str, list[dict[str, Any]]] = {
        package.id: [] for package in scope
    }
    for entry in scoped_entries:
        if canonical_task_event_type(entry.event_type) in _TRACE_EVENT_TYPES:
            annotations_by_package.setdefault(_event_package_id(entry), []).append(
                _annotation(entry)
            )

    lanes: list[dict[str, Any]] = []
    all_nodes: list[dict[str, Any]] = []
    for package in scope:
        package_invocations = [
            record for record in scoped_invocations if record.package_id == package.id
        ]
        nodes = [
            _invocation_node(record, transitions, now=generated_at)
            for record in package_invocations
        ]
        nodes.extend(
            _deterministic_nodes(
                package,
                transitions,
                package_invocations,
                scoped_entries,
                now=generated_at,
            )
        )
        nodes.sort(
            key=lambda item: (
                _parse_timestamp(item.get("started_at"))
                or datetime.max.replace(tzinfo=timezone.utc),
                0 if item.get("kind") == "agent" else 1,
                str(item.get("id", "")),
            )
        )
        annotations = sorted(
            annotations_by_package.get(package.id, []),
            key=lambda item: _parse_timestamp(item.get("timestamp"))
            or datetime.max.replace(tzinfo=timezone.utc),
        )
        for index, node in enumerate(nodes):
            node["sequence"] = index + 1
            node["incoming_reason"] = (
                _incoming_reason(nodes[index - 1], node, annotations)
                if index > 0
                else ""
            )
        lane = {
            "package_id": package.id,
            "title": package.title,
            "parent_id": package.parent_id,
            "shard_key": package.shard_key,
            "current_stage": package.stage.value,
            "status": package.status,
            "parallel_safe": package.parallel_safe,
            "nodes": nodes,
            "annotations": annotations,
        }
        lanes.append(lane)
        all_nodes.extend(nodes)

    all_nodes.sort(
        key=lambda item: _parse_timestamp(item.get("started_at"))
        or datetime.max.replace(tzinfo=timezone.utc)
    )
    starts = [
        stamp
        for item in all_nodes
        if (stamp := _parse_timestamp(item.get("started_at"))) is not None
    ]
    ends = [
        stamp
        for item in all_nodes
        if (stamp := _parse_timestamp(item.get("completed_at"))) is not None
    ]
    wall_clock = (
        max(0.0, (max(ends) - min(starts)).total_seconds()) if starts and ends else 0.0
    )
    active_seconds = _interval_union_seconds(all_nodes)
    agent_work = sum(
        float(item.get("duration_seconds", 0) or 0)
        for item in all_nodes
        if item.get("kind") == "agent"
    )
    deterministic_work = sum(
        float(item.get("duration_seconds", 0) or 0)
        for item in all_nodes
        if item.get("kind") == "deterministic"
    )
    failovers = sum(
        1
        for items in annotations_by_package.values()
        for item in items
        if item.get("event_type") == "agent_failover"
    )
    review_loops = sum(
        1
        for item in transitions
        if item.to_stage == WorkPackageStage.FIX_REVIEW.value
    )
    agents = sorted(
        {
            str(item.get("agent_id", "")).strip()
            for item in all_nodes
            if str(item.get("agent_id", "")).strip()
        }
    )
    transition_packages = {item.package_id for item in transitions}
    packages_requiring_history = {
        package.id
        for package in scope
        if package.stage != WorkPackageStage.PREPARE
        or package.status not in {"pending", "queued"}
        or any(record.package_id == package.id for record in scoped_invocations)
    }
    history_incomplete = bool(packages_requiring_history - transition_packages)
    return {
        "schema_version": 1,
        "generated_at": generated_at,
        "package_id": selected.id,
        "title": selected.title,
        "scope_package_ids": [package.id for package in scope],
        "history_complete": not history_incomplete,
        "warnings": [str(item) for item in warnings if str(item).strip()],
        "summary": {
            "wall_clock_seconds": round(wall_clock, 3),
            "active_wall_clock_seconds": round(active_seconds, 3),
            "waiting_seconds": round(max(0.0, wall_clock - active_seconds), 3),
            "agent_work_seconds": round(agent_work, 3),
            "deterministic_seconds": round(deterministic_work, 3),
            "attempt_count": sum(1 for item in all_nodes if item.get("kind") == "agent"),
            "stage_count": len(all_nodes),
            "agents_involved": agents,
            "review_loops": review_loops,
            "provider_fallbacks": failovers,
            "package_count": len(scope),
            "running_count": sum(1 for item in all_nodes if item.get("live")),
            "failed_count": sum(
                1
                for item in all_nodes
                if item.get("status") == "failed"
                or item.get("outcome") in {"failed", "validation_failed"}
            ),
        },
        "lanes": lanes,
    }
