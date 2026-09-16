"""Opt-in compaction RPC smoke test against a real loopback Gateway."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from execraft.runtime.openclaw_gateway import OpenClawGatewayClient
from execraft.runtime.openclaw_session_rpc import compact_session
from execraft.runtime_config import OpenClawMode, OpenClawRuntimeOptions


def test_real_gateway_compaction_when_explicit_session_is_configured(tmp_path: Path) -> None:
    url = os.environ.get("EXECRAFT_OPENCLAW_LIVE_COMPACTION_URL", "").strip()
    session_key = os.environ.get("EXECRAFT_OPENCLAW_LIVE_COMPACTION_SESSION", "").strip()
    agent_id = os.environ.get("EXECRAFT_OPENCLAW_LIVE_COMPACTION_AGENT", "").strip()
    if not url or not session_key or not agent_id:
        pytest.skip(
            "set EXECRAFT_OPENCLAW_LIVE_COMPACTION_URL, EXECRAFT_OPENCLAW_LIVE_COMPACTION_SESSION, "
            "and EXECRAFT_OPENCLAW_LIVE_COMPACTION_AGENT for the real compaction smoke test"
        )
    host = (urlsplit(url).hostname or "").lower()
    if host not in {"localhost", "127.0.0.1", "::1"}:
        pytest.skip("Live compaction intentionally supports only a loopback Gateway")

    token_name = "EXECRAFT_OPENCLAW_LIVE_COMPACTION_TOKEN"
    auth_ref = f"env:{token_name}" if os.environ.get(token_name) else ""
    client = OpenClawGatewayClient(
        OpenClawRuntimeOptions(
            mode=OpenClawMode.EXTERNAL,
            gateway=url,
            auth_ref=auth_ref,
            auth_kind="token" if auth_ref else "none",
            reconnect_attempts=0,
            request_timeout_seconds=60,
        ),
        runtime_id="live-compaction",
        state_root=tmp_path,
    )
    try:
        client.connect()
        result = compact_session(
            client,
            session_key,
            agent_id=agent_id,
            timeout_seconds=180,
        )
        assert result.attempted is True
        assert result.error == ""
        assert result.compacted is True
        assert result.tokens_before is not None
        # OpenClaw 2026.7.1-2 reports `tokensBefore` only: neither the compaction
        # response nor `sessions.compaction.list` carries a post-compaction size.
        # When a Gateway does report one it must not be larger than before, and
        # when it does not, continuation correctly declines to reuse the session.
        if result.tokens_after is not None:
            assert result.tokens_after <= result.tokens_before
    finally:
        client.close()
