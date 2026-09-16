"""Human-readable progress reporting for long orchestration runs.

The event journal remains the durable machine-readable source of truth.  This
module deliberately emits only bounded, non-sensitive summaries suitable for a
terminal or append-only text log.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Mapping, TextIO

from .event_compat import canonical_task_event_type


class HumanProgressReporter:
    """Format orchestration progress events for operators.

    ``stream`` may be ``None`` to suppress live terminal output while still
    writing the append-only log file.  The reporter is callable so it can be
    passed directly to :class:`ProjectOrchestrator` as its progress callback.
    """

    def __init__(self, *, stream: TextIO | None, log_path: Path | None = None):
        self._stream = stream
        self.log_path = Path(log_path).expanduser().resolve() if log_path else None
        self._lock = Lock()

    def __call__(self, event_type: str, payload: Mapping[str, Any]) -> None:
        line = self.format_event(event_type, payload)
        if not line:
            return
        timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S%z")
        rendered = f"[{timestamp}] {line}"
        with self._lock:
            if self._stream is not None:
                print(rendered, file=self._stream, flush=True)
            if self.log_path is not None:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with self.log_path.open("a", encoding="utf-8") as handle:
                    handle.write(rendered + "\n")

    @staticmethod
    def format_event(event_type: str, payload: Mapping[str, Any]) -> str:
        return _format_event(event_type, payload)


def _format_event(event_type: str, payload: Mapping[str, Any]) -> str:
    """Format one known progress event; unknown events are intentionally silent."""

    event_type = canonical_task_event_type(event_type)
    package_id = str(payload.get("package_id", "")).strip()
    prefix = f"{package_id} " if package_id else ""

    if event_type == "pipeline_started":
        return (
            f"RUN start state={payload.get('state', 'unknown')} "
            f"completed={payload.get('completed', 0)}/{payload.get('total', 0)}"
        )
    if event_type == "pipeline_finished":
        suffix = f" error={payload['error']}" if payload.get("error") else ""
        state = str(payload.get("state", "unknown"))
        if state == "waiting_for_agent":
            return f"RUN suspended state={state}; polling remains active{suffix}"
        if state == "operator_paused":
            return f"RUN paused at scheduled Work Package checkpoint{suffix}"
        return f"RUN stop state={state}{suffix}"
    if event_type == "work_package_directive_applied":
        action = "enabled" if payload.get("enabled") else "removed"
        return (
            f"{prefix}future directive {payload.get('kind', 'unknown')} "
            f"{action} — {_bounded_text(payload.get('reason') or payload.get('summary'))}"
        ).rstrip(" —")
    if event_type == "work_package_directive_rejected":
        return (
            f"{prefix}future directive {payload.get('kind', 'unknown')} "
            f"rejected — {_bounded_text(payload.get('error'))}"
        )
    if event_type == "work_package_pause_before_start_reached":
        return (
            f"{prefix}scheduled pause reached before stage="
            f"{payload.get('stage', 'prepare')} — "
            f"{_bounded_text(payload.get('reason'))}"
        ).rstrip(" —")
    if event_type == "work_package_pause_before_start_acknowledged":
        return f"{prefix}scheduled pause acknowledged; execution resumed"
    if event_type == "mandatory_decomposition_consumed":
        return (
            f"{prefix}mandatory decomposition completed "
            f"outcome={payload.get('outcome', 'unknown')}"
        )
    if event_type == "package_selected":
        complexity = payload.get("complexity")
        complexity_suffix = (
            f" complexity={complexity}" if complexity is not None else ""
        )
        return (
            f"{prefix}selected stage={payload.get('stage', 'unknown')}"
            f"{complexity_suffix} — {payload.get('title', '')}"
        ).rstrip()
    if event_type == "package_started":
        return f"{prefix}started; clean-start check passed"
    if event_type == "supervisor_started":
        return (
            f"{prefix}Supervisor incident attempt="
            f"{payload.get('attempt', 1)}/{payload.get('max_attempts', '?')} "
            f"agent={payload.get('agent_id', 'unknown')}"
        )
    if event_type == "write_scope_auto_expanded":
        paths = ",".join(str(item) for item in payload.get("added_paths", []))
        mode = " parallel" if payload.get("parallel") else ""
        return (
            f"{prefix}write scope auto-expanded{mode} "
            f"files={len(payload.get('added_paths', []))} "
            f"changed_lines={payload.get('changed_lines', 0)} "
            f"paths={paths or 'none'}"
        )
    if event_type == "write_scope_expanded":
        paths = ",".join(str(item) for item in payload.get("added_paths", []))
        return (
            f"{prefix}write scope approved paths={paths or 'none'} "
            f"next_stage={payload.get('next_stage', 'unknown')}"
        )
    if event_type == "repository_scope_check_reconciled":
        return (
            f"{prefix}stale repository-scope check reconciled "
            f"stage={payload.get('previous_stage', 'unknown')}->"
            f"{payload.get('next_stage', 'unknown')} "
            f"automatic={str(bool(payload.get('automatic'))).lower()}"
        )
    if event_type == "decomposition_requested":
        return (
            f"{prefix}decompose requested origin="
            f"{payload.get('origin_stage', 'unknown')} "
            f"complexity={payload.get('complexity', 'unknown')}"
        )
    if event_type == "decomposition_rejected":
        return (
            f"{prefix}decompose agent={payload.get('agent_id', 'unknown')} "
            f"rejected — {_bounded_text(payload.get('reason'))}"
        )
    if event_type == "decomposition_kept_atomic":
        return (
            f"{prefix}decompose kept atomic agent="
            f"{payload.get('agent_id', 'unknown')} — "
            f"{_bounded_text(payload.get('reason'))}"
        )
    if event_type == "package_decomposed":
        shards = ",".join(str(item) for item in payload.get("shards", []))
        return (
            f"{prefix}decomposed shards={payload.get('shard_count', 0)} "
            f"aggregate_stage={payload.get('aggregate_stage', 'unknown')} "
            f"agent={payload.get('agent_id', 'unknown')} "
            f"children={shards or 'none'}"
        )
    if event_type == "parallel_wave_started":
        packages = ",".join(str(item) for item in payload.get("packages", []))
        agents = ",".join(str(item) for item in payload.get("agents", []))
        return (
            f"PARALLEL wave={payload.get('wave_id', 'unknown')} started "
            f"packages={packages or 'none'} agents={agents or 'none'}"
        )
    if event_type == "parallel_wave_finished":
        packages = ",".join(str(item) for item in payload.get("packages", []))
        return (
            f"PARALLEL wave={payload.get('wave_id', 'unknown')} finished "
            f"packages={packages or 'none'}"
        )
    if event_type == "agents_assigned":
        return (
            f"{prefix}agents implement={payload.get('implementer') or 'none'} "
            f"review={payload.get('reviewer') or 'none'} "
            f"final={payload.get('final_reviewer') or 'none'} "
            f"complexity={payload.get('complexity', 'unknown')}"
        )
    if event_type == "provider_promotion_used":
        expires = _format_local_datetime(payload.get("expires_at")) or "unknown"
        mode = "fallback-only" if payload.get("fallback_only", True) else "immediate"
        return (
            f"{prefix}temporary promotion agent={payload.get('provider_id', 'unknown')} "
            f"capability={payload.get('capability', 'unknown')} "
            f"ceiling={payload.get('base_max_complexity', '?')}->"
            f"{payload.get('promoted_max_complexity', '?')} "
            f"complexity={payload.get('task_complexity', '?')} "
            f"mode={mode} expires={expires}"
        )
    if event_type == "provider_health_repaired":
        return (
            f"{prefix}agent={payload.get('agent_id', 'unknown')} stale provider "
            f"block cleared reason={payload.get('previous_reason', 'unknown')}"
        )
    if event_type == "agent_availability_recovered":
        return (
            f"agent={payload.get('provider_id', 'unknown')} transient status "
            f"cleared previous={payload.get('previous_availability', 'unknown')}"
        )
    if event_type == "fixer_independence_relaxed":
        return (
            f"{prefix}fix_review selected agent={payload.get('agent_id', 'unknown')} "
            f"policy={payload.get('policy', 'relaxed')}"
        )
    if event_type == "agents_rebalanced":
        return (
            f"{prefix}agents rebalanced implement="
            f"{payload.get('implementer') or 'none'} "
            f"review={payload.get('reviewer') or 'none'} "
            f"fix={payload.get('last_fixer') or 'none'} "
            f"final={payload.get('final_reviewer') or 'none'}"
        )
    if event_type in {"execution_policy_updated", "agent_preferences_updated"}:
        agents = ", ".join(
            f"{role}={' > '.join(str(item) for item in providers)}"
            for role, providers in sorted(
                (
                    payload.get("agent_preferences")
                    or payload.get("preferences")
                    or {}
                ).items()
            )
        ) or "automatic"
        skills = ", ".join(
            f"{role}={'+'.join(str(item) for item in values)}"
            for role, values in sorted(
                (payload.get("skill_preferences") or {}).items()
            )
        ) or "defaults"
        return f"{prefix}execution policy updated agents=[{agents}] skills=[{skills}]"
    if event_type == "package_requeued":
        return (
            f"{prefix}requeued stage={payload.get('stage', 'unknown')} "
            f"reason={payload.get('reason', 'waiting_for_agent')}; "
            "trying disjoint ready work"
        )
    if event_type == "agent_attempt_started":
        model = str(payload.get("model", "")).strip()
        model_suffix = f" model={model}" if model else ""
        capability = str(payload.get("capability", "agent"))
        stage = str(payload.get("stage") or capability)
        capability_suffix = (
            f" capability={capability}" if stage != capability else ""
        )
        return (
            f"{prefix}{stage} agent="
            f"{payload.get('agent_id', 'unknown')}{model_suffix}"
            f"{capability_suffix} started attempt={payload.get('attempt', 1)}"
        )
    if event_type == "agent_heartbeat":
        elapsed = _format_duration(payload.get("elapsed_seconds"))
        process_state = str(payload.get("process_state", "unknown"))
        pid = payload.get("pid", "?")
        processes = payload.get("process_count", "?")
        semantic_activity = _bounded_text(payload.get("semantic_activity"), limit=180)
        semantic_target = _bounded_text(payload.get("semantic_target"), limit=180)
        progress_state = str(payload.get("progress_state", "")).strip()
        warning = _bounded_text(payload.get("progress_warning"), limit=180)
        activity = semantic_activity or _agent_activity_label(payload)
        target_suffix = f" target={semantic_target}" if semantic_target else ""
        progress_suffix = f" progress={progress_state}" if progress_state else ""
        warning_suffix = f" warning={warning}" if warning else ""
        counters = (
            f" events={int(payload.get('progress_events', 0) or 0)}"
            f" files={int(payload.get('progress_files', 0) or 0)}"
            f" commands={int(payload.get('progress_commands', 0) or 0)}"
            f" tools_done={int(payload.get('progress_completed_tools', 0) or 0)}"
        ) if semantic_activity else ""
        model = str(payload.get("model", "")).strip()
        model_suffix = f" model={model}" if model else ""
        stage = str(payload.get("stage") or "agent")
        last_output_value = payload.get("last_output_age_seconds")
        last_output_suffix = (
            f" last_output={_format_duration(last_output_value)} ago"
            if last_output_value is not None
            else ""
        )
        transport = str(payload.get("transport", "")).strip()
        transport_suffix = f" transport={transport}" if transport else ""
        return (
            f"{prefix}{stage} agent={payload.get('agent_id', 'unknown')}"
            f"{model_suffix} still running elapsed={elapsed} pid={pid} "
            f"state={process_state} processes={processes} activity={activity}"
            f"{target_suffix}{progress_suffix}{counters}{warning_suffix}"
            f"{last_output_suffix}{transport_suffix}"
        )
    if event_type == "agent_waiting":
        delay = _format_duration(payload.get("delay_seconds"))
        unknown = int(payload.get("unknown_deadline_count", 0) or 0)
        unknown_suffix = f" unknown_deadlines={unknown}" if unknown else ""
        excluded = int(payload.get("policy_excluded_count", 0) or 0)
        excluded_suffix = (
            f" complexity_excluded={excluded}" if excluded else ""
        )
        excluded_details = _format_complexity_exclusions(
            payload.get("policy_excluded")
        )
        if excluded_details:
            excluded_suffix += f" [{excluded_details}]"
        retry_suffix = _format_agent_deadline(
            label="next_retry",
            agent_id=payload.get("next_retry_agent_id"),
            model=payload.get("next_retry_agent_model"),
            deadline=payload.get("next_retry_at"),
            reason=payload.get("next_retry_reason"),
        )
        unblock_suffix = _format_agent_deadline(
            label="first_reported_unblock",
            agent_id=payload.get("first_reported_unblock_agent_id"),
            model=payload.get("first_reported_unblock_agent_model"),
            deadline=payload.get("first_reported_unblock_at"),
            reason=payload.get("first_reported_unblock_reason"),
        )
        return (
            f"{prefix}no eligible {payload.get('capability', 'agent')} provider; "
            f"waiting {delay} before poll cycle={payload.get('cycle', 1)} "
            f"candidates={payload.get('candidate_count', 0)}"
            f"{excluded_suffix}{unknown_suffix}{retry_suffix}{unblock_suffix}"
        )
    if event_type == "agent_wait_expired":
        waited = _format_duration(payload.get("waited_seconds"))
        limit = _format_duration(payload.get("limit_seconds"))
        return (
            f"{prefix}{payload.get('capability', 'agent')} provider wait expired "
            f"after {waited} limit={limit}; escalating to HUMAN_REQUIRED"
        )
    if event_type == "agent_polling":
        return (
            f"{prefix}polling {payload.get('capability', 'agent')} providers "
            f"cycle={payload.get('cycle', 0)}"
        )
    if event_type == "provider_cooldown":
        until = (
            _format_local_datetime(payload.get("unavailable_until"))
            or "manual reset"
        )
        return (
            f"{prefix}agent={payload.get('agent_id', 'unknown')} provider "
            f"unavailable reason={payload.get('reason', 'unknown')} until={until}"
        )
    if event_type == "provider_skipped":
        until = _format_local_datetime(payload.get("unavailable_until"))
        suffix = f" until_local={until}" if until else ""
        return (
            f"{prefix}{payload.get('capability', 'agent')} agent="
            f"{payload.get('agent_id', 'unknown')} skipped "
            f"reason={payload.get('reason', 'provider_unavailable')}{suffix}"
        )
    if event_type == "agent_attempt_finished":
        status = str(payload.get("status", "unknown")).upper()
        duration = _format_duration(payload.get("duration_seconds"))
        detail = _bounded_text(payload.get("detail"))
        suffix = f" — {detail}" if detail else ""
        model = str(payload.get("model", "")).strip()
        model_suffix = f" model={model}" if model else ""
        capability = str(payload.get("capability", "agent"))
        stage = str(payload.get("stage") or capability)
        capability_suffix = (
            f" capability={capability}" if stage != capability else ""
        )
        return (
            f"{prefix}{stage} agent="
            f"{payload.get('agent_id', 'unknown')}{model_suffix}"
            f"{capability_suffix} {status} in {duration}{suffix}"
        )
    if event_type == "agent_failover":
        from_model = str(payload.get("from_model", "")).strip()
        to_model = str(payload.get("to_model", "")).strip()
        from_label = str(payload.get("from_agent", "unknown"))
        to_label = str(payload.get("to_agent", "unknown"))
        if from_model:
            from_label += f"[{from_model}]"
        if to_model:
            to_label += f"[{to_model}]"
        return (
            f"{prefix}{payload.get('capability', 'agent')} failover "
            f"{from_label} -> {to_label}"
        )
    if event_type == "stage_advanced":
        return (
            f"{prefix}stage {payload.get('from_stage', 'unknown')} -> "
            f"{payload.get('to_stage', 'unknown')}"
        )
    if event_type == "verification_started":
        return (
            f"{prefix}verify profile={payload.get('profile', 'unknown')} "
            f"commands={payload.get('command_count', 0)} "
            f"attempt={payload.get('attempt', 1)}"
        )
    if event_type == "verification_command_started":
        repository = payload.get("repository_id") or "workspace"
        return f"{prefix}verify [{repository}] $ {payload.get('command', '')}"
    if event_type == "verification_command_finished":
        repository = payload.get("repository_id") or "workspace"
        status = str(payload.get("status", "unknown")).upper()
        duration = _format_duration(payload.get("duration_seconds"))
        excerpt = _bounded_text(payload.get("excerpt"))
        suffix = f" — {excerpt}" if excerpt and status != "PASSED" else ""
        return f"{prefix}verify [{repository}] {status} in {duration}{suffix}"
    if event_type == "verification_finished":
        status = str(payload.get("status", "unknown")).upper()
        return f"{prefix}verification {status}"
    if event_type == "verification_retry":
        return (
            f"{prefix}verification failed; retry "
            f"{payload.get('attempt', 1)}/{payload.get('max_attempts', '?')} "
            f"returns to implement"
        )
    if event_type == "review_result":
        message = (
            f"{prefix}review verdict={payload.get('verdict', 'unknown')} "
            f"findings={payload.get('finding_count', 0)}"
        )
        observations = int(payload.get("observation_count", 0) or 0)
        if observations:
            message += f" observations={observations}"
        return message
    if event_type == "acceptance_evidence_collected":
        criteria = ",".join(str(item) for item in payload.get("criteria", []))
        message = (
            f"{prefix}acceptance evidence collected criteria={criteria or 'none'} "
            f"commands={payload.get('command_count', 0)} "
            f"reviewer={payload.get('reviewer') or 'unknown'}"
        )
        legacy = int(payload.get("legacy_observation_count", 0) or 0)
        if legacy:
            message += f" legacy_observations={legacy}"
        return message
    if event_type == "review_fix_queued":
        return (
            f"{prefix}review fixes queued cycle="
            f"{payload.get('cycle', 0)}/{payload.get('max_cycles', '?')} "
            f"findings={payload.get('finding_count', 0)}"
        )
    if event_type == "review_recovery_queued":
        supervisor = str(payload.get("excluded_supervisor_id", "")).strip()
        exclusion = f" supervisor_excluded={supervisor}" if supervisor else ""
        return (
            f"{prefix}deterministic review recovery queued cycle="
            f"{payload.get('cycle', 0)}/{payload.get('max_cycles', '?')} "
            f"findings={payload.get('finding_count', 0)}{exclusion}; "
            "sending the persisted findings directly to a bounded fixer"
        )
    if event_type == "review_recovery_fixer_completed":
        return (
            f"{prefix}deterministic review recovery fixer completed "
            f"cycle={payload.get('cycle', 0)}; returning to regression "
            "verification and independent final review"
        )
    if event_type == "supervisor_incident_superseded":
        return (
            f"{prefix}broad Supervisor recovery superseded by "
            "deterministic exhausted-review repair"
        )
    if event_type == "commit_started":
        repositories = ",".join(str(item) for item in payload.get("repositories", []))
        return f"{prefix}commit transaction started repositories={repositories or 'none'}"
    if event_type == "repository_committed":
        return (
            f"{prefix}committed repository={payload.get('repository_id', 'unknown')} "
            f"commit={payload.get('commit', 'unknown')}"
        )
    if event_type == "repository_skipped":
        return (
            f"{prefix}commit skipped optional repository="
            f"{payload.get('repository_id', 'unknown')} — "
            f"{payload.get('reason', 'unavailable')}"
        )
    if event_type == "package_completed":
        return f"{prefix}COMPLETED transaction={payload.get('transaction_id') or 'none'}"
    if event_type == "human_required":
        reason = _bounded_text(payload.get("reason")) or "manual decision required"
        return f"{prefix}HUMAN_REQUIRED — {reason}"
    if event_type == "resource_pressure":
        return (
            f"RESOURCE pressure filesystem_used="
            f"{payload.get('filesystem_used_percent', 'unknown')}%"
        )
    return ""


def _format_local_datetime(value: Any) -> str:
    """Render an ISO deadline in the operator's local timezone.

    Provider health is persisted in UTC, while the progress log itself is local.
    Converting deadlines here keeps the human-facing timestamps comparable to
    the timestamp at the beginning of each log line. Invalid values remain
    visible rather than being silently discarded.
    """

    text = str(value or "").strip()
    if not text:
        return ""
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        instant = datetime.fromisoformat(normalized)
    except ValueError:
        return text
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.astimezone().strftime("%Y-%m-%d %H:%M:%S%z")


def _format_complexity_exclusions(value: object, *, limit: int = 3) -> str:
    """Render a bounded explanation of providers rejected by complexity policy."""

    if not isinstance(value, list):
        return ""
    labels: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        agent_id = str(item.get("agent_id", "unknown"))
        model = str(item.get("model", "")).strip()
        if model:
            agent_id += f"[{model}]"
        task = item.get("task_complexity", "?")
        maximum = item.get("max_complexity", "?")
        labels.append(f"{agent_id}:{task}>{maximum}")
    if len(labels) > limit:
        remaining = len(labels) - limit
        labels = labels[:limit] + [f"+{remaining} more"]
    return ", ".join(labels)


def _format_agent_deadline(
    *,
    label: str,
    agent_id: Any,
    model: Any,
    deadline: Any,
    reason: Any,
) -> str:
    agent = str(agent_id or "").strip()
    rendered_deadline = _format_local_datetime(deadline)
    if not agent or not rendered_deadline:
        return ""
    model_name = str(model or "").strip()
    rendered_agent = f"{agent}[{model_name}]" if model_name else agent
    suffix = f" {label}={rendered_agent} at_local={rendered_deadline}"
    rendered_reason = str(reason or "").strip()
    if rendered_reason:
        suffix += f" reason={rendered_reason}"
    return suffix


def _agent_activity_label(payload: Mapping[str, Any]) -> str:
    """Describe what changed during the latest heartbeat interval."""

    if bool(payload.get("output_active")):
        return "output"
    if bool(payload.get("input_active")):
        return "input"
    if bool(payload.get("cpu_active")):
        return "compute"
    if bool(payload.get("disk_io_active")) or any(
        int(payload.get(name, 0) or 0) > 0
        for name in ("read_bytes_delta", "write_bytes_delta")
    ):
        return "disk"
    if bool(payload.get("stdio_active")) or bool(payload.get("io_active")) or any(
        int(payload.get(name, 0) or 0) > 0
        for name in ("read_chars_delta", "write_chars_delta")
    ):
        return "stdio"
    return "idle"


def _format_duration(value: Any) -> str:
    try:
        seconds = max(0.0, float(value))
    except (TypeError, ValueError):
        return "?"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, remainder = divmod(int(round(seconds)), 60)
    if minutes < 60:
        return f"{minutes}m{remainder:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _bounded_text(value: Any, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
