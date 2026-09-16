from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat

import pytest

from execraft.agents.profile import AgentExecutionPolicy, AgentProfileConfig
from execraft.execution_identity import ExecutionIdentity
from execraft.orchestrate.prompt_metrics import measure_prompt_composition
from execraft.orchestrate.runtime_continuation import context_epoch
from execraft.orchestrate.scheduler import AgentCapability, StructuredHandoff, build_agent_prompt
from execraft.runtime.contracts import RuntimeExecutionRequest
from execraft.runtime.openclaw_agent import OpenClawAgentRuntime, OpenClawRuntimeHost
from execraft.runtime.openclaw_protocol import GatewayEvent
from execraft.runtime.openclaw_skills import (
    OpenClawSkillProjectionError,
    load_openclaw_skill_projection,
    materialize_openclaw_skills,
    openclaw_profile_workspace,
)
from execraft.runtime_config import (
    OpenClawMode,
    OpenClawRuntimeOptions,
    RuntimeConfig,
    RuntimeKind,
)
from execraft.skills import SkillCatalog
from tests.test_openclaw_agent_runtime import _Service, _runtime as _external_runtime


def _materialized_skill() -> dict:
    return SkillCatalog.load().materialize("implement", ["ai-implement"])[0].as_mapping()


def _managed_runtime(tmp_path: Path):
    runtime_config = RuntimeConfig(
        id="openclaw-managed",
        kind=RuntimeKind.OPENCLAW,
        openclaw=OpenClawRuntimeOptions(
            mode=OpenClawMode.MANAGED,
            gateway="ws://127.0.0.1:18789",
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
    service = _Service(runtime_config)
    skill_workspace = openclaw_profile_workspace(
        tmp_path / "state", runtime_id=runtime_config.id, candidate_id=profile.id
    )
    runtime = OpenClawAgentRuntime(
        profile=profile,
        runtime=runtime_config,
        identity=identity,
        host=OpenClawRuntimeHost(service),
        skill_workspace=skill_workspace,
    )
    return runtime, service, skill_workspace


def _handoff(tmp_path: Path, skill: dict) -> StructuredHandoff:
    manifest = {
        key: skill[key]
        for key in ("id", "source", "version", "content_hash", "instruction_bytes")
    }
    return StructuredHandoff(
        work_package_id="WP9",
        stage="implementation",
        summary="Project selected workflow skills lazily",
        working_directory=str(tmp_path / "product-worktree"),
        handoff_id="handoff-wp9",
        requirements=["keep Execraft as the skill policy authority"],
        workflow_skills=[skill],
        skill_manifest=[manifest],
    )


def _request(tmp_path: Path, runtime: OpenClawAgentRuntime, skill: dict):
    handoff = _handoff(tmp_path, skill)
    return RuntimeExecutionRequest(
        identity=runtime.execution_identity,
        handoff=handoff,
        capability="implement",
        package_id="WP9",
        stage="implementation",
        attempt=1,
    )


def test_materialization_is_deterministic_private_and_body_free_manifest(tmp_path: Path):
    skill = _materialized_skill()
    workspace = tmp_path / "runtime-workspace"

    first = materialize_openclaw_skills(workspace, [skill])
    second = materialize_openclaw_skills(workspace, [skill])

    skill_file = workspace / "skills" / "ai-implement" / "SKILL.md"
    manifest_path = workspace / ".execraft-skill-manifest.json"
    assert first.changed is True
    assert second.changed is False
    assert first.manifest_sha256 == second.manifest_sha256
    assert skill["instructions"] in skill_file.read_text(encoding="utf-8")
    manifest_text = manifest_path.read_text(encoding="utf-8")
    assert skill["instructions"] not in manifest_text
    assert json.loads(manifest_text)["manifest_sha256"] == first.manifest_sha256
    if os.name == "posix":
        assert stat.S_IMODE(workspace.stat().st_mode) == 0o700
        assert stat.S_IMODE(skill_file.stat().st_mode) == 0o600
        assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600


def test_materialization_replaces_exact_selected_set_and_removes_stale_skill(tmp_path: Path):
    implement = _materialized_skill()
    review = SkillCatalog.load().materialize("review", ["ai-review"])[0].as_mapping()
    workspace = tmp_path / "runtime-workspace"

    materialize_openclaw_skills(workspace, [implement, review])
    replacement = materialize_openclaw_skills(workspace, [implement])

    assert replacement.changed is True
    assert (workspace / "skills" / "ai-implement" / "SKILL.md").is_file()
    assert not (workspace / "skills" / "ai-review").exists()


def test_projection_rejects_hash_mismatch_and_symlink_redirection(tmp_path: Path):
    skill = _materialized_skill()
    corrupt = dict(skill)
    corrupt["content_hash"] = "0" * 64
    with pytest.raises(OpenClawSkillProjectionError, match="content hash"):
        materialize_openclaw_skills(tmp_path / "workspace", [corrupt])

    real_workspace = tmp_path / "real-workspace"
    real_workspace.mkdir()
    linked_workspace = tmp_path / "linked-workspace"
    linked_workspace.symlink_to(real_workspace, target_is_directory=True)
    with pytest.raises(OpenClawSkillProjectionError, match="must not be a symlink"):
        materialize_openclaw_skills(linked_workspace, [skill])


def test_loaded_projection_rejects_tampered_skill_body(tmp_path: Path):
    skill = _materialized_skill()
    workspace = tmp_path / "runtime-workspace"
    materialize_openclaw_skills(workspace, [skill])
    skill_file = workspace / "skills" / "ai-implement" / "SKILL.md"
    skill_file.write_text(skill_file.read_text() + "\nmalicious drift\n", encoding="utf-8")

    assert load_openclaw_skill_projection(workspace) is None


def test_profile_workspace_is_deterministic_and_state_scoped(tmp_path: Path):
    state_root = tmp_path / "state"
    first = openclaw_profile_workspace(
        state_root, runtime_id="openclaw-managed", candidate_id="implementer"
    )
    second = openclaw_profile_workspace(
        state_root, runtime_id="openclaw-managed", candidate_id="implementer"
    )

    assert first == second
    assert first.is_relative_to(state_root.resolve())
    assert first.parts[-3:] == ("openclaw-managed", "workspaces", "implementer")


def test_compact_prompt_removes_skill_body_but_keeps_provenance(tmp_path: Path):
    skill = _materialized_skill()
    handoff = _handoff(tmp_path, skill)

    native_prompt = build_agent_prompt(handoff)
    lazy_prompt = build_agent_prompt(
        handoff, embed_workflow_skill_instructions=False
    )
    native_metrics = measure_prompt_composition(
        native_prompt, workflow_skills=handoff.workflow_skills
    )
    lazy_metrics = measure_prompt_composition(
        lazy_prompt, workflow_skills=handoff.workflow_skills
    )

    assert skill["instructions"] in native_prompt
    assert skill["instructions"] not in lazy_prompt
    assert "workflow skill reference: ai-implement" in lazy_prompt
    assert skill["content_hash"][:12] in lazy_prompt
    assert lazy_metrics.skill_instruction_bytes == 0
    assert native_metrics.skill_instruction_bytes == skill["instruction_bytes"]
    assert lazy_metrics.prompt_bytes < native_metrics.prompt_bytes


def test_managed_runtime_materializes_skill_and_sends_only_compact_reference(tmp_path: Path):
    runtime, service, skill_workspace = _managed_runtime(tmp_path)
    skill = _materialized_skill()
    product_worktree = tmp_path / "product-worktree"
    product_worktree.mkdir()

    result = runtime.execute_runtime(_request(tmp_path, runtime, skill))

    params = service.client.started[0][0]
    skill_file = skill_workspace / "skills" / "ai-implement" / "SKILL.md"
    assert skill_file.is_file()
    assert skill["instructions"] in skill_file.read_text(encoding="utf-8")
    assert skill["instructions"] not in params["message"]
    assert "workflow skill reference: ai-implement" in params["message"]
    assert not (product_worktree / "skills").exists()
    assert result.rendered_prompt == params["message"]
    assert result.runtime_metadata["skill_delivery_mode"] == "lazy"
    assert result.runtime_metadata["skill_ids"] == ["ai-implement"]
    assert result.runtime_metadata["skill_instruction_bytes"] == skill["instruction_bytes"]
    assert result.runtime_metadata["skill_prompt_savings_bytes"] > 0
    assert result.runtime_metadata["skill_prompt_savings_estimated_tokens"] > 0


def test_external_gateway_retains_inline_skill_body_for_correctness(tmp_path: Path):
    runtime, service = _external_runtime(tmp_path, mode=OpenClawMode.EXTERNAL)
    skill = _materialized_skill()
    product_worktree = tmp_path / "product-worktree"
    product_worktree.mkdir()
    service.client.agent_workspaces["implementer"] = str(product_worktree.resolve())
    request = _request(tmp_path, runtime, skill)

    result = runtime.execute_runtime(request)

    prompt = service.client.started[0][0]["message"]
    assert skill["instructions"] in prompt
    assert result.runtime_metadata["skill_delivery_mode"] == "inline_external_fallback"
    assert result.runtime_metadata["skill_prompt_savings_bytes"] == 0


def test_gateway_skill_read_events_are_recorded_as_best_effort_telemetry(tmp_path: Path):
    runtime, service, skill_workspace = _managed_runtime(tmp_path)
    skill = _materialized_skill()
    product_worktree = tmp_path / "product-worktree"
    product_worktree.mkdir()
    original_start = service.client.start_agent

    def start_with_read(params, **kwargs):
        accepted = original_start(params, **kwargs)
        skill_path = skill_workspace / "skills" / "ai-implement" / "SKILL.md"
        event = GatewayEvent(
            name="agent",
            payload={
                "runId": accepted.run_id,
                "stream": "tool",
                "data": {"tool": "read", "path": str(skill_path)},
            },
        )
        for handler in tuple(service.client.handlers):
            handler(event)
        return accepted

    service.client.start_agent = start_with_read
    result = runtime.execute_runtime(_request(tmp_path, runtime, skill))

    assert result.runtime_metadata["skill_read_count"] == 1
    assert result.runtime_metadata["skill_reads"] == {"ai-implement": 1}


def test_skill_hash_change_changes_context_epoch_and_prevents_stale_reuse(tmp_path: Path):
    runtime, _service, _workspace = _managed_runtime(tmp_path)
    skill = _materialized_skill()
    first = _handoff(tmp_path, skill)
    changed_skill = dict(skill)
    changed_skill["instructions"] += "\nAdditional authoritative rule."
    changed_skill["instruction_bytes"] = len(changed_skill["instructions"].encode("utf-8"))
    changed_skill["content_hash"] = hashlib.sha256(
        changed_skill["instructions"].encode("utf-8")
    ).hexdigest()
    second = _handoff(tmp_path, changed_skill)

    first_epoch = context_epoch(first, runtime.execution_identity)
    second_epoch = context_epoch(second, runtime.execution_identity)

    assert first_epoch != second_epoch


def test_usage_accounting_measures_actual_lazy_prompt_not_authoritative_skill_body(
    tmp_path: Path,
) -> None:
    from execraft.orchestrate.usage import normalize_agent_usage

    runtime, _service, _skill_workspace = _managed_runtime(tmp_path)
    skill = _materialized_skill()
    (tmp_path / "product-worktree").mkdir()
    request = _request(tmp_path, runtime, skill)

    result = runtime.execute_runtime(request)
    usage = normalize_agent_usage(
        result.output,
        prompt=result.rendered_prompt,
        provider="ollama",
        model="qwen3-coder",
        capability="implement",
        package_id="WP9",
        workflow_skills=request.handoff.workflow_skills,
    )

    assert usage.prompt_bytes == result.runtime_metadata["prompt_bytes"]
    assert usage.skill_instruction_bytes == 0
    assert usage.skill_instruction_estimated_tokens == 0
    assert result.runtime_metadata["skill_instruction_bytes"] == skill["instruction_bytes"]


def test_format_repair_clears_corrupt_generated_skill_snapshot(tmp_path: Path) -> None:
    from dataclasses import replace

    from execraft.runtime.openclaw_skill_runtime import prepare_openclaw_skill_turn

    runtime, _service, skill_workspace = _managed_runtime(tmp_path)
    skill = _materialized_skill()
    handoff = _handoff(tmp_path, skill)
    materialize_openclaw_skills(skill_workspace, [skill])
    skill_file = skill_workspace / "skills" / "ai-implement" / "SKILL.md"
    skill_file.write_text(skill_file.read_text() + "\ntampered\n", encoding="utf-8")
    repair = replace(
        handoff,
        workflow_skills=[],
        skill_manifest=[],
        execution_context={"format_repair": True},
    )

    turn = prepare_openclaw_skill_turn(
        runtime.runtime_config.openclaw,
        workspace=skill_workspace,
        handoff=repair,
        has_session_ref=True,
    )

    assert turn.projection is not None
    assert turn.projection.skills == ()
    assert turn.projection.changed is True
    assert not skill_file.exists()
