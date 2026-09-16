"""Reusable workspace preparation for CLI and onboarding workflows.

The historical implementation lived inside :mod:`execraft.cli`, which made the
safe worktree transaction unavailable to other application surfaces.  This
module owns workspace creation, validation, rendering, and rollback while the
CLI is reduced to input/output translation.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yaml

from execraft.project import (
    ProjectDescriptor,
    ProjectRegistration,
    load_project_registration,
    load_registered_project,
    register_project_descriptor,
    remove_project_registration,
    resolve_project_source_root,
    validate_task_against_project,
    write_project_binding,
)
from execraft.workspace.task_git import (
    RepositorySpec,
    TaskGitError,
    TaskManifest,
    current_branch,
    git,
    project_task_directory,
)
from execraft.workspace.workspace_git import (
    WorkspaceRecord,
    add_worktree,
    allocate_runtime,
    default_workspace_path,
    load_workspace,
    remove_worktree,
    sanitize_compose_project,
    write_env_file,
    write_workspace,
)

WorkspaceEvent = Callable[[str], None]
WorkspaceRenderer = Callable[..., None]


@dataclass(frozen=True)
class WorkspacePreparationResult:
    """Result of an idempotent workspace preparation request."""

    record: WorkspaceRecord
    created: bool
    binding_path: Path | None = None

    @property
    def workspace_root(self) -> Path:
        return Path(self.record.workspace_root).resolve()


def project_source_path(
    source_root: Path,
    project: ProjectDescriptor,
    repository_id: str,
) -> Path:
    """Resolve and confine one catalog repository below its source root."""

    relative = project.repository(repository_id).path
    source_root = source_root.resolve()
    relative_path = Path(relative)
    if relative_path.parts and relative_path.parts[0] == source_root.name:
        candidate = (source_root / Path(*relative_path.parts[1:])).resolve()
    else:
        candidate = (source_root / relative_path).resolve()
    try:
        candidate.relative_to(source_root)
    except ValueError as exc:
        raise TaskGitError(f"repository path escapes source root: {relative}") from exc
    return candidate


def active_task_path(workspace_root: Path) -> Path:
    return workspace_root.resolve() / ".execraft" / "active-task"


def render_workspace_record(
    control_root: Path,
    manifest: TaskManifest,
    record: WorkspaceRecord,
    *,
    clean: bool,
) -> None:
    """Render provider/editor assets and runtime environment for a workspace."""

    from execraft.render import render_workspace

    project = load_registered_project(control_root, manifest.project)
    workspace_root = Path(record.workspace_root)
    render_workspace(
        project.directory,
        workspace_root,
        clean=clean,
        task_id=manifest.id,
        repositories=record.repositories,
        dossier_dir=project_task_directory(control_root, manifest.project, manifest.id),
        policy_profile=record.policy_profile,
        force_clean=clean
        and not (workspace_root / ".execraft" / "workspace.yaml").is_file(),
    )
    write_env_file(workspace_root, record)
    active_task_path(workspace_root).write_text(f"{manifest.id}\n", encoding="utf-8")


def validate_workspace_location(
    control_root: Path,
    source_root: Path,
    workspace_root: Path,
    task_id: str,
) -> bool:
    """Return whether an existing shell is owned; reject unsafe overlap."""

    workspace_root = workspace_root.resolve()
    for protected, label in (
        (control_root.resolve(), "control-plane home"),
        (source_root.resolve(), "project source root"),
    ):
        if workspace_root == protected or protected in workspace_root.parents:
            raise TaskGitError(f"workspace root must be outside the {label}: {workspace_root}")
    if not workspace_root.exists() or not any(workspace_root.iterdir()):
        return False
    marker = workspace_root / ".execraft" / "workspace.yaml"
    if not marker.is_file():
        raise TaskGitError(
            f"workspace root is non-empty and has no Execraft ownership marker: {workspace_root}"
        )
    data = yaml.safe_load(marker.read_text(encoding="utf-8")) or {}
    if (
        data.get("task_id") != task_id
        or Path(str(data.get("workspace_root", ""))).resolve() != workspace_root
    ):
        raise TaskGitError(
            f"workspace ownership marker does not match {task_id}: {workspace_root}"
        )
    return True


def create_workspace_entries(
    manifest: TaskManifest,
    project: ProjectDescriptor,
    source_root: Path,
    workspace_root: Path,
    *,
    reuse_in_place: bool,
    event: WorkspaceEvent | None = None,
) -> list[dict[str, str]]:
    """Create task worktrees and roll back every partial creation on failure."""

    emit = event or (lambda _message: None)
    prepared: list[tuple[RepositorySpec, Path]] = []
    for repository in manifest.repositories:
        source = project_source_path(source_root, project, repository.id)
        if not source.exists():
            if repository.required:
                raise TaskGitError(
                    f"required repository {repository.id} is missing at {source}"
                )
            emit(f"Skip optional {repository.id}: {source} does not exist")
            continue
        prepared.append((repository, source))

    entries: list[dict[str, str]] = []
    created_worktrees: list[tuple[Path, Path, str, bool]] = []
    try:
        for repository, source in prepared:
            catalog = project.repository(repository.id)
            created = False
            if repository.mutability == "runtime_only":
                destination = source
                emit(f"Reference runtime-only {repository.id} at {source}")
            elif reuse_in_place and current_branch(source) == repository.task_branch:
                destination = source
                emit(f"Reuse {repository.id} in-place by explicit request: {source}")
            else:
                destination = workspace_root / catalog.workspace_name
                created = add_worktree(
                    source,
                    destination,
                    repository.task_branch,
                    repository.base_branch,
                )
                created_worktrees.append(
                    (source, destination, repository.task_branch, created)
                )
                action = "Created branch and worktree" if created else "Created worktree"
                emit(f"{action} for {repository.id}: {destination}")
            entries.append(
                {
                    "id": repository.id,
                    "source_path": str(source),
                    "worktree_path": str(destination),
                    "branch": repository.task_branch,
                    "role": repository.role,
                    "mutability": repository.mutability,
                    "created_branch": "true" if created else "false",
                }
            )
    except (OSError, TaskGitError):
        for source, destination, branch, created_branch in reversed(created_worktrees):
            if destination.exists():
                remove_worktree(source, destination, force=True)
            if created_branch:
                git(source, "branch", "-D", branch, check=False)
        raise
    return entries


def validate_workspace_record_ownership(
    record: WorkspaceRecord,
    manifest: TaskManifest,
    project: ProjectDescriptor,
) -> None:
    """Reject stale workspace routing and policy before any operation."""

    if record.task_id != manifest.id:
        raise TaskGitError(
            f"workspace task {record.task_id!r} does not match manifest {manifest.id!r}"
        )
    allowed_policies = set(project.policy_profiles) or {project.default_policy}
    if record.policy_profile not in allowed_policies:
        raise TaskGitError(
            f"workspace policy {record.policy_profile!r} is no longer declared by "
            f"project {project.id!r}"
        )
    source_root = Path(record.source_root).resolve()
    workspace_root = Path(record.workspace_root).resolve()
    task = {repository.id: repository for repository in manifest.repositories}
    record_ids = [str(item.get("id", "")) for item in record.repositories]
    if len(record_ids) != len(set(record_ids)):
        raise TaskGitError("workspace contains duplicate repository IDs")
    recorded = dict(zip(record_ids, record.repositories, strict=True))
    unknown = sorted(set(recorded) - set(task))
    if unknown:
        raise TaskGitError(
            "workspace contains repositories absent from its task: " + ", ".join(unknown)
        )
    missing = sorted(
        repository_id
        for repository_id, repository in task.items()
        if repository.required and repository_id not in recorded
    )
    if missing:
        raise TaskGitError(
            "workspace omits required task repositories: " + ", ".join(missing)
        )
    for repository_id, item in recorded.items():
        repository = task[repository_id]
        catalog = project.repository(repository_id)
        recorded_mutability = item.get("mutability", "task_owned")
        if recorded_mutability != repository.mutability:
            raise TaskGitError(
                f"workspace mutability for {repository_id!r} is stale: "
                f"recorded {recorded_mutability!r}, task requires {repository.mutability!r}"
            )
        if item.get("role", "component") != repository.role:
            raise TaskGitError(f"workspace role for {repository_id!r} is stale")
        if item.get("branch") != repository.task_branch:
            raise TaskGitError(
                f"workspace branch metadata for {repository_id!r} is stale: "
                f"recorded {item.get('branch')!r}, task requires {repository.task_branch!r}"
            )
        expected_source = project_source_path(source_root, project, repository_id).resolve()
        recorded_source = Path(str(item.get("source_path", ""))).resolve()
        if recorded_source != expected_source:
            raise TaskGitError(
                f"workspace source route for {repository_id!r} is stale: "
                f"recorded {recorded_source}, catalog requires {expected_source}"
            )
        recorded_worktree = Path(str(item.get("worktree_path", ""))).resolve()
        generated_worktree = (workspace_root / catalog.workspace_name).resolve()
        allowed_worktrees = (
            {expected_source}
            if repository.mutability == "runtime_only"
            else {expected_source, generated_worktree}
        )
        if recorded_worktree not in allowed_worktrees:
            raise TaskGitError(
                f"workspace route for {repository_id!r} is stale or unauthorized: "
                f"{recorded_worktree}"
            )
        if (
            repository.mutability == "task_owned"
            and current_branch(recorded_worktree) != repository.task_branch
        ):
            raise TaskGitError(
                f"workspace repository {repository_id!r} is not on task branch "
                f"{repository.task_branch!r}"
            )


def _restore_project_registration(
    project_id: str,
    registration: ProjectRegistration | None,
) -> None:
    """Restore the host-local project route after workspace setup failure."""

    if registration is None:
        remove_project_registration(project_id)
        return
    descriptor = registration.descriptor
    source_root = registration.source_root
    if descriptor is not None:
        register_project_descriptor(
            Path(descriptor),
            source_root=Path(source_root) if source_root is not None else None,
            replace=True,
        )
        return
    if source_root is not None:
        # Schema-v1 source-only bindings are preserved through the compatibility
        # writer.  No descriptor existed before the attempted workspace setup.
        remove_project_registration(project_id)
        write_project_binding(project_id, Path(source_root))
        return
    remove_project_registration(project_id)


def prepare_workspace(
    *,
    control_root: Path,
    manifest: TaskManifest,
    project: ProjectDescriptor,
    source_root_override: Path | None = None,
    workspace_root: Path | None = None,
    reuse_in_place: bool = False,
    policy_profile: str | None = None,
    bind_source: bool = True,
    reuse_existing: bool = True,
    event: WorkspaceEvent | None = None,
    renderer: WorkspaceRenderer | None = None,
) -> WorkspacePreparationResult:
    """Prepare or reuse a safe task workspace.

    Publication is transactional with respect to generated worktrees, rendered
    assets, and the workspace registry.  A ready existing record is reused only
    after full ownership validation and exact source/workspace route matching.
    """

    emit = event or (lambda _message: None)
    validate_task_against_project(manifest, project)
    source_root = resolve_project_source_root(project, source_root_override)
    target = (
        workspace_root.expanduser().resolve()
        if workspace_root is not None
        else default_workspace_path(control_root, manifest.id, project_id=project.id)
    )
    selected_policy = policy_profile or project.default_policy
    allowed_policies = set(project.policy_profiles) or {project.default_policy}
    if selected_policy not in allowed_policies:
        raise TaskGitError(
            f"policy profile {selected_policy!r} is not declared by project {project.id}"
        )

    if reuse_existing:
        try:
            existing = load_workspace(control_root, manifest.id)
        except TaskGitError:
            existing = None
        if existing is not None and existing.status == "ready":
            validate_workspace_record_ownership(existing, manifest, project)
            if Path(existing.source_root).resolve() != source_root:
                raise TaskGitError(
                    "existing workspace source root differs: "
                    f"{existing.source_root} != {source_root}"
                )
            if Path(existing.workspace_root).resolve() != target:
                raise TaskGitError(
                    f"existing workspace path differs: {existing.workspace_root} != {target}"
                )
            if existing.policy_profile != selected_policy:
                raise TaskGitError(
                    "existing workspace policy differs: "
                    f"{existing.policy_profile} != {selected_policy}"
                )
            emit(f"Reuse ready workspace: {target}")
            return WorkspacePreparationResult(record=existing, created=False)

    shell_preexisting = validate_workspace_location(
        control_root,
        source_root,
        target,
        manifest.id,
    )
    previous_registration = load_project_registration(project.id)
    binding_path = None
    entries: list[dict[str, str]] = []
    try:
        if source_root_override is not None and bind_source:
            binding_path = write_project_binding(project.id, source_root)
        target.mkdir(parents=True, exist_ok=True)
        domain_id, port_offset = allocate_runtime(
            control_root,
            manifest.id,
            project.capabilities,
        )
        entries = create_workspace_entries(
            manifest,
            project,
            source_root,
            target,
            reuse_in_place=reuse_in_place,
            event=emit,
        )
        record = WorkspaceRecord(
            schema_version=1,
            task_id=manifest.id,
            created_at=manifest.created_at,
            source_root=str(source_root),
            workspace_root=str(target),
            compose_project=(
                sanitize_compose_project(manifest.id)
                if "runtime.compose" in project.capabilities
                else ""
            ),
            ros_domain_id=domain_id,
            port_offset=port_offset,
            env_file=".execraft/runtime.env",
            repositories=entries,
            status="ready",
            policy_profile=selected_policy,
            capabilities=list(project.capabilities),
        )
        render = renderer or render_workspace_record
        render(control_root, manifest, record, clean=True)
        write_workspace(control_root, record)
    except Exception:
        for item in reversed(entries):
            if item.get("mutability", "task_owned") != "task_owned":
                continue
            source = Path(item["source_path"])
            destination = Path(item["worktree_path"])
            if destination != source and destination.exists():
                remove_worktree(source, destination, force=True)
            if item.get("created_branch") == "true":
                git(source, "branch", "-D", item["branch"], check=False)
        if not shell_preexisting and target.exists():
            shutil.rmtree(target, ignore_errors=True)
        if source_root_override is not None and bind_source:
            _restore_project_registration(project.id, previous_registration)
        raise
    return WorkspacePreparationResult(
        record=record,
        created=True,
        binding_path=binding_path,
    )


def manifest_drift(workspace_root: Path) -> list[str]:
    """Return modified or missing generated workspace assets."""

    import hashlib

    manifest_path = workspace_root / ".execraft" / "generated-manifest.json"
    if not manifest_path.is_file():
        return ["generated manifest is missing"]
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    findings: list[str] = []
    for relative, metadata in (data.get("files") or {}).items():
        path = workspace_root / relative
        if not path.is_file():
            findings.append(f"missing generated file: {relative}")
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != metadata.get("sha256"):
            findings.append(f"modified generated file: {relative}")
    return findings
