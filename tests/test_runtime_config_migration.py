from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from execraft.agents.config import parse_execution_config
from execraft.model_registry import load_model_route_registry
from execraft.runtime.config_migration import (
    RuntimeConfigMigrationError,
    apply_agents_v4_migration,
    normalize_execution_for_operator,
    preview_agents_v4_migration,
)


def _legacy_agents(path: Path) -> Path:
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 3,
                "scheduling": {"rotate_agents": True, "automatic_recovery": True},
                "supervisor": {"enabled": True, "agent": "worker-a"},
                "commit": {"mode": "automatic"},
                "providers": {
                    "worker": {
                        "adapter": "opencode",
                        "enabled": True,
                        "provider_id": "worker-a",
                        "model": "ollama-gpu/qwen:latest",
                        "capabilities": ["implement", "review"],
                        "priority": 100,
                        "concurrency_group": "gpu",
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def _providers(path: Path) -> Path:
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "endpoints": {
                    "gpu": {
                        "provider_id": "ollama-gpu",
                        "base_url": "http://10.0.0.2:11434/v1",
                        "models": {"qwen:latest": {"limit": {"context": 32768}}},
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def test_v3_preview_preserves_orchestration_and_restores_target(tmp_path: Path) -> None:
    agents = _legacy_agents(tmp_path / "agents.yaml")
    registry = load_model_route_registry(_providers(tmp_path / "providers.yaml"), environ={})

    preview = preview_agents_v4_migration(agents, model_registry=registry)
    rendered = yaml.safe_load(preview.rendered_yaml)

    assert rendered["schema_version"] == 4
    assert rendered["scheduling"] == {"rotate_agents": True, "automatic_recovery": True}
    assert rendered["supervisor"] == {"enabled": True, "agent": "worker-a"}
    assert rendered["commit"] == {"mode": "automatic"}
    assert rendered["execution_targets"]["gpu"]["kind"] == "inference_endpoint"
    profile = rendered["agents"]["worker-a"]
    assert profile["runtime"] == "native-opencode"
    assert profile["target"] == "gpu"
    route = rendered["model_routes"][profile["model_route"]]
    assert route["provider"] == "ollama"
    assert route["provider_alias"] == "ollama-gpu"
    assert route["default_target"] == "gpu"
    assert parse_execution_config(rendered, include_disabled=True).source_schema_version == 4


def test_apply_is_preview_bound_and_creates_backup(tmp_path: Path) -> None:
    agents = _legacy_agents(tmp_path / "agents.yaml")
    preview = preview_agents_v4_migration(agents)
    original = agents.read_bytes()

    backup = apply_agents_v4_migration(agents, preview)

    assert backup.read_bytes() == original
    assert yaml.safe_load(agents.read_text(encoding="utf-8"))["schema_version"] == 4


def test_apply_rejects_configuration_changed_after_preview(tmp_path: Path) -> None:
    agents = _legacy_agents(tmp_path / "agents.yaml")
    preview = preview_agents_v4_migration(agents)
    agents.write_text(agents.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")

    with pytest.raises(RuntimeConfigMigrationError, match="changed after preview"):
        apply_agents_v4_migration(agents, preview)


def test_existing_v4_preview_preserves_colocated_orchestration_policy(tmp_path: Path) -> None:
    agents = _legacy_agents(tmp_path / "agents.yaml")
    first = preview_agents_v4_migration(agents)
    apply_agents_v4_migration(agents, first)

    second = preview_agents_v4_migration(agents)
    rendered = yaml.safe_load(second.rendered_yaml)

    assert rendered["scheduling"] == {"rotate_agents": True, "automatic_recovery": True}
    assert rendered["supervisor"] == {"enabled": True, "agent": "worker-a"}
    assert rendered["commit"] == {"mode": "automatic"}


def test_v3_operator_inventory_is_enriched_without_rewriting_file(tmp_path: Path) -> None:
    agents = _legacy_agents(tmp_path / "agents.yaml")
    registry = load_model_route_registry(_providers(tmp_path / "providers.yaml"), environ={})
    before = agents.read_bytes()
    raw = yaml.safe_load(before.decode("utf-8"))

    execution, warnings = normalize_execution_for_operator(raw, model_registry=registry)

    assert agents.read_bytes() == before
    profile = execution.agent("worker-a")
    assert profile.runtime_id == "native-opencode"
    assert profile.target_id == "gpu"
    assert execution.target("gpu").kind.value == "inference_endpoint"
    assert warnings == ()
