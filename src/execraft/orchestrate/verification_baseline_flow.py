"""Orchestrator flow integration for package-scoped verification baselines.

The pure fingerprinting/matching policy lives in :mod:`verification_baseline`.
This mixin owns only orchestration side effects: journal events, durable package
state, Supervisor incident retirement, and restart migration.
"""

from __future__ import annotations

from typing import Any, Mapping

from .models import OrchestrateError, TaskExecutionState, WorkPackage, utc_now
from .supervisor import IncidentStatus, SupervisorIncident
from .verification import VerificationCommand, VerificationResult
from .verification_baseline import (
    build_package_baseline_acceptance,
    is_explicit_baseline_acceptance,
    legacy_package_baseline_candidate,
    match_result_against_baseline,
)


class VerificationBaselineFlow:
    """Small state-machine extension mixed into ``ProjectOrchestrator``."""

    def _consume_verification_baseline_match(
        self,
        package: WorkPackage,
        profile: str,
        command: VerificationCommand,
        result: VerificationResult,
    ) -> dict[str, Any] | None:
        match = match_result_against_baseline(
            package.verification_baseline_acceptance,
            package_id=package.id,
            profile=profile,
            repository_id=command.repository_id,
            command=command.command,
            result=result,
            package_completed=package.stage.value == "completed",
        )
        if not match.accepted:
            return None
        mapping = match.as_mapping()
        self._journal.append(
            "verification_baseline_matched",
            {"package_id": package.id, "profile": profile, **mapping},
        )
        self._emit_progress(
            "verification_baseline_matched",
            package_id=package.id,
            repository_id=command.repository_id,
            accepted_failures=len(match.observed),
        )
        return mapping

    def _handle_operator_verification_baseline_answer(
        self,
        incident: SupervisorIncident,
        question: Mapping[str, Any],
        selected: str,
        guidance: str,
    ) -> bool:
        selected_option = next(
            (
                item
                for item in question.get("options", [])
                if isinstance(item, Mapping)
                and str(item.get("id", "")).strip() == selected
            ),
            {},
        )
        try:
            package = self._state_record.plan_graph.package_by_id(incident.package_id)
        except OrchestrateError:
            return False
        return self._accept_verification_baseline_from_operator(
            package, incident, question, selected_option, guidance
        )

    def _accept_verification_baseline_from_operator(
        self,
        package: WorkPackage,
        incident: SupervisorIncident,
        question: Mapping[str, Any],
        selected_option: Mapping[str, Any],
        guidance: str,
    ) -> bool:
        if not is_explicit_baseline_acceptance(question, selected_option, guidance):
            return False
        authorization_text = " ".join(
            str(value)
            for value in (
                question.get("question", ""),
                question.get("context", ""),
                selected_option.get("label", ""),
                selected_option.get("consequence", ""),
                guidance,
            )
        )
        acceptance = build_package_baseline_acceptance(
            package,
            self.registry.commands,
            accepted_at=utc_now(),
            incident_id=incident.incident_id,
            authorization_text=authorization_text,
        )
        if acceptance is None:
            return False
        package.verification_baseline_acceptance = acceptance
        package.status = "pending"
        incident.summary = (
            "Operator accepted the exact package-scoped verification baseline; "
            "identical fingerprints now bypass repeated Supervisor escalation."
        )
        incident.human_question = {}
        incident.touch(status=IncidentStatus.RESOLVED)
        self._supervisor_incidents.save(incident)
        self._clear_agent_wait(package.id)
        self._journal.append(
            "verification_baseline_accepted",
            {
                "package_id": package.id,
                "incident_id": incident.incident_id,
                "accepted_by": "operator",
                "profile": acceptance.get("profile", ""),
                "commands": acceptance.get("commands", []),
                "expires_on": acceptance.get("expires_on", ""),
            },
        )
        self.save_state()
        self._emit_progress(
            "verification_baseline_accepted",
            package_id=package.id,
            command_count=len(acceptance.get("commands", [])),
        )
        if self.state == TaskExecutionState.WAITING_FOR_HUMAN_DECISION:
            self.transition_to(TaskExecutionState.SUPERVISING)
        self.transition_to(TaskExecutionState.RUNNING)
        return True

    def _verification_baseline_candidate(
        self,
    ) -> tuple[WorkPackage, dict[str, Any]] | None:
        return legacy_package_baseline_candidate(
            state_value=self.state.value,
            human_report=self.human_required_report(),
            plan_graph=self._state_record.plan_graph,
            configured_commands=self.registry.commands,
            journal_entries=self._journal.read(),
            utc_now_value=utc_now(),
        )

    def can_auto_resume_accepted_verification_baseline(self) -> bool:
        return self._verification_baseline_candidate() is not None

    def _resume_accepted_verification_baseline_unlocked(self) -> None:
        candidate = self._verification_baseline_candidate()
        if candidate is None:
            raise OrchestrateError("no accepted verification baseline can be resumed")
        package, acceptance = candidate
        migrated = not bool(package.verification_baseline_acceptance)
        package.verification_baseline_acceptance = acceptance
        package.status = "pending"
        active = self._supervisor_incidents.active()
        if active is not None and active.package_id == package.id:
            active.summary = "Retired: operator baseline authorization is deterministic."
            active.human_question = {}
            active.human_answer = {}
            active.touch(status=IncidentStatus.RESOLVED)
            self._supervisor_incidents.save(active)
        self._clear_agent_wait(package.id)
        self._state_record.error_message = ""
        self._journal.append(
            "verification_baseline_authorization_resumed",
            {
                "package_id": package.id,
                "migrated_legacy_decision": migrated,
                "profile": acceptance.get("profile", ""),
                "commands": acceptance.get("commands", []),
            },
        )
        self.save_state()
        self.transition_to(TaskExecutionState.RUNNING)
