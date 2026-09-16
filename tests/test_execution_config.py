from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from execraft.agents.config import (
    AgentConfigError,
    parse_agent_configs,
    parse_execution_config,
    project_native_agent_configs,
)
from execraft.agents.execution_compat import project_native_legacy_configs
from execraft.orchestrate.scheduler import AgentCapability
from execraft.runtime_config import RuntimeKind
from execraft.targets import ExecutionTargetKind


ROOT = Path(__file__).resolve().parents[1]


def _v4_native_config() -> dict:
    return {
        "schema_version": 4,
        "runtimes": {
            "native-opencode": {
                "kind": "native",
                "adapter": "opencode",
                "binary": "opencode",
            }
        },
        "execution_targets": {
            "satellite-a": {
                "kind": "inference_endpoint",
                "endpoint": "http://10.0.0.2:11434",
                "concurrency_group": "gpu-a",
            }
        },
        "model_routes": {
            "qwen-a": {
                "provider": "ollama-satellite",
                "model": "qwen3-coder:30b-32k",
                "default_target": "satellite-a",
                "context_window": 32768,
                "capabilities": ["tools"],
            }
        },
        "agents": {
            "implementation-qwen": {
                "runtime": "native-opencode",
                "model_route": "qwen-a",
                "capabilities": ["implement", "review", "fix_review"],
                "aliases": ["qwen-worker"],
                "priority": 175,
                "capability_weight": 70,
                "capability_weights": {"review": 80},
                "max_complexity": 65,
                "max_complexity_by_capability": {"review": 75},
                "skills": ["ai-implement"],
                "policy": {
                    "timeout_seconds": 3600,
                    "inactivity_timeout_seconds": 600,
                    "live_sessions": False,
                    "auto_approve": True,
                    "agent_by_capability": {"review": "ai-reviewer"},
                    "format_repair_agent": "ai-contract",
                    "sandbox_enabled": True,
                    "effort_by_capability": {"review": "high"},
                },
            }
        },
    }


def _without_non_native(raw: dict) -> dict:
    """Drop non-Native runtimes and the profiles bound to them."""

    stripped = copy.deepcopy(raw)
    native_ids = {
        runtime_id
        for runtime_id, runtime in (stripped.get("runtimes") or {}).items()
        if str(runtime.get("kind", "")).strip().lower() == "native"
    }
    stripped["runtimes"] = {
        key: value
        for key, value in (stripped.get("runtimes") or {}).items()
        if key in native_ids
    }
    stripped["agents"] = {
        key: value
        for key, value in (stripped.get("agents") or {}).items()
        if str(value.get("runtime", "")).strip() in native_ids
    }
    return stripped


def _shipped_agent_configs() -> list[str]:
    """Every shipped project, plus a pinned v3 fixture.

    Projects migrate to schema v4 one at a time. Discovering them keeps this
    test honest as they do, and the fixture keeps the v1-v3 half of the
    contract covered once none of them is v3 any more.
    """

    shipped = sorted(
        str(path.relative_to(ROOT))
        for path in (ROOT / "projects").glob("*/agents.yaml")
    )
    return [*shipped, "tests/fixtures/schema_v3/agents.yaml"]


@pytest.mark.parametrize("relative", _shipped_agent_configs())
def test_shipped_projects_round_trip_without_execution_drift(relative: str) -> None:
    """Every shipped project normalizes to v4 without changing native execution.

    Projects migrate to schema v4 individually, so this asserts the invariants
    that hold for both source schemas and branches only where the schemas
    genuinely differ. The no-drift property (``projected == legacy``) is the
    one that matters: it is what makes a migration safe to apply to a running
    project.
    """

    raw = yaml.safe_load((ROOT / relative).read_text(encoding="utf-8"))
    source_version = int(raw["schema_version"])
    normalized = parse_execution_config(raw, include_disabled=True)
    native, note = project_native_agent_configs(raw)

    assert normalized.schema_version == 4
    assert normalized.source_schema_version == source_version

    if all(item.kind == RuntimeKind.NATIVE for item in normalized.runtimes):
        # An all-Native project must still satisfy the historical fail-closed
        # provider-only contract exactly.
        assert note == ""
        legacy = parse_agent_configs(raw, include_disabled=True)
        assert project_native_legacy_configs(normalized) == legacy
        assert [item.candidate_id for item in normalized.agents] == [
            item.provider_id for item in legacy
        ]
    else:
        # A mixed project cannot use the provider-only API at all, so the
        # no-drift property is that the Native profiles project exactly as they
        # would if the non-Native ones were absent.
        assert note
        native_only = _without_non_native(raw)
        assert native == parse_agent_configs(native_only, include_disabled=True)

    if source_version == 3:
        # WP1 does not guess physical placement from provider/model naming.
        assert normalized.targets == ()
        assert all(
            item.id.startswith("legacy-runtime:") for item in normalized.runtimes
        )
    else:
        # v4 names each runtime explicitly instead of collapsing them.
        assert len({item.id for item in normalized.runtimes}) == len(
            normalized.runtimes
        )


def test_v3_normalization_separates_runtime_model_and_scheduler_identity() -> None:
    raw = {
        "schema_version": 3,
        "providers": {
            "worker": {
                "adapter": "opencode",
                "enabled": True,
                "provider_id": "worker-a",
                "binary": "opencode-custom",
                "model": "ollama-node/qwen:9b",
                "capabilities": ["review"],
                "priority": 20,
                "concurrency_group": "node-a",
            }
        },
    }

    normalized = parse_execution_config(raw)
    profile = normalized.agent("worker-a")
    runtime = normalized.runtime(profile.runtime_id)
    route = normalized.model_route(profile.model_route_id)

    assert profile.id == "worker-a"
    assert profile.name == "worker"
    assert profile.concurrency_group == "node-a"
    assert runtime.adapter == "opencode"
    assert runtime.binary == "opencode-custom"
    assert route.provider == "ollama-node"
    assert route.model == "qwen:9b"
    assert route.reference_for_native_adapter("opencode") == "ollama-node/qwen:9b"


def test_schema_v4_native_profile_projects_to_existing_adapter_contract() -> None:
    raw = _v4_native_config()

    normalized = parse_execution_config(raw)
    profile = normalized.agent("implementation-qwen")
    target = normalized.target("satellite-a")
    route = normalized.model_route("qwen-a")
    projected = parse_agent_configs(raw)[0]

    assert normalized.source_schema_version == 4
    assert profile.target_id == "satellite-a"
    assert profile.concurrency_group == "gpu-a"
    assert profile.skill_set == ("ai-implement",)
    assert target.kind == ExecutionTargetKind.INFERENCE_ENDPOINT
    assert route.context_window == 32768
    assert route.capabilities == frozenset({"tools"})

    assert projected.provider_id == "implementation-qwen"
    assert projected.adapter == "opencode"
    assert projected.model == "ollama-satellite/qwen3-coder:30b-32k"
    assert projected.concurrency_group == "gpu-a"
    assert projected.timeout_seconds == 3600
    assert projected.live_sessions is False
    assert projected.agent_for_capability(AgentCapability.REVIEW) == "ai-reviewer"
    assert projected.effort_for_capability(AgentCapability.REVIEW) == "high"


def test_schema_v4_read_only_projection_removes_mutating_capabilities() -> None:
    raw = _v4_native_config()

    normalized = parse_execution_config(raw, read_only=True)
    profile = normalized.agents[0]
    projected = parse_agent_configs(raw, read_only=True)[0]

    assert profile.capabilities == frozenset({AgentCapability.REVIEW})
    assert projected.capabilities == frozenset({AgentCapability.REVIEW})
    assert dict(profile.capability_weights) == {AgentCapability.REVIEW: 80}
    assert profile.policy.agent_by_capability == ((AgentCapability.REVIEW, "ai-reviewer"),)


def test_schema_v4_openclaw_is_not_projected_through_legacy_provider_api() -> None:
    raw = {
        "schema_version": 4,
        "runtimes": {"openclaw": {"kind": "openclaw"}},
        "execution_targets": {
            "local": {"kind": "local", "endpoint": "http://127.0.0.1:11434/v1"}
        },
        "model_routes": {
            "local-qwen": {
                "provider": "ollama",
                "model": "qwen3-coder:30b-32k",
                "endpoint": "http://127.0.0.1:11434/v1",
                "default_target": "local",
            }
        },
        "agents": {
            "worker": {
                "runtime": "openclaw",
                "model_route": "local-qwen",
                "capabilities": ["implement"],
            }
        },
    }

    normalized = parse_execution_config(raw)
    assert normalized.runtime("openclaw").kind == RuntimeKind.OPENCLAW
    assert normalized.agent("worker").target_id == "local"

    with pytest.raises(AgentConfigError, match="legacy provider-only API"):
        parse_agent_configs(raw)


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (
            lambda raw: raw["agents"]["implementation-qwen"].update(
                {"runtime": "missing"}
            ),
            "unknown runtime",
        ),
        (
            lambda raw: raw["agents"]["implementation-qwen"].update(
                {"target": "missing"}
            ),
            "unknown execution target",
        ),
        (
            lambda raw: raw["model_routes"]["qwen-a"].update(
                {"default_target": "missing"}
            ),
            "unknown default target",
        ),
    ],
)
def test_schema_v4_rejects_dangling_cross_references(mutator, message: str) -> None:
    raw = _v4_native_config()
    mutator(raw)
    with pytest.raises(AgentConfigError, match=message):
        parse_execution_config(raw)


def test_schema_v4_rejects_invalid_target_and_runtime_shapes() -> None:
    raw = _v4_native_config()
    raw["execution_targets"]["satellite-a"].pop("endpoint")
    with pytest.raises(AgentConfigError, match="requires endpoint"):
        parse_execution_config(raw)

    raw = _v4_native_config()
    raw["runtimes"]["native-opencode"]["adapter"] = "unknown"
    with pytest.raises(AgentConfigError, match="supported adapter"):
        parse_execution_config(raw)


def test_schema_v4_filters_disabled_agents_only_after_full_validation() -> None:
    raw = _v4_native_config()
    raw["agents"]["implementation-qwen"]["enabled"] = False

    assert parse_execution_config(raw).agents == ()
    assert len(parse_execution_config(raw, include_disabled=True).agents) == 1

    raw["agents"]["implementation-qwen"]["policy"]["live_sessions"] = "yes"
    with pytest.raises(AgentConfigError, match="must be a boolean"):
        parse_execution_config(raw)


def test_schema_v4_rejects_unknown_fields_with_precise_owner() -> None:
    raw = _v4_native_config()
    raw["model_routes"]["qwen-a"]["defaut_target"] = "satellite-a"

    with pytest.raises(AgentConfigError, match="unsupported field for model route 'qwen-a': defaut_target"):
        parse_execution_config(raw)


def test_schema_v4_mapping_round_trips_and_preserves_custom_name() -> None:
    raw = _v4_native_config()
    raw["agents"]["implementation-qwen"]["name"] = "Implementation Qwen"

    normalized = parse_execution_config(raw, include_disabled=True)
    rendered = normalized.as_mapping()
    reparsed = parse_execution_config(rendered, include_disabled=True)

    assert "source_schema_version" not in rendered
    assert reparsed == normalized
    assert reparsed.agent("implementation-qwen").name == "Implementation Qwen"


def test_schema_v4_rejects_unknown_top_level_fields() -> None:
    raw = _v4_native_config()
    raw["providers"] = {}

    with pytest.raises(AgentConfigError, match="unsupported field for agents.yaml schema v4: providers"):
        parse_execution_config(raw)


def test_schema_v4_rejects_profiles_without_usable_capabilities() -> None:
    raw = _v4_native_config()
    raw["agents"]["implementation-qwen"]["capabilities"] = []

    with pytest.raises(AgentConfigError, match="has no usable capabilities"):
        parse_execution_config(raw)

    raw = _v4_native_config()
    raw["agents"]["implementation-qwen"]["capabilities"] = ["implement"]
    with pytest.raises(AgentConfigError, match="has no usable capabilities"):
        parse_execution_config(raw, read_only=True)


def test_schema_v4_provider_alias_preserves_native_opencode_reference() -> None:
    raw = _v4_native_config()
    raw["model_routes"]["qwen-a"]["provider"] = "ollama"
    raw["model_routes"]["qwen-a"]["provider_alias"] = "ollama-satellite"

    normalized = parse_execution_config(raw)
    route = normalized.model_route("qwen-a")
    projected = parse_agent_configs(raw)[0]

    assert route.provider == "ollama"
    assert route.provider_alias == "ollama-satellite"
    assert route.reference_for_native_adapter("opencode") == (
        "ollama-satellite/qwen3-coder:30b-32k"
    )
    assert projected.model == "ollama-satellite/qwen3-coder:30b-32k"
    assert normalized.as_mapping()["model_routes"]["qwen-a"]["provider_alias"] == (
        "ollama-satellite"
    )


def test_schema_v4_rejects_native_opencode_noncompatible_api_family() -> None:
    raw = _v4_native_config()
    raw["model_routes"]["qwen-a"]["api_family"] = "anthropic-native"

    with pytest.raises(AgentConfigError, match="OpenCode supports only openai-compatible"):
        parse_execution_config(raw)


def test_schema_v4_rejects_model_route_target_endpoint_mismatch() -> None:
    raw = _v4_native_config()
    raw["model_routes"]["qwen-a"]["endpoint"] = "http://10.0.0.99:11434"

    with pytest.raises(AgentConfigError, match="endpoint does not match"):
        parse_execution_config(raw)


def test_schema_v4_native_profile_rejects_remote_runtime_target() -> None:
    raw = _v4_native_config()
    raw["execution_targets"]["satellite-a"] = {"kind": "remote_runtime"}

    with pytest.raises(AgentConfigError, match="remote_runtime"):
        parse_execution_config(raw)
