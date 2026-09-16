"""Read-only diagnostics for Roadmap / Project Execution coordination.

The coordination journal and pending intent are recovery records, not editable
project definitions.  This module deliberately projects only bounded semantic
metadata needed to understand a split write: revisions/digests and the
Phase/Gate/Milestone Roadmap placement or canonical schedule that changed.
It never returns an arbitrary replacement document or merge expression.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping

_MAX_FORENSIC_SUBJECTS = 32


def content_digest(raw: Mapping[str, Any]) -> str:
    """Fingerprint semantic document content while excluding revision tokens."""

    payload = dict(raw)
    payload.pop("revision", None)
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _roadmap_item_summary(raw: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    return {
        "item_id": str(raw.get("id", "")),
        "kind": str(raw.get("kind", "")),
        "project_asset_id": str(raw.get("project_asset_id", "")),
        "lane": str(raw.get("lane", "General")),
        "order": int(raw.get("order", 0)),
    }


def _asset_schedule(raw: Mapping[str, Any], kind: str) -> dict[str, str]:
    schedule = raw.get("schedule")
    if not isinstance(schedule, Mapping):
        schedule = {}
    start = str(schedule.get("start", ""))
    target = str(raw.get("target", "")) if kind == "milestone" else str(
        schedule.get("target", "")
    )
    return {key: value for key, value in (("start", start), ("target", target)) if value}


def _project_asset_summary(
    raw: Mapping[str, Any] | None, kind: str
) -> dict[str, Any] | None:
    if raw is None:
        return None
    summary: dict[str, Any] = {
        "asset_id": str(raw.get("id", "")),
        "kind": kind,
        "title": str(raw.get("title", "")),
        "schedule": _asset_schedule(raw, kind),
    }
    if kind == "gate":
        criteria = raw.get("criteria")
        if isinstance(criteria, Mapping):
            criteria = criteria.get("all", [])
        summary["criterion_count"] = len(criteria) if isinstance(criteria, list) else 0
    elif kind == "phase":
        summary["task_count"] = len(raw.get("tasks", [])) if isinstance(raw.get("tasks"), list) else 0
    elif kind == "milestone":
        requires = raw.get("requires")
        if isinstance(requires, Mapping):
            summary["requirement_count"] = sum(
                len(requires.get(key, [])) if isinstance(requires.get(key), list) else 0
                for key in ("tasks", "gates", "milestones")
            )
    return summary


def _indexed_roadmap_items(raw: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    rows = raw.get("items")
    if not isinstance(rows, list):
        return {}
    return {
        str(row.get("id", "")): row
        for row in rows
        if isinstance(row, Mapping) and str(row.get("id", ""))
    }


def _indexed_assets(raw: Mapping[str, Any]) -> dict[tuple[str, str], Mapping[str, Any]]:
    result: dict[tuple[str, str], Mapping[str, Any]] = {}
    for plural, kind in (("phases", "phase"), ("gates", "gate"), ("milestones", "milestone")):
        rows = raw.get(plural)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, Mapping) and str(row.get("id", "")):
                result[(kind, str(row.get("id", "")))] = row
    return result


def build_forensic_record(
    *,
    before_roadmap: Mapping[str, Any],
    desired_roadmap: Mapping[str, Any],
    before_project_execution: Mapping[str, Any],
    desired_project_execution: Mapping[str, Any],
) -> dict[str, Any]:
    """Capture bounded before/desired metadata for subjects changed by an intent."""

    before_items = _indexed_roadmap_items(before_roadmap)
    desired_items = _indexed_roadmap_items(desired_roadmap)
    item_ids = sorted(
        key
        for key in set(before_items) | set(desired_items)
        if before_items.get(key) != desired_items.get(key)
    )
    before_assets = _indexed_assets(before_project_execution)
    desired_assets = _indexed_assets(desired_project_execution)
    asset_ids = sorted(
        key
        for key in set(before_assets) | set(desired_assets)
        if before_assets.get(key) != desired_assets.get(key)
    )
    truncated = len(item_ids) > _MAX_FORENSIC_SUBJECTS or len(asset_ids) > _MAX_FORENSIC_SUBJECTS
    item_ids = item_ids[:_MAX_FORENSIC_SUBJECTS]
    asset_ids = asset_ids[:_MAX_FORENSIC_SUBJECTS]
    return {
        "schema_version": 1,
        "truncated": truncated,
        "roadmap": [
            {
                "identity": {"item_id": item_id},
                "before": _roadmap_item_summary(before_items.get(item_id)),
                "desired": _roadmap_item_summary(desired_items.get(item_id)),
            }
            for item_id in item_ids
        ],
        "project_execution": [
            {
                "identity": {"kind": kind, "asset_id": asset_id},
                "before": _project_asset_summary(before_assets.get((kind, asset_id)), kind),
                "desired": _project_asset_summary(desired_assets.get((kind, asset_id)), kind),
            }
            for kind, asset_id in asset_ids
        ],
    }


def _current_roadmap_subjects(
    record: list[Mapping[str, Any]], current: Mapping[str, Any]
) -> list[dict[str, Any]]:
    index = _indexed_roadmap_items(current)
    rows: list[dict[str, Any]] = []
    for subject in record:
        identity = subject.get("identity")
        item_id = str(identity.get("item_id", "")) if isinstance(identity, Mapping) else ""
        rows.append(
            {
                "identity": {"item_id": item_id},
                "before": subject.get("before"),
                "desired": subject.get("desired"),
                "current": _roadmap_item_summary(index.get(item_id)),
            }
        )
    return rows


def _current_project_subjects(
    record: list[Mapping[str, Any]], current: Mapping[str, Any]
) -> list[dict[str, Any]]:
    index = _indexed_assets(current)
    rows: list[dict[str, Any]] = []
    for subject in record:
        identity = subject.get("identity")
        kind = str(identity.get("kind", "")) if isinstance(identity, Mapping) else ""
        asset_id = str(identity.get("asset_id", "")) if isinstance(identity, Mapping) else ""
        rows.append(
            {
                "identity": {"kind": kind, "asset_id": asset_id},
                "before": subject.get("before"),
                "desired": subject.get("desired"),
                "current": _project_asset_summary(index.get((kind, asset_id)), kind),
            }
        )
    return rows


def forensic_comparison(
    *,
    record: Mapping[str, Any],
    current_roadmap: Mapping[str, Any] | None,
    current_project_execution: Mapping[str, Any] | None,
    roadmap_before_revision: int,
    roadmap_before_digest: str,
    roadmap_desired_revision: int,
    roadmap_desired_digest: str,
    project_before_revision: int,
    project_before_digest: str,
    project_desired_revision: int,
    project_desired_digest: str,
) -> dict[str, Any]:
    """Project a safe three-way comparison without exposing writable documents."""

    available = int(record.get("schema_version", 0)) == 1
    roadmap_record = record.get("roadmap") if available else []
    execution_record = record.get("project_execution") if available else []
    if not isinstance(roadmap_record, list):
        roadmap_record = []
    if not isinstance(execution_record, list):
        execution_record = []
    current_roadmap = current_roadmap or {}
    current_project_execution = current_project_execution or {}
    return {
        "available": available,
        "legacy_reason": "" if available else "Pending intent predates semantic forensic capture.",
        "truncated": bool(record.get("truncated", False)) if available else False,
        "roadmap": {
            "before": {"revision": roadmap_before_revision, "digest": roadmap_before_digest},
            "desired": {"revision": roadmap_desired_revision, "digest": roadmap_desired_digest},
            "current": {
                "revision": int(current_roadmap.get("revision", 0)),
                "digest": content_digest(current_roadmap) if current_roadmap else "",
            },
            "subjects": _current_roadmap_subjects(roadmap_record, current_roadmap),
        },
        "project_execution": {
            "before": {"revision": project_before_revision, "digest": project_before_digest},
            "desired": {"revision": project_desired_revision, "digest": project_desired_digest},
            "current": {
                "revision": int(current_project_execution.get("revision", 0)),
                "digest": content_digest(current_project_execution) if current_project_execution else "",
            },
            "subjects": _current_project_subjects(execution_record, current_project_execution),
        },
    }


@dataclass(frozen=True)
class CoordinationDocumentStatus:
    """Observed state of one durable document relative to a pending intent."""

    state: str
    expected_revision: int
    current_revision: int
    desired_revision: int
    recorded_result_revision: int

    def as_mapping(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "expected_revision": self.expected_revision,
            "current_revision": self.current_revision,
            "desired_revision": self.desired_revision,
            "recorded_result_revision": self.recorded_result_revision,
        }


@dataclass(frozen=True)
class RoadmapCoordinationStatus:
    """Read-only operator diagnosis for one pending canonical mutation."""

    pending: bool
    project_id: str
    roadmap_id: str = ""
    operation_id: str = ""
    operation: str = ""
    phase: str = ""
    created_at: str = ""
    updated_at: str = ""
    roadmap: CoordinationDocumentStatus | None = None
    project_execution: CoordinationDocumentStatus | None = None
    safe_actions: tuple[str, ...] = ()
    message: str = ""
    forensics: Mapping[str, Any] = field(default_factory=dict)

    @property
    def automatic_action_available(self) -> bool:
        return bool(
            {"retry_roll_forward", "accept_applied", "finalize_terminal"}.intersection(
                self.safe_actions
            )
        )

    @property
    def divergent(self) -> bool:
        return bool(
            self.pending
            and (
                self.roadmap is None
                or self.project_execution is None
                or "divergent" in {self.roadmap.state, self.project_execution.state}
            )
        )

    def as_mapping(self) -> dict[str, Any]:
        return {
            "pending": self.pending,
            "project_id": self.project_id,
            "roadmap_id": self.roadmap_id,
            "operation_id": self.operation_id,
            "operation": self.operation,
            "phase": self.phase,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "roadmap": self.roadmap.as_mapping() if self.roadmap else None,
            "project_execution": (
                self.project_execution.as_mapping() if self.project_execution else None
            ),
            "safe_actions": list(self.safe_actions),
            "automatic_action_available": self.automatic_action_available,
            "divergent": self.divergent,
            "message": self.message,
            "forensics": dict(self.forensics),
        }


__all__ = [
    "CoordinationDocumentStatus",
    "RoadmapCoordinationStatus",
    "build_forensic_record",
    "content_digest",
    "forensic_comparison",
]
