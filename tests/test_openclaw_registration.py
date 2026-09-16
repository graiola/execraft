from pathlib import Path

from execraft.cli import _register_project_agents
from execraft.orchestrate.orchestrator import OrchestrationConfig, ProjectOrchestrator
from execraft.runtime.openclaw_agent import OpenClawAgentRuntime


def _mixed_config():
    return {
        "schema_version": 4,
        "runtimes": {
            "native": {"kind": "native", "adapter": "codex", "binary": "python3"},
            "openclaw": {
                "kind": "openclaw",
                "mode": "external",
                "gateway": "ws://127.0.0.1:18789",
                "auth_kind": "none",
            },
        },
        "execution_targets": {
            "local-ollama": {
                "kind": "local",
                "endpoint": "http://127.0.0.1:11434/v1",
                "concurrency_group": "local-gpu",
            }
        },
        "model_routes": {
            "openai": {"provider": "openai", "model": "gpt-5.6"},
            "qwen": {
                "provider": "ollama",
                "provider_alias": "ollama-local",
                "model": "qwen3-coder:30b-32k",
                "endpoint": "http://127.0.0.1:11434/v1",
                "api_family": "openai-compatible",
                "default_target": "local-ollama",
            },
        },
        "agents": {
            "native-worker": {
                "runtime": "native",
                "model_route": "openai",
                "capabilities": ["review"],
                "priority": 50,
            },
            "openclaw-worker": {
                "runtime": "openclaw",
                "model_route": "qwen",
                "capabilities": ["implement"],
                "priority": 100,
                "aliases": ["oc-worker"],
            },
        },
    }


def test_mixed_v4_registration_builds_openclaw_and_native_candidates(tmp_path: Path):
    orchestrator = ProjectOrchestrator(
        "mixed-wp7", config=OrchestrationConfig(state_dir=tmp_path / "orchestrator")
    )
    registered = _register_project_agents(
        orchestrator,
        _mixed_config(),
        workspace_root=tmp_path,
        state_root=tmp_path / "state",
        policy_profile="workspace-write",
    )
    assert registered == ["openclaw-worker:available", "native-worker:available"]
    openclaw = orchestrator._find_adapter("openclaw-worker")
    assert isinstance(openclaw, OpenClawAgentRuntime)
    assert orchestrator._find_adapter("oc-worker") is openclaw
    assert openclaw.execution_identity.model_route_id == "qwen"
    assert openclaw.execution_identity.model_provider == "ollama"
    assert openclaw.execution_identity.target_id == "local-ollama"
    assert openclaw.execution_identity.concurrency_group == "local-gpu"
    native = orchestrator._find_adapter("native-worker")
    assert native.execution_identity.runtime_id == "native"


def test_managed_registration_assigns_state_owned_skill_workspace_and_projects_it(
    tmp_path: Path,
) -> None:
    config = _mixed_config()
    config["runtimes"]["openclaw"]["mode"] = "managed"
    config["runtimes"]["openclaw"]["executable"] = "openclaw"
    orchestrator = ProjectOrchestrator(
        "managed-wp9", config=OrchestrationConfig(state_dir=tmp_path / "orchestrator")
    )
    state_root = tmp_path / "state"

    _register_project_agents(
        orchestrator,
        config,
        workspace_root=tmp_path / "product",
        state_root=state_root,
        policy_profile="workspace-write",
    )

    runtime = orchestrator._find_adapter("openclaw-worker")
    expected = (
        state_root.resolve()
        / "openclaw"
        / "openclaw"
        / "workspaces"
        / "openclaw-worker"
    )
    assert runtime._skill_workspace == expected
    process = runtime._host.service.process
    assert process is not None
    # The payload handed to the Gateway is in OpenClaw's wire shape: an
    # ``agents.list`` array whose entries carry their own id.
    projected_agent = next(
        item
        for item in process._config_payload["agents"]["list"]
        if item["id"] == "openclaw-worker"
    )
    assert "workspace" not in projected_agent
    assert process._config_payload["skills"]["load"]["extraDirs"] == [
        str((expected / "skills").resolve())
    ]
    assert process._config_payload["agents"]["defaults"]["skipBootstrap"] is True
