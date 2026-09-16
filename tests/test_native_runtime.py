from __future__ import annotations

from pathlib import Path

import pytest

from execraft.agents.config import AgentProviderConfig
from execraft.runtime.native import build_native_runtime
from execraft.orchestrate.scheduler import (
    AgentCapability,
    Availability,
    StructuredHandoff,
    agent_adapter_capabilities,
    agent_capability_weight,
    agent_max_complexity,
)
from execraft.runtime import NativeAgentRuntime, RuntimeCapabilities, RuntimeSessionRef


class _Delegate:
    provider_id = "candidate-a"
    capabilities = {AgentCapability.IMPLEMENT, AgentCapability.REVIEW}
    availability = Availability.AVAILABLE
    adapter_name = "fake-native"
    model = "model-a"
    transport = "fake-transport"

    def __init__(self) -> None:
        self.received = None
        self.heartbeat = None

    @property
    def execution_capabilities(self):
        from execraft.orchestrate.scheduler import AgentAdapterCapabilities

        return AgentAdapterCapabilities(
            read_only_enforcement="hard",
            structured_output_enforcement="native_schema",
            streaming=True,
            semantic_streaming=True,
            provider_native_steering=True,
            session_resume=True,
        )

    def execute(self, handoff):
        self.received = handoff
        return {"ok": True, "session_id": "session-123", "value": 7}

    def configure_heartbeat(self, callback, *, interval_seconds=30.0):
        self.heartbeat = (callback, interval_seconds)


def _provider(adapter: str, **overrides) -> AgentProviderConfig:
    values = {
        "name": f"{adapter}-profile",
        "adapter": adapter,
        "enabled": True,
        "provider_id": f"{adapter}-candidate",
        "binary": adapter,
        "capabilities": frozenset({AgentCapability.IMPLEMENT, AgentCapability.REVIEW}),
        "model": "provider/model" if adapter == "opencode" else "model-x",
        "live_sessions": True,
        "capability_weight": 61,
        "capability_weights": ((AgentCapability.REVIEW, 77),),
        "max_complexity": 82,
        "max_complexity_by_capability": ((AgentCapability.REVIEW, 65),),
        "concurrency_group": "native-group",
    }
    values.update(overrides)
    return AgentProviderConfig(**values)


def test_native_runtime_delegates_execution_without_mutating_result():
    delegate = _Delegate()
    runtime = NativeAgentRuntime(delegate)
    handoff = StructuredHandoff(work_package_id="WP1", stage="implementation", summary="x")

    result = runtime.execute(handoff)

    assert result == {"ok": True, "session_id": "session-123", "value": 7}
    assert delegate.received is handoff
    assert runtime.runtime_id == "native"
    assert runtime.candidate_id == "candidate-a"
    assert runtime.provider_id == "candidate-a"
    assert runtime.native_adapter is delegate


def test_native_runtime_normalizes_execution_capabilities_and_legacy_projection():
    runtime = NativeAgentRuntime(_Delegate())

    capabilities = runtime.execution_capabilities

    assert isinstance(capabilities, RuntimeCapabilities)
    assert capabilities.read_only_enforcement == "hard"
    assert capabilities.provider_native_steering is True
    assert capabilities.session_resume is True
    # The existing scheduler accepts runtime-neutral capabilities without
    # changing its public compatibility type during WP2.
    legacy = agent_adapter_capabilities(runtime)
    assert legacy.as_mapping() == capabilities.as_mapping()


def test_native_runtime_forwards_optional_adapter_hooks():
    delegate = _Delegate()
    runtime = NativeAgentRuntime(delegate)
    def callback(payload):
        return payload

    runtime.configure_heartbeat(callback, interval_seconds=12.5)

    assert delegate.heartbeat == (callback, 12.5)
    assert runtime.transport == "fake-transport"


def test_native_runtime_normalizes_session_reference_without_mutating_payload():
    runtime = NativeAgentRuntime(_Delegate())
    payload = {"session_id": "abc", "other": 1}

    reference = runtime.session_ref_from_result(payload)

    assert reference == RuntimeSessionRef(
        runtime_id="native",
        candidate_id="candidate-a",
        session_id="abc",
        backend="fake-native",
    )
    assert payload == {"session_id": "abc", "other": 1}
    assert runtime.session_ref_from_result({"session_id": ""}) is None
    assert runtime.session_ref_from_result(None) is None


@pytest.mark.parametrize(
    ("adapter", "expected_type"),
    [
        ("codex", "CodexAgentAdapter"),
        ("claude-code", "ClaudeCodeAgentAdapter"),
        ("opencode", "OpenCodeAgentAdapter"),
        ("antigravity-cli", "AntigravityCliAgentAdapter"),
    ],
)
def test_build_native_runtime_wraps_existing_adapter(
    tmp_path: Path, adapter: str, expected_type: str
):
    runtime = build_native_runtime(_provider(adapter), workdir=tmp_path, read_only=False)

    assert isinstance(runtime, NativeAgentRuntime)
    assert type(runtime.native_adapter).__name__ == expected_type
    assert runtime.provider_id == f"{adapter}-candidate"
    assert runtime.model == ("provider/model" if adapter == "opencode" else "model-x")
    assert agent_capability_weight(runtime, AgentCapability.IMPLEMENT) == 61
    assert agent_capability_weight(runtime, AgentCapability.REVIEW) == 77
    assert agent_max_complexity(runtime, AgentCapability.IMPLEMENT) == 82
    assert agent_max_complexity(runtime, AgentCapability.REVIEW) == 65
    assert runtime._execraft_concurrency_group == "native-group"


def test_build_native_runtime_preserves_codex_read_only_policy(tmp_path: Path):
    runtime = build_native_runtime(
        _provider("codex", sandbox="workspace-write"),
        workdir=tmp_path,
        read_only=True,
    )
    assert runtime.native_adapter._sandbox == "read-only"


def test_build_native_runtime_preserves_claude_read_only_policy(tmp_path: Path):
    runtime = build_native_runtime(
        _provider("claude-code", permission_mode="acceptEdits"),
        workdir=tmp_path,
        read_only=True,
    )
    assert runtime.native_adapter._permission_mode == "plan"


def test_build_native_runtime_preserves_opencode_read_only_policy(tmp_path: Path):
    runtime = build_native_runtime(
        _provider("opencode", auto_approve=True),
        workdir=tmp_path,
        read_only=True,
    )
    assert runtime.native_adapter._auto_approve is False


def test_build_native_runtime_preserves_antigravity_read_only_policy(tmp_path: Path):
    runtime = build_native_runtime(
        _provider("antigravity-cli", dangerously_skip_permissions=True),
        workdir=tmp_path,
        read_only=True,
    )
    assert runtime.native_adapter._dangerously_skip_permissions is False
