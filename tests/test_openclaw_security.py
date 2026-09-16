from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from execraft.agents.config import parse_execution_config
from execraft.agents.profile import AgentExecutionPolicy, AgentProfileConfig
from execraft.execution_identity import ExecutionIdentity
from execraft.orchestrate.runtime_continuation import context_epoch
from execraft.orchestrate.scheduler import AgentCapability, AgentExecutionError, StructuredHandoff
from execraft.runtime.contracts import (
    RuntimeApprovalDecision,
    RuntimeExecutionRequest,
)
from execraft.runtime.openclaw_agent import OpenClawAgentRuntime, OpenClawRuntimeHost
from execraft.runtime.openclaw_approvals import OpenClawApprovalBridge
from execraft.runtime.openclaw_projection import project_openclaw_config
from execraft.runtime.openclaw_protocol import GatewayEvent
from execraft.runtime.openclaw_security_turn import (
    bind_openclaw_security_turn,
    resolve_openclaw_security_turn,
)
from execraft.runtime.security_policy import runtime_security_policy
from execraft.runtime_config import OpenClawMode, OpenClawRuntimeOptions, RuntimeConfig, RuntimeKind
from tests.test_openclaw_agent_runtime import _Service


def _profile(*, sandbox: str = "workspace-write") -> AgentProfileConfig:
    return AgentProfileConfig(
        id="worker",
        name="worker",
        enabled=True,
        capabilities=frozenset({AgentCapability.IMPLEMENT, AgentCapability.REVIEW}),
        runtime_id="openclaw",
        model_route_id="model",
        policy=AgentExecutionPolicy(timeout_seconds=5, sandbox=sandbox),
    )


def _identity() -> ExecutionIdentity:
    return ExecutionIdentity(
        candidate_id="worker",
        runtime_id="openclaw",
        runtime_backend="gateway",
        model_route_id="model",
        model_provider="openai",
        model="gpt-test",
        target_id="local",
        target_kind="local",
        concurrency_group="local",
        legacy_provider_id="worker",
    )


def _runtime_config(mode: OpenClawMode = OpenClawMode.MANAGED) -> RuntimeConfig:
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


def _request(
    root: Path,
    *,
    read_only: bool = False,
    required_isolation: str = "advisory",
    additional: tuple[Path, ...] = (),
    handler=None,
) -> RuntimeExecutionRequest:
    handoff = StructuredHandoff(
        work_package_id="SECURITY-CHECK",
        stage="review" if read_only else "implementation",
        summary="exercise OpenClaw security",
        working_directory=str(root),
        additional_writable_roots=[str(item) for item in additional],
        read_only=read_only,
        required_isolation=required_isolation,
        requirements=["keep Execraft authoritative"],
    )
    return RuntimeExecutionRequest(
        identity=_identity(),
        handoff=handoff,
        capability="review" if read_only else "implement",
        package_id="SECURITY-CHECK",
        stage=handoff.stage,
        approval_handler=handler,
    )


def test_runtime_neutral_read_only_policy_denies_all_mutators() -> None:
    policy = runtime_security_policy(_profile(), read_only=True)
    assert policy.workspace_access == "ro"
    assert policy.exec_access == "deny"
    assert policy.network_access == "deny"
    assert policy.hard_read_only is True
    for tool in ("write", "edit", "apply_patch", "exec", "process", "code_execution"):
        assert tool in policy.denied_tools


def test_managed_projection_has_hard_readonly_agent_and_state_owned_skill_sources(
    tmp_path: Path,
) -> None:
    raw = {
        "schema_version": 4,
        "runtimes": {"openclaw": {"kind": "openclaw", "mode": "managed", "auth_kind": "none"}},
        "execution_targets": {"local": {"kind": "local"}},
        "model_routes": {
            "model": {
                "provider": "openai",
                "model": "gpt-test",
                "credential_ref": "OPENAI_API_KEY",
                "default_target": "local",
            }
        },
        "agents": {
            "worker": {
                "runtime": "openclaw",
                "model_route": "model",
                "capabilities": ["implement", "review"],
            }
        },
    }
    execution = parse_execution_config(raw)
    state_workspace = tmp_path / "state" / "worker"
    projection = project_openclaw_config(
        execution, "openclaw", agent_workspaces={"worker": state_workspace}
    )

    entries = projection.config["agents"]["entries"]
    reviewer = entries["worker-readonly"]
    writer = entries["worker"]
    assert reviewer["sandbox"]["workspaceAccess"] == "ro"
    assert reviewer["tools"]["exec"]["mode"] == "deny"
    assert reviewer["tools"]["allow"] == ["read"]
    assert reviewer["tools"]["elevated"]["enabled"] is False
    assert writer["sandbox"]["workspaceAccess"] == "rw"
    assert writer["sandbox"]["docker"]["network"] == "none"
    assert writer["sandbox"]["docker"]["capDrop"] == ["ALL"]
    assert writer["tools"]["exec"]["host"] == "sandbox"
    assert writer["tools"]["exec"]["mode"] == "full"
    assert "workspace" not in writer and "workspace" not in reviewer
    assert projection.config["agents"]["defaults"]["skipBootstrap"] is True
    assert projection.config["skills"]["load"]["extraDirs"] == [
        str((state_workspace.resolve() / "skills").resolve())
    ]


def test_managed_readonly_execution_binds_exact_workspace_and_never_sends_cwd(
    tmp_path: Path,
) -> None:
    runtime_config = _runtime_config(OpenClawMode.MANAGED)
    service = _Service(runtime_config)
    runtime = OpenClawAgentRuntime(
        profile=_profile(),
        runtime=runtime_config,
        identity=_identity(),
        host=OpenClawRuntimeHost(service),
    )
    worktree = tmp_path / "review-worktree"
    worktree.mkdir()
    result = runtime.execute_runtime(
        _request(worktree, read_only=True, required_isolation="hard")
    )

    params = service.client.started[0][0]
    assert params["agentId"] == "worker-readonly"
    assert "cwd" not in params
    assert service.client.agent_workspaces["worker-readonly"] == str(worktree.resolve())
    assert result.runtime_metadata["security_enforcement"] == "managed_hard"
    assert result.runtime_metadata["security_policy"]["workspace_access"] == "ro"


def test_single_external_product_root_isolated_and_multiple_roots_fail_closed(
    tmp_path: Path,
) -> None:
    control = tmp_path / "control"
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    control.mkdir()
    repo_a.mkdir()
    repo_b.mkdir()
    runtime = _runtime_config(OpenClawMode.MANAGED)

    single = resolve_openclaw_security_turn(
        runtime, _profile(), _request(control, additional=(repo_a,))
    )
    assert single.workspace == repo_a.resolve()
    assert single.request.handoff.working_directory == str(repo_a.resolve())
    assert single.request.handoff.additional_writable_roots == []

    with pytest.raises(AgentExecutionError, match="multiple external writable roots"):
        resolve_openclaw_security_turn(
            runtime, _profile(), _request(control, additional=(repo_a, repo_b))
        )


def test_external_gateway_is_verification_only_and_hard_readonly_fails_closed(
    tmp_path: Path,
) -> None:
    runtime = _runtime_config(OpenClawMode.EXTERNAL)
    worktree = tmp_path / "worktree"
    other = tmp_path / "other"
    worktree.mkdir()
    other.mkdir()

    turn = resolve_openclaw_security_turn(runtime, _profile(), _request(worktree))

    class Client:
        def request(self, method, params):
            assert method == "agents.list"
            return {"agents": [{"id": "worker", "workspace": str(other)}]}

    with pytest.raises(AgentExecutionError, match="workspace does not match"):
        bind_openclaw_security_turn(Client(), runtime, turn)

    with pytest.raises(AgentExecutionError, match="cannot prove Execraft hard read-only"):
        resolve_openclaw_security_turn(
            runtime,
            _profile(),
            _request(worktree, read_only=True, required_isolation="hard"),
        )


def test_sensitive_workspace_root_is_rejected() -> None:
    runtime = _runtime_config(OpenClawMode.MANAGED)
    with pytest.raises(AgentExecutionError, match="refuses sensitive working directory"):
        resolve_openclaw_security_turn(runtime, _profile(), _request(Path("/")))


class _ApprovalClient:
    def __init__(self) -> None:
        self.hello = SimpleNamespace(
            features=SimpleNamespace(methods=frozenset({"approval.resolve"}))
        )
        self.resolutions: list[tuple[str, dict]] = []

    def request(self, method, params):
        self.resolutions.append((method, dict(params)))
        return {"resolved": True}


class _AllowOnce:
    def decide(self, request):
        assert request.command == "TOKEN=super-secret make test"
        assert request.cwd.endswith("private-worktree")
        return RuntimeApprovalDecision("allow-once", "operator approved once")


def test_approval_bridge_defaults_to_deny_and_telemetry_redacts_command_and_cwd(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "private-worktree"
    worktree.mkdir()
    client = _ApprovalClient()
    request = _request(worktree, handler=_AllowOnce())
    bridge = OpenClawApprovalBridge(
        client=client, request=request, agent_id="worker", session_key="session-1"
    )
    bridge(
        GatewayEvent(
            "exec.approval.requested",
            {
                "id": "approval-1",
                "agentId": "worker",
                "sessionKey": "session-1",
                "command": "TOKEN=super-secret make test",
                "cwd": str(worktree),
                "allowedDecisions": ["allow-once", "deny", "allow-always"],
            },
        )
    )
    bridge.finish(timeout=1)

    assert client.resolutions == [
        (
            "approval.resolve",
            {"id": "approval-1", "kind": "exec", "decision": "allow-once"},
        )
    ]
    telemetry = bridge.telemetry()
    serialized = repr(telemetry)
    assert "super-secret" not in serialized
    assert "private-worktree" not in serialized
    assert telemetry["approval_allowed_once_count"] == 1

    deny_client = _ApprovalClient()
    deny_bridge = OpenClawApprovalBridge(
        client=deny_client,
        request=_request(worktree),
        agent_id="worker",
        session_key="session-2",
    )
    deny_bridge(
        GatewayEvent(
            "exec.approval.requested",
            {
                "id": "approval-2",
                "agentId": "worker",
                "sessionKey": "session-2",
                "allowedDecisions": ["allow-once", "deny"],
            },
        )
    )
    deny_bridge.finish(timeout=1)
    assert deny_client.resolutions[0][1]["decision"] == "deny"


def test_readonly_approval_is_denied_even_if_handler_would_allow(tmp_path: Path) -> None:
    worktree = tmp_path / "review"
    worktree.mkdir()
    client = _ApprovalClient()
    bridge = OpenClawApprovalBridge(
        client=client,
        request=_request(worktree, read_only=True, handler=_AllowOnce()),
        agent_id="worker-readonly",
        session_key="session-ro",
    )
    bridge(
        GatewayEvent(
            "exec.approval.requested",
            {
                "id": "approval-ro",
                "agentId": "worker-readonly",
                "sessionKey": "session-ro",
                "command": "TOKEN=super-secret make test",
                "cwd": str(worktree),
                "allowedDecisions": ["allow-once", "deny"],
            },
        )
    )
    bridge.finish(timeout=1)
    assert client.resolutions[0][1]["decision"] == "deny"


def test_security_policy_change_changes_context_epoch(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    handoff = _request(worktree).handoff
    identity = _identity()
    isolated = {
        "read_only_enforcement": "hard",
        "workspace_write": True,
        "network_isolation": True,
        "command_allowlist": True,
    }
    networked = dict(isolated, network_isolation=False)
    assert context_epoch(handoff, identity, runtime_policy=isolated) != context_epoch(
        handoff, identity, runtime_policy=networked
    )
