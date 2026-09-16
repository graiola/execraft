"""Typed Gate evaluator contracts and deterministic evidence fingerprinting."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any, Mapping, Protocol

from ..models import GateCriterion, ProjectExecutionDefinition
from ..runtime_repository import ProjectExecutionRuntimeState
from ..task_port import TaskExecutionPort


@dataclass(frozen=True)
class CriterionResult:
    """Result of evaluating one typed ProjectGate criterion."""

    satisfied: bool
    evidence: Mapping[str, Any] = field(default_factory=dict)
    reason: str = ""
    awaiting_decision: bool = False


@dataclass(frozen=True)
class GateEvaluationContext:
    definition: ProjectExecutionDefinition
    runtime: ProjectExecutionRuntimeState
    task_port: TaskExecutionPort
    gate_id: str
    evidence_fingerprint: str = ""


class ProjectGateEvaluator(Protocol):
    criterion_type: str

    def evaluate(
        self,
        criterion: GateCriterion,
        context: GateEvaluationContext,
    ) -> CriterionResult:
        ...


def canonical_fingerprint(value: object) -> str:
    """Hash canonical JSON so equivalent evidence yields an identical fingerprint."""

    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return "sha256:" + sha256(encoded).hexdigest()
