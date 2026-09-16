from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

from execraft.agents.profile import AgentExecutionPolicy, AgentProfileConfig
from execraft.execution_identity import ExecutionIdentity
from execraft.orchestrate.scheduler import AgentCapability, AgentExecutionError, StructuredHandoff
from execraft.runtime.contracts import RuntimeExecutionRequest
from execraft.runtime.openclaw_agent import OpenClawAgentRuntime, OpenClawRuntimeHost
from execraft.runtime.openclaw_service import OpenClawDiagnostic
from execraft.runtime_config import OpenClawMode, OpenClawRuntimeOptions, RuntimeConfig, RuntimeKind


@dataclass
class _Accepted:
    run_id: str
    session_key: str
    final_payload: dict

    def wait_final(self, **_kwargs):
        return self.final_payload


class _Client:
    def __init__(self, workspace: Path | None = None):
        self.started = []
        self.cancelled = []
        self.run_started = threading.Event()
        self.release_wait = threading.Event()
        self.block_wait = False
        self.status = "ok"
        self.handlers = []
        self.agent_workspaces = (
            {"implementer": str(workspace.resolve())} if workspace is not None else {}
        )

    def request(self, method, params=None, **_kwargs):
        params = dict(params or {})
        if method == "agents.update":
            self.agent_workspaces[str(params["agentId"])] = str(params["workspace"])
            return {"agentId": params["agentId"], "workspace": params["workspace"]}
        if method == "agents.list":
            return {
                "agents": [
                    {"id": key, "workspace": value}
                    for key, value in sorted(self.agent_workspaces.items())
                ]
            }
        raise AssertionError(f"unexpected Gateway request: {method}")

    def add_event_handler(self, handler):
        self.handlers.append(handler)

    def remove_event_handler(self, handler):
        if handler in self.handlers:
            self.handlers.remove(handler)

    def start_agent(self, params, **kwargs):
        self.started.append((dict(params), dict(kwargs)))
        index = len(self.started)
        self.run_started.set()
        return _Accepted(
            run_id=f"run-{index}",
            session_key=f"agent:implementer:cold-{index}",
            final_payload={
                "status": self.status,
                "result": {
                    "payloads": [{"text": f"done-{index}"}],
                    "meta": {
                        "agentMeta": {
                            "usage": {"input": 100 + index, "output": 20, "total": 121},
                            "provider": "ollama",
                            "model": "qwen3-coder",
                            "sessionId": f"session-{index}",
                        }
                    },
                },
            },
        )

    def wait_agent_run(self, run_id, **_kwargs):
        if self.block_wait:
            self.release_wait.wait(timeout=2)
        status = "cancelled" if self.cancelled else self.status
        return {"runId": run_id, "status": status}

    def cancel_run(self, run_id, *, session_key=""):
        self.cancelled.append((run_id, session_key))
        self.release_wait.set()
        return {"aborted": True}


class _Service:
    def __init__(self, runtime, workspace: Path | None = None):
        self.runtime = runtime
        self.options = runtime.openclaw
        self.client = _Client(workspace)
        self.starts = 0
        self.stops = 0

    def start(self):
        self.starts += 1
        return OpenClawDiagnostic(
            runtime_id=self.runtime.id,
            mode=self.options.mode.value,
            status="healthy",
            gateway=self.options.gateway,
        )

    def stop(self):
        self.stops += 1


def _runtime(
    tmp_path: Path, *, gateway="ws://127.0.0.1:18789", mode=OpenClawMode.EXTERNAL
):
    runtime_config = RuntimeConfig(
        id="openclaw-managed",
        kind=RuntimeKind.OPENCLAW,
        openclaw=OpenClawRuntimeOptions(
            mode=mode,
            gateway=gateway,
            auth_kind="none",
            request_timeout_seconds=5,
        ),
    )
    profile = AgentProfileConfig(
        id="implementer",
        name="OpenClaw implementer",
        enabled=True,
        capabilities=frozenset({AgentCapability.IMPLEMENT}),
        runtime_id=runtime_config.id,
        model_route_id="qwen-route",
        policy=AgentExecutionPolicy(timeout_seconds=5),
    )
    identity = ExecutionIdentity(
        candidate_id=profile.id,
        runtime_id=runtime_config.id,
        runtime_backend="gateway",
        model_route_id="qwen-route",
        model_provider="ollama",
        model="qwen3-coder",
        target_id="local-ollama",
        target_kind="local",
        concurrency_group="local-ollama",
        legacy_provider_id=profile.id,
    )
    service = _Service(runtime_config, tmp_path)
    runtime = OpenClawAgentRuntime(
        profile=profile,
        runtime=runtime_config,
        identity=identity,
        host=OpenClawRuntimeHost(service),
    )
    return runtime, service


def _request(tmp_path: Path, runtime, *, attempt=1, handoff_id="handoff-wp7"):
    handoff = StructuredHandoff(
        work_package_id="WP7",
        stage="implementation",
        summary="Implement the cold OpenClaw runtime",
        working_directory=str(tmp_path),
        handoff_id=handoff_id,
        requirements=["keep Execraft as control plane"],
    )
    return RuntimeExecutionRequest(
        identity=runtime.execution_identity,
        handoff=handoff,
        capability="implement",
        package_id="WP7",
        stage="implementation",
        attempt=attempt,
    )


def test_cold_execution_maps_prompt_result_usage_and_session(tmp_path: Path):
    runtime, service = _runtime(tmp_path)
    result = runtime.execute_runtime(_request(tmp_path, runtime))

    assert result.output["ok"] is True
    assert result.output["final_message"] == "done-1"
    assert result.output["usage"]["input"] == 101
    assert result.session_ref is not None
    assert result.session_ref.session_id == "agent:implementer:cold-1"
    assert result.session_ref.backend == "gateway-session-key"
    assert result.runtime_metadata["run_id"] == "run-1"
    assert result.runtime_metadata["cold_start"] is True
    assert result.runtime_metadata["provider"] == "ollama"
    assert result.runtime_metadata["model"] == "qwen3-coder"
    assert result.runtime_metadata["openclaw_session_id"] == "session-1"
    params, kwargs = service.client.started[0]
    assert params["agentId"] == "implementer"
    assert "cwd" not in params
    assert service.client.agent_workspaces["implementer"] == str(tmp_path.resolve())
    assert params["deliver"] is False
    assert "EXECRAFT ORCHESTRATION CONTRACT" in params["message"]
    assert "keep Execraft as control plane" in params["message"]
    assert kwargs["idempotency_key"].startswith("execraft-implementer-1-")
    assert service.starts == service.stops == 1


def test_each_attempt_uses_a_distinct_cold_session(tmp_path: Path):
    runtime, service = _runtime(tmp_path)
    runtime.execute_runtime(_request(tmp_path, runtime, attempt=1))
    runtime.execute_runtime(_request(tmp_path, runtime, attempt=2))

    first = service.client.started[0][0]["sessionKey"]
    second = service.client.started[1][0]["sessionKey"]
    assert first != second
    assert "-1-" in first
    assert "-2-" in second
    assert runtime.execution_capabilities.session_resume is True


def test_runtime_rejects_incompatible_continuation_epoch(tmp_path: Path):
    runtime, _service = _runtime(tmp_path)
    request = _request(tmp_path, runtime)
    from execraft.runtime.contracts import RuntimeSessionRef

    request = RuntimeExecutionRequest(
        **{**request.__dict__, "session_ref": RuntimeSessionRef(runtime.runtime_id, runtime.candidate_id, "old")}
    )
    with pytest.raises(AgentExecutionError, match="incompatible context epoch"):
        runtime.execute_runtime(request)


def test_remote_gateway_execution_fails_closed_without_workspace_transport(tmp_path: Path):
    runtime, service = _runtime(tmp_path, gateway="wss://runtime.example.invalid")
    with pytest.raises(AgentExecutionError, match="remote full-runtime workspace transport is not supported") as exc:
        runtime.execute_runtime(_request(tmp_path, runtime))
    assert exc.value.classification == "configuration_error"
    assert service.starts == 0


def test_active_run_can_be_cancelled_end_to_end(tmp_path: Path):
    runtime, service = _runtime(tmp_path)
    service.client.block_wait = True
    errors = []

    def execute():
        try:
            runtime.execute_runtime(_request(tmp_path, runtime))
        except BaseException as exc:  # captured for assertion on the caller thread
            errors.append(exc)

    thread = threading.Thread(target=execute)
    thread.start()
    assert service.client.run_started.wait(timeout=1)
    for _ in range(100):
        active = runtime.active_runs
        if active:
            break
        threading.Event().wait(0.005)
    assert active
    assert runtime.cancel(active[0].execution_id) is True
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert service.client.cancelled == [("run-1", "agent:implementer:cold-1")]
    assert errors and isinstance(errors[0], AgentExecutionError)
    assert errors[0].classification == "cancelled"


def test_gateway_timeout_maps_to_typed_failure(tmp_path: Path):
    from execraft.runtime.openclaw_gateway import OpenClawGatewayTimeout

    runtime, service = _runtime(tmp_path)
    service.client.wait_agent_run = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        OpenClawGatewayTimeout("wait timed out")
    )
    with pytest.raises(AgentExecutionError) as exc:
        runtime.execute_runtime(_request(tmp_path, runtime))
    assert exc.value.classification == "timeout"


def test_real_gateway_client_service_and_runtime_execute_cold_run(tmp_path: Path):
    from execraft.runtime.openclaw_service import OpenClawGatewayService
    from tests.fakes.openclaw_gateway import AgentRunGatewayConnection, FakeDeviceStore

    runtime, _stub = _runtime(tmp_path, mode=OpenClawMode.EXTERNAL)
    connection = AgentRunGatewayConnection(
        agent_workspaces={"implementer": str(tmp_path.resolve())}
    )
    service = OpenClawGatewayService(
        runtime.runtime_config,
        state_root=tmp_path / "gateway-state",
        connection_factory=lambda *_args: connection,
        device_store=FakeDeviceStore(),
        payload_signer=lambda *_args: "signature",
    )
    actual = OpenClawAgentRuntime(
        profile=runtime.profile,
        runtime=runtime.runtime_config,
        identity=runtime.execution_identity,
        host=OpenClawRuntimeHost(service),
    )
    result = actual.execute_runtime(_request(tmp_path, actual))
    assert result.output["final_message"] == "completed by OpenClaw"
    assert result.output["usage"] == {"input": 120, "output": 30, "total": 150}
    assert result.runtime_metadata["run_id"] == "run-wp7-1"
    assert result.runtime_metadata["event_count"] == 1
    assert connection.closed is True


def test_real_gateway_client_cancellation_reaches_sessions_abort(tmp_path: Path):
    from execraft.runtime.openclaw_service import OpenClawGatewayService
    from tests.fakes.openclaw_gateway import AgentRunGatewayConnection, FakeDeviceStore

    runtime, _stub = _runtime(tmp_path, mode=OpenClawMode.EXTERNAL)
    connection = AgentRunGatewayConnection(
        hold_wait_until_abort=True,
        agent_workspaces={"implementer": str(tmp_path.resolve())},
    )
    service = OpenClawGatewayService(
        runtime.runtime_config,
        state_root=tmp_path / "gateway-state",
        connection_factory=lambda *_args: connection,
        device_store=FakeDeviceStore(),
        payload_signer=lambda *_args: "signature",
    )
    actual = OpenClawAgentRuntime(
        profile=runtime.profile,
        runtime=runtime.runtime_config,
        identity=runtime.execution_identity,
        host=OpenClawRuntimeHost(service),
    )
    errors = []

    def execute():
        try:
            actual.execute_runtime(_request(tmp_path, actual))
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=execute)
    thread.start()
    for _ in range(200):
        active = actual.active_runs
        if active:
            break
        threading.Event().wait(0.005)
    assert active
    assert actual.cancel(active[0].execution_id) is True
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert connection.aborted is True
    assert errors and isinstance(errors[0], AgentExecutionError)
    assert errors[0].classification == "cancelled"


def test_cleanup_failure_does_not_replay_or_discard_successful_run(tmp_path: Path):
    runtime, service = _runtime(tmp_path)

    def fail_stop():
        service.stops += 1
        raise RuntimeError("simulated cleanup failure")

    service.stop = fail_stop
    result = runtime.execute_runtime(_request(tmp_path, runtime))

    assert result.output["final_message"] == "done-1"
    assert len(service.client.started) == 1
    assert service.stops == 1
    assert "simulated cleanup failure" in result.runtime_metadata["cleanup_warning"]


def test_terminal_agent_meta_error_is_preserved_and_classified(tmp_path: Path):
    runtime, service = _runtime(tmp_path)
    service.client.status = "error"

    original = service.client.start_agent

    def start_with_error(*args, **kwargs):
        accepted = original(*args, **kwargs)
        accepted.final_payload["result"]["meta"]["error"] = {
            "kind": "provider_fetch_error",
            "message": "fetch failed while contacting satellite model endpoint",
        }
        return accepted

    service.client.start_agent = start_with_error
    with pytest.raises(AgentExecutionError) as exc:
        runtime.execute_runtime(_request(tmp_path, runtime))
    assert exc.value.classification == "network_transient"
    assert "fetch failed" in str(exc.value)


def test_terminal_failure_wins_over_inconsistent_wait_success(tmp_path: Path):
    runtime, service = _runtime(tmp_path)
    service.client.status = "error"
    service.client.wait_agent_run = lambda run_id, **_kwargs: {"runId": run_id, "status": "ok"}
    with pytest.raises(AgentExecutionError) as exc:
        runtime.execute_runtime(_request(tmp_path, runtime))
    assert exc.value.classification == "unclassified"
