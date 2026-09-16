"""Shared task-manifest and multi-repository Git helpers for the AI workflow."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml

from execraft.control_plane import ControlPlaneHome
from execraft.persistence.atomic import atomic_write_text, atomic_write_yaml


TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
REPOSITORY_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
PROTECTED_BRANCH_PATTERNS = (
    re.compile(r"^(main|master|develop)$"),
    re.compile(r"^release(?:/|$)"),
)
TASK_STATUSES = {
    "draft",
    "briefed",
    "planned",
    "in_progress",
    "review",
    "approved",
    "integrating",
    "merged",
    "closed",
    "blocked",
    "abandoned",
}


class TaskGitError(RuntimeError):
    """Raised when task metadata or a coordinated Git operation is unsafe."""


@dataclass
class RepositorySpec:
    id: str
    path: str = ""
    base_branch: str = ""
    task_branch: str = ""
    role: str = "component"
    required: bool = True
    start_commit: str = ""
    verify: list[str] = field(default_factory=list)
    latest_commit: str = ""
    mutability: str = "task_owned"

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "RepositorySpec":
        required = data.get("required", True)
        verify = data.get("verify") or []
        if not isinstance(verify, list) or not all(isinstance(item, str) for item in verify):
            raise TaskGitError("repository verify must be a list of shell command strings")
        return cls(
            id=str(data.get("id", "")).strip(),
            path=str(data.get("path", "")).strip(),
            base_branch=str(data.get("base_branch", "")).strip(),
            task_branch=str(data.get("task_branch", "")).strip(),
            role=str(data.get("role", "component")).strip() or "component",
            required=bool(required),
            start_commit=str(data.get("start_commit", "")).strip(),
            verify=list(verify),
            latest_commit=str(data.get("latest_commit", "")).strip(),
            mutability=str(data.get("mutability", "task_owned")).strip() or "task_owned",
        )

    def as_mapping(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "base_branch": self.base_branch,
            "task_branch": self.task_branch,
            "role": self.role,
            "required": self.required,
            "start_commit": self.start_commit,
        }
        if self.path:
            result["path"] = self.path
        if self.verify:
            result["verify"] = list(self.verify)
        if self.latest_commit:
            result["latest_commit"] = self.latest_commit
        if self.mutability != "task_owned":
            result["mutability"] = self.mutability
        return result


@dataclass
class TaskManifest:
    schema_version: int
    id: str
    project: str = ""
    title: str = ""
    status: str = ""
    created_at: str = ""
    branch_name: str = ""
    merge_strategy: str = "squash"
    integration_branch: str = ""
    repositories: list[RepositorySpec] = field(default_factory=list)
    integration_verify: list[str] = field(default_factory=list)
    last_updated: str = ""

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "TaskManifest":
        git_data = data.get("git") or {}
        integration = data.get("integration") or {}
        repositories_data = data.get("repositories") or []
        if not isinstance(git_data, Mapping):
            raise TaskGitError("TASK.yaml git must be a mapping")
        if not isinstance(integration, Mapping):
            raise TaskGitError("TASK.yaml integration must be a mapping")
        if not isinstance(repositories_data, list):
            raise TaskGitError("TASK.yaml repositories must be a list")
        integration_verify = integration.get("verify") or []
        if not isinstance(integration_verify, list) or not all(
            isinstance(item, str) for item in integration_verify
        ):
            raise TaskGitError("TASK.yaml integration.verify must be a list of commands")
        manifest = cls(
            schema_version=int(data.get("schema_version", 0)),
            id=str(data.get("id", "")).strip(),
            project=str(data.get("project", "")).strip(),
            title=str(data.get("title", "")).strip(),
            status=str(data.get("status", "")).strip(),
            created_at=str(data.get("created_at", "")).strip(),
            last_updated=str(data.get("last_updated", "")).strip(),
            branch_name=str(git_data.get("branch_name", "")).strip(),
            merge_strategy=str(git_data.get("merge_strategy", "squash")).strip(),
            integration_branch=str(
                git_data.get("integration_branch", "")
            ).strip(),
            repositories=[RepositorySpec.from_mapping(item) for item in repositories_data],
            integration_verify=list(integration_verify),
        )
        validate_manifest(manifest)
        return manifest

    def as_mapping(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "created_at": self.created_at,
            "last_updated": self.last_updated or utc_now(),
            "git": {
                "branch_name": self.branch_name,
                "merge_strategy": self.merge_strategy,
            },
            "repositories": [repository.as_mapping() for repository in self.repositories],
            "integration": {"verify": list(self.integration_verify)},
        }
        if self.project:
            result["project"] = self.project
        if self.integration_branch:
            result["git"]["integration_branch"] = self.integration_branch
        return result


@dataclass(frozen=True)
class RepositoryState:
    repository: RepositorySpec
    path: Path
    branch: str
    head: str
    dirty: bool
    operation: str | None


@dataclass(frozen=True)
class CheckoutStep:
    repository: RepositorySpec
    path: Path
    previous_branch: str
    previous_head: str
    destination_branch: str
    created_branch: bool = False


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def repository_root() -> Path:
    """Return the active control-plane home.

    The historical function name is retained because it is part of the
    internal API, but the returned directory no longer has to be a Git source
    checkout.  A normal wheel installation receives an XDG-backed writable
    home, while explicit and legacy environment variables remain supported.
    """

    return ControlPlaneHome.resolve().ensure_layout().root


def validate_task_id(task_id: str) -> str:
    value = task_id.strip()
    if not TASK_ID_RE.fullmatch(value):
        raise TaskGitError(
            "task_id must start with a lowercase letter or digit and contain only "
            "lowercase letters, digits, '.', '_' or '-'"
        )
    return value


def validate_branch_name(branch: str, *, label: str = "branch") -> str:
    value = branch.strip()
    if not value or not BRANCH_RE.fullmatch(value) or ".." in value or "//" in value:
        raise TaskGitError(f"invalid {label}: {branch!r}")
    if value.endswith("/") or value.endswith(".") or value.startswith("-"):
        raise TaskGitError(f"invalid {label}: {branch!r}")
    return value


def is_protected_branch(branch: str) -> bool:
    return any(pattern.fullmatch(branch) or pattern.match(branch) for pattern in PROTECTED_BRANCH_PATTERNS)


def run(
    args: Sequence[str],
    *,
    cwd: Path,
    check: bool = True,
    capture: bool = True,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command_env = os.environ.copy()
    command_env.setdefault("GIT_TERMINAL_PROMPT", "0")
    if env:
        command_env.update(env)
    completed = subprocess.run(
        list(args),
        cwd=cwd,
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        env=command_env,
    )
    if check and completed.returncode != 0:
        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        detail = "\n".join(item for item in (stdout, stderr) if item)
        raise TaskGitError(
            f"command failed ({completed.returncode}) in {cwd}: {' '.join(args)}"
            + (f"\n{detail}" if detail else "")
        )
    return completed


def git(path: Path, *args: str, check: bool = True) -> str:
    completed = run(("git", *args), cwd=path, check=check, capture=True)
    return (completed.stdout or "").strip()


def ensure_git_repository(path: Path, *, label: str) -> None:
    result = run(
        ("git", "rev-parse", "--is-inside-work-tree"),
        cwd=path,
        check=False,
        capture=True,
    )
    if result.returncode != 0 or (result.stdout or "").strip() != "true":
        raise TaskGitError(f"{label} is not a Git working tree: {path}")


def resolve_repository_path(root: Path, value: str) -> Path:
    candidate = (root / value).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise TaskGitError(f"repository path must stay inside the stack root: {value!r}") from exc
    return candidate


def resolve_repository_path_v2(root: Path, manifest: TaskManifest, repository_id: str) -> Path:
    if manifest.schema_version < 2 or not manifest.project:
        raise TaskGitError("resolve_repository_path_v2 requires a v2 manifest with a project")
    try:
        repo = lookup_repository(manifest, repository_id)
    except TaskGitError:
        raise TaskGitError(f"repository {repository_id!r} not found in manifest")
    if repo.path:
        return resolve_repository_path(root, repo.path)
    project_data = load_project_yaml(root, manifest.project)
    repo_rel = project_repository_path(project_data, repository_id)
    return resolve_repository_path(root, repo_rel)


def current_branch(path: Path) -> str:
    branch = git(path, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if not branch:
        raise TaskGitError(f"detached HEAD is not supported for coordinated tasks: {path}")
    return branch


def head_commit(path: Path, ref: str = "HEAD") -> str:
    value = git(path, "rev-parse", "--verify", f"{ref}^{{commit}}", check=False)
    if not value:
        raise TaskGitError(f"Git commit does not exist in {path}: {ref}")
    return value


def branch_exists(path: Path, branch: str) -> bool:
    return bool(git(path, "show-ref", "--verify", f"refs/heads/{branch}", check=False))


def working_tree_dirty(path: Path) -> bool:
    return bool(git(path, "status", "--porcelain=v1", "--untracked-files=all"))


def git_operation(path: Path) -> str | None:
    git_dir_raw = git(path, "rev-parse", "--git-dir")
    git_dir = Path(git_dir_raw)
    if not git_dir.is_absolute():
        git_dir = (path / git_dir).resolve()
    markers = (
        ("MERGE_HEAD", "merge"),
        ("CHERRY_PICK_HEAD", "cherry-pick"),
        ("REVERT_HEAD", "revert"),
        ("BISECT_LOG", "bisect"),
        ("rebase-merge", "rebase"),
        ("rebase-apply", "rebase"),
    )
    for marker, label in markers:
        if (git_dir / marker).exists():
            return label
    return None


def repository_state(root: Path, repository: RepositorySpec, *, manifest: TaskManifest | None = None) -> RepositoryState | None:
    if repository.path:
        path = resolve_repository_path(root, repository.path)
    elif manifest and manifest.schema_version >= 2 and manifest.project:
        path = resolve_repository_path_v2(root, manifest, repository.id)
    else:
        raise TaskGitError(f"repository {repository.id} has no path and no manifest/project context")
    if not path.exists():
        if repository.required:
            raise TaskGitError(f"required repository is missing: {repository.id} ({path})")
        return None
    ensure_git_repository(path, label=repository.id)
    return RepositoryState(
        repository=repository,
        path=path,
        branch=current_branch(path),
        head=head_commit(path),
        dirty=working_tree_dirty(path),
        operation=git_operation(path),
    )


def require_clean_state(state: RepositoryState, *, purpose: str) -> None:
    if state.operation:
        raise TaskGitError(
            f"cannot {purpose}: {state.repository.id} has an active {state.operation} operation"
        )
    if state.dirty:
        raise TaskGitError(
            f"cannot {purpose}: {state.repository.id} has uncommitted changes in {state.path}"
        )


def task_directory(root: Path, task_id: str) -> Path:
    return root / "docs" / "ai" / "tasks" / validate_task_id(task_id)


def task_manifest_path(root: Path, task_id: str) -> Path:
    return task_directory(root, task_id) / "TASK.yaml"


def project_task_directory(root: Path, project: str, task_id: str) -> Path:
    # Imported lazily to keep project descriptor parsing independent from task
    # manifest internals while allowing registered descriptors outside the
    # active control-plane home.
    from execraft.project import project_directory

    return project_directory(root, project) / "tasks" / validate_task_id(task_id)


def project_task_manifest_path(root: Path, project: str, task_id: str) -> Path:
    return project_task_directory(root, project, task_id) / "TASK.yaml"


def local_manifest_registry_path(root: Path, task_id: str) -> Path:
    home = ControlPlaneHome.resolve()
    if root.expanduser().resolve() == home.root:
        return home.task_index_dir / f"{validate_task_id(task_id)}.yaml"
    return root.expanduser().resolve() / ".registry" / "tasks" / f"{validate_task_id(task_id)}.yaml"


def _legacy_local_manifest_registry_path(root: Path, task_id: str) -> Path | None:
    """Return the old Git-common-dir task index when *root* is a Git checkout."""

    result = run(
        ("git", "rev-parse", "--git-common-dir"),
        cwd=root,
        check=False,
        capture=True,
    )
    if result.returncode != 0 or not (result.stdout or "").strip():
        return None
    raw = Path((result.stdout or "").strip())
    common = raw if raw.is_absolute() else (root / raw).resolve()
    return common / "ai-tasks" / f"{validate_task_id(task_id)}.yaml"


def load_manifest(root: Path, task_id: str) -> TaskManifest:
    from execraft.project import list_projects

    candidates_by_path: dict[Path, tuple[str, Path]] = {}
    for project in list_projects(root):
        path = project_task_manifest_path(root, project.id, task_id)
        if path.is_file():
            candidates_by_path[path.resolve()] = (project.id, path)
    # Preserve compatibility with partially constructed and schema-v1 catalog
    # directories that contain task dossiers but no loadable project.yaml yet.
    legacy_projects = root.expanduser().resolve() / "projects"
    if legacy_projects.is_dir():
        for directory in sorted(legacy_projects.iterdir()):
            path = directory / "tasks" / validate_task_id(task_id) / "TASK.yaml"
            if directory.is_dir() and path.is_file():
                candidates_by_path[path.resolve()] = (directory.name, path)
    candidates = list(candidates_by_path.values())
    if len(candidates) > 1:
        projects = ", ".join(sorted(project_id for project_id, _ in candidates))
        raise TaskGitError(
            f"task {task_id!r} exists in multiple project registries: {projects}"
        )
    path = candidates[0][1] if candidates else task_manifest_path(root, task_id)
    if not path.is_file():
        path = local_manifest_registry_path(root, task_id)
    if not path.is_file():
        legacy = _legacy_local_manifest_registry_path(root, task_id)
        if legacy is not None:
            path = legacy
    if not path.is_file():
        raise TaskGitError(
            f"task manifest does not exist in the checkout or local registry: {task_id}"
        )
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise TaskGitError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, Mapping):
        raise TaskGitError(f"TASK.yaml must contain a mapping: {path}")
    manifest = TaskManifest.from_mapping(data)
    if manifest.id != task_id:
        raise TaskGitError(
            f"task manifest ID mismatch: directory is {task_id!r}, manifest is {manifest.id!r}"
        )
    return manifest


def write_manifest(root: Path, manifest: TaskManifest) -> Path:
    validate_manifest(manifest)
    manifest.last_updated = utc_now()
    if manifest.schema_version >= 2 and manifest.project:
        path = project_task_manifest_path(root, manifest.project, manifest.id)
    else:
        path = task_manifest_path(root, manifest.id)
    atomic_write_yaml(path, manifest.as_mapping(), sort_keys=False, width=1000)
    registry = local_manifest_registry_path(root, manifest.id)
    atomic_write_text(registry, path.read_text(encoding="utf-8"))
    return path


def validate_manifest(manifest: TaskManifest) -> None:
    if manifest.schema_version == 1:
        _validate_manifest_v1(manifest)
    elif manifest.schema_version == 2:
        _validate_manifest_v2(manifest)
    else:
        raise TaskGitError(f"unsupported TASK.yaml schema_version: {manifest.schema_version}")


def _validate_manifest_v1(manifest: TaskManifest) -> None:
    validate_task_id(manifest.id)
    if not manifest.title:
        raise TaskGitError("TASK.yaml title cannot be empty")
    if manifest.status not in TASK_STATUSES:
        raise TaskGitError(
            f"TASK.yaml status must be one of {', '.join(sorted(TASK_STATUSES))}; "
            f"found {manifest.status!r}"
        )
    validate_branch_name(manifest.branch_name, label="task branch")
    validate_branch_name(manifest.integration_branch, label="integration branch")
    if manifest.merge_strategy not in {"squash", "merge", "ff-only"}:
        raise TaskGitError("TASK.yaml merge_strategy must be squash, merge, or ff-only")
    if not manifest.repositories:
        raise TaskGitError("TASK.yaml must contain at least one repository")
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    root_count = 0
    for repository in manifest.repositories:
        if not REPOSITORY_ID_RE.fullmatch(repository.id):
            raise TaskGitError(f"invalid repository ID: {repository.id!r}")
        if repository.id in seen_ids:
            raise TaskGitError(f"duplicate repository ID: {repository.id}")
        seen_ids.add(repository.id)
        path = Path(repository.path)
        if path.is_absolute() or ".." in path.parts:
            raise TaskGitError(f"unsafe repository path: {repository.path!r}")
        normalized = path.as_posix() or "."
        if normalized in seen_paths:
            raise TaskGitError(f"duplicate repository path: {repository.path}")
        seen_paths.add(normalized)
        if normalized == ".":
            root_count += 1
        validate_branch_name(repository.base_branch, label=f"{repository.id} base branch")
        validate_branch_name(repository.task_branch, label=f"{repository.id} task branch")
        if is_protected_branch(repository.task_branch):
            raise TaskGitError(
                f"task branch for {repository.id} is protected: {repository.task_branch}"
            )
        if repository.mutability not in {"task_owned", "runtime_only"}:
            raise TaskGitError(
                f"repository {repository.id} mutability must be task_owned or runtime_only"
            )
    if root_count != 1:
        raise TaskGitError("TASK.yaml must include the coordination repository at path '.' exactly once")


def _validate_manifest_v2(manifest: TaskManifest) -> None:
    validate_task_id(manifest.id)
    if not manifest.project:
        raise TaskGitError("TASK.yaml project cannot be empty in schema v2")
    if not manifest.title:
        raise TaskGitError("TASK.yaml title cannot be empty")
    if manifest.status not in TASK_STATUSES:
        raise TaskGitError(
            f"TASK.yaml status must be one of {', '.join(sorted(TASK_STATUSES))}; "
            f"found {manifest.status!r}"
        )
    validate_branch_name(manifest.branch_name, label="task branch")
    if manifest.integration_branch:
        validate_branch_name(manifest.integration_branch, label="integration branch")
    if manifest.merge_strategy not in {"squash", "merge", "ff-only"}:
        raise TaskGitError("TASK.yaml merge_strategy must be squash, merge, or ff-only")
    if not manifest.repositories:
        raise TaskGitError("TASK.yaml must contain at least one repository")
    seen_ids: set[str] = set()
    for repository in manifest.repositories:
        if not REPOSITORY_ID_RE.fullmatch(repository.id):
            raise TaskGitError(f"invalid repository ID: {repository.id!r}")
        if repository.id in seen_ids:
            raise TaskGitError(f"duplicate repository ID: {repository.id}")
        seen_ids.add(repository.id)
        if repository.path:
            path = Path(repository.path)
            if path.is_absolute() or ".." in path.parts:
                raise TaskGitError(f"unsafe repository path: {repository.path!r}")
        if repository.mutability not in {"task_owned", "runtime_only"}:
            raise TaskGitError(
                f"repository {repository.id} mutability must be task_owned or runtime_only"
            )
        validate_branch_name(repository.base_branch, label=f"{repository.id} base branch")
        validate_branch_name(repository.task_branch, label=f"{repository.id} task branch")
        if is_protected_branch(repository.task_branch):
            raise TaskGitError(
                f"task branch for {repository.id} is protected: {repository.task_branch}"
            )


def active_task_storage_path(root: Path) -> Path:
    home = ControlPlaneHome.resolve()
    if root.expanduser().resolve() == home.root:
        return home.active_task_path
    return root.expanduser().resolve() / ".registry" / "active-task"


def _legacy_active_task_storage_path(root: Path) -> Path | None:
    result = run(
        ("git", "rev-parse", "--git-path", "ai-active-task"),
        cwd=root,
        check=False,
        capture=True,
    )
    if result.returncode != 0 or not (result.stdout or "").strip():
        return None
    raw = Path((result.stdout or "").strip())
    return raw if raw.is_absolute() else (root / raw).resolve()


def read_active_task(root: Path) -> str:
    local_path = active_task_storage_path(root)
    if local_path.is_file():
        value = local_path.read_text(encoding="utf-8").strip()
        if value:
            return validate_task_id(value)
    git_local = _legacy_active_task_storage_path(root)
    if git_local is not None and git_local.is_file():
        value = git_local.read_text(encoding="utf-8").strip()
        if value:
            return validate_task_id(value)
    legacy = root / "docs" / "ai" / "ACTIVE_TASK"
    if legacy.is_file():
        value = legacy.read_text(encoding="utf-8").strip()
        if value:
            return validate_task_id(value)
    return ""


def write_active_task(root: Path, task_id: str) -> Path:
    value = validate_task_id(task_id)
    path = active_task_storage_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{value}\n", encoding="utf-8")
    return path


def clear_active_task(root: Path, task_id: str | None = None) -> bool:
    path = active_task_storage_path(root)
    current = read_active_task(root)
    if task_id and current != task_id:
        return False
    legacy = root / "docs" / "ai" / "ACTIVE_TASK"
    if path == legacy:
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text("", encoding="utf-8")
    elif path.exists():
        path.unlink()
    git_local = _legacy_active_task_storage_path(root)
    if git_local is not None and git_local != path and git_local.is_file():
        if git_local.read_text(encoding="utf-8").strip() == current:
            git_local.unlink()
    if legacy.is_file() and legacy.read_text(encoding="utf-8").strip() == current:
        legacy.write_text("", encoding="utf-8")
    return bool(current)


def resolve_task_id(root: Path, supplied: str | None) -> str:
    value = (supplied or "").strip() or read_active_task(root)
    if not value:
        raise TaskGitError("no task ID supplied and no active task is configured")
    return validate_task_id(value)


def ordered_repositories(manifest: TaskManifest, *, root_last: bool = True) -> list[RepositorySpec]:
    def _is_root(repo: RepositorySpec) -> bool:
        if manifest.schema_version >= 2:
            return repo.role == "deployment"
        return repo.path == "."
    return sorted(
        manifest.repositories,
        key=lambda repository: _is_root(repository) if root_last else (not _is_root(repository)),
    )


def lookup_repository(manifest: TaskManifest, repository_id: str) -> RepositorySpec:
    for repository in manifest.repositories:
        if repository.id == repository_id:
            return repository
    raise TaskGitError(f"task {manifest.id!r} does not contain repository {repository_id!r}")


def select_repositories(
    manifest: TaskManifest,
    repository_ids: Iterable[str] | None,
    *,
    include_root: bool = True,
) -> list[RepositorySpec]:
    def _is_root(repo: RepositorySpec) -> bool:
        if manifest.schema_version >= 2:
            return repo.role == "deployment"
        return repo.path == "."
    requested = [item for item in (repository_ids or []) if item]
    if not requested:
        selected = list(manifest.repositories)
    else:
        selected = [lookup_repository(manifest, item) for item in requested]
    if include_root and not any(_is_root(repo) for repo in selected):
        selected.append(next(repo for repo in manifest.repositories if _is_root(repo)))
    unique: dict[str, RepositorySpec] = {}
    for repository in selected:
        unique[repository.id] = repository
    return sorted(unique.values(), key=_is_root)


def checkout_transaction(
    root: Path,
    destinations: Sequence[tuple[RepositorySpec, str, str | None]],
    *,
    purpose: str,
    manifest: TaskManifest | None = None,
) -> list[CheckoutStep]:
    def _is_root(repo: RepositorySpec) -> bool:
        if manifest and manifest.schema_version >= 2:
            return repo.role == "deployment"
        return repo.path == "."

    states: list[RepositoryState] = []
    for repository, destination, create_from in destinations:
        validate_branch_name(destination, label=f"{repository.id} destination branch")
        state = repository_state(root, repository, manifest=manifest)
        if state is None:
            continue
        require_clean_state(state, purpose=purpose)
        if not branch_exists(state.path, destination) and not create_from:
            raise TaskGitError(
                f"cannot {purpose}: branch {destination!r} does not exist in {repository.id}"
            )
        if create_from:
            head_commit(state.path, create_from)
        states.append(state)

    by_id = {state.repository.id: state for state in states}
    ordered = sorted(destinations, key=lambda item: _is_root(item[0]))
    completed: list[CheckoutStep] = []
    try:
        for repository, destination, create_from in ordered:
            state = by_id.get(repository.id)
            if state is None:
                continue
            if state.branch == destination:
                completed.append(
                    CheckoutStep(repository, state.path, state.branch, state.head, destination, False)
                )
                continue
            created = False
            if branch_exists(state.path, destination):
                git(state.path, "switch", destination)
            else:
                assert create_from
                git(state.path, "switch", "-c", destination, create_from)
                created = True
            completed.append(
                CheckoutStep(repository, state.path, state.branch, state.head, destination, created)
            )
    except Exception:
        for step in reversed(completed):
            try:
                if current_branch(step.path) != step.previous_branch:
                    git(step.path, "switch", step.previous_branch)
                if step.created_branch and branch_exists(step.path, step.destination_branch):
                    git(step.path, "branch", "-d", step.destination_branch, check=False)
            except Exception:
                pass
        raise
    return completed


def shell_command(command: str, *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return run(("bash", "-lc", command), cwd=cwd, check=False, capture=True)


def load_project_yaml(root: Path, project: str) -> dict[str, Any]:
    from execraft.project import ProjectError, load_registered_project

    try:
        descriptor = load_registered_project(root, project)
    except ProjectError as exc:
        raise TaskGitError(f"project.yaml not found for project {project!r}: {exc}") from exc
    path = descriptor.directory / "project.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise TaskGitError(f"invalid project.yaml in {path}: {exc}") from exc
    if not isinstance(data, Mapping):
        raise TaskGitError(f"project.yaml must contain a mapping: {path}")
    return dict(data)


def project_repository_path(project_data: dict[str, Any], repository_id: str) -> str:
    repositories = project_data.get("repositories") or []
    for item in repositories:
        if isinstance(item, Mapping) and item.get("id") == repository_id:
            return str(item.get("path", ""))
    raise TaskGitError(f"repository {repository_id!r} not found in project catalog")


def migrate_v1_to_v2(
    v1_manifest: TaskManifest,
    *,
    project: str,
    project_data: dict[str, Any] | None = None,
) -> TaskManifest:
    if v1_manifest.schema_version != 1:
        raise TaskGitError(f"migrate_v1_to_v2 requires schema v1, got v{v1_manifest.schema_version}")
    catalog: dict[str, Mapping[str, Any]] = {}
    if project_data is not None:
        catalog = {
            str(item["id"]): item
            for item in (project_data.get("repositories") or [])
            if isinstance(item, Mapping) and item.get("id")
        }
        task_ids = {repository.id for repository in v1_manifest.repositories}
        unknown = sorted(task_ids - set(catalog))
        if unknown:
            raise TaskGitError(
                "legacy task contains repositories absent from the project catalog: "
                + ", ".join(unknown)
            )
        missing = sorted(
            repository_id
            for repository_id, item in catalog.items()
            if bool(item.get("required", True)) and repository_id not in task_ids
        )
        if missing:
            raise TaskGitError(
                "legacy task omits required project repositories: " + ", ".join(missing)
            )
    v2_repos: list[RepositorySpec] = []
    for repo in v1_manifest.repositories:
        catalog_repository = catalog.get(repo.id, {})
        role = str(catalog_repository.get("role", repo.role))
        catalog_mutability = str(
            catalog_repository.get("mutability", "task_owned")
        )
        mutability = (
            "runtime_only"
            if "runtime_only" in {repo.mutability, catalog_mutability}
            else "task_owned"
        )
        v2_repos.append(
            RepositorySpec(
                id=repo.id,
                path="",
                base_branch=repo.base_branch,
                task_branch=repo.task_branch,
                role=role,
                required=(
                    repo.required
                    if not catalog_repository
                    else repo.required or bool(catalog_repository.get("required", True))
                ),
                start_commit=repo.start_commit,
                verify=list(repo.verify),
                latest_commit=repo.latest_commit,
                mutability=mutability,
            )
        )
    return TaskManifest(
        schema_version=2,
        id=v1_manifest.id,
        project=project,
        title=v1_manifest.title,
        status=v1_manifest.status,
        created_at=v1_manifest.created_at,
        last_updated=v1_manifest.last_updated,
        branch_name=v1_manifest.branch_name,
        merge_strategy=v1_manifest.merge_strategy,
        integration_branch=v1_manifest.integration_branch,
        repositories=v2_repos,
        integration_verify=list(v1_manifest.integration_verify),
    )


def append_handoff_git_records(root: Path, task_id: str, records: Sequence[tuple[str, str, str]]) -> Path:
    handoff = task_directory(root, task_id) / "HANDOFF.md"
    if not handoff.is_file():
        raise TaskGitError(f"missing HANDOFF.md: {handoff}")
    text = handoff.read_text(encoding="utf-8").rstrip()
    heading = "## Git checkpoints"
    if heading not in text:
        text += f"\n\n{heading}\n\n| Repository | Branch | Commit |\n|---|---|---|\n"
    lines = [f"| {repository_id} | `{branch}` | `{commit}` |" for repository_id, branch, commit in records]
    text += "\n" + "\n".join(lines) + "\n"
    handoff.write_text(text, encoding="utf-8")
    return handoff


def ahead_behind(path: Path, base: str, branch: str) -> tuple[int, int] | None:
    output = git(path, "rev-list", "--left-right", "--count", f"{base}...{branch}", check=False)
    if not output:
        return None
    parts = output.split()
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return None
    behind, ahead = (int(parts[0]), int(parts[1]))
    return ahead, behind
