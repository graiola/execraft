"""Shared predicates for durable operator-action records."""

from __future__ import annotations

from typing import Any, Mapping


def is_auto_resumable_agent_wait(action: Mapping[str, Any] | None) -> bool:
    """Return whether a HUMAN_REQUIRED action is a legacy provider wait.

    Older Execraft versions escalated exhausted provider candidates to
    ``human_required`` even though no product decision was needed.  Those
    records are safe to resume after provider availability changes; all other
    human-required states must remain blocked until the explicit operator
    decision is completed.
    """

    if not action:
        return False
    reason = str(action.get("reason", "")).lower()
    recommended = str(action.get("recommended_decision", "")).lower()
    return (
        "could not be completed by any available agent" in reason
        or "no configured available agent" in reason
        or "resolve provider availability" in recommended
    )


def is_repository_scope_action(action: Mapping[str, Any] | None) -> bool:
    """Return whether an operator action represents a repository-scope check."""

    if not action:
        return False
    stage = str(action.get("stage", "")).strip().lower()
    reason = str(action.get("reason", "")).lower()
    recommended = str(action.get("recommended_decision", "")).lower()
    evidence = " ".join(str(item) for item in action.get("evidence", [])).lower()
    return (
        stage == "scope"
        or "write_scope" in reason
        or "write_scope" in evidence
        or "repository scope" in reason
        or "repository scope" in evidence
        or "clean-start check" in reason
        or "clean-start check" in evidence
        or "restore a clean workspace" in recommended
        or "amend the declared repository scope" in recommended
    )


def is_completed_repository_scope_action(
    action: Mapping[str, Any] | None,
    packages: Mapping[str, Any],
) -> bool:
    """Return whether a scope action points at an already-completed package.

    This is a UI/CLI preflight hint only.  The orchestrator still verifies the
    current workspace and declared scope under its exclusive lock before it
    clears the durable HUMAN_REQUIRED state.
    """

    if not is_repository_scope_action(action):
        return False
    package_id = str((action or {}).get("package_id", "")).strip()
    package = packages.get(package_id)
    stage = getattr(package, "stage", None)
    value = getattr(stage, "value", stage)
    return str(value) == "completed"
