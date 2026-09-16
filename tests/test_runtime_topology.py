from types import SimpleNamespace

import pytest

from execraft.runtime.selection import RuntimePreferenceMode, plan_runtime_selection
from execraft.runtime.topology import RuntimeTopologyError, build_runtime_topology, resolve_simple_selection


class Config:
    def __init__(self):
        self.schema_version = 4
        self.source_schema_version = 4
        self.runtimes = (
            SimpleNamespace(id="native", kind="native", adapter="codex", binary="codex", openclaw=None),
            SimpleNamespace(
                id="openclaw", kind="openclaw", adapter="", binary="openclaw",
                openclaw=SimpleNamespace(mode="managed", gateway="ws://127.0.0.1:18789", version_policy="pinned-compatible", auth_ref="env:OC"),
            ),
        )
        self.model_routes = (
            SimpleNamespace(id="local-qwen", provider="ollama", provider_alias="ollama-local", model="qwen", api_family="openai-compatible", endpoint="http://127.0.0.1:11434", context_window=32768, default_target="local", capabilities=(), credential_ref=""),
            SimpleNamespace(id="cloud", provider="openai", provider_alias="", model="gpt", api_family="", endpoint="", context_window=None, default_target="", capabilities=(), credential_ref="env:OPENAI_API_KEY"),
        )
        self.targets = (SimpleNamespace(id="local", kind="local", endpoint="http://127.0.0.1:11434", concurrency_group="local"),)
        self.agents = (
            SimpleNamespace(id="native-impl", name="Native", enabled=True, capabilities=("implement",), priority=1, runtime_id="native", model_route_id="cloud", target_id=""),
            SimpleNamespace(id="oc-impl", name="OpenClaw", enabled=True, capabilities=("implement",), priority=2, runtime_id="openclaw", model_route_id="local-qwen", target_id="local"),
        )
    def runtime(self, item_id): return next(x for x in self.runtimes if x.id == item_id)
    def model_route(self, item_id): return next(x for x in self.model_routes if x.id == item_id)
    def target(self, item_id): return next(x for x in self.targets if x.id == item_id)


def test_topology_exposes_runtime_model_target_without_secret_values():
    topology = build_runtime_topology(Config())
    assert topology["profiles"][1]["effective_tuple"] == {
        "profile": "oc-impl", "runtime": "openclaw", "model_route": "local-qwen", "target": "local"
    }
    assert topology["runtimes"][1]["openclaw"]["authentication_configured"] is True
    assert "env:OC" not in str(topology)
    assert topology["model_routes"][1]["credential_configured"] is True
    assert "OPENAI_API_KEY" not in str(topology)


def test_simple_selection_uses_route_default_target():
    result = resolve_simple_selection(Config(), runtime_id="openclaw", model_route_id="local-qwen")
    assert result.target_id == "local"
    assert result.inferred_target is False


def test_simple_selection_refuses_ambiguous_local_targets():
    config = Config()
    config.model_routes = (SimpleNamespace(id="route", provider="ollama", model="qwen", endpoint="", default_target=""),)
    config.targets = (
        SimpleNamespace(id="a", kind="local", endpoint="", concurrency_group=""),
        SimpleNamespace(id="b", kind="local", endpoint="", concurrency_group=""),
    )
    with pytest.raises(RuntimeTopologyError, match="advanced mode"):
        resolve_simple_selection(config, runtime_id="native", model_route_id="route")


def test_runtime_preference_compiles_to_existing_profile_order():
    config = Config()
    prefer = plan_runtime_selection(config, capability="implement", mode="prefer", runtime_id="openclaw")
    assert prefer.preferred_profiles == ("oc-impl", "native-impl")
    force = plan_runtime_selection(config, capability="implement", mode="force", runtime_id="openclaw", invocation_active=True)
    assert force.preferred_profiles == ("oc-impl",)
    assert force.effective_when == "after_current_invocation"
    assert force.requires_cancel_for_immediate is True
    assert force.hot_migration_supported is False
    automatic = plan_runtime_selection(config, capability="implement", mode=RuntimePreferenceMode.AUTOMATIC)
    assert automatic.preferred_profiles == ()


def test_runtime_selection_ignores_disabled_profiles():
    config = Config()
    config.agents = config.agents + (
        SimpleNamespace(
            id="disabled-oc", name="Disabled", enabled=False,
            capabilities=("implement",), priority=999, runtime_id="openclaw",
            model_route_id="local-qwen", target_id="local",
        ),
    )
    force = plan_runtime_selection(
        config, capability="implement", mode="force", runtime_id="openclaw"
    )
    assert force.preferred_profiles == ("oc-impl",)


def test_profile_effective_target_uses_model_route_default():
    config = Config()
    config.agents = (
        SimpleNamespace(
            id="default-target", name="Default target", enabled=True,
            capabilities=("implement",), priority=1, runtime_id="openclaw",
            model_route_id="local-qwen", target_id="",
        ),
    )
    topology = build_runtime_topology(config)
    assert topology["profiles"][0]["target_id"] == "local"
    assert topology["profiles"][0]["effective_tuple"]["target"] == "local"
    force = plan_runtime_selection(
        config, capability="implement", mode="force", target_id="local"
    )
    assert force.preferred_profiles == ("default-target",)
