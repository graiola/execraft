"""Anti-corruption boundary from Project Execution to canonical Task execution.

Project Execution deliberately sees a Task as one coarse execution unit.  The
port exposes no Work Package, Stage, agent-routing, review-loop, or verification
runner primitives; those remain private to the Task Execution domain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol


class TaskOutcome(str, Enum):
    """Project-level view of one canonical Task execution outcome."""

    NOT_STARTED = "not_started"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TaskVerificationSummary:
    """Public verification summary exported by Task Execution."""

    outcome: str = "unknown"
    passed: int = 0
    failed: int = 0
    total: int = 0


@dataclass(frozen=True)
class TaskExecutionSummary:
    """Minimal Task identity/availability projection used for eligibility."""

    task_id: str
    exists: bool
    active: bool
    executable: bool
    execution_state: str = ""
    outcome: TaskOutcome = TaskOutcome.NOT_STARTED
    title: str = ""


@dataclass(frozen=True)
class TaskEvidence:
    """Stable public evidence that Project Gates/Milestones may consume."""

    task_id: str
    outcome: TaskOutcome
    task_digest: str = ""
    plan_digest: str = ""
    verification: TaskVerificationSummary = field(default_factory=TaskVerificationSummary)
    repository_revisions: Mapping[str, str] = field(default_factory=dict)
    artifacts: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TaskStartResult:
    """Result of requesting canonical Task startup."""

    accepted: bool
    already_started: bool = False
    message: str = ""


@dataclass(frozen=True)
class TaskActionResult:
    """Result of a coarse Task action such as pause or resume."""

    accepted: bool
    message: str = ""


class TaskExecutionPort(Protocol):
    """Only Task-level capabilities visible to Project Execution."""

    def describe(self, task_id: str) -> TaskExecutionSummary:
        ...

    def start(self, task_id: str) -> TaskStartResult:
        ...

    def pause(self, task_id: str) -> TaskActionResult:
        ...

    def resume(self, task_id: str) -> TaskActionResult:
        ...

    def outcome(self, task_id: str) -> TaskOutcome:
        ...

    def evidence(self, task_id: str) -> TaskEvidence:
        ...


__all__ = [
    "TaskActionResult",
    "TaskEvidence",
    "TaskExecutionPort",
    "TaskExecutionSummary",
    "TaskOutcome",
    "TaskStartResult",
    "TaskVerificationSummary",
]
