from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import yaml

from execraft.agents import parse_agent_configs, project_native_agent_configs
from execraft.agents.config import parse_execution_config
from execraft.agents.execution_compat import project_native_legacy_configs
from execraft.agents.execution_identity import execution_identity_for_provider
from execraft.runtime.native import build_native_runtime
from execraft.model_registry import load_model_route_registry


ROOT = Path(__file__).resolve().parents[1]


def test_pre_v4_opencode_identity_falls_back_to_the_native_runtime() -> None:
    """A config that names no runtime still resolves to the Native runtime.

    This kept being covered by the shipped sample config until that project
    migrated to schema v4, so it is pinned to an explicit v1-v3 shape here
    rather than to whichever schema a shipped project happens to use.
    """

    raw = {
        "schema_version": 3,
        "providers": {
            "worker": {
                "adapter": "opencode",
                "provider_id": "opencode-ollama-gpu-a-coder",
                "binary": "opencode",
                "model": "ollama-gpu-a/qwen3-coder:30b-32k",
                "capabilities": ["implement"],
                "concurrency_group": "gpu-node-a",
            }
        },
    }
    provider = parse_agent_configs(raw, include_disabled=True)[0]
    registry = load_model_route_registry(ROOT / "projects/sample/opencode/providers.yaml")

    identity = execution_identity_for_provider(provider, model_registry=registry)

    assert identity.runtime_id == "native"
    assert identity.runtime_backend == "opencode"
    assert identity.model_provider == "ollama"
    assert identity.target_id == "gpu-node-a"


def test_v4_opencode_identity_uses_authoritative_config_ids() -> None:
    """Schema v4 carries its own route/runtime ids; the registry only enriches.

    Under v1-v3 the route id was synthesized from the registry endpoint (see
    :func:`test_pre_v4_opencode_identity_falls_back_to_the_native_runtime`).
    Once a project declares ``model_routes`` those ids are authoritative, while
    the registry still supplies physical placement.
    """

    raw = yaml.safe_load((ROOT / "projects/sample/agents.yaml").read_text(encoding="utf-8"))
    providers = project_native_agent_configs(raw)[0]
    provider = next(
        item
        for item in providers
        if item.provider_id == "opencode-ollama-gpu-a-coder"
    )
    registry = load_model_route_registry(ROOT / "projects/sample/opencode/providers.yaml")

    identity = execution_identity_for_provider(provider, model_registry=registry)

    assert identity.candidate_id == provider.provider_id
    # sample is schema v4, so the runtime is the one the config names.
    assert identity.runtime_id == "native-opencode"
    assert identity.runtime_backend == "opencode"
    assert identity.model_route_id == "model-ollama-gpu-a-qwen3-coder-30b-32k"
    assert identity.model_provider == "ollama"
    assert identity.model == "qwen3-coder:30b-32k"
    assert identity.target_id == "gpu-node-a"
    assert identity.target_kind == "inference_endpoint"
    assert identity.concurrency_group == "gpu-node-a"


def test_physical_target_concurrency_group_wins_over_legacy_alias_group() -> None:
    raw = yaml.safe_load((ROOT / "projects/sample/agents.yaml").read_text(encoding="utf-8"))
    provider = next(
        item
        for item in project_native_agent_configs(raw)[0]
        if item.provider_id == "opencode-ollama-gpu-a-coder"
    )
    provider = replace(provider, concurrency_group="legacy-per-alias")
    registry = load_model_route_registry(ROOT / "projects/sample/opencode/providers.yaml")

    identity = execution_identity_for_provider(provider, model_registry=registry)

    assert identity.concurrency_group == "gpu-node-a"


def test_schema_v4_projection_preserves_normalized_execution_identity(tmp_path: Path) -> None:
    raw = {
        "schema_version": 4,
        "runtimes": {
            "native-opencode": {
                "kind": "native",
                "adapter": "opencode",
                "binary": "opencode",
            }
        },
        "execution_targets": {
            "gpu-node": {
                "kind": "inference_endpoint",
                "endpoint": "http://10.0.0.2:11434/v1",
                "concurrency_group": "gpu-node",
            }
        },
        "model_routes": {
            "qwen": {
                "provider": "ollama",
                "provider_alias": "ollama-gpu",
                "model": "qwen3-coder:30b-32k",
                "endpoint": "http://10.0.0.2:11434/v1",
                "api_family": "openai-compatible",
                "default_target": "gpu-node",
            }
        },
        "agents": {
            "implementer-qwen": {
                "runtime": "native-opencode",
                "model_route": "qwen",
                "capabilities": ["implement"],
                "priority": 100,
            }
        },
    }
    normalized = parse_execution_config(raw)
    provider = project_native_legacy_configs(normalized)[0]
    runtime = build_native_runtime(provider, workdir=tmp_path, read_only=False)

    identity = runtime.execution_identity
    assert identity.candidate_id == "implementer-qwen"
    assert identity.runtime_id == "native-opencode"
    assert identity.runtime_backend == "opencode"
    assert identity.model_route_id == "qwen"
    assert identity.model_provider == "ollama"
    assert identity.model == "qwen3-coder:30b-32k"
    assert identity.target_id == "gpu-node"
    assert identity.target_kind == "inference_endpoint"
    assert identity.concurrency_group == "gpu-node"
