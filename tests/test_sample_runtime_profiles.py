from pathlib import Path

import yaml

from execraft.agents.config import project_native_agent_configs
from execraft.agents.opencode_registry import load_opencode_provider_registry
from execraft.model_registry import load_model_route_registry
from execraft.targets.config import ExecutionTargetKind
from execraft.orchestrate import AgentCapability


_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_SAMPLE_ROOT = _REPOSITORY_ROOT / "projects" / "sample"


def test_sample_registry_declares_remote_and_local_coder_workers():
    registry = load_opencode_provider_registry(
        _SAMPLE_ROOT / "opencode" / "providers.yaml",
        environ={},
    )

    assert set(registry.by_endpoint_id) == {
        "gpu-node-a",
        "gpu-node-b",
        "local-ollama",
    }
    first = registry.by_endpoint_id["gpu-node-a"]
    second = registry.by_endpoint_id["gpu-node-b"]
    local = registry.by_endpoint_id["local-ollama"]
    assert first.provider_id == "ollama-gpu-a"
    assert second.provider_id == "ollama-gpu-b"
    assert local.provider_id == "ollama-local"
    assert first.base_url == "http://192.0.2.10:11434/v1"
    assert second.base_url == "http://192.0.2.11:11434/v1"
    assert local.base_url == "http://127.0.0.1:11434/v1"
    assert first.models == second.models == local.models
    assert set(first.models) == {"qwen3-coder:30b-32k"}
    assert first.models["qwen3-coder:30b-32k"]["limit"] == {
        "context": 32768,
        "output": 4096,
    }
    assert first.provider_family == second.provider_family == local.provider_family == "ollama"
    assert first.target_kind == second.target_kind == ExecutionTargetKind.INFERENCE_ENDPOINT
    assert local.target_kind == ExecutionTargetKind.LOCAL
    assert first.concurrency_group == "gpu-node-a"
    assert second.concurrency_group == "gpu-node-b"
    assert local.concurrency_group == "local-ollama"


def test_sample_registry_has_runtime_neutral_canonical_routes_and_targets():
    registry = load_model_route_registry(
        _SAMPLE_ROOT / "opencode" / "providers.yaml",
        environ={},
    )

    assert {route.provider for route in registry.routes} == {"ollama"}
    assert {route.provider_alias for route in registry.routes} == {
        "ollama-gpu-a",
        "ollama-gpu-b",
        "ollama-local",
    }
    assert {target.id: target.kind for target in registry.targets} == {
        "gpu-node-a": ExecutionTargetKind.INFERENCE_ENDPOINT,
        "gpu-node-b": ExecutionTargetKind.INFERENCE_ENDPOINT,
        "local-ollama": ExecutionTargetKind.LOCAL,
    }


def test_sample_qwen_coder_agent_is_bounded_to_medium_complexity():
    raw = yaml.safe_load((_SAMPLE_ROOT / "agents.yaml").read_text(encoding="utf-8"))
    configs = project_native_agent_configs(raw)[0]
    coder = next(
        config
        for config in configs
        if config.provider_id == "opencode-ollama-gpu-a-coder"
    )

    assert coder.model == "ollama-gpu-a/qwen3-coder:30b-32k"
    assert coder.enabled is True
    assert coder.auto_approve is True
    assert coder.agent_for_capability(AgentCapability.DECOMPOSE) == "ai-architect"
    assert coder.agent_for_capability(AgentCapability.REVIEW) == "ai-reviewer"
    assert coder.agent_for_capability(AgentCapability.FIX_REVIEW) == "ai-fixer"
    assert coder.format_repair_agent == "ai-contract"
    assert coder.capabilities == frozenset(
        {
            AgentCapability.DECOMPOSE,
            AgentCapability.IMPLEMENT,
            AgentCapability.REVIEW,
            AgentCapability.FIX_REVIEW,
        }
    )
    assert coder.weight_for_capability(AgentCapability.DECOMPOSE) == 70
    assert coder.weight_for_capability(AgentCapability.IMPLEMENT) == 60
    assert coder.max_complexity_for(AgentCapability.DECOMPOSE) == 80
    assert coder.max_complexity_for(AgentCapability.IMPLEMENT) == 50
    assert coder.max_complexity_for(AgentCapability.REVIEW) == 75
    assert coder.max_complexity_for(AgentCapability.FIX_REVIEW) == 55


def test_qwen_coder_modelfile_matches_registered_profile():
    modelfile = (
        _SAMPLE_ROOT
        / "opencode"
        / "ollama"
        / "Modelfile.qwen3-coder-30b-32k"
    ).read_text(encoding="utf-8")

    assert "FROM qwen3-coder:30b" in modelfile
    assert "PARAMETER num_ctx 32768" in modelfile


def test_sample_fallback_models_have_explicit_complexity_caps():
    raw = yaml.safe_load((_SAMPLE_ROOT / "agents.yaml").read_text(encoding="utf-8"))
    configs = {
        config.provider_id: config
        for config in project_native_agent_configs(raw)[0]
    }

    assert "opencode-ollama-gpu-a" not in configs
    assert "opencode-ollama-gpu-b" not in configs

    # sample is schema v4; the caps live on the agent profile, not a provider.
    zen_raw = raw["agents"]["opencode-zen-free"]
    assert zen_raw["max_complexity"] == 65
    assert zen_raw["max_complexity_by_capability"]["review"] == 65

    zen = configs["opencode-zen-free"]
    assert zen.max_complexity == 65
    assert zen.max_complexity_for(AgentCapability.REVIEW) == 65
    assert zen.capabilities == frozenset(
        {AgentCapability.DECOMPOSE, AgentCapability.REVIEW}
    )
    assert zen.max_output_bytes == 16 * 1024 * 1024

    go = configs["opencode-go"]
    assert go.auto_approve is True
    assert go.agent_for_capability(AgentCapability.DECOMPOSE) == "ai-architect"
    assert go.agent_for_capability(AgentCapability.FIX_REVIEW) == "ai-fixer"
    assert go.format_repair_agent == "ai-contract"
    assert go.max_complexity_for(AgentCapability.IMPLEMENT) == 70
    assert go.max_complexity_for(AgentCapability.REVIEW) == 100
    assert go.max_complexity_for(AgentCapability.FIX_REVIEW) == 70

    # Both configured GPU coder workers are active.  Keep this assertion
    # aligned with the schema-v3 profile contract and the documented satellite
    # topology rather than retaining the old single-satellite rollout state.
    assert configs["opencode-ollama-gpu-a-coder"].enabled is True
    assert configs["opencode-ollama-gpu-b-coder"].enabled is True
    assert configs["opencode-ollama-local-coder"].enabled is True


def test_sample_second_coder_mirrors_first_with_distinct_concurrency():
    raw = yaml.safe_load((_SAMPLE_ROOT / "agents.yaml").read_text(encoding="utf-8"))
    configs = {
        config.provider_id: config
        for config in project_native_agent_configs(raw)[0]
    }

    first_coder = configs["opencode-ollama-gpu-a-coder"]
    second_coder = configs["opencode-ollama-gpu-b-coder"]
    assert second_coder.model == "ollama-gpu-b/qwen3-coder:30b-32k"
    assert second_coder.capabilities == first_coder.capabilities
    assert second_coder.capability_weights == first_coder.capability_weights
    assert (
        second_coder.max_complexity_by_capability
        == first_coder.max_complexity_by_capability
    )
    assert second_coder.concurrency_group == "gpu-node-b"
    assert first_coder.concurrency_group == "gpu-node-a"
    assert first_coder.concurrency_group != second_coder.concurrency_group


def test_sample_local_coder_mirrors_satellite_with_distinct_concurrency():
    raw = yaml.safe_load((_SAMPLE_ROOT / "agents.yaml").read_text(encoding="utf-8"))
    configs = {
        config.provider_id: config
        for config in project_native_agent_configs(raw)[0]
    }

    satellite = configs["opencode-ollama-gpu-a-coder"]
    local = configs["opencode-ollama-local-coder"]
    assert local.model == "ollama-local/qwen3-coder:30b-32k"
    assert local.enabled is True
    assert local.capabilities == satellite.capabilities
    assert local.capability_weights == satellite.capability_weights
    assert local.max_complexity_by_capability == satellite.max_complexity_by_capability
    assert local.concurrency_group == "local-ollama"
    assert local.concurrency_group != satellite.concurrency_group


def test_sample_local_endpoint_supports_environment_override():
    registry = load_opencode_provider_registry(
        _SAMPLE_ROOT / "opencode" / "providers.yaml",
        environ={"EXECRAFT_OLLAMA_LOCAL_URL": "http://localhost:22434/v1"},
    )

    local = registry.by_endpoint_id["local-ollama"]
    assert local.base_url == "http://localhost:22434/v1"
    assert local.base_url_source == "environment:EXECRAFT_OLLAMA_LOCAL_URL"
