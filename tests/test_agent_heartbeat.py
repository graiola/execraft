"""Tests for provider-neutral agent heartbeat binding."""

from __future__ import annotations

import subprocess

from execraft.agents.heartbeat import AgentHeartbeatEmitter, run_with_heartbeat
from execraft.orchestrate.scheduler import StructuredHandoff


def _handoff() -> StructuredHandoff:
    return StructuredHandoff(
        work_package_id="WP10",
        stage="final_review",
        summary="Review WP10",
    )


def test_emitter_enriches_process_snapshot_with_agent_identity() -> None:
    received: list[dict] = []
    emitter = AgentHeartbeatEmitter(
        provider_id="opencode-go", model="opencode-go/deepseek-v4-flash"
    )
    emitter.configure(received.append, interval_seconds=30)

    callback = emitter.callback_for(_handoff())
    assert callback is not None
    callback({"pid": 42, "elapsed_seconds": 30, "io_active": True})

    assert received == [
        {
            "package_id": "WP10",
            "stage": "final_review",
            "agent_id": "opencode-go",
            "model": "opencode-go/deepseek-v4-flash",
            "pid": 42,
            "elapsed_seconds": 30,
            "io_active": True,
        }
    ]


def test_real_runner_receives_heartbeat_options() -> None:
    calls: list[dict] = []
    received: list[dict] = []
    emitter = AgentHeartbeatEmitter(provider_id="agent")
    emitter.configure(received.append, interval_seconds=12.5)

    def runner(args, **kwargs):
        calls.append({"args": args, **kwargs})
        kwargs["heartbeat_callback"](
            {"pid": 99, "elapsed_seconds": 12.5, "io_active": False}
        )
        return subprocess.CompletedProcess(args, 0, "", "")

    run_with_heartbeat(
        runner,
        runner_is_injected=False,
        args=["agent"],
        cwd="/tmp",
        timeout=60,
        emitter=emitter,
        handoff=_handoff(),
    )

    assert calls[0]["heartbeat_interval"] == 12.5
    assert received[0]["agent_id"] == "agent"
    assert received[0]["pid"] == 99


def test_injected_runner_keeps_legacy_signature() -> None:
    emitter = AgentHeartbeatEmitter(provider_id="agent")
    emitter.configure(lambda _payload: None, interval_seconds=30)
    calls: list[tuple] = []

    def runner(args, *, cwd, timeout):
        calls.append((args, cwd, timeout))
        return subprocess.CompletedProcess(args, 0, "", "")

    run_with_heartbeat(
        runner,
        runner_is_injected=True,
        args=["agent"],
        cwd="/tmp",
        timeout=60,
        emitter=emitter,
        handoff=_handoff(),
    )

    assert calls == [(["agent"], "/tmp", 60)]
