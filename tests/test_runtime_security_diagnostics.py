from __future__ import annotations

from types import SimpleNamespace

from execraft.agents.profile import AgentExecutionPolicy, AgentProfileConfig
from execraft.orchestrate.scheduler import AgentCapability
from execraft.runtime.security_diagnostics import diagnose_openclaw_security
from execraft.runtime_config import OpenClawMode, OpenClawRuntimeOptions, RuntimeConfig, RuntimeKind


def _runtime(mode: OpenClawMode) -> RuntimeConfig:
    return RuntimeConfig(
        id="openclaw",
        kind=RuntimeKind.OPENCLAW,
        openclaw=OpenClawRuntimeOptions(
            mode=mode,
            gateway="ws://127.0.0.1:18789",
            auth_kind="none",
            request_timeout_seconds=5,
        ),
    )


def _profile() -> AgentProfileConfig:
    return AgentProfileConfig(
        id="worker",
        name="worker",
        enabled=True,
        capabilities=frozenset({AgentCapability.IMPLEMENT, AgentCapability.REVIEW}),
        runtime_id="openclaw",
        model_route_id="model",
        policy=AgentExecutionPolicy(timeout_seconds=5, sandbox="workspace-write"),
    )


def _execution(mode: OpenClawMode):
    runtime = _runtime(mode)
    return SimpleNamespace(
        agents=(_profile(),),
        runtime=lambda runtime_id: runtime if runtime_id == runtime.id else None,
    )


def test_managed_doctor_reports_policy_without_claiming_live_proof() -> None:
    diagnostic = diagnose_openclaw_security(_execution(OpenClawMode.MANAGED), "openclaw")
    payload = diagnostic.as_mapping()

    assert diagnostic.enforcement == "managed_hard"
    assert diagnostic.live_enforcement_evidence == "not-verified-by-doctor"
    assert payload["profiles"][0]["read_only"]["hard_read_only"] is True
    assert payload["profiles"][0]["read_only"]["policy"]["workspace_access"] == "ro"
    assert payload["profiles"][0]["read_only"]["policy"]["exec_access"] == "deny"
    normal_projection = payload["profiles"][0]["normal"]["openclaw_projection"]
    assert normal_projection["sandbox"]["docker"]["network"] == "none"


def test_external_doctor_never_claims_hard_owned_enforcement() -> None:
    diagnostic = diagnose_openclaw_security(_execution(OpenClawMode.EXTERNAL), "openclaw")

    assert diagnostic.enforcement == "external_provider_policy"
    assert diagnostic.sandbox_owner == "external-gateway"
    assert any("does not claim hard isolation" in item for item in diagnostic.warnings)
