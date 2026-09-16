"""Durable, private artifacts produced by external agent executions.

The event journal intentionally stores compact metadata. Full agent output can be
large and may contain structured JSON that must remain byte-for-byte recoverable,
so it is persisted separately and referenced from journal entries.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from execraft.persistence.atomic import atomic_write_bytes


_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class AgentArtifactReference:
    """Stable metadata for a persisted agent result."""

    path: Path
    sha256: str
    size_bytes: int
    content_type: str = "application/json"

    def as_mapping(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "content_type": self.content_type,
        }


class AgentArtifactStore:
    """Persist full agent results atomically outside the bounded event journal."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    def persist(
        self,
        *,
        project_id: str,
        package_id: str,
        stage: str,
        capability: str,
        agent_id: str,
        result: Mapping[str, Any],
    ) -> AgentArtifactReference:
        captured_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        record = {
            "schema_version": 1,
            "project_id": project_id,
            "package_id": package_id,
            "stage": stage,
            "capability": capability,
            "agent_id": agent_id,
            "captured_at": captured_at,
            "result": dict(result),
        }
        encoded = (
            json.dumps(record, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
        ).encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()

        package_dir = self.root / _safe_component(package_id)
        package_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(package_dir, 0o700)
        except OSError:
            pass

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = (
            f"{stamp}-{_safe_component(stage)}-{_safe_component(agent_id)}-"
            f"{uuid.uuid4().hex[:8]}.json"
        )
        path = package_dir / filename
        atomic_write_bytes(path, encoded, mode=0o600)

        return AgentArtifactReference(
            path=path.resolve(),
            sha256=digest,
            size_bytes=len(encoded),
        )


def _safe_component(value: str) -> str:
    cleaned = _SAFE_COMPONENT.sub("-", str(value).strip()).strip("-.")
    return cleaned or "unknown"
