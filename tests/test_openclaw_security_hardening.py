"""Deterministic coverage for the OpenClaw security boundary.

The suite verifies configuration, secret minimization, approval failure modes,
diagnostics, and promotion requirements. Real Docker/model enforcement remains
in the opt-in live suite and is never inferred from these tests.
"""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from execraft.agents.config import parse_execution_config
from execraft.agents.profile import AgentExecutionPolicy, AgentProfileConfig
from execraft.execution_identity import ExecutionIdentity
from execraft.orchestrate.scheduler import AgentCapability, StructuredHandoff
from execraft.runtime.contracts import RuntimeApprovalDecision, RuntimeExecutionRequest
from execraft.runtime.openclaw_approvals import OpenClawApprovalBridge
from execraft.runtime.openclaw_process import OpenClawManagedProcess, OpenClawProcessError
from execraft.runtime.openclaw_protocol import GatewayEvent, GatewayFeatures
from execraft.runtime.openclaw_security import openclaw_security_fragment
from execraft.runtime.openclaw_security_turn import resolve_openclaw_security_turn
from execraft.runtime.security_diagnostics import diagnose_openclaw_security
from execraft.runtime.security_policy import runtime_security_policy
from execraft.runtime_config import OpenClawMode, OpenClawRuntimeOptions, RuntimeConfig, RuntimeKind


def _script(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


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


def _identity() -> ExecutionIdentity:
    return ExecutionIdentity(
        candidate_id="worker",
        runtime_id="openclaw",
        runtime_backend="gateway",
        model_route_id="model",
        model_provider="ollama",
        model="qwen-test",
        target_id="local",
        target_kind="local",
        concurrency_group="local",
        legacy_provider_id="worker",
    )


def _runtime() -> RuntimeConfig:
    return RuntimeConfig(
        id="openclaw",
        kind=RuntimeKind.OPENCLAW,
        openclaw=OpenClawRuntimeOptions(
            mode=OpenClawMode.MANAGED,
            auth_kind="none",
            request_timeout_seconds=5,
        ),
    )


def _request(root: Path, *, handler=None) -> RuntimeExecutionRequest:
    handoff = StructuredHandoff(
        work_package_id="security-probe",
        stage="implementation",
        summary="security approval probe",
        working_directory=str(root),
        required_isolation="hard",
    )
    return RuntimeExecutionRequest(
        identity=_identity(),
        handoff=handoff,
        capability="implement",
        package_id="security-probe",
        stage="implementation",
        approval_handler=handler,
    )


def test_managed_process_inherits_only_safe_and_explicit_credential_environment(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "capture.json"
    executable = _script(
        tmp_path / "fake-openclaw",
        f"""import json, os, time
with open({str(capture)!r}, 'w') as stream:
    json.dump({{
        'unrelated': os.environ.get('UNRELATED_DEPLOY_TOKEN'),
        'model': os.environ.get('MODEL_API_KEY'),
        'path': bool(os.environ.get('PATH')),
        'home': os.environ.get('HOME'),
    }}, stream)
time.sleep(60)
""",
    )
    manager = OpenClawManagedProcess(
        OpenClawRuntimeOptions(executable=str(executable), auth_kind="none"),
        runtime_id="openclaw",
        state_root=tmp_path / "state",
        environment={
            **os.environ,
            "UNRELATED_DEPLOY_TOKEN": "must-not-reach-openclaw",
            "MODEL_API_KEY": "model-secret",
        },
        credential_env_refs=("env:MODEL_API_KEY",),
    )
    try:
        manager.start()
        deadline = time.time() + 2
        while not capture.is_file() and time.time() < deadline:
            time.sleep(0.01)
        payload = json.loads(capture.read_text(encoding="utf-8"))
        assert payload["unrelated"] is None
        assert payload["model"] == "model-secret"
        assert payload["path"] is True
        assert Path(payload["home"]).is_relative_to(manager.paths.state_dir)
        assert payload["home"] != os.environ.get("HOME")
        assert stat.S_IMODE(manager.paths.config_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(manager.paths.log_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(manager.paths.log_path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(manager.paths.state_dir.stat().st_mode) == 0o700
    finally:
        manager.stop(timeout_seconds=1)


def test_managed_process_rejects_malformed_credential_environment_reference(
    tmp_path: Path,
) -> None:
    executable = _script(tmp_path / "fake-openclaw", "import time; time.sleep(1)\n")
    with pytest.raises(OpenClawProcessError, match="credential environment reference"):
        OpenClawManagedProcess(
            OpenClawRuntimeOptions(executable=str(executable), auth_kind="none"),
            runtime_id="openclaw",
            state_root=tmp_path,
            credential_env_refs=("env:BAD-NAME",),
        )


def test_managed_process_rejects_broadly_readable_explicit_config(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("POSIX permission enforcement is not meaningful on Windows")
    executable = _script(tmp_path / "fake-openclaw", "import time; time.sleep(1)\n")
    config = tmp_path / "openclaw.json"
    config.write_text('{"gateway":{"mode":"local"}}\n', encoding="utf-8")
    config.chmod(0o644)
    manager = OpenClawManagedProcess(
        OpenClawRuntimeOptions(
            executable=str(executable),
            auth_kind="none",
            config_path=str(config),
        ),
        runtime_id="openclaw",
        state_root=tmp_path / "state",
    )
    with pytest.raises(Exception, match="owner-only"):
        manager.start()


def test_security_projection_explicitly_contains_workspace_only_patch_guard() -> None:
    normal = openclaw_security_fragment(runtime_security_policy(_profile(), read_only=False))
    readonly = openclaw_security_fragment(runtime_security_policy(_profile(), read_only=True))

    assert normal["tools"]["fs"]["workspaceOnly"] is True
    assert normal["tools"]["exec"]["applyPatch"]["workspaceOnly"] is True
    assert normal["sandbox"]["docker"]["network"] == "none"
    assert normal["tools"]["elevated"]["enabled"] is False
    assert readonly["tools"]["exec"]["mode"] == "deny"
    assert readonly["sandbox"]["workspaceAccess"] == "ro"


class _ApprovalClient:
    def __init__(self, methods: tuple[str, ...]) -> None:
        self.hello = SimpleNamespace(features=GatewayFeatures(frozenset(methods), frozenset()))
        self.requests: list[tuple[str, dict[str, str]]] = []

    def request(self, method: str, params: dict[str, str]):
        self.requests.append((method, dict(params)))
        return {"resolved": True}


class _SecretReasonHandler:
    def decide(self, _request):
        return RuntimeApprovalDecision(
            "allow-once",
            "TOKEN=approval-secret /private/operator/path",
        )


def _approval_event() -> GatewayEvent:
    return GatewayEvent(
        "exec.approval.requested",
        {
            "id": "approval-1",
            "agentId": "worker",
            "sessionKey": "session-1",
            "command": "TOKEN=command-secret make test",
            "cwd": "/private/worktree",
            "allowedDecisions": ["allow-once", "deny"],
        },
    )


def test_approval_bridge_negotiates_canonical_and_legacy_public_resolvers(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    for methods, expected_method, expected_params in (
        (
            ("approval.resolve",),
            "approval.resolve",
            {"id": "approval-1", "kind": "exec", "decision": "deny"},
        ),
        (
            ("exec.approval.resolve",),
            "exec.approval.resolve",
            {"id": "approval-1", "decision": "deny"},
        ),
    ):
        client = _ApprovalClient(methods)
        bridge = OpenClawApprovalBridge(
            client=client,
            request=_request(worktree),
            agent_id="worker",
            session_key="session-1",
        )
        bridge(_approval_event())
        bridge.finish(timeout=1)
        assert client.requests == [(expected_method, expected_params)]


def test_approval_bridge_fails_closed_when_gateway_advertises_no_resolver(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    client = _ApprovalClient(())
    bridge = OpenClawApprovalBridge(
        client=client,
        request=_request(worktree, handler=_SecretReasonHandler()),
        agent_id="worker",
        session_key="session-1",
    )
    bridge(_approval_event())
    bridge.finish(timeout=1)

    assert client.requests == []
    telemetry = bridge.telemetry()
    assert telemetry["approval_resolution_error_count"] == 1
    assert telemetry["approval_allowed_once_count"] == 0


def test_approval_telemetry_never_persists_handler_command_or_path_text(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    client = _ApprovalClient(("approval.resolve",))
    bridge = OpenClawApprovalBridge(
        client=client,
        request=_request(worktree, handler=_SecretReasonHandler()),
        agent_id="worker",
        session_key="session-1",
    )
    bridge(_approval_event())
    bridge.finish(timeout=1)

    serialized = json.dumps(bridge.telemetry(), sort_keys=True)
    for secret in ("approval-secret", "command-secret", "/private/operator/path", "/private/worktree"):
        assert secret not in serialized
    assert "operator-allow-once" in serialized


def test_unknown_privileged_approval_event_is_recorded_fail_closed(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    client = _ApprovalClient(("approval.resolve",))
    bridge = OpenClawApprovalBridge(
        client=client,
        request=_request(worktree, handler=_SecretReasonHandler()),
        agent_id="worker",
        session_key="session-1",
    )
    bridge(
        GatewayEvent(
            "node.approval.requested",
            {
                "id": "unknown-approval",
                "agentId": "worker",
                "sessionKey": "session-1",
                "kind": "node",
            },
        )
    )
    bridge.finish(timeout=1)

    telemetry = bridge.telemetry()
    assert client.requests == []
    assert telemetry["approval_denied_count"] == 1
    assert telemetry["approval_records"][0]["reason"] == "malformed or unsupported approval request"


@pytest.mark.parametrize("root", [Path("/run"), Path("/var/run"), Path("/boot")])
def test_runtime_rejects_additional_documented_sensitive_workspace_roots(root: Path) -> None:
    if not root.is_dir():
        pytest.skip(f"{root} does not exist on this host")
    handoff = StructuredHandoff(
        work_package_id="security-probe",
        stage="implementation",
        summary="sensitive root check",
        working_directory=str(root),
    )
    request = RuntimeExecutionRequest(
        identity=_identity(),
        handoff=handoff,
        capability="implement",
        package_id="security-probe",
        stage="implementation",
    )
    with pytest.raises(Exception, match="refuses sensitive working directory"):
        resolve_openclaw_security_turn(_runtime(), _profile(), request)


def test_security_diagnostic_distinguishes_configured_policy_from_live_proof() -> None:
    runtime = _runtime()
    execution = SimpleNamespace(
        agents=(_profile(),),
        runtime=lambda runtime_id: runtime if runtime_id == runtime.id else None,
    )
    diagnostic = diagnose_openclaw_security(execution, runtime.id)

    assert diagnostic.configured_secure is True
    assert diagnostic.enforcement == "managed_hard"
    assert diagnostic.live_enforcement_evidence == "not-verified-by-doctor"
