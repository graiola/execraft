"""Task-definition, replanning, and completion projections for the local GUI.

The dashboard is a control surface over the canonical task lifecycle services. This
module deliberately owns no second task lifecycle implementation: it renders
safe read models, selects the same read-only planning provider as the CLI, and
delegates mutations to :class:`ReplanService` and :class:`TaskCompletionService`.

Large task-definition documents are loaded only by the dedicated lifecycle
endpoint rather than every dashboard poll.
"""

from __future__ import annotations

import difflib
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from execraft.completion import task_completion_policy_from_scheduling
from execraft.completion.models import TaskCompletionError
from execraft.completion.service import TaskCompletionService
from execraft.gui.errors import GuiError
from execraft.gui.final_sync import FinalSyncLifecycleMixin
from execraft.onboarding.selection import ProviderSelector
from execraft.onboarding.task_definition import (
    TaskDefinitionError,
    TaskDefinitionInput,
    TaskDefinitionService,
)
from execraft.orchestrate.identity import resolve_storage_identity
from execraft.orchestrate.models import TaskExecutionStateRecord, WorkPackageKind
from execraft.repository_sync.card_request import (
    RepositorySyncCardRequest,
    RepositorySyncCardRequestError,
)
from execraft.repository_sync.planning import (
    RepositorySyncPlanningError,
    build_sync_before_definition,
    validate_sync_repository_selection,
)
from execraft.repository_sync.policy import RepositorySyncPolicy
from execraft.repository_sync.service import RepositorySyncService
from execraft.project import ProjectDescriptor
from execraft.replan import ReplanError
from execraft.replan.service import ReplanInputs, ReplanService
from execraft.workspace.task_git import (
    TaskGitError,
    TaskManifest,
    current_branch,
    git_operation,
    utc_now,
    working_tree_dirty,
)
from execraft.workspace.workspace_git import WorkspaceRecord, load_workspace

_MAX_DIFF_CHARS = 512 * 1024
_REPLAN_TRANSACTION = "replan-transaction.yaml"
_GENERATE_PLAN_REQUEST = (
    "Use the accepted BRIEF.md as the authoritative specification and preserve it unchanged. "
    "Replace any placeholder PLAN.md with a complete implementation plan and generate a "
    "coherent PLAN.graph.yaml. Divide the work into ordered, independently verifiable work "
    "packages; use only repositories declared by the task; and include dependencies, risks, "
    "priorities, requirements, acceptance criteria, affected repositories, and verification "
    "profiles."
)


@dataclass(frozen=True)
class _DocumentDiff:
    name: str
    text: str
    truncated: bool

    def as_mapping(self) -> dict[str, Any]:
        return {"name": self.name, "text": self.text, "truncated": self.truncated}


class TaskLifecycleController(FinalSyncLifecycleMixin):
    """Compose task lifecycle read models and canonical task mutations."""

    def __init__(
        self,
        *,
        control_root: Path,
        state_root: Path,
        project: ProjectDescriptor,
        task_id: str,
        task_dir: Path,
    ) -> None:
        self.control_root = Path(control_root).expanduser().resolve()
        self.state_root = Path(state_root).expanduser().resolve()
        self.project = project
        self.task_id = str(task_id).strip()
        self.task_dir = Path(task_dir).expanduser().resolve()
        self.definitions = TaskDefinitionService()
        self._plan_generation_lock = threading.Lock()
        self._plan_generation: dict[str, Any] = {
            "status": "idle",
            "provider_id": "",
            "candidate_id": "",
            "error": "",
        }
        # Updated only by explicit sync workbench actions; normal dashboard
        # polling reads this cache without running Git/network commands.
        self._sync_card_summaries: dict[str, dict[str, Any]] = {}

    def summary(self) -> dict[str, Any]:
        """Return a small lifecycle projection suitable for dashboard polling."""

        manifest_error = ""
        try:
            task_status = self._manifest().status
        except GuiError as exc:
            # The dashboard has historically tolerated partially migrated task
            # manifests so operators can inspect and repair them. Lifecycle
            # mutations still require the canonical validated TaskManifest.
            task_status = self._raw_manifest_status()
            manifest_error = str(exc)
        definition = self._definition_summary()
        completion = self._completion_summary()
        workspace = self._workspace_summary(inspect_git=False)
        transaction = self._replan_transaction_summary()
        return {
            "task_status": task_status,
            "manifest_error": manifest_error,
            "definition": definition,
            "plan_generation": self.plan_generation_status(),
            "replan_transaction": transaction,
            "completion": completion,
            "workspace": workspace,
            "repository_sync": self._repository_sync_summary(),
        }

    def snapshot(self) -> dict[str, Any]:
        """Return the complete lazy-loaded lifecycle workbench model."""

        summary = self.summary()
        documents: dict[str, str] = {}
        definition_error = ""
        try:
            documents = self.definitions.live_documents(self.task_dir, require_complete=False)
        except (TaskDefinitionError, OSError, UnicodeDecodeError) as exc:
            definition_error = str(exc)
        detailed_workspace = self._workspace_summary(inspect_git=True)
        detailed_summary = {**summary, "workspace": detailed_workspace}
        return {
            **detailed_summary,
            "documents": documents,
            "definition_error": definition_error,
            "pending_candidates": self._pending_candidates(),
            "revision_history": self._revision_history(),
            "completion_preview": self._completion_preview(),
            "final_sync": self._final_sync_summary(),
            "retained_resources": self._retained_resources(detailed_summary),
            "repository_sync_detail": self.repository_sync_snapshot(refresh=False),
        }
    def create_candidate(
        self,
        *,
        requested_change: str,
        brief_markdown: str,
        plan_markdown: str,
        plan_graph_yaml: str,
        package_mapping: Mapping[str, str],
        provider_id: str = "",
        from_current_files: bool = False,
        allow_structural_consistency: bool = False,
        allow_incomplete_definition: bool = False,
        preserve_current_brief: bool = False,
    ) -> dict[str, Any]:
        service = self._replan_service()
        definition = TaskDefinitionInput.from_contents(
            brief_markdown=brief_markdown,
            plan_markdown=plan_markdown,
            plan_graph_yaml=plan_graph_yaml,
            brief_source="gui-editor:BRIEF.md" if brief_markdown else "",
            plan_source="gui-editor:PLAN.md" if plan_markdown else "",
            plan_graph_source="gui-editor:PLAN.graph.yaml" if plan_graph_yaml else "",
        )
        if not (
            requested_change.strip()
            or definition.supplied
            or from_current_files
        ):
            raise GuiError(
                "replanning requires a change request, edited definition documents, "
                "or adoption of the current files"
            )
        try:
            # Structural-only mode is an explicit operator decision, not a
            # provider fallback. With no requested provider, honor it without
            # making an unnecessary model call that can block drift recovery.
            provider = None
            if provider_id.strip() or not allow_structural_consistency:
                provider = ProviderSelector().select(
                    self.project,
                    workdir=self._replan_workdir(),
                    requested=provider_id.strip(),
                )
            candidate = service.create_candidate(
                ReplanInputs(
                    requested_change=requested_change,
                    definition=definition,
                    from_current_files=from_current_files,
                    package_mapping=dict(package_mapping),
                    allow_structural_consistency=allow_structural_consistency,
                    allow_incomplete_definition=allow_incomplete_definition,
                    preserve_current_brief=preserve_current_brief,
                ),
                provider=provider,
                workdir=self._replan_workdir(),
            )
            return self._candidate_view(candidate.candidate_id)
        except (ReplanError, TaskDefinitionError, TaskGitError, OSError, ValueError) as exc:
            raise GuiError(str(exc)) from exc

    def generate_plan_from_brief(self, *, provider_id: str = "") -> dict[str, Any]:
        """Stage a complete plan and graph generated from the accepted brief."""

        documents = self.definitions.live_documents(
            self.task_dir, require_complete=False
        )
        if not documents.get("BRIEF.md", "").strip():
            raise GuiError("plan generation requires a non-empty accepted BRIEF.md")
        with self._plan_generation_lock:
            if self._plan_generation["status"] == "running":
                raise GuiError("plan generation is already running for this task")
            self._plan_generation = {
                "status": "running",
                "provider_id": provider_id.strip(),
                "candidate_id": "",
                "error": "",
                "started_at": utc_now(),
            }
        try:
            result = self.create_candidate(
                requested_change=_GENERATE_PLAN_REQUEST,
                brief_markdown="",
                plan_markdown="",
                plan_graph_yaml="",
                package_mapping={},
                provider_id=provider_id,
                allow_structural_consistency=False,
                allow_incomplete_definition=True,
                preserve_current_brief=True,
            )
        except Exception as exc:
            with self._plan_generation_lock:
                self._plan_generation.update(
                    {"status": "failed", "error": str(exc), "finished_at": utc_now()}
                )
            raise
        with self._plan_generation_lock:
            self._plan_generation.update(
                {
                    "status": "candidate_ready",
                    "candidate_id": str(result.get("candidate_id", "")),
                    "error": "",
                    "finished_at": utc_now(),
                }
            )
        return result

    def plan_generation_status(self) -> dict[str, Any]:
        with self._plan_generation_lock:
            return dict(self._plan_generation)

    def candidate(self, candidate_id: str) -> dict[str, Any]:
        try:
            return self._candidate_view(candidate_id)
        except (ReplanError, TaskDefinitionError, TaskGitError, OSError, ValueError) as exc:
            raise GuiError(str(exc)) from exc

    def apply_candidate(self, candidate_id: str) -> dict[str, Any]:
        try:
            result = self._replan_service().apply_candidate(candidate_id)
        except (ReplanError, TaskDefinitionError, TaskGitError, OSError, ValueError) as exc:
            raise GuiError(str(exc)) from exc
        with self._plan_generation_lock:
            if self._plan_generation.get("candidate_id") == candidate_id:
                self._plan_generation.update(
                    {"status": "applied", "error": "", "finished_at": utc_now()}
                )
        return {
            "result": result.as_mapping(),
            "lifecycle": self.snapshot(),
        }

    def recover_replan(self) -> dict[str, Any]:
        try:
            recovered = self._replan_service().recover_incomplete()
        except (ReplanError, TaskDefinitionError, TaskGitError, OSError, ValueError) as exc:
            raise GuiError(str(exc)) from exc
        return {"recovered": recovered, "lifecycle": self.snapshot()}

    def complete(self, *, dry_run: bool = False) -> dict[str, Any]:
        service = self._completion_service()
        try:
            result = service.complete(self.task_id, dry_run=dry_run)
        except (TaskCompletionError, TaskGitError, OSError, ValueError) as exc:
            report_path = getattr(exc, "report_path", None)
            detail = str(exc)
            if report_path:
                detail = f"{detail} (report: {report_path})"
            raise GuiError(detail) from exc
        return {
            "result": result.as_mapping(),
            "lifecycle": self.snapshot(),
        }

    def repository_sync_snapshot(self, *, refresh: bool = False) -> dict[str, Any]:
        """Return lazy divergence/transaction details for synchronization Work Packages."""

        state = self._state_record()
        if state is None:
            return {"available": False, "packages": [], "policy": self._repository_sync_policy().as_mapping()}
        sync_packages = [
            package
            for package in state.plan_graph.work_packages
            if package.kind == WorkPackageKind.REPOSITORY_SYNC
            and package.repository_sync is not None
        ]
        if not sync_packages:
            return {"available": True, "packages": [], "policy": self._repository_sync_policy().as_mapping()}
        try:
            service = self._repository_sync_service()
        except (TaskGitError, GuiError, OSError, ValueError) as exc:
            return {
                "available": False,
                "error": str(exc),
                "packages": [],
                "policy": self._repository_sync_policy().as_mapping(),
            }
        policy = self._repository_sync_policy()
        rows = []
        for package in sync_packages:
            divergence = service.divergence(package.repository_sync, refresh=refresh)
            rows.append(
                {
                    "id": package.id,
                    "title": package.title,
                    "stage": package.stage.value,
                    "status": package.status,
                    "dependencies": list(package.dependencies),
                    "spec": package.repository_sync.as_mapping(),
                    "repositories": [
                        {**item.as_mapping(), "severity": policy.severity(item.behind)}
                        for item in divergence
                    ],
                    "transaction": service.transaction_report(package.id),
                }
            )
        return {"available": True, "packages": rows, "policy": policy.as_mapping()}

    def repository_sync_card_summaries(self) -> dict[str, dict[str, Any]]:
        """Return cached card badges without touching Git or the network."""

        return {key: dict(value) for key, value in self._sync_card_summaries.items()}

    def repository_sync_options(
        self, package_id: str, *, refresh: bool = False
    ) -> dict[str, Any]:
        """Return repository/remote-branch choices for one Work Package card."""

        state = self._state_record()
        if state is None:
            raise GuiError("orchestration state is not initialized")
        package_id = str(package_id).strip()
        try:
            package = state.plan_graph.package_by_id(package_id)
        except Exception as exc:
            raise GuiError(str(exc)) from exc
        if package.stage.value == "completed":
            raise GuiError("completed Work Packages cannot schedule Pause & Sync")
        if package.parent_id:
            raise GuiError("Pause & Sync is available only on top-level Work Packages")
        if package.kind == WorkPackageKind.REPOSITORY_SYNC:
            raise GuiError("repository-sync Work Packages cannot schedule another sync from their card")
        mode = (
            "before"
            if package.stage.value == "prepare" and package.status == "pending"
            else "after"
        )
        manifest = self._manifest()
        available = {item.id for item in self.project.repositories}
        selected = set(package.affected_repositories) & available
        service = self._repository_sync_service()
        repositories: list[dict[str, Any]] = []
        largest_behind = 0
        behind_repositories = 0
        for repository in manifest.repositories:
            if repository.mutability != "task_owned" or repository.id not in available:
                continue
            try:
                branches = service.remote_branches(
                    repository.id, remote="origin", refresh=refresh
                )
                branch_rows = [item.as_mapping() for item in branches]
            except Exception as exc:
                branch_rows = []
                branch_error = str(exc)
            else:
                branch_error = ""
            default_divergence: dict[str, Any] = {}
            if repository.id in selected:
                try:
                    row = service.divergence_for_branch(
                        repository.id,
                        remote="origin",
                        source_branch=repository.base_branch,
                        refresh=refresh,
                    )
                    default_divergence = row.as_mapping()
                    if not row.error and row.behind > 0:
                        behind_repositories += 1
                        largest_behind = max(largest_behind, row.behind)
                except Exception as exc:
                    default_divergence = {"error": str(exc)}
            repositories.append(
                {
                    "id": repository.id,
                    "selected": repository.id in selected,
                    "configured_base_branch": repository.base_branch,
                    "task_branch": repository.task_branch,
                    "remote": "origin",
                    "branches": branch_rows,
                    "branch_error": branch_error,
                    "divergence": default_divergence,
                }
            )
        summary = {
            "behind_repositories": behind_repositories,
            "largest_behind": largest_behind,
            "mode": mode,
            "refreshed": bool(refresh),
        }
        self._sync_card_summaries[package_id] = summary
        return {
            "package_id": package_id,
            "title": package.title,
            "mode": mode,
            "repositories": repositories,
            "summary": summary,
        }

    def repository_sync_preview_selection(
        self,
        *,
        package_id: str,
        repositories: list[str],
        source_branches: Mapping[str, str],
        remote: str = "origin",
    ) -> dict[str, Any]:
        """Fetch only selected refs and preview exact ahead/behind divergence."""

        state = self._state_record()
        if state is None:
            raise GuiError("orchestration state is not initialized")
        available = {item.id for item in self.project.repositories}
        unavailable = sorted(set(repositories) - available)
        if unavailable:
            raise GuiError(
                "repository synchronization references repositories no longer in the project: "
                + ", ".join(unavailable)
            )
        try:
            package = state.plan_graph.package_by_id(str(package_id).strip())
        except Exception as exc:
            raise GuiError(str(exc)) from exc
        manifest = self._manifest()
        mode = (
            "before"
            if package.stage.value == "prepare" and package.status == "pending"
            else "after"
        )
        try:
            request = RepositorySyncCardRequest.create(
                manifest=manifest,
                package_id=package.id,
                mode=mode,
                repositories=repositories,
                source_branches=source_branches,
                remote=remote,
                auto_resume=True,
            )
        except RepositorySyncCardRequestError as exc:
            raise GuiError(str(exc)) from exc
        service = self._repository_sync_service()
        rows: list[dict[str, Any]] = []
        largest_behind = 0
        behind_repositories = 0
        for repository_id in request.repositories:
            repository = next(
                item for item in manifest.repositories if item.id == repository_id
            )
            branch = request.source_branches.get(repository_id) or repository.base_branch
            row = service.divergence_for_branch(
                repository_id, remote=request.remote, source_branch=branch, refresh=True
            )
            mapping = row.as_mapping()
            mapping["configured_base_branch"] = repository.base_branch
            mapping["source_selection"] = (
                "operator_override"
                if branch != repository.base_branch
                else "configured_base"
            )
            rows.append(mapping)
            if not row.error and row.behind > 0:
                behind_repositories += 1
                largest_behind = max(largest_behind, row.behind)
        summary = {
            "behind_repositories": behind_repositories,
            "largest_behind": largest_behind,
            "mode": request.mode,
            "refreshed": True,
        }
        self._sync_card_summaries[package.id] = summary
        return {
            "package_id": package.id,
            "mode": request.mode,
            "repositories": rows,
            "summary": summary,
        }

    def prepare_repository_sync_card_request(
        self,
        *,
        package_id: str,
        repositories: list[str],
        source_branches: Mapping[str, str],
        remote: str,
        conflict_policy: str,
        sync_package_id: str,
        auto_resume: bool,
    ) -> RepositorySyncCardRequest:
        state = self._state_record()
        if state is None:
            raise GuiError("orchestration state is not initialized")
        try:
            package = state.plan_graph.package_by_id(str(package_id).strip())
        except Exception as exc:
            raise GuiError(str(exc)) from exc
        if package.stage.value == "completed":
            raise GuiError("completed Work Packages cannot schedule Pause & Sync")
        if package.parent_id:
            raise GuiError("Pause & Sync is available only on top-level Work Packages")
        if package.kind == WorkPackageKind.REPOSITORY_SYNC:
            raise GuiError("repository-sync Work Packages cannot schedule another synchronization")
        available = {item.id for item in self.project.repositories}
        selected = repositories or [
            item for item in package.affected_repositories if item in available
        ]
        unavailable = sorted(set(selected) - available)
        if unavailable:
            raise GuiError(
                "repository synchronization references repositories no longer in the project: "
                + ", ".join(unavailable)
            )
        mode = (
            "before"
            if package.stage.value == "prepare" and package.status == "pending"
            else "after"
        )
        try:
            return RepositorySyncCardRequest.create(
                manifest=self._manifest(),
                package_id=package.id,
                mode=mode,
                repositories=selected,
                source_branches=source_branches,
                remote=remote,
                conflict_policy=conflict_policy,
                sync_package_id=sync_package_id,
                auto_resume=auto_resume,
            )
        except RepositorySyncCardRequestError as exc:
            raise GuiError(str(exc)) from exc

    def create_repository_sync_before(
        self,
        *,
        before_package_id: str,
        repositories: list[str],
        source_branches: Mapping[str, str],
        remote: str = "origin",
        conflict_policy: str = "ai_resolve",
        sync_package_id: str = "",
        apply: bool = False,
    ) -> dict[str, Any]:
        """Stage/apply a synchronization Work Package through the definition service."""

        try:
            current = self.definitions.live_documents(self.task_dir, require_complete=True)
            selected = list(repositories)
            if not selected:
                state = self._state_record()
                if state is None:
                    raise GuiError("orchestration state is not initialized")
                selected = list(
                    state.plan_graph.package_by_id(before_package_id).affected_repositories
                )
            selected = list(
                validate_sync_repository_selection(self._manifest(), selected)
            )
            insertion = build_sync_before_definition(
                brief_markdown=current["BRIEF.md"],
                plan_markdown=current["PLAN.md"],
                plan_graph_yaml=current["PLAN.graph.yaml"],
                before_package_id=before_package_id,
                repositories=selected,
                source_branches=source_branches,
                remote=remote,
                conflict_policy=conflict_policy,
                sync_package_id=sync_package_id,
            )
            candidate = self._replan_service().create_candidate(
                ReplanInputs(
                    requested_change=(
                        f"Insert {insertion.sync_package_id} before "
                        f"{insertion.target_package_id} as a repository synchronization Work Package."
                    ),
                    definition=insertion.definition,
                    allow_structural_consistency=True,
                ),
                provider=None,
                workdir=self._replan_workdir(),
            )
            candidate_view = self._candidate_view(candidate.candidate_id)
            if not apply:
                return {
                    "candidate": candidate_view,
                    "repository_sync": {
                        "package_id": insertion.sync_package_id,
                        "before": insertion.target_package_id,
                        "repositories": list(insertion.repositories),
                    },
                }
            result = self._replan_service().apply_candidate(candidate.candidate_id)
            return {
                "candidate": candidate_view,
                "result": result.as_mapping(),
                "lifecycle": self.snapshot(),
            }
        except (
            ReplanError,
            RepositorySyncPlanningError,
            TaskDefinitionError,
            TaskGitError,
            OSError,
            ValueError,
        ) as exc:
            raise GuiError(str(exc)) from exc

    def _candidate_view(self, candidate_id: str) -> dict[str, Any]:
        service = self._replan_service()
        candidate = service.load_candidate(candidate_id)
        current = self.definitions.live_documents(
            self.task_dir, require_complete=False
        )
        current = {
            name: current.get(name, "")
            for name in ("BRIEF.md", "PLAN.md", "PLAN.graph.yaml")
        }
        candidate_docs = {
            "BRIEF.md": candidate.brief_markdown,
            "PLAN.md": candidate.plan_markdown,
            "PLAN.graph.yaml": candidate.plan_graph_yaml,
        }
        diffs = [
            self._document_diff(name, current[name], candidate_docs[name]).as_mapping()
            for name in ("BRIEF.md", "PLAN.md", "PLAN.graph.yaml")
        ]
        return {
            **candidate.as_mapping(),
            "documents": candidate_docs,
            "diffs": diffs,
        }

    def _definition_summary(self) -> dict[str, Any]:
        try:
            metadata = self.definitions.load_metadata(self.task_dir)
            integrity = self.definitions.integrity_report(self.task_dir)
            documents = self.definitions.live_documents(
                self.task_dir, require_complete=False
            )
            execution_missing = [
                name
                for name in ("PLAN.md", "PLAN.graph.yaml")
                if not documents.get(name, "").strip()
            ]
            return {
                "available": True,
                "revision": integrity.revision,
                "integrity_ok": integrity.ok,
                "changed_documents": list(integrity.changed_documents),
                "unexpected_documents": list(integrity.unexpected_documents),
                "missing_documents": list(integrity.missing_documents),
                "executable": not execution_missing,
                "execution_missing_documents": execution_missing,
                "definition_sha256": str(metadata.get("definition_sha256", "")),
                "revision_path": str(metadata.get("revision_path", "")),
                "consistency": dict(metadata.get("consistency") or {}),
            }
        except (TaskDefinitionError, OSError, ValueError) as exc:
            return {
                "available": False,
                "revision": 0,
                "integrity_ok": False,
                "changed_documents": [],
                "unexpected_documents": [],
                "missing_documents": [],
                "reason": str(exc),
            }

    def _completion_summary(self) -> dict[str, Any]:
        try:
            result = self._completion_service().status(self.task_id)
        except (GuiError, TaskCompletionError, TaskGitError, OSError, ValueError) as exc:
            return {"status": "unavailable", "phase": "", "reason": str(exc)}
        if result is None:
            return {"status": "not_started", "phase": "", "resumable": False}
        mapping = result.as_mapping()
        mapping["resumable"] = result.status not in {"completed"}
        return mapping

    def _completion_preview(self) -> dict[str, Any]:
        try:
            result = self._completion_service().complete(self.task_id, dry_run=True)
        except (GuiError, TaskCompletionError, TaskGitError, OSError, ValueError) as exc:
            return {"eligible": False, "reason": str(exc), "actions": []}
        return {
            "eligible": True,
            "reason": "",
            "actions": list(result.actions),
            "result": result.as_mapping(),
        }

    def _workspace_summary(self, *, inspect_git: bool) -> dict[str, Any]:
        try:
            record = load_workspace(self.control_root, self.task_id)
        except (TaskGitError, OSError, ValueError) as exc:
            return {
                "available": False,
                "status": "unavailable",
                "reason": str(exc),
                "repositories": [],
            }
        return self._workspace_record_summary(record, inspect_git=inspect_git)

    def _workspace_record_summary(
        self, record: WorkspaceRecord, *, inspect_git: bool
    ) -> dict[str, Any]:
        repositories: list[dict[str, Any]] = []
        for item in record.repositories:
            worktree = Path(str(item.get("worktree_path", ""))).expanduser().resolve()
            source = Path(str(item.get("source_path", ""))).expanduser().resolve()
            row: dict[str, Any] = {
                "id": str(item.get("id", "")),
                "mutability": str(item.get("mutability", "task_owned")),
                "role": str(item.get("role", "")),
                "source_path": str(source),
                "worktree_path": str(worktree),
                "worktree_exists": worktree.is_dir(),
                "source_exists": source.is_dir(),
                "expected_branch": str(item.get("task_branch", item.get("branch", ""))),
                "branch": "",
                "dirty": False,
                "git_operation": "",
                "inspection_error": "",
            }
            if inspect_git and worktree.is_dir() and row["mutability"] == "task_owned":
                try:
                    row["branch"] = current_branch(worktree)
                    row["dirty"] = working_tree_dirty(worktree)
                    row["git_operation"] = git_operation(worktree) or ""
                except (TaskGitError, OSError, ValueError) as exc:
                    row["inspection_error"] = str(exc)
            repositories.append(row)
        workspace_root = Path(record.workspace_root).expanduser().resolve()
        return {
            "available": True,
            "status": record.status,
            "workspace_root": str(workspace_root),
            "shell_exists": workspace_root.is_dir(),
            "compose_project": record.compose_project,
            "runtime_status": record.runtime_status,
            "last_lifecycle_action": record.last_lifecycle_action,
            "last_lifecycle_at": record.last_lifecycle_at,
            "policy_profile": record.policy_profile,
            "capabilities": list(record.capabilities),
            "repositories": repositories,
        }

    def _retained_resources(self, summary: Mapping[str, Any]) -> list[dict[str, Any]]:
        try:
            manifest = self._manifest()
        except GuiError:
            manifest = None
        completion = summary.get("completion") or {}
        workspace = summary.get("workspace") or {}
        rows: list[dict[str, Any]] = [
            {
                "kind": "live_dossier",
                "label": "Live task dossier",
                "value": str(self.task_dir),
                "retained": self.task_dir.is_dir(),
            },
            {
                "kind": "workspace_tombstone",
                "label": "Workspace registry/tombstone",
                "value": str(workspace.get("status", "unavailable")),
                "retained": bool(workspace.get("available", False)),
            },
        ]
        for repository in (manifest.repositories if manifest is not None else []):
            rows.append(
                {
                    "kind": "task_branch",
                    "label": f"Task branch · {repository.id}",
                    "value": repository.task_branch,
                    "retained": True,
                }
            )
        archive_path = str(completion.get("archive_path", "")).strip()
        if archive_path:
            rows.append(
                {
                    "kind": "completion_archive",
                    "label": "Completion archive",
                    "value": archive_path,
                    "retained": Path(archive_path).expanduser().exists(),
                }
            )
        return rows

    def _pending_candidates(self) -> list[dict[str, Any]]:
        root = self.task_dir / "revisions"
        if not root.is_dir() or root.is_symlink():
            return []
        result: list[dict[str, Any]] = []
        service = self._replan_service()
        for path in sorted(root.glob("pending-*"), key=lambda item: item.name, reverse=True):
            if not path.is_dir() or path.is_symlink():
                continue
            candidate_id = path.name.removeprefix("pending-")
            try:
                candidate = service.load_candidate(candidate_id)
                result.append(candidate.as_mapping())
            except (ReplanError, OSError, ValueError) as exc:
                result.append(
                    {
                        "candidate_id": candidate_id,
                        "invalid": True,
                        "error": str(exc),
                        "path": str(path),
                    }
                )
        return result

    def _revision_history(self) -> list[dict[str, Any]]:
        root = self.task_dir / "revisions"
        if not root.is_dir() or root.is_symlink():
            return []
        rows: list[dict[str, Any]] = []
        for path in sorted(root.glob("revision-*"), key=lambda item: item.name, reverse=True):
            if not path.is_dir() or path.is_symlink():
                continue
            try:
                revision = int(path.name.rsplit("-", 1)[-1])
            except ValueError:
                continue
            row: dict[str, Any] = {
                "revision": revision,
                "path": str(path),
                "candidate_id": "",
                "applied": (path / "APPLIED.yaml").is_file() or revision == 1,
            }
            candidate_path = path / "CANDIDATE.yaml"
            if candidate_path.is_file() and not candidate_path.is_symlink():
                try:
                    raw = yaml.safe_load(candidate_path.read_text(encoding="utf-8")) or {}
                    if isinstance(raw, Mapping):
                        row["candidate_id"] = str(raw.get("candidate_id", ""))
                        row["requested_change"] = str(raw.get("requested_change", ""))
                        row["generated_by"] = str(raw.get("generated_by", ""))
                        row["consistency_mode"] = str(raw.get("consistency_mode", ""))
                except (OSError, yaml.YAMLError):
                    row["metadata_error"] = "revision metadata is unreadable"
            rows.append(row)
        return rows

    def _replan_transaction_summary(self) -> dict[str, Any]:
        identity = resolve_storage_identity(
            self.state_root,
            project_id=self.project.id,
            task_id=self.task_id,
            create=False,
        )
        path = identity.state_dir / _REPLAN_TRANSACTION
        if not path.is_file() or path.is_symlink():
            return {"present": False, "recoverable": False}
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            return {
                "present": True,
                "recoverable": False,
                "reason": f"replan transaction is unreadable: {exc}",
            }
        if not isinstance(raw, Mapping):
            return {
                "present": True,
                "recoverable": False,
                "reason": "replan transaction must contain a mapping",
            }
        status = str(raw.get("status", ""))
        return {
            "present": True,
            "recoverable": status != "completed",
            "status": status,
            "candidate_id": str(raw.get("candidate_id", "")),
            "revision": int(raw.get("revision", 0) or 0),
            "path": str(path),
        }

    def _state_record(self) -> TaskExecutionStateRecord | None:
        identity = resolve_storage_identity(
            self.state_root,
            project_id=self.project.id,
            task_id=self.task_id,
            create=False,
        )
        path = identity.state_dir / "state.json"
        if not path.is_file():
            return None
        try:
            import json

            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, Mapping):
                return None
            return TaskExecutionStateRecord.from_mapping(raw)
        except (OSError, ValueError, TypeError):
            return None

    def _repository_sync_summary(self) -> dict[str, Any]:
        state = self._state_record()
        if state is None:
            return {"count": 0, "pending": 0}
        packages = [
            package
            for package in state.plan_graph.work_packages
            if package.kind == WorkPackageKind.REPOSITORY_SYNC
        ]
        return {
            "count": len(packages),
            "pending": sum(1 for package in packages if package.stage.value != "completed"),
            "package_ids": [package.id for package in packages],
        }

    def _repository_sync_service(self) -> RepositorySyncService:
        workspace = load_workspace(self.control_root, self.task_id)
        repository_paths = {
            str(item.get("id", "")): Path(str(item.get("worktree_path", ""))).expanduser().resolve()
            for item in workspace.repositories
            if str(item.get("id", "")).strip()
        }
        identity = resolve_storage_identity(
            self.state_root,
            project_id=self.project.id,
            task_id=self.task_id,
            create=False,
        )
        return RepositorySyncService(
            state_dir=identity.state_dir,
            manifest=self._manifest(),
            repository_paths=repository_paths,
        )

    def _repository_sync_policy(self) -> RepositorySyncPolicy:
        agents_path = (
            self.project.configured_path("agents_file")
            or self.project.directory / "agents.yaml"
        )
        if not agents_path.is_file():
            return RepositorySyncPolicy()
        try:
            raw = yaml.safe_load(agents_path.read_text(encoding="utf-8")) or {}
            scheduling = raw.get("scheduling", {}) if isinstance(raw, Mapping) else {}
            repository_sync = (
                scheduling.get("repository_sync")
                if isinstance(scheduling, Mapping)
                else None
            )
            return RepositorySyncPolicy.from_mapping(repository_sync)
        except (OSError, yaml.YAMLError, ValueError):
            return RepositorySyncPolicy()

    def _replan_service(self) -> ReplanService:
        return ReplanService(
            control_root=self.control_root,
            state_root=self.state_root,
            project=self.project,
            manifest=self._manifest(),
            dossier=self.task_dir,
        )

    def _completion_service(self) -> TaskCompletionService:
        agents_path = (
            self.project.configured_path("agents_file")
            or self.project.directory / "agents.yaml"
        )
        scheduling: Mapping[str, Any] = {}
        if agents_path.is_file():
            try:
                raw = yaml.safe_load(agents_path.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError) as exc:
                raise GuiError(f"cannot read task completion policy: {exc}") from exc
            if not isinstance(raw, Mapping):
                raise GuiError("agents configuration must contain a mapping")
            scheduling_raw = raw.get("scheduling") or {}
            if not isinstance(scheduling_raw, Mapping):
                raise GuiError("agents scheduling must contain a mapping")
            scheduling = scheduling_raw
        try:
            policy = task_completion_policy_from_scheduling(scheduling)
        except ValueError as exc:
            raise GuiError(f"invalid task completion policy: {exc}") from exc
        return TaskCompletionService(
            self.control_root,
            self.state_root,
            policy=policy,
        )

    def _manifest(self) -> TaskManifest:
        path = self.task_dir / "TASK.yaml"
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise GuiError(f"cannot read task manifest: {exc}") from exc
        if not isinstance(raw, Mapping):
            raise GuiError("TASK.yaml must contain a mapping")
        try:
            manifest = TaskManifest.from_mapping(raw)
        except (TaskGitError, ValueError) as exc:
            raise GuiError(str(exc)) from exc
        if manifest.id != self.task_id or manifest.project != self.project.id:
            raise GuiError("task manifest identity does not match the open dashboard")
        return manifest

    def _raw_manifest_status(self) -> str:
        path = self.task_dir / "TASK.yaml"
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return "unavailable"
        if not isinstance(raw, Mapping):
            return "unavailable"
        return str(raw.get("status", "unknown")).strip() or "unknown"

    def _replan_workdir(self) -> Path:
        try:
            workspace = load_workspace(self.control_root, self.task_id)
            root = Path(workspace.workspace_root).expanduser().resolve()
            if root.is_dir():
                return root
        except (TaskGitError, OSError, ValueError):
            pass
        return self.task_dir

    @staticmethod
    def _document_diff(name: str, before: str, after: str) -> _DocumentDiff:
        text = "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"current/{name}",
                tofile=f"candidate/{name}",
                n=4,
            )
        )
        if len(text) <= _MAX_DIFF_CHARS:
            return _DocumentDiff(name=name, text=text, truncated=False)
        marker = "\n... diff truncated by GUI safety limit ...\n"
        return _DocumentDiff(
            name=name,
            text=text[: _MAX_DIFF_CHARS - len(marker)] + marker,
            truncated=True,
        )

__all__ = ["TaskLifecycleController"]
