"""Safe, explicit diagnostics for the OpenClaw runtime security boundary.

The doctor surface must distinguish configured/projected policy from enforcement
that was actually demonstrated in a live sandbox.  Serializing a Docker/tool
configuration is useful evidence, but it is not proof that the deployed runtime
honored it.  This module therefore reports both the intended posture and the
verification state without reading secrets or runtime-private files.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from execraft.runtime_config import OpenClawMode, RuntimeKind

from .openclaw_security import openclaw_security_fragment
from .security_policy import RuntimeSecurityPolicy, runtime_security_policy


@dataclass(frozen=True)
class OpenClawProfileSecurityDiagnostic:
    """Effective normal/read-only policy for one configured scheduler profile."""

    profile_id: str
    enabled: bool
    capabilities: tuple[str, ...]
    configured_policy: str
    normal_policy: RuntimeSecurityPolicy
    read_only_policy: RuntimeSecurityPolicy

    def as_mapping(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "enabled": self.enabled,
            "capabilities": list(self.capabilities),
            "configured_policy": self.configured_policy,
            "normal": {
                "policy": self.normal_policy.as_mapping(),
                "openclaw_projection": openclaw_security_fragment(self.normal_policy),
            },
            "read_only": {
                "policy": self.read_only_policy.as_mapping(),
                "hard_read_only": self.read_only_policy.hard_read_only,
                "openclaw_projection": openclaw_security_fragment(self.read_only_policy),
            },
        }


@dataclass(frozen=True)
class OpenClawSecurityDiagnostic:
    """Security posture that can be reported without claiming live proof."""

    runtime_id: str
    mode: str
    enforcement: str
    sandbox_owner: str
    workspace_binding: str
    live_enforcement_evidence: str
    configured_secure: bool
    profiles: tuple[OpenClawProfileSecurityDiagnostic, ...]
    warnings: tuple[str, ...] = ()

    def as_mapping(self) -> dict[str, object]:
        return {
            "runtime_id": self.runtime_id,
            "mode": self.mode,
            "enforcement": self.enforcement,
            "sandbox_owner": self.sandbox_owner,
            "workspace_binding": self.workspace_binding,
            "live_enforcement_evidence": self.live_enforcement_evidence,
            "configured_secure": self.configured_secure,
            "profiles": [item.as_mapping() for item in self.profiles],
            "warnings": list(self.warnings),
        }


def diagnose_openclaw_security(
    execution: Any,
    runtime_id: str,
) -> OpenClawSecurityDiagnostic:
    """Describe one runtime's configured security posture without side effects.

    ``execution`` is intentionally accepted by structural shape: diagnostics only
    need ``runtime(id)`` and the normalized ``agents`` collection.  Keeping this
    helper outside the CLI avoids making presentation code an authority for
    security semantics.
    """

    runtime = execution.runtime(runtime_id)
    if runtime.kind != RuntimeKind.OPENCLAW or runtime.openclaw is None:
        raise ValueError(f"runtime {runtime_id!r} is not a configured OpenClaw runtime")

    options = runtime.openclaw
    managed = options.mode == OpenClawMode.MANAGED
    mode = str(getattr(options.mode, "value", options.mode))
    profiles: list[OpenClawProfileSecurityDiagnostic] = []

    for profile in sorted(
        (item for item in execution.agents if item.runtime_id == runtime_id),
        key=lambda item: item.id,
    ):
        capabilities = tuple(
            sorted(str(getattr(item, "value", item)) for item in profile.capabilities)
        )
        configured_policy = profile.policy.sandbox.strip() or "workspace-write"
        profiles.append(
            OpenClawProfileSecurityDiagnostic(
                profile_id=profile.id,
                enabled=profile.enabled,
                capabilities=capabilities,
                configured_policy=configured_policy,
                normal_policy=runtime_security_policy(profile, read_only=False),
                read_only_policy=runtime_security_policy(profile, read_only=True),
            )
        )

    if managed:
        enforcement = "managed_hard"
        sandbox_owner = "Execraft-managed-projection"
        workspace_binding = "agents.update exact Execraft-authorized task root"
        warnings = (
            "doctor validates configured/projected policy only; live sandbox enforcement "
            "remains unverified until the opt-in negative security suite passes on "
            "the deployment environment",
        )
    else:
        enforcement = "external_provider_policy"
        sandbox_owner = "external-gateway"
        workspace_binding = "agents.list exact workspace match required"
        warnings = (
            "external Gateway sandbox/tool configuration is externally owned; Execraft "
            "does not claim hard isolation for that runtime",
        )

    enabled_profiles = tuple(item for item in profiles if item.enabled)
    configured_secure = managed and bool(enabled_profiles) and all(
        item.normal_policy.sandbox_required
        and item.normal_policy.workspace_only
        and not item.normal_policy.elevated_allowed
        and item.read_only_policy.hard_read_only
        for item in enabled_profiles
    )
    return OpenClawSecurityDiagnostic(
        runtime_id=runtime.id,
        mode=mode,
        enforcement=enforcement,
        sandbox_owner=sandbox_owner,
        workspace_binding=workspace_binding,
        live_enforcement_evidence="not-verified-by-doctor",
        configured_secure=configured_secure,
        profiles=tuple(profiles),
        warnings=warnings,
    )


def render_openclaw_doctor(
    execution: Any,
    runtime_id: str,
    gateway_result: Any,
    *,
    as_json: bool,
) -> str:
    """Render Gateway health and security posture without bloating the CLI module."""

    security = diagnose_openclaw_security(execution, runtime_id)
    payload = dict(gateway_result.as_mapping())
    payload["security"] = security.as_mapping()
    if as_json:
        return json.dumps(payload, indent=2, sort_keys=True)

    lines = [
        f"OpenClaw runtime: {gateway_result.runtime_id}",
        f"  mode: {gateway_result.mode}",
        f"  gateway: {gateway_result.gateway}",
        f"  status: {gateway_result.status}",
    ]
    if gateway_result.version:
        lines.append(
            f"  version: {gateway_result.version} (protocol {gateway_result.protocol})"
        )
    if gateway_result.methods:
        lines.append(f"  advertised methods: {', '.join(gateway_result.methods)}")

    lines.extend(
        (
            "  security:",
            f"    enforcement: {security.enforcement}",
            f"    sandbox owner: {security.sandbox_owner}",
            f"    workspace binding: {security.workspace_binding}",
            f"    configured secure: {'yes' if security.configured_secure else 'no'}",
            f"    live enforcement evidence: {security.live_enforcement_evidence}",
        )
    )
    for profile_security in security.profiles:
        normal = profile_security.normal_policy
        readonly = profile_security.read_only_policy
        state = "enabled" if profile_security.enabled else "disabled"
        lines.append(
            f"    profile {profile_security.profile_id} ({state}): "
            f"normal={normal.policy_id}/{normal.workspace_access}/"
            f"exec:{normal.exec_access}/net:{normal.network_access}; "
            f"read-only={readonly.workspace_access}/exec:{readonly.exec_access}/"
            f"hard:{'yes' if readonly.hard_read_only else 'no'}"
        )
    lines.extend(f"    warning: {warning}" for warning in security.warnings)
    if gateway_result.warning:
        lines.append(f"  warning: {gateway_result.warning}")
    if gateway_result.detail:
        lines.append(f"  detail: {gateway_result.detail}")
    return "\n".join(lines)
