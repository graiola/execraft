"""Task-dashboard HTTP route dispatch."""

from __future__ import annotations

from typing import Any, Mapping

from .onboarding import _MISSING
from .payload import (
    payload_bool,
    payload_int,
    payload_string_list,
    string_list_mapping,
    string_mapping,
)


PROTECTED_GET_PATHS = frozenset({"/api/agent/console", "/api/agent/artifact"})


def _query_value(query: Mapping[str, list[str]], key: str, default: str = "") -> str:
    values = query.get(key)
    return values[0] if values else default


def _query_int(query: Mapping[str, list[str]], key: str, default: int) -> int:
    return int(_query_value(query, key, str(default)))


def dispatch_get(service: Any, path: str, query: Mapping[str, list[str]]) -> Any:
    """Dispatch one task-dashboard read request or return ``_MISSING``."""

    if path == "/api/snapshot":
        return service.snapshot()
    if path == "/api/tasks":
        return {"tasks": service.list_tasks()}
    if path == "/api/task/lifecycle":
        return service.task_lifecycle_snapshot()
    if path == "/api/task/repository-sync":
        return service.task_repository_sync(
            refresh=_query_value(query, "refresh", "0") == "1"
        )
    if path == "/api/task/repository-sync/options":
        return service.task_repository_sync_options(
            _query_value(query, "package_id"),
            refresh=_query_value(query, "refresh", "0") == "1",
        )
    if path == "/api/task/replan/candidate":
        return service.task_replan_candidate_view(
            _query_value(query, "candidate_id")
        )
    if path == "/api/package/execution-trace":
        return service.execution_trace(_query_value(query, "package_id"))
    if path == "/api/scope/approval":
        return service.scope_approval_preview(_query_value(query, "package_id"))
    if path == "/api/operator-acceptance":
        return service.operator_acceptance_preview(_query_value(query, "package_id"))
    if path == "/api/log":
        return service.read_log(
            _query_int(query, "offset", 0),
            source=_query_value(query, "source", "orchestrator"),
        )
    if path == "/api/agent/console":
        return service.read_agent_console(
            _query_value(query, "agent_id"),
            session_id=_query_value(query, "session_id"),
            preferred_package_id=_query_value(query, "preferred_package_id"),
            preferred_stage=_query_value(query, "preferred_stage"),
            offset=_query_int(query, "offset", 0),
            include_sessions=_query_value(query, "include_sessions", "1") != "0",
            include_events=_query_value(query, "include_events", "1") != "0",
            interaction_offset=_query_int(query, "interaction_offset", 0),
            include_interactions=_query_value(query, "include_interactions", "1") != "0",
            since_version=_query_int(query, "since_version", -1),
            wait_seconds=min(25.0, max(0.0, _query_int(query, "wait_ms", 0) / 1000.0)),
            terminal_screen_token=_query_value(query, "terminal_screen_token"),
        )
    if path == "/api/agent/artifact":
        return service.read_agent_artifact(_query_value(query, "path"))
    if path == "/api/workspace/changes":
        return service.workspace_snapshot()
    if path == "/api/workspace/diff":
        return service.workspace_diff(
            _query_value(query, "repository"),
            _query_value(query, "path"),
        )
    if path == "/api/config":
        return service.read_config(_query_value(query, "name"))
    return _MISSING


def dispatch_post(service: Any, path: str, payload: Mapping[str, Any]) -> Any:
    """Dispatch one task-dashboard mutation request or return ``_MISSING``."""

    if path == "/api/task/repository-sync/rollback":
        return service.task_repository_sync_rollback(
            str(payload.get("package_id", ""))
        )
    if path == "/api/task/repository-sync/accept-resolution":
        return service.task_repository_sync_accept_resolution(
            str(payload.get("package_id", ""))
        )
    if path == "/api/task/repository-sync/preview":
        repositories_raw = payload.get("repositories") or []
        if not isinstance(repositories_raw, list):
            repositories_raw = []
        return service.task_repository_sync_preview(
            package_id=str(payload.get("package_id", "")),
            repositories=[str(item) for item in repositories_raw if str(item).strip()],
            source_branches=string_mapping(payload.get("source_branches")),
            remote=str(payload.get("remote", "origin")),
        )
    if path == "/api/task/repository-sync/request":
        repositories_raw = payload.get("repositories") or []
        if not isinstance(repositories_raw, list):
            repositories_raw = []
        return service.task_repository_sync_request(
            package_id=str(payload.get("package_id", "")),
            repositories=[str(item) for item in repositories_raw if str(item).strip()],
            source_branches=string_mapping(payload.get("source_branches")),
            remote=str(payload.get("remote", "origin")),
            conflict_policy=str(payload.get("conflict_policy", "ai_resolve")),
            sync_package_id=str(payload.get("sync_package_id", "")),
            auto_resume=payload_bool(payload, "auto_resume", default=True),
        )
    if path in {"/api/task/final-sync/preview", "/api/task/final-sync/apply"}:
        repositories_raw = payload.get("repositories") or []
        if not isinstance(repositories_raw, list):
            repositories_raw = []
        arguments = {
            "repositories": [
                str(item) for item in repositories_raw if str(item).strip()
            ],
            "source_branches": string_mapping(payload.get("source_branches")),
            "remote": str(payload.get("remote", "origin")),
        }
        if path.endswith("/preview"):
            return service.task_final_sync_preview(**arguments)
        return service.task_final_sync_apply(
            **arguments,
            conflict_policy=str(payload.get("conflict_policy", "ai_resolve")),
            sync_package_id=str(payload.get("sync_package_id", "")),
        )
    if path == "/api/task/repository-sync/before":
        repositories_raw = payload.get("repositories") or []
        if not isinstance(repositories_raw, list):
            repositories_raw = []
        return service.task_repository_sync_before(
            before_package_id=str(payload.get("before_package_id", "")),
            repositories=[str(item) for item in repositories_raw if str(item).strip()],
            source_branches=string_mapping(payload.get("source_branches")),
            remote=str(payload.get("remote", "origin")),
            conflict_policy=str(payload.get("conflict_policy", "ai_resolve")),
            sync_package_id=str(payload.get("sync_package_id", "")),
            apply=payload_bool(payload, "apply", default=False),
        )
    if path == "/api/task/replan/candidate":
        return service.task_replan_candidate(
            requested_change=str(payload.get("requested_change", "")),
            brief_markdown=str(payload.get("brief_markdown", "")),
            plan_markdown=str(payload.get("plan_markdown", "")),
            plan_graph_yaml=str(payload.get("plan_graph_yaml", "")),
            package_mapping=string_mapping(payload.get("package_mapping")),
            provider_id=str(payload.get("provider_id", "")),
            from_current_files=payload_bool(
                payload, "from_current_files", default=False
            ),
            allow_structural_consistency=payload_bool(
                payload, "allow_structural_consistency", default=False
            ),
        )
    if path == "/api/task/replan/generate":
        return service.task_generate_plan(
            provider_id=str(payload.get("provider_id", "")),
        )
    if path == "/api/task/replan/apply":
        return service.task_replan_apply(str(payload.get("candidate_id", "")))
    if path == "/api/task/replan/recover":
        return service.task_replan_recover()
    if path == "/api/task/complete":
        return service.task_complete(
            dry_run=payload_bool(payload, "dry_run", default=False)
        )
    if path == "/api/run/start":
        return service.start_run(
            no_wait_for_agents=payload_bool(payload, "no_wait_for_agents", default=False)
        )
    if path == "/api/run/stop":
        return service.stop_run()
    if path == "/api/decompose":
        return service.decompose(str(payload.get("package_id", "")))
    if path == "/api/package/pause":
        return service.set_package_pause(
            str(payload.get("package_id", "")),
            paused=payload_bool(payload, "paused", default=True),
            reason=str(payload.get("reason", "")),
            apply_to_shards=payload_bool(payload, "apply_to_shards", default=False),
        )
    if path == "/api/package/directive":
        return service.set_work_package_directive(
            str(payload.get("package_id", "")),
            kind=str(payload.get("kind", "")),
            enabled=payload_bool(payload, "enabled", default=True),
            reason=str(payload.get("reason", "")),
        )
    if path == "/api/package/policy":
        return service.update_package_policy(
            package_id=str(payload.get("package_id", "")),
            agent_preferences=string_list_mapping(
                payload.get("agent_preferences"),
                mapping_label="agent-preferences",
                item_label="agent preferences",
            ),
            skill_preferences=string_list_mapping(
                payload.get("skill_preferences"),
                mapping_label="skill-preferences",
                item_label="skill preferences",
            ),
            agent_preference_binding_roles=(
                payload_string_list(payload, "agent_preference_binding_roles")
                if "agent_preference_binding_roles" in payload
                else None
            ),
            apply_to_shards=payload_bool(payload, "apply_to_shards", default=False),
        )
    if path == "/api/package/preferences":
        return service.update_package_preferences(
            package_id=str(payload.get("package_id", "")),
            preferences=string_list_mapping(payload.get("preferences")),
            apply_to_shards=payload_bool(payload, "apply_to_shards", default=False),
        )
    if path == "/api/agent/console/start":
        return service.start_manual_agent_console(
            str(payload.get("agent_id", "")),
            acknowledged=payload_bool(payload, "acknowledged", default=False),
        )
    if path == "/api/agent/console/stop":
        return service.stop_manual_agent_console(
            agent_id=str(payload.get("agent_id", "")),
            session_id=str(payload.get("session_id", "")),
        )
    if path == "/api/agent/console/input":
        return service.write_agent_console(
            session_id=str(payload.get("session_id", "")),
            action=str(payload.get("action", "")),
            data=str(payload.get("data", "")),
            rows=payload.get("rows"),
            columns=payload.get("columns"),
            signal_name=str(payload.get("signal", "")),
            acknowledged=payload_bool(payload, "acknowledged", default=False),
        )
    if path == "/api/supervisor/answer":
        return service.answer_supervisor(
            str(payload.get("option_id", "")),
            message=str(payload.get("message", "")),
        )
    if path == "/api/scope/accept":
        return service.approve_protected_scope(
            str(payload.get("package_id", "")),
            expected_candidates=list(payload_string_list(payload, "expected_candidates")),
        )
    if path == "/api/operator-acceptance/accept":
        sequence_raw = payload.get("expected_sequence")
        try:
            expected_sequence = (
                int(sequence_raw) if sequence_raw is not None else None
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("expected_sequence must be an integer") from exc
        return service.accept_operator_risk(
            str(payload.get("package_id", "")),
            reason=str(payload.get("reason", "")),
            expected_sequence=expected_sequence,
            acknowledged=payload_bool(payload, "acknowledged", default=False),
        )
    if path == "/api/agent/action":
        return service.start_agent_action(
            str(payload.get("agent_id", "")),
            str(payload.get("action", "")),
            timeout_seconds=payload_int(payload, "timeout_seconds", default=180),
        )
    if path == "/api/agent/promotion":
        enabled = payload_bool(payload, "enabled", default=True)
        capabilities = list(payload_string_list(payload, "capabilities"))
        if not enabled:
            return service.revoke_agent_promotion(
                str(payload.get("agent_id", "")),
                capabilities=capabilities or None,
            )
        return service.set_agent_promotion(
            str(payload.get("agent_id", "")),
            capabilities=capabilities,
            promoted_max_complexity=payload_int(
                payload, "promoted_max_complexity", default=100
            ),
            duration_seconds=payload_int(
                payload, "duration_seconds", default=4 * 60 * 60
            ),
            fallback_only=payload_bool(payload, "fallback_only", default=True),
            allow_final_review=payload_bool(
                payload, "allow_final_review", default=False
            ),
            package_id=str(payload.get("package_id", "")),
            reason=str(payload.get("reason", "")),
        )
    if path == "/api/workspace/ai-commit":
        return service.generate_commit_message(
            selections=string_list_mapping(payload.get("selections")),
            expected_digests=string_mapping(payload.get("expected_digests")),
            agent_id=str(payload.get("agent_id", "")),
        )
    if path == "/api/workspace/commit":
        return service.commit_workspace_changes(
            selections=string_list_mapping(payload.get("selections")),
            expected_digests=string_mapping(payload.get("expected_digests")),
            subject=str(payload.get("subject", "")),
            body=str(payload.get("body", "")),
            reviewed=payload_bool(payload, "reviewed", default=False),
        )
    if path == "/api/workspace/delete":
        return service.delete_workspace_files(
            selections=string_list_mapping(payload.get("selections")),
            expected_digests=string_mapping(payload.get("expected_digests")),
        )
    if path == "/api/config/validate":
        return service.validate_config(
            str(payload.get("name", "")),
            str(payload.get("content", "")),
        )
    if path == "/api/config/save":
        return service.save_config(
            str(payload.get("name", "")),
            str(payload.get("content", "")),
            str(payload.get("sha256", "")),
        )
    return _MISSING


__all__ = ["PROTECTED_GET_PATHS", "dispatch_get", "dispatch_post"]
