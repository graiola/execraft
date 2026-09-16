from __future__ import annotations

import time
from pathlib import Path

import pytest

from execraft.runtime.openclaw_auth import GatewayCredential, StoredDeviceToken
from execraft.runtime.openclaw_gateway import (
    OpenClawGatewayClient,
    OpenClawGatewayRequestError,
    OpenClawPairingRequired,
    OpenClawVersionMismatch,
)
from execraft.runtime_config import OpenClawRuntimeOptions, OpenClawVersionPolicy
from tests.fakes.openclaw_gateway import FakeDeviceStore, ScriptedGatewayConnection


def _credential(_ref: str, _kind: str) -> GatewayCredential:
    return GatewayCredential(kind="token", value="shared-token")


def _client(tmp_path: Path, connection, *, store=None, options=None):
    return OpenClawGatewayClient(
        options or OpenClawRuntimeOptions(auth_ref="env:OPENCLAW_TEST_TOKEN"),
        runtime_id="openclaw",
        state_root=tmp_path,
        credential_resolver=_credential,
        connection_factory=lambda _url, _timeout: connection,
        device_store=store or FakeDeviceStore(),
        payload_signer=lambda _identity, payload: "signature:" + str(len(payload)),
    )


def test_gateway_handshake_uses_protocol_v4_device_proof_and_persists_token(tmp_path: Path) -> None:
    connection = ScriptedGatewayConnection()
    store = FakeDeviceStore()
    client = _client(tmp_path, connection, store=store)
    hello = client.connect()
    try:
        assert hello.protocol == 4
        assert hello.server_version == "2026.7.1-2"
        assert hello.features.methods >= {"health", "sessions.abort"}
        connect = connection.sent[0]
        params = connect["params"]
        assert params["minProtocol"] == params["maxProtocol"] == 4
        assert params["auth"]["token"] == "shared-token"
        assert params["device"]["id"] == "a" * 64
        assert params["device"]["nonce"] == "nonce-123"
        assert store.saved_token == "device-token-1"
        assert store.saved_scopes == (
            "operator.admin",
            "operator.approvals",
            "operator.read",
            "operator.write",
        )
    finally:
        client.close()


def test_explicit_shared_token_takes_precedence_over_stored_device_token(tmp_path: Path) -> None:
    connection = ScriptedGatewayConnection()
    store = FakeDeviceStore(
        stored_token=StoredDeviceToken("stored-device", ("operator.read",))
    )
    client = _client(tmp_path, connection, store=store)
    client.connect()
    try:
        params = connection.sent[0]["params"]
        assert params["scopes"] == [
            "operator.admin",
            "operator.approvals",
            "operator.read",
            "operator.write",
        ]
        assert params["auth"] == {"token": "shared-token"}
    finally:
        client.close()


def test_stored_device_token_is_used_when_no_shared_credential_is_configured(
    tmp_path: Path,
) -> None:
    connection = ScriptedGatewayConnection()
    store = FakeDeviceStore(
        stored_token=StoredDeviceToken("stored-device", ("operator.read",))
    )
    client = OpenClawGatewayClient(
        OpenClawRuntimeOptions(auth_kind="none"),
        runtime_id="openclaw",
        state_root=tmp_path,
        credential_resolver=lambda _ref, _kind: None,
        connection_factory=lambda _url, _timeout: connection,
        device_store=store,
        payload_signer=lambda _identity, _payload: "signature",
    )
    client.connect()
    try:
        params = connection.sent[0]["params"]
        assert params["scopes"] == [
            "operator.admin",
            "operator.approvals",
            "operator.read",
            "operator.write",
        ]
        assert params["auth"]["token"] == "stored-device"
        assert params["auth"]["deviceToken"] == "stored-device"
    finally:
        client.close()




def test_gateway_rejects_missing_required_runtime_scope(tmp_path: Path) -> None:
    connection = ScriptedGatewayConnection(
        granted_scopes=("operator.read", "operator.write")
    )
    client = _client(tmp_path, connection)
    from execraft.runtime.openclaw_gateway import OpenClawHandshakeError

    with pytest.raises(OpenClawHandshakeError, match="operator.approvals"):
        client.connect()

def test_gateway_correlates_requests_and_dispatches_events(tmp_path: Path) -> None:
    connection = ScriptedGatewayConnection(emit_event_before_health=True)
    client = _client(tmp_path, connection)
    events = []
    client.add_event_handler(events.append)
    client.connect()
    try:
        assert client.health() == {"status": "ok"}
        deadline = time.time() + 1
        while not events and time.time() < deadline:
            time.sleep(0.01)
        assert events[0].name == "agent"
        assert events[0].sequence == 7
    finally:
        client.close()


def test_cancel_run_is_explicit_and_idempotency_keyed(tmp_path: Path) -> None:
    connection = ScriptedGatewayConnection()
    client = _client(tmp_path, connection)
    client.connect()
    try:
        assert client.cancel_run("run-42", session_key="agent:dev:wp1") == {"aborted": True}
        request = next(frame for frame in connection.sent if frame.get("method") == "chat.abort")
        assert request["params"]["runId"] == "run-42"
        assert request["params"]["sessionKey"] == "agent:dev:wp1"
        assert request["params"]["idempotencyKey"].startswith("execraft-cancel-chat-")
    finally:
        client.close()


def test_cancel_run_falls_back_to_session_abort(tmp_path: Path) -> None:
    connection = ScriptedGatewayConnection(chat_abort_supported=False)
    client = _client(tmp_path, connection)
    client.connect()
    try:
        assert client.cancel_run("run-42", session_key="agent:dev:wp1") == {"aborted": True}
        request = next(frame for frame in connection.sent if frame.get("method") == "sessions.abort")
        assert request["params"] == {
            "runId": "run-42",
            "key": "agent:dev:wp1",
            "idempotencyKey": request["params"]["idempotencyKey"],
        }
        assert request["params"]["idempotencyKey"].startswith("execraft-cancel-session-")
    finally:
        client.close()


def test_pairing_required_is_structured(tmp_path: Path) -> None:
    connection = ScriptedGatewayConnection(
        connect_error={
            "code": "PAIRING_REQUIRED",
            "message": "pair device",
            "details": {"requestId": "pair-123", "recommendedNextStep": "approve"},
        }
    )
    client = _client(tmp_path, connection)
    with pytest.raises(OpenClawPairingRequired, match="pair-123"):
        client.connect()


def test_pinned_policy_rejects_unvalidated_gateway_release(tmp_path: Path) -> None:
    client = _client(tmp_path, ScriptedGatewayConnection(version="2026.7.2"))
    with pytest.raises(OpenClawVersionMismatch, match="unvalidated"):
        client.connect()


def test_warn_policy_accepts_unvalidated_release_with_warning(tmp_path: Path) -> None:
    options = OpenClawRuntimeOptions(
        auth_ref="env:OPENCLAW_TEST_TOKEN",
        version_policy=OpenClawVersionPolicy.WARN_UNVALIDATED,
    )
    client = _client(
        tmp_path,
        ScriptedGatewayConnection(version="2026.7.2"),
        options=options,
    )
    hello = client.connect()
    try:
        assert hello.server_version == "2026.7.2"
        assert "unvalidated" in client.state.warning
    finally:
        client.close()


def test_protocol_mismatch_fails_closed_even_with_warn_version_policy(tmp_path: Path) -> None:
    options = OpenClawRuntimeOptions(
        auth_ref="env:OPENCLAW_TEST_TOKEN",
        version_policy=OpenClawVersionPolicy.WARN_UNVALIDATED,
    )
    client = _client(tmp_path, ScriptedGatewayConnection(protocol=5), options=options)
    with pytest.raises(OpenClawVersionMismatch, match="protocol 5"):
        client.connect()


def test_rpc_error_preserves_structured_gateway_error(tmp_path: Path) -> None:
    connection = ScriptedGatewayConnection()
    client = _client(tmp_path, connection)
    client.connect()
    try:
        with pytest.raises(OpenClawGatewayRequestError) as caught:
            client.request("unknown.method")
        assert caught.value.error.code == "NOT_FOUND"
    finally:
        client.close()


def test_startup_sidecar_unavailable_reconnects_before_hello(tmp_path: Path) -> None:
    first = ScriptedGatewayConnection(
        connect_error={
            "code": "UNAVAILABLE",
            "message": "sidecars starting",
            "retryable": True,
            "retryAfterMs": 0,
            "details": {"reason": "startup-sidecars"},
        }
    )
    second = ScriptedGatewayConnection()
    connections = iter((first, second))
    options = OpenClawRuntimeOptions(
        auth_ref="env:OPENCLAW_TEST_TOKEN",
        reconnect_attempts=1,
    )
    client = OpenClawGatewayClient(
        options,
        runtime_id="openclaw",
        state_root=tmp_path,
        credential_resolver=_credential,
        connection_factory=lambda _url, _timeout: next(connections),
        sleeper=lambda _seconds: None,
        device_store=FakeDeviceStore(),
        payload_signer=lambda _identity, _payload: "signature",
    )
    hello = client.connect()
    try:
        assert hello.server_version == "2026.7.1-2"
        assert first.closed
        assert len(first.sent) == 1
        assert len(second.sent) == 1
    finally:
        client.close()


def test_rpc_timeout_does_not_replay_request(tmp_path: Path) -> None:
    class IgnoreHealthConnection(ScriptedGatewayConnection):
        def send(self, data: str) -> None:
            import json

            frame = json.loads(data)
            if frame.get("method") == "health":
                self.sent.append(frame)
                return
            super().send(data)

    connection = IgnoreHealthConnection()
    client = _client(tmp_path, connection)
    client.connect()
    try:
        from execraft.runtime.openclaw_gateway import OpenClawGatewayTimeout

        with pytest.raises(OpenClawGatewayTimeout):
            client.request("health", timeout_seconds=0.01)
        health_requests = [frame for frame in connection.sent if frame.get("method") == "health"]
        assert len(health_requests) == 1
    finally:
        client.close()


def test_disconnect_does_not_transparently_replay_rpc(tmp_path: Path) -> None:
    class DropHealthConnection(ScriptedGatewayConnection):
        def send(self, data: str) -> None:
            import json

            frame = json.loads(data)
            if frame.get("method") == "health":
                self.sent.append(frame)
                self._queue.put(OSError("network lost"))
                return
            super().send(data)

    connection = DropHealthConnection()
    factory_calls = 0

    def factory(_url: str, _timeout: float):
        nonlocal factory_calls
        factory_calls += 1
        return connection

    client = OpenClawGatewayClient(
        OpenClawRuntimeOptions(auth_ref="TOKEN", reconnect_attempts=3),
        runtime_id="openclaw",
        state_root=tmp_path,
        credential_resolver=_credential,
        connection_factory=factory,
        device_store=FakeDeviceStore(),
        payload_signer=lambda _identity, _payload: "signature",
    )
    client.connect()
    try:
        from execraft.runtime.openclaw_gateway import OpenClawGatewayDisconnected

        with pytest.raises(OpenClawGatewayDisconnected):
            client.health()
        assert factory_calls == 1
        assert len([item for item in connection.sent if item.get("method") == "health"]) == 1
    finally:
        client.close()


def test_malformed_required_hello_section_fails_closed(tmp_path: Path) -> None:
    class MalformedHelloConnection(ScriptedGatewayConnection):
        def send(self, data: str) -> None:
            import json

            frame = json.loads(data)
            super().send(data)
            if frame.get("method") == "connect" and self.connect_error is None:
                response = json.loads(self._queue.get_nowait())
                response["payload"].pop("policy")
                self._queue.put(json.dumps(response))

    client = _client(tmp_path, MalformedHelloConnection())
    from execraft.runtime.openclaw_gateway import OpenClawHandshakeError

    with pytest.raises(OpenClawHandshakeError, match="policy"):
        client.connect()


def test_describe_session_proves_exact_public_session_or_returns_none(tmp_path: Path) -> None:
    connection = ScriptedGatewayConnection(session_keys={"agent:impl:wp1"})
    client = _client(tmp_path, connection)
    client.connect()
    try:
        assert client.describe_session("agent:impl:wp1") == {"key": "agent:impl:wp1"}
        assert client.describe_session("agent:impl:missing") is None
        requests = [frame for frame in connection.sent if frame.get("method") == "sessions.describe"]
        assert [item["params"]["key"] for item in requests] == [
            "agent:impl:wp1",
            "agent:impl:missing",
        ]
    finally:
        client.close()


def test_describe_session_rejects_archived_row(tmp_path: Path) -> None:
    class ArchivedSessionConnection(ScriptedGatewayConnection):
        def send(self, data: str) -> None:
            import json

            frame = json.loads(data)
            if frame.get("method") == "sessions.describe":
                self.sent.append(frame)
                self._queue.put(
                    json.dumps(
                        {
                            "type": "res",
                            "id": frame["id"],
                            "ok": True,
                            "payload": {
                                "session": {
                                    "key": frame["params"]["key"],
                                    "archivedAt": "2026-08-29T00:00:00Z",
                                }
                            },
                        }
                    )
                )
                return
            super().send(data)

    client = _client(tmp_path, ArchivedSessionConnection())
    client.connect()
    try:
        assert client.describe_session("agent:impl:old") is None
    finally:
        client.close()
