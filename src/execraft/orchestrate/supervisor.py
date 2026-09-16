"""Autonomous supervision for recoverable orchestration incidents.

The supervisor is separate from the package state machine. It interprets a durable
``human_intervention_required`` event, coordinates one privileged agent, optionally
delegates bounded specialist work, and returns control only after a deterministic
recovery contract is produced. It may modify configured repositories but never owns
branch changes, pushes, or commits; those remain orchestrator responsibilities so
recovery cannot bypass the transaction journal or verification/review checks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping

from execraft.persistence.atomic import atomic_write_json

from .models import WorkPackageStage
from .scheduler import AgentCapability


class SupervisorConfigError(ValueError):
    """Raised when a project supervisor policy is ambiguous or unsafe."""


class IncidentClass(str, Enum):
    WORKSPACE_SCOPE = "workspace_scope"
    EVIDENCE_MISMATCH = "evidence_mismatch"
    REVIEW_EXHAUSTED = "review_exhausted"
    COMMIT_TRANSACTION = "commit_transaction"
    TEST_FAILURE = "test_failure"
    STATE_INCONSISTENCY = "state_inconsistency"
    AGENT_FAILURE = "agent_failure"
    REQUIREMENT_AMBIGUITY = "requirement_ambiguity"
    EXTERNAL_DEPENDENCY = "external_dependency"
    UNKNOWN = "unknown"


class IncidentStatus(str, Enum):
    OPEN = "open"
    DIAGNOSING = "diagnosing"
    DELEGATING = "delegating"
    VERIFYING = "verifying"
    RESOLVED = "resolved"
    WAITING_FOR_HUMAN = "waiting_for_human"
    EXHAUSTED = "exhausted"


class SupervisorDecisionKind(str, Enum):
    RESOLVED = "resolved"
    DELEGATE = "delegate"
    ASK_HUMAN = "ask_human"
    BLOCKED = "blocked"


class HumanDecisionRisk(str, Enum):
    """Risk classification for one Supervisor-proposed operator option.

    The value is part of the structured-output contract.  It is intentionally
    coarse: autonomous selection is permitted only for ``routine`` options,
    while every ambiguous, destructive, product, or external choice remains
    held for human decision.
    """

    ROUTINE = "routine"
    DESTRUCTIVE = "destructive"
    PRODUCT = "product"
    EXTERNAL = "external"
    UNKNOWN = "unknown"


_ALLOWED_SUPERVISOR_ADAPTERS = frozenset(
    {"codex", "claude", "claude-code", "antigravity", "antigravity-cli"}
)
_ALLOWED_DELEGATION_CAPABILITIES = frozenset(
    {
        AgentCapability.IMPLEMENT,
        AgentCapability.REVIEW,
        AgentCapability.FIX_REVIEW,
    }
)
_LOCAL_PROMPT_TRANSPORT_MARKERS = (
    "argument list too long",
    "[errno 7]",
    "errno 7",
    "e2big",
)


def is_recoverable_prompt_transport_failure(value: object) -> bool:
    """Recognize the pre-stdin-launcher ARG_MAX failure from persisted incidents.

    This is intentionally narrow. Product ambiguity and model decisions must stay
    held for human decision; a local ``execve(2)`` size failure is infrastructure state that
    can be retried automatically once the launcher no longer embeds prompts in
    argv.
    """

    text = str(value or "").casefold()
    return any(marker in text for marker in _LOCAL_PROMPT_TRANSPORT_MARKERS)


_DEFAULT_TRIGGER_CLASSES = frozenset(
    {
        IncidentClass.WORKSPACE_SCOPE,
        IncidentClass.EVIDENCE_MISMATCH,
        IncidentClass.REVIEW_EXHAUSTED,
        IncidentClass.COMMIT_TRANSACTION,
        IncidentClass.TEST_FAILURE,
        IncidentClass.STATE_INCONSISTENCY,
        IncidentClass.REQUIREMENT_AMBIGUITY,
        IncidentClass.EXTERNAL_DEPENDENCY,
    }
)

_DEFAULT_AUTO_DECISION_CLASSES = frozenset(
    {
        IncidentClass.WORKSPACE_SCOPE,
        IncidentClass.EVIDENCE_MISMATCH,
        IncidentClass.REVIEW_EXHAUSTED,
        IncidentClass.COMMIT_TRANSACTION,
        IncidentClass.TEST_FAILURE,
        IncidentClass.STATE_INCONSISTENCY,
        IncidentClass.AGENT_FAILURE,
    }
)

_DESTRUCTIVE_OPTION_MARKERS = (
    "discard",
    "delete",
    "destroy",
    "drop",
    "erase",
    "force reset",
    "hard reset",
    "lose the",
    "losing the",
    "overwrite",
    "purge",
    "remove intentional",
    "restore all",
    "revert",
    "rewrite history",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _positive_int(
    mapping: Mapping[str, Any], key: str, default: int, *, maximum: int
) -> int:
    raw = mapping.get(key, default)
    if isinstance(raw, bool):
        raise SupervisorConfigError(f"supervisor.{key} must be an integer")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise SupervisorConfigError(f"supervisor.{key} must be an integer") from exc
    if not 1 <= value <= maximum:
        raise SupervisorConfigError(
            f"supervisor.{key} must be between 1 and {maximum}"
        )
    return value


def _bounded_int(
    mapping: Mapping[str, Any],
    key: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
    prefix: str = "supervisor",
) -> int:
    raw = mapping.get(key, default)
    if isinstance(raw, bool):
        raise SupervisorConfigError(f"{prefix}.{key} must be an integer")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise SupervisorConfigError(f"{prefix}.{key} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise SupervisorConfigError(
            f"{prefix}.{key} must be between {minimum} and {maximum}"
        )
    return value


def _boolean(
    mapping: Mapping[str, Any],
    key: str,
    default: bool,
    *,
    prefix: str = "supervisor",
) -> bool:
    value = mapping.get(key, default)
    if not isinstance(value, bool):
        raise SupervisorConfigError(f"{prefix}.{key} must be a boolean")
    return value


@dataclass(frozen=True)
class SupervisorAutoDecisionPolicy:
    """Fail-closed policy for automatically answering bounded questions."""

    enabled: bool = False
    minimum_weight: int = 70
    minimum_margin: int = 20
    max_per_incident: int = 2
    allowed_classes: frozenset[IncidentClass] = _DEFAULT_AUTO_DECISION_CLASSES

    @classmethod
    def from_mapping(cls, raw: object) -> "SupervisorAutoDecisionPolicy":
        if raw is None:
            return cls()
        if not isinstance(raw, Mapping):
            raise SupervisorConfigError("supervisor.auto_decision must be a YAML mapping")

        classes_raw = raw.get("allowed_classes")
        allowed_classes = _DEFAULT_AUTO_DECISION_CLASSES
        if classes_raw is not None:
            if not isinstance(classes_raw, list) or not classes_raw:
                raise SupervisorConfigError(
                    "supervisor.auto_decision.allowed_classes must be a non-empty list"
                )
            parsed: set[IncidentClass] = set()
            for item in classes_raw:
                try:
                    parsed.add(IncidentClass(str(item).strip()))
                except ValueError as exc:
                    raise SupervisorConfigError(
                        "unsupported supervisor auto-decision class: "
                        f"{item!r}"
                    ) from exc
            allowed_classes = frozenset(parsed)

        return cls(
            enabled=_boolean(
                raw,
                "enabled",
                False,
                prefix="supervisor.auto_decision",
            ),
            minimum_weight=_bounded_int(
                raw,
                "minimum_weight",
                70,
                minimum=1,
                maximum=100,
                prefix="supervisor.auto_decision",
            ),
            minimum_margin=_bounded_int(
                raw,
                "minimum_margin",
                20,
                minimum=0,
                maximum=100,
                prefix="supervisor.auto_decision",
            ),
            max_per_incident=_bounded_int(
                raw,
                "max_per_incident",
                2,
                minimum=1,
                maximum=8,
                prefix="supervisor.auto_decision",
            ),
            allowed_classes=allowed_classes,
        )

    def as_mapping(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "minimum_weight": self.minimum_weight,
            "minimum_margin": self.minimum_margin,
            "max_per_incident": self.max_per_incident,
            "allowed_classes": sorted(item.value for item in self.allowed_classes),
        }


def _text(
    value: object,
    *,
    label: str,
    maximum: int,
    required: bool = False,
) -> str:
    """Normalize one model/config string and enforce a durable size bound."""

    text = str(value or "").strip()
    if required and not text:
        raise ValueError(f"{label} is required")
    if len(text) > maximum:
        raise ValueError(f"{label} exceeds the {maximum}-character limit")
    return text


@dataclass(frozen=True)
class SupervisorPolicy:
    """Validated project-level autonomous supervision policy."""

    enabled: bool = True
    agent_id: str = ""
    agent_ids: tuple[str, ...] = ()
    skill_id: str = "ai-supervise"
    max_attempts_per_incident: int = 3
    max_contract_failures: int = 3
    max_provider_waits: int = 24
    max_agent_delegations: int = 6
    max_delegation_rounds: int = 2
    max_runtime_minutes: int = 60
    max_wall_clock_minutes: int = 24 * 60
    ask_human_when_uncertain: bool = True
    require_human_for_destructive_actions: bool = True
    auto_decision: SupervisorAutoDecisionPolicy = field(
        default_factory=SupervisorAutoDecisionPolicy
    )
    trigger_classes: frozenset[IncidentClass] = _DEFAULT_TRIGGER_CLASSES

    def __post_init__(self) -> None:
        """Canonicalize legacy single-agent construction into an ordered pool."""

        legacy = str(self.agent_id).strip()
        configured = tuple(str(item).strip() for item in self.agent_ids if str(item).strip())
        if configured:
            if len(configured) != len(set(configured)):
                raise SupervisorConfigError("supervisor.agents cannot contain duplicates")
            if legacy and legacy != configured[0]:
                raise SupervisorConfigError(
                    "supervisor.agent must match the first supervisor.agents entry"
                )
        elif legacy:
            configured = (legacy,)
        object.__setattr__(self, "agent_ids", configured)
        object.__setattr__(self, "agent_id", configured[0] if configured else "")

    @classmethod
    def from_mapping(cls, raw: object) -> "SupervisorPolicy":
        if raw is None:
            return cls()
        if not isinstance(raw, Mapping):
            raise SupervisorConfigError("supervisor must be a YAML mapping")
        legacy_agent = str(raw.get("agent", "")).strip()
        agents_raw = raw.get("agents")
        if legacy_agent and agents_raw is not None:
            raise SupervisorConfigError(
                "supervisor must declare either agent or agents, not both"
            )
        if agents_raw is None:
            agent_ids = (legacy_agent,) if legacy_agent else ()
        else:
            if (
                not isinstance(agents_raw, list)
                or not agents_raw
                or len(agents_raw) > 8
            ):
                raise SupervisorConfigError(
                    "supervisor.agents must be a non-empty list of at most 8 providers"
                )
            agent_ids = tuple(str(item).strip() for item in agents_raw)
            if any(not item for item in agent_ids):
                raise SupervisorConfigError(
                    "supervisor.agents cannot contain empty provider IDs"
                )
            if len(agent_ids) != len(set(agent_ids)):
                raise SupervisorConfigError(
                    "supervisor.agents cannot contain duplicates"
                )
        skill_id = str(raw.get("skill", "ai-supervise")).strip()
        if not skill_id:
            raise SupervisorConfigError("supervisor.skill cannot be empty")

        trigger_raw = raw.get("trigger_classes")
        trigger_classes = _DEFAULT_TRIGGER_CLASSES
        if trigger_raw is not None:
            if not isinstance(trigger_raw, list) or not trigger_raw:
                raise SupervisorConfigError(
                    "supervisor.trigger_classes must be a non-empty list"
                )
            parsed: set[IncidentClass] = set()
            for item in trigger_raw:
                try:
                    parsed.add(IncidentClass(str(item).strip()))
                except ValueError as exc:
                    raise SupervisorConfigError(
                        f"unsupported supervisor trigger class: {item!r}"
                    ) from exc
            trigger_classes = frozenset(parsed)

        return cls(
            enabled=_boolean(raw, "enabled", True),
            agent_id=agent_ids[0] if agent_ids else "",
            agent_ids=agent_ids,
            skill_id=skill_id,
            max_attempts_per_incident=_positive_int(
                raw, "max_attempts_per_incident", 3, maximum=12
            ),
            max_contract_failures=_positive_int(
                raw, "max_contract_failures", 3, maximum=32
            ),
            max_provider_waits=_positive_int(
                raw, "max_provider_waits", 24, maximum=1000
            ),
            max_agent_delegations=_positive_int(
                raw, "max_agent_delegations", 6, maximum=32
            ),
            max_delegation_rounds=_positive_int(
                raw, "max_delegation_rounds", 2, maximum=6
            ),
            max_runtime_minutes=_positive_int(
                raw, "max_runtime_minutes", 60, maximum=24 * 60
            ),
            max_wall_clock_minutes=_positive_int(
                raw, "max_wall_clock_minutes", 24 * 60, maximum=30 * 24 * 60
            ),
            ask_human_when_uncertain=_boolean(
                raw, "ask_human_when_uncertain", True
            ),
            require_human_for_destructive_actions=_boolean(
                raw, "require_human_for_destructive_actions", True
            ),
            auto_decision=SupervisorAutoDecisionPolicy.from_mapping(
                raw.get("auto_decision")
            ),
            trigger_classes=trigger_classes,
        )

    def as_mapping(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "agents": list(self.agent_ids),
            "skill": self.skill_id,
            "max_attempts_per_incident": self.max_attempts_per_incident,
            "max_contract_failures": self.max_contract_failures,
            "max_provider_waits": self.max_provider_waits,
            "max_agent_delegations": self.max_agent_delegations,
            "max_delegation_rounds": self.max_delegation_rounds,
            "max_runtime_minutes": self.max_runtime_minutes,
            "max_wall_clock_minutes": self.max_wall_clock_minutes,
            "ask_human_when_uncertain": self.ask_human_when_uncertain,
            "require_human_for_destructive_actions": (
                self.require_human_for_destructive_actions
            ),
            "auto_decision": self.auto_decision.as_mapping(),
            "trigger_classes": sorted(item.value for item in self.trigger_classes),
        }


@dataclass(frozen=True)
class HumanDecisionOption:
    id: str
    label: str
    consequence: str = ""
    weight: int | None = None
    risk: HumanDecisionRisk = HumanDecisionRisk.UNKNOWN

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "HumanDecisionOption":
        option_id = _text(
            raw.get("id", ""),
            label="human decision option id",
            maximum=64,
            required=True,
        )
        label = _text(
            raw.get("label", ""),
            label=f"human decision option {option_id!r} label",
            maximum=300,
            required=True,
        )
        if not option_id or not option_id.replace("-", "_").isidentifier():
            raise ValueError(f"invalid human decision option id: {option_id!r}")
        if option_id == "stop":
            raise ValueError("human decision option id 'stop' is reserved")
        weight: int | None = None
        if "weight" in raw and raw.get("weight") is not None:
            value = raw.get("weight")
            if isinstance(value, bool):
                raise ValueError(
                    f"human decision option {option_id!r} weight must be an integer"
                )
            try:
                weight = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"human decision option {option_id!r} weight must be an integer"
                ) from exc
            if not 0 <= weight <= 100:
                raise ValueError(
                    f"human decision option {option_id!r} weight must be between 0 and 100"
                )
        try:
            risk = HumanDecisionRisk(str(raw.get("risk", "unknown")).strip())
        except ValueError as exc:
            raise ValueError(
                f"unsupported human decision option risk: {raw.get('risk')!r}"
            ) from exc
        return cls(
            id=option_id,
            label=label,
            consequence=_text(
                raw.get("consequence", ""),
                label=f"human decision option {option_id!r} consequence",
                maximum=1200,
            ),
            weight=weight,
            risk=risk,
        )

    def as_mapping(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "consequence": self.consequence,
            "weight": self.weight,
            "risk": self.risk.value,
        }


@dataclass(frozen=True)
class HumanDecisionRequest:
    question: str
    context: str
    options: tuple[HumanDecisionOption, ...]
    recommended_option: str = ""

    @classmethod
    def from_mapping(cls, raw: object) -> "HumanDecisionRequest":
        if not isinstance(raw, Mapping):
            raise ValueError("human_question must be an object")
        question = _text(
            raw.get("question", ""),
            label="human_question.question",
            maximum=2000,
            required=True,
        )
        context = _text(
            raw.get("context", ""),
            label="human_question.context",
            maximum=8000,
        )
        options_raw = raw.get("options", [])
        if not isinstance(options_raw, list) or not 2 <= len(options_raw) <= 6:
            raise ValueError("human_question.options must contain 2 to 6 options")
        options = tuple(
            HumanDecisionOption.from_mapping(item)
            for item in options_raw
            if isinstance(item, Mapping)
        )
        if len(options) != len(options_raw):
            raise ValueError("every human_question option must be an object")
        option_ids = [item.id for item in options]
        if len(option_ids) != len(set(option_ids)):
            raise ValueError("human_question option IDs must be unique")
        recommended = str(raw.get("recommended_option", "")).strip()
        if recommended and recommended not in option_ids:
            raise ValueError("recommended_option must reference a declared option")
        supplied_weights = [item.weight for item in options if item.weight is not None]
        if supplied_weights:
            if len(supplied_weights) != len(options):
                raise ValueError(
                    "human_question options must either all declare weight or all omit it"
                )
            if sum(supplied_weights) != 100:
                raise ValueError("human_question option weights must sum to 100")
            if recommended:
                by_id = {item.id: item for item in options}
                highest = max(supplied_weights)
                if by_id[recommended].weight != highest:
                    raise ValueError(
                        "recommended_option must reference a highest-weight option"
                    )
        return cls(
            question=question,
            context=context,
            options=options,
            recommended_option=recommended,
        )

    def as_mapping(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "context": self.context,
            "options": [item.as_mapping() for item in self.options],
            "recommended_option": self.recommended_option,
        }


@dataclass(frozen=True)
class SupervisorAutoDecisionEvaluation:
    """Result of applying the deterministic auto-decision safety policy."""

    eligible: bool
    reason: str
    option: HumanDecisionOption | None = None
    runner_up_weight: int = 0

    @property
    def margin(self) -> int:
        if self.option is None or self.option.weight is None:
            return 0
        return self.option.weight - self.runner_up_weight


def evaluate_supervisor_auto_decision(
    question: HumanDecisionRequest,
    *,
    classification: IncidentClass,
    policy: SupervisorAutoDecisionPolicy,
    decisions_taken: int,
    require_human_for_destructive_actions: bool,
) -> SupervisorAutoDecisionEvaluation:
    """Return the uniquely eligible routine option, otherwise a refusal reason.

    Model-provided weights are only one input.  The deterministic check also
    requires an allowed incident class, a unique winner, a configured margin,
    an explicit routine-risk label, a bounded per-incident budget, and a final
    destructive-language check.  Missing legacy weights therefore fail closed.
    """

    if not policy.enabled:
        return SupervisorAutoDecisionEvaluation(False, "auto-decision is disabled")
    if classification not in policy.allowed_classes:
        return SupervisorAutoDecisionEvaluation(
            False,
            f"incident class {classification.value!r} is not auto-decision eligible",
        )
    if decisions_taken >= policy.max_per_incident:
        return SupervisorAutoDecisionEvaluation(
            False,
            "the per-incident auto-decision budget is exhausted",
        )
    if not question.recommended_option:
        return SupervisorAutoDecisionEvaluation(False, "no option is recommended")
    if any(item.weight is None for item in question.options):
        return SupervisorAutoDecisionEvaluation(
            False,
            "one or more options have no normalized weight",
        )

    ranked = sorted(
        question.options,
        key=lambda item: int(item.weight or 0),
        reverse=True,
    )
    winner = ranked[0]
    runner_up_weight = int(ranked[1].weight or 0) if len(ranked) > 1 else 0
    highest = int(winner.weight or 0)
    tied = [item for item in ranked if int(item.weight or 0) == highest]
    if len(tied) != 1:
        return SupervisorAutoDecisionEvaluation(False, "the highest weight is tied")
    if winner.id != question.recommended_option:
        return SupervisorAutoDecisionEvaluation(
            False,
            "the recommendation is not the unique highest-weight option",
        )
    if highest < policy.minimum_weight:
        return SupervisorAutoDecisionEvaluation(
            False,
            f"top weight {highest} is below minimum {policy.minimum_weight}",
            winner,
            runner_up_weight,
        )
    margin = highest - runner_up_weight
    if margin < policy.minimum_margin:
        return SupervisorAutoDecisionEvaluation(
            False,
            f"top-option margin {margin} is below minimum {policy.minimum_margin}",
            winner,
            runner_up_weight,
        )
    if winner.id == "stop":
        return SupervisorAutoDecisionEvaluation(
            False, "the reserved stop option is never selected automatically"
        )
    if winner.risk != HumanDecisionRisk.ROUTINE:
        return SupervisorAutoDecisionEvaluation(
            False,
            f"recommended option risk is {winner.risk.value!r}, not 'routine'",
            winner,
            runner_up_weight,
        )
    if require_human_for_destructive_actions:
        selected_text = " ".join(
            (winner.id, winner.label, winner.consequence)
        ).casefold()
        if any(marker in selected_text for marker in _DESTRUCTIVE_OPTION_MARKERS):
            return SupervisorAutoDecisionEvaluation(
                False,
                "recommended option contains destructive-action language",
                winner,
                runner_up_weight,
            )
    return SupervisorAutoDecisionEvaluation(
        True,
        "unique routine option satisfies weight and margin thresholds",
        winner,
        runner_up_weight,
    )


@dataclass(frozen=True)
class SupervisorDelegation:
    task: str
    capability: AgentCapability
    agent_id: str = ""
    skill_ids: tuple[str, ...] = ()
    read_only: bool = False

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "SupervisorDelegation":
        task = _text(
            raw.get("task", ""),
            label="delegation.task",
            maximum=8000,
            required=True,
        )
        try:
            capability = AgentCapability(str(raw.get("capability", "")).strip())
        except ValueError as exc:
            raise ValueError(
                f"unsupported delegation capability: {raw.get('capability')!r}"
            ) from exc
        if capability not in _ALLOWED_DELEGATION_CAPABILITIES:
            raise ValueError(
                f"supervisor cannot delegate capability {capability.value!r}"
            )
        skills_raw = raw.get("skills", [])
        if not isinstance(skills_raw, list):
            raise ValueError("delegation.skills must be a list")
        if len(skills_raw) > 16:
            raise ValueError("delegation.skills cannot contain more than 16 entries")
        skill_ids: list[str] = []
        for item in skills_raw:
            skill_id = str(item).strip()
            if skill_id and skill_id not in skill_ids:
                skill_ids.append(skill_id)
        read_only = raw.get("read_only", capability == AgentCapability.REVIEW)
        if not isinstance(read_only, bool):
            raise ValueError("delegation.read_only must be a boolean")
        if capability == AgentCapability.REVIEW and not read_only:
            raise ValueError("review delegations must be read_only")
        return cls(
            task=task,
            capability=capability,
            agent_id=str(raw.get("agent_id", "")).strip(),
            skill_ids=tuple(skill_ids),
            read_only=read_only,
        )

    def as_mapping(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "capability": self.capability.value,
            "agent_id": self.agent_id,
            "skills": list(self.skill_ids),
            "read_only": self.read_only,
        }


def unfinished_supervisor_delegations(
    entries: Iterable[Any], incident_id: str
) -> list[dict[str, Any]]:
    """Reconstruct started-but-unfinished work after the latest delegate decision.

    Older orchestrator builds persisted delegation journal events but held the
    executable queue only on the Python stack. A provider wait or restart could
    therefore lose the task and produce a spurious operator question. Journal
    recovery deliberately clears ``agent_id``: the provider recorded by a
    started-without-finish event already failed to complete, so the normal
    capability scheduler must select the next healthy candidate.
    """

    incident_id = str(incident_id).strip()
    if not incident_id:
        return []
    rows = list(entries)
    decision_sequence = -1
    for entry in rows:
        payload = getattr(entry, "payload", {}) or {}
        if (
            getattr(entry, "event_type", "") == "supervisor_decision"
            and str(payload.get("incident_id", "")) == incident_id
            and str(payload.get("decision", "")) == "delegate"
        ):
            decision_sequence = int(getattr(entry, "sequence", -1))
    if decision_sequence < 0:
        return []

    started: dict[int, dict[str, Any]] = {}
    finished: set[int] = set()
    for entry in rows:
        if int(getattr(entry, "sequence", -1)) <= decision_sequence:
            continue
        payload = getattr(entry, "payload", {}) or {}
        if str(payload.get("incident_id", "")) != incident_id:
            continue
        index = int(payload.get("index", 0) or 0)
        if index <= 0:
            continue
        event_type = getattr(entry, "event_type", "")
        if event_type == "supervisor_delegation_started":
            candidate = {
                "task": str(payload.get("task", "")),
                "capability": str(payload.get("capability", "")),
                "agent_id": "",
                "skills": list(payload.get("skills", []) or []),
                "read_only": bool(payload.get("read_only", False)),
            }
            try:
                SupervisorDelegation.from_mapping(candidate)
            except ValueError:
                continue
            started[index] = candidate
        elif event_type == "supervisor_delegation_finished":
            finished.add(index)
    return [started[index] for index in sorted(started) if index not in finished]


@dataclass(frozen=True)
class SupervisorDecision:
    decision: SupervisorDecisionKind
    classification: IncidentClass
    summary: str
    actions_taken: tuple[str, ...] = ()
    resume_stage: WorkPackageStage | None = None
    delegations: tuple[SupervisorDelegation, ...] = ()
    human_question: HumanDecisionRequest | None = None
    acceptance_evidence: Mapping[str, str] = field(default_factory=dict)
    implementation_summary: str = ""
    retain_paths: tuple[str, ...] = ()
    discard_paths: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "SupervisorDecision":
        if raw.get("ok") is not True:
            raise ValueError("supervisor decision must set ok=true")
        try:
            decision = SupervisorDecisionKind(str(raw.get("decision", "")).strip())
        except ValueError as exc:
            raise ValueError(
                "supervisor decision must be resolved, delegate, ask_human, or blocked"
            ) from exc
        try:
            classification = IncidentClass(
                str(raw.get("classification", IncidentClass.UNKNOWN.value)).strip()
            )
        except ValueError as exc:
            raise ValueError(
                f"unsupported supervisor classification: {raw.get('classification')!r}"
            ) from exc
        summary = _text(
            raw.get("summary", ""),
            label="supervisor summary",
            maximum=12000,
            required=True,
        )
        actions_raw = raw.get("actions_taken", [])
        if not isinstance(actions_raw, list):
            raise ValueError("actions_taken must be a list")
        if len(actions_raw) > 32:
            raise ValueError("actions_taken cannot contain more than 32 entries")
        actions = tuple(
            _text(
                item,
                label="actions_taken item",
                maximum=4000,
                required=True,
            )
            for item in actions_raw
        )

        resume_stage = None
        resume_raw = str(raw.get("resume_stage", "")).strip()
        if resume_raw:
            try:
                resume_stage = WorkPackageStage(resume_raw)
            except ValueError as exc:
                raise ValueError(f"unsupported resume_stage: {resume_raw!r}") from exc
            if resume_stage == WorkPackageStage.COMPLETED:
                raise ValueError("supervisor cannot mark a package completed directly")

        delegations_raw = raw.get("delegations", [])
        if not isinstance(delegations_raw, list):
            raise ValueError("delegations must be a list")
        if len(delegations_raw) > 32:
            raise ValueError("delegations cannot contain more than 32 entries")
        delegations = tuple(
            SupervisorDelegation.from_mapping(item)
            for item in delegations_raw
            if isinstance(item, Mapping)
        )
        if len(delegations) != len(delegations_raw):
            raise ValueError("every delegation must be an object")

        question = None
        question_raw = raw.get("human_question")
        if (
            isinstance(question_raw, Mapping)
            and str(question_raw.get("question", "")).strip()
        ):
            question = HumanDecisionRequest.from_mapping(question_raw)

        evidence_raw = raw.get("acceptance_evidence", {})
        if isinstance(evidence_raw, Mapping):
            evidence_items = list(evidence_raw.items())
        elif isinstance(evidence_raw, list):
            if not all(isinstance(item, Mapping) for item in evidence_raw):
                raise ValueError(
                    "every acceptance_evidence entry must be an object"
                )
            evidence_items = [
                (item.get("criterion_id", ""), item.get("evidence", ""))
                for item in evidence_raw
            ]
        else:
            raise ValueError("acceptance_evidence must be an object or list")
        evidence: dict[str, str] = {}
        for key, value in evidence_items:
            criterion_id = _text(
                key,
                label="acceptance criterion id",
                maximum=256,
                required=True,
            )
            if criterion_id in evidence:
                raise ValueError(
                    f"duplicate acceptance criterion id: {criterion_id!r}"
                )
            evidence[criterion_id] = _text(
                value,
                label="acceptance evidence",
                maximum=8000,
                required=True,
            )
        if len(evidence) > 128:
            raise ValueError("acceptance_evidence exceeds 128 entries")
        implementation_summary = _text(
            raw.get("implementation_summary", ""),
            label="implementation_summary",
            maximum=12000,
        )

        def qualified_paths(key: str) -> tuple[str, ...]:
            values = raw.get(key, [])
            if not isinstance(values, list):
                raise ValueError(f"{key} must be a list")
            if len(values) > 512:
                raise ValueError(f"{key} cannot contain more than 512 entries")
            normalized: list[str] = []
            for item in values:
                value = str(item).strip()
                repository_id, separator, relative_path = value.partition(":")
                if (
                    not separator
                    or not repository_id
                    or not relative_path
                    or relative_path.startswith("/")
                    or len(value) > 4096
                    or value in normalized
                ):
                    if value in normalized:
                        continue
                    raise ValueError(
                        f"{key} contains an invalid repository-qualified path: {value!r}"
                    )
                normalized.append(value)
            return tuple(normalized)

        retain_paths = qualified_paths("retain_paths")
        discard_paths = qualified_paths("discard_paths")
        overlap = sorted(set(retain_paths) & set(discard_paths))
        if overlap:
            raise ValueError(
                "supervisor cannot both retain and discard paths: "
                + ", ".join(overlap)
            )

        if decision == SupervisorDecisionKind.DELEGATE and not delegations:
            raise ValueError("delegate decision requires at least one delegation")
        if decision == SupervisorDecisionKind.ASK_HUMAN and question is None:
            raise ValueError("ask_human decision requires human_question")
        if decision != SupervisorDecisionKind.ASK_HUMAN and question is not None:
            raise ValueError("human_question is valid only with ask_human")
        if decision != SupervisorDecisionKind.DELEGATE and delegations:
            raise ValueError("delegations are valid only with delegate")
        if decision != SupervisorDecisionKind.RESOLVED and (
            retain_paths or discard_paths
        ):
            raise ValueError(
                "retain_paths and discard_paths are valid only with resolved"
            )

        return cls(
            decision=decision,
            classification=classification,
            summary=summary,
            actions_taken=actions,
            resume_stage=resume_stage,
            delegations=delegations,
            human_question=question,
            acceptance_evidence=evidence,
            implementation_summary=implementation_summary,
            retain_paths=retain_paths,
            discard_paths=discard_paths,
        )


@dataclass
class SupervisorIncident:
    incident_id: str
    # Event-specific fingerprint. Includes the journal sequence and is retained
    # for exact audit/history correlation. Do not use it as the bounded retry
    # campaign identity: equivalent escalations can be re-emitted under a new
    # sequence during restart/reconciliation.
    fingerprint: str
    package_id: str
    stage: str
    classification: IncidentClass
    # Sequence-independent semantic identity for the escalation that owns this
    # retry budget. Legacy incident records may omit it and are hydrated by the
    # orchestrator from their original journal action.
    escalation_key: str = ""
    status: IncidentStatus = IncidentStatus.OPEN
    supervisor_agent_id: str = ""
    attempts: int = 0
    contract_failures: int = 0
    provider_waits: int = 0
    delegation_rounds: int = 0
    delegated_tasks: int = 0
    auto_decisions: int = 0
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)
    # Runtime budgets measure time spent inside active supervision attempts, not
    # wall-clock incident age. Provider cooldowns, daemon downtime, and operator
    # think time must not silently consume the recovery budget.
    active_runtime_seconds: float = 0.0
    summary: str = ""
    actions_taken: list[str] = field(default_factory=list)
    delegation_results: list[dict[str, Any]] = field(default_factory=list)
    # Delegations are durable work, not an in-memory loop. Persist the queue
    # and cursor so a provider cooldown or daemon restart resumes the exact
    # delegated task instead of asking the Supervisor to plan it again.
    pending_delegations: list[dict[str, Any]] = field(default_factory=list)
    pending_delegation_index: int = 0
    candidate_paths: list[str] = field(default_factory=list)
    human_question: dict[str, Any] = field(default_factory=dict)
    human_answer: dict[str, str] = field(default_factory=dict)
    workspace_digest_before: str = ""
    workspace_digest_after: str = ""
    escalation_sequence: int | None = None

    def touch(self, *, status: IncidentStatus | None = None) -> None:
        if status is not None:
            self.status = status
        self.updated_at = _utc_now()

    def elapsed_seconds(self, *, now: datetime | None = None) -> float:
        """Return durable incident age, tolerating legacy timestamp formats."""

        try:
            created = datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
        except ValueError:
            return 0.0
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        answered_at = str(self.human_answer.get("answered_at", "")).strip()
        if answered_at:
            try:
                answered = datetime.fromisoformat(
                    answered_at.replace("Z", "+00:00")
                )
                if answered.tzinfo is None:
                    answered = answered.replace(tzinfo=timezone.utc)
                created = max(created, answered)
            except ValueError:
                pass
        current = now or datetime.now(timezone.utc)
        return max(0.0, (current - created).total_seconds())

    def record_active_runtime(self, seconds: float) -> None:
        """Accumulate one bounded supervision execution interval."""

        try:
            value = float(seconds)
        except (TypeError, ValueError):
            return
        if value > 0:
            self.active_runtime_seconds = max(0.0, self.active_runtime_seconds) + value

    def reset_runtime_budget(self) -> None:
        """Grant a fresh bounded runtime window after an explicit resume."""

        self.active_runtime_seconds = 0.0

    def runtime_exhausted(
        self,
        policy: SupervisorPolicy,
        *,
        additional_seconds: float = 0.0,
    ) -> bool:
        consumed = max(0.0, self.active_runtime_seconds) + max(
            0.0, float(additional_seconds)
        )
        return consumed >= policy.max_runtime_minutes * 60

    def as_mapping(self) -> dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "fingerprint": self.fingerprint,
            "escalation_key": self.escalation_key,
            "package_id": self.package_id,
            "stage": self.stage,
            "classification": self.classification.value,
            "status": self.status.value,
            "supervisor_agent_id": self.supervisor_agent_id,
            "attempts": self.attempts,
            "contract_failures": self.contract_failures,
            "provider_waits": self.provider_waits,
            "delegation_rounds": self.delegation_rounds,
            "delegated_tasks": self.delegated_tasks,
            "auto_decisions": self.auto_decisions,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "elapsed_seconds": round(self.elapsed_seconds(), 3),
            "active_runtime_seconds": round(
                max(0.0, self.active_runtime_seconds), 3
            ),
            "summary": self.summary,
            "actions_taken": list(self.actions_taken),
            "delegation_results": [dict(item) for item in self.delegation_results],
            "pending_delegations": [dict(item) for item in self.pending_delegations],
            "pending_delegation_index": self.pending_delegation_index,
            "candidate_paths": list(self.candidate_paths),
            "human_question": dict(self.human_question),
            "human_answer": dict(self.human_answer),
            "workspace_digest_before": self.workspace_digest_before,
            "workspace_digest_after": self.workspace_digest_after,
            "escalation_sequence": self.escalation_sequence,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "SupervisorIncident":
        return cls(
            incident_id=str(raw.get("incident_id", "")),
            fingerprint=str(raw.get("fingerprint", "")),
            escalation_key=str(raw.get("escalation_key", "")),
            package_id=str(raw.get("package_id", "")),
            stage=str(raw.get("stage", "")),
            classification=IncidentClass(
                str(raw.get("classification", IncidentClass.UNKNOWN.value))
            ),
            status=IncidentStatus(str(raw.get("status", IncidentStatus.OPEN.value))),
            supervisor_agent_id=str(raw.get("supervisor_agent_id", "")),
            attempts=int(raw.get("attempts", 0)),
            contract_failures=max(0, int(raw.get("contract_failures", 0))),
            provider_waits=max(0, int(raw.get("provider_waits", 0))),
            delegation_rounds=int(raw.get("delegation_rounds", 0)),
            delegated_tasks=int(raw.get("delegated_tasks", 0)),
            auto_decisions=max(0, int(raw.get("auto_decisions", 0))),
            created_at=str(raw.get("created_at", "")) or _utc_now(),
            updated_at=str(raw.get("updated_at", "")) or _utc_now(),
            # Legacy incidents did not distinguish active runtime from age.
            # Migrating them with a zero active budget is intentionally
            # progress-safe: stale waits can resume, while new builds persist
            # every consumed execution interval durably.
            active_runtime_seconds=max(
                0.0, float(raw.get("active_runtime_seconds", 0.0) or 0.0)
            ),
            summary=str(raw.get("summary", "")),
            actions_taken=[str(item) for item in raw.get("actions_taken", [])],
            delegation_results=[
                dict(item)
                for item in raw.get("delegation_results", [])
                if isinstance(item, Mapping)
            ],
            pending_delegations=[
                dict(item)
                for item in raw.get("pending_delegations", [])
                if isinstance(item, Mapping)
            ],
            pending_delegation_index=max(
                0, int(raw.get("pending_delegation_index", 0))
            ),
            candidate_paths=bounded_strings(
                raw.get("candidate_paths", []), limit=512
            ),
            human_question=dict(raw.get("human_question") or {}),
            human_answer={
                str(key): str(value)
                for key, value in (raw.get("human_answer") or {}).items()
            },
            workspace_digest_before=str(raw.get("workspace_digest_before", "")),
            workspace_digest_after=str(raw.get("workspace_digest_after", "")),
            escalation_sequence=(
                int(raw["escalation_sequence"])
                if raw.get("escalation_sequence") is not None
                else None
            ),
        )


class SupervisorIncidentStore:
    """Atomic append-preserving incident store used by CLI, daemon and GUI."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()

    def list(self) -> list[SupervisorIncident]:
        if not self.path.is_file():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"corrupt supervisor incident store: {self.path}: {exc}") from exc
        if not isinstance(raw, list):
            raise ValueError(f"supervisor incident store must be a list: {self.path}")
        return [
            SupervisorIncident.from_mapping(item)
            for item in raw
            if isinstance(item, Mapping)
        ]

    def active(self) -> SupervisorIncident | None:
        for incident in reversed(self.list()):
            if incident.status not in {
                IncidentStatus.RESOLVED,
                IncidentStatus.EXHAUSTED,
            }:
                return incident
        return None

    def recent(self, limit: int = 5) -> list[SupervisorIncident]:
        return list(reversed(self.list()))[: max(0, int(limit))]

    def latest_for_fingerprint(self, fingerprint: str) -> SupervisorIncident | None:
        """Return the newest durable incident for one escalation fingerprint."""

        expected = str(fingerprint).strip()
        if not expected:
            return None
        return next(
            (item for item in reversed(self.list()) if item.fingerprint == expected),
            None,
        )

    def latest_exhausted_for_fingerprint(
        self,
        fingerprint: str,
        *,
        exclude_incident_id: str = "",
    ) -> SupervisorIncident | None:
        """Return the newest exhausted campaign for the same escalation.

        Terminal incidents are intentionally excluded from :meth:`active`, but
        callers still need to know that the exact same escalation already spent
        its bounded recovery budget. The exclusion supports migration of legacy
        duplicate incidents that were accidentally opened after exhaustion.
        """

        expected = str(fingerprint).strip()
        excluded = str(exclude_incident_id).strip()
        if not expected:
            return None
        return next(
            (
                item
                for item in reversed(self.list())
                if item.fingerprint == expected
                and item.status == IncidentStatus.EXHAUSTED
                and item.incident_id != excluded
            ),
            None,
        )

    def latest_exhausted_for_escalation_key(
        self,
        escalation_key: str,
        *,
        exclude_incident_id: str = "",
    ) -> SupervisorIncident | None:
        """Return the newest exhausted campaign for one semantic escalation."""

        expected = str(escalation_key).strip()
        excluded = str(exclude_incident_id).strip()
        if not expected:
            return None
        return next(
            (
                item
                for item in reversed(self.list())
                if item.escalation_key == expected
                and item.status == IncidentStatus.EXHAUSTED
                and item.incident_id != excluded
            ),
            None,
        )

    def get(self, incident_id: str) -> SupervisorIncident | None:
        return next(
            (item for item in self.list() if item.incident_id == incident_id),
            None,
        )

    def save(self, incident: SupervisorIncident) -> None:
        incidents = self.list()
        replaced = False
        for index, current in enumerate(incidents):
            if current.incident_id == incident.incident_id:
                incidents[index] = incident
                replaced = True
                break
        if not replaced:
            incidents.append(incident)
        atomic_write_json(
            self.path,
            [item.as_mapping() for item in incidents],
            indent=2,
            ensure_ascii=False,
        )

    def open_or_reuse(
        self,
        *,
        fingerprint: str,
        escalation_key: str = "",
        package_id: str,
        stage: str,
        classification: IncidentClass,
        escalation_sequence: int | None,
        supervisor_agent_id: str,
        workspace_digest: str,
        candidate_paths: Iterable[str] = (),
    ) -> SupervisorIncident:
        active = self.active()
        same_campaign = bool(
            active is not None
            and (
                active.fingerprint == fingerprint
                or (
                    escalation_key
                    and active.escalation_key
                    and active.escalation_key == escalation_key
                )
            )
        )
        if active is not None and same_campaign:
            active.supervisor_agent_id = supervisor_agent_id or active.supervisor_agent_id
            active.escalation_key = escalation_key or active.escalation_key
            # Workspace changes do not reset an incident-wide reasoning budget.
            # A failed Supervisor resolution can itself leave useful edits behind;
            # resetting here made each retry appear as attempt 1 and allowed an
            # unbounded edit/reject/relaunch loop. Explicit operator answers and
            # narrowly-scoped transport migrations reset budgets at their own
            # call sites when a genuinely fresh campaign is authorized.
            active.workspace_digest_after = workspace_digest
            active.candidate_paths = list(
                dict.fromkeys(
                    [*active.candidate_paths, *bounded_strings(candidate_paths, limit=512)]
                )
            )[:512]
            active.touch()
            self.save(active)
            return active
        if active is not None:
            active.summary = (
                active.summary
                or "Superseded by a newer human-required escalation."
            )
            active.touch(status=IncidentStatus.EXHAUSTED)
            self.save(active)
        incident_id = hashlib.sha256(
            f"{fingerprint}/{_utc_now()}/{uuid.uuid4()}".encode("utf-8")
        ).hexdigest()[:16]
        incident = SupervisorIncident(
            incident_id=incident_id,
            fingerprint=fingerprint,
            escalation_key=escalation_key,
            package_id=package_id,
            stage=stage,
            classification=classification,
            supervisor_agent_id=supervisor_agent_id,
            escalation_sequence=escalation_sequence,
            workspace_digest_before=workspace_digest,
            workspace_digest_after=workspace_digest,
            candidate_paths=bounded_strings(candidate_paths, limit=512),
        )
        self.save(incident)
        return incident


def _action_evidence(action: Mapping[str, Any]) -> list[str]:
    raw = action.get("evidence", [])
    if isinstance(raw, (str, bytes)):
        return [str(raw)]
    if isinstance(raw, Iterable):
        return [str(item) for item in raw]
    return [str(raw)] if raw is not None else []


def classify_incident(action: Mapping[str, Any] | None) -> IncidentClass:
    if not action:
        return IncidentClass.UNKNOWN
    stage = str(action.get("stage", "")).lower()
    reason = str(action.get("reason", "")).lower()
    evidence = " ".join(_action_evidence(action)).lower()
    combined = f"{stage} {reason} {evidence}"
    if "review/fix cycle budget exhausted" in reason:
        return IncidentClass.REVIEW_EXHAUSTED
    if "commit" in stage or "commit transaction" in reason:
        return IncidentClass.COMMIT_TRANSACTION
    if any(
        token in f"{stage} {reason}"
        for token in ("scope", "clean-start", "write_scope", "dirty")
    ):
        return IncidentClass.WORKSPACE_SCOPE
    if any(
        token in combined
        for token in ("evidence", "handoff", "implementation_summary")
    ):
        return IncidentClass.EVIDENCE_MISMATCH
    if any(token in combined for token in ("test", "verification", "build failed")):
        return IncidentClass.TEST_FAILURE
    if any(token in combined for token in ("state", "journal", "stale", "inconsistent")):
        return IncidentClass.STATE_INCONSISTENCY
    if any(token in combined for token in ("agent", "provider", "invalid structured output")):
        return IncidentClass.AGENT_FAILURE
    if any(token in combined for token in ("ambiguous", "product decision", "requirement")):
        return IncidentClass.REQUIREMENT_AMBIGUITY
    if any(token in combined for token in ("credential", "hardware", "external", "network")):
        return IncidentClass.EXTERNAL_DEPENDENCY
    return IncidentClass.UNKNOWN


def _normalized_incident_text(value: Any) -> str:
    """Canonicalize durable escalation text without erasing material content."""

    return " ".join(str(value or "").split())


def incident_escalation_key(action: Mapping[str, Any]) -> str:
    """Return a sequence-independent identity for one retry-budget campaign.

    ``incident_fingerprint`` intentionally includes the journal sequence so it
    can identify the exact escalation event in audit history. Retry budgets need
    a different identity: the same human-required condition may be emitted again
    while a daemon restarts or reconciles stale state. Merely receiving a new
    journal sequence must never replenish a bounded Supervisor budget.

    The semantic key therefore uses stable escalation content and deliberately
    excludes sequence/timestamp metadata. Evidence is normalized, deduplicated,
    and sorted so harmless ordering/whitespace differences do not create a new
    campaign; materially different evidence or reason still does.
    """

    reason = action.get("blocked_requirement") or action.get("reason", "")
    evidence = sorted(
        {
            text
            for item in _action_evidence(action)
            if (text := _normalized_incident_text(item))
        }
    )
    payload = {
        "package_id": _normalized_incident_text(action.get("package_id", "")),
        "stage": _normalized_incident_text(action.get("stage", "")).lower(),
        "classification": classify_incident(action).value,
        "reason": _normalized_incident_text(reason),
        "evidence": evidence,
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def incident_fingerprint(action: Mapping[str, Any]) -> str:
    payload = {
        "sequence": action.get("sequence"),
        "package_id": str(action.get("package_id", "")),
        "stage": str(action.get("stage", "")),
        "reason": str(action.get("reason", "")),
        "evidence": _action_evidence(action),
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def supervisor_output_schema() -> dict[str, Any]:
    """Return the optional, permissive Supervisor decision envelope.

    Runtime supervision no longer sends this schema to providers.  It remains a
    public reference for integrations that want machine-readable output while
    allowing arbitrary advisory fields.  Only the semantic decision and summary
    are fundamental; Execraft compiles optional details locally and validates
    execution effects separately.
    """

    return {
        "type": "object",
        "additionalProperties": True,
        "required": ["decision", "summary"],
        "properties": {
            "decision": {"type": "string", "minLength": 1},
            "summary": {"type": "string", "minLength": 1},
        },
    }

def provider_is_allowed_supervisor(adapter_name: str) -> bool:
    return str(adapter_name).strip().lower() in _ALLOWED_SUPERVISOR_ADAPTERS


def delegation_role(capability: AgentCapability) -> str:
    if capability == AgentCapability.REVIEW:
        return "review"
    if capability == AgentCapability.FIX_REVIEW:
        return "fix_review"
    return "implement"


def bounded_strings(values: Iterable[object], *, limit: int = 32) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value).strip()
        if text:
            result.append(text[:4000])
        if len(result) >= limit:
            break
    return result
