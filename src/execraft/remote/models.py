"""Typed, transport-neutral remote-control contracts.

The public command enum is deliberately closed: transports cannot expose arbitrary
shell, Git, secret, or cleanup operations through this API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class RemoteCommand(str, Enum):
    STATUS = "status"
    PROGRESS = "progress"
    TIMELINE = "timeline"
    RESOURCES = "resources"
    PAUSE = "pause"
    RESUME = "resume"
    RETRY = "retry"
    APPROVE = "approve"
    REJECT = "reject"
SENSITIVE_COMMANDS = {RemoteCommand.APPROVE, RemoteCommand.REJECT}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class RemoteRequest:
    request_id: str
    actor_id: str
    command: RemoteCommand
    project_id: str
    task_id: str
    created_at: str
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "RemoteRequest":
        return cls(
            request_id=str(data["request_id"]),
            actor_id=str(data["actor_id"]),
            command=RemoteCommand(str(data["command"])),
            project_id=str(data["project_id"]),
            task_id=str(data["task_id"]),
            created_at=str(data.get("created_at") or utc_now()),
            payload=dict(data.get("payload") or {}),
        )


@dataclass
class Decision:
    id: str
    project_id: str
    task_id: str
    revision: int
    summary: str
    options: list[str]
    recommended_option: str = ""
    expires_at: str = ""
    nonce: str = ""
    status: str = "pending"
    resolved_by: str = ""
    resolution: str = ""

    def expired(self, *, now: datetime | None = None) -> bool:
        if not self.expires_at:
            return False
        expiry = datetime.fromisoformat(self.expires_at)
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return expiry <= (now or datetime.now(timezone.utc))

    def as_mapping(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "revision": self.revision,
            "summary": self.summary,
            "options": list(self.options),
            "recommended_option": self.recommended_option,
            "expires_at": self.expires_at,
            "nonce": self.nonce,
            "status": self.status,
            "resolved_by": self.resolved_by,
            "resolution": self.resolution,
        }

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "Decision":
        return cls(
            id=str(data["id"]),
            project_id=str(data["project_id"]),
            task_id=str(data["task_id"]),
            revision=int(data["revision"]),
            summary=str(data.get("summary", "")),
            options=[str(item) for item in data.get("options", [])],
            recommended_option=str(data.get("recommended_option", "")),
            expires_at=str(data.get("expires_at", "")),
            nonce=str(data.get("nonce", "")),
            status=str(data.get("status", "pending")),
            resolved_by=str(data.get("resolved_by", "")),
            resolution=str(data.get("resolution", "")),
        )
