"""Human-readable rendering for orchestration CLI commands."""

from __future__ import annotations

from typing import Any, Mapping

from execraft.orchestrate.execution_policy import ROLE_BY_ID


def _print_waiting_context(
    waiting: Mapping[str, Any], *, compact: bool = False
) -> None:
    """Render execution-agent waits and planned Work Package pauses accurately."""

    if str(waiting.get("kind", "")).strip() == "pause_before_start":
        package_id = str(waiting.get("package_id", "")).strip()
        stage = str(waiting.get("stage", "prepare")).strip() or "prepare"
        reason = str(waiting.get("reason", "")).strip()
        if compact:
            suffix = f" reason={reason}" if reason else ""
            print(
                "Planned Work Package pause: "
                f"package={package_id} stage={stage}{suffix}"
            )
            return
        print("Planned Work Package pause:")
        print(f"  Package: {package_id}")
        print(f"  Stage: {stage}")
        print(f"  Reason: {reason or 'No reason recorded'}")
        print(f"  Reached: {waiting.get('reached_at', '')}")
        return

    if compact:
        print(
            "Waiting for agent: "
            f"package={waiting.get('package_id', '')} "
            f"stage={waiting.get('stage', '')} "
            f"next_check={waiting.get('next_check_at', '')}"
        )
        return

    print("Waiting for agent:")
    print(f"  Package: {waiting.get('package_id', '')}")
    print(f"  Stage: {waiting.get('stage', '')}")
    print(f"  Capability: {waiting.get('capability', '')}")
    print(f"  Cycle: {waiting.get('cycle', 0)}")
    print(f"  Next check: {waiting.get('next_check_at', '')}")
    for candidate in waiting.get("candidates", []):
        provider = candidate.get("agent_id") or "unconfigured"
        detail = f"    - {provider}: {candidate.get('reason', 'unavailable')}"
        if candidate.get("available_at"):
            detail += f" until {candidate['available_at']}"
        print(detail)


def _print_human_required_context(
    context: dict[str, Any] | None,
    *,
    project_id: str,
    task_id: str,
    detailed: bool = False,
) -> None:
    """Render an actionable explanation without requiring journal queries."""
    print()
    print("Human action required:")
    if not context:
        print("  Reason: no structured escalation context was recorded")
        print(
            "  Inspect: "
            f"~/.local/state/execraft/journals/{task_id}.json"
        )
        return

    package_id = str(context.get("package_id", "")).strip()
    package_title = str(context.get("package_title", "")).strip()
    package_label = package_id
    if package_title:
        package_label += f" — {package_title}"
    if package_label:
        print(f"  Package: {package_label}")
    if context.get("stage"):
        print(f"  Stage: {context['stage']}")
    print(f"  Reason: {context.get('reason') or 'manual decision required'}")

    agent = context.get("agent")
    if isinstance(agent, dict) and agent.get("id"):
        agent_label = str(agent["id"])
        if agent.get("model"):
            agent_label += f" [{agent['model']}]"
        if agent.get("capability"):
            agent_label += f" capability={agent['capability']}"
        print(f"  Agent: {agent_label}")

    artifact = context.get("artifact")
    if isinstance(artifact, dict) and artifact.get("path"):
        print(f"  Artifact: {artifact['path']}")
        digest = str(artifact.get("sha256", "")).strip()
        size = artifact.get("size_bytes")
        if detailed and (digest or size):
            details = []
            if digest:
                details.append(f"sha256={digest}")
            if size is not None:
                details.append(f"size={size} bytes")
            print("  Integrity: " + ", ".join(details))

    evidence = [str(item) for item in context.get("evidence", []) if str(item).strip()]
    if evidence:
        print("  Evidence:")
        visible = evidence if detailed else evidence[:2]
        for item in visible:
            print(f"    - {item}")
        if not detailed and len(evidence) > len(visible):
            print(
                f"    - ... {len(evidence) - len(visible)} more; "
                f"run 'execraft orchestrate explain --project {project_id} "
                f"--task-id {task_id}'"
            )

    if detailed:
        attempted = [
            str(item)
            for item in context.get("attempted_resolutions", [])
            if str(item).strip()
        ]
        if attempted:
            print("  Attempted resolutions:")
            for item in attempted:
                print(f"    - {item}")
        options = [
            str(item)
            for item in context.get("bounded_options", [])
            if str(item).strip()
        ]
        if options:
            print("  Available decisions:")
            for item in options:
                print(f"    - {item}")
        if context.get("impact"):
            print(f"  Impact: {context['impact']}")
        if context.get("timestamp"):
            print(
                f"  Event: sequence={context.get('sequence')} "
                f"timestamp={context['timestamp']}"
            )

    recommended = str(context.get("recommended_decision", "")).strip()
    if recommended:
        print(f"  Recommended action: {recommended}")
    if isinstance(artifact, dict) and artifact.get("path"):
        print(f"  Inspect output: jq . '{artifact['path']}'")
    print("  Resume after resolving:")
    print(
        "    execraft orchestrate transition "
        f"--project {project_id} --task-id {task_id} --to-state running"
    )
    print(
        "    execraft orchestrate run "
        f"--project {project_id} --task-id {task_id}"
    )


def _print_supervisor_human_decision(
    orchestrator: Any,
    *,
    project_id: str,
    task_id: str,
) -> bool:
    """Render the Supervisor digest and bounded choices at a terminal pause."""

    report = orchestrator.supervisor_status_report()
    incident = report.get("incident") or {}
    question = incident.get("human_question") or {}
    if not isinstance(question, dict) or not question.get("question"):
        return False

    print()
    print("Supervisor digest — human decision required:")
    if incident.get("package_id"):
        print(f"  Package: {incident['package_id']}")
    if incident.get("classification"):
        print(f"  Class: {incident['classification']}")
    if incident.get("summary"):
        print(f"  Summary: {incident['summary']}")
    actions = [
        str(item).strip()
        for item in incident.get("actions_taken", [])
        if str(item).strip()
    ]
    if actions:
        print("  Supervisor checks:")
        for item in actions:
            print(f"    - {item}")
    print(f"  Decision: {question['question']}")
    if question.get("context"):
        print(f"  Problem: {question['context']}")
    recommended = str(question.get("recommended_option", "")).strip()
    print("  Possible solutions:")
    for option in question.get("options", []):
        if not isinstance(option, dict):
            continue
        option_id = str(option.get("id", "")).strip()
        marker = " (recommended)" if option_id and option_id == recommended else ""
        consequence = (
            f" — {option.get('consequence')}"
            if option.get("consequence")
            else ""
        )
        print(f"    - {option_id}: {option.get('label')}{marker}{consequence}")
    print(
        "  Answer with: execraft orchestrate supervisor "
        f"--project {project_id} --task-id {task_id} "
        "--answer <option-id> [--message 'guidance']"
    )
    return True


def _print_execution_policy_report(
    package_id: str, report: Mapping[str, Any], *, apply_to_shards: bool
) -> None:
    """Render one stable execution-policy update report."""

    print(f"Execution policy updated for {package_id}")
    agents_report = report.get("agent_preferences", {})
    skills_report = report.get("skill_preferences", {})
    for role in ROLE_BY_ID:
        agents = agents_report.get(role, [])
        skills = skills_report.get(role, [])
        if agents or skills:
            print(
                f"  {role}: agents={' > '.join(agents) or 'automatic'}; "
                f"skills={', '.join(skills) or 'defaults'}"
            )
    if not agents_report and not skills_report:
        print("  automatic agents and canonical role skills")
    if apply_to_shards:
        print("Applied to: " + ", ".join(report.get("affected_packages", [])))
