from pathlib import Path

import pytest
import yaml

from execraft.model_registry import (
    ModelEndpoint,
    ModelRegistryError,
    ModelRouteRegistry,
    load_model_route_registry,
)
from execraft.routing_compat import evaluate_route_compatibility
from execraft.runtime_config import RuntimeConfig, RuntimeKind
from execraft.targets.config import ExecutionTargetConfig, ExecutionTargetKind
from execraft.targets.inventory import ExecutionTargetInventory


def _write(path: Path, endpoints: dict) -> Path:
    path.write_text(yaml.safe_dump({"schema_version": 1, "endpoints": endpoints}), encoding="utf-8")
    return path


def test_legacy_registry_normalizes_provider_route_and_remote_target(tmp_path: Path):
    registry = load_model_route_registry(
        _write(
            tmp_path / "providers.yaml",
            {
                "gpu": {
                    "provider_id": "ollama-gpu",
                    "base_url": "http://10.0.0.2:11434/v1",
                    "models": {"qwen:latest": {"limit": {"context": 32768}}},
                }
            },
        ),
        environ={},
    )

    endpoint = registry.by_endpoint_id["gpu"]
    route = registry.route_for_model("ollama-gpu/qwen:latest")
    assert endpoint.provider_family == "ollama"
    assert endpoint.provider_alias == "ollama-gpu"
    assert endpoint.target_kind == ExecutionTargetKind.INFERENCE_ENDPOINT
    assert endpoint.target_id == "gpu"
    assert route is not None
    assert route.provider == "ollama"
    assert route.provider_alias == "ollama-gpu"
    assert route.context_window == 32768
    assert route.default_target == "gpu"


def test_legacy_registry_infers_loopback_as_local_target(tmp_path: Path):
    registry = load_model_route_registry(
        _write(
            tmp_path / "providers.yaml",
            {
                "local": {
                    "provider_id": "ollama-local",
                    "base_url": "http://127.0.0.1:11434/v1",
                    "models": {"qwen": {}},
                }
            },
        ),
        environ={},
    )
    assert registry.endpoints[0].target_kind == ExecutionTargetKind.LOCAL


def test_explicit_runtime_neutral_metadata_overrides_legacy_inference(tmp_path: Path):
    registry = load_model_route_registry(
        _write(
            tmp_path / "providers.yaml",
            {
                "node": {
                    "provider_id": "custom-alias",
                    "provider": "ollama",
                    "api_family": "openai-compatible",
                    "target_id": "gpu-a",
                    "target_kind": "inference_endpoint",
                    "concurrency_group": "gpu-a-capacity",
                    "base_url": "http://10.0.0.8:11434/v1",
                    "models": {"coder": {}},
                }
            },
        ),
        environ={},
    )
    endpoint = registry.endpoints[0]
    assert endpoint.provider_family == "ollama"
    assert endpoint.provider_alias == "custom-alias"
    assert endpoint.target_id == "gpu-a"
    assert endpoint.concurrency_group == "gpu-a-capacity"


def test_environment_override_changes_canonical_endpoint_and_target(tmp_path: Path):
    registry = load_model_route_registry(
        _write(
            tmp_path / "providers.yaml",
            {
                "gpu": {
                    "provider_id": "ollama-gpu",
                    "base_url": "http://10.0.0.2:11434/v1",
                    "base_url_env": "TEST_GPU_URL",
                    "models": {"qwen": {}},
                }
            },
        ),
        environ={"TEST_GPU_URL": "http://10.9.0.2:11434/v1/"},
    )
    endpoint = registry.endpoints[0]
    assert endpoint.base_url == "http://10.9.0.2:11434/v1"
    assert endpoint.target_config().endpoint == endpoint.base_url


def test_execution_target_inventory_is_derived_once_from_canonical_registry(tmp_path: Path):
    registry = load_model_route_registry(
        _write(
            tmp_path / "providers.yaml",
            {
                "local": {
                    "provider_id": "ollama-local",
                    "base_url": "http://localhost:11434/v1",
                    "models": {"qwen": {}},
                },
                "gpu": {
                    "provider_id": "ollama-gpu",
                    "base_url": "http://10.0.0.2:11434/v1",
                    "models": {"qwen": {}},
                },
            },
        ),
        environ={},
    )
    inventory = ExecutionTargetInventory.from_model_registry(registry)
    assert set(inventory.by_id) == {"local", "gpu"}
    assert tuple(item.id for item in inventory.local) == ("local",)
    assert tuple(item.id for item in inventory.inference_endpoints) == ("gpu",)


def test_shared_target_requires_consistent_physical_definition():
    common = dict(
        provider_family="ollama",
        name="node",
        base_url_source="test",
        models={"m": {}},
        target_id="gpu",
        target_kind=ExecutionTargetKind.INFERENCE_ENDPOINT,
        concurrency_group="gpu",
    )
    with pytest.raises(ModelRegistryError, match="conflicting endpoint definitions"):
        ModelRouteRegistry(
            (
                ModelEndpoint(endpoint_id="a", provider_alias="ollama-a", base_url="http://10.0.0.2/v1", **common),
                ModelEndpoint(endpoint_id="b", provider_alias="ollama-b", base_url="http://10.0.0.3/v1", **common),
            )
        )


def test_native_opencode_rejects_non_openai_compatible_routed_api():
    runtime = RuntimeConfig(id="native", kind=RuntimeKind.NATIVE, adapter="opencode")
    route = ModelEndpoint(
        endpoint_id="x",
        provider_family="custom",
        provider_alias="custom",
        name="x",
        base_url="http://10.0.0.2/v1",
        base_url_source="test",
        models={"m": {}},
        target_id="x",
        target_kind=ExecutionTargetKind.INFERENCE_ENDPOINT,
        api_family="anthropic-native",
    ).route_config("m")
    result = evaluate_route_compatibility(runtime, route)
    assert result.compatible is False
    assert "OpenCode" in result.reason


def test_native_runtime_rejects_remote_runtime_target():
    runtime = RuntimeConfig(id="native", kind=RuntimeKind.NATIVE, adapter="opencode")
    endpoint = ModelEndpoint(
        endpoint_id="x",
        provider_family="ollama",
        provider_alias="ollama-x",
        name="x",
        base_url="http://10.0.0.2/v1",
        base_url_source="test",
        models={"m": {}},
        target_id="x",
        target_kind=ExecutionTargetKind.INFERENCE_ENDPOINT,
    )
    result = evaluate_route_compatibility(
        runtime,
        endpoint.route_config("m"),
        ExecutionTargetConfig(id="remote", kind=ExecutionTargetKind.REMOTE_RUNTIME),
    )
    assert result.compatible is False


def test_route_and_target_endpoint_mismatch_is_rejected():
    runtime = RuntimeConfig(id="native", kind=RuntimeKind.NATIVE, adapter="opencode")
    endpoint = ModelEndpoint(
        endpoint_id="x",
        provider_family="ollama",
        provider_alias="ollama-x",
        name="x",
        base_url="http://10.0.0.2/v1",
        base_url_source="test",
        models={"m": {}},
        target_id="x",
        target_kind=ExecutionTargetKind.INFERENCE_ENDPOINT,
    )
    result = evaluate_route_compatibility(
        runtime,
        endpoint.route_config("m"),
        ExecutionTargetConfig(
            id="other",
            kind=ExecutionTargetKind.INFERENCE_ENDPOINT,
            endpoint="http://10.0.0.3/v1",
        ),
    )
    assert result.compatible is False
    assert "does not match" in result.reason
