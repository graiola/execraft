"""OpenClaw Gateway lifecycle/diagnostic service used by setup and doctor paths."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from execraft.runtime_config import OpenClawMode, RuntimeConfig, RuntimeKind

from .openclaw_auth import (
    CredentialResolver,
    OpenClawAuthDependencyError,
    OpenClawCredentialError,
    resolve_environment_credential,
)
from .openclaw_gateway import (
    OpenClawDependencyError,
    OpenClawGatewayClient,
    OpenClawGatewayError,
    OpenClawPairingRequired,
    OpenClawVersionMismatch,
)
from .openclaw_projection import to_gateway_config
from .openclaw_process import (
    OpenClawConfigError,
    OpenClawExecutableMissing,
    OpenClawManagedProcess,
    OpenClawProcessError,
)


@dataclass(frozen=True)
class OpenClawDiagnostic:
    runtime_id: str
    mode: str
    status: str
    gateway: str
    protocol: int | None = None
    version: str = ""
    warning: str = ""
    detail: str = ""
    methods: tuple[str, ...] = ()

    @property
    def healthy(self) -> bool:
        return self.status in {"healthy", "healthy_unvalidated"}

    def as_mapping(self) -> dict[str, Any]:
        return {
            "runtime_id": self.runtime_id,
            "mode": self.mode,
            "status": self.status,
            "healthy": self.healthy,
            "gateway": self.gateway,
            "protocol": self.protocol,
            "version": self.version,
            "warning": self.warning,
            "detail": self.detail,
            "methods": list(self.methods),
        }


class OpenClawGatewayService:
    """Own a managed Gateway when configured, otherwise only the client."""

    def __init__(
        self,
        runtime: RuntimeConfig,
        *,
        state_root: Path,
        client_factory: Callable[..., OpenClawGatewayClient] = OpenClawGatewayClient,
        process_factory: Callable[..., OpenClawManagedProcess] = OpenClawManagedProcess,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        credential_resolver: CredentialResolver = resolve_environment_credential,
        config_payload: Mapping[str, object] | None = None,
        credential_env_refs: Iterable[str] = (),
        **client_kwargs: Any,
    ) -> None:
        if runtime.kind != RuntimeKind.OPENCLAW or runtime.openclaw is None:
            raise ValueError("OpenClawGatewayService requires an OpenClaw RuntimeConfig")
        self.runtime = runtime
        self.options = runtime.openclaw
        self._state_root = Path(state_root).expanduser().resolve()
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._client = client_factory(
            self.options,
            runtime_id=runtime.id,
            state_root=self._state_root,
            credential_resolver=credential_resolver,
            **client_kwargs,
        )
        self._process = (
            process_factory(
                self.options,
                runtime_id=runtime.id,
                state_root=self._state_root,
                credential_resolver=credential_resolver,
                config_payload=(
                    to_gateway_config(config_payload)
                    if config_payload is not None
                    else None
                ),
                credential_env_refs=credential_env_refs,
            )
            if self.options.mode == OpenClawMode.MANAGED
            else None
        )

    @property
    def client(self) -> OpenClawGatewayClient:
        return self._client

    @property
    def process(self) -> OpenClawManagedProcess | None:
        return self._process

    def start(self) -> OpenClawDiagnostic:
        """Start/connect with bounded managed-process restart attempts.

        A managed ``start()`` returning an unhealthy diagnostic never leaves an
        owned child running. This keeps doctor/GUI probes safe even when their
        callers forget an explicit ``stop()`` and makes restart attempts true
        process restarts rather than repeated probes against one wedged child.
        """

        attempts = (self.options.restart_attempts + 1) if self._process else 1
        last: OpenClawDiagnostic | None = None
        for attempt in range(attempts):
            start_failure = self._start_managed_process()
            if start_failure is not None:
                return start_failure
            last = self._wait_until_ready()
            if last.healthy:
                return last
            terminal = last.status in {"pairing_required", "incompatible", "config_error"}
            exhausted = self._process is None or attempt + 1 >= attempts
            if terminal or exhausted:
                return self._cleanup_failed_start(last)
            self._reset_managed_attempt()
            self._sleeper(min(0.25 * (2**attempt), 1.0))
        return self._cleanup_failed_start(
            last or self._diagnostic("unavailable", "OpenClaw Gateway did not start")
        )

    def probe(self) -> OpenClawDiagnostic:
        """Connect to an existing Gateway and issue an authenticated health RPC."""

        try:
            hello = self._client.connect()
            health = self._client.health()
            warning = self._client.state.warning
            status = "healthy_unvalidated" if warning else "healthy"
            detail = "Gateway health RPC succeeded"
            if isinstance(health, Mapping):
                detail = str(health.get("status", health.get("ok", detail)))
            return OpenClawDiagnostic(
                runtime_id=self.runtime.id,
                mode=self.options.mode.value,
                status=status,
                gateway=self.options.gateway,
                protocol=hello.protocol,
                version=hello.server_version,
                warning=warning,
                detail=detail,
                methods=tuple(sorted(hello.features.methods)),
            )
        except OpenClawPairingRequired as exc:
            return self._diagnostic("pairing_required", str(exc))
        except OpenClawVersionMismatch as exc:
            return self._diagnostic("incompatible", str(exc))
        except OpenClawExecutableMissing as exc:
            return self._diagnostic("not_installed", str(exc))
        except (OpenClawDependencyError, OpenClawAuthDependencyError) as exc:
            return self._diagnostic("dependency_missing", str(exc))
        except OpenClawCredentialError as exc:
            return self._diagnostic("credential_error", str(exc))
        except OpenClawConfigError as exc:
            return self._diagnostic("config_error", str(exc))
        except (OpenClawGatewayError, OpenClawProcessError, OSError) as exc:
            return self._diagnostic("unavailable", str(exc))

    def stop(self) -> None:
        """Release transport state and always reap an owned managed child."""

        client_error: Exception | None = None
        try:
            self._client.close()
        except Exception as exc:  # process cleanup must still run
            client_error = exc
        process_error: Exception | None = None
        if self._process is not None:
            try:
                self._process.stop()
            except Exception as exc:
                process_error = exc
        if process_error is not None:
            if client_error is not None:
                raise OpenClawProcessError(
                    "OpenClaw Gateway cleanup failed for both transport and managed process: "
                    f"transport={client_error}; process={process_error}"
                ) from process_error
            raise process_error
        if client_error is not None:
            raise client_error

    def _start_managed_process(self) -> OpenClawDiagnostic | None:
        if self._process is None:
            return None
        try:
            self._process.start()
        except OpenClawExecutableMissing as exc:
            return self._cleanup_failed_start(self._diagnostic("not_installed", str(exc)))
        except OpenClawCredentialError as exc:
            return self._cleanup_failed_start(self._diagnostic("credential_error", str(exc)))
        except OpenClawProcessError as exc:
            return self._cleanup_failed_start(self._diagnostic("unavailable", str(exc)))
        return None

    def _reset_managed_attempt(self) -> None:
        """Close both transport and child before one bounded restart attempt."""

        self.stop()

    def _cleanup_failed_start(self, diagnostic: OpenClawDiagnostic) -> OpenClawDiagnostic:
        """Return an unhealthy diagnostic only after best-effort owned cleanup."""

        if self._process is None:
            self._client.close()
            return diagnostic
        warning = ""
        try:
            self.stop()
        except Exception as exc:
            warning = f"managed Gateway cleanup failed after startup failure: {exc}"
        if not warning:
            return diagnostic
        detail = diagnostic.detail
        if detail:
            detail += f"; {warning}"
        else:
            detail = warning
        return OpenClawDiagnostic(
            runtime_id=diagnostic.runtime_id,
            mode=diagnostic.mode,
            status=diagnostic.status,
            gateway=diagnostic.gateway,
            protocol=diagnostic.protocol,
            version=diagnostic.version,
            warning=warning,
            detail=detail,
            methods=diagnostic.methods,
        )

    def _wait_until_ready(self) -> OpenClawDiagnostic:
        deadline = self._monotonic() + float(self.options.startup_timeout_seconds)
        last = self._diagnostic("starting", "waiting for Gateway protocol readiness")
        while self._monotonic() < deadline:
            if self._process is not None:
                try:
                    self._process.check_startup_exit()
                except OpenClawConfigError as exc:
                    return self._diagnostic("config_error", str(exc))
                except OpenClawProcessError as exc:
                    return self._diagnostic("unavailable", str(exc))
            last = self.probe()
            if last.healthy or last.status in {"pairing_required", "incompatible"}:
                return last
            self._sleeper(0.1)
        return self._diagnostic(
            "unavailable",
            f"Gateway did not become ready within {self.options.startup_timeout_seconds}s; "
            f"last status: {last.detail}",
        )

    def _diagnostic(self, status: str, detail: str) -> OpenClawDiagnostic:
        return OpenClawDiagnostic(
            runtime_id=self.runtime.id,
            mode=self.options.mode.value,
            status=status,
            gateway=self.options.gateway,
            detail=detail,
        )

    def __enter__(self) -> "OpenClawGatewayService":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()
