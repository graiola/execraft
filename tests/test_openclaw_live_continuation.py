"""Opt-in persistent-session smoke test against a real OpenClaw Gateway."""

from __future__ import annotations

from dataclasses import replace
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


def test_real_gateway_persistent_session_when_explicitly_configured(tmp_path: Path) -> None:
    url = os.environ.get("EXECRAFT_OPENCLAW_LIVE_CONTINUATION_URL", "").strip()
    agent_id = os.environ.get("EXECRAFT_OPENCLAW_LIVE_CONTINUATION_AGENT", "").strip()
    if not url or not agent_id:
        pytest.skip(
            "set EXECRAFT_OPENCLAW_LIVE_CONTINUATION_URL and EXECRAFT_OPENCLAW_LIVE_CONTINUATION_AGENT "
            "for the real continuation smoke test"
        )
    host = (urlsplit(url).hostname or "").lower()
    if host not in {"localhost", "127.0.0.1", "::1"}:
        pytest.skip("Live continuation intentionally supports only a loopback Gateway")

    token_name = "EXECRAFT_OPENCLAW_LIVE_CONTINUATION_TOKEN"
    auth_ref = f"env:{token_name}" if os.environ.get(token_name) else ""
    runtime_config = RuntimeConfig(
        id="openclaw-live-continuation",
        kind=RuntimeKind.OPENCLAW,
        openclaw=OpenClawRuntimeOptions(
            mode=OpenClawMode.EXTERNAL,
            gateway=url,
            auth_ref=auth_ref,
            auth_kind="token" if auth_ref else "none",
            reconnect_attempts=0,
            request_timeout_seconds=60,
        ),
    )
    profile = AgentProfileConfig(
        id=agent_id,
        name="Live continuation agent",
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
    full = StructuredHandoff(
        work_package_id="LIVE-CONTINUATION",
        stage="review",
        summary="Remember the marker SESSION-CONTINUATION-MARKER and return it in your response.",
        working_directory=str(workdir),
        requirements=["Do not modify files"],
    )
    epoch = "live-continuation-epoch"
    first = runtime.execute_runtime(
        RuntimeExecutionRequest(
            identity=identity,
            handoff=full,
            capability="review",
            package_id="LIVE-CONTINUATION",
            stage="review",
            context_epoch=epoch,
        )
    )
    assert first.session_ref is not None
    assert first.runtime_metadata["cold_start"] is True

    delta = replace(
        full,
        summary="Return the marker I asked you to remember in the previous turn.",
        requirements=[],
    )
    second = runtime.execute_runtime(
        RuntimeExecutionRequest(
            identity=identity,
            handoff=full,
            capability="review",
            package_id="LIVE-CONTINUATION",
            stage="review",
            attempt=2,
            session_ref=first.session_ref,
            context_epoch=epoch,
            continuation_handoff=delta,
        )
    )
    assert second.output["ok"] is True
    assert second.runtime_metadata["session_reused"] is True
    assert second.runtime_metadata["cold_start"] is False
    assert "SESSION-CONTINUATION-MARKER" in str(second.output.get("final_message", ""))
