"""Remote OpenClaw runtime target admission, health, and capacity.

Remote runtime targets are physical execution locations, not model endpoints.
Execraft keeps workflow/session authority locally and delegates one normalized
OpenClaw invocation through an externally owned Gateway. This compatibility path intentionally
supports only shared-filesystem workspace transport: Execraft never copies or
synchronizes repository state implicitly.
"""

from __future__ import annotations

import hashlib
import threading
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator, Mapping
from urllib.parse import urlsplit, urlunsplit

from execraft.orchestrate.scheduler import AgentExecutionError, Availability
from execraft.runtime.contracts import RuntimeExecutionRequest, RuntimeExecutionResult
from execraft.runtime_config import OpenClawMode, RuntimeConfig, RuntimeKind
from execraft.targets.config import ExecutionTargetConfig, ExecutionTargetKind


def bind_openclaw_runtime_to_remote_target(
    runtime: RuntimeConfig, target: ExecutionTargetConfig
) -> RuntimeConfig:
    """Bind reusable external-runtime policy to one physical Gateway target.

    ``RuntimeConfig`` owns protocol/auth/version policy.  The remote target owns
    physical placement, so its Gateway URL overrides the runtime's fallback URL
    without changing the canonical runtime id used by scheduler/session state.
    """

    if runtime.kind != RuntimeKind.OPENCLAW or runtime.openclaw is None:
        raise ValueError("remote runtime targets require an OpenClaw runtime")
    if runtime.openclaw.mode != OpenClawMode.EXTERNAL:
        raise ValueError("remote runtime targets require external OpenClaw mode")
    if target.kind != ExecutionTargetKind.REMOTE_RUNTIME:
        raise ValueError("remote runtime binding requires a remote_runtime target")
    options = replace(runtime.openclaw, gateway=target.endpoint)
    return replace(runtime, openclaw=options)

def remote_target_state_root(state_root: Path, target_id: str) -> Path:
    """Return host-local runtime state isolated by physical remote target.

    Device pairing/token state is Gateway-specific.  Reusing one runtime policy
    across multiple nodes must therefore not make those nodes share the same
    OpenClaw client state directory.
    """

    root = Path(state_root).expanduser().resolve()
    safe_id = "".join(
        ch if ch.isalnum() or ch in {"-", "_", "."} else "_"
        for ch in str(target_id).strip()
    ).strip(".")
    if not safe_id:
        raise ValueError("remote runtime target id cannot be empty")
    return root / "openclaw-remote-targets" / safe_id


class RemoteTargetCapacity:
    """Process-local hard admission bound shared by profiles on one target."""

    def __init__(self, maximum: int) -> None:
        if maximum < 1:
            raise ValueError("remote runtime target capacity must be >= 1")
        self.maximum = maximum
        self._active = 0
        self._lock = threading.RLock()

    @property
    def active(self) -> int:
        with self._lock:
            return self._active

    @property
    def available(self) -> bool:
        return self.active < self.maximum

    @contextmanager
    def lease(self, target_id: str) -> Iterator[None]:
        with self._lock:
            if self._active >= self.maximum:
                raise AgentExecutionError(
                    f"remote runtime target {target_id!r} is at capacity "
                    f"({self._active}/{self.maximum})",
                    classification="session_limit",
                )
            self._active += 1
        try:
            yield
        finally:
            with self._lock:
                self._active = max(0, self._active - 1)


class OpenClawRemoteTargetController:
    """Target-scoped readiness and provenance for an external OpenClaw Gateway."""

    def __init__(
        self,
        target: ExecutionTargetConfig,
        *,
        workdir: Path,
        service: Any,
        capacity: RemoteTargetCapacity,
    ) -> None:
        if target.kind != ExecutionTargetKind.REMOTE_RUNTIME:
            raise ValueError("remote target controller requires remote_runtime target")
        if target.workspace_transport != "shared":
            raise ValueError("remote runtime targets support only shared workspace transport")
        self.target = target
        self.workspace = Path(workdir).expanduser().resolve()
        self.service = service
        self.capacity = capacity
        self._workspace_provenance = hashlib.sha256(
            ("shared\0" + str(self.workspace)).encode("utf-8")
        ).hexdigest()

    def ensure_ready(self) -> None:
        """Probe every admission so partitions/restarts recover without stale state."""

        diagnostic = self.service.probe()
        if getattr(diagnostic, "healthy", False):
            return
        status = str(getattr(diagnostic, "status", "unavailable"))
        detail = str(getattr(diagnostic, "detail", "") or status)
        classification, persistent = {
            "pairing_required": ("authentication_required", True),
            "incompatible": ("configuration_error", True),
            "dependency_missing": ("environment_failure", True),
            "credential_error": ("auth_failure", True),
            "not_installed": ("environment_failure", True),
            "config_error": ("configuration_error", True),
            "unavailable": ("network_transient", False),
        }.get(status, ("network_transient", False))
        raise AgentExecutionError(
            f"remote OpenClaw target {self.target.id!r} is not ready: {detail}",
            classification=classification,
            persistent=persistent,
        )

    def telemetry(self) -> dict[str, Any]:
        return {
            "remote_runtime": True,
            "remote_target_id": self.target.id,
            "remote_target_kind": self.target.kind.value,
            "remote_gateway": _redacted_endpoint(self.target.endpoint),
            "remote_workspace_transport": self.target.workspace_transport,
            "remote_workspace": str(self.workspace),
            "remote_workspace_provenance": self._workspace_provenance,
            "remote_capacity_active": self.capacity.active,
            "remote_capacity_max": self.capacity.maximum,
            "remote_environment": (
                self.target.environment.as_mapping()
                if self.target.environment is not None
                else {}
            ),
            "repository_sync_performed": False,
        }


class RemoteOpenClawAgentRuntime:
    """Decorate an OpenClaw candidate with remote-target health/capacity policy."""

    def __init__(self, delegate: Any, controller: OpenClawRemoteTargetController) -> None:
        self._delegate = delegate
        self._remote_target = controller

    @property
    def availability(self):
        if not self._remote_target.capacity.available:
            return Availability.SESSION_LIMIT
        return self._delegate.availability

    @availability.setter
    def availability(self, value: Any) -> None:
        self._delegate.availability = value

    def execute(self, handoff: Any) -> dict[str, Any]:
        self._remote_target.ensure_ready()
        with self._remote_target.capacity.lease(self._remote_target.target.id):
            return self._delegate.execute(handoff)

    def execute_runtime(self, request: RuntimeExecutionRequest) -> RuntimeExecutionResult:
        self._remote_target.ensure_ready()
        with self._remote_target.capacity.lease(self._remote_target.target.id):
            result = self._delegate.execute_runtime(request)
        metadata = dict(result.runtime_metadata)
        metadata.update(self._remote_target.telemetry())
        return replace(result, runtime_metadata=metadata)

    def cancel(self, execution_id: str) -> bool:
        return bool(self._delegate.cancel(execution_id))

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        return getattr(self._delegate, name)


def _redacted_endpoint(value: str) -> str:
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port is not None else ""
    return urlunsplit((parsed.scheme.lower(), f"{host.lower()}{port}", parsed.path, "", ""))
