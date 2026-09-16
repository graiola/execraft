"""Project Milestone requirement evaluation and immutable achievement."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .baseline import BaselineBuildResult, ProjectBaselineBuilder
from .eligibility import gate_satisfied
from .events import ProjectEventJournal
from .models import (
    MilestoneHealth,
    MilestoneState,
    ProjectExecutionDefinition,
    ProjectMilestone,
)
from .runtime_repository import ProjectExecutionRuntimeState
from .task_port import TaskExecutionPort, TaskOutcome


@dataclass(frozen=True)
class MilestoneProjection:
    state: MilestoneState
    health: MilestoneHealth
    missing: tuple[str, ...] = ()


class MilestoneRequirementEvaluator:
    """Pure evaluator for Milestone achievement prerequisites and health."""

    def evaluate(
        self,
        milestone: ProjectMilestone,
        *,
        runtime: ProjectExecutionRuntimeState,
        task_port: TaskExecutionPort,
        today: date | None = None,
    ) -> MilestoneProjection:
        if milestone.id in runtime.cancelled_milestones:
            return MilestoneProjection(
                MilestoneState.CANCELLED,
                MilestoneHealth.ON_TRACK,
            )
        if milestone.id in runtime.milestone_achievements:
            return MilestoneProjection(
                MilestoneState.ACHIEVED,
                MilestoneHealth.ON_TRACK,
            )

        missing: list[str] = []
        for task_id in milestone.requires.tasks:
            if task_port.outcome(task_id) != TaskOutcome.COMPLETED:
                missing.append(f"task:{task_id}")
        for gate_id in milestone.requires.gates:
            if not gate_satisfied(runtime, gate_id):
                missing.append(f"gate:{gate_id}")
        for required_id in milestone.requires.milestones:
            if required_id not in runtime.milestone_achievements:
                missing.append(f"milestone:{required_id}")

        health = self._health(milestone, missing=missing, today=today or date.today())
        return MilestoneProjection(
            MilestoneState.PENDING,
            health,
            tuple(missing),
        )

    @staticmethod
    def _health(
        milestone: ProjectMilestone,
        *,
        missing: list[str],
        today: date,
    ) -> MilestoneHealth:
        if not milestone.target:
            return MilestoneHealth.ON_TRACK
        target = date.fromisoformat(milestone.target)
        if today > target:
            return MilestoneHealth.LATE
        if missing and target - today <= timedelta(days=7):
            return MilestoneHealth.AT_RISK
        return MilestoneHealth.ON_TRACK


class MilestoneAchievementService:
    """Detect first achievement and persist exactly one immutable baseline."""

    def __init__(self, builder: ProjectBaselineBuilder | None = None) -> None:
        self.builder = builder or ProjectBaselineBuilder()
        self.requirements = MilestoneRequirementEvaluator()

    def achieve_if_ready(
        self,
        milestone: ProjectMilestone,
        *,
        definition: ProjectExecutionDefinition,
        runtime: ProjectExecutionRuntimeState,
        task_port: TaskExecutionPort,
        journal: ProjectEventJournal | None = None,
    ) -> BaselineBuildResult | None:
        if milestone.id in runtime.milestone_achievements:
            return None

        projection = self.requirements.evaluate(
            milestone,
            runtime=runtime,
            task_port=task_port,
        )
        if projection.state != MilestoneState.PENDING or projection.missing:
            return None

        result = self.builder.build(
            milestone,
            definition=definition,
            runtime=runtime,
            task_port=task_port,
        )
        if not result.complete:
            return result

        runtime.milestone_achievements[milestone.id] = dict(result.baseline)
        if journal is not None:
            journal.append(
                "project_milestone_achieved",
                {
                    "milestone_id": milestone.id,
                    "achievement": result.baseline,
                },
            )
        return result
