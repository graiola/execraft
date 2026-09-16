"""Durable identity and retry-budget lifecycle for Supervisor campaigns.

Exact incident fingerprints remain sequence-sensitive audit identifiers.  A
separate semantic escalation key prevents daemon restarts or re-emitted journal
events from silently replenishing a bounded Supervisor retry budget.
"""

from __future__ import annotations

from typing import Any, Mapping

from .models import TaskExecutionState, WorkPackage
from .supervisor import (
    IncidentStatus,
    SupervisorIncident,
    incident_escalation_key,
    incident_fingerprint,
)


class SupervisorCampaignCoordinator:
    """Own Supervisor campaign identity, terminal latching, and retry events."""

    def __init__(self, host: Any):
        self._host = host

    @staticmethod
    def _action_payload(entry: Any) -> dict[str, Any]:
        payload = dict(entry.payload)
        payload.setdefault("sequence", entry.sequence)
        payload.setdefault("timestamp", entry.timestamp)
        payload.setdefault(
            "reason",
            payload.get("blocked_requirement") or "manual decision required",
        )
        return payload

    def action_for_incident(self, incident: SupervisorIncident) -> dict[str, Any]:
        """Return only the journal escalation that originally opened an incident."""

        if incident.escalation_sequence is None:
            return {}
        selected = next(
            (
                entry
                for entry in self._host._journal.read()
                if entry.sequence == incident.escalation_sequence
                and entry.event_type == "human_intervention_required"
            ),
            None,
        )
        return self._action_payload(selected) if selected is not None else {}

    def action(self, incident: SupervisorIncident | None = None) -> dict[str, Any]:
        """Return the durable escalation associated with the current campaign."""

        if incident is not None:
            exact = self.action_for_incident(incident)
            if exact:
                return exact
        selected = next(
            (
                entry
                for entry in reversed(self._host._journal.read())
                if entry.event_type == "human_intervention_required"
            ),
            None,
        )
        return self._action_payload(selected) if selected is not None else {}

    @staticmethod
    def identity(action: Mapping[str, Any]) -> tuple[str, str]:
        return incident_fingerprint(action), incident_escalation_key(action)

    def migrate_incident_key(
        self,
        incident: SupervisorIncident,
        *,
        persist: bool = False,
    ) -> str:
        """Reconstruct a legacy incident's semantic identity without unsafe fallback."""

        if incident.escalation_key:
            return incident.escalation_key
        action = self.action_for_incident(incident)
        if not action:
            return ""
        key = incident_escalation_key(action)
        if persist:
            incident.escalation_key = key
            self._host._supervisor_incidents.save(incident)
        return key

    def latest_exhausted(
        self,
        action: Mapping[str, Any],
        *,
        exclude_incident_id: str = "",
        persist_migration: bool = False,
    ) -> SupervisorIncident | None:
        """Find an exhausted campaign matching the exact or semantic escalation."""

        store = self._host._supervisor_incidents
        exact = store.latest_exhausted_for_fingerprint(
            incident_fingerprint(action),
            exclude_incident_id=exclude_incident_id,
        )
        if exact is not None:
            return exact

        campaign_key = incident_escalation_key(action)
        keyed = store.latest_exhausted_for_escalation_key(
            campaign_key,
            exclude_incident_id=exclude_incident_id,
        )
        if keyed is not None:
            return keyed

        excluded = str(exclude_incident_id).strip()
        for candidate in reversed(store.list()):
            if (
                candidate.status != IncidentStatus.EXHAUSTED
                or candidate.incident_id == excluded
            ):
                continue
            if self.migrate_incident_key(candidate, persist=persist_migration) == campaign_key:
                return candidate
        return None

    def migrate_active_key(self, incident: SupervisorIncident | None) -> None:
        if incident is not None and not incident.escalation_key:
            self.migrate_incident_key(incident, persist=True)

    def reconcile_exhausted_duplicate(
        self,
        package: WorkPackage,
        active: SupervisorIncident | None,
        action: Mapping[str, Any],
        *,
        fingerprint: str,
        escalation_key: str,
    ) -> bool:
        """Suppress a reopened incident when the same campaign already exhausted."""

        predecessor = self.latest_exhausted(
            action,
            exclude_incident_id=(active.incident_id if active is not None else ""),
            persist_migration=True,
        )
        if predecessor is None:
            return False

        if active is not None:
            active.summary = (
                "Legacy duplicate Supervisor incident suppressed because "
                f"incident {predecessor.incident_id} already exhausted the same "
                "semantic escalation campaign."
            )
            active.touch(status=IncidentStatus.EXHAUSTED)
            self._host._supervisor_incidents.save(active)
            self._host._clear_agent_wait(package.id)
            payload = {
                "incident_id": active.incident_id,
                "exhausted_predecessor_id": predecessor.incident_id,
                "package_id": package.id,
                "fingerprint": fingerprint,
                "escalation_key": escalation_key,
            }
            self._host._journal.append("supervisor_duplicate_incident_reconciled", payload)
            self._host._emit_progress(
                "supervisor_duplicate_incident_reconciled",
                incident_id=active.incident_id,
                exhausted_predecessor_id=predecessor.incident_id,
                package_id=package.id,
            )
        if self._host.state != TaskExecutionState.HUMAN_REQUIRED:
            self._host.transition_to(TaskExecutionState.HUMAN_REQUIRED)
        return True

    def exhaust(
        self,
        package: WorkPackage,
        incident: SupervisorIncident,
        *,
        reason: str,
    ) -> None:
        """Persist a terminal campaign or ask the operator per Supervisor policy."""

        if self._host.config.supervisor_policy.ask_human_when_uncertain:
            self._host._pause_for_supervisor_question(
                package,
                incident,
                {
                    "question": (
                        "The Supervisor could not safely resolve this incident within "
                        "its configured attempt budget. How should it proceed?"
                    ),
                    "context": reason,
                    "recommended_option": "inspect_and_retry",
                    "options": [
                        {
                            "id": "inspect_and_retry",
                            "label": "Let the Supervisor inspect again with my guidance",
                            "consequence": (
                                "The answer text will be supplied to a fresh bounded "
                                "supervision attempt."
                            ),
                            "weight": 50,
                            "risk": "unknown",
                        },
                        {
                            "id": "stop",
                            "label": "Stop and leave the workspace unchanged",
                            "consequence": (
                                "The task remains paused until an operator repairs it "
                                "manually."
                            ),
                            "weight": 50,
                            "risk": "unknown",
                        },
                    ],
                },
            )
            return
        if not incident.escalation_key:
            incident.escalation_key = self.migrate_incident_key(incident)
        incident.summary = reason
        incident.touch(status=IncidentStatus.EXHAUSTED)
        self._host._supervisor_incidents.save(incident)
        self._host._journal.append(
            "supervisor_incident_exhausted",
            {
                "incident_id": incident.incident_id,
                "package_id": package.id,
                "attempts": incident.attempts,
                "provider_waits": incident.provider_waits,
                "contract_failures": incident.contract_failures,
                "fingerprint": incident.fingerprint,
                "escalation_key": incident.escalation_key,
                "reason": reason[:4000],
            },
        )
        self._host._emit_progress(
            "supervisor_exhausted",
            incident_id=incident.incident_id,
            package_id=package.id,
            attempts=incident.attempts,
            reason=reason[:1000],
        )
        if self._host.state != TaskExecutionState.HUMAN_REQUIRED:
            self._host.transition_to(TaskExecutionState.HUMAN_REQUIRED)

    def schedule_retry(
        self,
        package: WorkPackage,
        incident: SupervisorIncident,
        *,
        agent_id: str,
        error: str,
        max_attempts: int,
    ) -> None:
        """Record a recoverable failed round without fabricating a human-decision hold."""

        self._host._journal.append(
            "supervisor_retry_scheduled",
            {
                "incident_id": incident.incident_id,
                "package_id": package.id,
                "agent_id": agent_id,
                "attempts": incident.attempts,
                "max_attempts": max_attempts,
                "error": error[:4000],
            },
        )
        self._host._emit_progress(
            "supervisor_retry_scheduled",
            incident_id=incident.incident_id,
            package_id=package.id,
            agent_id=agent_id,
            attempt=incident.attempts + 1,
            max_attempts=max_attempts,
            reason=error[:1000],
        )
