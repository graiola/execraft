"""Canonical value objects for Project Execution.
Project Execution coordinates canonical Tasks as whole units. It deliberately
contains no Work Package, agent, review-loop, or package-stage concepts.
Definitions are immutable; mutable observations and decisions live in the
runtime repository.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any, Mapping, Sequence, TypeVar
from execraft.project import validate_project_id
from execraft.workspace.task_git import TaskGitError, validate_task_id
from .errors import (
    ProjectExecutionConflictError,
    ProjectExecutionError,
    ProjectExecutionNotFoundError,
)
from .policy import ProjectExecutionPolicy, TaskFailureBehavior
PROJECT_EXECUTION_SCHEMA_VERSION = 1
_MAX_ASSET_ID_LENGTH = 96
_MAX_TITLE_LENGTH = 500
_MAX_TEXT_LENGTH = 20_000
class ExecutionMode(str, Enum):
    """Project-level execution policy."""
    OBSERVE = "observe"
    ASSISTED = "assisted"
    AUTOMATIC = "automatic"
class PhaseState(str, Enum):
    PLANNED = "planned"
    READY = "ready"
    ACTIVE = "active"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
class PhaseHealth(str, Enum):
    ON_TRACK = "on_track"
    AT_RISK = "at_risk"
    BLOCKED = "blocked"
    LATE = "late"
class GateState(str, Enum):
    WAITING = "waiting"
    READY = "ready"
    EVALUATING = "evaluating"
    AWAITING_DECISION = "awaiting_decision"
    PASSED = "passed"
    FAILED = "failed"
    WAIVED = "waived"
    CANCELLED = "cancelled"
class MilestoneState(str, Enum):
    PENDING = "pending"
    ACHIEVED = "achieved"
    CANCELLED = "cancelled"
class MilestoneHealth(str, Enum):
    ON_TRACK = "on_track"
    AT_RISK = "at_risk"
    LATE = "late"
class DeliveryPolicy(str, Enum):
    """Delivery semantics supported by Project Execution schema v1."""

    NONE = "none"
    CANDIDATE = "candidate"


def validate_asset_id(value: object, *, label: str = "project asset id") -> str:
    """Validate a stable Project Execution asset identifier."""
    text = str(value or "").strip()
    valid = text and len(text) <= _MAX_ASSET_ID_LENGTH
    if not valid or not text.replace("-", "_").isidentifier():
        raise ProjectExecutionError(f"invalid {label}: {text!r}")
    return text
def validate_task_reference(value: object) -> str:
    """Validate a reference to a canonical Task without loading Task state."""
    try:
        return validate_task_id(str(value or "").strip())
    except TaskGitError as exc:
        raise ProjectExecutionError(str(exc)) from exc
def _text(
    value: object,
    *,
    label: str,
    required: bool = False,
    limit: int = _MAX_TEXT_LENGTH,
) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise ProjectExecutionError(f"{label} cannot be empty")
    if len(text) > limit:
        raise ProjectExecutionError(f"{label} exceeds {limit} characters")
    return text
def _date(value: object, *, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ProjectExecutionError(
            f"{label} must be an ISO date (YYYY-MM-DD)"
        ) from exc
def _sequence(raw: object, *, label: str) -> Sequence[object]:
    if raw in (None, ""):
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ProjectExecutionError(f"{label} must be a list")
    return raw
def _ids(raw: object, *, task: bool = False, label: str) -> tuple[str, ...]:
    values = _sequence(raw, label=label)
    validator = validate_task_reference if task else validate_asset_id
    result = tuple(validator(value) for value in values)
    if len(set(result)) != len(result):
        raise ProjectExecutionError(f"{label} cannot contain duplicates")
    return result
T = TypeVar("T")
def _objects(values: Sequence[T], *, expected: type[T], label: str) -> tuple[T, ...]:
    """Freeze and type-check an object sequence supplied by programmatic callers."""
    result = tuple(values)
    if any(not isinstance(value, expected) for value in result):
        raise ProjectExecutionError(f"{label} contains an invalid value")
    return result
@dataclass(frozen=True)
class ProjectSchedule:
    """Optional project-level planning schedule."""
    start: str = ""
    target: str = ""
    def __post_init__(self) -> None:
        start = _date(self.start, label="schedule start")
        target = _date(self.target, label="schedule target")
        if start and target and start > target:
            raise ProjectExecutionError("schedule start cannot be after target")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "target", target)
    def as_mapping(self) -> dict[str, str]:
        return {
            key: value
            for key, value in (("start", self.start), ("target", self.target))
            if value
        }
    @classmethod
    def from_mapping(cls, raw: object) -> "ProjectSchedule":
        if raw in (None, ""):
            return cls()
        if not isinstance(raw, Mapping):
            raise ProjectExecutionError("schedule must be a mapping")
        return cls(start=raw.get("start", ""), target=raw.get("target", ""))
@dataclass(frozen=True)
class ProjectPhase:
    """A coherent stage of project execution."""
    id: str
    title: str
    description: str = ""
    schedule: ProjectSchedule = field(default_factory=ProjectSchedule)
    entry_gates: tuple[str, ...] = ()
    exit_gates: tuple[str, ...] = ()
    tasks: tuple[str, ...] = ()
    milestones: tuple[str, ...] = ()
    def __post_init__(self) -> None:
        object.__setattr__(self, "id", validate_asset_id(self.id, label="phase id"))
        object.__setattr__(
            self,
            "title",
            _text(
                self.title,
                label="phase title",
                required=True,
                limit=_MAX_TITLE_LENGTH,
            ),
        )
        object.__setattr__(
            self,
            "description",
            _text(self.description, label="phase description"),
        )
        if not isinstance(self.schedule, ProjectSchedule):
            raise ProjectExecutionError("phase schedule is invalid")
        object.__setattr__(
            self,
            "entry_gates",
            _ids(self.entry_gates, label="phase entry_gates"),
        )
        object.__setattr__(
            self,
            "exit_gates",
            _ids(self.exit_gates, label="phase exit_gates"),
        )
        object.__setattr__(
            self,
            "tasks",
            _ids(self.tasks, task=True, label="phase tasks"),
        )
        object.__setattr__(
            self,
            "milestones",
            _ids(self.milestones, label="phase milestones"),
        )
    def as_mapping(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "entry_gates": list(self.entry_gates),
            "exit_gates": list(self.exit_gates),
            "tasks": list(self.tasks),
            "milestones": list(self.milestones),
        }
        if self.description:
            row["description"] = self.description
        if schedule := self.schedule.as_mapping():
            row["schedule"] = schedule
        return row
    @classmethod
    def from_mapping(cls, raw: object) -> "ProjectPhase":
        if not isinstance(raw, Mapping):
            raise ProjectExecutionError("each phase must be a mapping")
        return cls(
            id=raw.get("id", ""),
            title=raw.get("title", ""),
            description=raw.get("description", ""),
            schedule=ProjectSchedule.from_mapping(raw.get("schedule")),
            entry_gates=_ids(raw.get("entry_gates"), label="phase entry_gates"),
            exit_gates=_ids(raw.get("exit_gates"), label="phase exit_gates"),
            tasks=_ids(raw.get("tasks"), task=True, label="phase tasks"),
            milestones=_ids(raw.get("milestones"), label="phase milestones"),
        )
@dataclass(frozen=True)
class TaskRequirements:
    """Project-level dependencies that can make a canonical Task eligible."""
    tasks: tuple[str, ...] = ()
    gates: tuple[str, ...] = ()
    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "tasks",
            _ids(self.tasks, task=True, label="task prerequisites"),
        )
        object.__setattr__(
            self,
            "gates",
            _ids(self.gates, label="task gate prerequisites"),
        )
    def as_mapping(self) -> dict[str, list[str]]:
        return {"tasks": list(self.tasks), "gates": list(self.gates)}
    @classmethod
    def from_mapping(cls, raw: object) -> "TaskRequirements":
        if raw in (None, ""):
            return cls()
        if not isinstance(raw, Mapping):
            raise ProjectExecutionError("task requires must be a mapping")
        unknown = set(raw) - {"tasks", "gates"}
        if unknown:
            names = ", ".join(sorted(map(str, unknown)))
            raise ProjectExecutionError(f"unsupported task prerequisite kind(s): {names}")
        return cls(
            tasks=_ids(raw.get("tasks"), task=True, label="task prerequisites"),
            gates=_ids(raw.get("gates"), label="task gate prerequisites"),
        )
@dataclass(frozen=True)
class ProjectTask:
    """Project execution metadata for a canonical Task."""
    task_id: str
    phase: str
    required: bool = True
    requires: TaskRequirements = field(default_factory=TaskRequirements)
    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", validate_task_reference(self.task_id))
        object.__setattr__(
            self,
            "phase",
            validate_asset_id(self.phase, label="task phase"),
        )
        if not isinstance(self.required, bool):
            raise ProjectExecutionError("task required must be a boolean")
        if not isinstance(self.requires, TaskRequirements):
            raise ProjectExecutionError("task requires is invalid")
    def as_mapping(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "required": self.required,
            "requires": self.requires.as_mapping(),
        }
    @classmethod
    def from_mapping(cls, task_id: str, raw: object) -> "ProjectTask":
        if not isinstance(raw, Mapping):
            raise ProjectExecutionError(
                f"project task {task_id!r} must be a mapping"
            )
        return cls(
            task_id=task_id,
            phase=raw.get("phase", ""),
            required=raw.get("required", True),
            requires=TaskRequirements.from_mapping(raw.get("requires")),
        )
@dataclass(frozen=True)
class GateCriterion:
    """One typed input to a ProjectGate ALL-composite."""
    type: str
    task_id: str = ""
    gate_id: str = ""
    outcome: str = ""
    artifact_id: str = ""
    def __post_init__(self) -> None:
        criterion_type = str(self.type or "").strip().lower()
        allowed = {
            "task_completion",
            "task_verification",
            "task_artifact",
            "project_gate",
            "human_approval",
        }
        if criterion_type not in allowed:
            raise ProjectExecutionError(
                f"unknown project gate evaluator type: {criterion_type!r}"
            )
        task_id = validate_task_reference(self.task_id) if self.task_id else ""
        gate_id = (
            validate_asset_id(self.gate_id, label="criterion gate id")
            if self.gate_id
            else ""
        )
        outcome = str(self.outcome or "").strip().lower()
        artifact_id = _text(self.artifact_id, label="artifact id", limit=500)
        requires_task = criterion_type in {
            "task_completion",
            "task_verification",
            "task_artifact",
        }
        if requires_task != bool(task_id):
            raise ProjectExecutionError(
                f"criterion {criterion_type!r} has invalid task_id"
            )
        if (criterion_type == "project_gate") != bool(gate_id):
            raise ProjectExecutionError(
                f"criterion {criterion_type!r} has invalid gate_id"
            )
        if criterion_type == "task_verification" and not outcome:
            outcome = "passed"
        elif criterion_type != "task_verification" and outcome:
            raise ProjectExecutionError(
                f"criterion {criterion_type!r} cannot define outcome"
            )
        if criterion_type == "task_artifact" and not artifact_id:
            raise ProjectExecutionError(
                "task_artifact criterion requires artifact_id"
            )
        if criterion_type != "task_artifact" and artifact_id:
            raise ProjectExecutionError(
                f"criterion {criterion_type!r} cannot define artifact_id"
            )
        object.__setattr__(self, "type", criterion_type)
        object.__setattr__(self, "task_id", task_id)
        object.__setattr__(self, "gate_id", gate_id)
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "artifact_id", artifact_id)
    def as_mapping(self) -> dict[str, str]:
        return {
            key: value
            for key, value in (
                ("type", self.type),
                ("task_id", self.task_id),
                ("gate_id", self.gate_id),
                ("outcome", self.outcome),
                ("artifact_id", self.artifact_id),
            )
            if value
        }
    @classmethod
    def from_mapping(cls, raw: object) -> "GateCriterion":
        if not isinstance(raw, Mapping):
            raise ProjectExecutionError("each gate criterion must be a mapping")
        allowed = {"type", "task_id", "gate_id", "outcome", "artifact_id"}
        unknown = set(raw) - allowed
        if unknown:
            names = ", ".join(sorted(map(str, unknown)))
            raise ProjectExecutionError(f"unknown gate criterion fields: {names}")
        return cls(
            type=raw.get("type", ""),
            task_id=raw.get("task_id", ""),
            gate_id=raw.get("gate_id", ""),
            outcome=raw.get("outcome", ""),
            artifact_id=raw.get("artifact_id", ""),
        )
@dataclass(frozen=True)
class ProjectGate:
    """A project boundary whose outcome is derived from typed evidence."""
    id: str
    title: str
    description: str = ""
    schedule: ProjectSchedule = field(default_factory=ProjectSchedule)
    criteria: tuple[GateCriterion, ...] = ()
    def __post_init__(self) -> None:
        object.__setattr__(self, "id", validate_asset_id(self.id, label="gate id"))
        object.__setattr__(
            self,
            "title",
            _text(
                self.title,
                label="gate title",
                required=True,
                limit=_MAX_TITLE_LENGTH,
            ),
        )
        object.__setattr__(
            self,
            "description",
            _text(self.description, label="gate description"),
        )
        if not isinstance(self.schedule, ProjectSchedule):
            raise ProjectExecutionError("gate schedule is invalid")
        criteria = _objects(
            self.criteria,
            expected=GateCriterion,
            label="gate criteria",
        )
        if not criteria:
            raise ProjectExecutionError(
                f"gate {self.id!r} must define at least one criterion"
            )
        if sum(item.type == "human_approval" for item in criteria) > 1:
            raise ProjectExecutionError(
                f"gate {self.id!r} may contain at most one human approval criterion"
            )
        object.__setattr__(self, "criteria", criteria)
    def as_mapping(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "criteria": {"all": [criterion.as_mapping() for criterion in self.criteria]},
        }
        if self.description:
            row["description"] = self.description
        if schedule := self.schedule.as_mapping():
            row["schedule"] = schedule
        return row
    @classmethod
    def from_mapping(cls, raw: object) -> "ProjectGate":
        if not isinstance(raw, Mapping):
            raise ProjectExecutionError("each gate must be a mapping")
        criteria_raw = raw.get("criteria")
        if isinstance(criteria_raw, Mapping):
            unknown = set(criteria_raw) - {"all"}
            if unknown:
                raise ProjectExecutionError(
                    "ProjectGate schema v1 supports only criteria.all"
                )
            criteria_raw = criteria_raw.get("all")
        criteria = tuple(
            GateCriterion.from_mapping(item)
            for item in _sequence(criteria_raw, label="gate criteria")
        )
        return cls(
            id=raw.get("id", ""),
            title=raw.get("title", ""),
            description=raw.get("description", ""),
            schedule=ProjectSchedule.from_mapping(raw.get("schedule")),
            criteria=criteria,
        )
@dataclass(frozen=True)
class MilestoneRequirements:
    """Requirements whose first satisfaction freezes a Milestone baseline."""
    tasks: tuple[str, ...] = ()
    gates: tuple[str, ...] = ()
    milestones: tuple[str, ...] = ()
    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "tasks",
            _ids(self.tasks, task=True, label="milestone tasks"),
        )
        object.__setattr__(
            self,
            "gates",
            _ids(self.gates, label="milestone gates"),
        )
        object.__setattr__(
            self,
            "milestones",
            _ids(self.milestones, label="milestone prerequisites"),
        )
    def as_mapping(self) -> dict[str, list[str]]:
        return {
            "tasks": list(self.tasks),
            "gates": list(self.gates),
            "milestones": list(self.milestones),
        }
    @classmethod
    def from_mapping(cls, raw: object) -> "MilestoneRequirements":
        if raw in (None, ""):
            return cls()
        if not isinstance(raw, Mapping):
            raise ProjectExecutionError("milestone requires must be a mapping")
        unknown = set(raw) - {"tasks", "gates", "milestones"}
        if unknown:
            names = ", ".join(sorted(map(str, unknown)))
            raise ProjectExecutionError(
                f"unsupported milestone prerequisite kind(s): {names}"
            )
        return cls(
            tasks=_ids(raw.get("tasks"), task=True, label="milestone tasks"),
            gates=_ids(raw.get("gates"), label="milestone gates"),
            milestones=_ids(
                raw.get("milestones"),
                label="milestone prerequisites",
            ),
        )
@dataclass(frozen=True)
class ProjectMilestone:
    """An achieved capability backed by an immutable reproducible baseline."""
    id: str
    title: str
    description: str = ""
    start: str = ""
    target: str = ""
    requires: MilestoneRequirements = field(default_factory=MilestoneRequirements)
    delivery_policy: DeliveryPolicy = DeliveryPolicy.NONE
    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "id",
            validate_asset_id(self.id, label="milestone id"),
        )
        object.__setattr__(
            self,
            "title",
            _text(
                self.title,
                label="milestone title",
                required=True,
                limit=_MAX_TITLE_LENGTH,
            ),
        )
        object.__setattr__(
            self,
            "description",
            _text(self.description, label="milestone description"),
        )
        if not isinstance(self.requires, MilestoneRequirements):
            raise ProjectExecutionError("milestone requires is invalid")
        start = _date(self.start, label="milestone start")
        target = _date(self.target, label="milestone target")
        if start and target and start > target:
            raise ProjectExecutionError("milestone start cannot be after target")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "target", target)
        try:
            policy = DeliveryPolicy(self.delivery_policy)
        except ValueError as exc:
            raise ProjectExecutionError(
                "delivery.policy must be none or candidate"
            ) from exc
        object.__setattr__(self, "delivery_policy", policy)
    def as_mapping(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "requires": self.requires.as_mapping(),
            "delivery": {"policy": self.delivery_policy.value},
        }
        if self.description:
            row["description"] = self.description
        # Keep top-level ``target`` as the canonical v1 shape from the PLAN while
        # retaining an optional schedule.start for migrated Roadmap v1 data.
        if self.start:
            row["schedule"] = {"start": self.start}
        if self.target:
            row["target"] = self.target
        return row
    @classmethod
    def from_mapping(cls, raw: object) -> "ProjectMilestone":
        if not isinstance(raw, Mapping):
            raise ProjectExecutionError("each milestone must be a mapping")
        delivery = raw.get("delivery") or {}
        if not isinstance(delivery, Mapping):
            raise ProjectExecutionError("milestone delivery must be a mapping")
        schedule = ProjectSchedule.from_mapping(raw.get("schedule"))
        return cls(
            id=raw.get("id", ""),
            title=raw.get("title", ""),
            description=raw.get("description", ""),
            start=schedule.start,
            target=raw.get("target", schedule.target),
            requires=MilestoneRequirements.from_mapping(raw.get("requires")),
            delivery_policy=delivery.get("policy", "none"),
        )
@dataclass(frozen=True)
class ProjectExecutionDefinition:
    """The complete canonical Project Execution graph."""
    project: str
    revision: int = 1
    mode: ExecutionMode = ExecutionMode.ASSISTED
    phases: tuple[ProjectPhase, ...] = ()
    tasks: tuple[ProjectTask, ...] = ()
    gates: tuple[ProjectGate, ...] = ()
    milestones: tuple[ProjectMilestone, ...] = ()
    policy: ProjectExecutionPolicy = field(default_factory=ProjectExecutionPolicy)
    schema_version: int = PROJECT_EXECUTION_SCHEMA_VERSION
    def __post_init__(self) -> None:
        if self.schema_version != PROJECT_EXECUTION_SCHEMA_VERSION:
            raise ProjectExecutionError(
                "unsupported Project Execution schema_version: "
                f"{self.schema_version!r}"
            )
        try:
            project = validate_project_id(self.project)
        except Exception as exc:
            raise ProjectExecutionError(str(exc)) from exc
        try:
            revision = int(self.revision)
        except (TypeError, ValueError) as exc:
            raise ProjectExecutionError(
                "Project Execution revision must be an integer"
            ) from exc
        if revision < 1:
            raise ProjectExecutionError(
                "Project Execution revision must be at least 1"
            )
        try:
            mode = ExecutionMode(self.mode)
        except ValueError as exc:
            raise ProjectExecutionError(
                "mode must be observe, assisted, or automatic"
            ) from exc
        if not isinstance(self.policy, ProjectExecutionPolicy):
            raise ProjectExecutionError("Project Execution policy is invalid")
        object.__setattr__(self, "project", project)
        object.__setattr__(self, "revision", revision)
        object.__setattr__(self, "mode", mode)
        object.__setattr__(
            self,
            "phases",
            _objects(self.phases, expected=ProjectPhase, label="phases"),
        )
        object.__setattr__(
            self,
            "tasks",
            _objects(self.tasks, expected=ProjectTask, label="tasks"),
        )
        object.__setattr__(
            self,
            "gates",
            _objects(self.gates, expected=ProjectGate, label="gates"),
        )
        object.__setattr__(
            self,
            "milestones",
            _objects(
                self.milestones,
                expected=ProjectMilestone,
                label="milestones",
            ),
        )
        self._validate_unique_ids()
    def _validate_unique_ids(self) -> None:
        for label, values in (
            ("phase", self.phases),
            ("gate", self.gates),
            ("milestone", self.milestones),
        ):
            ids = [value.id for value in values]
            if len(ids) != len(set(ids)):
                raise ProjectExecutionError(f"duplicate {label} IDs")
        task_ids = [value.task_id for value in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ProjectExecutionError("duplicate project task IDs")
        asset_ids = [
            value.id
            for group in (self.phases, self.gates, self.milestones)
            for value in group
        ]
        if len(asset_ids) != len(set(asset_ids)):
            raise ProjectExecutionError(
                "Phase, Gate, and Milestone IDs must be globally unique"
            )
    @property
    def task_index(self) -> dict[str, ProjectTask]:
        return {value.task_id: value for value in self.tasks}
    @property
    def phase_index(self) -> dict[str, ProjectPhase]:
        return {value.id: value for value in self.phases}
    @property
    def gate_index(self) -> dict[str, ProjectGate]:
        return {value.id: value for value in self.gates}
    @property
    def milestone_index(self) -> dict[str, ProjectMilestone]:
        return {value.id: value for value in self.milestones}
    def as_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "project": self.project,
            "revision": self.revision,
            "mode": self.mode.value,
            "policy": self.policy.as_mapping(),
            "phases": [value.as_mapping() for value in self.phases],
            "tasks": {
                value.task_id: value.as_mapping()
                for value in sorted(self.tasks, key=lambda item: item.task_id)
            },
            "gates": [value.as_mapping() for value in self.gates],
            "milestones": [value.as_mapping() for value in self.milestones],
        }
    @classmethod
    def from_mapping(cls, raw: object) -> "ProjectExecutionDefinition":
        if not isinstance(raw, Mapping):
            raise ProjectExecutionError(
                "PROJECT_EXECUTION.yaml must contain a mapping"
            )
        phases = tuple(
            ProjectPhase.from_mapping(value)
            for value in _sequence(raw.get("phases"), label="phases")
        )
        gates = tuple(
            ProjectGate.from_mapping(value)
            for value in _sequence(raw.get("gates"), label="gates")
        )
        milestones = tuple(
            ProjectMilestone.from_mapping(value)
            for value in _sequence(raw.get("milestones"), label="milestones")
        )
        tasks_raw = raw.get("tasks") or {}
        if not isinstance(tasks_raw, Mapping):
            raise ProjectExecutionError(
                "tasks must be a mapping keyed by canonical task ID"
            )
        tasks = tuple(
            ProjectTask.from_mapping(str(task_id), value)
            for task_id, value in tasks_raw.items()
        )
        schema_version = raw.get("schema_version", 0)
        try:
            normalized_schema_version = int(schema_version)
        except (TypeError, ValueError) as exc:
            raise ProjectExecutionError(
                "Project Execution schema_version must be an integer"
            ) from exc
        policy = ProjectExecutionPolicy.from_mapping(raw.get("policy"))
        definition = cls(
            schema_version=normalized_schema_version,
            project=raw.get("project", ""),
            revision=raw.get("revision", 1),
            mode=raw.get("mode", "assisted"),
            policy=policy,
            phases=phases,
            tasks=tasks,
            gates=gates,
            milestones=milestones,
        )
        # Import lazily to avoid a models -> validation -> models import cycle.
        from .validation import validate_definition
        validate_definition(definition)
        return definition
