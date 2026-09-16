"""Synchronous OpenClaw Gateway WebSocket/RPC client.

The client owns protocol framing, challenge authentication, request correlation,
event dispatch and bounded reconnect. It never reads OpenClaw private state and
never automatically replays an RPC after a disconnect: callers must explicitly
retry side-effecting operations with an idempotency key.
"""

from __future__ import annotations

import json
import platform as host_platform
import threading
import time
import uuid
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from execraft.runtime_config import OpenClawRuntimeOptions, OpenClawVersionPolicy

from .openclaw_auth import (
    CredentialResolver,
    DeviceIdentityStore,
    GatewayCredential,
    build_device_auth_payload_v3,
    resolve_environment_credential,
    required_runtime_scopes,
    sign_device_payload,
)
from .openclaw_protocol import (
    OPENCLAW_PROTOCOL_VERSION,
    TESTED_OPENCLAW_VERSIONS,
    GatewayError,
    GatewayEvent,
    GatewayHello,
)


class OpenClawGatewayError(RuntimeError):
    """Base class for Gateway connectivity/protocol failures."""


class OpenClawDependencyError(OpenClawGatewayError):
    pass


class OpenClawHandshakeError(OpenClawGatewayError):
    pass


class OpenClawTransportError(OpenClawHandshakeError):
    """Transient connection/open/read failure that may be retried before hello."""


class OpenClawPairingRequired(OpenClawHandshakeError):
    def __init__(self, error: GatewayError) -> None:
        self.error = error
        details = error.details or {}
        request_id = str(details.get("requestId", details.get("request_id", "")))
        suffix = f" (request {request_id})" if request_id else ""
        super().__init__(f"OpenClaw device pairing required{suffix}: {error.message}")


class OpenClawVersionMismatch(OpenClawHandshakeError):
    pass


class OpenClawGatewayDisconnected(OpenClawGatewayError):
    pass


class OpenClawGatewayRequestError(OpenClawGatewayError):
    def __init__(self, method: str, error: GatewayError) -> None:
        self.method = method
        self.error = error
        suffix = f" [{error.detail_code}]" if error.detail_code else ""
        super().__init__(f"OpenClaw RPC {method!r} failed{suffix}: {error.message}")


class OpenClawGatewayTimeout(OpenClawGatewayError):
    pass


class GatewayConnection(Protocol):
    def send(self, data: str) -> None: ...
    def recv(self, timeout: float | None = None) -> str | bytes: ...
    def close(self) -> None: ...


ConnectionFactory = Callable[[str, float], GatewayConnection]
EventHandler = Callable[[GatewayEvent], None]




@dataclass
class _PendingResponse:
    """Track either a single RPC response or OpenClaw's accepted+final pair."""

    expect_final: bool = False
    accepted: Future[Mapping[str, Any]] = field(default_factory=Future)
    final: Future[Mapping[str, Any]] = field(default_factory=Future)

    def accept(self, frame: Mapping[str, Any]) -> bool:
        payload = frame.get("payload")
        status = str(payload.get("status", "")) if isinstance(payload, Mapping) else ""
        if self.expect_final and status == "accepted" and not self.accepted.done():
            self.accepted.set_result(frame)
            return False
        if not self.accepted.done():
            self.accepted.set_result(frame)
        if not self.final.done():
            self.final.set_result(frame)
        return True

    def fail(self, error: BaseException) -> None:
        for future in (self.accepted, self.final):
            if not future.done():
                future.set_exception(error)


@dataclass(frozen=True)
class OpenClawAcceptedRun:
    """Accepted Gateway agent run with a future for its final response."""

    run_id: str
    session_key: str
    _pending: _PendingResponse = field(repr=False, compare=False)
    _timeout_seconds: float = field(repr=False, compare=False, default=0.0)
    _cleanup: Callable[[], None] = field(repr=False, compare=False, default=lambda: None)

    def wait_final(self, *, timeout_seconds: float | None = None) -> Any:
        timeout = timeout_seconds or self._timeout_seconds
        try:
            response = self._pending.final.result(timeout=timeout)
        except FutureTimeoutError as exc:
            self._cleanup()
            raise OpenClawGatewayTimeout(
                f"OpenClaw agent run {self.run_id!r} final response timed out after {timeout:g}s"
            ) from exc
        if not bool(response.get("ok", False)):
            raise OpenClawGatewayRequestError(
                "agent", GatewayError.from_mapping(response.get("error"))
            )
        return response.get("payload")


@dataclass(frozen=True)
class GatewayConnectionState:
    connected: bool
    hello: GatewayHello | None = None
    warning: str = ""
    last_shutdown_reason: str = ""
    restart_expected_ms: int | None = None


class OpenClawGatewayClient:
    """One authenticated operator connection to an OpenClaw Gateway."""

    def __init__(
        self,
        options: OpenClawRuntimeOptions,
        *,
        runtime_id: str,
        state_root: Path,
        credential_resolver: CredentialResolver = resolve_environment_credential,
        connection_factory: ConnectionFactory | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        device_store: DeviceIdentityStore | None = None,
        payload_signer: Callable[[Any, str], str] = sign_device_payload,
    ) -> None:
        self.options = options
        self.runtime_id = runtime_id
        self._state_root = Path(state_root).expanduser().resolve()
        self._credentials = credential_resolver
        self._factory = connection_factory or _default_connection_factory
        self._sleeper = sleeper
        self._device_store = device_store or DeviceIdentityStore(
            self._state_root / "openclaw" / runtime_id / "client"
        )
        self._payload_signer = payload_signer
        self._connection: GatewayConnection | None = None
        self._hello: GatewayHello | None = None
        self._warning = ""
        self._last_shutdown_reason = ""
        self._restart_expected_ms: int | None = None
        self._pending: dict[str, _PendingResponse] = {}
        self._event_handlers: list[EventHandler] = []
        self._lock = threading.RLock()
        self._reader: threading.Thread | None = None
        self._closed = False

    @property
    def hello(self) -> GatewayHello | None:
        with self._lock:
            return self._hello

    @property
    def state(self) -> GatewayConnectionState:
        with self._lock:
            return GatewayConnectionState(
                connected=self._connection is not None and self._hello is not None,
                hello=self._hello,
                warning=self._warning,
                last_shutdown_reason=self._last_shutdown_reason,
                restart_expected_ms=self._restart_expected_ms,
            )

    def add_event_handler(self, handler: EventHandler) -> None:
        with self._lock:
            if handler not in self._event_handlers:
                self._event_handlers.append(handler)

    def remove_event_handler(self, handler: EventHandler) -> None:
        with self._lock:
            if handler in self._event_handlers:
                self._event_handlers.remove(handler)

    def connect(self) -> GatewayHello:
        """Connect with bounded retry for startup/reconnect failures."""

        with self._lock:
            self._closed = False
            if self._connection is not None and self._hello is not None:
                return self._hello
        attempts = self.options.reconnect_attempts + 1
        last_error: BaseException | None = None
        for index in range(attempts):
            try:
                hello = self._connect_once()
                self._start_reader()
                return hello
            except _StartupUnavailable as exc:
                last_error = exc
                self._drop_connection(exc)
                if index + 1 >= attempts:
                    break
                self._sleeper(max(0.0, exc.retry_after_ms / 1000.0))
            except (OpenClawPairingRequired, OpenClawVersionMismatch):
                self._drop_connection(None)
                raise
            except OpenClawTransportError as exc:
                last_error = exc
                self._drop_connection(exc)
                if index + 1 >= attempts:
                    break
                self._sleeper(min(0.25 * (2**index), 2.0))
            except OpenClawHandshakeError:
                # Protocol/auth-shape failures are deterministic; retrying them
                # only hides an incompatible or malformed peer.
                self._drop_connection(None)
                raise
            except BaseException as exc:
                last_error = exc
                self._drop_connection(exc)
                if index + 1 >= attempts:
                    break
                self._sleeper(min(0.25 * (2**index), 2.0))
        if isinstance(last_error, OpenClawGatewayError):
            raise last_error
        raise OpenClawHandshakeError(
            f"could not connect to OpenClaw Gateway {self.options.gateway}: {last_error}"
        ) from last_error

    def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
        idempotency_key: str = "",
    ) -> Any:
        """Perform one RPC without transparently replaying it after disconnect."""

        if not method.strip():
            raise ValueError("Gateway RPC method cannot be empty")
        if self.hello is None:
            self.connect()
        request_id = uuid.uuid4().hex
        payload = dict(params or {})
        if idempotency_key and "idempotencyKey" not in payload:
            payload["idempotencyKey"] = idempotency_key
        frame: dict[str, Any] = {
            "type": "req",
            "id": request_id,
            "method": method,
            "params": payload,
        }
        pending = _PendingResponse()
        with self._lock:
            connection = self._connection
            if connection is None:
                raise OpenClawGatewayDisconnected("OpenClaw Gateway is disconnected")
            self._pending[request_id] = pending
        try:
            self._send_json(connection, frame)
        except BaseException as exc:
            with self._lock:
                self._pending.pop(request_id, None)
            self._drop_connection(exc)
            raise OpenClawGatewayDisconnected(
                f"OpenClaw Gateway disconnected while sending {method!r}"
            ) from exc
        timeout = timeout_seconds or float(self.options.request_timeout_seconds)
        try:
            response = pending.final.result(timeout=timeout)
        except FutureTimeoutError as exc:
            with self._lock:
                self._pending.pop(request_id, None)
            raise OpenClawGatewayTimeout(
                f"OpenClaw RPC {method!r} timed out after {timeout:g}s"
            ) from exc
        if not bool(response.get("ok", False)):
            raise OpenClawGatewayRequestError(method, GatewayError.from_mapping(response.get("error")))
        return response.get("payload")

    def start_agent(
        self,
        params: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
        idempotency_key: str,
    ) -> OpenClawAcceptedRun:
        """Start an agent run and retain OpenClaw's second/final response.

        The Gateway intentionally answers ``agent`` twice: an immediate accepted
        acknowledgement and a later terminal payload using the same request ID.
        Generic ``request`` remains single-response so no other RPC changes
        semantics.
        """

        if not idempotency_key.strip():
            raise ValueError("OpenClaw agent requires an idempotency key")
        if self.hello is None:
            self.connect()
        request_id = uuid.uuid4().hex
        payload = dict(params)
        payload.setdefault("idempotencyKey", idempotency_key)
        pending = _PendingResponse(expect_final=True)
        with self._lock:
            connection = self._connection
            if connection is None:
                raise OpenClawGatewayDisconnected("OpenClaw Gateway is disconnected")
            self._pending[request_id] = pending
        try:
            self._send_json(
                connection,
                {"type": "req", "id": request_id, "method": "agent", "params": payload},
            )
        except BaseException as exc:
            with self._lock:
                self._pending.pop(request_id, None)
            self._drop_connection(exc)
            raise OpenClawGatewayDisconnected(
                "OpenClaw Gateway disconnected while sending 'agent'"
            ) from exc
        timeout = timeout_seconds or float(self.options.request_timeout_seconds)
        try:
            accepted = pending.accepted.result(timeout=timeout)
        except FutureTimeoutError as exc:
            with self._lock:
                self._pending.pop(request_id, None)
            raise OpenClawGatewayTimeout(
                f"OpenClaw RPC 'agent' acceptance timed out after {timeout:g}s"
            ) from exc
        if not bool(accepted.get("ok", False)):
            with self._lock:
                self._pending.pop(request_id, None)
            raise OpenClawGatewayRequestError(
                "agent", GatewayError.from_mapping(accepted.get("error"))
            )
        accepted_payload = accepted.get("payload")
        if not isinstance(accepted_payload, Mapping):
            self._discard_pending(request_id, pending)
            raise OpenClawHandshakeError("OpenClaw agent acceptance payload must be an object")
        run_id = str(accepted_payload.get("runId", "")).strip()
        session_key = str(accepted_payload.get("sessionKey", "")).strip()
        if not run_id:
            self._discard_pending(request_id, pending)
            raise OpenClawHandshakeError("OpenClaw agent acceptance did not include runId")
        return OpenClawAcceptedRun(
            run_id=run_id,
            session_key=session_key,
            _pending=pending,
            _timeout_seconds=timeout,
            _cleanup=lambda: self._discard_pending(request_id, pending),
        )

    def wait_agent_run(
        self, run_id: str, *, timeout_seconds: float | None = None
    ) -> Any:
        if not run_id.strip():
            raise ValueError("run_id cannot be empty")
        timeout = timeout_seconds or float(self.options.request_timeout_seconds)
        return self.request(
            "agent.wait",
            {"runId": run_id, "timeoutMs": max(1, int(timeout * 1000))},
            timeout_seconds=timeout + min(5.0, max(1.0, timeout * 0.05)),
        )

    def health(self) -> Any:
        return self.request("health")

    def describe_session(self, session_key: str) -> Mapping[str, Any] | None:
        """Return one public Gateway session row when it can be proven to exist.

        Continuation fails closed: an absent session or a Gateway surface that
        cannot describe exact sessions is treated as non-reusable, allowing the
        caller to cold-reconstruct from Execraft's durable handoff instead of
        sending a delta into unknown runtime state.
        """

        key = session_key.strip()
        if not key:
            raise ValueError("session_key cannot be empty")
        try:
            payload = self.request("sessions.describe", {"key": key})
        except OpenClawGatewayRequestError as exc:
            code = (exc.error.detail_code or exc.error.code).upper()
            if code in {"NOT_FOUND", "METHOD_NOT_FOUND", "INVALID_REQUEST"}:
                return None
            raise
        if not isinstance(payload, Mapping):
            return None
        session = payload.get("session")
        if isinstance(session, Mapping):
            resolved = str(session.get("key", key)).strip()
            row = dict(session)
            return row if resolved == key and _session_row_reusable(row) else None
        # Some compatible protocol-v4 surfaces return the row directly.
        resolved = str(payload.get("key", "")).strip()
        row = dict(payload)
        return row if resolved == key and _session_row_reusable(row) else None

    def cancel_run(self, run_id: str, *, session_key: str = "") -> Any:
        """Cancel one accepted agent run using current and compatible stop RPCs.

        Current OpenClaw CLI clients use ``chat.abort`` when both the exact run
        and session are known. ``sessions.abort`` remains the public session
        fallback and also covers Gateway versions/surfaces where exact chat
        abort is unavailable. Neither request is transparently replayed.
        """

        run_id = run_id.strip()
        session_key = session_key.strip()
        if not run_id:
            raise ValueError("run_id cannot be empty")
        if session_key:
            try:
                response = self.request(
                    "chat.abort",
                    {"sessionKey": session_key, "runId": run_id},
                    idempotency_key=f"execraft-cancel-chat-{uuid.uuid4().hex}",
                )
                if _abort_confirmed(response, run_id):
                    return response
            except OpenClawGatewayRequestError as exc:
                code = (exc.error.detail_code or exc.error.code).upper()
                if code not in {"NOT_FOUND", "METHOD_NOT_FOUND", "INVALID_REQUEST"}:
                    raise

        params: dict[str, Any] = {"runId": run_id}
        if session_key:
            params["key"] = session_key
        response = self.request(
            "sessions.abort",
            params,
            idempotency_key=f"execraft-cancel-session-{uuid.uuid4().hex}",
        )
        return response

    def _discard_pending(self, request_id: str, pending: _PendingResponse) -> None:
        with self._lock:
            if self._pending.get(request_id) is pending:
                self._pending.pop(request_id, None)

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self._drop_connection(None)
        reader = self._reader
        if reader is not None and reader.is_alive() and reader is not threading.current_thread():
            reader.join(timeout=1.0)

    def _connect_once(self) -> GatewayHello:
        credential = self._resolve_credential()
        connection = self._factory(
            self.options.gateway, float(self.options.handshake_timeout_seconds)
        )
        with self._lock:
            self._connection = connection
            self._hello = None
        challenge = self._receive_json(connection, float(self.options.handshake_timeout_seconds))
        if challenge.get("type") != "event" or challenge.get("event") != "connect.challenge":
            raise OpenClawHandshakeError("Gateway did not send connect.challenge first")
        challenge_payload = challenge.get("payload")
        challenge_map = challenge_payload if isinstance(challenge_payload, Mapping) else {}
        nonce = str(challenge_map.get("nonce", "")).strip()
        signed_at = challenge_map.get("ts")
        if not nonce or not isinstance(signed_at, int) or signed_at < 0:
            raise OpenClawHandshakeError("Gateway sent an invalid connect.challenge")
        params = self._build_connect_params(
            nonce=nonce,
            signed_at_ms=signed_at,
            credential=credential,
        )
        request_id = uuid.uuid4().hex
        self._send_json(
            connection,
            {"type": "req", "id": request_id, "method": "connect", "params": params},
        )
        while True:
            frame = self._receive_json(connection, float(self.options.handshake_timeout_seconds))
            if frame.get("type") == "event":
                self._dispatch_event(frame)
                continue
            if frame.get("type") != "res" or frame.get("id") != request_id:
                raise OpenClawHandshakeError("unexpected frame during Gateway handshake")
            if not bool(frame.get("ok", False)):
                error = GatewayError.from_mapping(frame.get("error"))
                if error.detail_reason == "startup-sidecars" and error.retryable:
                    raise _StartupUnavailable(error.retry_after_ms or 250)
                if error.code == "PAIRING_REQUIRED" or error.detail_code == "PAIRING_REQUIRED":
                    raise OpenClawPairingRequired(error)
                raise OpenClawHandshakeError(
                    f"OpenClaw connect failed [{error.code}/{error.detail_code}]: {error.message}"
                )
            payload = frame.get("payload")
            if not isinstance(payload, Mapping) or payload.get("type") != "hello-ok":
                raise OpenClawHandshakeError("Gateway connect response is not hello-ok")
            _validate_hello_shape(payload)
            hello = GatewayHello.from_payload(payload)
            self._validate_hello(hello)
            if hello.device_token:
                self._device_store.save_token(hello.device_token, hello.scopes)
            with self._lock:
                self._hello = hello
            return hello

    def _resolve_credential(self) -> GatewayCredential | None:
        return self._credentials(self.options.auth_ref, self.options.auth_kind)

    def _build_connect_params(
        self,
        *,
        nonce: str,
        signed_at_ms: int,
        credential: GatewayCredential | None,
    ) -> dict[str, Any]:
        identity = self._device_store.load_or_create_identity()
        stored = self._device_store.load_token()
        using_stored_token = credential is None and stored is not None
        stored_scopes = tuple(stored.scopes) if using_stored_token and stored else ()
        scopes = required_runtime_scopes(stored_scopes)
        auth: dict[str, str] = {}
        signature_token = ""
        if credential is not None:
            if credential.kind == "token":
                auth["token"] = credential.value
                signature_token = credential.value
            elif credential.kind == "password":
                auth["password"] = credential.value
        elif stored is not None:
            # Match OpenClaw's connect-auth precedence: a stored device token
            # becomes the selected auth token only when no explicit shared
            # token/password is configured.
            auth["token"] = stored.token
            auth["deviceToken"] = stored.token
            signature_token = stored.token
        platform = host_platform.system().lower() or "unknown"
        device_family = "desktop"
        payload = build_device_auth_payload_v3(
            identity=identity,
            client_id="cli",
            client_mode="cli",
            role="operator",
            scopes=scopes,
            signed_at_ms=signed_at_ms,
            token=signature_token,
            nonce=nonce,
            platform=platform,
            device_family=device_family,
        )
        device = {
            "id": identity.device_id,
            "publicKey": identity.public_key,
            "signature": self._payload_signer(identity, payload),
            "signedAt": signed_at_ms,
            "nonce": nonce,
        }
        params: dict[str, Any] = {
            "minProtocol": OPENCLAW_PROTOCOL_VERSION,
            "maxProtocol": OPENCLAW_PROTOCOL_VERSION,
            "client": {
                "id": "cli",
                "version": "0.1.0",
                "platform": platform,
                "mode": "cli",
                "deviceFamily": device_family,
            },
            "role": "operator",
            "scopes": list(scopes),
            "caps": [],
            "commands": [],
            "permissions": {},
            "auth": auth,
            "locale": "en-US",
            "userAgent": "execraft/0.1.0",
            "device": device,
        }
        if not auth:
            params.pop("auth")
        return params

    def _validate_hello(self, hello: GatewayHello) -> None:
        if hello.protocol != OPENCLAW_PROTOCOL_VERSION:
            raise OpenClawVersionMismatch(
                f"OpenClaw Gateway protocol {hello.protocol} is incompatible; "
                f"Execraft requires OpenClaw protocol {OPENCLAW_PROTOCOL_VERSION}"
            )
        required_scopes = frozenset(required_runtime_scopes())
        missing_scopes = sorted(required_scopes.difference(hello.scopes))
        if missing_scopes:
            raise OpenClawHandshakeError(
                "OpenClaw Gateway did not grant required operator scopes: "
                + ", ".join(missing_scopes)
            )
        version = hello.server_version
        if version in TESTED_OPENCLAW_VERSIONS:
            with self._lock:
                self._warning = ""
            return
        message = (
            f"OpenClaw Gateway {version or '<unknown>'} is unvalidated; tested versions: "
            + ", ".join(sorted(TESTED_OPENCLAW_VERSIONS))
        )
        if self.options.version_policy == OpenClawVersionPolicy.PINNED_COMPATIBLE:
            raise OpenClawVersionMismatch(message)
        with self._lock:
            self._warning = message

    def _start_reader(self) -> None:
        with self._lock:
            if self._reader is not None and self._reader.is_alive():
                return
            self._reader = threading.Thread(
                target=self._reader_loop,
                name=f"execraft-openclaw-{self.runtime_id}",
                daemon=True,
            )
            self._reader.start()

    def _reader_loop(self) -> None:
        try:
            while True:
                with self._lock:
                    if self._closed:
                        return
                    connection = self._connection
                if connection is None:
                    return
                frame = self._receive_json(connection, None)
                frame_type = frame.get("type")
                if frame_type == "res":
                    request_id = str(frame.get("id", ""))
                    with self._lock:
                        pending = self._pending.get(request_id)
                    if pending is not None:
                        completed = pending.accept(frame)
                        if completed:
                            with self._lock:
                                self._pending.pop(request_id, None)
                elif frame_type == "event":
                    self._dispatch_event(frame)
        except BaseException as exc:
            self._drop_connection(exc)

    def _dispatch_event(self, frame: Mapping[str, Any]) -> None:
        sequence = frame.get("seq")
        event = GatewayEvent(
            name=str(frame.get("event", "")),
            payload=frame.get("payload"),
            sequence=sequence if isinstance(sequence, int) else None,
            state_version=(
                frame.get("stateVersion")
                if isinstance(frame.get("stateVersion"), Mapping)
                else None
            ),
        )
        if event.name == "shutdown":
            payload = event.payload if isinstance(event.payload, Mapping) else {}
            with self._lock:
                self._last_shutdown_reason = str(payload.get("reason", ""))
                value = payload.get("restartExpectedMs")
                self._restart_expected_ms = value if isinstance(value, int) else None
        with self._lock:
            handlers = tuple(self._event_handlers)
        for handler in handlers:
            try:
                handler(event)
            except Exception:
                # Event observers must not kill the transport reader.
                continue

    def _drop_connection(self, cause: BaseException | None) -> None:
        with self._lock:
            connection = self._connection
            self._connection = None
            self._hello = None
            pending = list(self._pending.values())
            self._pending.clear()
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        error = OpenClawGatewayDisconnected(
            f"OpenClaw Gateway connection closed{': ' + str(cause) if cause else ''}"
        )
        for pending_response in pending:
            pending_response.fail(error)

    @staticmethod
    def _send_json(connection: GatewayConnection, frame: Mapping[str, Any]) -> None:
        connection.send(json.dumps(frame, separators=(",", ":"), ensure_ascii=False))

    @staticmethod
    def _receive_json(
        connection: GatewayConnection, timeout: float | None
    ) -> Mapping[str, Any]:
        try:
            data = connection.recv(timeout=timeout)
        except TimeoutError as exc:
            raise OpenClawTransportError("timed out waiting for Gateway frame") from exc
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        try:
            frame = json.loads(data)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OpenClawHandshakeError("Gateway sent invalid JSON") from exc
        if not isinstance(frame, Mapping):
            raise OpenClawHandshakeError("Gateway frame must be a JSON object")
        return frame


def _session_row_reusable(row: Mapping[str, Any]) -> bool:
    """Reject archived/terminal placement rows as continuation anchors."""

    if bool(row.get("archived")) or str(row.get("archivedAt", "")).strip():
        return False
    placement = row.get("placement")
    if isinstance(placement, Mapping):
        state = str(placement.get("state", "")).strip().lower()
        if state in {"failed", "reclaimed", "destroyed"}:
            return False
    return True

def _abort_confirmed(payload: Any, run_id: str) -> bool:
    if not isinstance(payload, Mapping):
        return False
    if payload.get("aborted") is not True and payload.get("ok") is not True:
        return False
    run_ids = payload.get("runIds")
    return not isinstance(run_ids, list) or run_id in {str(item) for item in run_ids}


class _StartupUnavailable(OpenClawHandshakeError):
    def __init__(self, retry_after_ms: int) -> None:
        self.retry_after_ms = max(0, retry_after_ms)
        super().__init__("OpenClaw Gateway startup sidecars are not ready")


def _default_connection_factory(url: str, timeout: float) -> GatewayConnection:
    try:
        from websockets.sync.client import connect
    except ImportError as exc:  # pragma: no cover - optional dependency.
        raise OpenClawDependencyError(
            "OpenClaw runtime requires the 'openclaw' optional dependency set "
            "(install execraft[openclaw])"
        ) from exc
    try:
        return connect(url, open_timeout=timeout, close_timeout=min(timeout, 5.0))
    except Exception as exc:
        raise OpenClawTransportError(f"cannot open Gateway WebSocket {url}: {exc}") from exc


def _validate_hello_shape(payload: Mapping[str, Any]) -> None:
    """Fail closed when required v4 hello sections are absent or malformed."""

    for field in ("server", "features", "snapshot", "auth", "policy"):
        if not isinstance(payload.get(field), Mapping):
            raise OpenClawHandshakeError(
                f"OpenClaw hello-ok field {field!r} must be a JSON object"
            )
    server = payload["server"]
    if not str(server.get("version", "")).strip() or not str(server.get("connId", "")).strip():
        raise OpenClawHandshakeError("OpenClaw hello-ok server identity is incomplete")
