"""Runtime-neutral security policy for agent execution.

The control plane defines the required isolation semantics here; concrete
runtimes project them into provider-specific sandbox/tool syntax.  The policy is
intentionally small and fail-closed.  It describes *what* access is permitted,
not how OpenClaw, Codex, or another runtime happens to enforce it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from execraft.agents.profile import AgentProfileConfig

WorkspaceAccess = Literal["none", "ro", "rw"]
NetworkAccess = Literal["deny", "allow"]
ExecAccess = Literal["deny", "sandbox"]
ApprovalAccess = Literal["deny", "operator"]

_READ_ONLY_TOOLS = ("write", "edit", "apply_patch", "exec", "process", "code_execution")
_CONTROL_PLANE_TOOLS = (
    "group:web",
    "group:ui",
    "group:sessions",
    "group:automation",
    "group:messaging",
    "group:nodes",
    "group:agents",
    "group:media",
    "gateway",
    "cron",
    "nodes",
)
_ALWAYS_DENIED_TOOLS = _CONTROL_PLANE_TOOLS


@dataclass(frozen=True)
class RuntimeSecurityPolicy:
    """Portable security requirements for one runtime turn."""

    policy_id: str
    workspace_access: WorkspaceAccess
    network_access: NetworkAccess
    exec_access: ExecAccess
    approval_access: ApprovalAccess
    sandbox_required: bool = True
    workspace_only: bool = True
    elevated_allowed: bool = False
    allowed_tools: tuple[str, ...] = ("group:fs", "group:runtime")
    denied_tools: tuple[str, ...] = _ALWAYS_DENIED_TOOLS

    def __post_init__(self) -> None:
        if self.workspace_access == "ro" and self.exec_access != "deny":
            raise ValueError("read-only runtime policy must deny shell execution")
        if self.workspace_access == "none" and self.exec_access != "deny":
            raise ValueError("no-workspace runtime policy must deny shell execution")
        if self.elevated_allowed:
            raise ValueError("Execraft managed runtime policies do not permit elevated execution")

    @property
    def hard_read_only(self) -> bool:
        return self.workspace_access in {"none", "ro"} and all(
            tool in self.denied_tools for tool in _READ_ONLY_TOOLS
        )

    def as_mapping(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id,
            "workspace_access": self.workspace_access,
            "network_access": self.network_access,
            "exec_access": self.exec_access,
            "approval_access": self.approval_access,
            "sandbox_required": self.sandbox_required,
            "workspace_only": self.workspace_only,
            "elevated_allowed": self.elevated_allowed,
            "allowed_tools": list(self.allowed_tools),
            "denied_tools": list(self.denied_tools),
        }


def runtime_security_policy(
    profile: AgentProfileConfig,
    *,
    read_only: bool,
) -> RuntimeSecurityPolicy:
    """Resolve the effective portable policy for one profile/turn.

    Existing schema-v4 ``policy.sandbox`` names remain the compatibility input.
    OpenClaw never interprets Native permission flags directly.  A read-only
    handoff always upgrades the effective policy to the hard read-only preset,
    even when the same scheduler profile can also implement code.
    """

    requested = profile.policy.sandbox.strip().lower() or "workspace-write"
    if read_only or requested == "read-only":
        return RuntimeSecurityPolicy(
            policy_id="read-only",
            workspace_access="ro",
            network_access="deny",
            exec_access="deny",
            approval_access="deny",
            allowed_tools=("read",),
            denied_tools=tuple(dict.fromkeys((*_ALWAYS_DENIED_TOOLS, *_READ_ONLY_TOOLS))),
        )
    if requested == "workspace-write":
        return RuntimeSecurityPolicy(
            policy_id="workspace-write",
            workspace_access="rw",
            network_access="deny",
            exec_access="sandbox",
            approval_access="deny",
            denied_tools=_ALWAYS_DENIED_TOOLS,
        )
    if requested == "host-integration":
        # Managed runtime policy deliberately keeps shell execution in the sandbox. This preset
        # grants network egress to the sandbox, not host-shell escape.  Any
        # future host integration must be a separate reviewed policy surface.
        return RuntimeSecurityPolicy(
            policy_id="host-integration",
            workspace_access="rw",
            network_access="allow",
            exec_access="sandbox",
            approval_access="operator",
            denied_tools=_ALWAYS_DENIED_TOOLS,
        )
    raise ValueError(
        f"unsupported runtime security policy {profile.policy.sandbox!r}; "
        "expected read-only, workspace-write, or host-integration"
    )


def openclaw_agent_id(profile_id: str, *, read_only: bool) -> str:
    """Return the deterministic OpenClaw agent id for a security posture."""

    return f"{profile_id}-readonly" if read_only else profile_id


def openclaw_security_turn(
    profile: AgentProfileConfig, *, read_only: bool
) -> tuple[str, RuntimeSecurityPolicy]:
    """Resolve the OpenClaw agent identity and portable security policy together."""

    return (
        openclaw_agent_id(profile.id, read_only=read_only),
        runtime_security_policy(profile, read_only=read_only),
    )
