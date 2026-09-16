"""Git-worktree and isolated runtime helpers for parallel AI task workspaces."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from execraft.persistence.atomic import atomic_write_yaml
from execraft.workspace.task_git import (
    TaskGitError,
    branch_exists,
    git,
    run,
    task_directory,
    validate_task_id,
)

COMPOSE_NAME_RE = re.compile(r"[^a-z0-9_-]+")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def repository_environment_name(repository_id: str) -> str:
    """Return the stable environment variable used for a workspace repository path."""

    normalized = re.sub(r"[^A-Za-z0-9]+", "_", repository_id).strip("_").upper()
    if not normalized:
        raise TaskGitError(f"cannot derive environment name for repository {repository_id!r}")
    return f"EXECRAFT_REPO_{normalized}"


@dataclass(frozen=True)
class WorktreeEntry:
    path: Path
    head: str
    branch: str | None
    bare: bool = False
    detached: bool = False
    locked: bool = False
    prunable: bool = False


@dataclass
class WorkspaceRecord:
    schema_version: int
    task_id: str
    created_at: str
    source_root: str
    workspace_root: str
    compose_project: str
    ros_domain_id: int
    port_offset: int
    env_file: str
    repositories: list[dict[str, str]]
    status: str = "ready"
    policy_profile: str = "workspace-write"
    capabilities: list[str] = field(default_factory=list)
    runtime_env: dict[str, str] = field(default_factory=dict)
    runtime_status: str = "unknown"
    last_lifecycle_action: str = ""
    last_lifecycle_at: str = ""

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "WorkspaceRecord":
        repositories = data.get("repositories") or []
        if not isinstance(repositories, list):
            raise TaskGitError("workspace record repositories must be a list")
        raw_caps = data.get("capabilities") or []
        if not isinstance(raw_caps, list):
            raw_caps = []
        record = cls(
            schema_version=int(data.get("schema_version", 0)),
            task_id=validate_task_id(str(data.get("task_id", ""))),
            created_at=str(data.get("created_at", "")),
            source_root=str(data.get("source_root", "")),
            workspace_root=str(data.get("workspace_root", "")),
            compose_project=str(data.get("compose_project", "")),
            ros_domain_id=int(data.get("ros_domain_id", -1)),
            port_offset=int(data.get("port_offset", -1)),
            env_file=str(data.get("env_file", ".ai-task.env")),
            repositories=[dict(item) for item in repositories],
            status=str(data.get("status", "ready")),
            policy_profile=str(data.get("policy_profile", "workspace-write")),
            capabilities=[str(c) for c in raw_caps],
            runtime_env={str(k): str(v) for k, v in (data.get("runtime_env") or {}).items()},
            runtime_status=str(data.get("runtime_status", "unknown")),
            last_lifecycle_action=str(data.get("last_lifecycle_action", "")),
            last_lifecycle_at=str(data.get("last_lifecycle_at", "")),
        )
        validate_workspace_record(record)
        return record

    def as_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "created_at": self.created_at,
            "status": self.status,
            "source_root": self.source_root,
            "workspace_root": self.workspace_root,
            "compose_project": self.compose_project,
            "ros_domain_id": self.ros_domain_id,
            "port_offset": self.port_offset,
            "env_file": self.env_file,
            "repositories": list(self.repositories),
            "policy_profile": self.policy_profile,
            "capabilities": list(self.capabilities),
            "runtime_env": dict(self.runtime_env),
            "runtime_status": self.runtime_status,
            "last_lifecycle_action": self.last_lifecycle_action,
            "last_lifecycle_at": self.last_lifecycle_at,
        }


def validate_workspace_record(record: WorkspaceRecord) -> None:
    if record.schema_version != 1:
        raise TaskGitError(f"unsupported workspace schema_version: {record.schema_version}")
    if not record.created_at:
        raise TaskGitError("workspace record created_at cannot be empty")
    if not record.source_root or not Path(record.source_root).is_absolute():
        raise TaskGitError("workspace source_root must be absolute")
    if not record.workspace_root or not Path(record.workspace_root).is_absolute():
        raise TaskGitError("workspace workspace_root must be absolute")
    capabilities = set(record.capabilities)
    legacy_runtime = not capabilities and bool(record.compose_project)
    if "runtime.compose" in capabilities or legacy_runtime:
        if not record.compose_project:
            raise TaskGitError("workspace compose_project cannot be empty when runtime.compose is enabled")
    elif record.compose_project:
        raise TaskGitError("workspace compose_project set without runtime.compose capability")
    if "runtime.ros_domain" in capabilities or legacy_runtime:
        if not 0 <= record.ros_domain_id <= 101:
            raise TaskGitError("workspace ros_domain_id must be between 0 and 101")
    elif record.ros_domain_id != -1:
        raise TaskGitError("workspace ros_domain_id set without runtime.ros_domain capability")
    if "runtime.port_namespace" in capabilities or legacy_runtime:
        if not 0 <= record.port_offset <= 50000:
            raise TaskGitError("workspace port_offset must be between 0 and 50000")
    elif record.port_offset != 0:
        raise TaskGitError("workspace port_offset set without runtime.port_namespace capability")
    for key in record.runtime_env:
        if not ENV_NAME_RE.fullmatch(key):
            raise TaskGitError(f"invalid workspace runtime environment variable: {key!r}")
    if record.runtime_status not in {"unknown", "running", "stopped"}:
        raise TaskGitError(f"invalid workspace runtime_status: {record.runtime_status!r}")
    if not record.repositories:
        raise TaskGitError("workspace must contain at least one repository")
    seen_ids: set[str] = set()
    for item in record.repositories:
        repository_id = str(item.get("id", "")).strip()
        if not repository_id:
            raise TaskGitError("workspace repository ID cannot be empty")
        if repository_id in seen_ids:
            raise TaskGitError(f"duplicate workspace repository ID: {repository_id}")
        seen_ids.add(repository_id)
        for key in ("source_path", "worktree_path"):
            value = str(item.get(key, ""))
            if not value or not Path(value).is_absolute():
                raise TaskGitError(
                    f"workspace repository {repository_id} {key} must be absolute"
                )
        if item.get("mutability", "task_owned") not in {
            "task_owned",
            "runtime_only",
        }:
            raise TaskGitError(
                f"workspace repository {repository_id} has invalid mutability"
            )
    if not record.policy_profile:
        raise TaskGitError("workspace policy_profile cannot be empty")


def common_git_dir(path: Path) -> Path:
    raw = git(path, "rev-parse", "--git-common-dir")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = (path / candidate).resolve()
    return candidate


def workspace_registry_dir(root: Path) -> Path:
    """Return a writable workspace index for checkout and installed layouts."""

    result = run(
        ("git", "rev-parse", "--git-common-dir"),
        cwd=root,
        check=False,
        capture=True,
    )
    if result.returncode == 0 and (result.stdout or "").strip():
        raw = Path((result.stdout or "").strip())
        common = raw if raw.is_absolute() else (root / raw).resolve()
        return common / "ai-workspaces"
    return root.expanduser().resolve() / ".registry" / "workspaces"


def workspace_registry_path(root: Path, task_id: str) -> Path:
    return workspace_registry_dir(root) / f"{validate_task_id(task_id)}.yaml"


def load_workspace(root: Path, task_id: str) -> WorkspaceRecord:
    path = workspace_registry_path(root, task_id)
    if not path.is_file():
        raise TaskGitError(f"workspace is not registered for task {task_id!r}: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise TaskGitError(f"invalid workspace record {path}: {exc}") from exc
    if not isinstance(data, Mapping):
        raise TaskGitError(f"workspace record must contain a mapping: {path}")
    record = WorkspaceRecord.from_mapping(data)
    if record.task_id != task_id:
        raise TaskGitError(f"workspace task mismatch: expected {task_id}, found {record.task_id}")
    return record


def write_workspace(root: Path, record: WorkspaceRecord) -> Path:
    validate_workspace_record(record)
    path = workspace_registry_path(root, record.task_id)
    atomic_write_yaml(path, record.as_mapping(), sort_keys=False, width=1000)
    return path


def list_workspaces(root: Path) -> list[WorkspaceRecord]:
    directory = workspace_registry_dir(root)
    if not directory.is_dir():
        return []
    records: list[WorkspaceRecord] = []
    for path in sorted(directory.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if isinstance(data, Mapping):
                records.append(WorkspaceRecord.from_mapping(data))
        except (OSError, yaml.YAMLError, TaskGitError):
            continue
    return records


def parse_worktree_list(path: Path) -> list[WorktreeEntry]:
    output = git(path, "worktree", "list", "--porcelain")
    entries: list[WorktreeEntry] = []
    current: dict[str, Any] = {}
    for raw_line in output.splitlines() + [""]:
        line = raw_line.rstrip()
        if not line:
            if current:
                entries.append(
                    WorktreeEntry(
                        path=Path(current["worktree"]).resolve(),
                        head=str(current.get("HEAD", "")),
                        branch=(
                            str(current["branch"]).removeprefix("refs/heads/")
                            if current.get("branch")
                            else None
                        ),
                        bare=bool(current.get("bare")),
                        detached=bool(current.get("detached")),
                        locked=bool(current.get("locked")),
                        prunable=bool(current.get("prunable")),
                    )
                )
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value if value else True
    return entries


def worktree_for_branch(path: Path, branch: str) -> Path | None:
    for entry in parse_worktree_list(path):
        if entry.branch == branch:
            return entry.path
    return None


def validate_path_segment(value: str, *, label: str) -> str:
    """Reject separators and traversal before composing host-local paths."""

    normalized = str(value).strip()
    if (
        not normalized
        or normalized in {".", ".."}
        or Path(normalized).name != normalized
        or "/" in normalized
        or "\\" in normalized
    ):
        raise TaskGitError(f"invalid {label}: {value!r}")
    return normalized


def default_workspace_path(
    root: Path,
    task_id: str,
    *,
    project_id: str = "",
) -> Path:
    """Return the default shell path, optionally scoped by project identity.

    The optional project segment avoids collisions between independently
    registered projects while retaining the historical task-only path for
    callers that have not yet migrated to composite identity.
    """

    del root  # Retained for API compatibility and future per-home policies.
    home_override = os.environ.get("AI_WORKSPACE_HOME")
    home = (
        Path(home_override).expanduser().resolve()
        if home_override
        else Path.home() / "workspace" / "ai-workspaces"
    )
    parent = home / validate_path_segment(project_id, label="project id") if project_id else home
    return (parent / validate_path_segment(task_id, label="task id")).resolve()


def sanitize_compose_project(task_id: str) -> str:
    normalized = COMPOSE_NAME_RE.sub("_", task_id.lower()).strip("_-")
    normalized = normalized or "task"
    return f"ai_{normalized}"[:63].rstrip("_-")


def used_runtime_slots(root: Path, *, exclude_task: str | None = None) -> set[int]:
    slots: set[int] = set()
    for record in list_workspaces(root):
        if record.task_id != exclude_task and record.status != "removed":
            slots.add(max(record.port_offset // 100, 0))
    return slots


def allocate_runtime(
    root: Path, task_id: str, capabilities: Sequence[str] | None = None
) -> tuple[int, int]:
    # None preserves the legacy helper contract for callers/tests. Project-aware
    # callers pass an explicit capability list, which keeps non-ROS projects free
    # from robotics-specific runtime state.
    enabled = set(capabilities) if capabilities is not None else {
        "runtime.ros_domain", "runtime.port_namespace"
    }
    if not ({"runtime.ros_domain", "runtime.port_namespace"} & enabled):
        return -1, 0
    digest = int(hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:8], 16)
    used = used_runtime_slots(root, exclude_task=task_id)
    for step in range(80):
        slot = 1 + ((digest + step) % 80)
        if slot in used:
            continue
        domain = 20 + (slot % 80)
        if domain > 101:
            domain = 20 + (domain - 102)
        return (domain if "runtime.ros_domain" in enabled else -1,
                slot * 100 if "runtime.port_namespace" in enabled else 0)
    raise TaskGitError("unable to allocate an isolated ROS domain/port slot")


def add_local_excludes(path: Path) -> None:
    exclude_raw = git(path, "rev-parse", "--git-path", "info/exclude")
    exclude = Path(exclude_raw)
    if not exclude.is_absolute():
        exclude = (path / exclude).resolve()
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    marker = "# ai-workspace-local\n"
    rules = (
        marker
        + "/.ai-task.env\n"
        + "/.ai-workspace/\n"
    )
    if marker not in existing:
        prefix = existing + ("\n" if existing and not existing.endswith("\n") else "")
        exclude.write_text(prefix + rules, encoding="utf-8")


def write_env_file(workspace_root: Path, record: WorkspaceRecord) -> Path:
    path = workspace_root / record.env_file
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata_dir = workspace_root / ".execraft"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    capabilities = set(record.capabilities)
    values: dict[str, str] = {
        "AI_TASK_ID": record.task_id,
        "AI_WORKSPACE_ROOT": str(workspace_root),
        "AI_TASK_ENV_FILE": str(path),
    }
    if "runtime.compose" in capabilities or (not capabilities and record.compose_project):
        values["COMPOSE_PROJECT_NAME"] = record.compose_project
        values["COMPOSE_PREFIX"] = f"{record.compose_project}_"
    if "runtime.ros_domain" in capabilities or (not capabilities and record.ros_domain_id >= 0):
        values["ROS_DOMAIN_ID"] = str(record.ros_domain_id)
    if "runtime.port_namespace" in capabilities or (not capabilities and record.port_offset > 0):
        values["AI_PORT_OFFSET"] = str(record.port_offset)
    repository_map: dict[str, dict[str, str]] = {}
    repository_environment: dict[str, str] = {}
    for item in record.repositories:
        repository_id = str(item["id"])
        variable = repository_environment_name(repository_id)
        if variable in repository_environment:
            raise TaskGitError(
                f"repository IDs produce a duplicate environment variable: {variable}"
            )
        worktree_path = str(Path(item["worktree_path"]).resolve())
        repository_environment[variable] = worktree_path
        repository_map[repository_id] = {
            "id": repository_id,
            "role": str(item.get("role", "component")),
            "mutability": str(item.get("mutability", "task_owned")),
            "source_path": str(Path(item["source_path"]).resolve()),
            "worktree_path": worktree_path,
        }

    repository_map_path = metadata_dir / "repositories.json"
    repository_map_path.write_text(
        json.dumps(
            {"schema_version": 1, "repositories": repository_map},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    values["EXECRAFT_REPOSITORY_MAP"] = str(repository_map_path)
    values.update(repository_environment)
    values.update(record.runtime_env)
    path.write_text(
        "# Generated by execraft; do not commit.\n"
        + "\n".join(f"{key}={shlex.quote(value)}" for key, value in sorted(values.items()))
        + "\n",
        encoding="utf-8",
    )
    write_workspace_marker(record)
    return path


def write_workspace_marker(record: WorkspaceRecord) -> Path:
    """Atomically refresh the workspace-local ownership marker.

    The registry remains authoritative, while the marker proves that a shell is
    Execraft-owned and binds it to the same task and absolute path.
    """

    workspace_root = Path(record.workspace_root).expanduser().resolve()
    metadata_dir = workspace_root / ".execraft"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    path = metadata_dir / "workspace.yaml"
    atomic_write_yaml(path, record.as_mapping(), sort_keys=False, width=1000)
    return path


def load_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise TaskGitError(f"workspace environment file is missing: {path}")
    values: dict[str, str] = {}
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise TaskGitError(f"invalid environment line {line_number} in {path}")
        key, value = line.split("=", 1)
        key = key.strip()
        if not ENV_NAME_RE.fullmatch(key):
            raise TaskGitError(f"invalid environment variable {key!r} in {path}")
        parsed = shlex.split(value, posix=True)
        values[key] = parsed[0] if parsed else ""
    return values


def source_status_paths(
    root: Path, task_id: str, allowed_paths: Sequence[str] | None = None
) -> list[str]:
    output = git(root, "status", "--porcelain=v1", "--untracked-files=all")
    prefixes = tuple(path.rstrip("/") + "/" for path in (allowed_paths or []))
    exact = {path.rstrip("/") for path in (allowed_paths or [])}
    disallowed: list[str] = []
    for line in output.splitlines():
        path = line[3:] if len(line) >= 4 else line
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path in exact or any(path.startswith(prefix) for prefix in prefixes):
            continue
        disallowed.append(line)
    return disallowed


def add_worktree(source: Path, destination: Path, branch: str, base_branch: str) -> bool:
    existing = worktree_for_branch(source, branch)
    if existing:
        raise TaskGitError(f"branch {branch!r} is already checked out at {existing}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if branch_exists(source, branch):
        git(source, "worktree", "add", str(destination), branch)
        return False
    git(source, "worktree", "add", "-b", branch, str(destination), base_branch)
    return True


def remove_worktree(source: Path, destination: Path, *, force: bool = False) -> None:
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(destination))
    git(source, *args)
    git(source, "worktree", "prune")


def migrate_task_dossier(source_root: Path, workspace_root: Path, task_id: str) -> str:
    source = task_directory(source_root, task_id)
    destination = task_directory(workspace_root, task_id)
    if destination.exists():
        return "already-present"
    if not source.exists():
        return "missing"
    destination.parent.mkdir(parents=True, exist_ok=True)
    status = git(source_root, "status", "--porcelain=v1", "--", str(source.relative_to(source_root)))
    lines = [line for line in status.splitlines() if line]
    if lines and all(line.startswith("?? ") for line in lines):
        shutil.move(str(source), str(destination))
        return "moved-untracked"
    shutil.copytree(source, destination)
    return "copied"


def docker_project_containers(project: str, *, all_containers: bool = True) -> list[str]:
    args = [
        "docker",
        "ps",
        "-q",
        "--filter",
        f"label=com.docker.compose.project={project}",
    ]
    if all_containers:
        args.insert(2, "-a")
    try:
        result = subprocess.run(
            args, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
    except FileNotFoundError:
        return []
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def compose_isolation_findings(
    root: Path,
    compose_paths: Sequence[str | Path] | None = None,
) -> list[str]:
    """Return workspace-isolation findings for explicitly selected Compose inputs.

    The generic workspace layer must not assume a project-specific directory layout.
    When ``compose_paths`` is omitted, only conventional Compose files at the project
    root are inspected. Projects with Compose files elsewhere should declare those
    paths in their project capability/configuration and pass them explicitly.
    """

    findings: list[str] = []
    selected_paths: Sequence[str | Path] = compose_paths or (
        "compose.yaml",
        "compose.yml",
        "docker-compose.yaml",
        "docker-compose.yml",
    )
    files: list[Path] = []
    for item in selected_paths:
        candidate = Path(item)
        if not candidate.is_absolute():
            candidate = root / candidate
        if candidate.is_dir():
            files.extend(sorted(candidate.rglob("*.yaml")))
            files.extend(sorted(candidate.rglob("*.yml")))
        elif candidate.is_file():
            files.append(candidate)
    for path in sorted(set(files)):
        text = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("container_name:") and "COMPOSE_PREFIX" not in stripped:
                findings.append(f"{path.relative_to(root)}:{line_number}: fixed container_name")
            if re.match(r"^name:\s*[^$]", stripped):
                findings.append(f"{path.relative_to(root)}:{line_number}: fixed compose project name")
            if stripped in {"network_mode: host", 'network_mode: "host"', "network_mode: 'host'"}:
                findings.append(
                    f"{path.relative_to(root)}:{line_number}: host networking is shared across workspaces"
                )
    return findings


def workspace_repository_path(record: WorkspaceRecord, repository_id: str | None = None) -> Path:
    root = Path(record.workspace_root)
    if repository_id is None:
        return root
    for item in record.repositories:
        if item.get("id") == repository_id:
            return Path(item["worktree_path"])
    raise TaskGitError(f"workspace {record.task_id!r} has no repository {repository_id!r}")


def run_in_workspace(
    record: WorkspaceRecord,
    command: Sequence[str],
    *,
    repository_id: str | None = None,
    shell: bool = False,
    environment: Mapping[str, str] | None = None,
    unset_environment: Sequence[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    if not command:
        raise TaskGitError("workspace command cannot be empty")
    cwd = workspace_repository_path(record, repository_id)
    env = os.environ.copy()
    env.update(load_env_file(Path(record.workspace_root) / record.env_file))
    for key in unset_environment or ():
        if not ENV_NAME_RE.fullmatch(str(key)):
            raise TaskGitError(f"invalid environment variable to unset: {key!r}")
        env.pop(str(key), None)
    for key, value in (environment or {}).items():
        if not ENV_NAME_RE.fullmatch(str(key)):
            raise TaskGitError(f"invalid verification environment variable: {key!r}")
        env[str(key)] = str(value)
    if shell:
        return subprocess.run(command[0], cwd=cwd, env=env, shell=True, text=True, check=False)
    return subprocess.run(list(command), cwd=cwd, env=env, text=True, check=False)
