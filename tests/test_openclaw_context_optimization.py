from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from execraft.execution_identity import ExecutionIdentity
from execraft.orchestrate.invocations import AgentInvocationStore
from execraft.orchestrate.runtime_continuation import RuntimeSessionBindingStore, context_epoch
from execraft.runtime.contracts import RuntimeSessionRef
from execraft.skills import SkillCatalog
from tests.test_openclaw_continuation import (
    _ContinuationClient,
    _execute,
    _handoff,
    _runner,
)
from tests.test_openclaw_skills import _managed_runtime


class _CacheContinuationClient(_ContinuationClient):
    def start_agent(self, params, **kwargs):
        accepted = super().start_agent(params, **kwargs)
        usage = accepted.final_payload["result"]["meta"]["agentMeta"]["usage"]
        usage["cached_input_tokens"] = 10 if len(self.started) == 1 else 25
        return accepted


def _identity(
    *,
    runtime: str = "openclaw-managed",
    backend: str = "gateway",
    route: str = "qwen-route",
    provider: str = "ollama",
    model: str = "qwen3-coder",
    target: str = "local",
    target_kind: str = "local",
) -> ExecutionIdentity:
    return ExecutionIdentity(
        candidate_id="implementer",
        runtime_id=runtime,
        runtime_backend=backend,
        model_route_id=route,
        model_provider=provider,
        model=model,
        target_id=target,
        target_kind=target_kind,
    )


def test_context_epoch_invalidates_every_c4_authoritative_dimension(tmp_path: Path) -> None:
    skill = SkillCatalog.load().materialize("implement", ["ai-implement"])[0].as_mapping()
    base = _handoff(tmp_path, attempt=1, verification="")
    base.workflow_skills = [skill]
    base.skill_manifest = [
        {
            "id": skill["id"],
            "version": skill["version"],
            "content_hash": skill["content_hash"],
        }
    ]
    identity = _identity()
    policy = {
        "read_only_enforcement": "hard",
        "workspace_write": True,
        "network_isolation": True,
        "command_allowlist": True,
        "structured_output_enforcement": "prompt_only",
    }
    epoch = context_epoch(base, identity, runtime_policy=policy)

    assert context_epoch(
        replace(base, requirements=["changed requirement"]), identity, runtime_policy=policy
    ) != epoch
    assert context_epoch(
        replace(
            base,
            acceptance_criteria=[{"id": "AC1", "description": "changed acceptance"}],
        ),
        identity,
        runtime_policy=policy,
    ) != epoch

    # Fail closed even if a caller accidentally leaves a stale declared manifest hash.
    changed_skill = dict(skill)
    changed_skill["instructions"] += "\nNew mandatory context rule."
    changed = replace(base, workflow_skills=[changed_skill])
    assert context_epoch(changed, identity, runtime_policy=policy) != epoch

    assert context_epoch(base, _identity(runtime="openclaw-other"), runtime_policy=policy) != epoch
    assert context_epoch(base, _identity(backend="future-gateway"), runtime_policy=policy) != epoch
    assert context_epoch(base, _identity(route="qwen-route-2"), runtime_policy=policy) != epoch
    assert context_epoch(base, _identity(provider="vllm"), runtime_policy=policy) != epoch
    assert context_epoch(base, _identity(model="qwen-next"), runtime_policy=policy) != epoch
    assert context_epoch(base, _identity(target="satellite"), runtime_policy=policy) != epoch
    assert context_epoch(
        base, _identity(target_kind="inference_endpoint"), runtime_policy=policy
    ) != epoch
    assert context_epoch(
        base,
        identity,
        runtime_policy={**policy, "network_isolation": False},
    ) != epoch


def test_managed_continuation_measures_context_and_lazy_skill_reduction(
    tmp_path: Path,
) -> None:
    runtime, service, _skill_workspace = _managed_runtime(tmp_path)
    client = _CacheContinuationClient(tmp_path)
    service.client = client
    store_path = tmp_path / "runtime-sessions.db"
    runner = _runner(tmp_path, RuntimeSessionBindingStore(store_path))

    skill = SkillCatalog.load().materialize("implement", ["ai-implement"])[0].as_mapping()
    first_handoff = _handoff(tmp_path, attempt=1, verification="")
    first_handoff.workflow_skills = [skill]
    first_handoff.skill_manifest = [
        {
            "id": skill["id"],
            "version": skill["version"],
            "content_hash": skill["content_hash"],
        }
    ]
    product = Path(first_handoff.working_directory)
    product.mkdir(parents=True, exist_ok=True)

    first = _execute(runner, runtime, first_handoff, 1, "w0", "w1")
    second_handoff = replace(
        first_handoff,
        handoff_id="handoff-2",
        attempt=2,
        verification_summary="focused verification passed",
    )
    second = _execute(runner, runtime, second_handoff, 2, "w1", "w2")

    first_meta = first.invocation.runtime_metadata
    second_meta = second.invocation.runtime_metadata
    assert first_meta["session_reused"] is False
    assert second_meta["session_reused"] is True
    assert second_meta["prompt_bytes"] < second_meta["cold_equivalent_prompt_bytes"]
    assert second_meta["repeated_context_avoided_bytes"] > 0
    assert second_meta["repeated_context_avoided_estimated_tokens"] > 0
    assert second_meta["skill_prompt_savings_bytes"] > 0
    assert second_meta["skill_prompt_savings_estimated_tokens"] > 0
    assert skill["instructions"] not in client.started[1][0]["message"]
    assert "workflow skill reference: ai-implement" in client.started[1][0]["message"]
    assert "EXECRAFT ORCHESTRATION CONTRACT" in client.started[1][0]["message"]

    summary = AgentInvocationStore(tmp_path / "invocations.db").usage_summary("project")
    optimization = summary["runtime_optimization"]
    assert optimization["openclaw_invocations"] == 2
    assert optimization["session_reused"] == 1
    assert optimization["repeated_context_avoided_bytes"] > 0
    assert optimization["skill_prompt_savings_bytes"] > 0
    assert optimization["provider_input_tokens"] == 203
    assert optimization["provider_cached_input_tokens"] == 35
    assert optimization["provider_cache_read_tokens"] == 35
    assert optimization["provider_uncached_input_tokens"] == 168
    assert optimization["retry_attempts"] == 1
    assert optimization["context_reduction"]["bytes_percent"] > 0


def test_role_scoped_bindings_remain_independent_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "runtime-sessions.db"
    store = RuntimeSessionBindingStore(path)
    identity = _identity()
    for role, session_id in (("implementer", "session-impl"), ("reviewer", "session-review")):
        store.upsert(
            package_id="context-package",
            role=role,
            identity=identity,
            epoch=f"epoch-{role}",
            session_ref=RuntimeSessionRef(
                runtime_id=identity.runtime_id,
                candidate_id=identity.candidate_id,
                session_id=session_id,
                backend="gateway-session-key",
                context_epoch=f"epoch-{role}",
            ),
            last_invocation_id=f"inv-{role}",
            last_workspace_digest=f"digest-{role}",
            reused=False,
        )

    restarted = RuntimeSessionBindingStore(path)
    implementer = restarted.get("context-package", "implementer")
    reviewer = restarted.get("context-package", "reviewer")
    assert implementer is not None and implementer.session_ref.session_id == "session-impl"
    assert reviewer is not None and reviewer.session_ref.session_id == "session-review"
    assert implementer.session_ref.session_id != reviewer.session_ref.session_id


def test_session_loss_cold_reconstructs_without_claiming_delta_savings(
    tmp_path: Path,
) -> None:
    runtime, service, _skill_workspace = _managed_runtime(tmp_path)
    client = _CacheContinuationClient(tmp_path)
    service.client = client
    runner = _runner(tmp_path, RuntimeSessionBindingStore(tmp_path / "runtime-sessions.db"))

    skill = SkillCatalog.load().materialize("implement", ["ai-implement"])[0].as_mapping()
    first_handoff = _handoff(tmp_path, attempt=1, verification="")
    first_handoff.workflow_skills = [skill]
    first_handoff.skill_manifest = [
        {
            "id": skill["id"],
            "version": skill["version"],
            "content_hash": skill["content_hash"],
        }
    ]
    Path(first_handoff.working_directory).mkdir(parents=True, exist_ok=True)

    _execute(runner, runtime, first_handoff, 1, "w0", "w1")
    client.sessions.clear()
    reconstructed = _execute(
        runner,
        runtime,
        replace(
            first_handoff,
            handoff_id="handoff-2",
            attempt=2,
            verification_summary="new evidence after Gateway restart",
        ),
        2,
        "w1",
        "w2",
    )

    metadata = reconstructed.invocation.runtime_metadata
    assert metadata["cold_reconstruction"] is True
    assert metadata["session_reused"] is False
    assert metadata["repeated_context_avoided_bytes"] == 0
    assert metadata["repeated_context_avoided_estimated_tokens"] == 0
    assert metadata["prompt_bytes"] == metadata["cold_equivalent_prompt_bytes"]
    assert "repeated-context" in client.started[1][0]["message"]


def test_legacy_session_binding_migrates_fail_closed_for_resolved_model(
    tmp_path: Path,
) -> None:
    import json
    import sqlite3

    path = tmp_path / "runtime-sessions.db"
    session = RuntimeSessionRef(
        runtime_id="openclaw-managed",
        candidate_id="implementer",
        session_id="legacy-session",
        backend="gateway-session-key",
        context_epoch="legacy-epoch",
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE runtime_sessions (
                package_id TEXT NOT NULL,
                role TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                runtime_id TEXT NOT NULL,
                model_route_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                context_epoch TEXT NOT NULL,
                session_json TEXT NOT NULL,
                last_invocation_id TEXT NOT NULL,
                last_workspace_digest TEXT NOT NULL,
                reuse_count INTEGER NOT NULL DEFAULT 0,
                guarded_compaction_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(package_id, role)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO runtime_sessions VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "context-package",
                "implementer",
                "implementer",
                "openclaw-managed",
                "qwen-route",
                "local",
                "legacy-epoch",
                json.dumps(session.as_mapping()),
                "inv-legacy",
                "digest",
                0,
                0,
                "2026-09-04T00:00:00+00:00",
            ),
        )

    migrated = RuntimeSessionBindingStore(path).get("context-package", "implementer")
    assert migrated is not None
    assert migrated.runtime_backend == ""
    assert migrated.model_provider == ""
    assert migrated.model_name == ""
    assert migrated.target_kind == ""
    assert migrated.identity_compatible(_identity()) is False
