"""Package-scoped causal context assembly for agent stages."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any, Mapping

from .context_budget import (
    ContextBlock,
    ContextBudgetError,
    ContextPlanner,
    DEFAULT_TOKEN_BUDGETS,
    TokenBudget,
    estimate_tokens,
)
from .context_capsule import PackageContextCapsuleStore
from .invocations import AgentInvocationStore
from .journal import EventJournal
from .models import WorkPackage
from .verification_outcome import verification_uses_accepted_baseline
from .scheduler import AgentCapability, StructuredHandoff, build_agent_prompt


_MAX_SUMMARY_BYTES = 24 * 1024
_MAX_EXCERPT_BYTES = 16 * 1024
_MAX_PRIOR_INVOCATIONS = 6


@dataclass(frozen=True)
class GitSnapshot:
    """Typed Git repository state snapshot at a mutation boundary."""

    repository: str
    head: str
    status_porcelain: str
    status_short: str
    stat: str
    error: str = ""

    def as_mapping(self) -> dict[str, str]:
        return {
            "repository": self.repository,
            "head": self.head,
            "status": self.status_porcelain,
            "stat": self.stat,
            "error": self.error,
        }


def _truncate(value: object, maximum_bytes: int) -> str:
    text = str(value)
    encoded = text.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return text
    suffix = "\n...[bounded by Execraft]"
    budget = max(0, maximum_bytes - len(suffix.encode("utf-8")))
    return encoded[:budget].decode("utf-8", errors="ignore") + suffix


def _json(value: object, maximum_bytes: int = _MAX_EXCERPT_BYTES) -> str:
    """Serialize evidence for a provider prompt.

    Indentation costs roughly a fifth of the bytes of a nested evidence block
    and buys the model nothing, so blocks are emitted with compact separators.
    ``sort_keys`` stays on: a stable key order is what lets two renderings of
    the same evidence hash and cache identically.
    """

    return _truncate(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        maximum_bytes,
    )


class AgentContextAssembler:
    """Build a deterministic package capsule and budgeted stage context."""

    def __init__(
        self,
        *,
        project_id: str,
        task_id: str = "",
        repository_paths: Mapping[str, Path],
        journal: EventJournal,
        invocations: AgentInvocationStore,
        dossier_dir: Path | None = None,
        context_dir: Path | None = None,
        token_budgets: Mapping[str, TokenBudget] | None = None,
    ) -> None:
        self.project_id = project_id
        self.task_id = str(task_id).strip() or project_id
        self._repository_paths = {
            str(key): Path(value).resolve() for key, value in repository_paths.items()
        }
        self._journal = journal
        self._invocations = invocations
        capsule_dir = Path(context_dir or invocations.path.parent / "context-capsules")
        self._capsules = PackageContextCapsuleStore(
            dossier_dir=dossier_dir,
            output_dir=capsule_dir,
            invocations=invocations,
            project_id=project_id,
        )
        self._token_budgets = {
            **DEFAULT_TOKEN_BUDGETS,
            **dict(token_budgets or {}),
        }
        self._git_snapshots: dict[str, GitSnapshot] = {}

    @property
    def capsules(self) -> PackageContextCapsuleStore:
        return self._capsules

    def enrich(
        self,
        handoff: StructuredHandoff,
        package: WorkPackage,
        capability: AgentCapability,
    ) -> StructuredHandoff:
        """Return a package-scoped, deduplicated, budgeted handoff snapshot."""

        capsule, capsule_path = self._capsules.generate(package)
        verification = dict(getattr(package, "last_verification", {}) or {})
        implementation = dict(getattr(package, "last_implementation", {}) or {})
        review = dict(getattr(package, "last_review", {}) or {})
        prior = self._prior_invocations(package.id)
        verification_summary = handoff.verification_summary or self._verification_summary(
            verification
        )

        decisions = list(handoff.relevant_decisions)
        for item in capsule.decisions:
            summary = item.get("summary") or item.get("decision") or item.get("title")
            if summary:
                decisions.append(str(summary))
        if capsule.legacy_fallback:
            decisions.append(
                "Legacy graph fallback is active for this package. The bounded plan "
                "section in the package context capsule is authoritative; do not load "
                "the complete PLAN.md or HANDOFF.md."
            )
        if verification.get("status") == "failed":
            decisions.append(
                "This stage follows failed verification. Address the supplied command "
                "evidence rather than repeating implementation blindly."
            )
        if prior.get("latest_status") == "failed":
            decisions.append(
                "A previous provider attempt failed. Continue from the existing "
                "workspace and use the compact attempt summary."
            )

        context = dict(handoff.execution_context)
        context.update(
            {
                "project_id": self.project_id,
                "task_id": self.task_id,
                "package_id": package.id,
                "stage": package.stage.value,
                "capability": capability.value,
                "workspace_digest": self.workspace_digest(package),
                "package_context": {
                    "path": str(capsule_path),
                    "sha256": capsule.capsule_sha256,
                    "schema_version": capsule.schema_version,
                    "legacy_fallback": capsule.legacy_fallback,
                },
                # These compact projections are part of the public handoff
                # contract: they are always present, whereas the matching
                # evidence blocks are budget-planned and may be bounded. They
                # stay small deliberately — do not grow them into a second copy
                # of the evidence.
                "implementation": self._compact_mapping(implementation),
                "verification": self._compact_verification(verification),
                "review": self._compact_mapping(review),
                # Per-attempt history is the exception: it is large, unbounded in
                # practice, and carried once by the budget-planned
                # ``prior-attempt-summary.json`` block. Only the aggregate stays
                # here, where the planner cannot trim it.
                "prior_invocation_summary": {
                    key: value
                    for key, value in prior.items()
                    if key not in {"attempts", "fingerprint"}
                },
            }
        )
        skill_manifest = [
            {
                key: item[key]
                for key in (
                    "id",
                    "version",
                    "content_hash",
                    "source",
                    "selection_reason",
                    "instruction_bytes",
                )
                if key in item
            }
            for item in handoff.workflow_skills
        ]
        trigger = handoff.triggering_event_id
        if not trigger:
            sequence = self._journal.last_sequence()
            trigger = f"journal:{sequence}" if sequence else ""

        base = replace(
            handoff,
            repository_diff_summary="",
            verification_summary=verification_summary,
            relevant_decisions=list(dict.fromkeys(decisions)),
            bounded_excerpts={},
            execution_context=context,
            skill_manifest=skill_manifest,
            triggering_event_id=trigger,
        )
        budget = self._token_budgets.get(
            capability.value,
            DEFAULT_TOKEN_BUDGETS.get(capability.value, DEFAULT_TOKEN_BUDGETS["implement"]),
        )
        base = replace(
            base,
            input_token_budget=budget.input_hard_limit,
            output_token_budget=budget.output_target,
            output_token_target=budget.output_target,
            output_token_hard_limit=budget.output_hard_limit,
        )
        reserved_tokens = estimate_tokens(build_agent_prompt(base))
        blocks = self._candidate_blocks(
            handoff=handoff,
            package=package,
            capability=capability,
            capsule=capsule.as_mapping(),
            implementation=implementation,
            verification=verification,
            review=review,
            prior=prior,
        )
        plan = ContextPlanner(budget).plan(blocks, reserved_tokens=reserved_tokens)

        excerpts: dict[str, str] = {}
        repository_summary = ""
        for block in plan.included:
            if block.type == "workspace":
                repository_summary = block.content
            else:
                excerpts[block.id] = block.content
        final = replace(
            base,
            repository_diff_summary=repository_summary,
            bounded_excerpts=excerpts,
            context_manifest=[item.as_mapping() for item in plan.included],
            budget_report=plan.as_mapping(),
            context_profile=f"{capability.value}:package-scoped",
        )
        prompt_tokens = estimate_tokens(build_agent_prompt(final))
        if prompt_tokens > budget.input_hard_limit:
            contributors = sorted(
                final.context_manifest,
                key=lambda item: int(item.get("estimated_tokens", 0)),
                reverse=True,
            )[:5]
            raise ContextBudgetError(
                "rendered prompt exceeds input hard limit: "
                f"estimated={prompt_tokens} hard_limit={budget.input_hard_limit}; "
                f"largest_blocks={[item.get('id') for item in contributors]}"
            )
        report = dict(final.budget_report)
        report.update(
            {
                "rendered_prompt_estimated_tokens": prompt_tokens,
                "output_target_tokens": budget.output_target,
                "output_hard_limit_tokens": budget.output_hard_limit,
            }
        )
        return replace(final, budget_report=report)

    def _candidate_blocks(
        self,
        *,
        handoff: StructuredHandoff,
        package: WorkPackage,
        capability: AgentCapability,
        capsule: Mapping[str, Any],
        implementation: Mapping[str, Any],
        verification: Mapping[str, Any],
        review: Mapping[str, Any],
        prior: Mapping[str, Any],
    ) -> list[ContextBlock]:
        # Requirements and acceptance criteria already have dedicated handoff
        # fields. The capsule projection carries only information that would
        # otherwise require reopening the dossier.
        capsule_projection = {
            key: capsule[key]
            for key in (
                "schema_version",
                "package_id",
                "title",
                "objective",
                "affected_repositories",
                "dependencies",
                "read_scope",
                "write_scope",
                "conflict_keys",
                "risk",
                "verification_profile",
                "decisions",
                "references",
                "legacy_fallback",
                "capsule_sha256",
            )
            if key in capsule
        }
        if capsule.get("legacy_fallback") and capsule.get("plan_section"):
            capsule_projection["plan_section"] = capsule["plan_section"]
        blocks = [
            ContextBlock(
                id="package-context-capsule.json",
                type="capsule",
                source="generated-package-context",
                content=_json(capsule_projection, 64 * 1024),
                scope=package.id,
                priority=100,
                required=True,
                deduplication_key=f"capsule:{capsule.get('capsule_sha256', package.id)}",
            ),
            ContextBlock(
                id="workspace-summary.txt",
                type="workspace",
                source="git",
                content=handoff.repository_diff_summary or self.workspace_summary(package),
                scope=package.id,
                priority=75,
                required=capability in {AgentCapability.IMPLEMENT, AgentCapability.FIX_REVIEW},
                deduplication_key=f"workspace:{self.workspace_digest(package)}",
                truncation_policy="tail",
                minimum_tokens=256,
            ),
        ]
        if verification and verification.get("status") == "failed":
            blocks.append(
                ContextBlock(
                    id="verification-failure.json",
                    type="evidence",
                    source="package.last_verification",
                    content=_json(verification),
                    scope=package.id,
                    priority=95,
                    required=True,
                    deduplication_key=f"verification:{verification.get('invocation_id') or verification.get('attempt')}",
                    truncation_policy="tail",
                    minimum_tokens=256,
                )
            )
        if implementation and capability in {
            AgentCapability.REVIEW,
            AgentCapability.FIX_REVIEW,
            AgentCapability.SUPERVISE,
        }:
            blocks.append(
                ContextBlock(
                    id="implementation-result.json",
                    type="evidence",
                    source="package.last_implementation",
                    content=_json(implementation),
                    scope=package.id,
                    priority=90,
                    required=capability == AgentCapability.REVIEW,
                    deduplication_key=f"implementation:{implementation.get('invocation_id') or package.id}",
                    truncation_policy="head",
                    minimum_tokens=192,
                )
            )
        if review and capability in {
            AgentCapability.FIX_REVIEW,
            AgentCapability.REVIEW,
            AgentCapability.SUPERVISE,
        }:
            blocks.append(
                ContextBlock(
                    id="review-result.json",
                    type="evidence",
                    source="package.last_review",
                    content=_json(review),
                    scope=package.id,
                    priority=92,
                    required=capability == AgentCapability.FIX_REVIEW,
                    deduplication_key=f"review:{review.get('invocation_id') or package.id}",
                    truncation_policy="head",
                    minimum_tokens=192,
                )
            )
        if prior.get("attempts"):
            blocks.append(
                ContextBlock(
                    id="prior-attempt-summary.json",
                    type="history",
                    source="agent-invocation-ledger",
                    # Only the per-attempt detail. The aggregate counts and the
                    # fingerprint already ride in the execution context and the
                    # deduplication key respectively.
                    content=_json(prior.get("attempts", [])),
                    scope=package.id,
                    priority=70,
                    required=False,
                    deduplication_key=f"attempts:{prior.get('fingerprint', '')}",
                    truncation_policy="head",
                    minimum_tokens=128,
                )
            )
        reserved_names = {
            "package-context-capsule.json",
            "workspace-summary.txt",
            "verification-failure.json",
            "implementation-result.json",
            "review-result.json",
            "prior-attempt-summary.json",
        }
        for name, value in sorted(handoff.bounded_excerpts.items()):
            if name in reserved_names:
                continue
            blocks.append(
                ContextBlock(
                    id=name,
                    type="excerpt",
                    source="caller",
                    content=_truncate(value, _MAX_EXCERPT_BYTES),
                    scope=package.id,
                    priority=80,
                    required=False,
                    deduplication_key=f"caller:{name}:{hashlib.sha256(str(value).encode('utf-8')).hexdigest()}",
                    truncation_policy="head",
                    minimum_tokens=128,
                )
            )
        return blocks

    def begin_workspace_measurement(self, package: WorkPackage | None = None) -> None:
        """Start a new mutation boundary and recollect typed Git snapshots.

        Discards snapshots gathered for the previous boundary so the next read
        measures the workspace afresh. Reads within one boundary share the
        snapshot set collected here. Invoke before each pre- and
        post-invocation measurement so provider mutations are never hidden by
        a stale boundary, and once per enclosing package scope so successive
        packages with different repositories are all captured.
        """
        self._git_snapshots = {}
        self._collect_git_snapshots(package)

    def _collect_git_snapshots(self, package: WorkPackage | None = None) -> None:
        """Collect typed Git snapshots for the current mutation boundary.

        Repositories already captured in this boundary are reused so repeated
        reads share one measurement; repositories not yet captured are probed
        on demand so successive package scopes are never silently dropped.
        """
        repository_ids = (
            set(package.affected_repositories)
            if package is not None and package.affected_repositories
            else set(self._repository_paths)
        )
        for repository_id in sorted(repository_ids):
            if repository_id in self._git_snapshots:
                continue
            path = self._repository_paths.get(repository_id)
            if path is None or not (path / ".git").exists():
                continue
            head_result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=path,
                text=True,
                capture_output=True,
                check=False,
            )
            status_result = subprocess.run(
                ["git", "status", "--porcelain=v1", "--untracked-files=all"],
                cwd=path,
                text=True,
                capture_output=True,
                check=False,
            )
            stat_result = subprocess.run(
                ["git", "diff", "--stat", "HEAD", "--", "."],
                cwd=path,
                text=True,
                capture_output=True,
                check=False,
            )
            error_parts = []
            if head_result.returncode != 0:
                error_parts.append(f"rev-parse: {head_result.stderr.strip()}")
            if status_result.returncode != 0:
                error_parts.append(f"status: {status_result.stderr.strip()}")
            if stat_result.returncode != 0:
                error_parts.append(f"diff: {stat_result.stderr.strip()}")
            status_text = status_result.stdout.strip() if status_result.returncode == 0 else ""
            self._git_snapshots[repository_id] = GitSnapshot(
                repository=repository_id,
                head=head_result.stdout.strip() if head_result.returncode == 0 else "",
                status_porcelain=status_text,
                status_short=status_text,
                stat=stat_result.stdout.strip() if stat_result.returncode == 0 else "",
                error="; ".join(error_parts),
            )

    def workspace_summary(self, package: WorkPackage | None = None) -> str:
        self._collect_git_snapshots(package)
        sections: list[str] = []
        used = 0
        omitted = 0
        repository_ids = (
            set(package.affected_repositories)
            if package is not None and package.affected_repositories
            else set(self._repository_paths)
        )
        for repository_id in sorted(repository_ids):
            snapshot = self._git_snapshots.get(repository_id)
            if snapshot is None:
                continue
            if snapshot.error:
                section = f"[{repository_id}]\nGit error: {snapshot.error}"
            elif not snapshot.status_short and not snapshot.stat:
                continue
            else:
                section = _truncate(
                    f"[{repository_id}]\n" + "\n".join(item for item in (snapshot.status_short, snapshot.stat) if item),
                    _MAX_EXCERPT_BYTES,
                )
            encoded = section.encode("utf-8")
            remaining = _MAX_SUMMARY_BYTES - used
            if remaining <= 0:
                omitted += 1
                continue
            if len(encoded) > remaining:
                sections.append(_truncate(section, remaining))
                used = _MAX_SUMMARY_BYTES
                omitted += 1
                continue
            sections.append(section)
            used += len(encoded)
        if omitted:
            sections.append(f"[{omitted} additional repository section(s) omitted]")
        return "\n\n".join(sections) or "All affected repositories are clean."

    def workspace_digest(self, package: WorkPackage | None = None) -> str:
        self._collect_git_snapshots(package)
        rows: list[dict[str, str]] = []
        repository_ids = (
            set(package.affected_repositories)
            if package is not None and package.affected_repositories
            else set(self._repository_paths)
        )
        for repository_id in sorted(repository_ids):
            snapshot = self._git_snapshots.get(repository_id)
            if snapshot is None:
                continue
            rows.append(snapshot.as_mapping())
        return hashlib.sha256(
            json.dumps(rows, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def _prior_invocations(self, package_id: str) -> dict[str, Any]:
        records = self._invocations.list_for_package(
            self.project_id,
            package_id,
            limit=_MAX_PRIOR_INVOCATIONS,
            newest_first=True,
        )
        attempts: list[dict[str, Any]] = []
        failure_classes: dict[str, int] = {}
        for record in records[:3]:
            failure = self._compact_mapping(record.failure)
            classification = str(failure.get("classification", "")).strip()
            if classification:
                failure_classes[classification] = failure_classes.get(classification, 0) + 1
            attempts.append(
                {
                    "invocation_id": record.invocation_id,
                    "stage": record.stage,
                    "capability": record.capability,
                    "attempt": record.attempt,
                    "agent_id": record.agent_id,
                    "model": record.model,
                    "status": record.status,
                    "failure": failure,
                }
            )
        for record in records[3:]:
            classification = str(record.failure.get("classification", "")).strip()
            if classification:
                failure_classes[classification] = failure_classes.get(classification, 0) + 1
        payload = {
            "latest_status": records[0].status if records else "",
            "attempts": attempts,
            "older_attempt_count": max(0, len(records) - len(attempts)),
            "failure_classes": failure_classes,
        }
        payload["fingerprint"] = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return payload

    @staticmethod
    def _verification_summary(verification: Mapping[str, Any]) -> str:
        if not verification:
            return ""
        commands = verification.get("commands") or []
        fragments = [
            f"status={verification.get('status', 'unknown')}",
            f"attempt={verification.get('attempt', 0)}",
        ]
        if isinstance(commands, list):
            failed = [
                str(item.get("command", ""))
                for item in commands
                if isinstance(item, dict) and item.get("status") != "passed"
            ]
            if failed:
                label = (
                    "baseline_accepted_raw_failures"
                    if verification_uses_accepted_baseline(verification)
                    else "failed"
                )
                fragments.append(label + "=" + ", ".join(failed[:3]))
        return "; ".join(fragments)

    @staticmethod
    def _compact_verification(value: Mapping[str, Any]) -> dict[str, Any]:
        if not value:
            return {}
        compact = AgentContextAssembler._compact_mapping(value)
        for key in ("attempt", "returncode"):
            if key in value:
                compact[key] = value[key]
        commands = value.get("commands")
        if isinstance(commands, list):
            compact["commands"] = [
                {
                    key: item[key]
                    for key in ("command", "status", "returncode")
                    if key in item
                }
                for item in commands[:3]
                if isinstance(item, Mapping)
            ]
        return compact

    @staticmethod
    def _compact_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
        if not value:
            return {}
        keys = (
            "status",
            "verdict",
            "summary",
            "classification",
            "error",
            "retry_after_seconds",
            "persistent",
            "invocation_id",
            "path",
            "sha256",
        )
        return {
            key: value[key]
            for key in keys
            if key in value and value[key] not in (None, "", [], {})
        }
