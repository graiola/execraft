"""Durable, task-scoped temporary provider complexity promotions.

Static provider complexity ceilings are a safety policy and should not be edited
just to survive a temporary premium-provider outage.  This module provides an
operator-controlled runtime override that raises one provider/capability ceiling
for a bounded period while preserving the provider's normal capability weight.

Promotions live inside the collision-safe task state directory.  They are
therefore hot-reloadable by a running orchestrator, isolated from other tasks,
and automatically stop applying after their expiry timestamp.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Iterable

from execraft.persistence import FileLock, LockLevel
from execraft.persistence.atomic import atomic_write_json


@dataclass(frozen=True)
class ProviderPromotion:
    """One bounded complexity-ceiling override for a provider capability."""

    provider_id: str
    capability: str
    base_max_complexity: int
    promoted_max_complexity: int
    created_at: str
    expires_at: str
    fallback_only: bool = True
    package_id: str = ""
    allow_final_review: bool = False
    reason: str = ""
    source: str = "operator"

    @property
    def key(self) -> str:
        package = self.package_id or "*"
        return f"{self.provider_id}\0{self.capability}\0{package}"

    def is_active(self, *, now: datetime | None = None) -> bool:
        deadline = _parse_timestamp(self.expires_at)
        if deadline is None:
            return False
        current = _utc_now(now)
        return deadline > current

    def applies_to(
        self,
        *,
        provider_id: str,
        capability: str,
        package_id: str = "",
        stage: str = "",
        now: datetime | None = None,
    ) -> bool:
        if not self.is_active(now=now):
            return False
        if self.provider_id != provider_id or self.capability != capability:
            return False
        if self.package_id and self.package_id != package_id:
            return False
        if stage == "final_review" and capability == "review" and not self.allow_final_review:
            return False
        return True

    def as_mapping(self, *, now: datetime | None = None) -> dict[str, Any]:
        current = _utc_now(now)
        deadline = _parse_timestamp(self.expires_at)
        remaining = max(0.0, (deadline - current).total_seconds()) if deadline else 0.0
        return {
            "provider_id": self.provider_id,
            "capability": self.capability,
            "base_max_complexity": self.base_max_complexity,
            "promoted_max_complexity": self.promoted_max_complexity,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "remaining_seconds": remaining,
            "active": bool(deadline and deadline > current),
            "fallback_only": self.fallback_only,
            "package_id": self.package_id,
            "allow_final_review": self.allow_final_review,
            "reason": self.reason,
            "source": self.source,
        }

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "ProviderPromotion | None":
        provider_id = str(raw.get("provider_id", "")).strip()
        capability = str(raw.get("capability", "")).strip()
        if not provider_id or not capability:
            return None
        try:
            base = _complexity(raw.get("base_max_complexity", 100))
            promoted = _complexity(raw.get("promoted_max_complexity", 100))
        except ValueError:
            return None
        created_at = str(raw.get("created_at", "")).strip()
        expires_at = str(raw.get("expires_at", "")).strip()
        if _parse_timestamp(expires_at) is None:
            return None
        return cls(
            provider_id=provider_id,
            capability=capability,
            base_max_complexity=base,
            promoted_max_complexity=promoted,
            created_at=created_at,
            expires_at=expires_at,
            fallback_only=bool(raw.get("fallback_only", True)),
            package_id=str(raw.get("package_id", "")).strip(),
            allow_final_review=bool(raw.get("allow_final_review", False)),
            reason=str(raw.get("reason", ""))[:500],
            source=str(raw.get("source", "operator"))[:80] or "operator",
        )


class ProviderPromotionStore:
    """Atomic task-local persistence for temporary provider promotions."""

    SCHEMA_VERSION = 1

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = RLock()
        self._cache_signature: tuple[int, int, int] | None = None
        self._cache_records: dict[str, dict[str, Any]] = {}

    def promote(
        self,
        provider_id: str,
        capability: str,
        *,
        base_max_complexity: int,
        promoted_max_complexity: int = 100,
        duration_seconds: float,
        fallback_only: bool = True,
        package_id: str = "",
        allow_final_review: bool = False,
        reason: str = "",
        source: str = "operator",
        now: datetime | None = None,
    ) -> ProviderPromotion:
        """Create or replace one promotion and return its persisted value."""

        provider = str(provider_id).strip()
        capability_name = str(capability).strip()
        if not provider:
            raise ValueError("provider_id is required")
        if not capability_name:
            raise ValueError("capability is required")
        base = _complexity(base_max_complexity)
        promoted = _complexity(promoted_max_complexity)
        if promoted <= base:
            raise ValueError(
                "promoted_max_complexity must be greater than the configured base ceiling"
            )
        duration = float(duration_seconds)
        if duration <= 0:
            raise ValueError("promotion duration must be greater than zero")
        # Promotions are intentionally bounded.  A 7-day maximum is long enough
        # for quota incidents while preventing an accidental quasi-permanent
        # safety-policy bypass.
        if duration > 7 * 24 * 60 * 60:
            raise ValueError("promotion duration cannot exceed 7 days")
        current = _utc_now(now)
        promotion = ProviderPromotion(
            provider_id=provider,
            capability=capability_name,
            base_max_complexity=base,
            promoted_max_complexity=promoted,
            created_at=current.isoformat(),
            expires_at=(current + timedelta(seconds=duration)).isoformat(),
            fallback_only=bool(fallback_only),
            package_id=str(package_id).strip(),
            allow_final_review=bool(allow_final_review),
            reason=str(reason).strip()[:500],
            source=str(source).strip()[:80] or "operator",
        )

        def mutate(records: dict[str, dict[str, Any]]) -> None:
            records[promotion.key] = _persistent_mapping(promotion)

        self._mutate(mutate, now=current)
        return promotion

    def revoke(
        self,
        provider_id: str,
        *,
        capabilities: Iterable[str] | None = None,
        package_id: str | None = None,
    ) -> list[ProviderPromotion]:
        """Remove matching promotions and return the removed active records."""

        provider = str(provider_id).strip()
        capability_filter = {
            str(value).strip() for value in (capabilities or ()) if str(value).strip()
        }
        package_filter = None if package_id is None else str(package_id).strip()
        removed: list[ProviderPromotion] = []

        def mutate(records: dict[str, dict[str, Any]]) -> None:
            for key, raw in list(records.items()):
                promotion = ProviderPromotion.from_mapping(raw)
                if promotion is None:
                    records.pop(key, None)
                    continue
                if promotion.provider_id != provider:
                    continue
                if capability_filter and promotion.capability not in capability_filter:
                    continue
                if package_filter is not None and promotion.package_id != package_filter:
                    continue
                if promotion.is_active():
                    removed.append(promotion)
                records.pop(key, None)

        self._mutate(mutate)
        return removed

    def list(
        self,
        *,
        provider_id: str = "",
        include_expired: bool = False,
        now: datetime | None = None,
    ) -> list[ProviderPromotion]:
        current = _utc_now(now)
        result: list[ProviderPromotion] = []
        for raw in self._load().values():
            promotion = ProviderPromotion.from_mapping(raw)
            if promotion is None:
                continue
            if provider_id and promotion.provider_id != provider_id:
                continue
            if not include_expired and not promotion.is_active(now=current):
                continue
            result.append(promotion)
        return sorted(
            result,
            key=lambda item: (item.provider_id, item.capability, item.package_id, item.expires_at),
        )

    def applicable(
        self,
        provider_id: str,
        capability: str,
        *,
        package_id: str = "",
        stage: str = "",
        now: datetime | None = None,
    ) -> ProviderPromotion | None:
        """Return the strongest active promotion matching the execution scope."""

        matches = [
            item
            for item in self.list(provider_id=provider_id, now=now)
            if item.applies_to(
                provider_id=provider_id,
                capability=capability,
                package_id=package_id,
                stage=stage,
                now=now,
            )
        ]
        if not matches:
            return None
        # Package-specific records beat task-wide records; then prefer the
        # highest ceiling and furthest expiry for deterministic replacement.
        return max(
            matches,
            key=lambda item: (
                1 if item.package_id else 0,
                item.promoted_max_complexity,
                item.expires_at,
            ),
        )

    def effective_max_complexity(
        self,
        provider_id: str,
        capability: str,
        *,
        base_max_complexity: int,
        package_id: str = "",
        stage: str = "",
        now: datetime | None = None,
    ) -> tuple[int, ProviderPromotion | None]:
        """Return the effective ceiling and the promotion responsible for it."""

        base = _complexity(base_max_complexity)
        promotion = self.applicable(
            provider_id,
            capability,
            package_id=package_id,
            stage=stage,
            now=now,
        )
        if promotion is None:
            return base, None
        return max(base, promotion.promoted_max_complexity), promotion

    def _load(self) -> dict[str, dict[str, Any]]:
        """Load records, avoiding repeat disk I/O within one unchanged file version.

        The scheduler can ask for effective ceilings several times during one
        selection pass.  An mtime/size signature keeps those reads cheap while
        still observing an atomic replacement written by the GUI or another CLI
        process on the very next eligibility check.
        """

        with self._lock:
            signature = self._file_signature()
            if signature is None:
                self._cache_signature = None
                self._cache_records = {}
                return {}
            if signature == self._cache_signature:
                return _copy_records(self._cache_records)
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return {}
            records = payload.get("promotions") if isinstance(payload, dict) else None
            if not isinstance(records, dict):
                records = {}
            normalized = {
                str(key): dict(value)
                for key, value in records.items()
                if isinstance(value, dict)
            }
            self._cache_signature = signature
            self._cache_records = normalized
            return _copy_records(normalized)

    def _mutate(
        self,
        mutator: Callable[[dict[str, dict[str, Any]]], None],
        *,
        now: datetime | None = None,
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        # Reuse the repository-wide lock primitive so lock-file validation,
        # ownership checks, and lock ordering remain consistent with other
        # task-local persistence stores.
        with self._lock, FileLock(lock_path, level=LockLevel.RECORD):
            # Invalidate before loading: another process may have replaced the
            # file while this process was waiting for the advisory lock.
            self._cache_signature = None
            records = self._load()
            mutator(records)
            _purge_expired_records(records, now=now)
            atomic_write_json(
                self.path,
                {"schema_version": self.SCHEMA_VERSION, "promotions": records},
                indent=2,
                trailing_newline=True,
                mode=0o600,
            )
            self._cache_signature = self._file_signature()
            self._cache_records = _copy_records(records)

    def _file_signature(self) -> tuple[int, int, int] | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return stat.st_ino, stat.st_mtime_ns, stat.st_size


def _persistent_mapping(promotion: ProviderPromotion) -> dict[str, Any]:
    """Serialize only durable fields; activity/remaining time are derived."""

    return {
        "provider_id": promotion.provider_id,
        "capability": promotion.capability,
        "base_max_complexity": promotion.base_max_complexity,
        "promoted_max_complexity": promotion.promoted_max_complexity,
        "created_at": promotion.created_at,
        "expires_at": promotion.expires_at,
        "fallback_only": promotion.fallback_only,
        "package_id": promotion.package_id,
        "allow_final_review": promotion.allow_final_review,
        "reason": promotion.reason,
        "source": promotion.source,
    }


def _copy_records(records: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {key: dict(value) for key, value in records.items()}


def _purge_expired_records(
    records: dict[str, dict[str, Any]], *, now: datetime | None = None
) -> None:
    current = _utc_now(now)
    for key, raw in list(records.items()):
        promotion = ProviderPromotion.from_mapping(raw)
        if promotion is None or not promotion.is_active(now=current):
            records.pop(key, None)


def parse_promotion_duration(value: str) -> float:
    """Parse a compact operator duration such as ``30m``, ``4h`` or ``2d``."""

    text = str(value).strip().lower()
    if not text:
        raise ValueError("promotion duration is required")
    units = {
        "s": 1.0,
        "m": 60.0,
        "h": 60.0 * 60.0,
        "d": 24.0 * 60.0 * 60.0,
    }
    suffix = text[-1]
    if suffix not in units:
        raise ValueError("promotion duration must use s, m, h, or d (for example 4h)")
    try:
        amount = float(text[:-1])
    except ValueError as exc:
        raise ValueError(f"invalid promotion duration: {value}") from exc
    seconds = amount * units[suffix]
    if seconds <= 0:
        raise ValueError("promotion duration must be greater than zero")
    if seconds > 7 * 24 * 60 * 60:
        raise ValueError("promotion duration cannot exceed 7 days")
    return seconds


def _complexity(value: Any) -> int:
    try:
        complexity = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("complexity ceiling must be an integer") from exc
    if not 0 <= complexity <= 100:
        raise ValueError("complexity ceiling must be between 0 and 100")
    return complexity


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utc_now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)
