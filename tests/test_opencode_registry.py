import json
from pathlib import Path

import pytest
import yaml

from execraft.agents.opencode_registry import (
    OpenCodeRegistryError,
    load_opencode_provider_registry,
)


def _write_registry(path: Path, *, base_url: str = "http://10.42.0.107:11434/v1") -> Path:
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "endpoints": {
                    "gpu-node-a": {
                        "provider_id": "ollama-gpu-a",
                        "name": "GPU inference node",
                        "base_url": base_url,
                        "base_url_env": "EXECRAFT_OLLAMA_GPU_A_URL",
                        "models": {
                            "qwen3.5:9b": {
                                "name": "Qwen 3.5 9B",
                                "limit": {"context": 32768, "output": 8192},
                            },
                            "small-coder": {"name": "Small coder"},
                        },
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def test_registry_renders_multiple_models_for_one_satellite(tmp_path: Path):
    registry = load_opencode_provider_registry(
        _write_registry(tmp_path / "providers.yaml"),
        environ={},
    )

    endpoint = registry.by_endpoint_id["gpu-node-a"]
    assert endpoint.provider_id == "ollama-gpu-a"
    assert endpoint.models_url == "http://10.42.0.107:11434/v1/models"
    assert registry.endpoint_for_model("ollama-gpu-a/qwen3.5:9b") == endpoint

    provider = registry.as_opencode_providers()["ollama-gpu-a"]
    assert provider["options"]["baseURL"] == "http://10.42.0.107:11434/v1"
    assert set(provider["models"]) == {"qwen3.5:9b", "small-coder"}
    json.dumps(provider)


def test_environment_url_overrides_committed_satellite_address(tmp_path: Path):
    registry = load_opencode_provider_registry(
        _write_registry(tmp_path / "providers.yaml"),
        environ={"EXECRAFT_OLLAMA_GPU_A_URL": "http://192.0.2.18:11434/v1/"},
    )

    endpoint = registry.by_provider_id["ollama-gpu-a"]
    assert endpoint.base_url == "http://192.0.2.18:11434/v1"
    assert endpoint.base_url_source == "environment:EXECRAFT_OLLAMA_GPU_A_URL"


def test_registry_rejects_credentials_in_endpoint_url(tmp_path: Path):
    path = _write_registry(
        tmp_path / "providers.yaml",
        base_url="http://user:secret@10.42.0.107:11434/v1",
    )

    with pytest.raises(OpenCodeRegistryError, match="must not embed credentials"):
        load_opencode_provider_registry(
            path,
            environ={
                "EXECRAFT_OLLAMA_GPU_A_URL": "http://192.0.2.10:11434/v1"
            },
        )


def test_registry_rejects_credentials_in_environment_override(tmp_path: Path):
    path = _write_registry(tmp_path / "providers.yaml")

    with pytest.raises(
        OpenCodeRegistryError,
        match="environment variable EXECRAFT_OLLAMA_GPU_A_URL.*credentials",
    ):
        load_opencode_provider_registry(
            path,
            environ={
                "EXECRAFT_OLLAMA_GPU_A_URL": (
                    "http://user:secret@192.0.2.10:11434/v1"
                )
            },
        )


def test_registry_reuses_model_set_across_multiple_satellites(tmp_path: Path):
    path = tmp_path / "providers.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "model_sets": {
                    "qwen-workers": {
                        "qwen3.5:9b": {"name": "Qwen reviewer"},
                        "qwen3-coder:30b-16k": {"name": "Qwen coder"},
                    }
                },
                "endpoints": {
                    "node-a": {
                        "provider_id": "ollama-a",
                        "base_url": "http://192.0.2.10:11434/v1",
                        "model_set": "qwen-workers",
                    },
                    "node-b": {
                        "provider_id": "ollama-b",
                        "base_url": "http://192.0.2.11:11434/v1",
                        "model_set": "qwen-workers",
                        "models": {
                            "node-b-only": {"name": "Node B extra model"}
                        },
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    registry = load_opencode_provider_registry(path, environ={})

    assert set(registry.by_provider_id) == {"ollama-a", "ollama-b"}
    assert set(registry.by_provider_id["ollama-a"].models) == {
        "qwen3.5:9b",
        "qwen3-coder:30b-16k",
    }
    assert set(registry.by_provider_id["ollama-b"].models) == {
        "qwen3.5:9b",
        "qwen3-coder:30b-16k",
        "node-b-only",
    }


def test_registry_rejects_unknown_model_set(tmp_path: Path):
    path = tmp_path / "providers.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "endpoints": {
                    "node-a": {
                        "provider_id": "ollama-a",
                        "base_url": "http://192.0.2.10:11434/v1",
                        "model_set": "missing",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(OpenCodeRegistryError, match="unknown model_set"):
        load_opencode_provider_registry(path, environ={})


def test_disabled_endpoint_still_validates_model_set_structure(tmp_path: Path):
    path = tmp_path / "providers.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "endpoints": {
                    "disabled-node": {
                        "enabled": False,
                        "provider_id": "ollama-disabled",
                        "model_set": "missing",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(OpenCodeRegistryError, match="unknown model_set"):
        load_opencode_provider_registry(path, environ={})


def test_registry_rejects_duplicate_opencode_provider_ids(tmp_path: Path):
    path = tmp_path / "providers.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "endpoints": {
                    "node-a": {
                        "provider_id": "ollama-shared",
                        "base_url": "http://10.0.0.2:11434/v1",
                        "models": {"model-a": {}},
                    },
                    "node-b": {
                        "provider_id": "ollama-shared",
                        "base_url": "http://10.0.0.3:11434/v1",
                        "models": {"model-b": {}},
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(OpenCodeRegistryError, match="duplicate OpenCode provider_id"):
        load_opencode_provider_registry(path)


def test_missing_registry_is_backwards_compatible(tmp_path: Path):
    registry = load_opencode_provider_registry(tmp_path / "missing.yaml")
    assert registry.endpoints == ()
    assert registry.as_opencode_providers() == {}
