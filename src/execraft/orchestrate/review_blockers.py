"""Deterministic classification of review findings that require external action.

Review/fix retries are useful only when an implementation-capable agent can
actually change the workspace to resolve the finding.  Some final-review
findings instead require capabilities outside the agent execution sandbox:
physical hardware, host networking, container daemons, credentials, or a real
simulator run that must produce durable evidence.

Those findings must remain blocking, but repeatedly sending them to source-code
fixers wastes the bounded review budget and obscures the real operator action.
This module deliberately uses a conservative explicit marker plus narrow legacy
phrases.  Ambiguous findings remain ordinary repairable findings.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol

from .models import TaskExecutionState, WorkPackage, _StageEscalated


EXTERNAL_ACTION_MARKER = "[EXTERNAL_ACTION_REQUIRED]"

# Strong indications that the current agent execution environment cannot perform
# the required action.  These are intentionally phrases rather than broad words
# such as "network" or "docker", which can also describe ordinary fixable code.
_ENVIRONMENT_BLOCK_MARKERS = (
    "sandbox denied",
    "sandbox restriction",
    "sandbox does not permit",
    "cannot be executed in this environment",
    "cannot execute in this environment",
    "cannot be run in this environment",
    "cannot run in this environment",
    "requires physical hardware",
    "physical hardware unavailable",
    "hardware is unavailable",
    "docker daemon unavailable",
    "docker is unavailable",
    "af_inet denied",
    "af_inet is denied",
    "network access unavailable",
    "network access is unavailable",
    "credentials are required",
    "credential is required",
    "external service unavailable",
    "simulator unavailable",
    "host-only acceptance",
)

# A strong environment marker must also be connected to acceptance/verification
# work.  This avoids treating prose about an implementation's own sandbox or
# network handling as an external operator blocker.

_SOURCE_REPAIR_MARKERS = (
    "fix the implementation",
    "modify the implementation",
    "change the implementation",
    "remove the assertion",
    "remove the assertions",
    "fix the test",
    "fix the tests",
    "change the test",
    "change the tests",
    "update the test",
    "update the tests",
)

_ACCEPTANCE_ACTION_MARKERS = (
    "acceptance",
    "verification",
    "evidence",
    "real simulation",
    "real-simulation",
    "gazebo",
    "px4",
    "hardware",
    "docker",
    "af_inet",
    "credential",
)


def is_external_action_finding(finding: object) -> bool:
    """Return whether a blocking review finding requires non-agent action.

    Reviewers are instructed to use :data:`EXTERNAL_ACTION_MARKER` for new
    findings.  The phrase matcher preserves correct behavior for durable review
    artifacts produced by older prompts, including sandbox-denied simulation
    acceptance runs.
    """

    text = " ".join(str(finding or "").split()).casefold()
    if not text:
        return False
    if EXTERNAL_ACTION_MARKER.casefold() in text:
        return True
    if any(marker in text for marker in _SOURCE_REPAIR_MARKERS):
        return False
    return any(marker in text for marker in _ENVIRONMENT_BLOCK_MARKERS) and any(
        marker in text for marker in _ACCEPTANCE_ACTION_MARKERS
    )


def external_action_findings(findings: Iterable[object]) -> tuple[str, ...]:
    """Return normalized findings that are classified as external blockers."""

    result: list[str] = []
    for raw in findings:
        text = str(raw).strip()
        if text and is_external_action_finding(text) and text not in result:
            result.append(text)
    return tuple(result)


def all_findings_require_external_action(findings: Iterable[object]) -> bool:
    """Return true only when every non-empty blocking finding is external.

    Mixed finding sets continue through normal fix cycles so source-repairable
    defects are not hidden behind an unrelated host/hardware requirement.
    """

    normalized = tuple(
        dict.fromkeys(str(item).strip() for item in findings if str(item).strip())
    )
    return bool(normalized) and len(external_action_findings(normalized)) == len(
        normalized
    )


class ExternalReviewActionHost(Protocol):
    """Minimal orchestration surface needed to persist an external check."""

    _journal: Any

    def _emit_progress(self, event_type: str, **payload: Any) -> None: ...
    def transition_to(self, new_state: TaskExecutionState) -> None: ...


def escalate_external_review_action(
    host: ExternalReviewActionHost, package: WorkPackage, findings: Iterable[object]
) -> None:
    """Persist an actionable external check and stop the current package stage."""

    normalized = [str(item).strip() for item in findings if str(item).strip()]
    package.review_findings = normalized
    escalation = {
        "package_id": package.id,
        "stage": package.stage.value,
        "blocked_requirement": "external acceptance action required",
        "attempted_resolutions": [
            "independent review confirmed the remaining blocker cannot be resolved by source edits in the current agent execution environment",
            "preserved the exact blocking review finding without consuming another review/fix cycle",
        ],
        "evidence": list(external_action_findings(normalized)),
        "bounded_options": [
            "execute the required host/hardware/simulator acceptance action and persist its durable evidence, then resume",
            "add or correct a host-side verification command that can produce the required evidence, run it, then resume",
            "replan the acceptance criterion only if the external requirement itself is no longer valid",
        ],
        "impact": (
            f"work package '{package.id}' remains blocked at '{package.stage.value}' "
            "until the external acceptance evidence exists"
        ),
        "recommended_decision": (
            "perform the external acceptance run, retain the generated evidence in "
            "the declared workspace, then resume to rerun final review"
        ),
        "supervisor_eligible": False,
    }
    host._journal.append("external_acceptance_action_required", escalation)
    host._journal.append("human_intervention_required", escalation)
    host._emit_progress(
        "human_required", package_id=package.id, reason=escalation["blocked_requirement"]
    )
    host.transition_to(TaskExecutionState.HUMAN_REQUIRED)
    raise _StageEscalated(package.id)
