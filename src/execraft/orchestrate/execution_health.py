"""Persistent health for independent execution dimensions.

Provider health remains readable during the schema-v3 migration, but it can no
longer represent every failure correctly once runtime, model route and physical
target are independent.  This store records those dimensions separately so a
network outage on one satellite does not quarantine unrelated candidates or
models that happen to share a legacy provider-shaped identity.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from execraft.execution_identity import ExecutionIdentity
from execraft.persistence import FileLock, LockLevel, atomic_write_json

_DIMENSIONS = frozenset({"candidate", "runtime", "model_route", "target"})


@dataclass(frozen=True)
class ExecutionHealth:
    dimension: str
    identity: str
    status: str = "available"
    reason: str = ""
    unavailable_until: str = ""
    last_failure_at: str = ""
    consecutive_failures: int = 0
    detail: str = ""

    @property
    def key(self) -> str:
        return f"{self.dimension}:{self.identity}"

    @property
    def is_available(self) -> bool:
        if self.status == "available":
            return True
        if not self.unavailable_until:
            return False
        try:
            deadline = datetime.fromisoformat(self.unavailable_until)
        except ValueError:
            return False
        return deadline <= datetime.now(timezone.utc)

    def as_mapping(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "identity": self.identity,
            "status": self.status,
            "reason": self.reason,
            "unavailable_until": self.unavailable_until,
            "last_failure_at": self.last_failure_at,
            "consecutive_failures": self.consecutive_failures,
            "detail": self.detail,
        }


def failure_health_dimension(
    classification: str,
    identity: ExecutionIdentity,
    *,
    dimension_hint: str = "",
) -> tuple[str, str] | None:
    """Return the narrowest health dimension for a classified failure.

    Most adapters expose only a provider-neutral failure classification, so the
    historical fallback below infers the affected layer from the execution
    identity. Runtimes that can identify the failing boundary precisely may
    supply ``dimension_hint``. This is particularly important for OpenClaw:
    both a dead Checkway and a dead inference satellite are network failures,
    but quarantining the satellite for a Checkway outage would hide healthy
    Native/OpenCode routes that use the same target.
    """

    reason = str(classification).strip()
    if reason == "invalid_output":
        return None  # ContractHealthStore owns schema/output compatibility.
    hinted = _hinted_health_dimension(dimension_hint, identity)
    if hinted is not None:
        return hinted
    if reason == "network_transient":
        if identity.target_id:
            return "target", identity.target_id
        return "runtime", identity.runtime_id
    if reason == "invalid_model":
        if identity.model_route_id:
            return "model_route", identity.model_route_id
        return "candidate", identity.candidate_id
    if reason in {
        "quota_exhausted",
        "rate_limited",
        "authentication_required",
        "auth_failure",
        "provider_error",
    }:
        if identity.model_route_id:
            return "model_route", identity.model_route_id
        return "candidate", identity.candidate_id
    if reason in {
        "session_limit",
        "permission_required",
        "configuration_error",
        "invalid_configuration",
    }:
        return "runtime", identity.runtime_id
    if reason in {"output_silence", "output_limit"}:
        if identity.target_id:
            return "target", identity.target_id
        return "runtime", identity.runtime_id
    if reason in {"timeout", "environment_failure", "tool_failure"}:
        return "candidate", identity.candidate_id
    return "candidate", identity.candidate_id


def _hinted_health_dimension(
    dimension_hint: str, identity: ExecutionIdentity
) -> tuple[str, str] | None:
    """Resolve a trusted dimension name to the matching identity component.

    Invalid/empty hints intentionally fall back to the classification mapping;
    callers cannot inject an arbitrary health-record identity through an error.
    """

    dimension = str(dimension_hint).strip()
    values = {
        "candidate": identity.candidate_id,
        "runtime": identity.runtime_id,
        "model_route": identity.model_route_id,
        "target": identity.target_id,
    }
    value = values.get(dimension, "")
    return (dimension, value) if value else None


class ExecutionHealthStore:
    """JSON-backed dimensioned execution health with bounded cooldowns."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def get(self, dimension: str, identity: str) -> ExecutionHealth:
        dimension = _normalize_dimension(dimension)
        identity = str(identity).strip()
        if not identity:
            return ExecutionHealth(dimension=dimension, identity="")
        raw = self._load().get(f"{dimension}:{identity}") or {}
        health = ExecutionHealth(
            dimension=dimension,
            identity=identity,
            status=str(raw.get("status", "available")),
            reason=str(raw.get("reason", "")),
            unavailable_until=str(raw.get("unavailable_until", "")),
            last_failure_at=str(raw.get("last_failure_at", "")),
            consecutive_failures=int(raw.get("consecutive_failures", 0)),
            detail=str(raw.get("detail", "")),
        )
        if health.status != "available" and health.is_available:
            return ExecutionHealth(
                dimension=dimension,
                identity=identity,
                status="probe_due",
                reason=health.reason,
                unavailable_until=health.unavailable_until,
                last_failure_at=health.last_failure_at,
                consecutive_failures=health.consecutive_failures,
                detail=health.detail,
            )
        return health

    def health_for_identity(self, identity: ExecutionIdentity) -> tuple[ExecutionHealth, ...]:
        values = [
            ("candidate", identity.candidate_id),
            ("runtime", identity.runtime_id),
            ("model_route", identity.model_route_id),
            ("target", identity.target_id),
        ]
        return tuple(self.get(dimension, value) for dimension, value in values if value)

    def first_unavailable(self, identity: ExecutionIdentity) -> ExecutionHealth | None:
        for health in self.health_for_identity(identity):
            if not health.is_available:
                return health
        return None

    def mark_available(self, dimension: str, identity: str) -> None:
        dimension = _normalize_dimension(dimension)
        identity = str(identity).strip()
        if not identity:
            return
        self._update(
            f"{dimension}:{identity}",
            ExecutionHealth(dimension=dimension, identity=identity).as_mapping(),
        )

    def mark_identity_available(self, identity: ExecutionIdentity) -> None:
        for dimension, value in (
            ("candidate", identity.candidate_id),
            ("runtime", identity.runtime_id),
            ("model_route", identity.model_route_id),
            ("target", identity.target_id),
        ):
            if value:
                self.mark_available(dimension, value)

    def mark_failure(
        self,
        dimension: str,
        identity: str,
        *,
        reason: str,
        detail: str = "",
        retry_after_seconds: float | None = None,
        persistent: bool = True,
    ) -> ExecutionHealth:
        dimension = _normalize_dimension(dimension)
        identity = str(identity).strip()
        if not identity:
            return ExecutionHealth(dimension=dimension, identity="")
        tracked_transient = reason in {
            "network_transient",
            "provider_error",
            "output_silence",
            "output_limit",
        }
        if not persistent and not tracked_transient:
            return self.get(dimension, identity)

        now = datetime.now(timezone.utc)
        key = f"{dimension}:{identity}"

        def build(current: dict[str, Any]) -> dict[str, Any]:
            previous = int(current.get("consecutive_failures", 0))
            unavailable_until = ""
            if retry_after_seconds is not None and retry_after_seconds > 0:
                unavailable_until = (
                    now + timedelta(seconds=float(retry_after_seconds))
                ).isoformat()
            elif reason == "rate_limited":
                unavailable_until = (now + timedelta(minutes=5)).isoformat()
            elif reason == "network_transient":
                unavailable_until = (
                    now + timedelta(seconds=min(60 * (2 ** previous), 15 * 60))
                ).isoformat()
            elif reason in {"provider_error", "output_silence"}:
                unavailable_until = (
                    now + timedelta(seconds=min(5 * 60 * (2 ** previous), 60 * 60))
                ).isoformat()
            elif reason == "output_limit":
                unavailable_until = (
                    now + timedelta(seconds=min(15 * 60 * (2 ** previous), 2 * 60 * 60))
                ).isoformat()
            health = ExecutionHealth(
                dimension=dimension,
                identity=identity,
                status="cooldown" if unavailable_until else "blocked",
                reason=reason,
                unavailable_until=unavailable_until,
                last_failure_at=now.isoformat(),
                consecutive_failures=previous + 1,
                detail=detail[:1000],
            )
            return health.as_mapping()

        record = self._mutate(key, build)
        return self.get(dimension, identity) if record else ExecutionHealth(dimension, identity)

    def list(self) -> list[ExecutionHealth]:
        records = self._load()
        result: list[ExecutionHealth] = []
        for key in sorted(records):
            dimension, _, identity = key.partition(":")
            if dimension in _DIMENSIONS and identity:
                result.append(self.get(dimension, identity))
        return result

    def unavailable_candidate_ids(
        self, identities: Iterable[ExecutionIdentity]
    ) -> set[str]:
        return {
            identity.candidate_id
            for identity in identities
            if self.first_unavailable(identity) is not None
        }

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.is_file():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        records = payload.get("records") if isinstance(payload, dict) else None
        return dict(records) if isinstance(records, dict) else {}

    def _update(self, key: str, record: dict[str, Any]) -> None:
        self._mutate(key, lambda _current: record)

    def _mutate(self, key: str, builder) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with FileLock(lock_path, level=LockLevel.RECORD):
            records = self._load()
            record = dict(builder(dict(records.get(key) or {})))
            records[key] = record
            atomic_write_json(
                self.path,
                {"schema_version": 1, "records": records},
                indent=2,
                trailing_newline=True,
                mode=0o600,
            )
            return record


def _normalize_dimension(value: str) -> str:
    dimension = str(value).strip()
    if dimension not in _DIMENSIONS:
        raise ValueError(f"unsupported execution health dimension: {value!r}")
    return dimension
