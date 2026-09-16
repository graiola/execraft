"""Ownership and commit recovery for post-shard aggregate review fixes.

Aggregate parents normally own no implementation commit: their child shards do.
A write-capable ``fix_review`` executed after those child commits is different.
This module records the exact retained fixer delta and permits one narrowly
bounded parent repair transaction while leaving unrelated workspace mutations to
the normal fail-closed finalization check.
"""

from __future__ import annotations

from typing import Any, Mapping

from .models import WorkPackage, WorkPackageStage, utc_now
from .package_finalization import qualify_dirty_paths, workspace_path_fingerprints
from .scheduler import AgentCapability


def record_aggregate_review_fix_delta(
    host: Any,
    package: WorkPackage,
    result: Mapping[str, Any],
) -> None:
    """Persist exact path/content ownership after an aggregate ``fix_review``."""

    if not (
        package.execution_mode == "aggregate"
        and package.stage == WorkPackageStage.FIX_REVIEW
    ):
        return
    dirty = host._workspace_dirty_paths(set(package.affected_repositories))
    qualified = list(qualify_dirty_paths(dirty))
    evidence = {
        "package_id": package.id,
        "invocation_id": str(result.get("_execraft_invocation_id", "")),
        "agent_id": str(result.get("_execraft_executed_by", package.last_fixer_id)),
        "qualified_paths": qualified,
        "path_fingerprints": workspace_path_fingerprints(
            host._repository_paths, dirty
        ),
        "captured_at": utc_now(),
    }
    host._journal.append("aggregate_review_fix_delta", evidence)
    host._emit_progress(
        "aggregate_review_fix_delta",
        package_id=package.id,
        path_count=len(qualified),
        invocation_id=evidence["invocation_id"],
    )


def _latest_or_recovered_delta(
    host: Any,
    package: WorkPackage,
    dirty_paths: Mapping[str, list[str]],
) -> Mapping[str, Any] | None:
    """Return durable fixer ownership, migrating a pre-feature run if exact."""

    for event in reversed(host._journal.read()):
        if (
            event.event_type == "aggregate_review_fix_delta"
            and str(event.payload.get("package_id", "")) == package.id
        ):
            return event.payload

    implementation = dict(package.last_implementation or {})
    if str(implementation.get("status", "")) != "fixed" or not dirty_paths:
        return None
    invocation_id = str(implementation.get("invocation_id", "")).strip()
    if not invocation_id:
        return None
    try:
        invocation = host._agent_invocations.get(invocation_id)
    except KeyError:
        return None
    if (
        invocation.package_id != package.id
        or invocation.capability != AgentCapability.FIX_REVIEW.value
        or invocation.status != "completed"
        or not invocation.workspace_after_digest
    ):
        return None

    host._context_assembler.begin_workspace_measurement(package)
    if (
        host._context_assembler.workspace_digest(package)
        != invocation.workspace_after_digest
    ):
        return None

    qualified = list(qualify_dirty_paths(dirty_paths))
    recovered = {
        "package_id": package.id,
        "invocation_id": invocation_id,
        "agent_id": invocation.agent_id,
        "qualified_paths": qualified,
        "path_fingerprints": workspace_path_fingerprints(
            host._repository_paths, dirty_paths
        ),
        "captured_at": utc_now(),
        "recovered_from_invocation_ledger": True,
    }
    host._journal.append("aggregate_review_fix_delta", recovered)
    host._journal.append(
        "aggregate_review_fix_delta_recovered",
        {
            "package_id": package.id,
            "invocation_id": invocation_id,
            "qualified_paths": qualified,
        },
    )
    host._emit_progress(
        "aggregate_review_fix_delta_recovered",
        package_id=package.id,
        invocation_id=invocation_id,
        path_count=len(qualified),
    )
    return recovered


def commit_aggregate_review_fix_delta(
    host: Any,
    package: WorkPackage,
    dirty_paths: Mapping[str, list[str]],
) -> str | None:
    """Commit only the exact post-shard fixer delta owned by an aggregate."""

    evidence = _latest_or_recovered_delta(host, package, dirty_paths)
    if not evidence:
        return None
    expected_paths = {str(item) for item in evidence.get("qualified_paths", [])}
    current_paths = set(qualify_dirty_paths(dirty_paths))
    if not current_paths or not current_paths.issubset(expected_paths):
        return None
    expected_fingerprints = {
        str(key): str(value)
        for key, value in dict(evidence.get("path_fingerprints") or {}).items()
    }
    current_fingerprints = workspace_path_fingerprints(
        host._repository_paths, dirty_paths
    )
    if any(
        expected_fingerprints.get(path) != fingerprint
        for path, fingerprint in current_fingerprints.items()
    ):
        return None
    if not host._scope_recovery_coordinator.prepare_commit_scope(package):
        return None

    existing = host._commit_journal.last_by_work_package(package.id)
    if existing is not None and existing.status == "committed":
        transaction_id = existing.transaction_id
    elif existing is not None and existing.status == "pending" and host.config.strict_checks:
        transaction_id = host._recover_commit_transaction(package, existing)
    else:
        if existing is not None and existing.status == "pending":
            host._commit_journal.fail(
                existing.transaction_id,
                "interrupted aggregate repair transaction; starting a fresh attempt",
            )
        transaction = host._begin_commit_transaction(package)
        transaction_id = transaction.transaction_id if transaction else None
        if transaction_id:
            host._commit_journal.commit(transaction_id)

    payload = {
        "package_id": package.id,
        "transaction_id": transaction_id,
        "qualified_paths": sorted(current_paths),
        "source_invocation_id": str(evidence.get("invocation_id", "")),
        "source_agent_id": str(evidence.get("agent_id", "")),
    }
    host._journal.append("aggregate_review_fix_committed", payload)
    host._emit_progress("aggregate_review_fix_committed", **payload)
    return transaction_id


def finalize_aggregate_package(host: Any, package: WorkPackage) -> None:
    """Close an aggregate, committing only an owned post-shard repair delta."""

    policy = host.config.workspace_finalization_policy
    if not policy.enabled:
        host._mark_package_completed(package, transaction_id=None)
        return

    assessment = host._aggregate_finalization_assessment(package)
    if not assessment.ok:
        host._escalate_workspace_finalization_failure(
            package,
            mode="aggregate",
            message="; ".join(assessment.problems()),
            evidence=assessment.as_mapping(),
        )

    initial_dirty = host._workspace_dirty_paths()
    removed = host._scope_recovery_coordinator.cleanup_finalization_artifacts(
        package,
        list(qualify_dirty_paths(initial_dirty)),
        source="aggregate_barrier",
        affected_only=False,
    )
    remaining = host._workspace_dirty_paths()
    repair_transaction_id: str | None = None
    if remaining:
        repair_transaction_id = commit_aggregate_review_fix_delta(
            host, package, remaining
        )
        if repair_transaction_id:
            remaining = host._workspace_dirty_paths()
    if remaining:
        host._escalate_workspace_finalization_failure(
            package,
            mode="aggregate",
            message=(
                "aggregate verification or review left unexplained workspace "
                "changes after child shard commits"
            ),
            dirty_paths=remaining,
            evidence=assessment.as_mapping(),
        )

    payload = {
        "package_id": package.id,
        "mode": "aggregate",
        "transaction_id": repair_transaction_id,
        "repositories": sorted(host._repository_paths),
        "removed_artifacts": removed,
        "child_evidence": [item.as_mapping() for item in assessment.children],
    }
    host._journal.append("aggregate_workspace_finalized", payload)
    host._emit_progress("aggregate_workspace_finalized", **payload)
    host._mark_package_completed(package, transaction_id=repair_transaction_id)
