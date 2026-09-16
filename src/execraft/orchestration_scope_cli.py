"""CLI presentation and mutations for explicit orchestration scope handling."""

from __future__ import annotations

import argparse
import json
from typing import Any

from execraft.workspace.task_git import TaskGitError


def run_scope_command(
    args: argparse.Namespace,
    orchestrator: Any,
    *,
    task_id: str,
) -> int:
    """Execute ``execraft orchestrate scope`` without bloating the root CLI module."""

    if not args.package_id:
        raise TaskGitError("orchestrate scope requires --package-id")
    if args.accept_scope and args.reconcile_scope:
        raise TaskGitError(
            "orchestrate scope accepts only one of --accept-scope or --reconcile-scope"
        )
    if args.reconcile_scope:
        report = orchestrator.reconcile_resolved_scope_check(args.package_id)
        print(f"Reconciled repository-scope check for {args.package_id}")
        print(f"Stage: {report.get('previous_stage')} -> {report.get('next_stage')}")
        print("Project state: running")
        return 0
    if args.accept_scope:
        expected_candidates = _expected_candidates(args)
        report = orchestrator.approve_declared_write_scope(
            args.package_id,
            expected_candidates=expected_candidates,
        )
        if getattr(args, "json", False):
            print(json.dumps(report, indent=2, sort_keys=True))
            return 0
        if report.get("reconciled"):
            print(f"Reconciled resolved repository-scope check for {args.package_id}")
        else:
            print(f"Approved write-scope expansion for {args.package_id}")
            print("Added paths:")
            for path in report.get("added_paths", []):
                print(f"  - {path}")
        print(f"Stage: {report.get('previous_stage')} -> {report.get('next_stage')}")
        print("Project state: running")
        return 0

    report = orchestrator.declared_write_scope_report(args.package_id)
    if getattr(args, "json", False):
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    _print_scope_report(args, orchestrator, report, task_id=task_id)
    violations = report.get("violations", [])
    auto_recovery = report.get("auto_recovery", {})
    if violations and not auto_recovery.get("can_recover"):
        return 1
    return 0


def _expected_candidates(args: argparse.Namespace) -> list[str] | None:
    expected_raw = str(getattr(args, "expected_scope_json", "") or "").strip()
    if not expected_raw:
        return None
    try:
        decoded = json.loads(expected_raw)
    except json.JSONDecodeError as exc:
        raise TaskGitError("--expected-scope-json must be valid JSON") from exc
    if not isinstance(decoded, list) or not all(
        isinstance(item, str) and item.strip() for item in decoded
    ):
        raise TaskGitError(
            "--expected-scope-json must be a JSON list of non-empty strings"
        )
    return [str(item).strip() for item in decoded]


def _print_scope_report(
    args: argparse.Namespace,
    orchestrator: Any,
    report: dict[str, Any],
    *,
    task_id: str,
) -> None:
    print(f"Package: {report['package_id']}")
    print(f"Stage: {report['stage']}")
    print(f"Parent: {report.get('parent_id') or 'none'}")
    print("Declared write scope:")
    for path in report.get("write_scope", []):
        print(f"  - {path}")
    violations = report.get("violations", [])
    print(f"Violations: {len(violations)}")
    for path in violations:
        print(f"  - {path}")
    assessments = {
        str(item.get("path", "")): item
        for item in report.get("assessments", [])
        if isinstance(item, dict)
    }
    if assessments:
        print("Classification:")
        for path in violations:
            item = assessments.get(path, {})
            category = item.get("category", "unknown")
            automatic = "auto-expandable" if item.get("auto_expandable") else "manual"
            lines = item.get("changed_lines", 0)
            print(f"  - {path}: {category}, {automatic}, changed_lines={lines}")
    workspace_scope = report.get("workspace_scope", {})
    workspace_candidates = [
        item
        for item in workspace_scope.get("candidates", [])
        if isinstance(item, dict)
    ]
    if workspace_candidates:
        print(f"Unowned workspace changes: {len(workspace_candidates)}")
        for item in workspace_candidates:
            print(
                "  - "
                f"{item.get('path', '')}: {item.get('relationship', 'unknown')}"
            )
    auto_recovery = report.get("auto_recovery", {})
    if auto_recovery:
        enabled = bool(auto_recovery.get("can_recover"))
        print("Automatic recovery: " + ("available" if enabled else "not available"))
        if auto_recovery.get("mode"):
            print(f"Recovery mode: {auto_recovery.get('mode')}")
        if auto_recovery.get("reason"):
            print(f"Recovery reason: {auto_recovery.get('reason')}")
        if auto_recovery.get("cleanup_paths"):
            print("Cleanup candidates:")
            for path in auto_recovery.get("cleanup_paths", []):
                print(f"  - {path}")
        if auto_recovery.get("candidate_repositories"):
            print("Candidate repositories:")
            for repository_id in auto_recovery.get("candidate_repositories", []):
                print(f"  - {repository_id}")
        if auto_recovery.get("attempts") is not None:
            print(
                "Recovery attempts: "
                f"{auto_recovery.get('attempts', 0)}/"
                f"{auto_recovery.get('max_attempts', 0)}"
            )
    if violations and not auto_recovery.get("can_recover"):
        print("Approve after inspecting the diff:")
        print(
            "  execraft orchestrate scope "
            f"--project {args.project_id} --task-id {task_id} "
            f"--package-id {args.package_id} --accept-scope"
        )
        return
    if auto_recovery.get("can_recover"):
        print(
            "Run / Resume will invoke automatic workspace recovery. "
            "The selected fixer repairs the delta; the orchestrator then "
            "revalidates and commits the owned repositories atomically."
        )
        print(
            "  execraft orchestrate run "
            f"--project {args.project_id} --task-id {task_id}"
        )
        return
    reconciliation = report.get("reconciliation", {})
    if reconciliation.get("can_reconcile"):
        authorized_dirty = reconciliation.get("authorized_dirty_paths") or {}
        if authorized_dirty:
            print(
                "Current dirty paths are fully covered by the package scope "
                "and may proceed to commit:"
            )
            for repository_id, paths in sorted(authorized_dirty.items()):
                for path in paths:
                    print(f"  - {repository_id}:{path}")
        print("No current violations remain. Reconcile the stale check with:")
        print(
            "  execraft orchestrate scope "
            f"--project {args.project_id} --task-id {task_id} "
            f"--package-id {args.package_id} --reconcile-scope"
        )
    elif orchestrator.state.value == "human_required" and not violations:
        print(
            "No current violations remain, but the active human-required "
            f"condition is not reconcilable: {reconciliation.get('reason', 'unknown reason')}"
        )
