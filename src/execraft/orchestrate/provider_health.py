"""Persistent provider health and cooldown state.

Provider accounts are shared across projects, so health is stored once below the
Execraft state root.  The scheduler consults this store before starting an agent;
a known-exhausted provider is skipped until its reset time instead of being
launched repeatedly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from execraft.persistence import FileLock, LockLevel, atomic_write_json


@dataclass(frozen=True)
class ProviderHealth:
    provider_id: str
    status: str = "available"
    reason: str = ""
    unavailable_until: str = ""
    last_failure_at: str = ""
    consecutive_failures: int = 0
    detail: str = ""

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
            "provider_id": self.provider_id,
            "status": self.status,
            "reason": self.reason,
            "unavailable_until": self.unavailable_until,
            "last_failure_at": self.last_failure_at,
            "consecutive_failures": self.consecutive_failures,
            "detail": self.detail,
        }


class ProviderHealthStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def get(self, provider_id: str) -> ProviderHealth:
        records = self._load()
        raw = records.get(provider_id) or {}
        if str(raw.get("reason", "")) == "invalid_output":
            # Migrate pre-contract-health records eagerly. A model/schema
            # formatting failure never described endpoint availability, so an
            # old global cooldown must not survive the upgrade.
            available = ProviderHealth(provider_id=provider_id)
            self._update(provider_id, available.as_mapping())
            return available
        health = ProviderHealth(
            provider_id=provider_id,
            status=str(raw.get("status", "available")),
            reason=str(raw.get("reason", "")),
            unavailable_until=str(raw.get("unavailable_until", "")),
            last_failure_at=str(raw.get("last_failure_at", "")),
            consecutive_failures=int(raw.get("consecutive_failures", 0)),
            detail=str(raw.get("detail", "")),
        )
        if health.status != "available" and health.is_available:
            # A cooldown expiring only means that one controlled probe is due;
            # it does not prove the provider recovered. Preserve the failure
            # streak until a real successful execution calls mark_available().
            return ProviderHealth(
                provider_id=provider_id,
                status="probe_due",
                reason=health.reason,
                unavailable_until=health.unavailable_until,
                last_failure_at=health.last_failure_at,
                consecutive_failures=health.consecutive_failures,
                detail=health.detail,
            )
        return health

    def unavailable_ids(self) -> set[str]:
        return {
            provider_id
            for provider_id in self._load()
            if not self.get(provider_id).is_available
        }

    def mark_failure(
        self,
        provider_id: str,
        *,
        reason: str,
        detail: str = "",
        retry_after_seconds: float | None = None,
        persistent: bool = True,
    ) -> ProviderHealth:
        # Some provider CLIs collapse quota exhaustion and backend outages
        # into a generic server error. Track those transient failures even when
        # the adapter cannot prove persistence, otherwise status incorrectly
        # returns to "available" and the orchestrator hammers the same account.
        tracked_transient_reasons = {
            "provider_error",
            "output_silence",
            "output_limit",
        }
        if not persistent and reason not in tracked_transient_reasons:
            return self.get(provider_id)

        persistent_reasons = {
            "quota_exhausted",
            "session_limit",
            "rate_limited",
            "network_transient",
            "authentication_required",
            "auth_failure",
            "permission_required",
            "invalid_model",
            *tracked_transient_reasons,
        }
        if reason not in persistent_reasons:
            return self.get(provider_id)

        now = datetime.now(timezone.utc)
        def build(current: dict[str, Any]) -> dict[str, Any]:
            unavailable_until = ""
            if retry_after_seconds is not None and retry_after_seconds > 0:
                unavailable_until = (
                    now + timedelta(seconds=retry_after_seconds)
                ).isoformat()
            elif reason == "rate_limited":
                unavailable_until = (now + timedelta(minutes=5)).isoformat()
            elif reason == "network_transient":
                # Network failures are temporary but should still suppress
                # immediate CLI relaunches. Persist a bounded exponential
                # cooldown across daemon restarts.
                previous = int(current.get("consecutive_failures", 0))
                seconds = min(60 * (2 ** max(0, previous)), 15 * 60)
                unavailable_until = (now + timedelta(seconds=seconds)).isoformat()
            elif reason == "provider_error":
                # Unknown backend failures often hide a quota reset. Start with
                # a five-minute probe window and grow 3x on every failed probe,
                # capped at six hours. Operators can still record an exact UI
                # reset with `agents set-cooldown`.
                previous = int(current.get("consecutive_failures", 0))
                seconds = min(5 * 60 * (3 ** max(0, previous)), 6 * 60 * 60)
                unavailable_until = (now + timedelta(seconds=seconds)).isoformat()
            elif reason == "output_silence":
                # A live CLI with no stdout/stderr often indicates a wedged local
                # transport, model server, or child process.  Suppress immediate
                # reselection long enough for a different agent to take over,
                # then require one controlled probe rather than declaring the
                # endpoint permanently unavailable.
                previous = int(current.get("consecutive_failures", 0))
                seconds = min(5 * 60 * (2 ** max(0, previous)), 30 * 60)
                unavailable_until = (now + timedelta(seconds=seconds)).isoformat()
            elif reason == "output_limit":
                # Excessive provider output is a transport-quality failure, not
                # useful progress. Quarantine the endpoint long enough for a
                # bounded failover, then allow one controlled probe.
                previous = int(current.get("consecutive_failures", 0))
                seconds = min(15 * 60 * (2 ** max(0, previous)), 2 * 60 * 60)
                unavailable_until = (now + timedelta(seconds=seconds)).isoformat()
            health = ProviderHealth(
                provider_id=provider_id,
                status="cooldown" if unavailable_until else "blocked",
                reason=reason,
                unavailable_until=unavailable_until,
                last_failure_at=now.isoformat(),
                consecutive_failures=int(current.get("consecutive_failures", 0)) + 1,
                detail=detail[:1000],
            )
            return health.as_mapping()

        record = self._mutate(provider_id, build)
        return ProviderHealth(
            provider_id=provider_id,
            status=str(record.get("status", "available")),
            reason=str(record.get("reason", "")),
            unavailable_until=str(record.get("unavailable_until", "")),
            last_failure_at=str(record.get("last_failure_at", "")),
            consecutive_failures=int(record.get("consecutive_failures", 0)),
            detail=str(record.get("detail", "")),
        )

    def set_cooldown(
        self,
        provider_id: str,
        *,
        unavailable_until: datetime,
        reason: str = "manual_cooldown",
        detail: str = "",
    ) -> ProviderHealth:
        """Persist an operator-supplied provider unblock deadline.

        This is useful when a provider UI exposes a quota reset that its CLI
        collapses into a generic server error. The deadline must be
        timezone-aware and in the future; it is normalized to UTC before
        persistence.
        """

        if unavailable_until.tzinfo is None:
            raise ValueError("provider cooldown deadline must include a timezone")
        deadline = unavailable_until.astimezone(timezone.utc)
        now = datetime.now(timezone.utc)
        if deadline <= now:
            raise ValueError("provider cooldown deadline must be in the future")
        normalized_reason = reason.strip() or "manual_cooldown"

        def build(current: dict[str, Any]) -> dict[str, Any]:
            health = ProviderHealth(
                provider_id=provider_id,
                status="cooldown",
                reason=normalized_reason,
                unavailable_until=deadline.isoformat(),
                last_failure_at=now.isoformat(),
                consecutive_failures=int(current.get("consecutive_failures", 0)),
                detail=detail[:1000],
            )
            return health.as_mapping()

        record = self._mutate(provider_id, build)
        return ProviderHealth(
            provider_id=provider_id,
            status=str(record.get("status", "cooldown")),
            reason=str(record.get("reason", normalized_reason)),
            unavailable_until=str(record.get("unavailable_until", "")),
            last_failure_at=str(record.get("last_failure_at", "")),
            consecutive_failures=int(record.get("consecutive_failures", 0)),
            detail=str(record.get("detail", "")),
        )

    def mark_available(self, provider_id: str) -> None:
        self._update(provider_id, ProviderHealth(provider_id=provider_id).as_mapping())

    def reset(self, provider_id: str) -> None:
        self.mark_available(provider_id)

    def list(self) -> list[ProviderHealth]:
        return [self.get(provider_id) for provider_id in sorted(self._load())]

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.is_file():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        providers = payload.get("providers") if isinstance(payload, dict) else None
        return dict(providers) if isinstance(providers, dict) else {}

    def _update(self, provider_id: str, record: dict[str, Any]) -> None:
        self._mutate(provider_id, lambda _current: record)

    def _mutate(self, provider_id: str, builder) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with FileLock(lock_path, level=LockLevel.RECORD):
            records = self._load()
            record = dict(builder(dict(records.get(provider_id) or {})))
            records[provider_id] = record
            atomic_write_json(
                self.path,
                {"schema_version": 1, "providers": records},
                indent=2,
                trailing_newline=True,
                mode=0o600,
            )
            return record
