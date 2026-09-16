"""Command-line interface for the external AI development control plane."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Mapping

from execraft.skills import SkillCatalog, SkillCatalogError
from execraft.network import is_loopback_host
from execraft.control_plane import ControlPlaneHome, candidate_legacy_roots, xdg_state_home
from execraft.project import (
    ProjectError,
    list_project_catalog,
    list_projects,
    load_project,
    load_registered_project,
    project_directory,
    register_project_descriptor,
    resolve_current_project,
    validate_task_against_project,
    write_project_binding,
)
from execraft.workspace.task_git import (
    TaskGitError,
    TaskManifest,
    current_branch,
    head_commit,
    load_manifest,
    project_task_directory,
    repository_root,
    utc_now,
    validate_task_id,
    working_tree_dirty,
    write_manifest,
)
from execraft.workspace.workspace_git import (
    WorkspaceRecord,
    default_workspace_path,
    load_workspace,
    run_in_workspace,
    write_workspace,
)
from execraft.workspace.cleanup import WorkspaceLifecycleService
from execraft import bootstrap, guard
from execraft.archive import ArchivePreflightReport, TaskArchiveManager
from execraft.completion import (
    TaskCompletionError,
)
from execraft.completion.service import TaskCompletionService
from execraft.removal_cli import run_project_removal, run_task_removal
from execraft.removal_models import PermanentRemovalError
from execraft.orchestrate import (
    AgentCapability,
    OrchestrateError,
    HumanProgressReporter,
    OrchestrationConfig,
    ProjectOrchestrator,
    ResourceManager,
    VerificationRegistry,
    load_plan_graph_file,
    run_until_terminal,
)
from execraft.agents import (
    AgentConfigError,
    OpenCodeProviderRegistry,
    parse_execution_config,
    probe_openai_compatible_endpoint,
)
from execraft.orchestrate.identity import resolve_storage_identity
from execraft.orchestrate.task_status import sync_runtime_status
from execraft.onboarding.transactions import CreationTransactionError
from execraft.onboarding.profiles import default_profile_catalog
from execraft.onboarding.upgrades import ProjectUpgradeError, ProjectUpgradeService
from execraft.onboarding.greenfield import (
    GreenfieldError,
    GreenfieldService,
    default_greenfield_catalog,
)
from execraft.onboarding.start import (
    PlannerMode,
    StartRequest,
    StartWorkflowError,
    StartWorkflowService,
)
from execraft.onboarding.task_definition import TaskDefinitionInput
from execraft.onboarding.selection import ProviderSelector
from execraft.replan import ReplanError
from execraft.replan.service import ReplanInputs, ReplanService
from execraft.repository_sync.planning import (
    RepositorySyncPlanningError,
    build_sync_before_definition,
    validate_sync_repository_selection,
)
from execraft.workspace.lifecycle import (
    manifest_drift as lifecycle_manifest_drift,
    prepare_workspace,
    render_workspace_record as lifecycle_render_workspace_record,
    validate_workspace_record_ownership as lifecycle_validate_workspace_record_ownership,
)

from execraft.cli_parsers import build_parser
from execraft.cli_export import run_export_command
from execraft.cli_agents import run_agents_command
from execraft.cli_orchestration import run_orchestrate_command
from execraft.cli_orchestration_output import _print_supervisor_human_decision
from execraft.cli_config import (
    build_orchestration_config,
    load_project_opencode_registry as _load_project_opencode_registry,
    load_resource_policy,
    load_yaml_mapping as _load_yaml_mapping,
    project_config_path as _project_config_path,
)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args, unknown = parser.parse_known_args(argv)
    if unknown:
        if args.command == "workspace" and args.action == "run":
            args.workspace_command.extend(item for item in unknown if item != "--")
        else:
            parser.error(f"unrecognized arguments: {' '.join(unknown)}")
    try:
        handler = _command_handlers().get(str(getattr(args, "command", "")))
        if handler is not None:
            return handler(args)
    except (
        ProjectError,
        TaskGitError,
        OrchestrateError,
        CreationTransactionError,
        StartWorkflowError,
        GreenfieldError,
        ReplanError,
        TaskCompletionError,
        PermanentRemovalError,
        FileNotFoundError,
        ValueError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    parser.print_help()
    return 1

def _guard_command(args: argparse.Namespace) -> int:
    return guard.report(
        args.repo_root,
        include_content_check=args.content,
        verbose=args.verbose,
    )


def _command_handlers() -> Mapping[str, Callable[[argparse.Namespace], int]]:
    """Return top-level handlers while preserving public CLI aliases."""

    return {
        "project": cmd_project,
        "projects": cmd_project,
        "init": cmd_project,
        "start": cmd_start,
        "new": cmd_new,
        "home": cmd_home,
        "task": cmd_task,
        "archive": cmd_archive,
        "render": cmd_render,
        "workspace": cmd_workspace,
        "code": cmd_code,
        "agents": cmd_agents,
        "doctor": cmd_doctor,
        "openclaw": cmd_openclaw,
        "gui": cmd_gui,
        "browser": cmd_browser,
        "plan": cmd_plan,
        "guard": _guard_command,
        "export": run_export_command,
        "orchestrate": cmd_orchestrate,
    }

def _confirm_application(*, prompt: str, assume_yes: bool) -> None:
    """Require one explicit confirmation before a multi-step creation workflow."""

    if assume_yes:
        return
    if not sys.stdin.isatty():
        raise StartWorkflowError(
            "non-interactive creation requires --yes; use --dry-run --json to inspect first"
        )
    answer = input(f"{prompt} [Y/n] ").strip().lower()
    if answer not in {"", "y", "yes"}:
        raise StartWorkflowError("operation cancelled by operator")


def _task_definition_from_args(args: argparse.Namespace) -> TaskDefinitionInput:
    return TaskDefinitionInput.from_paths(
        brief_file=getattr(args, "brief_file", None),
        plan_file=getattr(args, "plan_file", None),
        plan_graph_file=getattr(args, "plan_graph_file", None),
    )


def _start_request_from_args(args: argparse.Namespace, *, source_root: Path) -> StartRequest:
    return StartRequest(
        description=args.description,
        source_root=source_root,
        project_id=args.project_id or "",
        task_id=args.task_id or "",
        title=args.title or "",
        repository_ids=tuple(args.repositories or ()),
        provider_id=args.provider or "",
        planner_mode=PlannerMode(args.planner),
        project_template=args.project_template,
        task_template=args.task_template,
        workspace_root=args.workspace_root,
        policy_profile=args.policy or "",
        no_workspace=bool(args.no_workspace),
        reuse_in_place=bool(args.reuse_in_place),
        accept_decisions=bool(args.accept_decisions),
        require_provider=bool(args.require_provider),
        force_plan=bool(args.force_plan),
        task_definition=_task_definition_from_args(args),
    )


def cmd_start(args: argparse.Namespace) -> int:
    """Run the one-command project/task/workspace/planning journey."""

    home = ControlPlaneHome.resolve()
    source_root = (args.source or Path.cwd()).expanduser().resolve()
    service = StartWorkflowService(
        home=home,
        onboarding=bootstrap.create_onboarding_service(),
    )
    request = _start_request_from_args(args, source_root=source_root)
    preview = service.preview(request)
    if args.dry_run:
        if args.json:
            print(json.dumps(preview.as_mapping(), indent=2, sort_keys=True))
        else:
            print(preview.render_text())
            if preview.project_plan is not None:
                print("\nProject creation effects:")
                print(preview.project_plan.render_text())
            if preview.task_plan is not None:
                print("\nTask creation effects:")
                print(preview.task_plan.render_text())
        return 0

    if not args.json:
        print(preview.render_text())
    if not preview.can_apply:
        raise StartWorkflowError(
            "start preview contains blocking findings; review the dry-run and "
            "resolve or explicitly accept the reported decisions"
        )
    _confirm_application(prompt="Create and start this task?", assume_yes=bool(args.yes))
    outcome = service.run(request)
    if args.json:
        print(json.dumps(outcome.as_mapping(), indent=2, sort_keys=True))
    else:
        print()
        print(outcome.render_text())
        dossier = project_task_directory(home.root, outcome.project_id, outcome.task_id)
        print(f"Task dossier: {dossier}")
        print(f"Executable plan: {dossier / 'PLAN.graph.yaml'}")
        if outcome.workspace_root is not None:
            print(
                "Next: review the plan, then run "
                f"'execraft orchestrate initialize --task-id {outcome.task_id} "
                f"--project {outcome.project_id}'"
            )
    return 0 if outcome.ready else 1


def cmd_new(args: argparse.Namespace) -> int:
    """Create a greenfield source repository and register its descriptor."""

    catalog = default_greenfield_catalog()
    if args.list_templates:
        payload = [
            {
                "id": item.id,
                "version": item.version,
                "reference": item.reference,
                "description": item.description,
            }
            for item in catalog.templates()
        ]
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            for item in payload:
                print(f"{item['reference']}\t{item['description']}")
        return 0
    if not args.name:
        raise GreenfieldError("new requires NAME or --list-templates")

    home = ControlPlaneHome.resolve()
    onboarding = bootstrap.create_onboarding_service()
    service = GreenfieldService(onboarding=onboarding, templates=catalog)
    parent = (args.directory or Path.cwd()).expanduser().resolve()
    preview = service.create(
        name=args.name,
        parent=parent,
        descriptor_output=home.projects_dir,
        source_template=args.template,
        project_template=args.project_template,
        project_features=tuple(args.features or ()),
        include_devcontainer=bool(args.devcontainer),
        dry_run=True,
    )
    if args.dry_run:
        payload: dict[str, Any] = preview.as_mapping()
        if args.start_description:
            payload["first_task"] = {
                "description": args.start_description,
                "task_id": args.task_id,
                "planner": args.planner,
                "workspace_root": str(args.workspace_root or ""),
            }
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(preview.source_plan.render_text())
            print("\nProject descriptor effects:")
            print(preview.project_outcome.plan.render_text())
            if args.start_description:
                print("\nFirst task will be created after source/project publication.")
        return 0

    if not args.json:
        print(preview.source_plan.render_text())
        print("\nProject descriptor effects:")
        print(preview.project_outcome.plan.render_text())
    _confirm_application(prompt=f"Create greenfield project {args.name!r}?", assume_yes=bool(args.yes))
    outcome = service.create(
        name=args.name,
        parent=parent,
        descriptor_output=home.projects_dir,
        source_template=args.template,
        project_template=args.project_template,
        project_features=tuple(args.features or ()),
        include_devcontainer=bool(args.devcontainer),
        dry_run=False,
    )
    payload = outcome.as_mapping()
    start_outcome = None
    if args.start_description:
        assert outcome.source_root is not None
        start_service = StartWorkflowService(home=home, onboarding=onboarding)
        start_request = StartRequest(
            description=args.start_description,
            source_root=outcome.source_root,
            project_id=args.name,
            task_id=args.task_id or "",
            title=args.title or "",
            repository_ids=tuple(args.repositories or ()),
            provider_id=args.provider or "",
            planner_mode=PlannerMode(args.planner),
            workspace_root=args.workspace_root,
            no_workspace=bool(args.no_workspace),
        )
        start_outcome = start_service.run(start_request)
        payload["first_task"] = start_outcome.as_mapping()

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        assert outcome.source_root is not None
        print(f"Created source repository: {outcome.source_root}")
        print(f"Registered descriptor: {outcome.project_outcome.path}")
        if start_outcome is not None:
            print()
            print(start_outcome.render_text())
    return 0

def cmd_home(args: argparse.Namespace) -> int:
    """Inspect the active home or register projects from a legacy checkout."""

    active = ControlPlaneHome.resolve().ensure_layout()
    if args.action == "show":
        payload = active.as_mapping()
        payload["legacy_candidates"] = [
            str(path) for path in candidate_legacy_roots()
        ]
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        print(f"Control-plane home: {active.root}")
        print(f"Origin: {active.origin}")
        if active.environment_variable:
            print(f"Selected by: {active.environment_variable}")
        print(f"Projects: {active.projects_dir}")
        print(f"Configuration: {active.config_dir}")
        print(f"State: {active.state_dir}")
        if active.is_legacy:
            print(
                "Legacy checkout mode is active. Run 'execraft home migrate' to "
                "register its descriptors for checkout-independent use."
            )
        return 0

    source = args.from_root.expanduser().resolve() if args.from_root else None
    if source is None:
        source = next(iter(candidate_legacy_roots()), None)
    if source is None:
        raise ProjectError(
            "no legacy Execraft checkout was found; pass 'execraft home migrate --from PATH'"
        )
    projects_root = source / "projects"
    if not projects_root.is_dir():
        raise ProjectError(f"legacy projects directory does not exist: {projects_root}")

    # Legacy selection must migrate toward the normal installed home.  An
    # explicitly selected modern home is already the operator's intended
    # destination and should not be replaced by the XDG default in reporting.
    target = (
        ControlPlaneHome.xdg().ensure_layout()
        if active.is_legacy
        else active
    )
    migrated: list[dict[str, str]] = []
    for child in sorted(projects_root.iterdir()):
        project_file = child / "project.yaml"
        if not child.is_dir() or not project_file.is_file():
            continue
        legacy_project = load_project(child)
        registration_path = register_project_descriptor(project_file)
        project = load_registered_project(target.root, legacy_project.id)
        migrated.append(
            {
                "project": project.id,
                "descriptor": str(project_file.resolve()),
                "registration": str(registration_path),
            }
        )
    payload = {
        "source": str(source),
        "target_home": str(target.root),
        "registered": migrated,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"Registered {len(migrated)} project(s) from {source}")
    print(f"Installed control-plane home: {target.root}")
    for item in migrated:
        print(f"  {item['project']}: {item['descriptor']}")
    if active.is_legacy:
        print(
            "The current process still uses legacy checkout mode. Leave the checkout "
            "or unset the legacy root environment variable to use the XDG home."
        )
    return 0


def _print_discovery_report(report: bootstrap.DiscoveryReport) -> None:
    """Render discovery evidence without coupling the domain model to stdout."""

    print(f"Discovered {len(report.repositories)} repositories")
    print(f"  Project ID: {report.project_id}")
    print(f"  Source root: {report.source_root}")
    print(f"  Languages: {', '.join(sorted(report.languages)) or 'none detected'}")
    for repository in report.repositories:
        state = "dirty" if repository.dirty else "clean"
        operation = f", operation: {repository.operation}" if repository.operation else ""
        print(
            f"  {repository.id}: {repository.path} "
            f"(branch: {repository.branch}, base: {repository.base_branch}, {state}{operation})"
        )
    if report.findings:
        print("Findings:")
        for finding in report.findings:
            print(
                f"  [{finding.severity.value}] {finding.code}: {finding.message}"
            )


def cmd_project(args: argparse.Namespace) -> int:
    home = ControlPlaneHome.resolve()
    root = home.root
    action = (
        "bootstrap"
        if args.command == "init"
        else ("list" if args.command == "projects" else args.action)
    )
    if action == "list":
        catalog = list_project_catalog(root)
        if getattr(args, "json", False):
            rows: list[dict[str, Any]] = []
            for entry in catalog:
                registration = entry.registration
                rows.append(
                    {
                        "id": entry.project.id,
                        "description": entry.project.description,
                        "profile": entry.project.profile,
                        "features": list(entry.project.features),
                        "generated_with": entry.project.generated_with,
                        "repositories": len(entry.project.repositories),
                        "descriptor": str(entry.project.directory / "project.yaml"),
                        "source_root": (
                            str(registration.source_root)
                            if registration is not None
                            and registration.source_root is not None
                            else ""
                        ),
                        "origin": entry.origin,
                    }
                )
            print(json.dumps(rows, indent=2, sort_keys=True))
            return 0
        for entry in catalog:
            project = entry.project
            print(
                f"{project.id}	{len(project.repositories)} repositories	"
                f"{project.description}	{entry.origin}"
            )
        return 0

    if action == "delete":
        state_root = (
            args.state_dir.expanduser().resolve()
            if getattr(args, "state_dir", None)
            else home.state_dir
        )
        return run_project_removal(args, control_root=root, state_root=state_root)

    service = bootstrap.create_onboarding_service()

    if action == "templates":
        descriptors = service.templates.descriptors()
        payload = [
            {
                "id": item.id,
                "version": item.version,
                "reference": item.reference,
                "kind": item.kind,
                "description": item.description,
            }
            for item in descriptors
        ]
        if getattr(args, "json", False):
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            for item in payload:
                print(
                    f"{item['kind']}\t{item['reference']}\t{item['description']}"
                )
        return 0

    if action in {"profiles", "features"}:
        catalog = default_profile_catalog()
        values = catalog.profiles() if action == "profiles" else catalog.features()
        payload = [
            {
                "id": item.id,
                "version": item.version,
                "reference": item.reference,
                "description": item.description,
                **(
                    {
                        "default_features": list(item.default_features),
                        "default_policy": item.default_policy,
                        "autonomous": item.autonomous,
                        "parallelism": item.parallelism,
                        "automatic_commits": item.automatic_commits,
                    }
                    if action == "profiles"
                    else {
                        "technologies": list(item.technologies),
                        "capabilities": list(item.capabilities),
                        "editor_extensions": list(item.editor_extensions),
                    }
                ),
            }
            for item in values
        ]
        if getattr(args, "json", False):
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            for item in payload:
                print(f"{item['reference']}\t{item['description']}")
        return 0

    if action in {"bootstrap", "inspect"}:
        source = (getattr(args, "source", None) or Path.cwd()).expanduser().resolve()
        report = service.inspect_project(source)
        if action == "inspect":
            if getattr(args, "json", False):
                print(json.dumps(report.as_mapping(), indent=2, sort_keys=True))
            else:
                _print_discovery_report(report)
            return 0 if report.can_scaffold else 1

        output = getattr(args, "output", None) or home.projects_dir
        outcome = service.create_project(
            report=report,
            output_dir=output,
            template_id=getattr(args, "template", "standard"),
            register=not bool(getattr(args, "dry_run", False)),
            dry_run=bool(getattr(args, "dry_run", False)),
            accept_decisions=bool(getattr(args, "accept_decisions", False)),
            feature_ids=tuple(getattr(args, "features", ()) or ()),
            include_devcontainer=bool(getattr(args, "devcontainer", False)),
        )
        payload = {
            "discovery": report.as_mapping(),
            "creation": outcome.as_mapping(),
            "control_plane_home": str(home.root),
        }
        if getattr(args, "json", False):
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0 if outcome.plan.can_apply else 1

        _print_discovery_report(report)
        if getattr(args, "dry_run", False):
            print()
            print(outcome.plan.render_text())
            return 0 if outcome.plan.can_apply else 1
        assert outcome.path is not None
        print(f"Generated project: {outcome.path}")
        registration = outcome.details.get("registration", "")
        if registration:
            print(f"Registered project: {registration}")
        print(f"Control-plane home: {home.root}")
        print("Verification suggestions are disabled until explicitly reviewed and enabled.")
        return 0

    if action == "register":
        descriptor = getattr(args, "descriptor", None)
        if descriptor is None:
            raise ProjectError("project register requires --descriptor PATH")
        candidate = descriptor.expanduser().resolve()
        project = load_project(candidate if candidate.is_dir() else candidate.parent)
        registration = register_project_descriptor(
            descriptor,
            source_root=args.source,
        )
        print(f"Registered project {project.id}: {project.directory / 'project.yaml'}")
        print(f"Registration: {registration}")
        if args.source is not None:
            print(f"Source root: {args.source.expanduser().resolve()}")
        return 0

    project_id = getattr(args, "project_id", None)
    project = resolve_current_project(root, project_id=project_id)

    if action in {"check-update", "upgrade"}:
        if bool(getattr(args, "devcontainer", False)) and bool(
            getattr(args, "without_devcontainer", False)
        ):
            raise ProjectUpgradeError(
                "--devcontainer and --without-devcontainer are mutually exclusive"
            )
        upgrade_service = ProjectUpgradeService()
        provenance_path = upgrade_service.provenance_path(project)
        if not provenance_path.is_file() and bool(getattr(args, "adopt", False)):
            if action != "upgrade":
                raise ProjectUpgradeError("--adopt is only valid with project upgrade")
            if not bool(getattr(args, "yes", False)):
                _confirm_application(
                    prompt=f"Adopt existing project {project.id!r} for managed upgrades?",
                    assume_yes=False,
                )
            adopted = upgrade_service.adopt(
                project,
                profile_reference=(
                    getattr(args, "profile", "") or project.profile or "standard@1"
                ),
                feature_references=tuple(getattr(args, "features", ()) or project.features),
            )
            if not getattr(args, "json", False):
                print(
                    f"Adopted {project.id} as {adopted.profile}; "
                    f"tracked {len(adopted.managed_files)} managed files"
                )
        include_devcontainer: bool | None
        if bool(getattr(args, "devcontainer", False)):
            include_devcontainer = True
        elif bool(getattr(args, "without_devcontainer", False)):
            include_devcontainer = False
        else:
            include_devcontainer = None
        plan = upgrade_service.plan(
            project,
            source_root=getattr(args, "source", None),
            target_profile=getattr(args, "profile", ""),
            add_features=tuple(getattr(args, "features", ()) or ()),
            remove_features=tuple(getattr(args, "remove_features", ()) or ()),
            include_devcontainer=include_devcontainer,
        )
        payload = plan.as_mapping()
        if action == "check-update" or bool(getattr(args, "dry_run", False)):
            try:
                if getattr(args, "json", False):
                    print(json.dumps(payload, indent=2, sort_keys=True))
                else:
                    print(plan.render_text())
            finally:
                upgrade_service.cleanup(plan)
            return 0 if not plan.provenance_missing and not plan.conflicts else 1
        if plan.up_to_date:
            upgrade_service.cleanup(plan)
            if getattr(args, "json", False):
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(f"Project {project.id} is already up to date")
            return 0
        if not plan.can_apply and not bool(getattr(args, "force_managed", False)):
            upgrade_service.cleanup(plan)
            raise ProjectUpgradeError(
                "upgrade contains managed-file conflicts; inspect --dry-run or pass --force-managed"
            )
        if not getattr(args, "json", False):
            print(plan.render_text())
        _confirm_application(
            prompt=f"Apply profile upgrade to project {project.id!r}?",
            assume_yes=bool(getattr(args, "yes", False)),
        )
        upgrade_service.apply(
            plan, force_conflicts=bool(getattr(args, "force_managed", False))
        )
        if getattr(args, "json", False):
            payload["applied"] = True
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"Upgraded project {project.id} to {plan.target_profile}")
        return 0

    if action == "bind":
        if args.source is None:
            raise ProjectError("project bind requires --source /path/to/checkouts")
        binding = write_project_binding(
            project.id,
            args.source,
            descriptor=project.directory / "project.yaml",
        )
        print(f"Bound project {project.id} to {args.source.expanduser().resolve()}")
        print(f"Registration: {binding}")
        return 0

    if action == "doctor":
        readiness = service.evaluate_readiness(project, source_root=args.source)
        structural_issues = bootstrap.doctor_project(
            project.directory / "project.yaml", args.source
        )
        if structural_issues:
            payload = readiness.as_mapping()
            payload["structural_issues"] = structural_issues
            payload["ready"] = False
        else:
            payload = readiness.as_mapping()
        if getattr(args, "json", False):
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(readiness.render_text())
            if structural_issues:
                print("Structural issues:")
                for issue in structural_issues:
                    print(f"  - {issue}")
        return 0 if readiness.ready and not structural_issues else 1

    print(
        f"Project {project.id} is valid: {len(project.repositories)} repositories, "
        f"default policy {project.default_policy}"
    )
    print(f"Descriptor: {project.directory / 'project.yaml'}")
    return 0


def _workspace_active_task(workspace_root: Path) -> Path:
    return workspace_root.resolve() / ".execraft" / "active-task"


def cmd_task(args: argparse.Namespace) -> int:
    root = repository_root()
    if args.action == "new":
        definition = _task_definition_from_args(args)
        if args.brief and definition.has_brief:
            raise TaskGitError("task new cannot combine --brief with --brief-file")
        if not args.task_id:
            raise TaskGitError("task new requires TASK_ID")
        if not args.title and not definition.supplied:
            raise TaskGitError(
                "task new requires --title unless a task-definition document is imported"
            )
        project = resolve_current_project(root, project_id=args.project_id)
        state_root = (
            args.state_dir.expanduser().resolve()
            if args.state_dir
            else xdg_state_home()
        )
        outcome = bootstrap.create_onboarding_service().create_task(
            control_root=root,
            project=project,
            task_id=args.task_id,
            title=args.title or definition.suggested_title(),
            branch=args.branch or "",
            repository_ids=tuple(args.repositories or ()),
            brief=args.brief,
            template_id=args.template,
            state_root=state_root,
            definition=definition,
            dry_run=bool(args.dry_run),
        )
        if args.json:
            print(json.dumps(outcome.as_mapping(), indent=2, sort_keys=True))
            return 0
        if args.dry_run:
            print(outcome.plan.render_text())
            return 0
        assert outcome.path is not None
        print(f"Created task {args.task_id} for project {project.id}")
        print(f"Task dossier: {outcome.path}")
        print(f"Runtime status: {outcome.details['runtime_status']}")
        return 0

    if args.action == "delete":
        state_root = (
            args.state_dir.expanduser().resolve()
            if args.state_dir
            else xdg_state_home()
        )
        return run_task_removal(args, control_root=root, state_root=state_root)

    if not args.task_id:
        active = _workspace_active_task(args.workspace_root)
        if active.is_file():
            args.task_id = active.read_text(encoding="utf-8").strip()
    if not args.task_id:
        raise TaskGitError(f"task {args.action} requires a task ID")
    manifest = load_manifest(root, validate_task_id(args.task_id))

    if args.action in {"status", "sync-status"}:
        dossier = project_task_directory(root, manifest.project, manifest.id)
        state_root = (
            args.state_dir.expanduser().resolve()
            if args.state_dir
            else xdg_state_home()
        )
        runtime = sync_runtime_status(
            dossier,
            task_id=manifest.id,
            project_id=manifest.project,
            state_root=state_root,
        )
        if args.action == "sync-status":
            if runtime.has_state:
                record = runtime.record
                assert record is not None
                print(
                    f"Synchronized runtime status: {record.state.value} "
                    f"({record.completed_packages}/{record.total_packages})"
                )
            else:
                print("Synchronized runtime status: not initialized")
            print(f"Runtime status: {runtime.path}")
            return 0

        print(f"Task: {manifest.id}")
        print(f"Project: {manifest.project}")
        print(f"Status: {manifest.status} (task lifecycle)")
        print(f"Branch: {manifest.branch_name}")
        if runtime.has_state:
            record = runtime.record
            assert record is not None
            active = [
                package
                for package in record.plan_graph.work_packages
                if package.stage.value not in {"prepare", "completed"}
            ]
            current = sorted(active, key=lambda item: (-item.priority, item.id))[0] if active else None
            print(
                f"Orchestration: {record.state.value} "
                f"({record.completed_packages}/{record.total_packages})"
            )
            if current is not None:
                print(
                    f"Current package: {current.id} — {current.title} "
                    f"[stage={current.stage.value}, status={current.status}]"
                )
            else:
                print("Current package: none")
        else:
            print("Orchestration: not initialized")
        print(f"Runtime status: {runtime.path}")
        print(f"Control plane: {root}")
        git_probe = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
        if git_probe.returncode == 0 and git_probe.stdout.strip() == "true":
            control_branch = current_branch(root)
            control_head = head_commit(root)
            control_state = "dirty" if working_tree_dirty(root) else "clean"
            print(
                f"Control-plane Git: {control_branch} @ {control_head[:12]} "
                f"({control_state})"
            )
        else:
            print("Control-plane Git: not applicable (installed control home)")
        for repository in manifest.repositories:
            print(
                f"  {repository.id}: {repository.task_branch} "
                f"({repository.mutability}, {repository.role})"
            )
        return 0
    if args.action == "switch":
        active = _workspace_active_task(args.workspace_root)
        active.parent.mkdir(parents=True, exist_ok=True)
        active.write_text(f"{manifest.id}\n", encoding="utf-8")
        print(f"Active task: {manifest.id}")
        return 0
    if args.action == "commit":
        if not args.task_id:
            raise TaskGitError("task commit requires a task ID (or use --workspace-root with an active task)")
        from execraft.orchestrate.transactions import CommitJournal, RepositorySnapshot

        workspace_record = load_workspace(root, args.task_id)

        commit_journal = CommitJournal(
            root / ".git" / "ai-commits" / f"{args.task_id}.json"
        )
        package_id = f"{args.task_id}-manual-{utc_now().replace(':', '-')}"
        tx = commit_journal.begin(
            hashlib.sha256(package_id.encode()).hexdigest()[:16],
            utc_now(),
            package_id,
        )
        for item in workspace_record.repositories:
            repo_path = Path(item["worktree_path"])
            if repo_path.exists() and item.get("mutability", "task_owned") == "task_owned":
                snapshot = RepositorySnapshot(
                    repository_id=str(item["id"]),
                    branch=current_branch(repo_path),
                    head_commit=head_commit(repo_path),
                    dirty=working_tree_dirty(repo_path),
                    path=str(repo_path),
                )
                commit_journal.add_snapshots(tx.transaction_id, post_snapshots=[snapshot])

        commit_journal.commit(tx.transaction_id)
        print(f"Commit transaction recorded: {tx.transaction_id}")
        print(f"Repositories: {len(workspace_record.repositories)}")
        print("Use the canonical ai-commit workflow to finalize multi-repository commits.")
        return 0
    if args.action == "review":
        if manifest.status not in {"in_progress", "review"}:
            raise TaskGitError(f"cannot review task from status {manifest.status}")
        manifest.status = "review"
        write_manifest(root, manifest)
        print(f"Task {manifest.id} is ready for review")
        return 0
    if args.action == "sync-before":
        if manifest.status in {"closed", "merged", "abandoned", "integrating"}:
            raise TaskGitError(
                f"cannot insert repository synchronization from lifecycle status {manifest.status}"
            )
        if not str(args.before or "").strip():
            raise TaskGitError("task sync-before requires --before <work-package-id>")
        project = resolve_current_project(root, project_id=manifest.project)
        state_root = (
            args.state_dir.expanduser().resolve()
            if args.state_dir
            else xdg_state_home()
        )
        dossier = project_task_directory(root, manifest.project, manifest.id)
        repositories = list(args.repositories or [])
        if not repositories:
            try:
                graph = load_plan_graph_file(dossier / "PLAN.graph.yaml")
                target = graph.package_by_id(str(args.before).strip())
                repositories = list(target.affected_repositories)
            except Exception as exc:
                raise TaskGitError(
                    "cannot infer synchronized repositories from the target Work Package; "
                    "supply --repositories explicitly: " + str(exc)
                ) from exc
        overrides = _parse_repository_source_branches(args.source_branch)
        try:
            repositories = list(
                validate_sync_repository_selection(manifest, repositories)
            )
            insertion = build_sync_before_definition(
                brief_markdown=(dossier / "BRIEF.md").read_text(encoding="utf-8"),
                plan_markdown=(dossier / "PLAN.md").read_text(encoding="utf-8"),
                plan_graph_yaml=(dossier / "PLAN.graph.yaml").read_text(encoding="utf-8"),
                before_package_id=str(args.before),
                repositories=repositories,
                source_branches=overrides,
                remote=str(args.remote or "origin"),
                conflict_policy=str(args.conflict_policy),
                sync_package_id=str(args.sync_id or ""),
            )
        except (OSError, RepositorySyncPlanningError) as exc:
            raise TaskGitError(f"cannot create repository-sync Work Package: {exc}") from exc
        service = ReplanService(
            control_root=root,
            state_root=state_root,
            project=project,
            manifest=manifest,
            dossier=dossier,
        )
        candidate = service.create_candidate(
            ReplanInputs(
                requested_change=(
                    f"Insert {insertion.sync_package_id} before {insertion.target_package_id} "
                    "as an orchestration-owned repository synchronization Work Package."
                ),
                definition=insertion.definition,
                allow_structural_consistency=True,
            ),
            provider=None,
            workdir=dossier,
        )
        if args.apply:
            result = service.apply_candidate(candidate.candidate_id)
            if args.json:
                print(json.dumps(result.as_mapping(), indent=2, sort_keys=True))
            else:
                _print_replan_candidate(candidate)
                print(
                    f"Applied repository-sync Work Package {insertion.sync_package_id} "
                    f"before {insertion.target_package_id} as revision {result.revision}"
                )
            return 0
        if args.json:
            payload = candidate.as_mapping()
            payload["repository_sync"] = {
                "package_id": insertion.sync_package_id,
                "before": insertion.target_package_id,
                "repositories": list(insertion.repositories),
            }
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            _print_replan_candidate(candidate)
            print(
                f"Repository-sync candidate {insertion.sync_package_id} will run before "
                f"{insertion.target_package_id}."
            )
            print(
                f"Apply with: execraft task replan {manifest.id} "
                f"--candidate {candidate.candidate_id} --apply"
            )
        return 0

    if args.action == "replan":
        _validate_replan_cli_mode(args)
        if manifest.status in {"closed", "merged", "abandoned", "integrating"}:
            raise TaskGitError(
                f"cannot replan task from lifecycle status {manifest.status}"
            )
        project = resolve_current_project(root, project_id=manifest.project)
        state_root = (
            args.state_dir.expanduser().resolve()
            if args.state_dir
            else xdg_state_home()
        )
        dossier = project_task_directory(root, manifest.project, manifest.id)
        service = ReplanService(
            control_root=root,
            state_root=state_root,
            project=project,
            manifest=manifest,
            dossier=dossier,
        )
        if args.recover:
            recovered = service.recover_incomplete()
            print("Recovered interrupted replan transaction" if recovered else "No interrupted replan transaction found")
            return 0
        if args.candidate:
            if args.apply:
                result = service.apply_candidate(args.candidate)
                if args.json:
                    print(json.dumps(result.as_mapping(), indent=2, sort_keys=True))
                else:
                    print(f"Applied task definition revision {result.revision}: {result.candidate_id}")
                    print(f"Revision archive: {result.revision_path}")
                    print(f"Definition SHA-256: {result.definition_sha256}")
                    print(f"Context capsules invalidated: {result.invalidated_capsules}")
                    if result.previous_task_status != result.task_status:
                        print(
                            "Task lifecycle: "
                            f"{result.previous_task_status} -> {result.task_status}"
                        )
                return 0
            candidate = service.load_candidate(args.candidate)
            if args.json:
                print(json.dumps(candidate.as_mapping(), indent=2, sort_keys=True))
            else:
                _print_replan_candidate(candidate)
            return 0

        request_text = str(args.request or "").strip()
        if args.request_file:
            if request_text:
                raise TaskGitError("task replan accepts either --request or --request-file, not both")
            request_text = _read_replan_request_file(args.request_file)
        definition = _task_definition_from_args(args)
        if not (request_text or definition.supplied or args.from_current_files):
            raise TaskGitError(
                "task replan requires --request, imported definition files, --from-current-files, or --candidate"
            )
        mapping = _parse_replan_supersede(args.supersede)
        workdir = dossier
        try:
            workspace = load_workspace(root, manifest.id)
            workspace_root = Path(workspace.workspace_root).expanduser().resolve()
            if workspace_root.is_dir():
                workdir = workspace_root
        except TaskGitError:
            pass
        provider = ProviderSelector().select(
            project,
            workdir=workdir,
            requested=str(args.provider or ""),
        )
        candidate = service.create_candidate(
            ReplanInputs(
                requested_change=request_text,
                definition=definition,
                from_current_files=bool(args.from_current_files),
                package_mapping=mapping,
                allow_structural_consistency=bool(args.allow_structural),
            ),
            provider=provider,
            workdir=workdir,
        )
        if args.apply:
            result = service.apply_candidate(candidate.candidate_id)
            if args.json:
                print(json.dumps(result.as_mapping(), indent=2, sort_keys=True))
            else:
                _print_replan_candidate(candidate)
                print(f"Applied revision {result.revision}; archive: {result.revision_path}")
                if result.previous_task_status != result.task_status:
                    print(
                        "Task lifecycle: "
                        f"{result.previous_task_status} -> {result.task_status}"
                    )
            return 0
        if args.json:
            print(json.dumps(candidate.as_mapping(), indent=2, sort_keys=True))
        else:
            _print_replan_candidate(candidate)
            print(f"Apply with: execraft task replan {manifest.id} --candidate {candidate.candidate_id} --apply")
        return 0
    if args.action == "complete":
        state_root = (
            args.state_dir.expanduser().resolve()
            if args.state_dir
            else xdg_state_home()
        )
        service = TaskCompletionService(
            root,
            state_root,
            archive_root=args.archive_root,
        )
        result = service.complete(manifest.id, dry_run=bool(args.dry_run))
        if args.json:
            print(json.dumps(result.as_mapping(), indent=2, sort_keys=True))
        else:
            _print_task_completion_result(result, preview=bool(args.dry_run))
        return 0

    if args.action == "close":
        if args.check and args.archive:
            raise TaskGitError("task close accepts either --check or --archive, not both")
        state_root = (
            args.state_dir.expanduser().resolve()
            if args.state_dir
            else xdg_state_home()
        )
        manager = TaskArchiveManager(
            root,
            state_root,
            archive_root=args.archive_root,
        )
        if args.check or args.archive:
            report = manager.preflight(manifest)
            _print_archive_preflight(report)
            if args.check:
                return 0 if report.ok else 1
            report.require_ok()
            result = manager.archive(manifest)
            action = "Created" if result.created else "Reused"
            print(f"{action} completion archive: {result.archive_path}")
            print(f"Manifest: {result.manifest_path}")
            print(f"Manifest SHA-256: {result.manifest_sha256}")
            print(f"Closed task {manifest.id}")
            return 0
        if manifest.status not in {"approved", "merged", "closed"}:
            raise TaskGitError(
                f"task must be approved or merged before close; found {manifest.status}"
            )
        manifest.status = "closed"
        write_manifest(root, manifest)
        print(f"Closed task {manifest.id} without a completion archive")
        print("Run 'execraft task close <task-id> --archive' before destroying its workspace.")
        return 0
    raise TaskGitError(f"unsupported task action: {args.action}")






def _print_task_completion_result(result: Any, *, preview: bool = False) -> None:
    """Render one compact, actionable completion transaction summary."""

    prefix = "Completion preview" if preview else "Task completion"
    print(f"{prefix}: {result.task_id} [{result.status}/{result.phase}]")
    if result.archive_path is not None:
        print(f"Archive: {result.archive_path}")
    if result.workspace_status:
        print(f"Workspace registry: {result.workspace_status}")
    if result.removed_worktrees:
        print(f"Removed worktrees: {len(result.removed_worktrees)}")
    if result.shell_removed:
        print("Generated workspace shell removed")
    if result.resumed:
        print("Resumed an interrupted completion transaction")
    print(f"Completion report: {result.report_path}")


def _complete_finished_orchestration(
    *,
    orchestrator: ProjectOrchestrator,
    task_id: str,
) -> bool:
    """Run configured post-completion cleanup and report failures fail-closed.

    Returns ``True`` when completion is disabled or finished successfully. A
    cleanup failure does not rewrite the already-completed orchestration state;
    it instead leaves a resumable completion transaction and makes the driver
    exit non-zero so automation cannot mistake partial teardown for success.
    """

    config = getattr(orchestrator, "config", None)
    if config is None:
        # Lightweight embedding/test doubles may not expose a policy.
        # Production orchestrators always carry OrchestrationConfig.
        return True
    policy = config.task_completion_policy
    if not policy.automatic:
        return True
    service = TaskCompletionService(
        repository_root(),
        config.state_dir,
        policy=policy,
    )
    try:
        result = service.complete(task_id)
    except (TaskCompletionError, TaskGitError) as exc:
        report_path = getattr(exc, "report_path", None)
        print(f"Automatic task completion incomplete: {exc}", file=sys.stderr)
        if report_path:
            print(f"Completion report: {report_path}", file=sys.stderr)
        print(
            f"Resume safely with: execraft task complete {task_id}",
            file=sys.stderr,
        )
        return False
    _print_task_completion_result(result)
    return True


def _validate_replan_cli_mode(args: argparse.Namespace) -> None:
    """Reject ambiguous replan flag combinations before reading or mutating state."""

    has_definition_files = any(
        getattr(args, name, None)
        for name in ("brief_file", "plan_file", "plan_graph_file")
    )
    has_request = bool(str(getattr(args, "request", "") or "").strip())
    has_request_file = getattr(args, "request_file", None) is not None
    has_supersede = bool(getattr(args, "supersede", None))
    provider = bool(str(getattr(args, "provider", "") or "").strip())
    structural = bool(getattr(args, "allow_structural", False))
    from_current = bool(getattr(args, "from_current_files", False))
    apply = bool(getattr(args, "apply", False))

    if has_request and has_request_file:
        raise TaskGitError(
            "task replan accepts either --request or --request-file, not both"
        )
    if from_current and has_definition_files:
        raise TaskGitError(
            "--from-current-files cannot be combined with replacement definition files"
        )
    if bool(getattr(args, "recover", False)):
        if any(
            (
                getattr(args, "candidate", ""),
                has_request,
                has_request_file,
                has_definition_files,
                has_supersede,
                provider,
                structural,
                from_current,
                apply,
            )
        ):
            raise TaskGitError("--recover must be used as a standalone replan action")
        return
    if getattr(args, "candidate", ""):
        if any(
            (
                has_request,
                has_request_file,
                has_definition_files,
                has_supersede,
                provider,
                structural,
                from_current,
            )
        ):
            raise TaskGitError(
                "--candidate may only be combined with --apply and output/state options"
            )


def _read_replan_request_file(path: Path, *, maximum_bytes: int = 256 * 1024) -> str:
    """Read a bounded UTF-8 replan request without following symbolic links."""

    raw = Path(path).expanduser()
    if raw.is_symlink():
        raise TaskGitError(f"replan request file cannot be a symbolic link: {raw}")
    try:
        resolved = raw.resolve(strict=True)
        stat = resolved.stat()
    except OSError as exc:
        raise TaskGitError(f"cannot read replan request file {raw}: {exc}") from exc
    if not resolved.is_file():
        raise TaskGitError(f"replan request is not a regular file: {resolved}")
    if stat.st_size > maximum_bytes:
        raise TaskGitError(
            f"replan request exceeds the {maximum_bytes}-byte safety limit: {resolved}"
        )
    try:
        text = resolved.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TaskGitError(f"replan request must be UTF-8: {resolved}") from exc
    if "\x00" in text:
        raise TaskGitError(f"replan request cannot contain NUL bytes: {resolved}")
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()

def _parse_repository_source_branches(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in values:
        if "=" not in raw:
            raise TaskGitError(
                f"invalid --source-branch value {raw!r}; expected REPOSITORY=BRANCH"
            )
        repository_id, branch = (part.strip() for part in raw.split("=", 1))
        if not repository_id or not branch:
            raise TaskGitError(
                f"invalid --source-branch value {raw!r}; expected REPOSITORY=BRANCH"
            )
        if repository_id in result and result[repository_id] != branch:
            raise TaskGitError(
                f"repository {repository_id!r} has conflicting source-branch overrides"
            )
        result[repository_id] = branch
    return result


def _parse_replan_supersede(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in values or []:
        old, separator, new = str(raw).partition("=")
        old = old.strip()
        new = new.strip()
        if separator != "=" or not old or not new:
            raise TaskGitError(f"invalid --supersede mapping {raw!r}; expected OLD=NEW")
        if old == new:
            raise TaskGitError(f"--supersede must use a new package ID: {raw!r}")
        result[old] = new
    return result


def _print_replan_candidate(candidate: Any) -> None:
    impact = candidate.impact
    print(f"Replan candidate: {candidate.candidate_id} -> revision {candidate.revision}")
    print(f"Consistency: {candidate.consistency_mode}: {candidate.consistency_summary}")
    print(f"Applicable: {'yes' if impact.applicable else 'no'}")
    if impact.added_packages:
        print("Added packages: " + ", ".join(impact.added_packages))
    if impact.removed_packages:
        print("Removed packages: " + ", ".join(impact.removed_packages))
    for item in impact.packages:
        replacement = f" -> {item.replacement_id}" if item.replacement_id else ""
        changed = f" [{', '.join(item.changed_fields)}]" if item.changed_fields else ""
        print(f"  {item.package_id}: {item.classification}{replacement}{changed}")
    for blocker in impact.blockers:
        print(f"  BLOCKER: {blocker}")
    for warning in impact.warnings:
        print(f"  WARNING: {warning}")
    print(f"Candidate files: {candidate.path}")


def _print_archive_preflight(report: ArchivePreflightReport) -> None:
    print(f"Archive preflight: {report.project}/{report.task_id}")
    for check in report.checks:
        if check.ok:
            marker = "PASS"
        elif check.severity == "warning":
            marker = "WARN"
        else:
            marker = "FAIL"
        print(f"  [{marker}] {check.id}: {check.message}")
    print(f"Result: {'ready' if report.ok else 'blocked'}")


def cmd_archive(args: argparse.Namespace) -> int:
    root = repository_root()
    configured_state_dir = getattr(args, "state_dir", None)
    state_root = (
        configured_state_dir.expanduser().resolve()
        if configured_state_dir
        else xdg_state_home()
    )
    manager = TaskArchiveManager(root, state_root, archive_root=args.archive_root)

    if args.action == "list":
        records = manager.list_records(project=args.project_id)
        if not records:
            print("No task completion archives found")
            return 0
        for record in records:
            print(
                f"{record.get('project', '')}/{record.get('task_id', '')}	"
                f"{record.get('archive_id', '')}	{record.get('created_at', '')}	"
                f"{record.get('path', '')}"
            )
        return 0

    if not args.task_id or not args.project_id:
        raise TaskGitError(f"archive {args.action} requires TASK_ID and --project")
    result = manager.latest(args.project_id, validate_task_id(args.task_id))
    if args.action == "show":
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        print(f"Project: {manifest.get('project', '')}")
        print(f"Task: {manifest.get('task_id', '')}")
        print(f"Archive: {manifest.get('archive_id', '')}")
        print(f"Created: {manifest.get('created_at', '')}")
        print(f"State: {manifest.get('final_state', '')}")
        print(
            "Packages: "
            f"{manifest.get('completed_packages', 0)}/{manifest.get('total_packages', 0)}"
        )
        print(f"Path: {result.archive_path}")
        print(f"Manifest SHA-256: {result.manifest_sha256}")
        return 0
    if args.action == "verify":
        verification = manager.verify(
            result.archive_path,
            expected_manifest_sha256=result.manifest_sha256,
        )
        print(f"Archive verified: {verification['archive_path']}")
        print(f"Files checked: {verification['files_checked']}")
        print(f"Manifest SHA-256: {verification['manifest_sha256']}")
        return 0
    raise TaskGitError(f"unsupported archive action: {args.action}")

def _render_record(root: Path, manifest: TaskManifest, record: WorkspaceRecord, *, clean: bool) -> None:
    lifecycle_render_workspace_record(root, manifest, record, clean=clean)


def cmd_workspace(args: argparse.Namespace) -> int:
    root = repository_root()
    task_id = validate_task_id(args.task_id)

    if args.action == "start":
        manifest = load_manifest(root, task_id)
        project = load_registered_project(root, manifest.project)
        workspace_root = (
            args.workspace_root.resolve()
            if args.workspace_root
            else default_workspace_path(root, task_id, project_id=project.id)
        )
        result = prepare_workspace(
            control_root=root,
            manifest=manifest,
            project=project,
            source_root_override=args.source_root,
            workspace_root=workspace_root,
            reuse_in_place=args.reuse_in_place,
            policy_profile=args.policy or None,
            bind_source=not args.no_bind,
            reuse_existing=True,
            event=print,
            renderer=_render_record,
        )
        action = "created" if result.created else "reused"
        print(f"Workspace {action} at: {result.workspace_root}")
        print(f"Repositories: {len(result.record.repositories)}")
        if result.binding_path is not None:
            print(f"Stored local binding: {result.binding_path}")
        return 0

    record = load_workspace(root, task_id)
    workspace_root = Path(record.workspace_root)

    if args.action not in {"destroy", "stop"}:
        manifest = load_manifest(root, task_id)
        project = load_registered_project(root, manifest.project)
        validate_task_against_project(manifest, project)
        lifecycle_validate_workspace_record_ownership(record, manifest, project)

    if args.action == "status":
        print(f"Task: {record.task_id}")
        print(f"Status: {record.status}")
        print(f"Workspace: {record.workspace_root}")
        if record.compose_project:
            print(f"Compose: {record.compose_project}")
        if record.ros_domain_id >= 0:
            print(f"ROS domain: {record.ros_domain_id}")
        if record.port_offset:
            print(f"Port offset: {record.port_offset}")
        print(f"Policy: {record.policy_profile}")
        for item in record.repositories:
            print(
                f"  {item['id']}: {item['worktree_path']} "
                f"({item.get('mutability', 'task_owned')})"
            )
        findings = lifecycle_manifest_drift(workspace_root)
        print("Generated files: clean" if not findings else "Generated files: drifted")
        for finding in findings:
            print(f"  {finding}")
        return 1 if findings else 0

    if args.action == "sync":
        failures = 0
        for item in record.repositories:
            path = Path(item["worktree_path"])
            if not path.exists():
                print(f"missing: {item['id']} ({path})", file=sys.stderr)
                failures += 1
                continue
            if item.get("mutability", "task_owned") == "task_owned":
                branch = current_branch(path)
                expected = item["branch"]
                if branch != expected:
                    print(
                        f"branch mismatch: {item['id']} expected {expected}, found {branch}",
                        file=sys.stderr,
                    )
                    failures += 1
                    continue
            item["head"] = head_commit(path)
            print(f"synced {item['id']}: {item['head'][:12]}")
        write_workspace(root, record)
        return 1 if failures else 0

    if args.action == "refresh":
        _render_record(root, manifest, record, clean=True)
        print(f"Workspace {task_id} refreshed")
        return 0

    if args.action == "run":
        command = list(args.workspace_command)
        if command and command[0] == "--":
            command.pop(0)
        if not command:
            raise TaskGitError("workspace run requires a command after '--'")
        if record.policy_profile == "read-only":
            raise TaskGitError("workspace run is disabled by the read-only policy")
        if args.repository:
            selected = next(
                (
                    item
                    for item in record.repositories
                    if item.get("id") == args.repository
                ),
                None,
            )
            if selected is None:
                raise TaskGitError(
                    f"workspace {task_id!r} has no repository {args.repository!r}"
                )
            if selected.get("mutability") == "runtime_only":
                raise TaskGitError(
                    f"workspace run is disabled for runtime-only repository {args.repository!r}"
                )
        result = run_in_workspace(record, command, repository_id=args.repository)
        return result.returncode

    if args.action == "verify":
        from execraft.orchestrate.verification import VerificationRegistry, resolve_profile

        verification_path_value = project.paths.get("verification_file", "")
        registry = (
            VerificationRegistry.load(project.configured_path("verification_file"))
            if verification_path_value else VerificationRegistry()
        )
        profile = resolve_profile(args.profile)
        commands = registry.commands_for_profile(profile)
        if registry.require_commands and not commands:
            raise TaskGitError(
                f"no enabled verification commands match profile {args.profile!r}; "
                f"review {verification_path_value or 'the project verification registry'}"
            )
        failures = 0
        if commands:
            for command in commands:
                repository_id = command.repository_id or None
                if repository_id:
                    selected = next(
                        (item for item in record.repositories if item.get("id") == repository_id),
                        None,
                    )
                    if selected is None:
                        raise TaskGitError(
                            f"verification command references repository absent from workspace: {repository_id}"
                        )
                    if selected.get("mutability") == "runtime_only":
                        raise TaskGitError(
                            f"verification command cannot execute in runtime-only repository {repository_id}"
                        )
                label = repository_id or "workspace"
                print(f"[{label}/{command.profile}] {command.command}")
                result = run_in_workspace(
                    record,
                    [command.command],
                    repository_id=repository_id,
                    shell=True,
                    environment=command.environment,
                    unset_environment=command.unset_environment,
                )
                if result.returncode != command.expected_returncode:
                    failures += 1
        else:
            # Schema-v1 compatibility. New projects should use verification.yaml.
            for repository in manifest.repositories:
                if repository.mutability == "runtime_only":
                    continue
                for command in repository.verify:
                    print(f"[{repository.id}/legacy] {command}")
                    result = run_in_workspace(
                        record, [command], repository_id=repository.id, shell=True
                    )
                    if result.returncode != 0:
                        failures += 1
            deployment = next(
                (item["id"] for item in record.repositories if item.get("role") in {"deployment", "integration"}),
                record.repositories[0]["id"],
            )
            for command in manifest.integration_verify:
                print(f"[integration/legacy] {command}")
                result = run_in_workspace(record, [command], repository_id=deployment, shell=True)
                if result.returncode != 0:
                    failures += 1
        return 1 if failures else 0

    if args.action in {"destroy", "stop"}:
        if args.action == "stop" and args.remove_shell:
            raise TaskGitError("workspace stop does not remove worktrees or the workspace shell")
        state_root = (
            args.state_dir.expanduser().resolve()
            if args.state_dir
            else xdg_state_home()
        )
        lifecycle = WorkspaceLifecycleService(
            root,
            state_root,
            archive_root=args.archive_root,
        )
        if args.action == "stop":
            result = lifecycle.stop(
                task_id,
                dry_run=args.dry_run,
                force=args.force,
            )
        else:
            result = lifecycle.destroy(
                task_id,
                remove_shell=args.remove_shell,
                dry_run=args.dry_run,
                force=args.force,
                require_archive=not args.force,
            )
        _print_workspace_lifecycle_report(result.report, overridden=args.force)
        for action in result.actions:
            prefix = "Would: " if args.dry_run else ""
            print(f"{prefix}{action}")
        if args.action == "stop":
            print(
                f"Workspace {task_id} runtime {'would be stopped' if args.dry_run else 'stopped'}; "
                "Git worktrees retained"
            )
        elif args.dry_run:
            print(f"Workspace {task_id} destruction preview complete")
        elif result.shell_removed:
            print(f"Workspace shell removed: {workspace_root}")
        else:
            print(f"Workspace {task_id} destroyed; generated shell retained")
        if args.force:
            print(
                "WARNING: force mode bypassed ordinary lifecycle policy checks; "
                "ownership and repository-route integrity remained mandatory. "
                "Inspect retained branches and runtime resources manually.",
                file=sys.stderr,
            )
        return 0
    raise TaskGitError(f"unsupported workspace action: {args.action}")



def _print_workspace_lifecycle_report(
    report: Any,
    *,
    overridden: bool = False,
) -> None:
    """Render deterministic stop/destroy preflight evidence."""

    print(f"Workspace {report.operation} preflight: {report.task_id}")
    for check in report.checks:
        marker = "PASS" if check.ok else ("WARN" if check.severity == "warning" else "FAIL")
        print(f"  [{marker}] {check.id}: {check.message}")
    if report.ok:
        result = "ready"
    elif overridden:
        result = "ordinary policy blocked; explicit force override applied"
    else:
        result = "blocked"
    print(f"Result: {result}")


def cmd_render(args: argparse.Namespace) -> int:
    from execraft.render import load_project, render_workspace

    project_dir = Path(args.project_dir).resolve()
    workspace_root = Path(args.workspace_root).resolve()
    if args.dry_run:
        print(json.dumps(load_project(project_dir), indent=2))
        print(f"Would render into: {workspace_root}")
        return 0
    result = render_workspace(
        project_dir,
        workspace_root,
        clean=args.clean,
        force_clean=args.force,
        task_id=args.task_id,
        policy_profile=args.policy,
    )
    print(f"Rendered {len(result.rendered_files)} files into {workspace_root}")
    return 0


def cmd_code(args: argparse.Namespace) -> int:
    root = repository_root()
    task_id = validate_task_id(args.task_id)
    record = load_workspace(root, task_id)
    manifest = load_manifest(root, task_id)
    project = load_registered_project(root, manifest.project)
    validate_task_against_project(manifest, project)
    lifecycle_validate_workspace_record_ownership(record, manifest, project)
    workspace_file = Path(record.workspace_root) / f"{record.task_id}.code-workspace"
    if not workspace_file.is_file():
        raise TaskGitError(f"generated workspace file is missing: {workspace_file}")
    command = ["code", "--new-window", str(workspace_file)]
    if args.dry_run:
        print(" ".join(command))
        return 0
    try:
        return subprocess.run(command, check=False).returncode
    except FileNotFoundError as exc:
        raise TaskGitError("VS Code command 'code' is not installed") from exc


def cmd_agents(args: argparse.Namespace) -> int:
    """Delegate agent health/diagnostic operations to the focused command module."""

    return run_agents_command(args)


def cmd_openclaw(args: argparse.Namespace) -> int:
    """Probe one configured OpenClaw runtime without enabling agent execution."""

    if args.action != "doctor":
        raise TaskGitError(f"unsupported OpenClaw action: {args.action}")
    root = repository_root()
    project = resolve_current_project(root, project_id=args.project_id)
    agents_path = _project_config_path(root, project, "agents_file")
    mapping = _load_yaml_mapping(agents_path, label="agent registry")
    try:
        execution = parse_execution_config(mapping, include_disabled=True)
    except AgentConfigError as exc:
        raise TaskGitError(str(exc)) from exc

    from execraft.runtime.openclaw_service import OpenClawGatewayService
    from execraft.runtime_config import OpenClawMode, RuntimeKind

    runtimes = [item for item in execution.runtimes if item.kind == RuntimeKind.OPENCLAW]
    if args.runtime_id:
        runtimes = [item for item in runtimes if item.id == args.runtime_id]
        if not runtimes:
            raise TaskGitError(f"unknown configured OpenClaw runtime: {args.runtime_id}")
    elif len(runtimes) > 1:
        choices = ", ".join(item.id for item in runtimes)
        raise TaskGitError(f"multiple OpenClaw runtimes configured; use --runtime ({choices})")
    if not runtimes:
        raise TaskGitError(f"no OpenClaw runtime configured in {agents_path}")

    runtime = runtimes[0]
    from execraft.runtime.openclaw_projection import (
        OpenClawProjectionError,
        project_openclaw_config,
    )

    try:
        projection = project_openclaw_config(execution, runtime.id)
    except OpenClawProjectionError as exc:
        raise TaskGitError(f"OpenClaw configuration projection failed: {exc}") from exc
    state_root = args.state_dir.expanduser().resolve() if args.state_dir else xdg_state_home()
    service = OpenClawGatewayService(
        runtime, state_root=state_root, config_payload=projection.config,
        credential_env_refs=projection.credential_refs,
    )
    try:
        result = (
            service.start()
            if runtime.openclaw is not None and runtime.openclaw.mode == OpenClawMode.MANAGED
            else service.probe()
        )
    finally:
        service.stop()
    from execraft.runtime.security_diagnostics import render_openclaw_doctor

    print(render_openclaw_doctor(execution, runtime.id, result, as_json=args.json))
    return 0 if result.healthy else 1


def cmd_doctor(args: argparse.Namespace) -> int:
    root = repository_root()
    failures: list[str] = []
    print(f"Execraft root: {root}")
    for required in ("git", sys.executable):
        executable = required if required != sys.executable else Path(required).name
        if shutil.which(str(executable)) is None and not Path(required).is_file():
            failures.append(f"missing required executable: {required}")
    projects = (
        [load_registered_project(root, args.project_id)]
        if args.project_id
        else list_projects(root)
    )
    print(f"validated projects: {', '.join(project.id for project in projects) or 'none'}")
    for optional in ("code", "codex", "claude", "opencode", "agy", "ollama", "docker"):
        state = "available" if shutil.which(optional) else "not installed"
        print(f"{optional}: {state}")
    if args.task_id:
        task_id = validate_task_id(args.task_id)
        record = load_workspace(root, task_id)
        manifest = load_manifest(root, task_id)
        project = load_registered_project(root, manifest.project)
        validate_task_against_project(manifest, project)
        lifecycle_validate_workspace_record_ownership(record, manifest, project)
        for finding in lifecycle_manifest_drift(Path(record.workspace_root)):
            failures.append(finding)
    for failure in failures:
        print(f"FAIL: {failure}", file=sys.stderr)
    print("doctor: healthy" if not failures else "doctor: problems found")
    return 1 if failures else 0


def _browser_run_state(runs_dir: Path, run_id: str) -> dict[str, Any]:
    path = runs_dir / "fake-state" / f"{run_id}.json"
    if not path.is_file():
        raise TaskGitError(f"browser run not found: {run_id}")
    return json.loads(path.read_text(encoding="utf-8"))



def _resolve_gui_task_id(root: Path, project_id: str, task_id: str | None, state_root: Path) -> str:
    if task_id:
        return validate_task_id(task_id)
    tasks_root = project_directory(root, project_id) / "tasks"
    if not tasks_root.is_dir():
        raise TaskGitError(f"project has no task dossiers: {tasks_root}")
    candidates: list[tuple[float, str]] = []
    for directory in tasks_root.iterdir():
        if not directory.is_dir() or not (directory / "TASK.yaml").is_file():
            continue
        state_path = resolve_storage_identity(
            state_root,
            project_id=project_id,
            task_id=directory.name,
            create=False,
        ).state_dir / "state.json"
        modified = state_path.stat().st_mtime if state_path.is_file() else directory.stat().st_mtime
        candidates.append((modified, directory.name))
    if not candidates:
        raise TaskGitError(f"project {project_id!r} has no task dossiers")
    return max(candidates)[1]


def cmd_gui(args: argparse.Namespace) -> int:
    from execraft.gui import (
        ActiveTaskRef,
        ControlCenterService,
        DashboardService,
        serve_dashboard,
    )

    home = ControlPlaneHome.resolve()
    root = home.root
    state_root = (
        args.state_dir.expanduser().resolve()
        if args.state_dir
        else xdg_state_home()
    )
    if not 0 <= args.port <= 65535:
        raise TaskGitError("gui --port must be between 0 and 65535")
    if not is_loopback_host(args.host):
        raise TaskGitError(
            "refusing to expose the dashboard beyond loopback; use an "
            "authenticated reverse proxy once remote dashboard support is enabled"
        )

    focused_project = ""
    initial_task = None
    if args.project_id or args.task_id:
        project = resolve_current_project(root, project_id=args.project_id)
        focused_project = project.id
        task_id = args.task_id
        if task_id:
            task_id = validate_task_id(task_id)
        elif args.project_id:
            # Preserve the historical --project behavior while allowing a
            # project with no tasks to open its onboarding home.
            try:
                task_id = _resolve_gui_task_id(root, project.id, None, state_root)
            except TaskGitError:
                task_id = None
        if task_id:
            initial_task = ActiveTaskRef(project.id, task_id)
    else:
        try:
            focused_project = resolve_current_project(root).id
        except ProjectError:
            focused_project = ""

    def task_dashboard_factory(project_id: str, task_id: str) -> DashboardService:
        return DashboardService(
            root=root,
            project_id=project_id,
            task_id=task_id,
            state_root=state_root,
        )

    service = ControlCenterService(
        home=home,
        task_dashboard_factory=task_dashboard_factory,
        focused_project_id=focused_project,
        initial_task=initial_task,
    )
    serve_dashboard(
        service=service,
        host=args.host,
        port=args.port,
        open_browser=args.open_browser,
    )
    return 0

def cmd_browser(args: argparse.Namespace) -> int:
    from execraft.browser import BrowserAgent, FakeBrowserAdapter, PlaywrightProbeAdapter

    workspace_root = args.workspace_root.resolve()
    runs_dir = workspace_root / ".execraft" / "runs"
    if args.adapter == "playwright":
        if args.action not in {"login", "probe"}:
            raise TaskGitError(
                "the Playwright adapter currently supports only non-destructive login/probe checks"
            )
        adapter = PlaywrightProbeAdapter()
    else:
        adapter = FakeBrowserAdapter(state_dir=runs_dir / "fake-state")
    agent = BrowserAgent(adapter, workspace_root, runs_dir=runs_dir)

    async def run() -> int:
        if args.action == "login":
            print(json.dumps(await agent.login(args.profile_dir), indent=2))
            return 0
        if args.action == "probe":
            result = await agent.probe()
            print(json.dumps(result.__dict__, indent=2))
            return 0 if result.available else 1
        if args.action == "prepare":
            if not args.task_id:
                raise TaskGitError("browser prepare requires --task-id")
            root = repository_root()
            record = load_workspace(root, validate_task_id(args.task_id))
            manifest = load_manifest(root, args.task_id)
            project = load_registered_project(root, manifest.project)
            validate_task_against_project(manifest, project)
            lifecycle_validate_workspace_record_ownership(record, manifest, project)
            task_repos = {
                item["id"]: Path(item["worktree_path"])
                for item in record.repositories
                if item.get("mutability", "task_owned") == "task_owned"
            }
            runtime_repos = {
                item["id"]: Path(item["worktree_path"])
                for item in record.repositories
                if item.get("mutability") == "runtime_only"
            }
            result = await agent.prepare(
                args.task_id,
                task_repos,
                project_task_directory(root, manifest.project, manifest.id),
                manifest=json.loads(
                    (workspace_root / ".execraft" / "generated-manifest.json").read_text(
                        encoding="utf-8"
                    )
                ),
                runtime_repos=runtime_repos,
            )
            print(
                json.dumps(
                    {"run_id": result.run_id, "archive_path": str(result.archive_path)},
                    indent=2,
                )
            )
            return 0
        if not args.run_id:
            raise TaskGitError(f"browser {args.action} requires --run-id")
        if args.action == "execute":
            result = await agent.execute(args.run_id)
            print(json.dumps(result.__dict__, indent=2))
            return 0 if result.success else 1
        if args.action == "status":
            result = await agent.status(args.run_id)
            print(json.dumps(result.__dict__, indent=2))
            return 0 if result.state != "failed" else 1
        if args.action == "apply":
            state = _browser_run_state(runs_dir, args.run_id)
            changes = list(state.get("changed_files") or [])
            if not changes:
                raise TaskGitError("browser run has no completed candidate changes")
            task_repos = set((state.get("bundle_manifest", {}).get("repos") or {}).keys())
            runtime_value = state.get("bundle_manifest", {}).get("runtime_repos") or []
            runtime_repos = set(runtime_value)
            task_id = str(state.get("bundle_manifest", {}).get("task_id", ""))
            if not task_id:
                raise TaskGitError("browser run has no task ID")
            root = repository_root()
            record = load_workspace(root, validate_task_id(task_id))
            if record.policy_profile == "read-only":
                raise TaskGitError("browser apply is disabled by the read-only policy")
            manifest = load_manifest(root, task_id)
            project = load_registered_project(root, manifest.project)
            validate_task_against_project(manifest, project)
            lifecycle_validate_workspace_record_ownership(record, manifest, project)
            repository_roots = {
                item["id"]: Path(item["worktree_path"])
                for item in record.repositories
                if item["id"] in task_repos
            }
            verify_commands: list[str | tuple[Path, str]] = []
            for repository in manifest.repositories:
                if repository.id not in repository_roots:
                    continue
                verify_commands.extend(
                    (repository_roots[repository.id], command)
                    for command in repository.verify
                )
            deployment = next(
                (
                    item
                    for item in record.repositories
                    if item.get("role") in {"deployment", "integration"}
                ),
                record.repositories[0],
            )
            verify_commands.extend(
                (Path(deployment["worktree_path"]), command)
                for command in manifest.integration_verify
            )
            verify_commands.extend(args.verify_command or [])
            result = await agent.apply(
                args.run_id,
                repository_roots,
                changes,
                task_repos,
                runtime_repos,
                state.get("bundle_manifest") or {},
                verify_commands,
                handoff_path=project_task_directory(root, manifest.project, manifest.id) / "HANDOFF.md",
            )
            print(
                json.dumps(
                    {
                        "message": result.message,
                        "verification_passed": result.verification_passed,
                    },
                    indent=2,
                )
            )
            return 0 if result.verification_passed else 1
        raise TaskGitError(f"unsupported browser action: {args.action}")

    return asyncio.run(run())


def cmd_plan(args: argparse.Namespace) -> int:
    if args.plan_file:
        plan_path = args.plan_file
    elif args.task_id:
        root = repository_root()
        manifest = load_manifest(root, validate_task_id(args.task_id))
        dossier = project_task_directory(root, manifest.project, manifest.id)
        graph_path = dossier / "PLAN.graph.yaml"
        plan_path = graph_path if graph_path.is_file() else dossier / "PLAN.md"
        if not plan_path.is_file():
            raise TaskGitError(f"plan file not found for task {args.task_id}")
    else:
        raise TaskGitError("plan requires --file or --task-id")

    graph, report = load_plan_graph_file(plan_path)
    if report.has_errors():
        if report.cycles_detected:
            for finding in report.cycles_detected:
                print(f"CYCLE: {finding}", file=sys.stderr)
        for error in report.errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print(f"Normalized {report.packages_found} work packages from plan")
    for wp in graph.work_packages:
        print(f"  {wp.id}: {wp.title}")
        print(f"    dependencies: {', '.join(wp.dependencies) or 'none'}")
        print(f"    acceptance criteria: {len(wp.acceptance_criteria)}")
        print(f"    risk: {wp.risk}, priority: {wp.priority}")

    if args.action == "validate":
        completeness = graph.validate_completeness()
        if completeness:
            for finding in completeness:
                print(f"VALIDATION: {finding}", file=sys.stderr)
            return 1
        print("Plan graph validation: PASS")
    return 0


def _capabilities(raw: Any) -> set[AgentCapability]:
    if not isinstance(raw, list):
        raise TaskGitError("agent capabilities must be a list")
    result: set[AgentCapability] = set()
    for item in raw:
        try:
            result.add(AgentCapability(str(item)))
        except ValueError as exc:
            raise TaskGitError(f"unsupported agent capability: {item!r}") from exc
    return result


def _register_project_agents(
    orchestrator: ProjectOrchestrator,
    config: dict[str, Any],
    *,
    workspace_root: Path,
    policy_profile: str,
    opencode_registry: OpenCodeProviderRegistry | None = None,
    endpoint_probe=probe_openai_compatible_endpoint,
    state_root: Path | None = None,
) -> list[str]:
    read_only = policy_profile == "read-only"
    try:
        execution = parse_execution_config(config, read_only=read_only)
    except AgentConfigError as exc:
        raise TaskGitError(str(exc)) from exc

    registry = opencode_registry or OpenCodeProviderRegistry()
    from execraft.render import refresh_opencode_providers
    from execraft.agents.runtime_candidates import build_runtime_candidates
    from execraft.runtime.subagent_policy import parse_subagent_strategy

    refresh_opencode_providers(workspace_root, registry)
    model_registry = registry.model_registry
    subagent_strategy = parse_subagent_strategy(config)
    try:
        candidates = build_runtime_candidates(
            execution,
            workdir=workspace_root,
            state_root=(state_root or xdg_state_home()),
            read_only=read_only,
            opencode_config_path=workspace_root / "opencode.json",
            model_registry=model_registry,
            subagent_strategy=subagent_strategy,
        )
    except (AgentConfigError, ValueError) as exc:
        raise TaskGitError(str(exc)) from exc
    endpoints = {
        candidate.provider_id: model_registry.endpoint_for_model(candidate.model)
        for candidate in candidates
        if str(getattr(candidate, "adapter_name", "")) == "opencode"
    }
    endpoints = {key: value for key, value in endpoints.items() if value is not None}
    initial_probes: dict[str, Any] = {}
    if endpoints:
        def safe_probe(endpoint):
            try:
                return endpoint_probe(endpoint)
            except Exception as exc:  # pragma: no cover - defensive plugin boundary
                return {"reachable": False, "models": (), "error": str(exc)}

        with ThreadPoolExecutor(max_workers=min(4, len(endpoints))) as executor:
            futures = {
                provider_id: executor.submit(safe_probe, endpoint)
                for provider_id, endpoint in endpoints.items()
            }
            initial_probes = {
                provider_id: future.result()
                for provider_id, future in futures.items()
            }

    registered: list[str] = []
    for candidate in candidates:
        endpoint = endpoints.get(candidate.provider_id)
        configure_endpoint_probe = getattr(candidate, "configure_endpoint_probe", None)
        if endpoint is not None and callable(configure_endpoint_probe):
            configure_endpoint_probe(
                lambda endpoint=endpoint: endpoint_probe(endpoint),
                model_id=registry.model_id(candidate.model),
                initial_result=initial_probes[candidate.provider_id],
            )
        aliases = execution.agent(candidate.candidate_id).aliases
        orchestrator.register_agent(candidate, aliases=aliases)
        registered.append(f"{candidate.provider_id}:{candidate.availability.value}")
    return registered


def _build_orchestrator(args: argparse.Namespace) -> tuple[ProjectOrchestrator, str, list[str], Path | None]:
    root = repository_root()
    project = resolve_current_project(root, project_id=args.project_id)
    args.project_id = project.id
    task_id = validate_task_id(args.task_id or project.id)

    try:
        record = load_workspace(root, task_id)
    except TaskGitError:
        if args.action in {"status", "explain", "transition"}:
            record = None
        else:
            raise TaskGitError(
                f"orchestration requires a ready workspace for task {task_id!r}; "
                f"run 'execraft workspace start {task_id} ...' first"
            )

    repository_paths: dict[str, Path] = {}
    workspace_root: Path | None = None
    policy_profile = project.default_policy
    workspace_roots: list[Path] = []
    if record is not None:
        workspace_root = Path(record.workspace_root).resolve()
        workspace_roots = [workspace_root]
        policy_profile = record.policy_profile
        repository_paths = {str(item["id"]): Path(item["worktree_path"]).resolve() for item in record.repositories}

    verification_path = _project_config_path(root, project, "verification_file")
    registry = VerificationRegistry.load(verification_path) if verification_path else VerificationRegistry()
    if args.action not in {"status", "trace", "sync", "explain", "transition"} and registry.require_commands and not registry.commands:
        raise TaskGitError(f"verification registry contains no commands: {verification_path}")

    resources_path = _project_config_path(root, project, "resources_file")
    resource_policy = load_resource_policy(resources_path)

    agents_path = _project_config_path(root, project, "agents_file")
    agents_mapping: dict[str, Any] = {}
    if args.action not in {"status", "trace", "sync", "explain", "transition"}:
        agents_mapping = _load_yaml_mapping(agents_path, label="agent registry")

    config = build_orchestration_config(
        agents_mapping,
        state_dir=args.state_dir,
        heartbeat_interval=args.heartbeat_interval,
    )
    resource_manager = (
        ResourceManager(
            policy=resource_policy,
            workspace_retirement=WorkspaceLifecycleService(root, config.state_dir),
        )
        if args.action == "daemon"
        else None
    )
    verification_environment: dict[str, str] = {}
    if record is not None and workspace_root is not None:
        from execraft.workspace.workspace_git import load_env_file

        verification_environment = load_env_file(workspace_root / record.env_file)

    progress_log_path: Path | None = None
    progress_callback = None
    if args.action in {"run", "daemon"}:
        storage_identity = resolve_storage_identity(
            config.state_dir,
            project_id=project.id,
            task_id=task_id,
        )
        progress_log_path = (
            args.log_file.expanduser().resolve()
            if args.log_file
            else storage_identity.state_dir / "orchestrator.log"
        )
        progress_callback = HumanProgressReporter(
            stream=None if args.quiet else sys.stdout,
            log_path=progress_log_path,
        )

    skills_dir = project.configured_path("skills_dir") or project.directory / "skills"
    try:
        skill_catalog = SkillCatalog.load(project_skills_dir=skills_dir)
        if config.supervisor_policy.enabled:
            skill_catalog.materialize(
                "supervise", [config.supervisor_policy.skill_id]
            )
    except SkillCatalogError as exc:
        raise TaskGitError(f"invalid workflow skill catalog: {exc}") from exc

    orchestrator = ProjectOrchestrator(
        task_id,
        config=config,
        registry=registry,
        resource_manager=resource_manager,
        workspace_roots=workspace_roots,
        repository_paths=repository_paths,
        repository_requirements={
            repository.id: repository.required
            for repository in project.repositories
        },
        workspace_root=workspace_root,
        verification_environment=verification_environment,
        progress_callback=progress_callback,
        task_dossier_dir=project_task_directory(root, project.id, task_id),
        skill_catalog=skill_catalog,
        project_namespace=project.id,
    )

    agents: list[str] = []
    if args.action not in {"status", "trace", "explain", "transition"}:
        if workspace_root is None:
            raise TaskGitError("workspace root is unavailable")
        agents = _register_project_agents(
            orchestrator,
            agents_mapping,
            workspace_root=workspace_root,
            policy_profile=policy_profile,
            opencode_registry=_load_project_opencode_registry(root, project),
        )
        if not agents:
            raise TaskGitError(f"no enabled agents in {agents_path}")
        supervisor = orchestrator.supervisor_status_report()
        if (
            config.supervisor_policy.enabled
            and supervisor.get("invalid_configured_agents")
        ):
            raise TaskGitError(
                "each configured supervisor agent must be an enabled Codex, "
                "Claude, or Antigravity provider with the supervise capability: "
                + ", ".join(supervisor["invalid_configured_agents"])
            )
        missing_capabilities = [
            capability.value
            for capability in (
                AgentCapability.IMPLEMENT,
                AgentCapability.REVIEW,
                AgentCapability.FIX_REVIEW,
                *(
                    (AgentCapability.DECOMPOSE,)
                    if config.auto_decompose_enabled
                    else ()
                ),
            )
            if not orchestrator.has_configured_capability(capability)
        ]
        if missing_capabilities:
            raise TaskGitError(
                "no enabled agent is configured for required orchestration "
                "capabilities: " + ", ".join(missing_capabilities)
            )
    return orchestrator, task_id, agents, progress_log_path





def cmd_orchestrate(args: argparse.Namespace) -> int:
    """Compose dependencies and delegate orchestration command behavior."""

    return run_orchestrate_command(
        args,
        build_orchestrator=_build_orchestrator,
        complete_finished_orchestration=_complete_finished_orchestration,
        run_until_terminal_fn=run_until_terminal,
    )

if __name__ == "__main__":
    sys.exit(main())
