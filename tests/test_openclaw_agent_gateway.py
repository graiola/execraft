from pathlib import Path

from execraft.runtime.openclaw_gateway import OpenClawGatewayClient
from execraft.runtime_config import OpenClawMode, OpenClawRuntimeOptions
from tests.fakes.openclaw_gateway import AgentRunGatewayConnection, FakeDeviceStore


def _client(tmp_path: Path, connection: AgentRunGatewayConnection):
    return OpenClawGatewayClient(
        OpenClawRuntimeOptions(
            mode=OpenClawMode.EXTERNAL,
            gateway="ws://127.0.0.1:18789",
            auth_kind="none",
            request_timeout_seconds=2,
        ),
        runtime_id="openclaw-test",
        state_root=tmp_path,
        connection_factory=lambda *_args: connection,
        device_store=FakeDeviceStore(),
        payload_signer=lambda *_args: "signature",
    )


def test_agent_rpc_preserves_accepted_and_terminal_responses(tmp_path: Path):
    connection = AgentRunGatewayConnection()
    client = _client(tmp_path, connection)
    try:
        accepted = client.start_agent(
            {
                "message": "implement WP7",
                "agentId": "implementer",
                "sessionKey": "agent:implementer:cold",
            },
            idempotency_key="execraft-wp7-1",
        )
        assert accepted.run_id == "run-wp7-1"
        assert accepted.session_key == "agent:impl:execraft-cold"
        assert client.wait_agent_run(accepted.run_id)["status"] == "ok"
        final = accepted.wait_final()
        assert final["status"] == "ok"
        assert final["result"]["payloads"][0]["text"] == "completed by OpenClaw"
        agent_frame = next(item for item in connection.sent if item.get("method") == "agent")
        assert agent_frame["params"]["idempotencyKey"] == "execraft-wp7-1"
    finally:
        client.close()
