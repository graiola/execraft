"""Stable identity for one schedulable agent execution candidate.

The legacy control plane historically used ``provider_id`` as a single key for
scheduler identity, CLI adapter, model route and physical node. This model keeps that
field readable while making the independent dimensions explicit.  This value
object intentionally has no dependency on concrete runtimes or orchestration
state so it can be persisted safely in ledgers and diagnostics.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class ExecutionIdentity:
    """Runtime/model/target identity for one logical scheduler candidate."""

    candidate_id: str
    runtime_id: str
    runtime_backend: str = ""
    model_route_id: str = ""
    model_provider: str = ""
    model: str = ""
    target_id: str = ""
    target_kind: str = ""
    concurrency_group: str = ""
    legacy_provider_id: str = ""

    def __post_init__(self) -> None:
        if not self.candidate_id.strip():
            raise ValueError("execution candidate_id cannot be empty")
        if not self.runtime_id.strip():
            raise ValueError("execution runtime_id cannot be empty")

    @property
    def provider_id(self) -> str:
        """Compatibility alias for persisted/provider-centric callers."""

        return self.legacy_provider_id or self.candidate_id

    def as_mapping(self) -> dict[str, str]:
        return {
            "candidate_id": self.candidate_id,
            "runtime_id": self.runtime_id,
            "runtime_backend": self.runtime_backend,
            "model_route_id": self.model_route_id,
            "model_provider": self.model_provider,
            "model": self.model,
            "target_id": self.target_id,
            "target_kind": self.target_kind,
            "concurrency_group": self.concurrency_group,
            "provider_id": self.provider_id,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ExecutionIdentity":
        candidate_id = str(raw.get("candidate_id") or raw.get("provider_id") or "").strip()
        return cls(
            candidate_id=candidate_id,
            runtime_id=str(raw.get("runtime_id") or "native").strip(),
            runtime_backend=str(raw.get("runtime_backend") or raw.get("adapter") or "").strip(),
            model_route_id=str(raw.get("model_route_id") or "").strip(),
            model_provider=str(raw.get("model_provider") or "").strip(),
            model=str(raw.get("model") or "").strip(),
            target_id=str(raw.get("target_id") or "").strip(),
            target_kind=str(raw.get("target_kind") or "").strip(),
            concurrency_group=str(raw.get("concurrency_group") or "").strip(),
            legacy_provider_id=str(raw.get("provider_id") or candidate_id).strip(),
        )


def execution_identity_of(candidate: Any) -> ExecutionIdentity:
    """Return normalized identity from a runtime candidate or legacy adapter."""

    identity = getattr(candidate, "execution_identity", None)
    if isinstance(identity, ExecutionIdentity):
        return identity
    candidate_id = str(
        getattr(candidate, "candidate_id", "")
        or getattr(candidate, "provider_id", "")
    ).strip()
    if not candidate_id:
        raise ValueError("execution candidate does not expose candidate/provider identity")
    backend = str(
        getattr(candidate, "adapter_name", "")
        or candidate.__class__.__name__
    ).strip()
    model_reference = str(getattr(candidate, "model", "") or "").strip()
    provider = ""
    model = model_reference
    if "/" in model_reference:
        provider, model = model_reference.split("/", 1)
    return ExecutionIdentity(
        candidate_id=candidate_id,
        runtime_id=str(getattr(candidate, "runtime_id", "") or "native"),
        runtime_backend=backend,
        model_provider=provider,
        model=model,
        concurrency_group=str(
            getattr(candidate, "_execraft_concurrency_group", "") or candidate_id
        ),
        legacy_provider_id=str(getattr(candidate, "provider_id", "") or candidate_id),
    )
