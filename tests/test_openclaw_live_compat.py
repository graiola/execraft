"""Opt-in compatibility smoke test for the pinned real OpenClaw Gateway."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from execraft.runtime.openclaw_gateway import OpenClawGatewayClient
from execraft.runtime.openclaw_protocol import OPENCLAW_PROTOCOL_VERSION, TESTED_OPENCLAW_VERSIONS
from execraft.runtime_config import OpenClawMode, OpenClawRuntimeOptions


def test_real_gateway_compatibility_when_explicitly_configured(tmp_path: Path) -> None:
    url = os.environ.get("EXECRAFT_OPENCLAW_INTEGRATION_URL", "").strip()
    if not url:
        pytest.skip("set EXECRAFT_OPENCLAW_INTEGRATION_URL for real Gateway compatibility test")
    token_name = "EXECRAFT_OPENCLAW_INTEGRATION_TOKEN"
    if not os.environ.get(token_name):
        pytest.skip(f"set {token_name} for real Gateway compatibility test")
    client = OpenClawGatewayClient(
        OpenClawRuntimeOptions(
            mode=OpenClawMode.EXTERNAL,
            gateway=url,
            auth_ref=f"env:{token_name}",
            reconnect_attempts=0,
        ),
        runtime_id="integration",
        state_root=tmp_path,
    )
    try:
        hello = client.connect()
        assert hello.protocol == OPENCLAW_PROTOCOL_VERSION
        assert hello.server_version in TESTED_OPENCLAW_VERSIONS
        assert client.health() is not None
    finally:
        client.close()
