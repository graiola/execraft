import json
from pathlib import Path

import pytest

from execraft.agents.config import AgentConfigError, parse_agent_configs
from execraft.agents.diagnostics import EndpointProbeResult
from execraft.agents.opencode_registry import OpenCodeEndpoint, OpenCodeProviderRegistry
from execraft.cli import _register_project_agents
from execraft.orchestrate import AgentCapability, OrchestrationConfig, ProjectOrchestrator


def _dual_opencode_config():
    return {
        "schema_version": 2,
        "providers": {
            "go": {
                "adapter": "opencode",
                "enabled": True,
                "provider_id": "opencode-go",
                "binary": "python3",
                "model": "opencode-go/deepseek-v4-flash",
                "capabilities": ["implement", "review", "fix_review"],
                "priority": 100,
            },
            "zen": {
                "adapter": "opencode",
                "enabled": True,
                "provider_id": "opencode-zen-free",
                "binary": "python3",
                "model": "opencode/deepseek-v4-flash-free",
                "aliases": ["opencode"],
                "capabilities": ["review"],
                "agent_by_capability": {"review": "ai-reviewer"},
                "priority": 200,
            },
        },
    }


def test_multiple_instances_share_adapter_but_keep_independent_ids_and_models():
    configs = parse_agent_configs(_dual_opencode_config())

    assert [item.provider_id for item in configs] == [
        "opencode-zen-free",
        "opencode-go",
    ]
    assert [item.adapter for item in configs] == ["opencode", "opencode"]
    assert configs[0].model == "opencode/deepseek-v4-flash-free"
    assert configs[1].model == "opencode-go/deepseek-v4-flash"


def test_opencode_agent_by_capability_is_validated_and_preserved():
    configs = parse_agent_configs(_dual_opencode_config())
    zen = next(item for item in configs if item.provider_id == "opencode-zen-free")

    assert zen.agent_for_capability(AgentCapability.REVIEW) == "ai-reviewer"
    assert zen.agent_for_capability(AgentCapability.IMPLEMENT) == ""


def test_opencode_format_repair_agent_is_validated_and_preserved():
    config = _dual_opencode_config()
    config["providers"]["go"]["format_repair_agent"] = "ai-contract"

    go = next(
        item
        for item in parse_agent_configs(config)
        if item.provider_id == "opencode-go"
    )

    assert go.format_repair_agent == "ai-contract"

    config["providers"]["go"]["format_repair_agent"] = "bad agent"
    with pytest.raises(AgentConfigError, match="invalid format repair agent"):
        parse_agent_configs(config)

    config = _dual_opencode_config()
    config["providers"]["codex"] = {
        "adapter": "codex",
        "enabled": True,
        "provider_id": "codex",
        "capabilities": ["review"],
        "format_repair_agent": "ai-contract",
    }
    with pytest.raises(AgentConfigError, match="supported only by OpenCode"):
        parse_agent_configs(config)


def test_agent_by_capability_rejects_invalid_or_inapplicable_entries():
    config = _dual_opencode_config()
    config["providers"]["zen"]["agent_by_capability"] = {"implement": "build"}
    with pytest.raises(AgentConfigError, match="not declared"):
        parse_agent_configs(config)

    config = _dual_opencode_config()
    config["providers"]["zen"]["agent_by_capability"] = {"review": "bad agent"}
    with pytest.raises(AgentConfigError, match="invalid provider-native agent name"):
        parse_agent_configs(config)

    config = _dual_opencode_config()
    config["providers"]["codex"] = {
        "adapter": "codex",
        "enabled": True,
        "provider_id": "codex",
        "capabilities": ["review"],
        "agent_by_capability": {"review": "reviewer"},
    }
    with pytest.raises(AgentConfigError, match="supported only by OpenCode"):
        parse_agent_configs(config)


def test_read_only_drops_agent_mapping_for_removed_capability():
    config = _dual_opencode_config()
    config["providers"]["go"]["agent_by_capability"] = {
        "implement": "build",
        "review": "ai-reviewer",
    }

    go = next(
        item
        for item in parse_agent_configs(config, read_only=True)
        if item.provider_id == "opencode-go"
    )

    assert go.agent_for_capability(AgentCapability.IMPLEMENT) == ""
    assert go.agent_for_capability(AgentCapability.REVIEW) == "ai-reviewer"


def test_opencode_model_is_required_and_must_be_provider_qualified():
    config = _dual_opencode_config()
    del config["providers"]["zen"]["model"]
    with pytest.raises(AgentConfigError, match="must declare an explicit model"):
        parse_agent_configs(config)

    config = _dual_opencode_config()
    config["providers"]["zen"]["model"] = "deepseek-v4-flash-free"
    with pytest.raises(AgentConfigError, match="provider/model"):
        parse_agent_configs(config)


def test_duplicate_logical_provider_id_is_rejected():
    config = _dual_opencode_config()
    config["providers"]["go"]["provider_id"] = "opencode-zen-free"
    with pytest.raises(AgentConfigError, match="duplicate agent provider_id"):
        parse_agent_configs(config)


def test_read_only_removes_mutating_capabilities_but_keeps_review():
    config = _dual_opencode_config()
    config["providers"]["go"]["capabilities"].append("supervise")

    configs = parse_agent_configs(config, read_only=True)
    go = next(item for item in configs if item.provider_id == "opencode-go")
    assert go.capabilities == frozenset({AgentCapability.REVIEW})


def test_cli_registration_creates_two_independent_opencode_adapters(tmp_path: Path):
    orchestrator = ProjectOrchestrator(
        "dual-opencode",
        config=OrchestrationConfig(state_dir=tmp_path / "state"),
    )

    registered = _register_project_agents(
        orchestrator,
        _dual_opencode_config(),
        workspace_root=tmp_path,
        policy_profile="workspace-write",
    )

    assert registered == ["opencode-zen-free:available", "opencode-go:available"]
    status = orchestrator.agent_status()
    assert [item["provider_id"] for item in status] == [
        "opencode-zen-free",
        "opencode-go",
    ]
    assert status[0]["model"] == "opencode/deepseek-v4-flash-free"
    assert status[0]["aliases"] == ["opencode"]
    assert status[0]["provider_health"]["status"] == "available"
    assert orchestrator._find_adapter("opencode-zen-free")._config_path == (
        tmp_path / "opencode.json"
    )
    assert orchestrator._find_adapter("opencode-zen-free")._agent_for_stage("final_review") == "ai-reviewer"
    assert status[1]["model"] == "opencode-go/deepseek-v4-flash"
    assert status[1]["provider_health"]["status"] == "available"


def test_runtime_endpoint_preflight_skips_disconnected_satellite(tmp_path: Path):
    config = {
        "schema_version": 2,
        "providers": {
            "satellite": {
                "adapter": "opencode",
                "enabled": True,
                "provider_id": "opencode-satellite",
                "binary": "python3",
                "model": "ollama-satellite/qwen:latest",
                "capabilities": ["implement"],
                "priority": 100,
            },
            "local": {
                "adapter": "opencode",
                "enabled": True,
                "provider_id": "opencode-local",
                "binary": "python3",
                "model": "ollama-local/qwen:latest",
                "capabilities": ["implement"],
                "priority": 100,
            },
        },
    }

    def endpoint(endpoint_id: str, provider_id: str) -> OpenCodeEndpoint:
        return OpenCodeEndpoint(
            endpoint_id=endpoint_id,
            provider_id=provider_id,
            name=endpoint_id,
            base_url=f"http://{endpoint_id}.invalid/v1",
            base_url_source="test",
            npm="@ai-sdk/openai-compatible",
            models={"qwen:latest": {}},
            options={},
        )

    registry = OpenCodeProviderRegistry(
        (
            endpoint("satellite", "ollama-satellite"),
            endpoint("local", "ollama-local"),
        )
    )
    orchestrator = ProjectOrchestrator(
        "endpoint-preflight",
        config=OrchestrationConfig(state_dir=tmp_path / "state", rotate_agents=False),
    )

    registered = _register_project_agents(
        orchestrator,
        config,
        workspace_root=tmp_path,
        policy_profile="workspace-write",
        opencode_registry=registry,
        endpoint_probe=lambda item: (
            EndpointProbeResult(error="host unreachable")
            if item.endpoint_id == "satellite"
            else EndpointProbeResult(models=frozenset({"qwen:latest"}))
        ),
    )

    assert registered == [
        "opencode-satellite:network_transient",
        "opencode-local:available",
    ]
    assert (
        orchestrator._select_agent_for_capability(AgentCapability.IMPLEMENT)
        == "opencode-local"
    )


def test_registration_resyncs_stale_workspace_config_and_clears_model_block(
    tmp_path: Path,
):
    """A provider added after the render must not stay permanently blocked.

    ``opencode.json`` is generated once when the workspace shell is rendered.
    An endpoint declared later is unknown to the OpenCode CLI even though the
    endpoint probe and ``agents doctor`` see it, so every invocation failed with
    ``Model not found`` and left an indefinite ``invalid_model`` block behind.
    """

    config = {
        "schema_version": 2,
        "providers": {
            "local": {
                "adapter": "opencode",
                "enabled": True,
                "provider_id": "opencode-ollama-local-coder",
                "binary": "python3",
                "model": "ollama-local/qwen3-coder:30b-32k",
                "capabilities": ["implement"],
                "priority": 100,
            },
        },
    }
    registry = OpenCodeProviderRegistry(
        (
            OpenCodeEndpoint(
                endpoint_id="local-ollama",
                provider_id="ollama-local",
                name="Ollama on local development GPU",
                base_url="http://127.0.0.1:11434/v1",
                base_url_source="test",
                npm="@ai-sdk/openai-compatible",
                models={"qwen3-coder:30b-32k": {}},
                options={},
            ),
        )
    )
    # A workspace rendered before the endpoint existed.
    (tmp_path / "opencode.json").write_text(
        json.dumps({"permission": {"*": "deny"}, "provider": {}}), encoding="utf-8"
    )
    orchestrator = ProjectOrchestrator(
        "stale-workspace-config",
        config=OrchestrationConfig(state_dir=tmp_path / "state"),
    )
    orchestrator._provider_health.mark_failure(
        "opencode-ollama-local-coder",
        reason="invalid_model",
        detail="configured model is unavailable",
        persistent=True,
    )

    _register_project_agents(
        orchestrator,
        config,
        workspace_root=tmp_path,
        policy_profile="workspace-write",
        opencode_registry=registry,
        endpoint_probe=lambda item: EndpointProbeResult(
            models=frozenset({"qwen3-coder:30b-32k"})
        ),
    )

    workspace_config = json.loads((tmp_path / "opencode.json").read_text())
    assert (
        workspace_config["provider"]["ollama-local"]["models"]["qwen3-coder:30b-32k"]
        == {}
    )
    assert workspace_config["permission"] == {"*": "deny"}
    assert orchestrator._provider_health.get(
        "opencode-ollama-local-coder"
    ).is_available


def test_legacy_opencode_alias_resolves_to_zen_free_adapter(tmp_path: Path):
    orchestrator = ProjectOrchestrator(
        "legacy-alias",
        config=OrchestrationConfig(state_dir=tmp_path / "state"),
    )
    _register_project_agents(
        orchestrator,
        _dual_opencode_config(),
        workspace_root=tmp_path,
        policy_profile="workspace-write",
    )

    assert orchestrator._find_adapter("opencode").provider_id == "opencode-zen-free"


def test_agent_runtime_watchdog_settings_are_parsed():
    configs = parse_agent_configs(
        {
            "schema_version": 2,
            "providers": {
                "go": {
                    "adapter": "opencode",
                    "enabled": True,
                    "provider_id": "opencode-go",
                    "model": "opencode-go/deepseek-v4-flash",
                    "capabilities": ["review"],
                    "timeout_seconds": 3600,
                    "inactivity_timeout_seconds": 600,
                    "output_silence_timeout_seconds": 450,
                    "first_output_timeout_seconds": 120,
                    "max_output_bytes": 16777216,
                    "max_internal_retry_delay_seconds": 90,
                }
            },
        }
    )
    assert configs[0].inactivity_timeout_seconds == 600
    assert configs[0].output_silence_timeout_seconds == 450
    assert configs[0].first_output_timeout_seconds == 120
    assert configs[0].max_output_bytes == 16777216
    assert configs[0].max_internal_retry_delay_seconds == 90


def test_negative_watchdog_settings_are_rejected():
    with pytest.raises(AgentConfigError, match="inactivity_timeout_seconds"):
        parse_agent_configs(
            {
                "schema_version": 2,
                "providers": {
                    "go": {
                        "adapter": "opencode",
                        "enabled": True,
                        "provider_id": "opencode-go",
                        "model": "opencode-go/deepseek-v4-flash",
                        "capabilities": ["review"],
                        "inactivity_timeout_seconds": -1,
                    }
                },
            }
        )

    with pytest.raises(AgentConfigError, match="output_silence_timeout_seconds"):
        parse_agent_configs(
            {
                "schema_version": 2,
                "providers": {
                    "go": {
                        "adapter": "opencode",
                        "enabled": True,
                        "provider_id": "opencode-go",
                        "model": "opencode-go/deepseek-v4-flash",
                        "capabilities": ["review"],
                        "output_silence_timeout_seconds": -1,
                    }
                },
            }
        )

    with pytest.raises(AgentConfigError, match="max_output_bytes"):
        parse_agent_configs(
            {
                "schema_version": 3,
                "providers": {
                    "go": {
                        "adapter": "opencode",
                        "enabled": True,
                        "provider_id": "opencode-go",
                        "model": "opencode-go/deepseek-v4-flash",
                        "capabilities": ["review"],
                        "max_output_bytes": 100,
                    }
                },
            }
        )

    with pytest.raises(AgentConfigError, match="first_output_timeout_seconds"):
        parse_agent_configs(
            {
                "schema_version": 3,
                "providers": {
                    "go": {
                        "adapter": "opencode",
                        "enabled": True,
                        "provider_id": "opencode-go",
                        "model": "opencode-go/deepseek-v4-flash",
                        "capabilities": ["review"],
                        "first_output_timeout_seconds": -1,
                    }
                },
            }
        )


def test_first_output_timeout_cannot_exceed_absolute_timeout():
    with pytest.raises(AgentConfigError, match="first_output_timeout_seconds"):
        parse_agent_configs(
            {
                "schema_version": 3,
                "providers": {
                    "go": {
                        "adapter": "opencode",
                        "enabled": True,
                        "provider_id": "opencode-go",
                        "model": "opencode-go/deepseek-v4-flash",
                        "capabilities": ["review"],
                        "timeout_seconds": 60,
                        "first_output_timeout_seconds": 61,
                    }
                },
            }
        )


def test_disabled_providers_are_available_to_administrative_callers_only():
    config = _dual_opencode_config()
    config["providers"]["go"]["enabled"] = False

    runtime = parse_agent_configs(config)
    administrative = parse_agent_configs(config, include_disabled=True)

    assert [item.provider_id for item in runtime] == ["opencode-zen-free"]
    assert [item.provider_id for item in administrative] == [
        "opencode-zen-free",
        "opencode-go",
    ]
    assert administrative[1].enabled is False


def _antigravity_config():
    return {
        "schema_version": 2,
        "providers": {
            "antigravity": {
                "adapter": "antigravity-cli",
                "enabled": True,
                "provider_id": "antigravity",
                "binary": "python3",
                "model": "gemini-3-pro",
                "capabilities": ["implement", "review", "fix_review"],
                "capability_weight": 90,
                "capability_weights": {"review": 94},
                "dangerously_skip_permissions": True,
                "sandbox_enabled": True,
                "policy_paths": ["/tmp/base.policy", "/tmp/project.policy"],
            }
        },
    }


def test_antigravity_config_and_capability_weights_are_preserved():
    config = parse_agent_configs(_antigravity_config())[0]

    assert config.adapter == "antigravity-cli"
    assert config.binary == "python3"
    assert config.model == "gemini-3-pro"
    assert config.weight_for_capability(AgentCapability.IMPLEMENT) == 90
    assert config.weight_for_capability(AgentCapability.REVIEW) == 94
    assert config.dangerously_skip_permissions is True
    assert config.sandbox_enabled is True
    assert config.policy_paths == ("/tmp/base.policy", "/tmp/project.policy")


def test_invalid_weight_and_antigravity_policy_paths_are_rejected():
    config = _antigravity_config()
    config["providers"]["antigravity"]["capability_weight"] = 101
    with pytest.raises(AgentConfigError, match="between 1 and 100"):
        parse_agent_configs(config)

    config = _antigravity_config()
    config["providers"]["antigravity"]["policy_paths"] = "not-a-list"
    with pytest.raises(AgentConfigError, match="policy_paths"):
        parse_agent_configs(config)

    config = _antigravity_config()
    config["providers"]["antigravity"]["policy_paths"] = [""]
    with pytest.raises(AgentConfigError, match="policy_paths"):
        parse_agent_configs(config)


def test_antigravity_cli_registration_exposes_weights(tmp_path: Path):
    orchestrator = ProjectOrchestrator(
        "antigravity",
        config=OrchestrationConfig(state_dir=tmp_path / "state"),
    )

    registered = _register_project_agents(
        orchestrator,
        _antigravity_config(),
        workspace_root=tmp_path,
        policy_profile="workspace-write",
    )

    assert registered == ["antigravity:available"]
    status = orchestrator.agent_status()[0]
    assert status["adapter"] == "antigravity-cli"
    assert status["capability_weight"] == 90
    assert status["capability_weights"]["implement"] == 90
    assert status["capability_weights"]["review"] == 94


def test_max_complexity_policy_is_validated_and_preserved():
    config = _dual_opencode_config()
    config["providers"]["go"]["max_complexity"] = 70
    config["providers"]["go"]["max_complexity_by_capability"] = {
        "implement": 45,
        "review": 80,
    }

    go = next(
        item
        for item in parse_agent_configs(config)
        if item.provider_id == "opencode-go"
    )

    assert go.max_complexity_for(AgentCapability.IMPLEMENT) == 45
    assert go.max_complexity_for(AgentCapability.REVIEW) == 80
    assert go.max_complexity_for(AgentCapability.FIX_REVIEW) == 70


def test_invalid_max_complexity_policy_is_rejected():
    config = _dual_opencode_config()
    config["providers"]["go"]["max_complexity"] = 101
    with pytest.raises(AgentConfigError, match="max_complexity"):
        parse_agent_configs(config)

    config = _dual_opencode_config()
    config["providers"]["go"]["max_complexity_by_capability"] = {
        "implement": -1,
    }
    with pytest.raises(AgentConfigError, match="between 0 and 100"):
        parse_agent_configs(config)

    config = _dual_opencode_config()
    config["providers"]["zen"]["max_complexity_by_capability"] = {
        "implement": 20,
    }
    with pytest.raises(AgentConfigError, match="not declared"):
        parse_agent_configs(config)


def test_concurrency_group_defaults_to_provider_and_is_registered(tmp_path: Path):
    config = _dual_opencode_config()
    config["providers"]["zen"]["concurrency_group"] = "shared-satellite"
    parsed = parse_agent_configs(config)
    zen = next(item for item in parsed if item.provider_id == "opencode-zen-free")
    go = next(item for item in parsed if item.provider_id == "opencode-go")
    assert zen.concurrency_group == "shared-satellite"
    assert go.concurrency_group == "opencode-go"

    orchestrator = ProjectOrchestrator(
        "groups", config=OrchestrationConfig(state_dir=tmp_path / "state")
    )
    _register_project_agents(
        orchestrator,
        config,
        workspace_root=tmp_path,
        policy_profile="workspace-write",
    )
    status = {item["provider_id"]: item for item in orchestrator.agent_status()}
    assert status["opencode-zen-free"]["concurrency_group"] == "shared-satellite"
    assert status["opencode-go"]["concurrency_group"] == "opencode-go"


def test_agent_profiles_reduce_duplicate_provider_configuration():
    configs = parse_agent_configs(
        {
            "schema_version": 3,
            "profiles": {
                "reviewer": {
                    "adapter": "opencode",
                    "enabled": True,
                    "binary": "opencode",
                    "capabilities": ["review"],
                    "timeout_seconds": 600,
                    "capability_weights": {"review": 70},
                }
            },
            "providers": {
                "first": {
                    "extends": "reviewer",
                    "provider_id": "first",
                    "model": "ollama-a/qwen:9b",
                    "concurrency_group": "node-a",
                },
                "second": {
                    "extends": "reviewer",
                    "provider_id": "second",
                    "model": "ollama-b/qwen:9b",
                    "concurrency_group": "node-b",
                },
            },
        }
    )

    assert [item.provider_id for item in configs] == ["first", "second"]
    assert configs[0].timeout_seconds == 600
    assert configs[0].weight_for_capability(AgentCapability.REVIEW) == 70
    assert configs[0].concurrency_group == "node-a"
    assert configs[1].concurrency_group == "node-b"


def test_agent_profile_inheritance_cycles_and_unknown_profiles_are_rejected():
    with pytest.raises(AgentConfigError, match="inheritance cycle"):
        parse_agent_configs(
            {
                "schema_version": 3,
                "profiles": {
                    "a": {"extends": "b"},
                    "b": {"extends": "a"},
                },
                "providers": {"worker": {"extends": "a"}},
            }
        )

    with pytest.raises(AgentConfigError, match="unknown agent profile"):
        parse_agent_configs(
            {
                "schema_version": 3,
                "providers": {"worker": {"extends": "missing"}},
            }
        )


def test_disabled_providers_are_still_structurally_validated():
    with pytest.raises(AgentConfigError, match="must declare an explicit model"):
        parse_agent_configs(
            {
                "schema_version": 3,
                "providers": {
                    "disabled": {
                        "adapter": "opencode",
                        "enabled": False,
                        "provider_id": "disabled",
                        "capabilities": ["review"],
                    }
                },
            }
        )


def test_all_profiles_are_validated_even_when_unused():
    with pytest.raises(AgentConfigError, match="unknown agent profile"):
        parse_agent_configs(
            {
                "schema_version": 3,
                "profiles": {
                    "unused": {"extends": "missing"},
                },
                "providers": {},
            }
        )


def test_profiles_require_schema_three_and_string_extends():
    with pytest.raises(AgentConfigError, match="require schema_version 3"):
        parse_agent_configs(
            {
                "schema_version": 2,
                "profiles": {"reviewer": {"capabilities": ["review"]}},
                "providers": {},
            }
        )

    with pytest.raises(AgentConfigError, match="extends must be a string"):
        parse_agent_configs(
            {
                "schema_version": 3,
                "profiles": {"reviewer": {"extends": ["base"]}},
                "providers": {},
            }
        )


def test_provider_boolean_fields_are_strict():
    raw = {
        "schema_version": 3,
        "providers": {
            "reviewer": {
                "adapter": "opencode",
                "enabled": "false",
                "provider_id": "reviewer",
                "model": "ollama/qwen:9b",
                "capabilities": ["review"],
            }
        },
    }
    with pytest.raises(AgentConfigError, match="enabled must be a boolean"):
        parse_agent_configs(raw)


def test_unused_profiles_reject_invalid_primitive_fields():
    with pytest.raises(AgentConfigError, match="enabled must be a boolean"):
        parse_agent_configs(
            {
                "schema_version": 3,
                "profiles": {
                    "unused": {
                        "enabled": "false",
                        "timeout_seconds": 600,
                    }
                },
                "providers": {},
            }
        )

    with pytest.raises(AgentConfigError, match="timeout_seconds"):
        parse_agent_configs(
            {
                "schema_version": 3,
                "profiles": {
                    "unused": {
                        "enabled": False,
                        "timeout_seconds": 0,
                    }
                },
                "providers": {},
            }
        )


def test_unused_profiles_reject_invalid_capability_mappings():
    with pytest.raises(AgentConfigError, match="capability_weights"):
        parse_agent_configs(
            {
                "schema_version": 3,
                "profiles": {
                    "unused": {
                        "capabilities": ["review"],
                        "capability_weights": {"review": 101},
                    }
                },
                "providers": {},
            }
        )

    with pytest.raises(AgentConfigError, match="agent_by_capability"):
        parse_agent_configs(
            {
                "schema_version": 3,
                "profiles": {
                    "unused": {
                        "capabilities": ["review"],
                        "agent_by_capability": {"review": "not a valid agent"},
                    }
                },
                "providers": {},
            }
        )


def test_codex_and_claude_live_sessions_default_true_and_can_be_disabled():
    configs = parse_agent_configs(
        {
            "schema_version": 2,
            "providers": {
                "codex": {
                    "adapter": "codex",
                    "provider_id": "codex",
                    "enabled": True,
                    "capabilities": ["implement"],
                },
                "claude": {
                    "adapter": "claude-code",
                    "provider_id": "claude",
                    "enabled": True,
                    "capabilities": ["review"],
                    "live_sessions": False,
                },
            },
        }
    )

    by_id = {config.provider_id: config for config in configs}
    assert by_id["codex"].live_sessions is True
    assert by_id["claude"].live_sessions is False


def test_live_sessions_must_be_boolean():
    with pytest.raises(AgentConfigError, match="live_sessions must be a boolean"):
        parse_agent_configs(
            {
                "schema_version": 2,
                "providers": {
                    "codex": {
                        "adapter": "codex",
                        "provider_id": "codex",
                        "enabled": True,
                        "capabilities": ["implement"],
                        "live_sessions": "yes",
                    }
                },
            }
        )
