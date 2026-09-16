"""Safe permanent removal of Execraft project/task lifecycle state.

Archive remains the normal reversible lifecycle operation. This module is the
single irreversible path and deliberately limits deletion to Execraft-owned data.
Source repositories and externally registered project descriptors are never
recursively deleted.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from execraft.orchestrate.identity import resolve_storage_identity
from execraft.persistence import LockUnavailableError, atomic_write_json, file_lock_is_held
from execraft.project_execution.repository import ProjectExecutionRepository
from execraft.project import (
    ProjectError,
    load_project_registration,
    project_binding_path,
    project_directory,
    remove_project_registration,
    validate_project_id,
)
from execraft.removal_git import (
    created_task_branches,
    delete_created_branches,
    validate_branch_removals,
)
from execraft.removal_models import PermanentRemovalError, RemovalPlan, RemovalResult
from execraft.workspace.cleanup import WorkspaceLifecycleService
from execraft.workspace.task_git import (
    active_task_storage_path,
    clear_active_task,
    local_manifest_registry_path,
    validate_task_id,
)
from execraft.workspace.workspace_git import (
    WorkspaceRecord,
    load_workspace,
    workspace_registry_path,
)

class PermanentRemovalService:
    """Plan and execute irreversible removal of Execraft-owned lifecycle data."""

    def __init__(
        self,
        control_root: Path,
        state_root: Path,
        *,
        archive_root: Path | None = None,
    ) -> None:
        self.control_root = control_root.expanduser().resolve()
        self.state_root = state_root.expanduser().resolve()
        self.archive_root = (
            archive_root.expanduser().resolve()
            if archive_root is not None
            else self.state_root / "archives"
        )
        self.catalog_archive_root = self.control_root / "projects" / ".archive"

    def preview_task(
        self,
        project_id: str,
        task_id: str,
        *,
        delete_branches: bool = False,
    ) -> RemovalPlan:
        project = validate_project_id(project_id)
        task = validate_task_id(task_id)
        self._require_task_exists(project, task)
        archived_project_task = (
            self.catalog_archive_root / "projects" / project / "tasks" / task
        )
        if archived_project_task.exists():
            raise PermanentRemovalError(
                "cannot delete an individual task from an archived project because that "
                "would invalidate the project archive; reactivate the project first or "
                "delete the archived project permanently"
            )
        self._assert_registry_ownership(project, task)
        self._assert_not_project_execution_referenced(project, task)
        self._validate_archive_index()
        if self._driver_active(project, task):
            raise PermanentRemovalError(
                "stop the task orchestrator driver before permanent deletion"
            )

        workspace = self._workspace_record(task)
        branches = created_task_branches(workspace) if delete_branches else []
        validate_branch_removals(branches)
        plan = RemovalPlan("task", project, task, branch_removals=branches)
        plan.paths.extend(self._task_paths(project, task))
        plan.preserved.append("source repositories")
        if not delete_branches:
            plan.preserved.append("local task branches")
        return self._deduplicated(plan)

    def remove_task(
        self,
        project_id: str,
        task_id: str,
        *,
        delete_branches: bool = False,
        dry_run: bool = False,
    ) -> RemovalResult:
        plan = self.preview_task(
            project_id,
            task_id,
            delete_branches=delete_branches,
        )
        if dry_run:
            return self._result(plan, plan.paths, [], dry_run=True)

        self._retire_workspace(plan.item_id)
        removed_paths = self._remove_paths(plan.paths)
        self._remove_archive_index_records(plan.project_id, plan.item_id)
        self._clear_active_task_if_matches(plan.item_id)
        self._prune_empty_parents(plan.project_id)
        removed_branches = delete_created_branches(plan.branch_removals)
        return self._result(plan, removed_paths, removed_branches)

    def preview_project(
        self,
        project_id: str,
        *,
        delete_branches: bool = False,
    ) -> RemovalPlan:
        project = validate_project_id(project_id)
        task_ids = self._project_task_ids(project)
        if not self._project_exists(project, task_ids):
            raise PermanentRemovalError(f"project not found: {project}")
        self._validate_archive_index()
        for task_id in task_ids:
            self._assert_registry_ownership(project, task_id)
        external_project = self._is_external_project(project)
        plan = RemovalPlan("project", project, project)
        for task_id in task_ids:
            if self._driver_active(project, task_id):
                raise PermanentRemovalError(
                    f"stop orchestrator driver for {project}/{task_id} before permanent deletion"
                )
            workspace = self._workspace_record(task_id)
            if delete_branches:
                plan.branch_removals.extend(created_task_branches(workspace))
            task_paths = self._task_paths(project, task_id)
            if external_project:
                external_root = project_directory(self.control_root, project).resolve()
                task_paths = [
                    path
                    for path in task_paths
                    if not self._is_within(path, external_root)
                ]
            plan.paths.extend(task_paths)
        validate_branch_removals(plan.branch_removals)
        plan.paths.extend(self._project_owned_paths(project))
        plan.preserved.append("source repositories")
        if external_project:
            plan.preserved.append("external project descriptor and project directory")
        if not delete_branches:
            plan.preserved.append("local task branches")
        return self._deduplicated(plan)

    def remove_project(
        self,
        project_id: str,
        *,
        delete_branches: bool = False,
        dry_run: bool = False,
    ) -> RemovalResult:
        plan = self.preview_project(project_id, delete_branches=delete_branches)
        task_ids = self._project_task_ids(plan.project_id)
        if dry_run:
            return self._result(plan, plan.paths, [], dry_run=True)

        for task_id in task_ids:
            self._retire_workspace(task_id)
        removed_paths = self._remove_paths(plan.paths)
        for task_id in task_ids:
            self._remove_archive_index_records(plan.project_id, task_id)
            self._clear_active_task_if_matches(task_id)
        if load_project_registration(plan.project_id) is not None:
            registration_path = project_binding_path(plan.project_id)
            remove_project_registration(plan.project_id)
            if not registration_path.exists():
                removed_paths.append(registration_path)
        self._remove_project_archive_index_records(plan.project_id)
        self._prune_empty_parents(plan.project_id)
        removed_branches = delete_created_branches(plan.branch_removals)
        return self._result(plan, removed_paths, removed_branches)

    def _assert_not_project_execution_referenced(self, project_id: str, task_id: str) -> None:
        """Refuse to destroy a canonical Task while Project Execution references it."""
        try:
            directory = project_directory(self.control_root, project_id)
        except ProjectError:
            directory = self.control_root / "projects" / project_id
        definition = ProjectExecutionRepository(directory).load_optional()
        if definition is None:
            return
        references: list[str] = []
        if task_id in definition.task_index:
            references.append("project task membership")
        for gate in definition.gates:
            if any(criterion.task_id == task_id for criterion in gate.criteria):
                references.append(f"Gate {gate.id}")
        for milestone in definition.milestones:
            if task_id in milestone.requires.tasks:
                references.append(f"Milestone {milestone.id}")
        if references:
            raise PermanentRemovalError(
                "remove Project Execution references before permanently deleting "
                f"Task {task_id!r}: {', '.join(sorted(set(references)))}"
            )

    def _task_paths(self, project_id: str, task_id: str) -> list[Path]:
        paths: list[Path] = []
        for dossier in self._task_dossiers(project_id, task_id):
            if dossier.exists() or dossier.is_symlink():
                paths.append(dossier)
        for path in (
            local_manifest_registry_path(self.control_root, task_id),
            workspace_registry_path(self.control_root, task_id),
        ):
            if path.exists() or path.is_symlink():
                paths.append(path)
        identity = resolve_storage_identity(
            self.state_root,
            project_id=project_id,
            task_id=task_id,
            create=False,
        )
        for path in (identity.state_dir, identity.journal_path):
            if path.exists() or path.is_symlink():
                paths.append(path)
        completion = self.archive_root / project_id / task_id
        if completion.exists() or completion.is_symlink():
            paths.append(completion)
        start_journal = self.state_root / "starts" / project_id / f"{task_id}.yaml"
        if start_journal.exists() or start_journal.is_symlink():
            paths.append(start_journal)
        return paths

    def _project_owned_paths(self, project_id: str) -> list[Path]:
        paths: list[Path] = []
        internal = self.control_root / "projects" / project_id
        archived = self.catalog_archive_root / "projects" / project_id
        archived_tasks = self.catalog_archive_root / "tasks" / project_id
        for path in (archived, archived_tasks):
            if path.exists() or path.is_symlink():
                paths.append(path)
        if internal.is_dir() and not internal.is_symlink():
            paths.append(internal)
        return paths

    def _task_dossiers(self, project_id: str, task_id: str) -> Iterable[Path]:
        try:
            active_project = project_directory(self.control_root, project_id)
        except ProjectError:
            active_project = self.control_root / "projects" / project_id
        yield active_project / "tasks" / task_id
        yield self.catalog_archive_root / "tasks" / project_id / task_id
        yield self.catalog_archive_root / "projects" / project_id / "tasks" / task_id

    def _project_task_ids(self, project_id: str) -> list[str]:
        ids: set[str] = set()
        roots: list[Path] = []
        try:
            roots.append(project_directory(self.control_root, project_id))
        except ProjectError:
            pass
        roots.extend(
            (
                self.control_root / "projects" / project_id,
                self.catalog_archive_root / "projects" / project_id,
            )
        )
        for root in roots:
            tasks = root / "tasks"
            if not tasks.is_dir():
                continue
            for child in tasks.iterdir():
                if child.is_dir() and not child.is_symlink():
                    try:
                        ids.add(validate_task_id(child.name))
                    except Exception:
                        continue
        archived_tasks = self.catalog_archive_root / "tasks" / project_id
        if archived_tasks.is_dir():
            for child in archived_tasks.iterdir():
                if child.is_dir() and not child.is_symlink():
                    try:
                        ids.add(validate_task_id(child.name))
                    except Exception:
                        continue
        ids.update(self._registry_task_ids(project_id))
        return sorted(ids)

    def _registry_task_ids(self, project_id: str) -> set[str]:
        directory = local_manifest_registry_path(self.control_root, "placeholder").parent
        result: set[str] = set()
        if not directory.is_dir():
            return result
        for path in directory.glob("*.yaml"):
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError):
                continue
            if isinstance(data, Mapping) and str(data.get("project", "")).strip() == project_id:
                try:
                    result.add(validate_task_id(str(data.get("id", path.stem))))
                except Exception:
                    continue
        return result

    def _assert_registry_ownership(self, project_id: str, task_id: str) -> None:
        path = local_manifest_registry_path(self.control_root, task_id)
        if not path.is_file():
            return
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise PermanentRemovalError(
                f"cannot safely interpret task registry {path}: {exc}"
            ) from exc
        if not isinstance(data, Mapping):
            raise PermanentRemovalError(f"task registry must contain a mapping: {path}")
        owner = str(data.get("project", "")).strip()
        if owner and owner != project_id:
            raise PermanentRemovalError(
                f"task ID {task_id!r} is registered to project {owner!r}; refusing "
                f"to remove ambiguous workspace/registry state for project {project_id!r}"
            )

    def _validate_archive_index(self) -> None:
        index = self.archive_root / "index.json"
        if not index.is_file():
            return
        try:
            data = json.loads(index.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PermanentRemovalError(
                f"cannot safely interpret completion archive index {index}: {exc}"
            ) from exc
        if not isinstance(data, list):
            raise PermanentRemovalError(
                f"completion archive index must be a JSON array: {index}"
            )

    def _workspace_record(self, task_id: str) -> WorkspaceRecord | None:
        path = workspace_registry_path(self.control_root, task_id)
        if not path.is_file():
            return None
        try:
            return load_workspace(self.control_root, task_id)
        except Exception as exc:
            raise PermanentRemovalError(
                f"cannot safely interpret workspace registry for {task_id}: {exc}"
            ) from exc

    def _retire_workspace(self, task_id: str) -> None:
        record = self._workspace_record(task_id)
        if record is None:
            return
        root = Path(record.workspace_root).expanduser().resolve()
        if record.status == "removed" and not root.exists():
            return
        lifecycle = WorkspaceLifecycleService(
            self.control_root,
            self.state_root,
            archive_root=self.archive_root,
        )
        try:
            lifecycle.destroy(
                task_id,
                remove_shell=True,
                require_archive=False,
                force=False,
            )
        except Exception as exc:
            raise PermanentRemovalError(
                f"workspace retirement failed for {task_id}; no catalog state was deleted: {exc}"
            ) from exc

    def _require_task_exists(self, project_id: str, task_id: str) -> None:
        if any(path.exists() for path in self._task_dossiers(project_id, task_id)):
            return
        manifest = local_manifest_registry_path(self.control_root, task_id)
        if manifest.is_file():
            try:
                data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError):
                data = {}
            if isinstance(data, Mapping) and str(data.get("project", "")).strip() == project_id:
                return
        raise PermanentRemovalError(f"task not found: {project_id}/{task_id}")

    def _project_exists(self, project_id: str, task_ids: list[str]) -> bool:
        if task_ids or load_project_registration(project_id) is not None:
            return True
        return any(
            path.exists()
            for path in (
                self.control_root / "projects" / project_id,
                self.catalog_archive_root / "projects" / project_id,
                self.catalog_archive_root / "tasks" / project_id,
            )
        )

    def _is_external_project(self, project_id: str) -> bool:
        registration = load_project_registration(project_id)
        if registration is None or registration.descriptor is None:
            return False
        internal_root = (self.control_root / "projects").resolve()
        try:
            registration.descriptor.resolve().relative_to(internal_root)
            return False
        except ValueError:
            return True

    def _driver_active(self, project_id: str, task_id: str) -> bool:
        identity = resolve_storage_identity(
            self.state_root,
            project_id=project_id,
            task_id=task_id,
            create=False,
        )
        for name in ("orchestrator-driver.lock", "orchestrator.lock"):
            try:
                if file_lock_is_held(identity.state_dir / name):
                    return True
            except LockUnavailableError:  # pragma: no cover - non-POSIX fallback
                return False
        return False

    def _remove_paths(self, paths: Iterable[Path]) -> list[Path]:
        removed: list[Path] = []
        # Deepest paths first avoids redundant child operations when a project
        # directory is also part of the plan.
        unique = sorted({path.expanduser().resolve() for path in paths}, key=lambda p: len(p.parts), reverse=True)
        for path in unique:
            if not path.exists() and not path.is_symlink():
                continue
            try:
                self._remove_path(path)
            except OSError as exc:
                raise PermanentRemovalError(
                    f"failed to remove Execraft-owned path {path}: {exc}"
                ) from exc
            removed.append(path)
        return removed

    @staticmethod
    def _remove_path(path: Path) -> None:
        if path.is_symlink():
            path.unlink()
            return
        if path.is_dir():
            PermanentRemovalService._make_tree_writable(path)
            shutil.rmtree(path)
            return
        try:
            path.chmod(path.stat().st_mode | 0o200)
        except OSError:
            pass
        path.unlink(missing_ok=True)

    @staticmethod
    def _make_tree_writable(root: Path) -> None:
        for current, directories, files in os.walk(root, topdown=False):
            for name in files:
                path = Path(current) / name
                if not path.is_symlink():
                    try:
                        path.chmod(path.stat().st_mode | 0o600)
                    except OSError:
                        pass
            for name in directories:
                path = Path(current) / name
                if not path.is_symlink():
                    try:
                        path.chmod(path.stat().st_mode | 0o700)
                    except OSError:
                        pass
        try:
            root.chmod(root.stat().st_mode | 0o700)
        except OSError:
            pass

    def _remove_archive_index_records(self, project_id: str, task_id: str) -> None:
        self._rewrite_archive_index(
            lambda item: not (
                str(item.get("project", "")) == project_id
                and str(item.get("task_id", "")) == task_id
            )
        )

    def _remove_project_archive_index_records(self, project_id: str) -> None:
        self._rewrite_archive_index(
            lambda item: str(item.get("project", "")) != project_id
        )

    def _rewrite_archive_index(self, keep: Any) -> None:
        index = self.archive_root / "index.json"
        if not index.is_file():
            return
        try:
            data = json.loads(index.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PermanentRemovalError(f"cannot update completion archive index: {exc}") from exc
        if not isinstance(data, list):
            raise PermanentRemovalError(f"completion archive index must be a JSON array: {index}")
        filtered = [item for item in data if not isinstance(item, Mapping) or keep(item)]
        if filtered == data:
            return
        try:
            atomic_write_json(
                index,
                filtered,
                indent=2,
                trailing_newline=True,
            )
        except OSError as exc:
            raise PermanentRemovalError(
                f"cannot update completion archive index {index}: {exc}"
            ) from exc

    def _clear_active_task_if_matches(self, task_id: str) -> None:
        path = active_task_storage_path(self.control_root)
        if path.is_file() and path.read_text(encoding="utf-8").strip() == task_id:
            clear_active_task(self.control_root, task_id)

    def _prune_empty_parents(self, project_id: str) -> None:
        candidates = (
            self.catalog_archive_root / "tasks" / project_id,
            self.archive_root / project_id,
            self.state_root / "starts" / project_id,
        )
        for path in candidates:
            try:
                path.rmdir()
            except OSError:
                pass

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        try:
            path.expanduser().resolve().relative_to(root.expanduser().resolve())
            return True
        except ValueError:
            return False

    @staticmethod
    def _deduplicated(plan: RemovalPlan) -> RemovalPlan:
        plan.paths = list(dict.fromkeys(path.expanduser().resolve() for path in plan.paths))
        unique_branches = {
            (item.source_path, item.branch): item for item in plan.branch_removals
        }
        plan.branch_removals = list(unique_branches.values())
        plan.preserved = list(dict.fromkeys(plan.preserved))
        return plan

    @staticmethod
    def _result(
        plan: RemovalPlan,
        removed_paths: Iterable[Path],
        removed_branches: Iterable[str],
        *,
        dry_run: bool = False,
    ) -> RemovalResult:
        return RemovalResult(
            kind=plan.kind,
            project_id=plan.project_id,
            item_id=plan.item_id,
            removed_paths=tuple(str(path) for path in removed_paths),
            removed_branches=tuple(removed_branches),
            preserved=tuple(plan.preserved),
            dry_run=dry_run,
        )
