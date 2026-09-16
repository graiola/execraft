"""Orchestration CLI command behavior.

The top-level CLI owns composition; this module owns the orchestration command's
status/reporting/action flow so configuration and unrelated commands do not share
one giant dispatcher.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Mapping

from execraft.control_plane import xdg_state_home
from execraft.cli_orchestration_policy import (
    execution_policy_from_args as _execution_policy_from_args,
    legacy_agent_preferences_from_args as _legacy_agent_preferences_from_args,
)
from execraft.cli_orchestration_output import (
    _print_execution_policy_report,
    _print_human_required_context,
    _print_supervisor_human_decision,
    _print_waiting_context,
)
from execraft.orchestrate import (
    DaemonConfig,
    ProjectOrchestrator,
    TaskExecutionState,
    load_plan_graph_file,
    run_until_terminal,
)
from execraft.orchestrate.directives import PAUSE_FOR_REPOSITORY_SYNC
from execraft.orchestrate.operator_risk_cli import run_accept_risk_cli
from execraft.project import resolve_current_project
from execraft.repository_sync.coordinator import (
    RepositorySyncCoordinationError,
    RepositorySyncCoordinator,
)
from execraft.workspace.task_git import (
    TaskGitError,
    load_manifest,
    project_task_directory,
    repository_root,
)


def _can_auto_resume_terminal_state(orchestrator: Any) -> bool:
    """Use the authoritative predicate with compatibility for test/plugins.

    Older injected orchestrator doubles predate ``can_auto_resume_terminal_state``.
    Keep the CLI tolerant while production always uses the single authoritative
    method on :class:`ProjectOrchestrator`.
    """

    predicate = getattr(orchestrator, "can_auto_resume_terminal_state", None)
    if callable(predicate):
        return bool(predicate())
    legacy = getattr(orchestrator, "can_auto_resume_human_required", None)
    return bool(callable(legacy) and legacy())


def _coordinate_paused_repository_sync(
    args: argparse.Namespace,
    orchestrator: ProjectOrchestrator,
    task_id: str,
) -> dict[str, Any] | None:
    """Recover/apply one card synchronization request outside driver locks.

    ``run_until_terminal`` releases both driver/run locks before this helper is
    called. The coordinator first repairs the narrow crash window between a
    task-definition publication and installation of the requested post-sync pause
    policy, then converts a newly reached safe-boundary request into the canonical
    synchronization Work Package.
    """

    if not callable(
        getattr(orchestrator, "acknowledge_repository_sync_boundary", None)
    ):
        # Lightweight test/embedding orchestrators may implement only the
        # historical run/status protocol. They cannot own a Pause & Sync
        # boundary, so no control-plane coordination is applicable.
        return None

    root = repository_root()
    project = resolve_current_project(root, project_id=args.project_id)
    manifest = load_manifest(root, task_id)
    state_root = (
        args.state_dir.expanduser().resolve()
        if args.state_dir
        else xdg_state_home()
    )
    coordinator = RepositorySyncCoordinator(
        control_root=root,
        state_root=state_root,
        project=project,
        manifest=manifest,
        dossier=project_task_directory(root, project.id, task_id),
    )
    try:
        recovered = coordinator.recover_execution_policies(orchestrator)
    except RepositorySyncCoordinationError as exc:
        raise TaskGitError(
            f"cannot recover queued Pause & Sync execution policy: {exc}"
        ) from exc
    if recovered:
        orchestrator.load_state()

    report = orchestrator.status_report()
    waiting = report.get("waiting") or {}
    if (
        report.get("state") != "operator_paused"
        or not isinstance(waiting, dict)
        or waiting.get("kind") != PAUSE_FOR_REPOSITORY_SYNC
    ):
        return None
    try:
        result = coordinator.apply_waiting(waiting)
        orchestrator.load_state()
        coordinator.install_execution_policy(result, orchestrator)
    except RepositorySyncCoordinationError as exc:
        raise TaskGitError(
            f"cannot apply queued Pause & Sync request: {exc}"
        ) from exc
    payload = result.as_mapping()
    print(
        "Pause & Sync boundary prepared: "
        f"{result.insertion.sync_package_id} ({result.request.mode} "
        f"{result.request.package_id}, revision {result.replan.revision})"
    )
    if not result.request.auto_resume:
        print(
            "The driver will pause again after the synchronization Work Package "
            "completes."
        )
    return payload


def _handle_init(args: argparse.Namespace, orchestrator: Any, task_id: str, agents: list[str]) -> int:
    if not args.plan_file:
        raise TaskGitError("orchestrate init requires --plan-file")
    graph, report = load_plan_graph_file(args.plan_file)
    report = orchestrator.initialize_graph(graph, report)
    state = orchestrator.status_report()
    print(f"Task {task_id} initialized for project {args.project_id}")
    print(f"State: {state['state']}")
    print(f"Packages: {state['total_packages']}")
    print(f"Agents: {', '.join(agents)}")
    if report.has_errors():
        print(f"Warnings/errors: {report.summary()}", file=sys.stderr)
        return 1
    return 0


def _handle_context(args: argparse.Namespace, orchestrator: Any) -> int:
    if not args.package_id:
        raise TaskGitError("orchestrate context requires --package-id")
    print(
        json.dumps(
            orchestrator.context_report(
                str(args.package_id), rebuild=bool(args.rebuild_context)
            ),
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def _handle_usage(args: argparse.Namespace, orchestrator: Any) -> int:
    print(
        json.dumps(
            orchestrator.token_usage_report(package_id=str(args.package_id or "")),
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def _handle_sync(args: argparse.Namespace, orchestrator: Any) -> int:
    if not args.package_id:
        raise TaskGitError("orchestrate sync requires --package-id")
    mutations = int(bool(args.rollback)) + int(bool(args.accept_resolution))
    if mutations > 1 or (mutations and args.refresh):
        raise TaskGitError(
            "orchestrate sync accepts only one of --refresh, --rollback, or --accept-resolution"
        )
    if args.rollback:
        payload = orchestrator.rollback_repository_sync(str(args.package_id))
    elif args.accept_resolution:
        payload = orchestrator.accept_repository_sync_resolution(str(args.package_id))
    else:
        payload = orchestrator.repository_sync_report(
            str(args.package_id), refresh=bool(args.refresh)
        )
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def _handle_trace(args: argparse.Namespace, orchestrator: Any, task_id: str) -> int:
    if not 1 <= int(args.limit) <= 500:
        raise TaskGitError("orchestrate trace --limit must be between 1 and 500")
    history = orchestrator.invocation_history(
        package_id=str(args.package_id or ""),
        limit=int(args.limit),
        include_handoff=bool(args.include_handoff),
    )
    print(
        json.dumps(
            {
                "project_id": args.project_id,
                "task_id": task_id,
                "package_id": str(args.package_id or ""),
                "count": len(history),
                "invocations": history,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def _handle_status(args: argparse.Namespace, orchestrator: Any, task_id: str) -> int:
    report = orchestrator.status_report()
    print(f"Project: {args.project_id}")
    print(f"Task: {task_id}")
    print(f"State: {report['state']}")
    print(f"Total packages: {report['total_packages']}")
    print(f"Completed: {report['completed_packages']}")
    print(f"Pending: {report['pending_packages']}")

    current = report["current_package"]
    if current:
        print(
            "Current package: "
            f"{current['id']} — {current['title']} "
            f"[stage={current['stage']}, status={current['status']}]"
        )
    else:
        print("Current package: none")

    next_package = report["next_package"]
    if next_package:
        print(f"Next package: {next_package['id']} — {next_package['title']}")
    else:
        print("Next package: none")

    paused_packages = report.get("paused_packages", [])
    print(f"Operator paused: {len(paused_packages)}")
    for package in paused_packages:
        reason = package.get("operator_pause_reason") or "no reason recorded"
        print(f"  - {package['id']} — {reason}")

    ready_packages = report["ready_packages"]
    print(f"Ready queue: {len(ready_packages)}")
    for index, package in enumerate(ready_packages, start=1):
        print(
            f"  {index}. {package['id']} — {package['title']} "
            f"[priority={package['priority']}, risk={package['risk']}, "
            f"verification={package['verification_profile']}]"
        )

    scheduler = report.get("scheduler") or {}
    parallel_wave = scheduler.get("parallel_wave") if isinstance(scheduler, dict) else None
    if isinstance(parallel_wave, dict) and parallel_wave:
        print("Active parallel wave:")
        print(f"  Wave: {parallel_wave.get('wave_id', '')}")
        print("  Packages: " + ", ".join(str(item) for item in parallel_wave.get("package_ids", [])))
        print("  Agents: " + ", ".join(str(item) for item in parallel_wave.get("agents", [])))

    waiting = report.get("waiting") or {}
    if waiting:
        _print_waiting_context(waiting)
    _print_unavailable_providers(report)
    _print_blocked_packages(report, ready_packages)
    _print_supervisor_status(report)

    if report["human_required"]:
        _print_human_required_context(
            report["human_required"], project_id=args.project_id, task_id=task_id
        )
    if report["error"]:
        print(f"Error: {report['error']}", file=sys.stderr)
    return 1 if report["state"] in {"failed", "cancelled"} else 0


def _print_unavailable_providers(report: Mapping[str, Any]) -> None:
    unavailable = [
        item
        for item in report.get("provider_health", [])
        if item.get("status") != "available"
    ]
    if not unavailable:
        return
    print("Unavailable providers:")
    for item in unavailable:
        label = f"  - {item.get('provider_id', 'unknown')}: {item.get('reason', 'unavailable')}"
        if item.get("unavailable_until"):
            label += f" until {item['unavailable_until']}"
        print(label)


def _print_blocked_packages(
    report: Mapping[str, Any], ready_packages: list[Mapping[str, Any]]
) -> None:
    blocked = report["blocked_packages"]
    print(f"Blocked packages: {len(blocked)}")
    if ready_packages or not blocked:
        return
    for package in blocked[:10]:
        missing = ", ".join(package["missing_dependencies"]) or "unknown"
        print(f"  - {package['id']} — waiting for: {missing}")
    if len(blocked) > 10:
        print(f"  ... and {len(blocked) - 10} more")


def _print_supervisor_status(report: Mapping[str, Any]) -> None:
    supervisor = report.get("supervisor") or {}
    if not isinstance(supervisor, dict) or not supervisor.get("enabled"):
        return
    print(
        "Supervisor: "
        + (
            str(supervisor.get("agent_id"))
            if supervisor.get("available")
            else "enabled but unavailable"
        )
    )
    incident = supervisor.get("incident") or {}
    if not isinstance(incident, dict) or not incident:
        return
    print(
        "  Incident: "
        f"{incident.get('incident_id', '')} "
        f"[{incident.get('classification', '')}, {incident.get('status', '')}]"
    )
    if incident.get("summary"):
        print(f"  Summary: {incident.get('summary')}")
    question = incident.get("human_question") or {}
    if isinstance(question, dict) and question.get("question"):
        print(f"  Question: {question.get('question')}")
        for option in question.get("options", []):
            if isinstance(option, dict):
                print(f"    - {option.get('id')}: {option.get('label')}")


def _handle_explain(args: argparse.Namespace, orchestrator: Any, task_id: str) -> int:
    context = orchestrator.human_required_report()
    if context is None:
        print(f"No active human intervention is required for task {task_id}.")
        return 0
    _print_human_required_context(
        context, project_id=args.project_id, task_id=task_id, detailed=True
    )
    return 0


def _handle_supervisor(args: argparse.Namespace, orchestrator: Any, task_id: str) -> int:
    if args.answer:
        incident = orchestrator.submit_supervisor_answer(args.answer, message=args.message)
        print(f"Supervisor answer recorded for incident {incident.get('incident_id', '')}")
        print("Project state: supervising")
        print(
            "Resume with: execraft orchestrate run "
            f"--project {args.project_id} --task-id {task_id}"
        )
        return 0

    report = orchestrator.supervisor_status_report()
    print(f"Supervisor enabled: {str(bool(report.get('enabled'))).lower()}")
    configured = report.get("configured_agents") or (
        [report["configured_agent"]] if report.get("configured_agent") else []
    )
    print("Configured agents: " + (" -> ".join(configured) if configured else "automatic"))
    print(f"Resolved agent: {report.get('agent_id') or 'unavailable'}")
    incident = report.get("incident") or {}
    deterministic = report.get("deterministic_recovery") or {}
    if not incident and not deterministic.get("available"):
        print("Active incident: none")
        return 0

    _print_supervisor_incident(report, incident)
    deterministic_resume = bool(deterministic.get("available"))
    auto_resume = bool(report.get("auto_resume_lost_delegation"))
    if deterministic_resume:
        _print_deterministic_supervisor_resume(args, task_id, deterministic)
    elif auto_resume:
        _print_automatic_supervisor_resume(args, task_id, report)
    else:
        _print_supervisor_question(args, task_id, incident)
    return 0


def _print_supervisor_incident(report: Mapping[str, Any], incident: Mapping[str, Any]) -> None:
    print(f"Incident: {incident.get('incident_id', '')}")
    print(f"Package: {incident.get('package_id', '')}")
    print(f"Class: {incident.get('classification', '')}")
    print(f"Status: {incident.get('status', '')}")
    print(
        "Attempts: "
        f"{incident.get('attempts', 0)}/"
        f"{report.get('policy', {}).get('max_attempts_per_incident', 0)}"
    )
    if incident.get("summary"):
        print(f"Summary: {incident.get('summary')}")


def _print_deterministic_supervisor_resume(
    args: argparse.Namespace, task_id: str, deterministic: Mapping[str, Any]
) -> None:
    print(
        "Deterministic recovery: available "
        f"(direct fix_review, cycle {deterministic.get('cycle', 0)}/"
        f"{deterministic.get('max_cycles', 0)}, "
        f"findings={deterministic.get('finding_count', 0)})"
    )
    if deterministic.get("avoid_supervisor_agent"):
        print(
            "Execution-agent policy: the configured Supervisor is excluded from "
            "this repair campaign."
        )
    print(
        "Resume with: execraft orchestrate run "
        f"--project {args.project_id} --task-id {task_id}"
    )


def _print_automatic_supervisor_resume(
    args: argparse.Namespace, task_id: str, report: Mapping[str, Any]
) -> None:
    count = int(report.get("pending_delegation_count", 0) or 0)
    source = str(report.get("pending_delegation_source", "") or "durable state")
    print(
        "Automatic resume: available "
        f"({count} pending delegation{'s' if count != 1 else ''}, source={source})"
    )
    if report.get("stale_human_question"):
        print(
            "Stale human decision: it was generated by the interrupted "
            "delegation and will be discarded automatically."
        )
    print(
        "Resume with: execraft orchestrate run "
        f"--project {args.project_id} --task-id {task_id}"
    )


def _print_supervisor_question(
    args: argparse.Namespace, task_id: str, incident: Mapping[str, Any]
) -> None:
    question = incident.get("human_question") or {}
    if not isinstance(question, dict) or not question.get("question"):
        return
    print("Human decision requested:")
    print(f"  {question.get('question')}")
    if question.get("context"):
        print(f"  Context: {question.get('context')}")
    for option in question.get("options", []):
        if not isinstance(option, dict):
            continue
        suffix = f" — {option.get('consequence')}" if option.get("consequence") else ""
        print(f"  - {option.get('id')}: {option.get('label')}{suffix}")
    print(
        "Answer with: execraft orchestrate supervisor "
        f"--project {args.project_id} --task-id {task_id} "
        "--answer <option-id> [--message 'guidance']"
    )


def _handle_decompose(args: argparse.Namespace, orchestrator: Any) -> int:
    if not args.package_id:
        raise TaskGitError("orchestrate decompose requires --package-id")
    completed = orchestrator.decompose_package(args.package_id)
    report = orchestrator.status_report()
    package = orchestrator._state_record.plan_graph.package_by_id(args.package_id)
    print(
        f"Package {args.package_id}: decomposition_status="
        f"{package.decomposition_status or 'waiting'} "
        f"shards={len(package.shard_ids)} state={report['state']}"
    )
    return 0 if completed else 1


def _handle_policy(args: argparse.Namespace, orchestrator: Any) -> int:
    if not args.package_id:
        raise TaskGitError(f"orchestrate {args.action} requires --package-id")
    package = orchestrator._state_record.plan_graph.package_by_id(args.package_id)
    if args.action == "prefer":
        agent_preferences = _legacy_agent_preferences_from_args(args, package)
        report = orchestrator.set_agent_preferences(
            args.package_id,
            agent_preferences,
            apply_to_shards=bool(args.apply_to_shards),
        )
    else:
        agent_preferences, skill_preferences, binding_roles = _execution_policy_from_args(
            args, package
        )
        report = orchestrator.set_execution_policy(
            args.package_id,
            agent_preferences=agent_preferences,
            skill_preferences=skill_preferences,
            agent_preference_binding_roles=binding_roles,
            apply_to_shards=bool(args.apply_to_shards),
        )
    _print_execution_policy_report(
        args.package_id, report, apply_to_shards=bool(args.apply_to_shards)
    )
    return 0


def _handle_pause(args: argparse.Namespace, orchestrator: Any) -> int:
    if not args.package_id:
        raise TaskGitError("orchestrate pause requires --package-id")
    report = orchestrator.set_package_pause(
        args.package_id,
        paused=not bool(args.resume),
        reason=args.pause_reason,
        apply_to_shards=bool(args.apply_to_shards),
    )
    action = "Paused" if report["paused"] else "Resumed"
    print(f"{action} Work Package scheduling: {', '.join(report['affected_packages'])}")
    if report["reason"]:
        print(f"Reason: {report['reason']}")
    return 0


def _execution_result_code(
    args: argparse.Namespace,
    orchestrator: Any,
    task_id: str,
    report: Mapping[str, Any],
    complete_finished_orchestration,
) -> int:
    if report["human_required"]:
        _print_human_required_context(
            report["human_required"], project_id=args.project_id, task_id=task_id
        )
    elif report["state"] == "waiting_for_human_decision":
        _print_supervisor_human_decision(
            orchestrator, project_id=args.project_id, task_id=task_id
        )
    completion_ok = True
    if report["state"] == "completed":
        completion_ok = complete_finished_orchestration(
            orchestrator=orchestrator, task_id=task_id
        )
    return (
        0
        if completion_ok
        and report["state"] in {"completed", "waiting_for_human_decision"}
        else 1
    )


def _handle_run(
    args: argparse.Namespace,
    orchestrator: Any,
    task_id: str,
    progress_log_path: Any,
    complete_finished_orchestration,
    run_until_terminal_fn,
) -> int:
    allowed_states = {
        "validating_plan", "running", "waiting_for_agent", "supervising", "operator_paused"
    }
    if _can_auto_resume_terminal_state(orchestrator):
        allowed_states.add(orchestrator.state.value)
    if orchestrator.state.value not in allowed_states:
        print(
            f"Cannot run from state {orchestrator.state.value}; expected "
            "validating_plan, running, waiting_for_agent, supervising, or operator_paused",
            file=sys.stderr,
        )
        return 1
    if progress_log_path is not None and not args.quiet:
        print(f"Progress log: {progress_log_path}", flush=True)
    daemon_config = None if args.no_wait_for_agents else _daemon_config(args)

    while True:
        _coordinate_paused_repository_sync(args, orchestrator, task_id)
        if args.no_wait_for_agents:
            orchestrator.run_pipeline()
        else:
            run_until_terminal_fn(orchestrator, config=daemon_config)
        report = orchestrator.status_report()
        waiting = report.get("waiting") or {}
        if (
            report.get("state") == "operator_paused"
            and isinstance(waiting, dict)
            and waiting.get("kind") == PAUSE_FOR_REPOSITORY_SYNC
        ):
            continue
        break

    print(f"Pipeline finished: {report['state']}")
    if report.get("waiting"):
        _print_waiting_context(report["waiting"], compact=True)
    return _execution_result_code(
        args, orchestrator, task_id, report, complete_finished_orchestration
    )


def _daemon_config(args: argparse.Namespace) -> DaemonConfig:
    return DaemonConfig(
        max_attempts=args.max_attempts,
        initial_backoff_seconds=args.initial_backoff_seconds,
        max_backoff_seconds=args.max_backoff_seconds,
        backoff_multiplier=args.backoff_multiplier,
    )


def _handle_daemon(
    args: argparse.Namespace,
    orchestrator: Any,
    task_id: str,
    progress_log_path: Any,
    complete_finished_orchestration,
    run_until_terminal_fn,
) -> int:
    allowed_states = {
        "validating_plan", "running", "waiting_for_agent", "paused_low_disk",
        "supervising", "operator_paused",
    }
    if _can_auto_resume_terminal_state(orchestrator):
        allowed_states.add(orchestrator.state.value)
    if orchestrator.state.value not in allowed_states:
        print(
            f"Cannot run from state {orchestrator.state.value}; expected "
            "validating_plan, running, waiting_for_agent, paused_low_disk, "
            "supervising, or operator_paused",
            file=sys.stderr,
        )
        return 1
    if progress_log_path is not None and not args.quiet:
        print(f"Progress log: {progress_log_path}", flush=True)
    result = run_until_terminal_fn(orchestrator, config=_daemon_config(args))
    report = orchestrator.status_report()
    print(f"Daemon finished after {result.attempts} attempt(s): {report['state']}")
    if result.exhausted:
        print("Exhausted max-attempts before reaching a terminal state", file=sys.stderr)
        return 1
    return _execution_result_code(
        args, orchestrator, task_id, report, complete_finished_orchestration
    )


def _handle_transition(args: argparse.Namespace, orchestrator: Any) -> int:
    if not args.to_state:
        raise TaskGitError("orchestrate transition requires --to-state")
    new_state = TaskExecutionState(args.to_state)
    orchestrator.transition_to(new_state)
    print(f"Transitioned to {new_state.value}")
    return 0


def run_orchestrate_command(
    args: argparse.Namespace,
    *,
    build_orchestrator,
    complete_finished_orchestration,
    run_until_terminal_fn=run_until_terminal,
) -> int:
    """Run one orchestration CLI action using explicit composition dependencies."""

    orchestrator, task_id, agents, progress_log_path = build_orchestrator(args)
    if args.action == "init":
        return _handle_init(args, orchestrator, task_id, agents)

    orchestrator.load_state()
    if args.action == "context":
        return _handle_context(args, orchestrator)
    if args.action == "usage":
        return _handle_usage(args, orchestrator)
    if args.action == "sync":
        return _handle_sync(args, orchestrator)
    if args.action == "trace":
        return _handle_trace(args, orchestrator, task_id)
    if args.action == "status":
        return _handle_status(args, orchestrator, task_id)
    if args.action == "explain":
        return _handle_explain(args, orchestrator, task_id)
    if args.action == "supervisor":
        return _handle_supervisor(args, orchestrator, task_id)
    if args.action == "accept-risk":
        return run_accept_risk_cli(orchestrator, args, task_id=task_id)
    if args.action == "decompose":
        return _handle_decompose(args, orchestrator)
    if args.action == "scope":
        from execraft.orchestration_scope_cli import run_scope_command
        return run_scope_command(args, orchestrator, task_id=task_id)
    if args.action in {"policy", "prefer"}:
        return _handle_policy(args, orchestrator)
    if args.action == "pause":
        return _handle_pause(args, orchestrator)
    if args.action == "run":
        return _handle_run(
            args,
            orchestrator,
            task_id,
            progress_log_path,
            complete_finished_orchestration,
            run_until_terminal_fn,
        )
    if args.action == "daemon":
        return _handle_daemon(
            args,
            orchestrator,
            task_id,
            progress_log_path,
            complete_finished_orchestration,
            run_until_terminal_fn,
        )
    if args.action == "transition":
        return _handle_transition(args, orchestrator)
    raise TaskGitError(f"unsupported orchestrate action: {args.action}")
