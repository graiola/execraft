"""CLI helpers for explicit irreversible project/task removal."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from execraft.permanent_removal import PermanentRemovalService
from execraft.removal_models import PermanentRemovalError, RemovalPlan


def _confirm_exact_id(*, expected: str, assume_yes: bool) -> None:
    """Require typing the exact target ID unless automation explicitly passes --yes."""

    if assume_yes:
        return
    if not sys.stdin.isatty():
        raise PermanentRemovalError(
            "non-interactive permanent deletion requires --yes; use --dry-run --json first"
        )
    answer = input(
        f"Permanent deletion cannot be undone. Type {expected!r} to confirm: "
    ).strip()
    if answer != expected:
        raise PermanentRemovalError("permanent deletion confirmation did not match the target ID")


def _print_plan(plan: RemovalPlan) -> None:
    print(f"Permanent removal plan for {plan.kind} {plan.item_id}:")
    for path in plan.paths:
        print(f"  remove: {path}")
    for branch in plan.branch_removals:
        print(
            f"  delete Execraft-created branch: {branch.repository_id}:{branch.branch} "
            f"({branch.source_path})"
        )
    for item in plan.preserved:
        print(f"  preserve: {item}")


def run_task_removal(args: Any, *, control_root: Path, state_root: Path) -> int:
    """Execute ``execraft task delete`` without growing the legacy command handler."""

    task_id = str(getattr(args, "task_id", "") or "").strip()
    project_id = str(getattr(args, "project_id", "") or "").strip()
    if not task_id:
        raise PermanentRemovalError("task delete requires an explicit TASK_ID")
    if not project_id:
        raise PermanentRemovalError("task delete requires --project PROJECT_ID")
    service = PermanentRemovalService(
        control_root,
        state_root,
        archive_root=getattr(args, "archive_root", None),
    )
    plan = service.preview_task(
        project_id,
        task_id,
        delete_branches=bool(getattr(args, "delete_branches", False)),
    )
    if bool(getattr(args, "dry_run", False)):
        payload = plan.as_mapping()
        payload["dry_run"] = True
        if bool(getattr(args, "json", False)):
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            _print_plan(plan)
        return 0
    _confirm_exact_id(expected=task_id, assume_yes=bool(getattr(args, "yes", False)))
    result = service.remove_task(
        project_id,
        task_id,
        delete_branches=bool(getattr(args, "delete_branches", False)),
    )
    if bool(getattr(args, "json", False)):
        print(json.dumps(result.as_mapping(), indent=2, sort_keys=True))
    else:
        print(f"Permanently removed task {project_id}/{task_id}")
        print(f"Removed {len(result.removed_paths)} Execraft path(s)")
        if result.removed_branches:
            print("Removed branches: " + ", ".join(result.removed_branches))
    return 0


def run_project_removal(args: Any, *, control_root: Path, state_root: Path) -> int:
    """Execute ``execraft project delete`` while preserving external source data."""

    project_id = str(getattr(args, "project_id", "") or "").strip()
    if not project_id:
        raise PermanentRemovalError("project delete requires an explicit PROJECT_ID")
    service = PermanentRemovalService(
        control_root,
        state_root,
        archive_root=getattr(args, "archive_root", None),
    )
    plan = service.preview_project(
        project_id,
        delete_branches=bool(getattr(args, "delete_branches", False)),
    )
    if bool(getattr(args, "dry_run", False)):
        payload = plan.as_mapping()
        payload["dry_run"] = True
        if bool(getattr(args, "json", False)):
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            _print_plan(plan)
        return 0
    _confirm_exact_id(expected=project_id, assume_yes=bool(getattr(args, "yes", False)))
    result = service.remove_project(
        project_id,
        delete_branches=bool(getattr(args, "delete_branches", False)),
    )
    if bool(getattr(args, "json", False)):
        print(json.dumps(result.as_mapping(), indent=2, sort_keys=True))
    else:
        print(f"Permanently removed project {project_id} from Execraft")
        print(f"Removed {len(result.removed_paths)} Execraft path(s)")
        if result.removed_branches:
            print("Removed branches: " + ", ".join(result.removed_branches))
        for item in result.preserved:
            print(f"Preserved: {item}")
    return 0
