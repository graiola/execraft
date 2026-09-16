from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from execraft.orchestrate.scheduler import AgentExecutionError, Availability
from execraft.runtime.openclaw_remote_target import (
    OpenClawRemoteTargetController,
    RemoteOpenClawAgentRuntime,
    RemoteTargetCapacity,
    bind_openclaw_runtime_to_remote_target,
    remote_target_state_root,
)
from execraft.runtime_config import (
    OpenClawMode,
    OpenClawRuntimeOptions,
    RuntimeConfig,
    RuntimeKind,
)
from execraft.targets.config import (
    ExecutionTargetConfig,
    ExecutionTargetKind,
    RemoteRuntimeEnvironmentConfig,
)


@dataclass
class _Diag:
    status: str = "healthy"
    detail: str = ""

    @property
    def healthy(self):
        return self.status.startswith("healthy")


class _Service:
    def __init__(self):
        self.diag = _Diag()
        self.probes = 0

    def probe(self):
        self.probes += 1
        return self.diag


class _Delegate:
    availability = Availability.AVAILABLE
    candidate_id = "remote-worker"

    def __init__(self):
        self.calls = 0

    def execute(self, handoff):
        self.calls += 1
        return {"ok": True}

    def cancel(self, execution_id):
        return True


def _target(*, maximum: int = 1) -> ExecutionTargetConfig:
    return ExecutionTargetConfig(
        id="build01",
        kind=ExecutionTargetKind.REMOTE_RUNTIME,
        endpoint="wss://build01.example/ws?token=secret",
        concurrency_group="build01",
        workspace_transport="shared",
        max_concurrency=maximum,
        environment=RemoteRuntimeEnvironmentConfig(tools=("docker",), sandbox=True),
    )


def _controller(tmp_path: Path, *, maximum=1):
    target = _target(maximum=maximum)
    service = _Service()
    return (
        OpenClawRemoteTargetController(
            target,
            workdir=tmp_path / "task",
            service=service,
            capacity=RemoteTargetCapacity(maximum),
        ),
        service,
    )


def test_remote_target_binds_physical_gateway_without_changing_runtime_identity(tmp_path):
    runtime = RuntimeConfig(
        id="remote-openclaw",
        kind=RuntimeKind.OPENCLAW,
        openclaw=OpenClawRuntimeOptions(
            mode=OpenClawMode.EXTERNAL,
            gateway="ws://fallback.example:18789",
            auth_kind="none",
        ),
    )
    target = _target()

    bound = bind_openclaw_runtime_to_remote_target(runtime, target)

    assert bound.id == runtime.id
    assert bound.openclaw is not None
    assert bound.openclaw.gateway == target.endpoint
    assert runtime.openclaw is not None
    assert runtime.openclaw.gateway == "ws://fallback.example:18789"
    first = remote_target_state_root(tmp_path, "build01")
    second = remote_target_state_root(tmp_path, "build02")
    assert first != second
    assert first.parent == second.parent


def test_remote_target_provenance_is_deterministic_and_secret_free(tmp_path):
    controller, _ = _controller(tmp_path)
    meta = controller.telemetry()
    assert meta["remote_workspace"] == str((tmp_path / "task").resolve())
    assert len(meta["remote_workspace_provenance"]) == 64
    assert meta["repository_sync_performed"] is False
    assert "secret" not in meta["remote_gateway"]
    assert meta["remote_environment"]["tools"] == ["docker"]


def test_network_failure_does_not_poison_future_admission(tmp_path):
    controller, service = _controller(tmp_path)
    runtime = RemoteOpenClawAgentRuntime(_Delegate(), controller)
    service.diag = _Diag("unavailable", "partition")
    with pytest.raises(AgentExecutionError, match="partition") as exc:
        runtime.execute(object())
    assert exc.value.classification == "network_transient"
    service.diag = _Diag("healthy")
    assert runtime.execute(object()) == {"ok": True}
    assert service.probes == 2


def test_remote_capacity_fails_closed_and_recovers(tmp_path):
    controller, _ = _controller(tmp_path)
    runtime = RemoteOpenClawAgentRuntime(_Delegate(), controller)
    with controller.capacity.lease("build01"):
        assert runtime.availability == Availability.SESSION_LIMIT
        with pytest.raises(AgentExecutionError) as exc:
            runtime.execute(object())
        assert exc.value.classification == "session_limit"
    assert runtime.availability == Availability.AVAILABLE
