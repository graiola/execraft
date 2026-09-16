"""Argument parsers for task, archive, render, and workspace operations."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def add_workspace_commands(sub: Any) -> None:
    task = sub.add_parser("task", help="Task dossier operations")
    task.add_argument(
        "action",
        choices=["new", "status", "sync-status", "switch", "commit", "review", "replan", "sync-before", "close", "complete", "delete"],
    )
    task.add_argument("task_id", nargs="?")
    task.add_argument("--project", dest="project_id")
    task.add_argument("--title")
    task.add_argument("--brief", default="", help="Initial task intent written into BRIEF.md")
    task.add_argument("--brief-file", type=Path, help="Import an existing BRIEF.md")
    task.add_argument("--plan-file", type=Path, help="Import an existing PLAN.md")
    task.add_argument(
        "--plan-graph-file",
        type=Path,
        help="Import an existing PLAN.graph.yaml",
    )
    task.add_argument("--template", default="standard", help="Task template ID (default: standard)")
    task.add_argument("--dry-run", action="store_true", help="Preview task creation/deletion effects without changing files")
    task.add_argument("--json", action="store_true", help="Print machine-readable task creation output")
    task.add_argument("--branch")
    task.add_argument("--workspace-root", type=Path, default=Path.cwd())
    task.add_argument("--message", "-m")
    task.add_argument("--repositories", nargs="*")
    task.add_argument(
        "--before",
        default="",
        help="Target Work Package before which a repository-sync Work Package is inserted",
    )
    task.add_argument(
        "--sync-id",
        default="",
        help="Explicit package ID for the inserted repository-sync Work Package",
    )
    task.add_argument(
        "--source-branch",
        action="append",
        default=[],
        metavar="REPOSITORY=BRANCH",
        help="Override TASK.yaml base_branch for one synchronized repository",
    )
    task.add_argument(
        "--remote",
        default="origin",
        help="Git remote used by repository synchronization (default: origin)",
    )
    task.add_argument(
        "--conflict-policy",
        choices=["ai_resolve", "human"],
        default="ai_resolve",
        help="How a repository-sync Work Package handles merge conflicts",
    )
    task.add_argument(
        "--request",
        default="",
        help="Natural-language change request for task replan",
    )
    task.add_argument(
        "--request-file",
        type=Path,
        help="Read the task replan change request from a UTF-8 file",
    )
    task.add_argument(
        "--from-current-files",
        action="store_true",
        help="Stage manually edited live BRIEF.md/PLAN.md/PLAN.graph.yaml as the candidate",
    )
    task.add_argument(
        "--candidate",
        default="",
        help="Existing pending replan candidate ID to inspect or apply",
    )
    task.add_argument(
        "--apply",
        action="store_true",
        help="Apply the newly created or --candidate replan revision after validation",
    )
    task.add_argument(
        "--recover",
        action="store_true",
        help="Rollback an interrupted replanning publication transaction",
    )
    task.add_argument(
        "--agent",
        "--provider",
        dest="provider",
        default="",
        help="Planning execution agent used by ai-replan semantic validation (--provider is deprecated)",
    )
    task.add_argument(
        "--allow-structural",
        action="store_true",
        help="Explicitly allow structural-only consistency checks when no AI execution agent is used",
    )
    task.add_argument(
        "--supersede",
        action="append",
        default=[],
        metavar="OLD=NEW",
        help="Map a started package to a NEW replacement ID (repeatable)",
    )
    task.add_argument(
        "--check",
        action="store_true",
        help="Validate task completion and archive prerequisites without changing files",
    )
    task.add_argument(
        "--archive",
        action="store_true",
        help="Create an immutable completion archive before closing the task",
    )
    task.add_argument(
        "--state-dir",
        type=Path,
        help="Override the Execraft state root (default: ~/.local/state/execraft)",
    )
    task.add_argument(
        "--archive-root",
        type=Path,
        help="Override the completion archive root (default: <state-dir>/archives)",
    )
    task.add_argument(
        "--delete-branches",
        action="store_true",
        help="Also delete local task branches that the workspace registry proves Execraft created",
    )
    task.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Confirm irreversible task deletion without an interactive exact-ID prompt",
    )

    archive = sub.add_parser("archive", help="Inspect and verify completed task archives")
    archive.add_argument("action", choices=["list", "show", "verify"])
    archive.add_argument("task_id", nargs="?")
    archive.add_argument("--project", dest="project_id")
    archive.add_argument("--state-dir", type=Path)
    archive.add_argument("--archive-root", type=Path)

    render = sub.add_parser("render", help="Render provider configuration into a workspace shell")
    render.add_argument("project_dir")
    render.add_argument("workspace_root")
    render.add_argument("--task-id")
    render.add_argument("--clean", action="store_true")
    render.add_argument("--force", action="store_true", help="Allow cleaning an unowned target")
    render.add_argument("--policy", help="Policy profile to compile into provider configuration")
    render.add_argument("--dry-run", action="store_true")

    workspace = sub.add_parser("workspace", help="Workspace lifecycle operations")
    workspace.add_argument(
        "action",
        choices=["start", "status", "sync", "run", "verify", "refresh", "destroy", "stop"],
    )
    workspace.add_argument("task_id")
    workspace.add_argument(
        "--source-root",
        type=Path,
        help=(
            "Project checkout root. When supplied to workspace start it is stored "
            "as the local project binding unless --no-bind is used."
        ),
    )
    workspace.add_argument(
        "--no-bind",
        action="store_true",
        help="Use --source-root for this invocation without updating the local project binding",
    )
    workspace.add_argument("--workspace-root", type=Path)
    workspace.add_argument("--repository")
    workspace.add_argument("--reuse-in-place", action="store_true")
    workspace.add_argument("--policy")
    workspace.add_argument(
        "--profile", choices=["cheap", "focused", "integration", "full"], default="focused",
        help="Verification profile used by 'workspace verify'",
    )
    workspace.add_argument(
        "--force",
        action="store_true",
        help=(
            "Dangerous recovery mode: bypass archive, pin, dirty-tree, Git-operation, "
            "and runtime-shutdown policy checks. It cannot bypass an active orchestrator "
            "or ownership integrity. Normal destruction must not use it."
        ),
    )
    workspace.add_argument(
        "--remove-shell",
        action="store_true",
        help="Remove the generated shell after Git worktrees are safely detached",
    )
    workspace.add_argument(
        "--dry-run",
        action="store_true",
        help="Show lifecycle checks and actions without changing runtime or files",
    )
    workspace.add_argument(
        "--state-dir",
        type=Path,
        help="Override the Execraft state root used for activity and archive checks",
    )
    workspace.add_argument(
        "--archive-root",
        type=Path,
        help="Override the completion archive root used by workspace destroy",
    )
    workspace.add_argument("workspace_command", nargs="*")

    code = sub.add_parser("code", help="Open a generated workspace in VS Code")
    code.add_argument("task_id")
    code.add_argument("--dry-run", action="store_true")
