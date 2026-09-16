from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from execraft.agents.execution_config import ExecutionArchitectureConfig
from execraft.agents.profile import AgentProfileConfig
from execraft.model_registry import load_model_route_registry
from execraft.model_routes import ModelRouteConfig
from execraft.runtime.openclaw_projection import (
    OpenClawProjectionError,
    materialize_openclaw_config,
    to_gateway_config,
    project_openclaw_config,
)
from execraft.runtime_config import OpenClawMode, OpenClawRuntimeOptions, RuntimeConfig, RuntimeKind
from execraft.targets.config import ExecutionTargetConfig, ExecutionTargetKind
from execraft.orchestrate.scheduler import AgentCapability


def _runtime(*, mode: OpenClawMode = OpenClawMode.MANAGED, gateway: str = "ws://127.0.0.1:18789") -> RuntimeConfig:
    return RuntimeConfig(
        id="openclaw",
        kind=RuntimeKind.OPENCLAW,
        openclaw=OpenClawRuntimeOptions(mode=mode, gateway=gateway, auth_kind="none"),
    )


def _profile(route: str, *, candidate: str = "worker", target: str = "") -> AgentProfileConfig:
    return AgentProfileConfig(
        id=candidate,
        name=candidate,
        enabled=True,
        capabilities=frozenset({AgentCapability.IMPLEMENT}),
        runtime_id="openclaw",
        model_route_id=route,
        target_id=target,
    )


def _execution(*, routes, targets=(), agents=None, runtime=None) -> ExecutionArchitectureConfig:
    return ExecutionArchitectureConfig(
        agents=tuple(agents or (_profile(routes[0].id),)),
        runtimes=(runtime or _runtime(),),
        model_routes=tuple(routes),
        targets=tuple(targets),
        source_schema_version=4,
    )


def test_local_ollama_projection_uses_native_openclaw_api_without_v1() -> None:
    route = ModelRouteConfig(
        id="qwen-local",
        provider="ollama",
        provider_alias="ollama-local",
        model="qwen3-coder:30b-32k",
        endpoint="http://127.0.0.1:11434/v1",
        api_family="openai-compatible",
        context_window=32768,
        default_target="local-ollama",
    )
    target = ExecutionTargetConfig(
        id="local-ollama",
        kind=ExecutionTargetKind.LOCAL,
        endpoint="http://127.0.0.1:11434/v1",
        concurrency_group="local-ollama",
    )

    projection = project_openclaw_config(_execution(routes=(route,), targets=(target,)), "openclaw")
    provider = projection.config["models"]["providers"]["ollama-local"]
    assert provider["api"] == "ollama"
    assert provider["baseUrl"] == "http://127.0.0.1:11434"
    assert provider["apiKey"] == "ollama-local"
    assert provider["models"] == [
        {
            "id": "qwen3-coder:30b-32k",
            "name": "qwen3-coder:30b-32k",
            "contextWindow": 32768,
        }
    ]
    assert projection.config["agents"]["entries"]["worker"]["model"] == (
        "ollama-local/qwen3-coder:30b-32k"
    )


def test_same_wp3_satellite_inventory_projects_to_native_ollama() -> None:
    registry = load_model_route_registry(Path("projects/sample/opencode/providers.yaml"), environ={})
    route = registry.route_for_model("ollama-gpu-a/qwen3-coder:30b-32k")
    assert route is not None
    target = next(item for item in registry.targets if item.id == "gpu-node-a")
    execution = _execution(routes=(route,), targets=(target,))

    projection = project_openclaw_config(execution, "openclaw")
    provider = projection.config["models"]["providers"]["ollama-gpu-a"]
    assert provider["api"] == "ollama"
    assert provider["baseUrl"] == "http://192.0.2.10:11434"
    assert provider["apiKey"] == "ollama-local"
    assert projection.models == ("ollama-gpu-a/qwen3-coder:30b-32k",)


def test_cloud_route_projects_secret_ref_without_secret_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "super-secret-value")
    route = ModelRouteConfig(
        id="openai-main",
        provider="openai",
        model="gpt-5.6",
        credential_ref="env:OPENAI_API_KEY",
        context_window=196608,
    )
    projection = project_openclaw_config(_execution(routes=(route,)), "openclaw")
    provider = projection.config["models"]["providers"]["openai"]
    assert provider["apiKey"] == {
        "source": "env",
        "provider": "default",
        "id": "OPENAI_API_KEY",
    }
    assert provider["models"] == [
        {"id": "gpt-5.6", "name": "gpt-5.6", "contextWindow": 196608}
    ]
    assert "super-secret-value" not in projection.to_bytes().decode()
    assert projection.credential_refs == ("OPENAI_API_KEY",)


def test_cloud_builtin_without_explicit_credential_uses_openclaw_auth_profiles() -> None:
    route = ModelRouteConfig(id="claude", provider="anthropic", model="claude-opus-4-6")
    projection = project_openclaw_config(_execution(routes=(route,)), "openclaw")
    assert "models" not in projection.config
    assert projection.config["agents"]["entries"]["worker"]["model"] == (
        "anthropic/claude-opus-4-6"
    )


def test_custom_openai_compatible_private_route_gets_non_secret_local_marker() -> None:
    route = ModelRouteConfig(
        id="vllm",
        provider="vllm",
        provider_alias="vllm-lan",
        model="coder",
        endpoint="http://10.0.0.9:8000/v1",
        api_family="openai-compatible",
        default_target="vllm-lan",
    )
    target = ExecutionTargetConfig(
        id="vllm-lan",
        kind=ExecutionTargetKind.INFERENCE_ENDPOINT,
        endpoint=route.endpoint,
    )
    projection = project_openclaw_config(_execution(routes=(route,), targets=(target,)), "openclaw")
    provider = projection.config["models"]["providers"]["vllm-lan"]
    assert provider["api"] == "openai-completions"
    assert provider["apiKey"] == "execraft-local"


def test_public_custom_route_requires_credential_reference() -> None:
    route = ModelRouteConfig(
        id="proxy",
        provider="custom",
        provider_alias="custom-proxy",
        model="coder",
        endpoint="https://models.example.invalid/v1",
        api_family="openai-compatible",
        default_target="proxy",
    )
    target = ExecutionTargetConfig(
        id="proxy",
        kind=ExecutionTargetKind.INFERENCE_ENDPOINT,
        endpoint=route.endpoint,
    )
    with pytest.raises(OpenClawProjectionError, match="requires credential_ref"):
        project_openclaw_config(_execution(routes=(route,), targets=(target,)), "openclaw")



def test_public_custom_route_with_credential_projects_secret_ref() -> None:
    route = ModelRouteConfig(
        id="proxy",
        provider="custom",
        provider_alias="custom-proxy",
        model="coder",
        endpoint="https://models.example.invalid/v1",
        api_family="openai-compatible",
        credential_ref="CUSTOM_PROXY_API_KEY",
        default_target="proxy",
    )
    target = ExecutionTargetConfig(
        id="proxy",
        kind=ExecutionTargetKind.INFERENCE_ENDPOINT,
        endpoint=route.endpoint,
    )
    projection = project_openclaw_config(
        _execution(routes=(route,), targets=(target,)), "openclaw"
    )
    provider = projection.config["models"]["providers"]["custom-proxy"]
    assert provider["apiKey"] == {
        "source": "env",
        "provider": "default",
        "id": "CUSTOM_PROXY_API_KEY",
    }
    assert provider["api"] == "openai-completions"
    assert projection.credential_refs == ("CUSTOM_PROXY_API_KEY",)


def test_projection_rejects_non_environment_credential_reference() -> None:
    route = ModelRouteConfig(
        id="proxy",
        provider="custom",
        provider_alias="custom-proxy",
        model="coder",
        endpoint="https://models.example.invalid/v1",
        api_family="openai-compatible",
        credential_ref="file:/tmp/secret",
        default_target="proxy",
    )
    target = ExecutionTargetConfig(
        id="proxy",
        kind=ExecutionTargetKind.INFERENCE_ENDPOINT,
        endpoint=route.endpoint,
    )
    with pytest.raises(OpenClawProjectionError, match="environment reference"):
        project_openclaw_config(
            _execution(routes=(route,), targets=(target,)), "openclaw"
        )

def test_remote_external_gateway_cannot_reinterpret_control_host_loopback_target() -> None:
    route = ModelRouteConfig(
        id="local",
        provider="ollama",
        provider_alias="ollama-local",
        model="qwen",
        endpoint="http://127.0.0.1:11434/v1",
        default_target="local",
    )
    target = ExecutionTargetConfig(
        id="local", kind=ExecutionTargetKind.LOCAL, endpoint=route.endpoint
    )
    runtime = _runtime(mode=OpenClawMode.EXTERNAL, gateway="wss://gateway.example.invalid/ws")
    with pytest.raises(OpenClawProjectionError, match="remote runtime placement is not supported here"):
        project_openclaw_config(
            _execution(routes=(route,), targets=(target,), runtime=runtime), "openclaw"
        )


def test_provider_alias_is_required_to_disambiguate_different_endpoints() -> None:
    route_a = ModelRouteConfig(
        id="a", provider="custom", model="a", endpoint="http://10.0.0.1:8000/v1",
        default_target="a",
    )
    route_b = ModelRouteConfig(
        id="b", provider="custom", model="b", endpoint="http://10.0.0.2:8000/v1",
        default_target="b",
    )
    targets = (
        ExecutionTargetConfig(id="a", kind=ExecutionTargetKind.INFERENCE_ENDPOINT, endpoint=route_a.endpoint),
        ExecutionTargetConfig(id="b", kind=ExecutionTargetKind.INFERENCE_ENDPOINT, endpoint=route_b.endpoint),
    )
    agents = (_profile("a", candidate="one"), _profile("b", candidate="two"))
    with pytest.raises(OpenClawProjectionError, match="distinct provider_alias"):
        project_openclaw_config(
            _execution(routes=(route_a, route_b), targets=targets, agents=agents), "openclaw"
        )


def test_projection_is_deterministic_and_materialized_private(tmp_path: Path) -> None:
    route = ModelRouteConfig(
        id="openai", provider="openai", model="gpt-5.6", credential_ref="OPENAI_API_KEY"
    )
    execution = _execution(routes=(route,))
    first = project_openclaw_config(execution, "openclaw")
    second = project_openclaw_config(execution, "openclaw")
    assert first.to_bytes() == second.to_bytes()
    assert first.sha256 == second.sha256

    destination = materialize_openclaw_config(first, tmp_path / "runtime" / "openclaw.json")
    # What lands on disk is the Gateway's shape, not Execraft's internal one.
    assert json.loads(destination.read_text()) == to_gateway_config(first.config)
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_managed_process_materializes_projection_and_refreshes_owned_config(tmp_path: Path) -> None:
    from execraft.runtime.openclaw_process import OpenClawManagedProcess

    executable = tmp_path / "fake-openclaw"
    executable.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(60)\n", encoding="utf-8")
    executable.chmod(0o700)
    route = ModelRouteConfig(
        id="openai", provider="openai", model="gpt-5.6", credential_ref="OPENAI_API_KEY"
    )
    projection = project_openclaw_config(_execution(routes=(route,)), "openclaw")
    options = OpenClawRuntimeOptions(
        executable=str(executable), auth_kind="none"
    )
    manager = OpenClawManagedProcess(
        options,
        runtime_id="openclaw",
        state_root=tmp_path,
        config_payload=projection.config,
    )
    manager.paths.config_path.parent.mkdir(parents=True, exist_ok=True)
    manager.paths.config_path.write_text('{"gateway":{"mode":"local"}}\n', encoding="utf-8")
    try:
        manager.start()
        assert json.loads(manager.paths.config_path.read_text()) == projection.config
    finally:
        manager.stop(timeout_seconds=1)


def test_native_and_openclaw_are_projections_of_same_canonical_satellite() -> None:
    from execraft.agents.opencode_registry import load_opencode_provider_registry

    path = Path("projects/sample/opencode/providers.yaml")
    native = load_opencode_provider_registry(path, environ={})
    generic = native.model_registry
    route = generic.route_for_model("ollama-gpu-a/qwen3-coder:30b-32k")
    assert route is not None
    target = next(item for item in generic.targets if item.id == route.default_target)
    projection = project_openclaw_config(
        _execution(routes=(route,), targets=(target,)), "openclaw"
    )

    native_provider = native.by_provider_id["ollama-gpu-a"].as_opencode_provider()
    openclaw_provider = projection.config["models"]["providers"]["ollama-gpu-a"]
    assert native_provider["options"]["baseURL"] == "http://192.0.2.10:11434/v1"
    assert openclaw_provider["baseUrl"] == "http://192.0.2.10:11434"
    assert route.endpoint == target.endpoint == "http://192.0.2.10:11434/v1"


def test_models_on_same_provider_are_merged_deterministically() -> None:
    routes = (
        ModelRouteConfig(id="b", provider="openai", model="gpt-b", credential_ref="OPENAI_API_KEY", context_window=128000),
        ModelRouteConfig(id="a", provider="openai", model="gpt-a", credential_ref="OPENAI_API_KEY", context_window=64000),
    )
    agents = (_profile("b", candidate="z"), _profile("a", candidate="a"))
    projection = project_openclaw_config(_execution(routes=routes, agents=agents), "openclaw")
    provider = projection.config["models"]["providers"]["openai"]
    assert [item["id"] for item in provider["models"]] == ["gpt-a", "gpt-b"]
    assert list(projection.config["agents"]["entries"]) == ["a", "a-readonly", "z", "z-readonly"]
    assert projection.models == ("openai/gpt-a", "openai/gpt-b")


def test_projection_can_assign_state_owned_agent_workspace_without_changing_model_route(
    tmp_path: Path,
) -> None:
    route = ModelRouteConfig(
        id="openai",
        provider="openai",
        model="gpt-5.6",
        credential_ref="OPENAI_API_KEY",
    )
    execution = _execution(routes=(route,))
    workspace = tmp_path / "state" / "openclaw" / "workspaces" / "worker"

    projection = project_openclaw_config(
        execution,
        "openclaw",
        agent_workspaces={"worker": workspace},
    )

    agent = projection.config["agents"]["entries"]["worker"]
    assert "workspace" not in agent
    assert agent["model"] == "openai/gpt-5.6"
    assert projection.config["skills"]["load"]["extraDirs"] == [
        str((workspace.resolve() / "skills").resolve())
    ]
    assert projection.config["agents"]["defaults"]["skipBootstrap"] is True
    without_workspace = project_openclaw_config(execution, "openclaw")
    assert "skills" not in without_workspace.config


def test_gateway_config_uses_openclaws_agents_list_shape() -> None:
    """OpenClaw 2026.7.1-2 rejects ``agents.entries`` with ``agents: Invalid input``.

    Execraft keeps agents id-keyed internally because subagent, security and
    skill projection all address agents by id; the array shape is produced only
    at the serialization boundary.
    """

    internal = {
        "agents": {
            "defaults": {"skipBootstrap": True},
            "entries": {"b": {"model": "m"}, "a": {"model": "m"}},
        }
    }

    rendered = to_gateway_config(internal)

    assert "entries" not in rendered["agents"]
    assert rendered["agents"]["list"] == [
        {"id": "a", "model": "m"},
        {"id": "b", "model": "m"},
    ]
    assert rendered["agents"]["defaults"] == {"skipBootstrap": True}
    # The internal projection must not be mutated by rendering it.
    assert "entries" in internal["agents"]


def test_compaction_defaults_omit_the_unsupported_enabled_key() -> None:
    """``agents.defaults.compaction`` has no ``enabled`` field in OpenClaw.

    Compaction is intrinsic to the session runtime, so Execraft either imposes a
    policy or leaves the Gateway's own defaults alone.
    """

    route = ModelRouteConfig(
        id="openai", provider="openai", model="gpt-5.6", credential_ref="OPENAI_API_KEY"
    )
    config = project_openclaw_config(_execution(routes=(route,)), "openclaw").config
    compaction = config["agents"]["defaults"]["compaction"]

    assert "enabled" not in compaction
    assert compaction["mode"] == "safeguard"
