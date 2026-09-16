"""Explicit operator approval for repository write-scope expansion.

The automatic recovery path deliberately cannot authorize protected paths such
as CI workflows.  This module owns the explicit operator mutation, including an
optimistic candidate-set guard used by interactive clients.
"""

from __future__ import annotations

from typing import Any

from .models import OrchestrateError, TaskExecutionState, WorkPackageStage


def approve_declared_write_scope(
    coordinator: Any,
    package_id: str,
    *,
    expected_candidates: list[str] | None = None,
) -> dict[str, Any]:
    """Authorize the exact current candidates, or reconcile a resolved check.

    ``expected_candidates`` represents the set the operator actually previewed.
    A mismatch rejects approval so a stale GUI/CLI confirmation cannot acquire
    a path that appeared after the preview.
    """

    host = coordinator._host
    with host._exclusive_run_lock():
        package, context = coordinator._active_scope_context(package_id)
        context_text = coordinator._scope_check_context_text(context)
        if "workspace must be clean before a new package starts" in context_text:
            raise OrchestrateError(
                "clean-start contamination cannot be adopted as package scope; "
                "restore or commit the pre-existing work first"
            )

        candidates = coordinator._scope_recovery_candidates_for_context(package, context)
        _assert_preview_matches(candidates, expected_candidates)
        if not candidates:
            return coordinator.reconcile_resolved_scope_check_unlocked(
                package_id,
                automatic=False,
            )
        if package.stage == WorkPackageStage.COMPLETED:
            raise OrchestrateError(
                f"completed package {package_id!r} cannot acquire new workspace paths"
            )

        previous_stage = package.stage
        additions, added_repositories = coordinator.append_recovered_scope_paths(
            package,
            candidates,
        )
        remaining = coordinator._scope_recovery_candidates_for_context(package, context)
        if remaining:
            raise OrchestrateError(
                "workspace ownership expansion did not cover all current candidates: "
                + ", ".join(remaining)
            )

        next_stage = _next_stage_after_approval(package, previous_stage)
        payload = {
            "package_id": package.id,
            "previous_stage": previous_stage.value,
            "next_stage": next_stage.value,
            "added_paths": additions,
            "added_repositories": added_repositories,
            "candidates": candidates,
        }
        host._journal.append("write_scope_expansion_approved", payload)
        host._emit_progress("write_scope_expanded", **payload)
        host._state_record.error_message = ""
        package.status = "pending"
        if next_stage != previous_stage:
            host._advance_package_stage(package, next_stage)
        else:
            host.save_state()
        host.transition_to(TaskExecutionState.RUNNING)
        return coordinator.declared_write_scope_report(package.id) | {
            "reconciled": False,
            **payload,
        }


def _assert_preview_matches(
    candidates: list[str],
    expected_candidates: list[str] | None,
) -> None:
    if expected_candidates is None:
        return
    expected = _normalized_candidates(expected_candidates)
    current = _normalized_candidates(candidates)
    if current == expected:
        return

    added = sorted(set(current) - set(expected))
    removed = sorted(set(expected) - set(current))
    details: list[str] = []
    if added:
        details.append("new candidates: " + ", ".join(added))
    if removed:
        details.append("no longer candidates: " + ", ".join(removed))
    raise OrchestrateError(
        "workspace scope changed after operator preview; refresh the approval "
        "before continuing"
        + (" (" + "; ".join(details) + ")" if details else "")
    )


def _normalized_candidates(candidates: list[str]) -> list[str]:
    return sorted(
        dict.fromkeys(str(item).strip() for item in candidates if str(item).strip())
    )


def _next_stage_after_approval(package: Any, previous_stage: WorkPackageStage) -> WorkPackageStage:
    if previous_stage == WorkPackageStage.IMPLEMENT:
        return WorkPackageStage.FAST_VERIFY
    if previous_stage in {
        WorkPackageStage.FIX_REVIEW,
        WorkPackageStage.REVIEW,
        WorkPackageStage.FINAL_REVIEW,
        WorkPackageStage.READY_TO_COMMIT,
    }:
        package.review_findings = []
        package.final_reviewer_id = ""
        return WorkPackageStage.REGRESSION_VERIFY
    return previous_stage
