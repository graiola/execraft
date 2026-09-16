from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from execraft.execution_identity import ExecutionIdentity
from execraft.orchestrate.runtime_continuation import (
    RuntimeSessionBindingStore,
    build_delta_handoff,
    context_epoch,
    continuation_role,
)
from execraft.orchestrate.scheduler import AgentCapability, StructuredHandoff, build_agent_prompt
from execraft.runtime.contracts import RuntimeSessionRef


def _identity() -> ExecutionIdentity:
    return ExecutionIdentity(
        candidate_id="impl",
        runtime_id="openclaw",
        runtime_backend="gateway",
        model_route_id="qwen",
        model_provider="ollama",
        model="qwen",
        target_id="local",
        target_kind="local",
    )


def _handoff(tmp_path: Path) -> StructuredHandoff:
    capsule = tmp_path / "capsule.json"
    capsule.write_text(
        '{"source_fingerprints":{"PLAN.md":"plan-a","BRIEF.md":"brief-a"}}',
        encoding="utf-8",
    )
    return StructuredHandoff(
        work_package_id="WP8",
        stage="implementation",
        summary="Continue implementation",
        requirements=["preserve durable Execraft state"],
        acceptance_criteria=[{"id": "AC1", "description": "continuation is reconstructible"}],
        expected_output_schema={"type": "object"},
        working_directory=str(tmp_path),
        bounded_excerpts={"large.txt": "same-context\n" * 500},
        verification_summary="initial evidence",
        workflow_skills=[{"id": "ai-implement", "instructions": "mandatory skill body"}],
        skill_manifest=[{"id": "ai-implement", "version": "1", "content_hash": "skill-a"}],
        execution_context={"package_context": {"path": str(capsule)}},
        required_isolation="provider_policy",
    )


def test_context_epoch_tracks_authoritative_inputs_not_transient_evidence(tmp_path: Path):
    handoff = _handoff(tmp_path)
    identity = _identity()
    epoch = context_epoch(handoff, identity)

    assert context_epoch(replace(handoff, verification_summary="new evidence"), identity) == epoch
    assert context_epoch(replace(handoff, requirements=["changed requirement"]), identity) != epoch
    assert context_epoch(
        replace(
            handoff,
            skill_manifest=[
                {"id": "ai-implement", "version": "1", "content_hash": "skill-b"}
            ],
        ),
        identity,
    ) != epoch


def test_delta_handoff_omits_unchanged_large_context_but_keeps_skill_body(tmp_path: Path):
    previous = _handoff(tmp_path)
    current = replace(
        previous,
        handoff_id="next",
        attempt=2,
        verification_summary="tests now pass",
        execution_context={**previous.execution_context, "new_evidence": "green"},
    )
    epoch = context_epoch(current, _identity())
    delta = build_delta_handoff(
        current,
        previous,
        epoch=epoch,
        prior_invocation_id="inv-1",
        previous_workspace_digest="before",
        current_workspace_digest="after",
    )

    assert delta.bounded_excerpts == {}
    assert delta.requirements == []
    assert delta.acceptance_criteria == []
    assert delta.verification_summary == "tests now pass"
    assert delta.workflow_skills == current.workflow_skills
    assert delta.execution_context["runtime_continuation"]["context_epoch"] == epoch
    assert len(build_agent_prompt(delta)) < len(build_agent_prompt(current))


def test_package_role_binding_is_persistent_and_reviewer_isolated(tmp_path: Path):
    path = tmp_path / "runtime-sessions.sqlite3"
    store = RuntimeSessionBindingStore(path)
    identity = _identity()
    session = RuntimeSessionRef(
        runtime_id="openclaw",
        candidate_id="impl",
        session_id="agent:impl:session-1",
        backend="gateway-session-key",
        context_epoch="epoch-a",
    )
    store.upsert(
        package_id="WP8",
        role=continuation_role(AgentCapability.IMPLEMENT),
        identity=identity,
        epoch="epoch-a",
        session_ref=session,
        last_invocation_id="inv-1",
        last_workspace_digest="digest-1",
        reused=False,
    )

    restored = RuntimeSessionBindingStore(path).get("WP8", "implementer")
    assert restored is not None
    assert restored.session_ref.session_id == session.session_id
    assert restored.compatible(identity, "epoch-a") is True
    assert RuntimeSessionBindingStore(path).get("WP8", "reviewer") is None
    assert continuation_role(AgentCapability.FIX_REVIEW) == "implementer"
    assert continuation_role(AgentCapability.REVIEW) == "reviewer"


def test_corrupt_binding_is_discarded_for_cold_reconstruction(tmp_path: Path):
    import sqlite3

    path = tmp_path / "runtime-sessions.sqlite3"
    store = RuntimeSessionBindingStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO runtime_sessions(
                package_id, role, candidate_id, runtime_id, model_route_id,
                target_id, context_epoch, session_json, last_invocation_id,
                last_workspace_digest, reuse_count, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "WP8-corrupt",
                "implementer",
                "impl",
                "openclaw",
                "qwen",
                "local",
                "epoch-a",
                "{broken-json",
                "inv-1",
                "digest",
                0,
                "2026-08-29T00:00:00+00:00",
            ),
        )
    assert store.get("WP8-corrupt", "implementer") is None
    with sqlite3.connect(path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM runtime_sessions WHERE package_id = 'WP8-corrupt'"
        ).fetchone()[0]
    assert count == 0
