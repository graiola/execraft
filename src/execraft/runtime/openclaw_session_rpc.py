"""Public OpenClaw session telemetry and compaction RPC helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .openclaw_gateway import OpenClawGatewayError, OpenClawGatewayRequestError


class SessionGatewayClient(Protocol):
    def describe_session(self, session_key: str) -> Mapping[str, Any] | None: ...

    def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
        idempotency_key: str = "",
    ) -> Any: ...


@dataclass(frozen=True)
class OpenClawSessionSnapshot:
    key: str
    context_tokens: int | None = None
    context_window: int | None = None
    compaction_count: int = 0
    cache_read: int = 0
    cache_write: int = 0

    @classmethod
    def from_row(cls, key: str, row: Mapping[str, Any]) -> "OpenClawSessionSnapshot":
        usage = row.get("usage") if isinstance(row.get("usage"), Mapping) else {}
        model = row.get("model") if isinstance(row.get("model"), Mapping) else {}
        return cls(
            key=key,
            # ``contextTokens`` is the model's context window on OpenClaw
            # 2026.7.1-2, not the session's current occupancy, so it belongs to
            # ``context_window`` below. Reading it here made every session look
            # completely full. That release exposes no current-occupancy figure
            # (``totalTokens`` is cumulative usage and never falls after a
            # compaction), so this stays unknown rather than guessing.
            context_tokens=_optional_int(
                row.get("context_tokens"),
                usage.get("contextTokens"),
            ),
            context_window=_optional_int(
                row.get("contextWindow"),
                row.get("contextWindowTokens"),
                row.get("context_window"),
                model.get("contextWindow"),
                row.get("contextTokens"),
            ),
            compaction_count=_int(
                row.get("compactionCount"),
                row.get("compactionCheckpointCount"),
                row.get("compaction_count"),
                default=0,
            ),
            cache_read=_int(
                usage.get("cacheRead"), row.get("cacheRead"), default=0
            ),
            cache_write=_int(
                usage.get("cacheWrite"), row.get("cacheWrite"), default=0
            ),
        )

    @property
    def context_pressure(self) -> float | None:
        if not self.context_tokens or not self.context_window:
            return None
        return self.context_tokens / self.context_window

    def as_mapping(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "context_tokens": self.context_tokens,
            "context_window": self.context_window,
            "context_pressure": self.context_pressure,
            "compaction_count": self.compaction_count,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
        }


@dataclass(frozen=True)
class OpenClawCompactionResult:
    attempted: bool
    compacted: bool
    tokens_before: int | None = None
    tokens_after: int | None = None
    error: str = ""

    def as_mapping(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "compacted": self.compacted,
            "tokens_before": self.tokens_before,
            "tokens_after": self.tokens_after,
            "error": self.error,
        }


def session_snapshot(
    client: SessionGatewayClient, session_key: str
) -> OpenClawSessionSnapshot | None:
    row = client.describe_session(session_key)
    if row is None:
        return None
    return OpenClawSessionSnapshot.from_row(session_key, row)


def compact_session(
    client: SessionGatewayClient,
    session_key: str,
    *,
    agent_id: str,
    timeout_seconds: int,
) -> OpenClawCompactionResult:
    """Run first-class LLM compaction without replaying agent work."""

    params: dict[str, Any] = {"key": session_key}
    if agent_id:
        params["agentId"] = agent_id
    try:
        payload = client.request(
            "sessions.compact",
            params,
            timeout_seconds=float(timeout_seconds),
        )
    except OpenClawGatewayRequestError as exc:
        code = (exc.error.detail_code or exc.error.code).upper()
        return OpenClawCompactionResult(
            attempted=True,
            compacted=False,
            error=f"{code or 'RPC_ERROR'}: {exc}",
        )
    except OpenClawGatewayError as exc:
        return OpenClawCompactionResult(
            attempted=True, compacted=False, error=str(exc)
        )
    if not isinstance(payload, Mapping) or payload.get("compacted") is not True:
        return OpenClawCompactionResult(
            attempted=True,
            compacted=False,
            error="OpenClaw did not confirm session compaction",
        )
    result = payload.get("result") if isinstance(payload.get("result"), Mapping) else {}
    # OpenClaw 2026.7.1-2 reports ``tokensBefore`` only -- neither the compaction
    # response nor ``sessions.compaction.list`` carries a post-compaction size,
    # and ``totalTokens`` is cumulative session usage that never falls after a
    # compaction. There is therefore no post-compaction evidence to be had on
    # this release, and continuation correctly declines to reuse the session
    # rather than assume the compaction was sufficient.
    return OpenClawCompactionResult(
        attempted=True,
        compacted=True,
        tokens_before=_optional_int(result.get("tokensBefore"), payload.get("tokensBefore")),
        tokens_after=_optional_int(result.get("tokensAfter"), payload.get("tokensAfter")),
    )


def _optional_int(*values: object) -> int | None:
    for value in values:
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return max(0, int(value))
        if isinstance(value, str):
            try:
                return max(0, int(value.strip()))
            except ValueError:
                continue
    return None


def _int(*values: object, default: int) -> int:
    value = _optional_int(*values)
    return default if value is None else value
