"""Completion-time repository synchronization for dashboard façades."""

from __future__ import annotations

from typing import Any, Mapping

from execraft.gui.errors import GuiError
from execraft.onboarding.task_definition import TaskDefinitionError
from execraft.orchestrate.models import TaskExecutionState, TaskExecutionStateRecord
from execraft.repository_sync.planning import (
    RepositorySyncPlanningError,
    build_final_sync_definition,
    validate_sync_repository_selection,
)
from execraft.replan import ReplanError
from execraft.replan.service import ReplanInputs
from execraft.workspace.task_git import TaskGitError


def reconcile_run_status(
    run_status: Mapping[str, Any], record: TaskExecutionStateRecord | None
) -> dict[str, Any]:
    """Mark an old GUI-process failure superseded by durable completion."""

    result = dict(run_status)
    nonzero_stopped_driver = bool(
        result.get("last_exit_code") not in {None, 0}
        and not result.get("owned_running")
        and not result.get("external_running")
    )
    result["last_exit_superseded"] = bool(
        record is not None
        and record.state == TaskExecutionState.COMPLETED
        and record.total_packages > 0
        and record.completed_packages == record.total_packages
        and not record.error_message
        and nonzero_stopped_driver
    )
    # `execraft orchestrate run` intentionally returns non-zero when it reaches a
    # durable operator/environment check.  That is incomplete orchestration, not
    # a crashed GUI child process.  Expose the distinction so the dashboard can
    # render the actionable check instead of a misleading red process failure.
    result["last_exit_expected_control_hold"] = bool(
        record is not None
        and record.state
        in {
            TaskExecutionState.HUMAN_REQUIRED,
            TaskExecutionState.WAITING_FOR_ENVIRONMENT,
            TaskExecutionState.OPERATOR_PAUSED,
        }
        and nonzero_stopped_driver
    )
    return result


class FinalSyncDashboardMixin:
    """Thin dashboard-service forwarding surface for final synchronization."""

    task_lifecycle: Any

    def task_final_sync_preview(
        self,
        *,
        repositories: list[str],
        source_branches: Mapping[str, str],
        remote: str = "origin",
    ) -> dict[str, Any]:
        return self.task_lifecycle.final_repository_sync_preview(
            repositories=repositories,
            source_branches=source_branches,
            remote=remote,
        )

    def task_final_sync_apply(
        self,
        *,
        repositories: list[str],
        source_branches: Mapping[str, str],
        remote: str = "origin",
        conflict_policy: str = "ai_resolve",
        sync_package_id: str = "",
    ) -> dict[str, Any]:
        return self.task_lifecycle.create_final_repository_sync(
            repositories=repositories,
            source_branches=source_branches,
            remote=remote,
            conflict_policy=conflict_policy,
            sync_package_id=sync_package_id,
        )


class FinalSyncLifecycleMixin:
    """Operator-controlled final sync behavior composed into TaskLifecycleController."""

    def final_repository_sync_preview(
        self,
        *,
        repositories: list[str],
        source_branches: Mapping[str, str],
        remote: str = "origin",
    ) -> dict[str, Any]:
        self._require_completed_orchestration_for_final_sync()
        selected = self._final_sync_repositories(repositories)
        unknown_overrides = sorted(set(source_branches) - set(selected))
        if unknown_overrides:
            raise GuiError(
                "source-branch overrides reference repositories outside the selected set: "
                + ", ".join(unknown_overrides)
            )
        manifest = self._manifest()
        service = self._repository_sync_service()
        rows: list[dict[str, Any]] = []
        for repository_id in selected:
            repository = next(
                item for item in manifest.repositories if item.id == repository_id
            )
            branch = source_branches.get(repository_id) or repository.base_branch
            try:
                divergence = service.divergence_for_branch(
                    repository_id,
                    remote=remote,
                    source_branch=branch,
                    refresh=True,
                )
            except Exception as exc:
                raise GuiError(
                    f"cannot inspect final upstream divergence for {repository_id}: {exc}"
                ) from exc
            row = divergence.as_mapping()
            if row.get("error"):
                raise GuiError(
                    f"cannot inspect final upstream divergence for {repository_id}: "
                    f"{row['error']}"
                )
            if row.get("git_operation"):
                raise GuiError(
                    f"cannot schedule final synchronization while {repository_id} has "
                    f"an active Git operation: {row['git_operation']}"
                )
            if row.get("dirty"):
                raise GuiError(
                    f"cannot schedule final synchronization while {repository_id} "
                    "has uncommitted changes"
                )
            row["configured_base_branch"] = repository.base_branch
            row["source_selection"] = (
                "operator_override"
                if branch != repository.base_branch
                else "configured_base"
            )
            rows.append(row)
        behind = [row for row in rows if int(row.get("behind", 0) or 0) > 0]
        return {
            "eligible": True,
            "sync_required": bool(behind),
            "repositories": rows,
            "selected_repositories": list(selected),
            "behind_repositories": len(behind),
            "behind_commits": sum(int(row.get("behind", 0) or 0) for row in behind),
            "remote": str(remote).strip() or "origin",
        }

    def create_final_repository_sync(
        self,
        *,
        repositories: list[str],
        source_branches: Mapping[str, str],
        remote: str = "origin",
        conflict_policy: str = "ai_resolve",
        sync_package_id: str = "",
    ) -> dict[str, Any]:
        self._require_completed_orchestration_for_final_sync()
        preview = self.final_repository_sync_preview(
            repositories=repositories,
            source_branches=source_branches,
            remote=remote,
        )
        if not preview["sync_required"]:
            return {
                "applied": False,
                "sync_required": False,
                "preview": preview,
                "lifecycle": self.snapshot(),
            }
        try:
            current = self.definitions.live_documents(
                self.task_dir, require_complete=True
            )
            insertion = build_final_sync_definition(
                brief_markdown=current["BRIEF.md"],
                plan_markdown=current["PLAN.md"],
                plan_graph_yaml=current["PLAN.graph.yaml"],
                repositories=preview["selected_repositories"],
                source_branches=source_branches,
                remote=remote,
                conflict_policy=conflict_policy,
                sync_package_id=sync_package_id,
            )
            candidate = self._replan_service().create_candidate(
                ReplanInputs(
                    requested_change=(
                        f"Append {insertion.sync_package_id} as the operator-approved "
                        "final repository synchronization check before archival and cleanup."
                    ),
                    definition=insertion.definition,
                    allow_structural_consistency=True,
                ),
                provider=None,
                workdir=self._replan_workdir(),
            )
            result = self._replan_service().apply_candidate(candidate.candidate_id)
            return {
                "applied": True,
                "sync_required": True,
                "package_id": insertion.sync_package_id,
                "preview": preview,
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

    def _require_completed_orchestration_for_final_sync(self) -> TaskExecutionStateRecord:
        state = self._state_record()
        if state is None:
            raise GuiError("orchestration state is not initialized")
        if state.state != TaskExecutionState.COMPLETED:
            raise GuiError(
                "final synchronization is available only after orchestration completes"
            )
        if state.completed_packages != state.total_packages:
            raise GuiError(
                "final synchronization requires every work package to be completed"
            )
        return state

    def _final_sync_repositories(self, repositories: list[str]) -> tuple[str, ...]:
        manifest = self._manifest()
        selected = repositories or [
            item.id for item in manifest.repositories if item.mutability == "task_owned"
        ]
        try:
            return validate_sync_repository_selection(manifest, selected)
        except RepositorySyncPlanningError as exc:
            raise GuiError(str(exc)) from exc

    def _final_sync_summary(self) -> dict[str, Any]:
        try:
            state = self._require_completed_orchestration_for_final_sync()
            repositories = self._final_sync_repositories([])
        except GuiError as exc:
            return {"eligible": False, "reason": str(exc), "repositories": []}
        return {
            "eligible": bool(state.plan_graph.work_packages),
            "reason": "",
            "repositories": list(repositories),
            "requires_confirmation": True,
        }
