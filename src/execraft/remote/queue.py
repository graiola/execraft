"""Durable decision and replay-protection stores."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from execraft.persistence.atomic import atomic_write_json

from .models import Decision, utc_now


class RemoteStoreError(RuntimeError):
    pass


class DecisionQueue:
    def __init__(self, path: Path):
        self.path = path

    def _load_all(self) -> list[Decision]:
        if not self.path.is_file():
            return []
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise RemoteStoreError(f"decision queue must contain a list: {self.path}")
        return [Decision.from_mapping(item) for item in data]

    def _save_all(self, decisions: list[Decision]) -> None:
        atomic_write_json(
            self.path,
            [item.as_mapping() for item in decisions],
            indent=2,
        )

    def put(self, decision: Decision) -> None:
        decisions = [item for item in self._load_all() if item.id != decision.id]
        decisions.append(decision)
        self._save_all(decisions)

    def pending(self, *, project_id: str = "", task_id: str = "") -> list[Decision]:
        return [
            item
            for item in self._load_all()
            if item.status == "pending"
            and not item.expired()
            and (not project_id or item.project_id == project_id)
            and (not task_id or item.task_id == task_id)
        ]

    def resolve(
        self,
        decision_id: str,
        *,
        actor_id: str,
        revision: int,
        nonce: str,
        resolution: str,
    ) -> Decision:
        decisions = self._load_all()
        match = next((item for item in decisions if item.id == decision_id), None)
        if match is None:
            raise RemoteStoreError(f"unknown decision: {decision_id}")
        if match.status != "pending":
            raise RemoteStoreError(f"decision is already resolved: {decision_id}")
        if match.expired():
            raise RemoteStoreError(f"decision has expired: {decision_id}")
        if match.revision != revision:
            raise RemoteStoreError(f"stale decision revision: {revision}")
        if not match.nonce or match.nonce != nonce:
            raise RemoteStoreError("invalid or reused decision nonce")
        if resolution not in match.options and resolution != "rejected":
            raise RemoteStoreError(f"unsupported decision resolution: {resolution}")
        match.status = "rejected" if resolution == "rejected" else "approved"
        match.resolved_by = actor_id
        match.resolution = resolution
        match.nonce = ""  # one-time challenge
        self._save_all(decisions)
        return match


class RemoteAuditLog:
    def __init__(self, path: Path):
        self.path = path

    def append(self, event_type: str, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"timestamp": utc_now(), "event_type": event_type, "payload": payload}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")


class ReplayGuard:
    def __init__(self, path: Path):
        self.path = path

    def _ids(self) -> set[str]:
        if not self.path.is_file():
            return set()
        return {line.strip() for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()}

    def claim(self, request_id: str) -> bool:
        if not request_id:
            raise RemoteStoreError("request_id cannot be empty")
        known = self._ids()
        if request_id in known:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(request_id + "\n")
        return True
