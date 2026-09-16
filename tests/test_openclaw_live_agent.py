"""Opt-in cold-agent smoke test against the pinned real OpenClaw Gateway."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from execraft.agents.profile import AgentExecutionPolicy, AgentProfileConfig
from execraft.execution_identity import ExecutionIdentity
from execraft.orchestrate.scheduler import AgentCapability, StructuredHandoff
from execraft.runtime.contracts import RuntimeExecutionRequest
from execraft.runtime.openclaw_agent import OpenClawAgentRuntime, OpenClawRuntimeHost
from execraft.runtime.openclaw_service import OpenClawGatewayService
from execraft.runtime_config import OpenClawMode, OpenClawRuntimeOptions, RuntimeConfig, RuntimeKind


def test_real_gateway_cold_agent_when_explicitly_configured(tmp_path: Path) -> None:
    url = os.environ.get("EXECRAFT_OPENCLAW_LIVE_AGENT_URL", "").strip()
    agent_id = os.environ.get("EXECRAFT_OPENCLAW_LIVE_AGENT_ID", "").strip()
    if not url or not agent_id:
        pytest.skip(
            "set EXECRAFT_OPENCLAW_LIVE_AGENT_URL and EXECRAFT_OPENCLAW_LIVE_AGENT_ID "
            "for the real cold-agent smoke test"
        )
    host = (urlsplit(url).hostname or "").lower()
    if host not in {"localhost", "127.0.0.1", "::1"}:
        pytest.skip("Live agent execution intentionally supports only a loopback Gateway")

    token_name = "EXECRAFT_OPENCLAW_LIVE_AGENT_TOKEN"
    auth_ref = f"env:{token_name}" if os.environ.get(token_name) else ""
    auth_kind = "token" if auth_ref else "none"
    runtime_config = RuntimeConfig(
        id="openclaw-live-agent",
        kind=RuntimeKind.OPENCLAW,
        openclaw=OpenClawRuntimeOptions(
            mode=OpenClawMode.EXTERNAL,
            gateway=url,
            auth_ref=auth_ref,
            auth_kind=auth_kind,
            reconnect_attempts=0,
            request_timeout_seconds=60,
        ),
    )
    profile = AgentProfileConfig(
        id=agent_id,
        name="Live compatibility agent",
        enabled=True,
        capabilities=frozenset({AgentCapability.REVIEW}),
        runtime_id=runtime_config.id,
        model_route_id="live-gateway-config",
        policy=AgentExecutionPolicy(timeout_seconds=60),
    )
    identity = ExecutionIdentity(
        candidate_id=agent_id,
        runtime_id=runtime_config.id,
        runtime_backend="gateway",
        model_route_id="live-gateway-config",
        model_provider="",
        model="",
        target_id="local-openclaw",
        target_kind="local",
        concurrency_group="local-openclaw",
        legacy_provider_id=agent_id,
    )
    service = OpenClawGatewayService(runtime_config, state_root=tmp_path / "state")
    runtime = OpenClawAgentRuntime(
        profile=profile,
        runtime=runtime_config,
        identity=identity,
        host=OpenClawRuntimeHost(service),
    )
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    handoff = StructuredHandoff(
        work_package_id="LIVE-AGENT",
        stage="review",
        summary="Return a concise successful review response without changing files.",
        working_directory=str(workdir),
        requirements=["Do not modify files", "Return a non-empty final response"],
    )
    result = runtime.execute_runtime(
        RuntimeExecutionRequest(
            identity=identity,
            handoff=handoff,
            capability="review",
            package_id="LIVE-AGENT",
            stage="review",
        )
    )
    assert result.output["ok"] is True
    assert str(result.output.get("final_message", "")).strip()
    assert result.runtime_metadata["cold_start"] is True
    assert result.session_ref is not None
