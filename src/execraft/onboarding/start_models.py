"""Immutable value objects for the one-command start workflow."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from execraft.agents.config import AgentProviderConfig
from execraft.onboarding.models import CreationPlan, Evidence
from execraft.onboarding.providers import ProviderInventoryItem
from execraft.onboarding.task_definition import TaskDefinitionInput
from execraft.workspace.task_git import validate_task_id


class StartWorkflowError(RuntimeError):
    """Raised when the one-command workflow cannot safely continue."""


class ImportedPlanConsistencyError(StartWorkflowError):
    """Raised when an imported brief and plan materially contradict each other."""


class PlannerMode(str, Enum):
    AUTO = "auto"
    AGENT = "agent"
    LOCAL = "local"


class StepStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    COMPLETED = "completed"
    REUSED = "reused"
    SKIPPED = "skipped"
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass(frozen=True)
class TaskIntent:
    description: str
    title: str
    task_id: str
    intent_sha256: str
    explicit_task_id: bool = False

    @classmethod
    def create(
        cls,
        description: str,
        *,
        title: str = "",
        task_id: str = "",
        identity_material: str = "",
        digest_override: str = "",
    ) -> "TaskIntent":
        normalized = " ".join(description.split())
        if not normalized:
            raise StartWorkflowError("start requires a non-empty task description")
        generated_title = title.strip() or _sentence_title(normalized)
        generated_id = validate_task_id(task_id) if task_id else _slugify_task(normalized)
        if digest_override:
            if not re.fullmatch(r"[0-9a-f]{64}", digest_override):
                raise StartWorkflowError("expected request SHA-256 must be 64 lowercase hex characters")
            digest = digest_override
        else:
            digest_source = identity_material or normalized
            digest = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()
        return cls(
            description=normalized,
            title=generated_title,
            task_id=generated_id,
            intent_sha256=digest,
            explicit_task_id=bool(task_id),
        )

    def with_task_id(self, task_id: str) -> "TaskIntent":
        return replace(self, task_id=validate_task_id(task_id))

    def as_mapping(self) -> dict[str, str]:
        return {
            "description": self.description,
            "title": self.title,
            "task_id": self.task_id,
            "intent_sha256": self.intent_sha256,
            "explicit_task_id": str(self.explicit_task_id).lower(),
        }


@dataclass(frozen=True)
class RepositoryScope:
    repository_ids: tuple[str, ...]
    evidence: tuple[Evidence, ...]
    explicit: bool = False

    def as_mapping(self) -> dict[str, Any]:
        return {
            "repositories": list(self.repository_ids),
            "explicit": self.explicit,
            "evidence": [item.as_mapping() for item in self.evidence],
        }


@dataclass(frozen=True)
class ProviderChoice:
    provider: ProviderInventoryItem | None
    config: AgentProviderConfig | None
    reason: str
    candidates: tuple[ProviderInventoryItem, ...] = ()

    @property
    def available(self) -> bool:
        return self.provider is not None and self.config is not None

    def as_mapping(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "selected": self.provider.as_mapping() if self.provider else None,
            "reason": self.reason,
            "candidates": [item.as_mapping() for item in self.candidates],
        }


@dataclass(frozen=True)
class DraftPlanArtifact:
    markdown: str
    graph: Mapping[str, Any]
    generated_by: str
    provider_id: str = ""
    fallback_reason: str = ""
    consistency_mode: str = ""
    consistency_summary: str = ""

    def as_mapping(self) -> dict[str, Any]:
        return {
            "generated_by": self.generated_by,
            "provider_id": self.provider_id,
            "fallback_reason": self.fallback_reason,
            "consistency_mode": self.consistency_mode,
            "consistency_summary": self.consistency_summary,
            "work_packages": len(self.graph.get("work_packages", [])),
        }


@dataclass(frozen=True)
class StartStep:
    id: str
    status: StepStatus
    summary: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def as_mapping(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status.value,
            "summary": self.summary,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class StartOutcome:
    project_id: str
    task_id: str
    source_root: Path
    workspace_root: Path | None
    provider: ProviderChoice
    repository_scope: RepositoryScope
    steps: tuple[StartStep, ...]
    project_plan: CreationPlan | None = None
    task_plan: CreationPlan | None = None
    plan_artifact: DraftPlanArtifact | None = None
    journal_path: Path | None = None
    applied: bool = False

    @property
    def can_apply(self) -> bool:
        blocked = {StepStatus.BLOCKED, StepStatus.FAILED}
        return not any(step.status in blocked for step in self.steps)

    @property
    def ready(self) -> bool:
        return self.applied and self.can_apply

    def as_mapping(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "can_apply": self.can_apply,
            "ready": self.ready,
            "project": self.project_id,
            "task_id": self.task_id,
            "source_root": str(self.source_root),
            "workspace_root": str(self.workspace_root) if self.workspace_root else "",
            "provider": self.provider.as_mapping(),
            "repository_scope": self.repository_scope.as_mapping(),
            "steps": [item.as_mapping() for item in self.steps],
            "project_plan": self.project_plan.as_mapping() if self.project_plan else None,
            "task_plan": self.task_plan.as_mapping() if self.task_plan else None,
            "plan": self.plan_artifact.as_mapping() if self.plan_artifact else None,
            "journal": str(self.journal_path) if self.journal_path else "",
        }

    def render_text(self) -> str:
        lines = [
            f"Project:   {self.project_id}",
            f"Task:      {self.task_id}",
            f"Source:    {self.source_root}",
            f"Workspace: {self.workspace_root or '<disabled>'}",
            "Repositories: " + ", ".join(self.repository_scope.repository_ids),
            "Planner:    "
            + (
                f"{self.provider.provider.name} ({self.provider.provider.adapter})"
                if self.provider.provider
                else "local deterministic draft"
            ),
            "Steps:",
        ]
        for step in self.steps:
            lines.append(f"  {step.status.value.upper():<9} {step.id:<12} {step.summary}")
        if self.journal_path:
            lines.append(f"Journal: {self.journal_path}")
        if self.applied:
            lines.append(f"Overall: {'READY' if self.ready else 'INCOMPLETE'}")
        else:
            lines.append("Overall: PREVIEW")
        return "\n".join(lines)


@dataclass(frozen=True)
class StartRequest:
    description: str
    source_root: Path
    project_id: str = ""
    task_id: str = ""
    title: str = ""
    repository_ids: tuple[str, ...] = ()
    provider_id: str = ""
    planner_mode: PlannerMode = PlannerMode.AUTO
    project_template: str = "standard"
    task_template: str = "standard"
    workspace_root: Path | None = None
    policy_profile: str = ""
    no_workspace: bool = False
    reuse_in_place: bool = False
    accept_decisions: bool = False
    require_provider: bool = False
    force_plan: bool = False
    task_definition: TaskDefinitionInput = field(default_factory=TaskDefinitionInput)
    expected_request_sha256: str = ""



def _sentence_title(description: str, *, limit: int = 90) -> str:
    title = description.strip().rstrip(".?!")
    if len(title) > limit:
        title = title[: limit - 1].rstrip() + "…"
    return title[0].upper() + title[1:] if title else "Task"


def _slugify_task(description: str, *, limit: int = 56) -> str:
    words = re.findall(r"[a-z0-9]+", description.lower())
    ignored = {"a", "an", "the", "to", "for", "of", "and", "with", "into"}
    significant = [word for word in words if word not in ignored] or words
    slug = "-".join(significant[:8])[:limit].strip("-")
    return validate_task_id(slug or "task")



__all__ = [
    "DraftPlanArtifact",
    "PlannerMode",
    "ProviderChoice",
    "RepositoryScope",
    "StartOutcome",
    "StartRequest",
    "StartStep",
    "StartWorkflowError",
    "StepStatus",
    "TaskIntent",
]
