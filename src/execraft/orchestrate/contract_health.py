"""Contract-local structured-output reliability state."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from execraft.persistence import FileLock, LockLevel, atomic_write_json


def schema_sha256(schema: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(schema),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ContractHealth:
    provider_id: str
    model: str
    capability: str
    schema_hash: str
    consecutive_failures: int = 0
    successful_results: int = 0
    failed_results: int = 0
    unavailable_until: str = ""
    last_failure_at: str = ""
    detail: str = ""

    @property
    def is_available(self) -> bool:
        if self.consecutive_failures < 2 or not self.unavailable_until:
            return True
        try:
            deadline = datetime.fromisoformat(self.unavailable_until)
        except ValueError:
            return False
        return deadline <= datetime.now(timezone.utc)

    def as_mapping(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "model": self.model,
            "capability": self.capability,
            "schema_hash": self.schema_hash,
            "consecutive_failures": self.consecutive_failures,
            "successful_results": self.successful_results,
            "failed_results": self.failed_results,
            "unavailable_until": self.unavailable_until,
            "last_failure_at": self.last_failure_at,
            "detail": self.detail,
        }


class ContractHealthStore:
    """Persist failures without contaminating provider endpoint health."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @staticmethod
    def _key(
        provider_id: str, model: str, capability: str, schema_hash: str
    ) -> str:
        value = "\0".join((provider_id, model, capability, schema_hash))
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def get(
        self,
        provider_id: str,
        model: str,
        capability: str,
        schema_hash: str,
    ) -> ContractHealth:
        raw = self._load().get(
            self._key(provider_id, model, capability, schema_hash), {}
        )
        return ContractHealth(
            provider_id=provider_id,
            model=model,
            capability=capability,
            schema_hash=schema_hash,
            consecutive_failures=max(
                0, int(raw.get("consecutive_failures", 0))
            ),
            successful_results=max(0, int(raw.get("successful_results", 0))),
            failed_results=max(0, int(raw.get("failed_results", 0))),
            unavailable_until=str(raw.get("unavailable_until", "")),
            last_failure_at=str(raw.get("last_failure_at", "")),
            detail=str(raw.get("detail", "")),
        )

    def mark_failure(
        self,
        provider_id: str,
        model: str,
        capability: str,
        schema_hash: str,
        *,
        detail: str = "",
    ) -> ContractHealth:
        now = datetime.now(timezone.utc)
        key = self._key(provider_id, model, capability, schema_hash)

        def build(current: dict[str, Any]) -> dict[str, Any]:
            failures = max(0, int(current.get("consecutive_failures", 0))) + 1
            unavailable_until = ""
            if failures >= 2:
                seconds = min(15 * 60 * (2 ** (failures - 2)), 2 * 60 * 60)
                unavailable_until = (now + timedelta(seconds=seconds)).isoformat()
            return ContractHealth(
                provider_id=provider_id,
                model=model,
                capability=capability,
                schema_hash=schema_hash,
                consecutive_failures=failures,
                successful_results=max(
                    0, int(current.get("successful_results", 0))
                ),
                failed_results=max(
                    0, int(current.get("failed_results", 0))
                )
                + 1,
                unavailable_until=unavailable_until,
                last_failure_at=now.isoformat(),
                detail=detail[:1000],
            ).as_mapping()

        return self._health(self._mutate(key, build))

    def mark_available(
        self,
        provider_id: str,
        model: str,
        capability: str,
        schema_hash: str,
    ) -> None:
        key = self._key(provider_id, model, capability, schema_hash)

        def build(current: dict[str, Any]) -> dict[str, Any]:
            return ContractHealth(
                provider_id=provider_id,
                model=model,
                capability=capability,
                schema_hash=schema_hash,
                consecutive_failures=0,
                successful_results=max(
                    0, int(current.get("successful_results", 0))
                )
                + 1,
                failed_results=max(0, int(current.get("failed_results", 0))),
            ).as_mapping()

        self._mutate(key, build)

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.is_file():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        contracts = payload.get("contracts") if isinstance(payload, dict) else None
        return dict(contracts) if isinstance(contracts, dict) else {}

    def _mutate(self, key: str, builder) -> dict[str, Any]:
        result: dict[str, Any] = {}

        def update(records: dict[str, dict[str, Any]]) -> None:
            nonlocal result
            result = dict(builder(dict(records.get(key) or {})))
            records[key] = result

        self._mutate_records(update)
        return result

    def _mutate_records(self, callback) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with FileLock(lock_path, level=LockLevel.RECORD):
            records = self._load()
            callback(records)
            atomic_write_json(
                self.path,
                {"schema_version": 1, "contracts": records},
                indent=2,
                trailing_newline=True,
                mode=0o600,
            )

    @staticmethod
    def _health(raw: Mapping[str, Any]) -> ContractHealth:
        return ContractHealth(
            provider_id=str(raw.get("provider_id", "")),
            model=str(raw.get("model", "")),
            capability=str(raw.get("capability", "")),
            schema_hash=str(raw.get("schema_hash", "")),
            consecutive_failures=int(raw.get("consecutive_failures", 0)),
            successful_results=int(raw.get("successful_results", 0)),
            failed_results=int(raw.get("failed_results", 0)),
            unavailable_until=str(raw.get("unavailable_until", "")),
            last_failure_at=str(raw.get("last_failure_at", "")),
            detail=str(raw.get("detail", "")),
        )
