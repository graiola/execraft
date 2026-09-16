"""Runtime configuration values independent from model routes and agent policy.

The module holds configuration *contracts* only. Concrete runtime lifecycle and
transport implementations live under :mod:`execraft.runtime`, so schema parsing and
project inspection remain usable when optional runtime dependencies are absent.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit

from execraft.network import is_loopback_host


class RuntimeKind(str, Enum):
    """Supported runtime families in the normalized configuration model."""

    NATIVE = "native"
    OPENCLAW = "openclaw"


class OpenClawMode(str, Enum):
    """How Execraft obtains an OpenClaw Gateway."""

    MANAGED = "managed"
    EXTERNAL = "external"


class OpenClawVersionPolicy(str, Enum):
    """Compatibility behavior for Gateway versions not validated by Execraft."""

    PINNED_COMPATIBLE = "pinned-compatible"
    WARN_UNVALIDATED = "warn-unvalidated"


@dataclass(frozen=True)
class OpenClawOptimizationOptions:
    """Token/cache/context policy for managed OpenClaw sessions.

    These settings are control-plane policy. They intentionally avoid OpenClaw
    private state and can be disabled without changing correctness: Execraft can
    always cold-reconstruct a complete handoff from durable state.
    """

    cache_retention: str = "short"
    context_pruning_mode: str = "off"
    context_pruning_ttl: str = "1h"
    proactive_compaction: bool = True
    compaction_mode: str = "safeguard"
    compaction_timeout_seconds: int = 180
    context_pressure_ratio: float = 0.80
    max_context_tokens: int = 0
    max_delta_prompt_bytes: int = 65536
    max_session_reuse_turns: int = 16
    max_session_age_seconds: int = 21600
    post_compaction_guard: bool = True

    def __post_init__(self) -> None:
        if self.cache_retention not in {"none", "short", "long"}:
            raise ValueError("OpenClaw cache_retention must be none, short, or long")
        if self.context_pruning_mode not in {"off", "cache-ttl"}:
            raise ValueError("OpenClaw context_pruning_mode must be off or cache-ttl")
        if not self.context_pruning_ttl.strip():
            raise ValueError("OpenClaw context_pruning_ttl cannot be empty")
        if self.compaction_mode not in {"default", "safeguard"}:
            raise ValueError("OpenClaw compaction_mode must be default or safeguard")
        if self.compaction_timeout_seconds <= 0:
            raise ValueError("OpenClaw compaction timeout must be positive")
        if not 0.0 < self.context_pressure_ratio <= 1.0:
            raise ValueError("OpenClaw context_pressure_ratio must be in (0, 1]")
        if self.max_context_tokens < 0:
            raise ValueError("OpenClaw max_context_tokens cannot be negative")
        if self.max_delta_prompt_bytes <= 0:
            raise ValueError("OpenClaw max_delta_prompt_bytes must be positive")
        if self.max_session_reuse_turns <= 0:
            raise ValueError("OpenClaw max_session_reuse_turns must be positive")
        if self.max_session_age_seconds <= 0:
            raise ValueError("OpenClaw max_session_age_seconds must be positive")

    def as_mapping(self) -> dict[str, object]:
        return {
            "cache_retention": self.cache_retention,
            "context_pruning_mode": self.context_pruning_mode,
            "context_pruning_ttl": self.context_pruning_ttl,
            "proactive_compaction": self.proactive_compaction,
            "compaction_mode": self.compaction_mode,
            "compaction_timeout_seconds": self.compaction_timeout_seconds,
            "context_pressure_ratio": self.context_pressure_ratio,
            "max_context_tokens": self.max_context_tokens,
            "max_delta_prompt_bytes": self.max_delta_prompt_bytes,
            "max_session_reuse_turns": self.max_session_reuse_turns,
            "max_session_age_seconds": self.max_session_age_seconds,
            "post_compaction_guard": self.post_compaction_guard,
        }


@dataclass(frozen=True)
class OpenClawRuntimeOptions:
    """Gateway lifecycle/connectivity settings for one OpenClaw runtime.

    Secrets are referenced by environment-variable name via ``auth_ref``. The
    resolved secret is never serialized back through :meth:`as_mapping`.
    """

    mode: OpenClawMode = OpenClawMode.MANAGED
    gateway: str = "ws://127.0.0.1:18789"
    executable: str = "openclaw"
    auth_ref: str = ""
    auth_kind: str = "token"
    state_dir: str = ""
    config_path: str = ""
    request_timeout_seconds: int = 30
    handshake_timeout_seconds: int = 15
    startup_timeout_seconds: int = 60
    reconnect_attempts: int = 3
    restart_attempts: int = 1
    version_policy: OpenClawVersionPolicy = OpenClawVersionPolicy.PINNED_COMPATIBLE
    optimization: OpenClawOptimizationOptions = OpenClawOptimizationOptions()

    def __post_init__(self) -> None:
        parsed = urlsplit(self.gateway)
        if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
            raise ValueError("OpenClaw gateway must use an absolute ws:// or wss:// URL")
        if self.mode == OpenClawMode.MANAGED:
            if not is_loopback_host(parsed.hostname):
                raise ValueError("managed OpenClaw gateway must use a loopback host")
            if parsed.port is None:
                raise ValueError("managed OpenClaw gateway URL must declare an explicit port")
            if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
                raise ValueError("managed OpenClaw gateway URL cannot contain path/query/fragment")
            if parsed.scheme == "wss" and not self.config_path:
                raise ValueError(
                    "managed wss:// OpenClaw gateway requires an explicit TLS-capable config_path"
                )
        if self.auth_kind not in {"token", "password", "none"}:
            raise ValueError("OpenClaw auth_kind must be token, password, or none")
        if self.request_timeout_seconds <= 0:
            raise ValueError("OpenClaw request timeout must be positive")
        if self.handshake_timeout_seconds <= 0:
            raise ValueError("OpenClaw handshake timeout must be positive")
        if self.startup_timeout_seconds <= 0:
            raise ValueError("OpenClaw startup timeout must be positive")
        if self.reconnect_attempts < 0 or self.restart_attempts < 0:
            raise ValueError("OpenClaw retry counts cannot be negative")

    def as_mapping(self) -> dict[str, object]:
        result: dict[str, object] = {
            "mode": self.mode.value,
            "gateway": self.gateway,
            "version_policy": self.version_policy.value,
        }
        if self.executable != "openclaw":
            result["executable"] = self.executable
        if self.auth_ref:
            result["auth_ref"] = self.auth_ref
        if self.auth_kind != "token":
            result["auth_kind"] = self.auth_kind
        if self.state_dir:
            result["state_dir"] = self.state_dir
        if self.config_path:
            result["config_path"] = self.config_path
        if self.request_timeout_seconds != 30:
            result["request_timeout_seconds"] = self.request_timeout_seconds
        if self.handshake_timeout_seconds != 15:
            result["handshake_timeout_seconds"] = self.handshake_timeout_seconds
        if self.startup_timeout_seconds != 60:
            result["startup_timeout_seconds"] = self.startup_timeout_seconds
        if self.reconnect_attempts != 3:
            result["reconnect_attempts"] = self.reconnect_attempts
        if self.restart_attempts != 1:
            result["restart_attempts"] = self.restart_attempts
        result["optimization"] = self.optimization.as_mapping()
        return result


@dataclass(frozen=True)
class RuntimeConfig:
    """One configured agent runtime.

    ``adapter`` and ``binary`` remain Native-runtime implementation details for
    the compatibility window. OpenClaw-specific lifecycle settings are grouped
    under ``openclaw`` so they do not leak into model routes or targets.
    """

    id: str
    kind: RuntimeKind
    adapter: str = ""
    binary: str = ""
    openclaw: OpenClawRuntimeOptions | None = None

    def as_mapping(self) -> dict[str, object]:
        result: dict[str, object] = {"kind": self.kind.value}
        if self.kind == RuntimeKind.NATIVE:
            if self.adapter:
                result["adapter"] = self.adapter
            if self.binary:
                result["binary"] = self.binary
        elif self.openclaw is not None:
            result.update(self.openclaw.as_mapping())
        return result
