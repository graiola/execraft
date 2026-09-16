"""Repository-sync orchestration helpers.

Keep repository-sync-specific verification scope and agent handoff semantics out
of the main orchestrator state machine.  These helpers are deliberately pure:
the caller retains ownership of persisted state, Git transactions, journaling,
and stage transitions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .models import WorkPackage
from .scheduler import StructuredHandoff
from .structured_output import acceptance_evidence_output_schema
from .verification import (
    VerificationCommand,
    VerificationProfile,
    VerificationRegistry,
    VerificationResult,
)


@dataclass(frozen=True)
class RepositorySyncVerificationScope:
    """Commands applicable to the repositories changed by one sync transaction."""

    commands: tuple[VerificationCommand, ...]
    changed_repositories: tuple[str, ...]
    skipped_noop_repositories: tuple[str, ...]


def repository_sync_verification_scope(
    registry: VerificationRegistry,
    profile: VerificationProfile,
    affected_repositories: Sequence[str],
    transaction: Any,
) -> RepositorySyncVerificationScope:
    """Select global checks plus checks for repositories actually changed by sync.

    A repository-sync ``noop`` item is proven by the transaction service to be
    at the same target commit it had before preparation.  Re-running a
    repository-specific check for such an item cannot detect a regression caused
    by the sync package, and can deadlock an otherwise valid transaction on an
    unrelated baseline failure.  Global checks still run because they can cover
    cross-repository compatibility.
    """

    status_by_repository = {
        str(item.repository_id): str(item.status)
        for item in getattr(transaction, "repositories", ())
    }
    affected = tuple(dict.fromkeys(str(repo) for repo in affected_repositories if repo))
    changed = tuple(repo for repo in affected if status_by_repository.get(repo) != "noop")
    skipped = tuple(repo for repo in affected if status_by_repository.get(repo) == "noop")

    commands: list[VerificationCommand] = []
    seen: set[tuple[str, str]] = set()
    for command in registry.commands_for_profile(profile):
        if command.repository_id and command.repository_id not in changed:
            continue
        key = (command.command, command.repository_id)
        if key in seen:
            continue
        seen.add(key)
        commands.append(command)
    return RepositorySyncVerificationScope(tuple(commands), changed, skipped)


def verification_failure_findings(results: Iterable[VerificationResult]) -> list[str]:
    """Convert failed commands into bounded, actionable fixer findings."""

    findings: list[str] = []
    for result in results:
        if result.status == "passed":
            continue
        excerpt = " ".join(str(result.relevant_excerpt or "").split())[:1200]
        suffix = f" — {excerpt}" if excerpt else ""
        findings.append(
            f"Verification command failed: {result.command} "
            f"(status={result.status or 'failed'}, returncode={result.returncode}){suffix}"
        )
    return findings


def build_repository_sync_handoff(
    package: WorkPackage,
    transaction: Any,
    *,
    working_directory: str,
    workflow_skills: list[dict[str, Any]],
) -> StructuredHandoff:
    """Build either conflict-resolution or verification-repair instructions.

    Verification retries must not be described as merge-conflict resolution:
    after the merge candidate exists, the remaining task is a bounded
    compatibility repair while the control plane continues to own Git state.
    """

    conflicts = [
        f"{item.repository_id}: {path}"
        for item in transaction.repositories
        for path in getattr(item, "conflict_paths", ())
    ]
    failed_commands = list((package.last_verification or {}).get("failed_commands") or [])
    repair = bool(failed_commands and not conflicts)
    if repair:
        findings = [_failed_command_finding(item) for item in failed_commands]
        stage = "repository_sync_verification_repair"
        summary = (
            "Repair only compatibility regressions exposed by verification of the "
            "repository-sync candidate. Git state, staging, and commits remain owned "
            "by the Execraft control plane."
        )
        decisions = [
            "Do not run git merge, merge --abort, rebase, reset, checkout, "
            "switch, cherry-pick, commit, or push.",
            "Do not edit Execraft state, transaction, dossier, or journal files.",
            "Treat the failed verification commands as the repair boundary; "
            "do not fix unrelated baseline failures.",
            "Edit only package-owned repository files needed to restore compatibility; "
            "Execraft will stage and validate the result.",
        ]
        context_profile = "repository_sync_verification_repair"
    else:
        findings = conflicts
        stage = "repository_sync_resolution"
        summary = (
            "Resolve the repository synchronization candidate prepared by Execraft. "
            "Git state, source SHAs, staging, and commits are owned by the control plane."
        )
        decisions = [
            "Do not run git merge, merge --abort, rebase, reset, checkout, "
            "switch, cherry-pick, commit, or push.",
            "Do not edit Execraft state, transaction, dossier, or journal files.",
            "Resolve both textual and semantic compatibility issues while preserving "
            "the task contract and pinned upstream behavior.",
            "Edit only package-owned repository files; Execraft will validate the "
            "index and stage the result.",
        ]
        context_profile = "repository_sync_conflict"

    return StructuredHandoff(
        work_package_id=package.id,
        stage=stage,
        summary=summary,
        unresolved_findings=findings,
        relevant_decisions=decisions,
        bounded_excerpts={
            "repository-sync-transaction.json": json.dumps(
                transaction.as_mapping(), indent=2, ensure_ascii=False
            )[:48_000],
            "previous-verification.json": json.dumps(
                package.last_verification or {}, indent=2, ensure_ascii=False
            )[:16_000],
        },
        requirements=list(package.requirements),
        acceptance_criteria=[
            {"id": item.id, "description": item.description}
            for item in package.acceptance_criteria
        ],
        expected_output_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["ok", "status", "summary", "acceptance_evidence"],
            "properties": {
                "ok": {"type": "boolean", "const": True},
                "status": {"type": "string", "enum": ["implemented", "fixed"]},
                "summary": {"type": "string", "minLength": 1},
                "acceptance_evidence": acceptance_evidence_output_schema(),
            },
        },
        working_directory=working_directory,
        read_only=False,
        workflow_skills=workflow_skills,
        context_profile=context_profile,
    )


def _failed_command_finding(item: Any) -> str:
    if not isinstance(item, dict):
        return f"Verification failure: {item}"
    command = str(item.get("command", "unknown command"))
    status = str(item.get("status", "failed"))
    excerpt = " ".join(str(item.get("relevant_excerpt", "")).split())[:1200]
    suffix = f" — {excerpt}" if excerpt else ""
    return f"Verification command failed: {command} (status={status}){suffix}"
