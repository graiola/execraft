from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace

import pytest
import yaml

from execraft.agents.execution_config import parse_v4_execution_config
from execraft.orchestrate.execution_health import ExecutionHealthStore, failure_health_dimension
from execraft.orchestrate.scheduler import AgentCapability, AgentExecutionError
from execraft.runtime.openclaw_agent import OpenClawRuntimeHost
from execraft.runtime.openclaw_projection import project_openclaw_config, to_gateway_config
from execraft.runtime.openclaw_service import OpenClawDiagnostic, OpenClawGatewayService
from execraft.runtime_config import (
    OpenClawMode,
    OpenClawRuntimeOptions,
    RuntimeConfig,
    RuntimeKind,
)
from tests.test_openclaw_agent_runtime import _request, _runtime
from tests.test_openclaw_continuation import (
    _ContinuationClient,
    _execute as _execute_attempt,
    _handoff as _continuation_handoff,
    _runner as _attempt_runner,
)

ROOT = Path(__file__).resolve().parents[1]


class _Clock:
    def __init__(self, step: float = 0.6) -> None:
        self.value = 0.0
        self.step = step

    def __call__(self) -> float:
        current = self.value
        self.value += self.step
        return current


class _LifecycleProcess:
    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0
        self.running = False

    def start(self):
        self.starts += 1
        self.running = True
        return SimpleNamespace(running=True, pid=1000 + self.starts, exit_code=None)

    def check_startup_exit(self) -> None:
        return None

    def stop(self):
        self.stops += 1
        self.running = False
        return SimpleNamespace(running=False, pid=None, exit_code=0)


class _LifecycleClient:
    def __init__(self, generation, *, fail_generations: int = 0) -> None:
        self._generation = generation
        self._fail_generations = fail_generations
        self.connects = 0
        self.closes = 0
        self.fail_close = False

    @property
    def state(self):
        return SimpleNamespace(warning="")

    def connect(self):
        self.connects += 1
        if self._generation() <= self._fail_generations:
            from execraft.runtime.openclaw_gateway import OpenClawGatewayError

            raise OpenClawGatewayError("Gateway connection refused")
        return SimpleNamespace(
            protocol=4,
            server_version="2026.7.1-2",
            features=SimpleNamespace(methods=frozenset({"health", "agent.wait"})),
        )

    def health(self):
        return {"status": "ok"}

    def close(self) -> None:
        self.closes += 1
        if self.fail_close:
            raise RuntimeError("simulated transport cleanup failure")


def _managed_service(
    tmp_path: Path,
    *,
    restart_attempts: int,
    fail_generations: int,
):
    process = _LifecycleProcess()
    client = _LifecycleClient(lambda: process.starts, fail_generations=fail_generations)
    runtime = RuntimeConfig(
        id="openclaw-local",
        kind=RuntimeKind.OPENCLAW,
        openclaw=OpenClawRuntimeOptions(
            mode=OpenClawMode.MANAGED,
            gateway="ws://127.0.0.1:18789",
            auth_kind="none",
            startup_timeout_seconds=1,
            reconnect_attempts=0,
            restart_attempts=restart_attempts,
        ),
    )
    service = OpenClawGatewayService(
        runtime,
        state_root=tmp_path / "execraft-state",
        client_factory=lambda *_args, **_kwargs: client,
        process_factory=lambda *_args, **_kwargs: process,
        monotonic=_Clock(),
        sleeper=lambda _seconds: None,
    )
    return service, process, client


def test_managed_gateway_restart_is_bounded_and_success_keeps_one_owned_process(
    tmp_path: Path,
) -> None:
    service, process, client = _managed_service(
        tmp_path,
        restart_attempts=1,
        fail_generations=1,
    )

    diagnostic = service.start()

    assert diagnostic.healthy is True
    assert process.starts == 2
    assert process.stops == 1
    assert process.running is True
    assert client.connects >= 2

    service.stop()
    assert process.stops == 2
    assert process.running is False


def test_failed_managed_start_reaps_owned_process_before_return(tmp_path: Path) -> None:
    service, process, _client = _managed_service(
        tmp_path,
        restart_attempts=0,
        fail_generations=99,
    )

    diagnostic = service.start()

    assert diagnostic.status == "unavailable"
    assert process.starts == 1
    assert process.stops == 1
    assert process.running is False


def test_transport_cleanup_failure_cannot_skip_managed_process_reaping(tmp_path: Path) -> None:
    service, process, client = _managed_service(
        tmp_path,
        restart_attempts=0,
        fail_generations=0,
    )
    assert service.start().healthy is True
    client.fail_close = True

    with pytest.raises(RuntimeError, match="transport cleanup"):
        service.stop()

    assert process.stops == 1
    assert process.running is False


def test_external_gateway_never_constructs_or_owns_a_managed_process(tmp_path: Path) -> None:
    process_factory_calls = 0
    client = _LifecycleClient(lambda: 1)
    runtime = RuntimeConfig(
        id="openclaw-external",
        kind=RuntimeKind.OPENCLAW,
        openclaw=OpenClawRuntimeOptions(
            mode=OpenClawMode.EXTERNAL,
            gateway="ws://127.0.0.1:18789",
            auth_kind="none",
        ),
    )

    def process_factory(*_args, **_kwargs):
        nonlocal process_factory_calls
        process_factory_calls += 1
        raise AssertionError("external Gateway must not create a managed process")

    service = OpenClawGatewayService(
        runtime,
        state_root=tmp_path / "state",
        client_factory=lambda *_args, **_kwargs: client,
        process_factory=process_factory,
    )
    diagnostic = service.probe()
    service.stop()

    assert diagnostic.healthy is True
    assert service.process is None
    assert process_factory_calls == 0


def test_sample_exposes_practical_openclaw_implementation_profiles_and_routes() -> None:
    raw = yaml.safe_load((ROOT / "projects/sample/agents.yaml").read_text(encoding="utf-8"))
    execution = parse_v4_execution_config(raw, include_disabled=True)
    projection = to_gateway_config(project_openclaw_config(execution, "openclaw-local").config)

    expected = {
        "openclaw-local-coder": (
            "model-ollama-local-qwen3-coder-30b-32k",
            "local-ollama",
            "ollama-local/qwen3-coder:30b-32k",
        ),
        "openclaw-gpu-a-coder": (
            "model-ollama-gpu-a-qwen3-coder-30b-32k",
            "gpu-node-a",
            "ollama-gpu-a/qwen3-coder:30b-32k",
        ),
        "openclaw-gpu-b-coder": (
            "model-ollama-gpu-b-qwen3-coder-30b-32k",
            "gpu-node-b",
            "ollama-gpu-b/qwen3-coder:30b-32k",
        ),
    }
    agents = {row["id"]: row for row in projection["agents"]["list"]}
    for profile_id, (route_id, target_id, model_ref) in expected.items():
        profile = execution.agent(profile_id)
        assert AgentCapability.IMPLEMENT in profile.capabilities
        assert profile.model_route_id == route_id
        assert profile.target_id == target_id
        assert profile.priority < execution.agent("opencode-ollama-local-coder").priority
        assert agents[profile_id]["model"] == model_ref

    providers = projection["models"]["providers"]
    assert providers["ollama-local"]["baseUrl"] == "http://127.0.0.1:11434"
    assert providers["ollama-gpu-a"]["baseUrl"] == "http://192.0.2.10:11434"
    assert providers["ollama-gpu-b"]["baseUrl"] == "http://192.0.2.11:11434"

    # Implementation and review remain separate role/profile contexts while
    # sharing the exact same canonical local model route and inference target.
    local_coder = execution.agent("openclaw-local-coder")
    local_reviewer = execution.agent("openclaw-local-review")
    assert local_coder.capabilities == frozenset({AgentCapability.IMPLEMENT})
    assert AgentCapability.REVIEW in local_reviewer.capabilities
    assert AgentCapability.IMPLEMENT not in local_reviewer.capabilities
    assert local_coder.model_route_id == local_reviewer.model_route_id
    assert local_coder.target_id == local_reviewer.target_id == "local-ollama"


def test_local_openclaw_implementation_and_review_keep_separate_durable_roles(
    tmp_path: Path,
) -> None:
    from execraft.orchestrate.runtime_continuation import RuntimeSessionBindingStore
    from execraft.runtime.openclaw_agent import OpenClawAgentRuntime, OpenClawRuntimeHost

    implementer, service = _runtime(tmp_path)
    client = _ContinuationClient(tmp_path)
    client.agent_workspaces["reviewer"] = str(tmp_path.resolve())
    service.client = client
    host = OpenClawRuntimeHost(service)
    implementer = OpenClawAgentRuntime(
        profile=implementer.profile,
        runtime=implementer.runtime_config,
        identity=implementer.execution_identity,
        host=host,
    )
    reviewer_profile = replace(
        implementer.profile,
        id="reviewer",
        name="OpenClaw reviewer",
        capabilities=frozenset({AgentCapability.REVIEW}),
    )
    reviewer_identity = replace(
        implementer.execution_identity,
        candidate_id="reviewer",
        legacy_provider_id="reviewer",
    )
    reviewer = OpenClawAgentRuntime(
        profile=reviewer_profile,
        runtime=implementer.runtime_config,
        identity=reviewer_identity,
        host=host,
    )
    sessions_path = tmp_path / "runtime-sessions.db"
    store = RuntimeSessionBindingStore(sessions_path)
    runner = _attempt_runner(tmp_path, store)

    implementation = _execute_attempt(
        runner,
        implementer,
        _continuation_handoff(tmp_path, attempt=1, verification=""),
        1,
        "w0",
        "w1",
        AgentCapability.IMPLEMENT,
    )
    review_handoff = replace(
        _continuation_handoff(tmp_path, attempt=1, verification="deterministic checks passed"),
        stage="review",
        handoff_id="handoff-review-1",
    )
    review = _execute_attempt(
        runner,
        reviewer,
        review_handoff,
        1,
        "w1",
        "w1",
        AgentCapability.REVIEW,
    )

    assert implementation.success is True
    assert review.success is True
    assert "deterministic checks passed" in client.started[1][0]["message"]
    implementer_binding = store.get("WP8", "implementer")
    reviewer_binding = store.get("WP8", "reviewer")
    assert implementer_binding is not None and reviewer_binding is not None
    assert implementer_binding.session_ref.session_id != reviewer_binding.session_ref.session_id
    assert implementer_binding.runtime_id == reviewer_binding.runtime_id
    assert implementer_binding.model_route_id == reviewer_binding.model_route_id == "qwen-route"
    assert implementer_binding.target_id == reviewer_binding.target_id == "local-ollama"


def test_gateway_unavailable_is_scoped_to_runtime_not_selected_target(tmp_path: Path) -> None:
    runtime, service = _runtime(tmp_path)
    service.start = lambda: OpenClawDiagnostic(
        runtime_id=runtime.runtime_id,
        mode="external",
        status="unavailable",
        gateway="ws://127.0.0.1:18789",
        detail="connection refused",
    )

    with pytest.raises(AgentExecutionError) as caught:
        runtime.execute_runtime(_request(tmp_path, runtime))

    error = caught.value
    assert error.classification == "network_transient"
    assert error.health_dimension == "runtime"
    assert failure_health_dimension(
        error.classification,
        runtime.execution_identity,
        dimension_hint=error.health_dimension,
    ) == ("runtime", runtime.runtime_id)


def test_model_target_network_failure_is_scoped_to_inference_target(tmp_path: Path) -> None:
    runtime, service = _runtime(tmp_path)
    service.client.status = "error"
    original = service.client.start_agent

    def start_with_target_failure(*args, **kwargs):
        accepted = original(*args, **kwargs)
        accepted.final_payload["result"]["meta"]["error"] = {
            "kind": "provider_fetch_error",
            "message": "fetch failed: connection refused by Ollama inference endpoint",
        }
        return accepted

    service.client.start_agent = start_with_target_failure
    with pytest.raises(AgentExecutionError) as caught:
        runtime.execute_runtime(_request(tmp_path, runtime))

    error = caught.value
    assert error.classification == "network_transient"
    assert error.health_dimension == "target"
    assert failure_health_dimension(
        error.classification,
        runtime.execution_identity,
        dimension_hint=error.health_dimension,
    ) == ("target", "local-ollama")


def test_invalid_model_is_scoped_to_model_route(tmp_path: Path) -> None:
    runtime, service = _runtime(tmp_path)
    service.client.status = "error"
    original = service.client.start_agent

    def start_with_model_failure(*args, **kwargs):
        accepted = original(*args, **kwargs)
        accepted.final_payload["result"]["meta"]["error"] = {
            "kind": "model_not_found",
            "message": "unknown model qwen3-coder",
        }
        return accepted

    service.client.start_agent = start_with_model_failure
    with pytest.raises(AgentExecutionError) as caught:
        runtime.execute_runtime(_request(tmp_path, runtime))

    error = caught.value
    assert error.classification == "invalid_model"
    assert error.health_dimension == "model_route"
    assert failure_health_dimension(
        error.classification,
        runtime.execution_identity,
        dimension_hint=error.health_dimension,
    ) == ("model_route", "qwen-route")


def test_incompatible_gateway_is_runtime_configuration_failure(tmp_path: Path) -> None:
    runtime, service = _runtime(tmp_path)
    service.start = lambda: OpenClawDiagnostic(
        runtime_id=runtime.runtime_id,
        mode="external",
        status="incompatible",
        gateway="ws://127.0.0.1:18789",
        version="unsupported",
        detail="Gateway protocol is incompatible",
    )

    with pytest.raises(AgentExecutionError) as caught:
        runtime.execute_runtime(_request(tmp_path, runtime))

    assert caught.value.classification == "configuration_error"
    assert caught.value.health_dimension == "runtime"


def test_success_status_without_assistant_payload_is_invalid_output(tmp_path: Path) -> None:
    runtime, service = _runtime(tmp_path)
    original = service.client.start_agent

    def start_without_model_output(*args, **kwargs):
        accepted = original(*args, **kwargs)
        accepted.final_payload["result"]["payloads"] = []
        return accepted

    service.client.start_agent = start_without_model_output
    with pytest.raises(AgentExecutionError) as caught:
        runtime.execute_runtime(_request(tmp_path, runtime))

    assert caught.value.classification == "invalid_output"
    assert caught.value.health_dimension == ""


def test_gateway_timeout_remains_typed_without_poisoning_model_or_target(tmp_path: Path) -> None:
    from execraft.runtime.openclaw_gateway import OpenClawGatewayTimeout

    runtime, service = _runtime(tmp_path)
    service.client.wait_agent_run = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        OpenClawGatewayTimeout("agent.wait timed out")
    )

    with pytest.raises(AgentExecutionError) as caught:
        runtime.execute_runtime(_request(tmp_path, runtime))

    assert caught.value.classification == "timeout"
    assert caught.value.health_dimension == ""
    assert failure_health_dimension("timeout", runtime.execution_identity) == (
        "candidate",
        runtime.candidate_id,
    )


def test_openclaw_runtime_host_releases_service_after_failure(tmp_path: Path) -> None:
    runtime, service = _runtime(tmp_path)
    service.start = lambda: OpenClawDiagnostic(
        runtime_id=runtime.runtime_id,
        mode="external",
        status="unavailable",
        gateway="ws://127.0.0.1:18789",
        detail="Gateway unavailable",
    )

    with pytest.raises(AgentExecutionError):
        with OpenClawRuntimeHost(service).lease():
            raise AssertionError("unhealthy runtime must not yield")

    assert service.stops == 1


def test_state_root_for_managed_gateway_is_separate_from_product_workspace(tmp_path: Path) -> None:
    from execraft.runtime.openclaw_process import resolve_managed_paths

    product_workspace = tmp_path / "product-repo"
    product_workspace.mkdir()
    state_root = tmp_path / "execraft-state"
    paths = resolve_managed_paths(
        OpenClawRuntimeOptions(mode=OpenClawMode.MANAGED, auth_kind="none"),
        runtime_id="openclaw-local",
        state_root=state_root,
    )

    assert paths.state_dir.is_relative_to(state_root.resolve())
    assert not paths.state_dir.is_relative_to(product_workspace.resolve())
    assert not paths.config_path.is_relative_to(product_workspace.resolve())
    assert not paths.log_path.is_relative_to(product_workspace.resolve())


@pytest.mark.skipif(os.name != "posix", reason="process-group orphan check requires POSIX")
def test_managed_process_stop_reaps_child_process_group(tmp_path: Path) -> None:
    import signal
    import stat
    import time

    from execraft.runtime.openclaw_process import OpenClawManagedProcess

    child_pid_path = tmp_path / "child.pid"
    executable = tmp_path / "fake-openclaw"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import os, signal, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])\n"
        "open(os.environ['CHILD_PID'], 'w').write(str(child.pid))\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    manager = OpenClawManagedProcess(
        OpenClawRuntimeOptions(
            mode=OpenClawMode.MANAGED,
            executable=str(executable),
            auth_kind="none",
        ),
        runtime_id="openclaw-local",
        state_root=tmp_path / "state",
        environment={**os.environ, "CHILD_PID": str(child_pid_path)},
        credential_env_refs=("CHILD_PID",),
    )
    manager.start()
    deadline = time.time() + 2
    while not child_pid_path.is_file() and time.time() < deadline:
        time.sleep(0.01)
    assert child_pid_path.is_file()
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))

    manager.stop(timeout_seconds=0.05)

    assert not manager.status.running
    deadline = time.time() + 2
    while time.time() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        # Best-effort cleanup before failing the test if the platform kept the
        # child alive despite process-group termination.
        try:
            os.kill(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        pytest.fail("managed Gateway child process survived process-group shutdown")


def test_attempt_ledger_persists_gateway_failure_on_runtime_dimension(tmp_path: Path) -> None:
    from execraft.orchestrate.runtime_continuation import RuntimeSessionBindingStore

    runtime, service = _runtime(tmp_path)
    service.start = lambda: OpenClawDiagnostic(
        runtime_id=runtime.runtime_id,
        mode="external",
        status="unavailable",
        gateway="ws://127.0.0.1:18789",
        detail="Gateway connection refused",
    )
    runner = _attempt_runner(
        tmp_path, RuntimeSessionBindingStore(tmp_path / "runtime-sessions.db")
    )

    result = _execute_attempt(
        runner,
        runtime,
        _continuation_handoff(tmp_path, attempt=1, verification=""),
        1,
        "w0",
        "w0",
    )

    assert result.success is False
    assert result.invocation.failure["classification"] == "network_transient"
    assert result.invocation.failure["health_dimension"] == "runtime"
    health = ExecutionHealthStore(tmp_path / "execution-health.json")
    assert health.get("runtime", runtime.runtime_id).reason == "network_transient"
    assert health.get("target", "local-ollama").status == "available"


def test_attempt_ledger_persists_model_endpoint_failure_on_target_dimension(
    tmp_path: Path,
) -> None:
    from execraft.orchestrate.runtime_continuation import RuntimeSessionBindingStore

    runtime, service = _runtime(tmp_path)
    service.client.status = "error"
    original = service.client.start_agent

    def start_with_target_failure(*args, **kwargs):
        accepted = original(*args, **kwargs)
        accepted.final_payload["result"]["meta"]["error"] = {
            "kind": "provider_fetch_error",
            "message": "fetch failed: connection refused by Ollama inference endpoint",
        }
        return accepted

    service.client.start_agent = start_with_target_failure
    runner = _attempt_runner(
        tmp_path, RuntimeSessionBindingStore(tmp_path / "runtime-sessions.db")
    )

    result = _execute_attempt(
        runner,
        runtime,
        _continuation_handoff(tmp_path, attempt=1, verification=""),
        1,
        "w0",
        "w0",
    )

    assert result.success is False
    assert result.invocation.failure["classification"] == "network_transient"
    assert result.invocation.failure["health_dimension"] == "target"
    health = ExecutionHealthStore(tmp_path / "execution-health.json")
    assert health.get("target", "local-ollama").reason == "network_transient"
    assert health.get("runtime", runtime.runtime_id).status == "available"


def test_stale_session_is_discarded_even_when_gateway_still_has_it(tmp_path: Path) -> None:
    from execraft.orchestrate.runtime_continuation import RuntimeSessionBindingStore

    runtime, service = _runtime(tmp_path)
    client = _ContinuationClient(tmp_path)
    service.client = client
    sessions_path = tmp_path / "runtime-sessions.db"
    runner = _attempt_runner(tmp_path, RuntimeSessionBindingStore(sessions_path))

    _execute_attempt(
        runner,
        runtime,
        _continuation_handoff(tmp_path, attempt=1, verification=""),
        1,
        "w0",
        "w1",
    )
    old_key = client.started[0][0]["sessionKey"]
    assert old_key in client.sessions
    with sqlite3.connect(sessions_path) as connection:
        connection.execute(
            "UPDATE runtime_sessions SET updated_at=? WHERE package_id=? AND role=?",
            ("2000-01-01T00:00:00+00:00", "WP8", "implementer"),
        )
        connection.commit()

    # Recreate the Execraft runner as well: durable state, not process-local state,
    # drives the cold reconstruction decision after a control-plane restart.
    runner = _attempt_runner(tmp_path, RuntimeSessionBindingStore(sessions_path))
    second = _execute_attempt(
        runner,
        runtime,
        _continuation_handoff(tmp_path, attempt=2, verification="fresh evidence"),
        2,
        "w1",
        "w2",
    )

    new_key = client.started[1][0]["sessionKey"]
    assert old_key != new_key
    assert old_key in client.sessions  # staleness, not Gateway loss, forced the rebuild
    assert "repeated-context" in client.started[1][0]["message"]
    assert second.invocation.runtime_metadata["cold_reconstruction"] is True
    assert second.invocation.runtime_metadata["continuation_decision"] == "stale_session"

class _ConcurrentHostService:
    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0
        self.first_start_entered = threading.Event()
        self.release_first_start = threading.Event()
        self.stop_entered = threading.Event()
        self.release_stop = threading.Event()
        self.block_stop = False
        self._lock = threading.Lock()

    def start(self):
        with self._lock:
            self.starts += 1
            generation = self.starts
        if generation == 1 and not self.release_first_start.is_set():
            self.first_start_entered.set()
            self.release_first_start.wait(timeout=2)
            raise RuntimeError("simulated first startup crash")
        return OpenClawDiagnostic(
            runtime_id="openclaw-local",
            mode="managed",
            status="healthy",
            gateway="ws://127.0.0.1:18789",
        )

    def stop(self) -> None:
        with self._lock:
            self.stops += 1
        if self.block_stop:
            self.stop_entered.set()
            self.release_stop.wait(timeout=2)


def test_shared_host_waiter_restarts_after_concurrent_startup_exception() -> None:
    service = _ConcurrentHostService()
    host = OpenClawRuntimeHost(service)  # type: ignore[arg-type]
    entered: list[str] = []
    errors: list[str] = []

    def use_host(name: str) -> None:
        try:
            with host.lease():
                entered.append(name)
        except RuntimeError as exc:
            errors.append(str(exc))

    first = threading.Thread(target=use_host, args=("first",))
    second = threading.Thread(target=use_host, args=("second",))
    first.start()
    assert service.first_start_entered.wait(timeout=1)
    second.start()
    service.release_first_start.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive() and not second.is_alive()
    assert errors == ["simulated first startup crash"]
    assert entered == ["second"]
    assert service.starts == 2
    assert service.stops == 2


def test_shared_host_serializes_last_stop_before_next_start() -> None:
    service = _ConcurrentHostService()
    # Let the initial start succeed; this test targets stop/start serialization.
    service.release_first_start.set()
    service.block_stop = True
    host = OpenClawRuntimeHost(service)  # type: ignore[arg-type]
    first_entered = threading.Event()
    second_entered = threading.Event()

    def first_use() -> None:
        with host.lease():
            first_entered.set()

    def second_use() -> None:
        with host.lease():
            second_entered.set()

    first = threading.Thread(target=first_use)
    first.start()
    assert first_entered.wait(timeout=1)
    assert service.stop_entered.wait(timeout=1)

    second = threading.Thread(target=second_use)
    second.start()
    # The new lease must remain behind the final shutdown transition.
    second.join(timeout=0.05)
    assert second.is_alive()
    assert service.starts == 1

    service.release_stop.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive() and not second.is_alive()
    assert second_entered.is_set()
    assert service.starts == 2
    assert service.stops == 2
