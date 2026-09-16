"""Typed work-package stage engine.

The engine owns lifecycle dispatch while the project orchestrator remains the
facade for persistence, agents, verification, recovery, and finalization.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Protocol

from .models import OrchestrateError, WorkPackage, WorkPackageKind, WorkPackageStage
from .recovery_playbook import indexed_findings
from .review_policy import final_review_fallback_tiers, primary_review_fallback_tiers
from .scheduler import AgentCapability, AgentSchedule, StructuredHandoff
from .structured_output import acceptance_evidence_output_schema, review_output_schema


class StageDisposition(Enum):
    """Tell the engine whether it may dispatch the package's next stage."""

    CONTINUE = "continue"
    STOP = "stop"


class PackageStageHost(Protocol):
    """Services required by stage handlers from the orchestration facade."""

    config: Any
    _journal: Any

    def save_state(self, *, reason: str = "state_update") -> Any: ...
    def _emit_progress(self, event_type: str, **payload: Any) -> None: ...
    def _process_repository_sync_pre_stages(self, package: WorkPackage) -> None: ...
    def _should_auto_decompose(self, package: WorkPackage) -> bool: ...
    def _enter_decomposition(self, package: WorkPackage, *, trigger: str = "automatic") -> None: ...
    def _run_decomposition(self, package: WorkPackage) -> None: ...
    def _enforce_repository_sync_divergence_check(self, package: WorkPackage) -> None: ...
    def _validate_clean_start(self, package: WorkPackage) -> None: ...
    def _advance_package_stage(self, package: WorkPackage, new_stage: WorkPackageStage) -> None: ...
    def _schedule_agents(self, package: WorkPackage) -> AgentSchedule: ...
    def _ensure_review_assignments(self, package: WorkPackage) -> AgentSchedule: ...
    def _call_agent(self, capability: AgentCapability, agent_id: str, package: WorkPackage, **kwargs: Any) -> dict[str, Any]: ...
    def _call_prebuilt_handoff(self, capability: AgentCapability, agent_id: str, package: WorkPackage, handoff: StructuredHandoff, **kwargs: Any) -> dict[str, Any]: ...
    def _package_working_directory(self, package: WorkPackage, *, write_capable: bool) -> Any: ...
    def _workflow_skills_for(self, package: WorkPackage, capability: AgentCapability) -> list[dict[str, Any]]: ...
    def _apply_implementation_result(self, package: WorkPackage, result: dict[str, Any]) -> None: ...
    def _run_verification(self, package: WorkPackage) -> bool: ...
    def _review_result(self, package: WorkPackage, result: dict[str, Any]) -> tuple[str, list[str]]: ...
    def _complete_review_shard(self, package: WorkPackage, *, verdict: str, findings: list[str], result: dict[str, Any]) -> None: ...
    def _queue_review_fixes(self, package: WorkPackage, findings: list[str]) -> None: ...
    def _select_fixer(self, package: WorkPackage) -> Any: ...
    def _agent_payload(self, result: dict[str, Any]) -> dict[str, Any]: ...
    def _prepare_repository_sync_acceptance_evidence(self, package: WorkPackage) -> None: ...
    def _validate_acceptance_evidence(self, package: WorkPackage) -> None: ...
    def _finalize_repository_sync_package(self, package: WorkPackage) -> None: ...
    def _finalize_aggregate_package(self, package: WorkPackage) -> None: ...
    def _finalize_standard_package(self, package: WorkPackage) -> None: ...


class PackageStageHandler(Protocol):
    stage: WorkPackageStage

    def handle(self, host: PackageStageHost, package: WorkPackage) -> StageDisposition: ...


@dataclass(frozen=True)
class DecomposeStageHandler:
    stage: WorkPackageStage = WorkPackageStage.DECOMPOSE

    def handle(self, host: PackageStageHost, package: WorkPackage) -> StageDisposition:
        host._run_decomposition(package)
        return StageDisposition.STOP if package.decomposition_status == "expanded" else StageDisposition.CONTINUE


@dataclass(frozen=True)
class PrepareStageHandler:
    stage: WorkPackageStage = WorkPackageStage.PREPARE

    def handle(self, host: PackageStageHost, package: WorkPackage) -> StageDisposition:
        host._enforce_repository_sync_divergence_check(package)
        host._validate_clean_start(package)
        host._advance_package_stage(package, WorkPackageStage.IMPLEMENT)
        host._journal.append("package_started", {"package_id": package.id, "title": package.title})
        host._emit_progress("package_started", package_id=package.id)
        return StageDisposition.CONTINUE


def _implementation_schema() -> dict[str, Any]:
    return {
        "type": "object", "additionalProperties": False,
        "required": ["ok", "status", "summary", "acceptance_evidence"],
        "properties": {
            "ok": {"type": "boolean", "const": True},
            "status": {"type": "string", "enum": ["implemented"]},
            "summary": {"type": "string", "minLength": 1},
            "acceptance_evidence": acceptance_evidence_output_schema(),
        },
    }


@dataclass(frozen=True)
class ImplementStageHandler:
    stage: WorkPackageStage = WorkPackageStage.IMPLEMENT

    def handle(self, host: PackageStageHost, package: WorkPackage) -> StageDisposition:
        # A partially persisted schedule is not sufficient to enter implementation.
        # Review assignments may have been selected while every implementation
        # provider was temporarily unavailable.  Re-run scheduling whenever the
        # implementer is missing so provider recovery can make forward progress.
        if not package.agent_id:
            schedule = host._schedule_agents(package)
            package.agent_id = schedule.implementer_id
            package.reviewer_id = schedule.reviewer_id
            package.final_reviewer_id = schedule.final_reviewer_id
            host.save_state()
            host._journal.append("agent_assigned", {
                "package_id": package.id, "implementer": schedule.implementer_id,
                "reviewer": schedule.reviewer_id, "final_reviewer": schedule.final_reviewer_id,
                "complexity": package.complexity_score(),
            })
            host._emit_progress(
                "agents_assigned", package_id=package.id,
                implementer=schedule.implementer_id, reviewer=schedule.reviewer_id,
                final_reviewer=schedule.final_reviewer_id,
                complexity=package.complexity_score(),
            )
        else:
            schedule = AgentSchedule(package.agent_id, package.reviewer_id, package.final_reviewer_id)
        result = host._call_agent(
            AgentCapability.IMPLEMENT, schedule.implementer_id, package,
            summary=f"Implement: {package.title}",
            working_directory=host._package_working_directory(package, write_capable=True),
            output_schema=_implementation_schema(),
        )
        host._apply_implementation_result(package, result)
        host._advance_package_stage(package, WorkPackageStage.FAST_VERIFY)
        return StageDisposition.CONTINUE


@dataclass(frozen=True)
class VerificationStageHandler:
    stage: WorkPackageStage
    next_stage: WorkPackageStage

    def handle(self, host: PackageStageHost, package: WorkPackage) -> StageDisposition:
        if not host._run_verification(package):
            return StageDisposition.STOP
        host._advance_package_stage(package, self.next_stage)
        return StageDisposition.CONTINUE


@dataclass(frozen=True)
class ReviewStageHandler:
    stage: WorkPackageStage = WorkPackageStage.REVIEW

    def handle(self, host: PackageStageHost, package: WorkPackage) -> StageDisposition:
        schedule = host._ensure_review_assignments(package)
        result = host._call_agent(
            AgentCapability.REVIEW, schedule.reviewer_id, package,
            summary=f"Review: {package.title}",
            excluded_agent_ids={schedule.implementer_id},
            fallback_exclusion_tiers=primary_review_fallback_tiers(
                host.config, package
            ),
            working_directory=host._package_working_directory(package, write_capable=False),
            read_only=True, output_schema=review_output_schema(),
        )
        verdict, findings = host._review_result(package, result)
        if package.execution_mode == "review_shard":
            host._complete_review_shard(package, verdict=verdict, findings=findings, result=result)
            return StageDisposition.STOP
        schedule = host._ensure_review_assignments(package)
        if verdict == "changes_required":
            host._queue_review_fixes(package, findings)
        else:
            package.review_findings = []
            next_stage = WorkPackageStage.FINAL_REVIEW if schedule.final_reviewer_id else WorkPackageStage.READY_TO_COMMIT
            host._advance_package_stage(package, next_stage)
            if not schedule.final_reviewer_id:
                host._journal.append("final_review_skipped", {"package_id": package.id, "reason": "no independent final reviewer configured"})
        return StageDisposition.CONTINUE


def _fix_schema() -> dict[str, Any]:
    return {
        "type": "object", "additionalProperties": False,
        "required": ["ok", "status", "summary", "resolved_findings", "acceptance_evidence"],
        "properties": {
            "ok": {"type": "boolean", "const": True},
            "status": {"type": "string", "enum": ["fixed"]},
            "summary": {"type": "string", "minLength": 1},
            "resolved_findings": {"type": "array", "items": {"type": "string", "minLength": 1}},
            "acceptance_evidence": acceptance_evidence_output_schema(),
        },
    }


@dataclass(frozen=True)
class FixReviewStageHandler:
    stage: WorkPackageStage = WorkPackageStage.FIX_REVIEW

    def handle(self, host: PackageStageHost, package: WorkPackage) -> StageDisposition:
        host._ensure_review_assignments(package)
        fixer = host._select_fixer(package)
        rescue_active = bool(package.review_recovery_cycles and package.review_recovery_fingerprint)
        if rescue_active:
            finding_map = indexed_findings(package.review_findings)
            expected_ids = list(finding_map)
            metadata = {
                "implementation_summary": package.implementation_summary,
                "acceptance_criteria": [
                    {"id": item.id, "description": item.description, "evidence": item.evidence, "verified": item.verified}
                    for item in package.acceptance_criteria
                ],
                "review_recovery_cycle": package.review_recovery_cycles,
            }
            handoff = StructuredHandoff(
                work_package_id=package.id, stage="review_recovery_fix",
                summary="Repair exactly the indexed final-review findings. Inspect the referenced files and current package metadata, make the smallest correct changes, and return durable metadata that describes the retained implementation without overclaiming.",
                unresolved_findings=[f"[{finding_id}] {finding}" for finding_id, finding in finding_map.items()],
                relevant_decisions=[
                    "This is a deterministic rescue campaign, not a new implementation pass.",
                    "Do not broaden product scope or rework unrelated code.",
                    "Every indexed finding must be resolved and reported by ID.",
                    "The implementation_summary field is durable package metadata; make it factually exact.",
                    "Acceptance evidence must cite artifacts or commands that really exist after your changes.",
                    "Do not commit, push, change branches, or edit Execraft state/journal files.",
                ],
                bounded_excerpts={
                    "review-findings.json": json.dumps(finding_map, indent=2, ensure_ascii=False),
                    "package-metadata.json": json.dumps(metadata, indent=2, ensure_ascii=False),
                },
                requirements=list(package.requirements),
                acceptance_criteria=[{"id": item.id, "description": item.description} for item in package.acceptance_criteria],
                expected_output_schema={
                    "type": "object", "additionalProperties": False,
                    "required": ["ok", "status", "summary", "implementation_summary", "resolved_finding_ids", "acceptance_evidence"],
                    "properties": {
                        "ok": {"type": "boolean", "const": True},
                        "status": {"type": "string", "enum": ["fixed"]},
                        "summary": {"type": "string", "minLength": 1},
                        "implementation_summary": {"type": "string", "minLength": 1},
                        "resolved_finding_ids": {"type": "array", "items": {"enum": expected_ids}, "minItems": len(expected_ids), "maxItems": len(expected_ids), "uniqueItems": True},
                        "acceptance_evidence": acceptance_evidence_output_schema(),
                    },
                },
                working_directory=host._package_working_directory(package, write_capable=True),
                read_only=False,
                workflow_skills=host._workflow_skills_for(package, AgentCapability.FIX_REVIEW),
            )
            result = host._call_prebuilt_handoff(
                AgentCapability.FIX_REVIEW, fixer.agent_id, package, handoff,
                excluded_agent_ids=set(fixer.excluded_agent_ids),
                fallback_exclusion_tiers=[(policy, set(excluded)) for policy, excluded in fixer.fallback_tiers],
            )
            payload = host._agent_payload(result)
            resolved_ids = payload.get("resolved_finding_ids") or []
            if not isinstance(resolved_ids, list) or set(resolved_ids) != set(expected_ids):
                raise OrchestrateError("validated review-recovery finding-ID invariant escaped the invocation boundary")
            completed = {
                "package_id": package.id, "cycle": package.review_recovery_cycles,
                "finding_fingerprint": package.review_recovery_fingerprint,
                "resolved_finding_ids": resolved_ids,
                "agent_id": str(result.get("_execraft_executed_by", fixer.agent_id)),
            }
            host._journal.append("review_recovery_fixer_completed", completed)
            host._emit_progress("review_recovery_fixer_completed", **completed)
        else:
            result = host._call_agent(
                AgentCapability.FIX_REVIEW, fixer.agent_id, package,
                summary=f"Fix review findings: {package.title}",
                unresolved_findings=package.review_findings,
                working_directory=host._package_working_directory(package, write_capable=True),
                excluded_agent_ids=set(fixer.excluded_agent_ids),
                fallback_exclusion_tiers=[(policy, set(excluded)) for policy, excluded in fixer.fallback_tiers],
                output_schema=_fix_schema(),
            )
        host._apply_implementation_result(package, result)
        host._advance_package_stage(package, WorkPackageStage.REGRESSION_VERIFY)
        return StageDisposition.CONTINUE


@dataclass(frozen=True)
class FinalReviewStageHandler:
    stage: WorkPackageStage = WorkPackageStage.FINAL_REVIEW

    def handle(self, host: PackageStageHost, package: WorkPackage) -> StageDisposition:
        schedule = host._ensure_review_assignments(package)
        reviewer_id = schedule.final_reviewer_id or schedule.reviewer_id
        result = host._call_agent(
            AgentCapability.REVIEW, reviewer_id, package,
            summary=f"Final review: {package.title}", unresolved_findings=package.review_findings,
            working_directory=host._package_working_directory(package, write_capable=False),
            read_only=True,
            excluded_agent_ids={schedule.implementer_id, schedule.reviewer_id, package.last_fixer_id},
            # Independence is preferred. Ordinary packages may relax it in
            # bounded tiers so a single-provider project still executes the
            # final check; packages that require independent review receive no
            # relaxation tiers and therefore fail closed.
            fallback_exclusion_tiers=final_review_fallback_tiers(
                host.config, package, schedule
            ),
            output_schema=review_output_schema(),
        )
        verdict, findings = host._review_result(package, result)
        if verdict == "changes_required":
            host._queue_review_fixes(package, findings)
            return StageDisposition.STOP
        package.review_findings = []
        host._advance_package_stage(package, WorkPackageStage.READY_TO_COMMIT)
        return StageDisposition.CONTINUE


@dataclass(frozen=True)
class ReadyToCommitStageHandler:
    stage: WorkPackageStage = WorkPackageStage.READY_TO_COMMIT

    def handle(self, host: PackageStageHost, package: WorkPackage) -> StageDisposition:
        if package.kind == WorkPackageKind.REPOSITORY_SYNC:
            host._prepare_repository_sync_acceptance_evidence(package)
        host._validate_acceptance_evidence(package)
        if package.kind == WorkPackageKind.REPOSITORY_SYNC:
            host._finalize_repository_sync_package(package)
        elif package.execution_mode == "aggregate":
            host._finalize_aggregate_package(package)
        else:
            host._finalize_standard_package(package)
        return StageDisposition.STOP


class PackageStageEngine:
    """Dispatch persisted package stages through typed, independently testable handlers."""

    def __init__(self, host: PackageStageHost, handlers: tuple[PackageStageHandler, ...] | None = None):
        self._host = host
        configured = handlers or (
            DecomposeStageHandler(), PrepareStageHandler(), ImplementStageHandler(),
            VerificationStageHandler(WorkPackageStage.FAST_VERIFY, WorkPackageStage.REVIEW),
            ReviewStageHandler(), FixReviewStageHandler(),
            VerificationStageHandler(WorkPackageStage.REGRESSION_VERIFY, WorkPackageStage.FINAL_REVIEW),
            FinalReviewStageHandler(), ReadyToCommitStageHandler(),
        )
        self._handlers: Mapping[WorkPackageStage, PackageStageHandler] = {handler.stage: handler for handler in configured}
        if len(self._handlers) != len(configured):
            raise ValueError("duplicate package-stage handler")

    @property
    def handled_stages(self) -> frozenset[WorkPackageStage]:
        return frozenset(self._handlers)

    def process(self, package: WorkPackage) -> None:
        if package.kind == WorkPackageKind.REPOSITORY_SYNC:
            self._host._process_repository_sync_pre_stages(package)
        if package.decomposition_required and not package.decomposition_status and package.stage != WorkPackageStage.DECOMPOSE:
            self._host._enter_decomposition(package, trigger="mandatory")
        elif self._host._should_auto_decompose(package):
            self._host._enter_decomposition(package)

        while True:
            handler = self._handlers.get(package.stage)
            if handler is None:
                return
            previous_stage = package.stage
            disposition = handler.handle(self._host, package)
            if disposition is StageDisposition.STOP or package.stage == previous_stage:
                return
