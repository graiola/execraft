"""Native runtime facade over the existing CLI agent adapters.

The concrete Codex/Claude/OpenCode/Antigravity adapters remain unchanged and
fully testable, but orchestration-facing construction now produces this facade.
That makes Native a first-class runtime without altering prompt rendering,
process supervision, sessions, failure classification, or usage extraction.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from execraft.agents.antigravity_cli_adapter import AntigravityCliAgentAdapter
from execraft.agents.claude_code_adapter import ClaudeCodeAgentAdapter
from execraft.agents.codex_adapter import CodexAgentAdapter
from execraft.agents.legacy_config import AgentProviderConfig
from execraft.agents.execution_identity import execution_identity_for_provider
from execraft.execution_identity import ExecutionIdentity
from execraft.model_registry import ModelRouteRegistry
from execraft.agents.opencode_adapter import OpenCodeAgentAdapter
from execraft.orchestrate.scheduler import agent_adapter_capabilities

from .contracts import (
    RuntimeCapabilities,
    RuntimeExecutionRequest,
    RuntimeExecutionResult,
    RuntimeSessionRef,
)


class NativeAgentRuntime:
    """Compatibility facade that executes through one existing CLI adapter.

    ``__getattr__`` intentionally forwards optional control/configuration hooks
    (heartbeat, streaming, steering, endpoint probes, etc.) without changing
    duplicate transport behavior.  Stable runtime/scheduler properties are
    explicit, making the delegation boundary visible and testable.
    """

    def __init__(
        self, adapter: Any, *, execution_identity: ExecutionIdentity | None = None
    ) -> None:
        self._adapter = adapter
        self._execution_identity = execution_identity or ExecutionIdentity(
            candidate_id=str(adapter.provider_id),
            runtime_id="native",
            runtime_backend=str(
                getattr(adapter, "adapter_name", adapter.__class__.__name__)
            ),
            model=str(getattr(adapter, "model", "")),
            concurrency_group=str(adapter.provider_id),
            legacy_provider_id=str(adapter.provider_id),
        )

    @property
    def runtime_id(self) -> str:
        """The configured runtime this candidate belongs to.

        Derived from the execution identity rather than hardcoded: under
        schema v4 several Native runtimes (``native-codex``,
        ``native-opencode``, ...) coexist, and a fixed ``"native"`` would
        disagree with this candidate's own identity.
        """
        return self._execution_identity.runtime_id

    @property
    def candidate_id(self) -> str:
        return self._execution_identity.candidate_id

    @property
    def execution_identity(self) -> ExecutionIdentity:
        return self._execution_identity

    @property
    def provider_id(self) -> str:
        """Legacy provider identity retained for persisted compatibility."""
        return self._execution_identity.provider_id

    @property
    def capabilities(self):
        return self._adapter.capabilities

    @property
    def availability(self):
        return self._adapter.availability

    @property
    def execution_capabilities(self) -> RuntimeCapabilities:
        legacy = agent_adapter_capabilities(self._adapter)
        return RuntimeCapabilities.from_mapping(legacy.as_mapping())

    @property
    def adapter_name(self) -> str:
        return str(getattr(self._adapter, "adapter_name", self._adapter.__class__.__name__))

    @property
    def model(self) -> str:
        return str(getattr(self._adapter, "model", ""))

    @property
    def native_adapter(self) -> Any:
        """Expose the delegated adapter for diagnostics/tests, not orchestration."""
        return self._adapter

    def execute(self, handoff: Any) -> dict[str, Any]:
        """Preserve the scheduler/test compatibility call surface."""

        return self._adapter.execute(handoff)

    def execute_runtime(
        self, request: RuntimeExecutionRequest
    ) -> RuntimeExecutionResult:
        """Execute the normalized runtime envelope."""

        output = self._adapter.execute(request.handoff)
        session_ref = self.session_ref_from_result(output)
        return RuntimeExecutionResult(
            output=dict(output),
            identity=self._execution_identity,
            session_ref=session_ref,
            runtime_metadata={"backend": self.adapter_name},
        )

    def cancel(self, execution_id: str) -> bool:
        """Forward cancellation when a Native adapter exposes it."""
        cancel = getattr(self._adapter, "cancel", None)
        if not callable(cancel):
            return False
        outcome = cancel(execution_id)
        return True if outcome is None else bool(outcome)

    def session_ref_from_result(
        self, result: Mapping[str, Any] | None
    ) -> RuntimeSessionRef | None:
        """Normalize provider session metadata without changing result payloads."""
        if not isinstance(result, Mapping):
            return None
        session_id = str(result.get("session_id", "")).strip()
        if not session_id:
            return None
        return RuntimeSessionRef(
            runtime_id=self.runtime_id,
            candidate_id=self.candidate_id,
            session_id=session_id,
            backend=self.adapter_name,
        )

    def __getattr__(self, name: str) -> Any:
        """Forward transport-specific optional hooks through the compatibility boundary."""
        if name.startswith("__"):
            raise AttributeError(name)
        return getattr(self._adapter, name)


def build_native_runtime(
    provider: AgentProviderConfig,
    *,
    workdir: Path,
    read_only: bool,
    opencode_config_path: Path | None = None,
    model_registry: ModelRouteRegistry | None = None,
    execution_identity: ExecutionIdentity | None = None,
) -> NativeAgentRuntime:
    """Construct a Native runtime from the legacy compatibility projection."""

    effort_by_capability = {
        capability.value: level for capability, level in provider.effort_by_capability
    }
    common = {
        "provider_id": provider.provider_id,
        "capabilities": set(provider.capabilities),
        "model": provider.model,
        "effort": provider.effort,
        "effort_by_capability": effort_by_capability,
        "workdir": workdir,
        "timeout_seconds": provider.timeout_seconds,
        "inactivity_timeout_seconds": provider.inactivity_timeout_seconds,
        "output_silence_timeout_seconds": provider.output_silence_timeout_seconds,
        "first_output_timeout_seconds": provider.first_output_timeout_seconds,
        "max_internal_retry_delay_seconds": provider.max_internal_retry_delay_seconds,
        "binary": provider.binary,
    }
    if provider.adapter == "codex":
        adapter = CodexAgentAdapter(
            **common,
            sandbox="read-only" if read_only else provider.sandbox,
            live_sessions=provider.live_sessions,
        )
    elif provider.adapter in {"claude", "claude-code"}:
        adapter = ClaudeCodeAgentAdapter(
            **common,
            permission_mode="plan" if read_only else provider.permission_mode,
            live_sessions=provider.live_sessions,
        )
    elif provider.adapter == "opencode":
        adapter = OpenCodeAgentAdapter(
            **common,
            auto_approve=provider.auto_approve and not read_only,
            agent_by_capability=dict(provider.agent_by_capability),
            format_repair_agent=provider.format_repair_agent,
            max_output_bytes=provider.max_output_bytes,
            config_path=opencode_config_path,
        )
    elif provider.adapter in {"antigravity", "antigravity-cli"}:
        adapter = AntigravityCliAgentAdapter(
            **common,
            dangerously_skip_permissions=(
                provider.dangerously_skip_permissions and not read_only
            ),
            sandbox_enabled=provider.sandbox_enabled,
            policy_paths=provider.policy_paths,
            capability_weight=provider.capability_weight,
            capability_weights=dict(provider.capability_weights),
        )
    else:  # pragma: no cover - configuration validation owns this branch.
        raise ValueError(f"unsupported agent adapter: {provider.adapter}")

    # Legacy Native provider policy is projected onto the runtime-facing
    # candidate for compatibility. Scheduling reads the normalized
    # ExecutionIdentity while these attributes preserve older extension/tests.
    identity = execution_identity or execution_identity_for_provider(
        provider, model_registry=model_registry
    )
    runtime = NativeAgentRuntime(adapter, execution_identity=identity)
    setattr(runtime, "_execraft_capability_weight", provider.capability_weight)
    setattr(runtime, "_execraft_capability_weights", dict(provider.capability_weights))
    setattr(runtime, "_execraft_max_complexity", provider.max_complexity)
    setattr(
        runtime,
        "_execraft_max_complexity_by_capability",
        dict(provider.max_complexity_by_capability),
    )
    setattr(runtime, "_execraft_concurrency_group", identity.concurrency_group)
    return runtime
