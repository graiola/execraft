"""OpenClaw projection for Execraft runtime-neutral security policies."""

from __future__ import annotations

from typing import Any

from execraft.agents.profile import AgentProfileConfig

from .security_policy import RuntimeSecurityPolicy, openclaw_agent_id, runtime_security_policy

_SANDBOX_IMAGE = "openclaw-sandbox:bookworm-slim"


def openclaw_security_fragment(policy: RuntimeSecurityPolicy) -> dict[str, Any]:
    """Render one fail-closed OpenClaw per-agent security fragment."""

    network = "none" if policy.network_access == "deny" else "bridge"
    tools: dict[str, Any] = {
        "allow": list(policy.allowed_tools),
        "deny": list(policy.denied_tools),
        "fs": {"workspaceOnly": policy.workspace_only},
        "elevated": {"enabled": False},
    }
    if policy.exec_access == "deny":
        tools["exec"] = {
            "host": "sandbox",
            "mode": "deny",
            "strictInlineEval": True,
            "applyPatch": {"workspaceOnly": True},
        }
    else:
        tools["exec"] = {
            "host": "sandbox",
            "mode": "ask" if policy.approval_access == "operator" else "full",
            "strictInlineEval": True,
            "applyPatch": {"workspaceOnly": True},
        }

    return {
        "sandbox": {
            "mode": "all",
            "backend": "docker",
            "scope": "session",
            "workspaceAccess": policy.workspace_access,
            "docker": {
                "image": _SANDBOX_IMAGE,
                "readOnlyRoot": True,
                "tmpfs": ["/tmp", "/var/tmp", "/run"],
                "network": network,
                "capDrop": ["ALL"],
            },
        },
        "tools": tools,
    }


def openclaw_agent_security_entries(
    profile: AgentProfileConfig,
) -> tuple[tuple[str, RuntimeSecurityPolicy], ...]:
    """Return writable/base and hard-read-only agent identities for a profile.

    A dedicated read-only OpenClaw agent lets the same Execraft scheduler profile
    serve both implementation and review without weakening review isolation.
    """

    base = runtime_security_policy(profile, read_only=False)
    readonly = runtime_security_policy(profile, read_only=True)
    return (
        (openclaw_agent_id(profile.id, read_only=False), base),
        (openclaw_agent_id(profile.id, read_only=True), readonly),
    )
