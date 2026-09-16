from __future__ import annotations

import pytest

from execraft.agents.config import AgentConfigError, parse_execution_config
from execraft.runtime_config import OpenClawMode, OpenClawVersionPolicy, RuntimeKind


def _base_runtime(**overrides):
    runtime = {
        "kind": "openclaw",
        "mode": "external",
        "gateway": "wss://gateway.example/ws",
        "auth_ref": "env:OPENCLAW_GATEWAY_TOKEN",
    }
    runtime.update(overrides)
    return {
        "schema_version": 4,
        "runtimes": {"openclaw-main": runtime},
        "execution_targets": {"local": {"kind": "local"}},
        "model_routes": {
            "model": {"provider": "openai", "model": "gpt", "default_target": "local"}
        },
        "agents": {
            "implementer": {
                "runtime": "openclaw-main",
                "model_route": "model",
                "capabilities": ["implement"],
            }
        },
    }


def test_openclaw_runtime_options_are_normalized_without_secret_values() -> None:
    config = parse_execution_config(
        _base_runtime(
            request_timeout_seconds=45,
            reconnect_attempts=5,
            restart_attempts=0,
            version_policy="warn-unvalidated",
        )
    )
    runtime = config.runtime("openclaw-main")
    assert runtime.kind == RuntimeKind.OPENCLAW
    assert runtime.openclaw is not None
    assert runtime.openclaw.mode == OpenClawMode.EXTERNAL
    assert runtime.openclaw.version_policy == OpenClawVersionPolicy.WARN_UNVALIDATED
    assert runtime.openclaw.request_timeout_seconds == 45
    assert runtime.openclaw.reconnect_attempts == 5
    mapping = runtime.as_mapping()
    assert mapping["auth_ref"] == "env:OPENCLAW_GATEWAY_TOKEN"
    assert "token" not in mapping


def test_minimal_wp1_openclaw_config_remains_parseable() -> None:
    raw = _base_runtime()
    raw["runtimes"]["openclaw-main"] = {"kind": "openclaw"}
    runtime = parse_execution_config(raw).runtime("openclaw-main")
    assert runtime.openclaw is not None
    assert runtime.openclaw.mode == OpenClawMode.MANAGED
    assert runtime.openclaw.gateway == "ws://127.0.0.1:18789"


@pytest.mark.parametrize("gateway", ["http://localhost:18789", "localhost:18789", ""])
def test_openclaw_gateway_requires_websocket_url(gateway: str) -> None:
    with pytest.raises(AgentConfigError, match="ws:// or wss://"):
        parse_execution_config(_base_runtime(gateway=gateway))


def test_native_runtime_rejects_openclaw_lifecycle_fields() -> None:
    raw = _base_runtime()
    raw["runtimes"]["openclaw-main"] = {
        "kind": "native",
        "adapter": "codex",
        "gateway": "ws://127.0.0.1:18789",
    }
    with pytest.raises(AgentConfigError, match="cannot declare OpenClaw fields"):
        parse_execution_config(raw)


def test_managed_gateway_must_be_loopback_and_explicit_port() -> None:
    raw = _base_runtime(mode="managed", gateway="ws://gateway.example:18789")
    with pytest.raises(AgentConfigError, match="loopback"):
        parse_execution_config(raw)

    raw = _base_runtime(mode="managed", gateway="ws://localhost")
    with pytest.raises(AgentConfigError, match="explicit port"):
        parse_execution_config(raw)


def test_managed_wss_requires_explicit_tls_config() -> None:
    raw = _base_runtime(mode="managed", gateway="wss://127.0.0.1:18789")
    with pytest.raises(AgentConfigError, match="TLS-capable config_path"):
        parse_execution_config(raw)
