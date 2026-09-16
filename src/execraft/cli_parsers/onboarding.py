"""Argument parsers for project onboarding and greenfield workflows."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from execraft.onboarding.start import PlannerMode

def _add_agent_selection_arguments(parser: Any, *, include_required: bool) -> None:
    """Expose runtime-neutral agent terminology with provider aliases for compatibility."""

    parser.add_argument(
        "--agent",
        "--provider",
        dest="provider",
        default="",
        help="Execution agent/profile ID, instance name, or alias (--provider is deprecated)",
    )
    if include_required:
        parser.add_argument(
            "--require-agent",
            "--require-provider",
            dest="require_provider",
            action="store_true",
            help="Fail if no eligible execution agent is available",
        )


def add_onboarding_commands(sub: Any) -> None:
    project = sub.add_parser("project", help="Project descriptor operations")
    project.add_argument(
        "action",
        choices=["list", "validate", "bootstrap", "register", "bind", "doctor", "inspect", "templates", "profiles", "features", "check-update", "upgrade", "delete"],
    )
    project.add_argument("project_id", nargs="?")
    project.add_argument("--source", type=Path, help="Source root for bootstrap, bind, or doctor")
    project.add_argument("--output", type=Path, help="Output directory (for bootstrap, default: projects/)")
    project.add_argument(
        "--descriptor",
        type=Path,
        help="Existing project.yaml or containing directory (for register)",
    )
    project.add_argument("--dry-run", action="store_true", help="Preview the selected project mutation without publishing it")
    project.add_argument("--template", default="standard", help="Project template ID for bootstrap (default: standard)")
    project.add_argument("--profile", default="", help="Target profile for adoption or upgrade")
    project.add_argument("--feature", dest="features", action="append", default=[], help="Add a composable project feature (repeatable)")
    project.add_argument("--remove-feature", dest="remove_features", action="append", default=[], help="Remove a managed feature during upgrade")
    project.add_argument("--devcontainer", action="store_true", help="Include the optional workspace dev-container feature")
    project.add_argument("--without-devcontainer", action="store_true", help="Remove the optional workspace dev-container feature during upgrade")
    project.add_argument("--adopt", action="store_true", help="Adopt an existing untracked descriptor before future upgrades")
    project.add_argument("--force-managed", action="store_true", help="Replace locally modified profile-managed files")
    project.add_argument("--yes", "-y", action="store_true", help="Apply an upgrade or permanent deletion without interactive confirmation")
    project.add_argument(
        "--accept-decisions",
        action="store_true",
        help="Accept discovery findings marked decision_required",
    )
    project.add_argument(
        "--delete-branches",
        action="store_true",
        help="Also delete local task branches that the workspace registry proves Execraft created",
    )
    project.add_argument(
        "--state-dir",
        type=Path,
        help="Override the Execraft state root used by permanent deletion",
    )
    project.add_argument(
        "--archive-root",
        type=Path,
        help="Override the completion archive root used by permanent deletion",
    )
    project.add_argument("--json", action="store_true", help="Print machine-readable output")

    init = sub.add_parser(
        "init",
        help="Register the current source tree as an Execraft project",
    )
    init.add_argument("--source", type=Path, help="Source root (default: current directory)")
    init.add_argument(
        "--output",
        type=Path,
        help="Descriptor parent directory (default: <control-home>/projects)",
    )
    init.add_argument("--dry-run", action="store_true")
    init.add_argument("--template", default="standard", help="Project profile/template ID (default: standard)")
    init.add_argument("--feature", dest="features", action="append", default=[], help="Add a composable project feature (repeatable)")
    init.add_argument("--devcontainer", action="store_true", help="Include an optional workspace dev-container definition")
    init.add_argument(
        "--accept-decisions",
        action="store_true",
        help="Accept discovery findings marked decision_required",
    )
    init.add_argument("--json", action="store_true", help="Print machine-readable discovery and creation output")

    start = sub.add_parser(
        "start",
        help="Create or reuse a project, task, workspace, and validated draft plan",
    )
    start.add_argument(
        "description",
        nargs="?",
        default="",
        help="Natural-language task intent (optional when importing BRIEF.md/PLAN.md)",
    )
    start.add_argument("--source", type=Path, help="Project source root (default: current directory)")
    start.add_argument("--project", dest="project_id")
    start.add_argument("--id", dest="task_id", help="Explicit task ID")
    start.add_argument("--title", default="", help="Task title (derived from description by default)")
    start.add_argument("--repositories", nargs="*", default=())
    _add_agent_selection_arguments(start, include_required=True)
    start.add_argument(
        "--planner",
        choices=[item.value for item in PlannerMode],
        default=PlannerMode.AUTO.value,
        help="Draft planner: auto, agent, or local (default: auto)",
    )
    start.add_argument("--project-template", default="standard")
    start.add_argument("--task-template", default="standard")
    start.add_argument("--workspace-root", type=Path)
    start.add_argument("--policy")
    start.add_argument("--no-workspace", action="store_true")
    start.add_argument("--reuse-in-place", action="store_true")
    start.add_argument("--force-plan", action="store_true")
    start.add_argument("--brief-file", type=Path, help="Import an existing BRIEF.md")
    start.add_argument("--plan-file", type=Path, help="Import an existing PLAN.md")
    start.add_argument(
        "--plan-graph-file",
        type=Path,
        help="Import an existing PLAN.graph.yaml and validate it against task scope",
    )
    start.add_argument("--accept-decisions", action="store_true")
    start.add_argument("--dry-run", action="store_true")
    start.add_argument("--yes", "-y", action="store_true", help="Apply without an interactive confirmation")
    start.add_argument("--json", action="store_true")

    new = sub.add_parser(
        "new",
        help="Create a greenfield Git project and optionally start its first task",
    )
    new.add_argument("name", nargs="?", help="Project ID and source-directory name")
    new.add_argument("--directory", type=Path, help="Parent directory (default: current directory)")
    new.add_argument("--template", default="python-service", help="Source template")
    new.add_argument("--project-template", default="standard")
    new.add_argument("--feature", dest="features", action="append", default=[], help="Add a composable project feature (repeatable)")
    new.add_argument("--devcontainer", action="store_true", help="Include an optional workspace dev-container definition")
    new.add_argument("--start", dest="start_description", default="", help="Create the first task from this intent")
    new.add_argument("--task-id", default="")
    new.add_argument("--title", default="")
    new.add_argument("--repositories", nargs="*", default=())
    _add_agent_selection_arguments(new, include_required=False)
    new.add_argument(
        "--planner",
        choices=[item.value for item in PlannerMode],
        default=PlannerMode.AUTO.value,
    )
    new.add_argument("--workspace-root", type=Path)
    new.add_argument("--no-workspace", action="store_true")
    new.add_argument("--list-templates", action="store_true")
    new.add_argument("--dry-run", action="store_true")
    new.add_argument("--yes", "-y", action="store_true")
    new.add_argument("--json", action="store_true")

    home = sub.add_parser("home", help="Inspect or migrate the control-plane home")
    home.add_argument("action", nargs="?", default="show", choices=["show", "migrate"])
    home.add_argument(
        "--from",
        dest="from_root",
        type=Path,
        help="Legacy Execraft checkout to register during migration",
    )
    home.add_argument("--json", action="store_true", help="Print machine-readable output")

    projects = sub.add_parser("projects", help="Compatibility alias for 'project list'")
    projects.add_argument("action", nargs="?", default="list", choices=["list"])
    projects.add_argument("--json", action="store_true", help="Print machine-readable output")
