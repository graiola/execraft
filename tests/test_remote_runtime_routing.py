from __future__ import annotations

from execraft.agents.execution_config import ExecutionArchitectureConfig
from execraft.agents.profile import AgentProfileConfig
from execraft.model_routes import ModelRouteConfig
from execraft.orchestrate.scheduler import AgentCapability
from execraft.routing_compat import evaluate_route_compatibility
from execraft.runtime.openclaw_projection import project_openclaw_config
from execraft.runtime_config import (
    OpenClawMode,
    OpenClawRuntimeOptions,
    RuntimeConfig,
    RuntimeKind,
)
from execraft.targets.config import ExecutionTargetConfig, ExecutionTargetKind


def _execution() -> ExecutionArchitectureConfig:
    runtime = RuntimeConfig(
        id="remote-openclaw",
        kind=RuntimeKind.OPENCLAW,
        openclaw=OpenClawRuntimeOptions(
            mode=OpenClawMode.EXTERNAL,
            # Runtime owns policy and a compatibility/default URL; the selected
            # remote target owns the actual physical Gateway placement.
            gateway="ws://fallback.example:18789",
            auth_kind="none",
        ),
    )
    target = ExecutionTargetConfig(
        id="build01",
        kind=ExecutionTargetKind.REMOTE_RUNTIME,
        endpoint="wss://build01.example:18789",
        concurrency_group="build01",
        workspace_transport="shared",
        max_concurrency=2,
    )
    route = ModelRouteConfig(
        id="qwen-remote",
        provider="ollama",
        provider_alias="ollama-build01",
        model="qwen3-coder:30b-32k",
        endpoint="http://127.0.0.1:11434",
        api_family="ollama",
        default_target=target.id,
    )
    profile = AgentProfileConfig(
        id="remote-worker",
        name="remote-worker",
        enabled=True,
        capabilities=frozenset({AgentCapability.IMPLEMENT}),
        runtime_id=runtime.id,
        model_route_id=route.id,
        target_id=target.id,
    )
    return ExecutionArchitectureConfig(
        agents=(profile,),
        runtimes=(runtime,),
        model_routes=(route,),
        targets=(target,),
        source_schema_version=4,
    )


def test_remote_runtime_gateway_and_model_endpoint_are_independent_dimensions():
    execution = _execution()
    runtime = execution.runtime("remote-openclaw")
    route = execution.model_route("qwen-remote")
    target = execution.target("build01")

    compatibility = evaluate_route_compatibility(runtime, route, target)
    assert compatibility.compatible, compatibility.reason

    projection = project_openclaw_config(execution, runtime.id)
    provider = projection.config["models"]["providers"]["ollama-build01"]
    assert provider["baseUrl"] == "http://127.0.0.1:11434"
    assert target.endpoint == "wss://build01.example:18789"
    assert runtime.openclaw is not None
    assert runtime.openclaw.gateway == "ws://fallback.example:18789"
