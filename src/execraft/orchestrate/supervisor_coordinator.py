"""Supervisor incident and recovery coordinator.

Coordinates supervisor incident creation, execution, delegation, auto-decision,
human escalation, and resume logic for autonomous incident recovery.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Mapping

from .models import (
    OrchestrateError,
    TaskExecutionState,
    WorkPackage,
    WorkPackageKind,
    WorkPackageStage,
    _AgentWaitRequested,
)
from .scheduler import (
    AgentAdapter,
    AgentCapability,
    StructuredHandoff,
)
from execraft.skills import SkillCatalog, SkillCatalogError
from .execution_policy import effective_skill_ids
from .supervisor import (
    IncidentClass,
    IncidentStatus,
    SupervisorDecision,
    SupervisorDelegation,
    SupervisorIncident,
    SupervisorIncidentStore,
    bounded_strings,
    delegation_role,
    is_recoverable_prompt_transport_failure,
    provider_is_allowed_supervisor,
)
from .structured_output import review_output_schema
from .supervisor_contract import compile_supervisor_decision
from .repository_sync_flow import repository_sync_verification_scope
from .verification import resolve_profile
from .verification_outcome import (
    LEGACY_REPOSITORY_SYNC_COMMIT_CHECK_REASON,
    verification_is_accepted,
)
from .workspace_recovery import flatten_dirty_paths

logger = logging.getLogger(__name__)


class SupervisorCoordinator:
    """Coordinate supervisor incident creation, execution, and recovery."""

    def __init__(self, host: Any):
        self._host = host

    @property
    def _supervisor_incidents(self) -> SupervisorIncidentStore:
        """Delegate to host's supervisor incident store."""
        return self._host._supervisor_incidents

    @property
    def _journal(self) -> Any:
        """Delegate to host's journal."""
        return self._host._journal

    @property
    def _skill_catalog(self) -> SkillCatalog:
        """Delegate to host's skill catalog."""
        return self._host._skill_catalog

    @property
    def config(self) -> Any:
        """Delegate to host's configuration."""
        return self._host.config

    def can_auto_resume_supervisor_transport_failure(self) -> bool:
        """Return whether a supervisor transport failure can be auto-resumed."""
        if self._host.state != TaskExecutionState.WAITING_FOR_HUMAN_DECISION:
            return False
        incident = self._supervisor_incidents.active()
        if incident is None or incident.status != IncidentStatus.WAITING_FOR_HUMAN:
            return False
        if self.supervisor_adapter() is None:
            return False
        evidence = "\n".join(
            (
                incident.summary,
                json.dumps(incident.human_question, ensure_ascii=False),
            )
        )
        return is_recoverable_prompt_transport_failure(evidence)

    def can_auto_resume_lost_supervisor_delegation(self) -> bool:
        """Return whether a lost supervisor delegation can be auto-resumed."""
        return self._host._scope_recovery_coordinator.can_auto_resume_lost_supervisor_delegation()

    def resume_obsolete_repository_sync_verification(self) -> bool:
        """Retire a test-failure incident whose failing checks are no longer in scope.

        This is intentionally narrow: it applies only when a repository-sync
        transaction proves that every previously failing command belonged to a
        no-op repository.  The failed attempts then describe the old verification
        scope, not the current candidate, so spending a Supervisor call on them
        cannot improve the workspace.
        """

        host = self._host
        if host.state not in {
            TaskExecutionState.WAITING_FOR_AGENT, TaskExecutionState.SUPERVISING, TaskExecutionState.HUMAN_REQUIRED
        }:
            return False
        incident = self._supervisor_incidents.active()
        if incident is None or incident.classification != IncidentClass.TEST_FAILURE:
            return False
        try:
            package = host._state_record.plan_graph.package_by_id(incident.package_id)
        except OrchestrateError:
            return False
        if (
            package.kind != WorkPackageKind.REPOSITORY_SYNC
            or package.stage not in {
                WorkPackageStage.FAST_VERIFY, WorkPackageStage.REGRESSION_VERIFY
            }
            or str((package.last_verification or {}).get("status", "")) != "failed"
        ):
            return False
        transaction = host._repository_sync_service().transactions.load(package.id)
        if transaction is None:
            return False
        scope = repository_sync_verification_scope(
            host.registry, resolve_profile(package.verification_profile),
            package.affected_repositories, transaction,
        )
        active_commands = {command.command for command in scope.commands}
        failed_commands = {
            str(item.get("command", ""))
            for item in (package.last_verification or {}).get("failed_commands", [])
            if isinstance(item, Mapping) and str(item.get("command", ""))
        }
        if not failed_commands or not failed_commands.isdisjoint(active_commands):
            return False

        incident.pending_delegations = []
        incident.pending_delegation_index = 0
        incident.human_question = {}
        incident.human_answer = {}
        incident.summary = (
            "Superseded deterministically: every previously failing verification "
            "command belongs to a repository proven no-op by the sync transaction."
        )
        incident.actions_taken.append("obsolete repository-sync verification scope retired")
        incident.touch(status=IncidentStatus.RESOLVED)
        self._supervisor_incidents.save(incident)
        package.verification_attempts = 0
        host._clear_agent_wait(package.id)
        host._state_record.error_message = ""
        host.save_state()
        event = {
            "incident_id": incident.incident_id,
            "package_id": package.id,
            "failed_commands": sorted(failed_commands),
            "skipped_noop_repositories": list(scope.skipped_noop_repositories),
        }
        self._journal.append("repository_sync_verification_incident_superseded", event)
        host._emit_progress("repository_sync_verification_recovery", **event)
        host.transition_to(TaskExecutionState.RUNNING)
        return True

    def can_resume_obsolete_repository_sync_commit_check(self) -> bool:
        """Return whether a legacy literal-``passed`` commit readiness check is obsolete."""

        return self._obsolete_repository_sync_commit_check_candidate() is not None

    def resume_obsolete_repository_sync_commit_check(self) -> bool:
        """Retire a stale commit-check incident after effective verification passed.

        Older repository-sync completion code accepted only the literal status
        ``passed``.  Packages verified through an exact, operator-authorized
        baseline therefore reached READY_TO_COMMIT with an approved final review
        and were incorrectly escalated.  This migration is deliberately keyed to
        that exact historical escalation text and a verified repository-sync
        transaction so genuine commit failures remain blocking.
        """

        candidate = self._obsolete_repository_sync_commit_check_candidate()
        if candidate is None:
            return False
        package, incident = candidate
        host = self._host
        previous_status = package.status
        if package.status != "pending":
            package.status = "pending"

        if incident is not None:
            incident.pending_delegations = []
            incident.pending_delegation_index = 0
            incident.human_question = {}
            incident.human_answer = {}
            incident.summary = (
                "Superseded deterministically: repository-sync verification was "
                "effectively accepted and final review approved; the previous "
                "commit readiness check recognized only literal 'passed'."
            )
            incident.actions_taken.append(
                "obsolete repository-sync literal-pass commit readiness check retired"
            )
            incident.touch(status=IncidentStatus.RESOLVED)
            self._supervisor_incidents.save(incident)

        host._clear_agent_wait(package.id)
        host._state_record.error_message = ""
        host.save_state()
        event = {
            "package_id": package.id,
            "previous_status": previous_status,
            "next_status": package.status,
            "verification_status": str(
                (package.last_verification or {}).get("status", "")
            ),
            "review_verdict": str((package.last_review or {}).get("verdict", "")),
            "incident_id": incident.incident_id if incident is not None else "",
        }
        self._journal.append("repository_sync_commit_check_superseded", event)
        host._emit_progress("repository_sync_commit_check_recovery", **event)
        if host.state != TaskExecutionState.RUNNING:
            host.transition_to(TaskExecutionState.RUNNING)
        return True

    def _obsolete_repository_sync_commit_check_candidate(
        self,
    ) -> tuple[WorkPackage, SupervisorIncident | None] | None:
        host = self._host
        if host.state not in {
            TaskExecutionState.HUMAN_REQUIRED,
            TaskExecutionState.SUPERVISING,
            TaskExecutionState.WAITING_FOR_AGENT,
        }:
            return None

        incident = self._supervisor_incidents.active()
        package_id = incident.package_id if incident is not None else ""
        if not package_id and host.state == TaskExecutionState.HUMAN_REQUIRED:
            report = host.human_required_report() or {}
            package_id = str(report.get("package_id", "")).strip()
        if not package_id:
            return None

        escalation = self._latest_escalation_for_package(package_id, incident)
        if escalation is None:
            return None
        reason = str(escalation.payload.get("blocked_requirement", "")).strip().lower()
        if reason != LEGACY_REPOSITORY_SYNC_COMMIT_CHECK_REASON:
            return None

        try:
            package = host._state_record.plan_graph.package_by_id(package_id)
        except OrchestrateError:
            return None
        if (
            package.kind != WorkPackageKind.REPOSITORY_SYNC
            or package.stage != WorkPackageStage.READY_TO_COMMIT
            or not verification_is_accepted(package.last_verification)
            or str((package.last_review or {}).get("verdict", "")).strip().lower()
            != "approved"
        ):
            return None

        transaction = host._repository_sync_service().transactions.load(package.id)
        if transaction is None:
            return None
        phase = str(getattr(transaction, "phase", "")).strip().lower()
        if not (getattr(transaction, "complete", False) or phase in {"verified", "committing"}):
            return None
        return package, incident

    def _latest_escalation_for_package(
        self, package_id: str, incident: SupervisorIncident | None
    ) -> Any | None:
        entries = self._journal.read()
        if incident is not None and incident.escalation_sequence is not None:
            exact = next(
                (
                    entry
                    for entry in entries
                    if entry.sequence == incident.escalation_sequence
                    and entry.event_type == "human_intervention_required"
                ),
                None,
            )
            if exact is not None:
                return exact
        return next(
            (
                entry
                for entry in reversed(entries)
                if entry.event_type == "human_intervention_required"
                and str(entry.payload.get("package_id", "")) == package_id
            ),
            None,
        )

    def supervisor_adapters(self) -> list[AgentAdapter]:
        """Return all eligible supervisor adapters."""
        policy = self.config.supervisor_policy
        if not policy.enabled:
            return []
        candidates: list[AgentAdapter] = []
        if policy.agent_ids:
            for agent_id in policy.agent_ids:
                adapter = self._host._find_adapter(agent_id)
                if adapter is not None and adapter not in candidates:
                    candidates.append(adapter)
        else:
            candidates.extend(self._host._agent_slots)
        eligible: list[AgentAdapter] = []
        for adapter in candidates:
            metadata = self._host._agent_metadata(adapter)
            if not provider_is_allowed_supervisor(metadata.get("adapter", "")):
                continue
            if AgentCapability.SUPERVISE not in adapter.capabilities:
                continue
            eligible.append(adapter)
        return eligible

    def supervisor_adapter(self) -> AgentAdapter | None:
        """Return the first configured privileged supervisor, if eligible."""
        return next(iter(self.supervisor_adapters()), None)

    def supervisor_excluded_agents(self, selected_id: str) -> set[str]:
        """Return agent IDs excluded from supervisor failover."""
        del selected_id  # The full configured pool remains eligible for failover.
        allowed = {adapter.provider_id for adapter in self.supervisor_adapters()}
        return {
            adapter.provider_id
            for adapter in self._host._agent_slots
            if adapter.provider_id not in allowed
        }

    def delegation_output_schema(self, capability: AgentCapability) -> dict[str, Any]:
        """Return the output schema for a delegated capability."""
        if capability == AgentCapability.REVIEW:
            return review_output_schema()
        if capability in {AgentCapability.IMPLEMENT, AgentCapability.FIX_REVIEW}:
            return {
                "type": "object",
                "additionalProperties": False,
                "required": ["ok", "status", "summary"],
                "properties": {
                    "ok": {"type": "boolean", "const": True},
                    "status": {
                        "type": "string",
                        "enum": ["implemented", "fixed"],
                    },
                    "summary": {"type": "string", "minLength": 1},
                },
            }
        return {}

    def execute_supervisor_round(
        self,
        package: WorkPackage,
        incident: SupervisorIncident,
        action: Mapping[str, Any],
        adapter: AgentAdapter,
    ) -> tuple[SupervisorDecision, str]:
        """Execute one supervisor round and return the decision."""
        current_candidates = self._host._scope_recovery_coordinator.workspace_scope_snapshot(package).candidate_paths
        incident.candidate_paths = list(
            dict.fromkeys([*incident.candidate_paths, *current_candidates])
        )[:512]
        incident.supervisor_agent_id = adapter.provider_id
        incident.touch(status=IncidentStatus.DIAGNOSING)
        self._supervisor_incidents.save(incident)
        self._journal.append(
            "supervisor_attempt_started",
            {
                "incident_id": incident.incident_id,
                "package_id": package.id,
                "agent_id": adapter.provider_id,
                "attempt": incident.attempts + 1,
                "classification": incident.classification.value,
            },
        )
        self._host._emit_progress(
            "supervisor_started",
            incident_id=incident.incident_id,
            package_id=package.id,
            agent_id=adapter.provider_id,
            attempt=incident.attempts + 1,
            max_attempts=self.config.supervisor_policy.max_attempts_per_incident,
        )
        handoff = self._host._supervisor_handoff(package, incident, action)
        try:
            result = self._host._execute_agent(
                AgentCapability.SUPERVISE,
                adapter.provider_id,
                handoff,
                package,
                excluded_agent_ids=self.supervisor_excluded_agents(
                    adapter.provider_id
                ),
                fallback_agent_ids=[
                    item.provider_id for item in self.supervisor_adapters()
                ],
            )
        except _AgentWaitRequested:
            # Provider transport and availability retries are governed by the
            # provider-health/wait budgets. They are not Supervisor reasoning
            # attempts because no decision was produced. Charging both budgets
            # made cooldown-only poll cycles exhaust an incident at 3/3 without
            # any model ever diagnosing it.
            incident.provider_waits += 1
            incident.touch(status=IncidentStatus.OPEN)
            self._supervisor_incidents.save(incident)
            self._journal.append(
                "supervisor_attempt_deferred",
                {
                    "incident_id": incident.incident_id,
                    "package_id": package.id,
                    "agent_id": adapter.provider_id,
                    "reason": "provider_wait",
                    "attempts": incident.attempts,
                    "contract_failures": incident.contract_failures,
                    "provider_waits": incident.provider_waits,
                },
            )
            raise
        executed_by = str(result.get("_execraft_executed_by", adapter.provider_id))
        incident.supervisor_agent_id = executed_by
        # A provider that completed a reasoning turn consumed one Supervisor
        # attempt even when its semantic decision is unusable. Formatting is no
        # longer a provider-health failure: the permissive compiler repairs
        # representation locally and only rejects genuinely ambiguous intent.
        incident.attempts += 1
        # Remove only policy-approved generated artifacts before compiling path
        # intent. This prevents an incidental __pycache__/pyc path from becoming
        # inferred retained scope or invalidating an otherwise useful decision.
        self._host._scope_recovery_coordinator.cleanup_scope_artifacts(
            package,
            flatten_dirty_paths(self._host._workspace_dirty_paths()),
            affected_only=False,
            source="supervisor_decision_compile",
        )
        current_candidates = self._host._scope_recovery_coordinator.workspace_scope_snapshot(package).candidate_paths
        try:
            compiled = compile_supervisor_decision(
                result,
                candidate_paths=tuple(dict.fromkeys([*incident.candidate_paths, *current_candidates])),
                require_human_for_high_impact=(
                    self.config.supervisor_policy.require_human_for_destructive_actions
                ),
            )
        except ValueError as exc:
            incident.summary = str(exc)
            incident.touch()
            self._supervisor_incidents.save(incident)
            raise OrchestrateError(f"unusable supervisor decision: {exc}") from exc
        decision = compiled.decision
        incident.classification = decision.classification
        incident.summary = decision.summary
        incident.actions_taken.extend(
            item for item in decision.actions_taken if item not in incident.actions_taken
        )
        incident.touch()
        self._supervisor_incidents.save(incident)
        self._journal.append(
            "supervisor_decision",
            {
                "incident_id": incident.incident_id,
                "package_id": package.id,
                "agent_id": executed_by,
                "decision": decision.decision.value,
                "classification": decision.classification.value,
                "summary": decision.summary,
                "actions_taken": list(decision.actions_taken),
                "delegation_count": len(decision.delegations),
                "decision_source": compiled.source,
                "normalization_warnings": list(compiled.warnings),
            },
        )
        if compiled.warnings:
            self._host._emit_progress(
                "supervisor_decision_normalized",
                incident_id=incident.incident_id,
                package_id=package.id,
                agent_id=executed_by,
                source=compiled.source,
                warnings=list(compiled.warnings),
            )
        return decision, executed_by

    def execute_supervisor_delegations(
        self,
        package: WorkPackage,
        incident: SupervisorIncident,
        delegations: tuple[Any, ...] = (),
    ) -> None:
        """Execute or resume one durable Supervisor delegation round."""
        policy = self.config.supervisor_policy
        if delegations:
            if incident.pending_delegations:
                raise OrchestrateError(
                    "cannot replace an unfinished Supervisor delegation round"
                )
            remaining = policy.max_agent_delegations - incident.delegated_tasks
            if len(delegations) > remaining:
                raise OrchestrateError(
                    f"supervisor requested {len(delegations)} delegations with only "
                    f"{remaining} remaining in the incident budget"
                )
            if incident.delegation_rounds >= policy.max_delegation_rounds:
                raise OrchestrateError(
                    "supervisor delegation round budget exhausted"
                )
            incident.delegation_rounds += 1
            incident.pending_delegations = [
                item.as_mapping()
                if isinstance(item, SupervisorDelegation)
                else dict(item)
                for item in delegations
            ]
            incident.pending_delegation_index = 0

        if not incident.pending_delegations:
            incident.touch(status=IncidentStatus.DIAGNOSING)
            self._supervisor_incidents.save(incident)
            return

        incident.touch(status=IncidentStatus.DELEGATING)
        self._supervisor_incidents.save(incident)
        supervisor_id = incident.supervisor_agent_id
        total = len(incident.pending_delegations)

        while incident.pending_delegation_index < total:
            index = incident.pending_delegation_index
            try:
                delegation = SupervisorDelegation.from_mapping(
                    incident.pending_delegations[index]
                )
            except ValueError as exc:
                raise OrchestrateError(
                    f"invalid persisted Supervisor delegation {index + 1}: {exc}"
                ) from exc

            role = delegation_role(delegation.capability)
            skill_ids = list(delegation.skill_ids) or effective_skill_ids(role, [])
            try:
                skills = [
                    item.as_mapping()
                    for item in self._skill_catalog.materialize(role, skill_ids)
                ]
            except SkillCatalogError as exc:
                raise OrchestrateError(
                    f"invalid supervisor delegation skill policy: {exc}"
                ) from exc

            excluded = {supervisor_id} if supervisor_id else set()
            agent_id = delegation.agent_id
            adapter = self._host._find_adapter(agent_id) if agent_id else None
            if agent_id and adapter is None:
                raise OrchestrateError(
                    f"supervisor delegation references unknown agent {agent_id!r}"
                )
            if adapter is not None:
                agent_id = adapter.provider_id
                if agent_id == supervisor_id:
                    # A Supervisor naming itself is a predictable model mistake,
                    # and it is recorded in a durable delegation that resumes
                    # before every supervision round.  Raising here wedged the
                    # incident permanently: no restart, operator answer, or
                    # budget reset ever reached the code that could replace the
                    # choice.  Degrade to ordinary selection instead -- the
                    # supervisor stays in ``excluded``, so the anti-recursion
                    # guarantee is unchanged.
                    self._journal.append(
                        "supervisor_delegation_self_reference_replaced",
                        {
                            "incident_id": incident.incident_id,
                            "package_id": package.id,
                            "index": index + 1,
                            "agent_id": agent_id,
                            "capability": delegation.capability.value,
                        },
                    )
                    agent_id = ""
                elif delegation.capability not in adapter.capabilities:
                    raise OrchestrateError(
                        f"delegated agent {agent_id!r} does not support "
                        f"{delegation.capability.value}"
                    )
            if not agent_id:
                agent_id = self._host._select_agent_for_capability(
                    delegation.capability,
                    package=package,
                    exclude_ids=excluded,
                ) or ""
            if not agent_id:
                self._host._wait_for_agent_availability(
                    package,
                    delegation.capability,
                    [],
                    excluded_agent_ids=excluded,
                )

            handoff = StructuredHandoff(
                work_package_id=package.id,
                stage=f"supervisor_delegate_{delegation.capability.value}",
                summary=delegation.task,
                repository_diff_summary=self._host._supervisor_workspace_summary(),
                relevant_decisions=[
                    f"Supervisor incident: {incident.incident_id}",
                    "Return findings or completed changes to the Supervisor; do not commit, push, or change branches.",
                ],
                requirements=list(package.requirements),
                acceptance_criteria=[
                    {"id": item.id, "description": item.description}
                    for item in package.acceptance_criteria
                ],
                expected_output_schema=self.delegation_output_schema(
                    delegation.capability
                ),
                working_directory=str(self._host._workspace_root or Path.cwd()),
                read_only=delegation.read_only,
                workflow_skills=skills,
            )
            self._journal.append(
                "supervisor_delegation_started",
                {
                    "incident_id": incident.incident_id,
                    "package_id": package.id,
                    "index": index + 1,
                    "total": total,
                    "task": delegation.task,
                    "capability": delegation.capability.value,
                    "agent_id": agent_id,
                    "skills": list(delegation.skill_ids),
                    "read_only": delegation.read_only,
                },
            )
            self._host._emit_progress(
                "supervisor_delegation_started",
                incident_id=incident.incident_id,
                package_id=package.id,
                index=index + 1,
                total=total,
                capability=delegation.capability.value,
            )
            try:
                result = self._host._execute_agent(
                    delegation.capability,
                    agent_id,
                    handoff,
                    package,
                    excluded_agent_ids=excluded,
                )
                payload = self._host._agent_payload(result)
                artifact = result.get("_execraft_agent_artifact")
                findings_raw = payload.get("findings") or payload.get("resolved_findings") or []
                record = {
                    "agent_id": str(result.get("_execraft_executed_by", agent_id)),
                    "capability": delegation.capability.value,
                    "task": delegation.task,
                    "skills": list(skill_ids),
                    "summary": str(
                        payload.get("summary")
                        or payload.get("final_message")
                        or payload.get("status")
                        or "delegation completed"
                    )[:8000],
                    "findings": bounded_strings(findings_raw),
                    "artifact": dict(artifact) if isinstance(artifact, Mapping) else {},
                }
                incident.delegation_results.append(record)
                incident.delegated_tasks += 1
                incident.pending_delegation_index += 1
                incident.touch(status=IncidentStatus.DELEGATING)
                self._supervisor_incidents.save(incident)
                self._journal.append(
                    "supervisor_delegation_finished",
                    {
                        "incident_id": incident.incident_id,
                        "package_id": package.id,
                        "index": index + 1,
                        "total": total,
                        **record,
                    },
                )
            except _AgentWaitRequested:
                self._journal.append(
                    "supervisor_delegation_deferred",
                    {
                        "incident_id": incident.incident_id,
                        "package_id": package.id,
                        "index": index + 1,
                        "reason": "provider_wait",
                    },
                )
                raise

        incident.pending_delegations = []
        incident.pending_delegation_index = 0
        incident.touch(status=IncidentStatus.DIAGNOSING)
        self._supervisor_incidents.save(incident)

    def supervisor_recovery_incident(self) -> SupervisorIncident | None:
        """Return the active supervisor incident, if any."""
        return self._supervisor_incidents.active()

    def _supervisor_action(
        self, incident: SupervisorIncident
    ) -> Mapping[str, Any] | None:
        """Return the journal action that triggered the incident."""
        if incident.escalation_sequence is None:
            return None
        for entry in self._journal.list():
            if entry.sequence == incident.escalation_sequence:
                return entry.payload or {}
        return None

    def _supervisor_package(
        self, action: Mapping[str, Any], incident: SupervisorIncident
    ) -> WorkPackage | None:
        """Return the package associated with the incident."""
        package_id = str(
            action.get("package_id", "") or incident.package_id
        )
        if not package_id:
            return None
        try:
            return self._host._state_record.plan_graph.package_by_id(package_id)
        except OrchestrateError:
            return None
