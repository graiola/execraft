"""Runtime-neutral execution contracts.

These contracts deliberately describe *agent execution*, not Execraft workflow
state.  The control plane owns packages, attempts, verification and durable
handoffs; a runtime may only execute the handoff it is given and report the
result/session metadata needed by later orchestration layers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from execraft.execution_identity import ExecutionIdentity


_READ_ONLY_LEVELS = {"advisory": 0, "provider_policy": 1, "hard": 2}
_STRUCTURED_OUTPUT_LEVELS = {"unsupported", "prompt_only", "tool_schema", "native_schema"}


@dataclass(frozen=True)
class RuntimeCapabilities:
    """Execution guarantees exposed by an agent runtime.

    The fields intentionally mirror the capability semantics already consumed
    by the scheduler so compatibility behavior is preserved exactly. Keeping this value
    object outside the scheduler prevents future runtimes from depending on a
    concrete orchestration implementation.
    """

    read_only_enforcement: str = "provider_policy"
    workspace_write: bool = True
    network_isolation: bool = False
    command_allowlist: bool = False
    structured_output: bool = True
    structured_output_enforcement: str = "prompt_only"
    streaming: bool = False
    raw_output_streaming: bool = True
    semantic_streaming: bool = False
    provider_native_steering: bool = False
    interactive_pty: bool = False
    session_resume: bool = False

    def __post_init__(self) -> None:
        if self.read_only_enforcement not in _READ_ONLY_LEVELS:
            raise ValueError(
                "read_only_enforcement must be advisory, provider_policy, or hard"
            )
        if self.provider_native_steering and not self.semantic_streaming:
            raise ValueError("provider_native_steering requires semantic_streaming")
        if self.structured_output_enforcement not in _STRUCTURED_OUTPUT_LEVELS:
            raise ValueError(
                "structured_output_enforcement must be unsupported, prompt_only, "
                "tool_schema, or native_schema"
            )
        if not self.structured_output:
            object.__setattr__(self, "structured_output_enforcement", "unsupported")

    def satisfies_read_only(self, required: str) -> bool:
        if required not in _READ_ONLY_LEVELS:
            raise ValueError(f"unsupported required isolation level: {required}")
        return (
            _READ_ONLY_LEVELS[self.read_only_enforcement]
            >= _READ_ONLY_LEVELS[required]
        )

    def as_mapping(self) -> dict[str, Any]:
        return {
            "read_only_enforcement": self.read_only_enforcement,
            "workspace_write": self.workspace_write,
            "network_isolation": self.network_isolation,
            "command_allowlist": self.command_allowlist,
            "structured_output": self.structured_output,
            "structured_output_enforcement": self.structured_output_enforcement,
            "streaming": self.streaming,
            "raw_output_streaming": self.raw_output_streaming,
            "semantic_streaming": self.semantic_streaming,
            "provider_native_steering": self.provider_native_steering,
            "interactive_pty": self.interactive_pty,
            "session_resume": self.session_resume,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RuntimeCapabilities":
        """Build a normalized capability value from legacy/runtime metadata."""
        structured_output = bool(value.get("structured_output", True))
        return cls(
            read_only_enforcement=str(
                value.get("read_only_enforcement", "provider_policy")
            ),
            workspace_write=bool(value.get("workspace_write", True)),
            network_isolation=bool(value.get("network_isolation", False)),
            command_allowlist=bool(value.get("command_allowlist", False)),
            structured_output=structured_output,
            structured_output_enforcement=str(
                value.get(
                    "structured_output_enforcement",
                    "prompt_only" if structured_output else "unsupported",
                )
            ),
            streaming=bool(value.get("streaming", False)),
            raw_output_streaming=bool(value.get("raw_output_streaming", True)),
            semantic_streaming=bool(
                value.get("semantic_streaming", value.get("streaming", False))
            ),
            provider_native_steering=bool(
                value.get(
                    "provider_native_steering",
                    value.get("native_steering", False),
                )
            ),
            interactive_pty=bool(value.get("interactive_pty", False)),
            session_resume=bool(value.get("session_resume", False)),
        )


@dataclass(frozen=True)
class RuntimeApprovalRequest:
    """Sanitized runtime-neutral request for one privileged action."""

    approval_id: str
    kind: str
    title: str = ""
    description: str = ""
    command: str = ""
    cwd: str = ""
    allowed_decisions: tuple[str, ...] = ("deny",)


@dataclass(frozen=True)
class RuntimeApprovalDecision:
    """One-shot operator decision returned to a runtime approval bridge."""

    decision: str
    reason: str = ""

    def __post_init__(self) -> None:
        if self.decision not in {"allow-once", "deny"}:
            raise ValueError("runtime approval decision must be allow-once or deny")


class RuntimeApprovalHandler(Protocol):
    """Synchronous bridge used when a runtime requires operator approval."""

    def decide(self, request: RuntimeApprovalRequest) -> RuntimeApprovalDecision:
        ...


@dataclass(frozen=True)
class RuntimeSessionRef:
    """Opaque continuation reference owned by one runtime implementation.

    Execraft may persist this reference, but must never treat runtime session
    contents as authoritative project/work-package state.
    """

    runtime_id: str
    candidate_id: str
    session_id: str
    backend: str = ""
    context_epoch: str = ""

    def as_mapping(self) -> dict[str, str]:
        return {
            "runtime_id": self.runtime_id,
            "candidate_id": self.candidate_id,
            "session_id": self.session_id,
            "backend": self.backend,
            "context_epoch": self.context_epoch,
        }


@dataclass(frozen=True)
class RuntimeExecutionRequest:
    """Normalized input passed from the control plane to an agent runtime."""

    identity: ExecutionIdentity
    handoff: Any
    capability: str = ""
    package_id: str = ""
    stage: str = ""
    attempt: int = 1
    metadata: Mapping[str, Any] = field(default_factory=dict)
    session_ref: RuntimeSessionRef | None = None
    context_epoch: str = ""
    continuation_handoff: Any | None = None
    approval_handler: RuntimeApprovalHandler | None = None


@dataclass(frozen=True)
class RuntimeExecutionResult:
    """Normalized runtime result while preserving the provider output payload."""

    output: dict[str, Any]
    identity: ExecutionIdentity
    session_ref: RuntimeSessionRef | None = None
    runtime_metadata: Mapping[str, Any] = field(default_factory=dict)
    rendered_prompt: str = ""



class AgentRuntime(Protocol):
    """Minimal substitution seam for an agent execution runtime.

    ``execute_runtime`` is the canonical control-plane boundary. The
    provider-shaped ``execute(handoff)`` method remains only as a compatibility
    surface for legacy Native callers while peer runtimes such as OpenClaw use
    the normalized request/result envelope directly.
    """

    @property
    def runtime_id(self) -> str:
        ...

    @property
    def candidate_id(self) -> str:
        ...

    @property
    def execution_capabilities(self) -> RuntimeCapabilities:
        ...

    @property
    def execution_identity(self) -> ExecutionIdentity:
        ...

    def execute_runtime(
        self, request: RuntimeExecutionRequest
    ) -> RuntimeExecutionResult:
        ...

    def execute(self, handoff: Any) -> dict[str, Any]:
        """Legacy compatibility surface retained during runtime migration."""
        ...

    def session_ref_from_result(
        self, result: Mapping[str, Any] | None
    ) -> RuntimeSessionRef | None:
        ...

    def cancel(self, execution_id: str) -> bool:
        """Best-effort cancellation for an in-flight runtime execution."""
        ...
