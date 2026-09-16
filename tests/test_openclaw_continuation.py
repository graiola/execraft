from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from execraft.orchestrate.agent_attempt import AgentAttemptRunner
from execraft.orchestrate.artifacts import AgentArtifactStore
from execraft.orchestrate.contract_health import ContractHealthStore
from execraft.orchestrate.execution_health import ExecutionHealthStore
from execraft.orchestrate.invocations import AgentInvocationStore
from execraft.orchestrate.provider_health import ProviderHealthStore
from execraft.orchestrate.runtime_continuation import RuntimeSessionBindingStore
from execraft.orchestrate.scheduler import AgentCapability, StructuredHandoff, build_agent_prompt
from execraft.runtime.openclaw_agent import OpenClawRuntimeHost
from tests.test_openclaw_agent_runtime import _runtime


@dataclass
class _Accepted:
    run_id: str
    session_key: str
    final_payload: dict

    def wait_final(self, **_kwargs):
        return self.final_payload


class _ContinuationClient:
    def __init__(self, workspace: Path | None = None) -> None:
        self.started: list[tuple[dict, dict]] = []
        self.sessions: set[str] = set()
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

    def describe_session(self, key: str):
        return {"key": key} if key in self.sessions else None

    def start_agent(self, params, **kwargs):
        params = dict(params)
        self.started.append((params, dict(kwargs)))
        self.sessions.add(params["sessionKey"])
        index = len(self.started)
        return _Accepted(
            run_id=f"run-{index}",
            session_key=params["sessionKey"],
            final_payload={
                "status": "ok",
                "result": {
                    "payloads": [{"text": f"done-{index}"}],
                    "meta": {
                        "agentMeta": {
                            "usage": {"input": 100 + index, "output": 20},
                            "provider": "ollama",
                            "model": "qwen3-coder",
                        }
                    },
                },
            },
        )

    def wait_agent_run(self, run_id, **_kwargs):
        return {"runId": run_id, "status": "ok"}

    def cancel_run(self, *_args, **_kwargs):
        return {"aborted": True}


def _runner(tmp_path: Path, session_store: RuntimeSessionBindingStore):
    return AgentAttemptRunner(
        agent_invocations=AgentInvocationStore(tmp_path / "invocations.db"),
        agent_artifacts=AgentArtifactStore(tmp_path / "artifacts"),
        contract_health=ContractHealthStore(tmp_path / "contract-health.json"),
        provider_health=ProviderHealthStore(tmp_path / "provider-health.json"),
        execution_health=ExecutionHealthStore(tmp_path / "execution-health.json"),
        runtime_sessions=session_store,
        project_id="project",
        task_id="task",
        invocation_project_id="project",
    )


def _execute(
    runner,
    runtime,
    handoff: StructuredHandoff,
    attempt: int,
    before: str,
    after: str,
    capability: AgentCapability = AgentCapability.IMPLEMENT,
):
    return runner.execute_attempt(
        adapter=runtime,
        handoff=handoff,
        capability=capability,
        package_id="WP8",
        stage=handoff.stage,
        shard_key="",
        attempt_number=attempt,
        parent_invocation_id="",
        triggering_event_id=f"event-{attempt}",
        workspace_before_digest=before,
        workspace_after_digest_fn=lambda: after,
        rendered_prompt=build_agent_prompt(handoff),
        metadata={
            "adapter": "openclaw",
            "model": "qwen3-coder",
            "execution_capabilities": runtime.execution_capabilities.as_mapping(),
        },
        strict_checks=False,
        require_structured_output=False,
    )


def _handoff(tmp_path: Path, *, attempt: int, verification: str) -> StructuredHandoff:
    return StructuredHandoff(
        work_package_id="WP8",
        stage="implementation",
        summary="Implement persistent sessions",
        handoff_id=f"handoff-{attempt}",
        attempt=attempt,
        working_directory=str(tmp_path),
        requirements=["durable state remains authoritative"],
        acceptance_criteria=[{"id": "AC1", "description": "resume safely"}],
        verification_summary=verification,
        bounded_excerpts={"unchanged.txt": "repeated-context\n" * 600},
        workflow_skills=[{"id": "ai-implement", "instructions": "skill body remains in WP8"}],
        skill_manifest=[{"id": "ai-implement", "version": "1", "content_hash": "skill-a"}],
    )


def test_attempt_runner_reuses_same_session_with_delta_and_persists_telemetry(tmp_path: Path):
    runtime, service = _runtime(tmp_path)
    client = _ContinuationClient(tmp_path)
    service.client = client
    store = RuntimeSessionBindingStore(tmp_path / "runtime-sessions.db")
    runner = _runner(tmp_path, store)

    first_handoff = _handoff(tmp_path, attempt=1, verification="")
    first = _execute(runner, runtime, first_handoff, 1, "w0", "w1")
    # Recreate Execraft's runner and binding store to prove the continuation
    # survives control-plane process lifecycle boundaries.
    runner = _runner(tmp_path, RuntimeSessionBindingStore(tmp_path / "runtime-sessions.db"))
    second_handoff = _handoff(tmp_path, attempt=2, verification="tests pass")
    second = _execute(runner, runtime, second_handoff, 2, "w1", "w2")

    first_key = client.started[0][0]["sessionKey"]
    second_key = client.started[1][0]["sessionKey"]
    assert first_key == second_key
    assert "repeated-context" in client.started[0][0]["message"]
    assert "repeated-context" not in client.started[1][0]["message"]
    assert "tests pass" in client.started[1][0]["message"]
    assert len(client.started[1][0]["message"]) < len(client.started[0][0]["message"])
    assert first.invocation.runtime_metadata["cold_start"] is True
    assert second.invocation.runtime_metadata["session_reused"] is True
    assert second.invocation.runtime_metadata["cold_start"] is False
    binding = store.get("WP8", "implementer")
    assert binding is not None and binding.reuse_count == 1
    assert binding.last_invocation_id == second.invocation.invocation_id


def test_missing_gateway_session_cold_reconstructs_full_handoff(tmp_path: Path):
    runtime, service = _runtime(tmp_path)
    client = _ContinuationClient(tmp_path)
    service.client = client
    store = RuntimeSessionBindingStore(tmp_path / "runtime-sessions.db")
    runner = _runner(tmp_path, store)

    _execute(runner, runtime, _handoff(tmp_path, attempt=1, verification=""), 1, "w0", "w1")
    old_key = client.started[0][0]["sessionKey"]
    client.sessions.clear()  # simulate Gateway restart/session loss
    second = _execute(
        runner,
        runtime,
        _handoff(tmp_path, attempt=2, verification="new evidence"),
        2,
        "w1",
        "w2",
    )

    new_key = client.started[1][0]["sessionKey"]
    assert new_key != old_key
    assert "repeated-context" in client.started[1][0]["message"]
    assert "durable state remains authoritative" in client.started[1][0]["message"]
    assert second.invocation.runtime_metadata["cold_reconstruction"] is True
    assert second.invocation.runtime_metadata["session_reused"] is False


def test_requirement_change_invalidates_binding_before_runtime_reuse(tmp_path: Path):
    runtime, service = _runtime(tmp_path)
    client = _ContinuationClient(tmp_path)
    service.client = client
    store = RuntimeSessionBindingStore(tmp_path / "runtime-sessions.db")
    runner = _runner(tmp_path, store)

    first = _handoff(tmp_path, attempt=1, verification="")
    _execute(runner, runtime, first, 1, "w0", "w1")
    changed = replace(
        _handoff(tmp_path, attempt=2, verification=""),
        requirements=["changed authoritative requirement"],
    )
    second = _execute(runner, runtime, changed, 2, "w1", "w2")

    assert client.started[0][0]["sessionKey"] != client.started[1][0]["sessionKey"]
    assert second.invocation.runtime_metadata["cold_start"] is True
    assert second.invocation.runtime_metadata["cold_reconstruction"] is False


def test_fix_review_and_format_repair_reuse_implementer_session(tmp_path: Path):
    runtime, service = _runtime(tmp_path)
    client = _ContinuationClient(tmp_path)
    service.client = client
    store = RuntimeSessionBindingStore(tmp_path / "runtime-sessions.db")
    runner = _runner(tmp_path, store)

    first = _handoff(tmp_path, attempt=1, verification="")
    _execute(runner, runtime, first, 1, "w0", "w1")
    fix = replace(_handoff(tmp_path, attempt=2, verification="review finding"), stage="fix_review")
    _execute(runner, runtime, fix, 2, "w1", "w2", AgentCapability.FIX_REVIEW)
    repair = replace(
        fix,
        handoff_id="handoff-3",
        attempt=3,
        requirements=[],
        acceptance_criteria=[],
        workflow_skills=[],
        skill_manifest=[],
        bounded_excerpts={"previous-invalid-response.txt": "{not-json"},
        execution_context={"format_repair": True},
    )
    third = _execute(runner, runtime, repair, 3, "w2", "w2", AgentCapability.FIX_REVIEW)

    keys = [item[0]["sessionKey"] for item in client.started]
    assert keys[0] == keys[1] == keys[2]
    assert third.invocation.runtime_metadata["session_reused"] is True
    assert "previous-invalid-response" in client.started[2][0]["message"]
