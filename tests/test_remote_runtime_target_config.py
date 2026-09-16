from __future__ import annotations
import pytest
from execraft.agents.target_config import parse_execution_target
from execraft.agents.config_errors import AgentConfigError
from execraft.targets.config import ExecutionTargetKind


def test_remote_runtime_target_parses_environment_and_capacity():
    target=parse_execution_target("build01", {
        "kind":"remote_runtime", "endpoint":"wss://build01.example/ws",
        "workspace_transport":"shared", "max_concurrency":2,
        "environment":{"toolchains":["gcc-14"],"tools":["docker"],"gpu":["gpu"],"models":["qwen"],"sandbox":True},
    })
    assert target.kind == ExecutionTargetKind.REMOTE_RUNTIME
    assert target.workspace_transport == "shared"
    assert target.max_concurrency == 2
    assert target.environment is not None and target.environment.sandbox is True
    assert target.as_mapping()["environment"]["tools"] == ["docker"]


def test_remote_target_requires_gateway_url_without_embedded_credentials():
    with pytest.raises(AgentConfigError, match="ws:// or wss://"):
        parse_execution_target("x", {"kind":"remote_runtime","endpoint":"http://build01"})
    with pytest.raises(AgentConfigError, match="must not contain credentials"):
        parse_execution_target("x", {"kind":"remote_runtime","endpoint":"wss://u:p@build01/ws"})
    with pytest.raises(AgentConfigError, match="query/fragment secrets"):
        parse_execution_target("x", {"kind":"remote_runtime","endpoint":"wss://build01/ws?token=secret"})


def test_remote_only_fields_are_rejected_on_inference_target():
    with pytest.raises(AgentConfigError, match="remote-runtime-only"):
        parse_execution_target("gpu", {"kind":"inference_endpoint","endpoint":"http://gpu:11434","max_concurrency":2})


def test_rejects_unimplemented_workspace_transport():
    with pytest.raises(AgentConfigError, match="only workspace_transport 'shared'"):
        parse_execution_target("x", {"kind":"remote_runtime","endpoint":"ws://build01:18789","workspace_transport":"rsync"})
