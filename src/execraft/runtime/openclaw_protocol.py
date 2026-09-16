"""Public OpenClaw Gateway protocol values used by Execraft.

This module contains no transport dependency.  It records the exact wire and
release compatibility Execraft has validated so unsupported Gateway upgrades fail
closed rather than drifting silently.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

OPENCLAW_PROTOCOL_VERSION = 4
TESTED_OPENCLAW_VERSIONS = frozenset({"2026.7.1-2"})


def normalize_openclaw_version(value: object) -> str:
    text = str(value or "").strip()
    return text[1:] if text.startswith("v") else text


@dataclass(frozen=True)
class GatewayFeatures:
    methods: frozenset[str]
    events: frozenset[str]

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "GatewayFeatures":
        raw = payload.get("features")
        features = raw if isinstance(raw, Mapping) else {}
        methods = features.get("methods", [])
        events = features.get("events", [])
        return cls(
            methods=frozenset(str(item) for item in methods if str(item)),
            events=frozenset(str(item) for item in events if str(item)),
        )


@dataclass(frozen=True)
class GatewayHello:
    protocol: int
    server_version: str
    connection_id: str
    features: GatewayFeatures
    scopes: frozenset[str]
    max_payload_bytes: int | None = None
    max_buffered_bytes: int | None = None
    tick_interval_ms: int | None = None
    device_token: str = ""

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "GatewayHello":
        server = payload.get("server")
        auth = payload.get("auth")
        policy = payload.get("policy")
        server_map = server if isinstance(server, Mapping) else {}
        auth_map = auth if isinstance(auth, Mapping) else {}
        policy_map = policy if isinstance(policy, Mapping) else {}
        scopes = auth_map.get("scopes", [])
        return cls(
            protocol=int(payload.get("protocol", 0) or 0),
            server_version=normalize_openclaw_version(server_map.get("version", "")),
            connection_id=str(server_map.get("connId", "")),
            features=GatewayFeatures.from_payload(payload),
            scopes=frozenset(str(item) for item in scopes if str(item)),
            max_payload_bytes=_optional_int(policy_map.get("maxPayload")),
            max_buffered_bytes=_optional_int(policy_map.get("maxBufferedBytes")),
            tick_interval_ms=_optional_int(policy_map.get("tickIntervalMs")),
            device_token=str(auth_map.get("deviceToken", "") or ""),
        )


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None


@dataclass(frozen=True)
class GatewayEvent:
    name: str
    payload: Any = None
    sequence: int | None = None
    state_version: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class GatewayError:
    code: str
    message: str
    details: Mapping[str, Any] | None = None
    retryable: bool = False
    retry_after_ms: int | None = None

    @classmethod
    def from_mapping(cls, raw: object) -> "GatewayError":
        value = raw if isinstance(raw, Mapping) else {}
        details = value.get("details")
        return cls(
            code=str(value.get("code", "UNKNOWN")),
            message=str(value.get("message", "Gateway request failed")),
            details=details if isinstance(details, Mapping) else None,
            retryable=bool(value.get("retryable", False)),
            retry_after_ms=_optional_int(value.get("retryAfterMs")),
        )

    @property
    def detail_code(self) -> str:
        return str((self.details or {}).get("code", ""))

    @property
    def detail_reason(self) -> str:
        return str((self.details or {}).get("reason", ""))
