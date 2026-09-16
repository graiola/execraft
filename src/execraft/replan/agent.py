"""Read-only AI collaborator for candidate task replanning.

The model may propose a candidate definition and package mapping, but it never
publishes files or mutates orchestration state. Deterministic validation and
reconciliation remain the responsibility of :mod:`execraft.replan.service`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

from execraft.runtime.native import build_native_runtime
from execraft.onboarding.start_models import ProviderChoice
from execraft.orchestrate.scheduler import AgentExecutionError, StructuredHandoff
from execraft.skills import SkillCatalog

from .models import ReplanConsistencyError, ReplanError

_MAX_REPLAN_CONTEXT_BYTES = 384 * 1024


@dataclass(frozen=True)
class AgentReplanProposal:
    consistent: bool
    consistency_summary: str
    brief_markdown: str
    plan_markdown: str
    plan_graph_yaml: str
    package_mapping: Mapping[str, str]
    change_summary: str
    provider_id: str


class AgentReplanner:
    """Use a planning-capable provider to propose/validate one replan."""

    def __init__(
        self,
        *,
        adapter_builder: Callable[..., Any] = build_native_runtime,
        skill_catalog: SkillCatalog | None = None,
    ) -> None:
        self.adapter_builder = adapter_builder
        self.skill_catalog = skill_catalog or SkillCatalog.load()

    def propose(
        self,
        *,
        provider: ProviderChoice,
        workdir: Path,
        project_id: str,
        task_id: str,
        allowed_repositories: tuple[str, ...],
        current_brief: str,
        current_plan: str,
        current_graph_yaml: str,
        state_summary: Mapping[str, Any],
        requested_change: str,
        supplied_brief: str = "",
        supplied_plan: str = "",
        supplied_graph_yaml: str = "",
    ) -> AgentReplanProposal:
        if not provider.available or provider.config is None or provider.provider is None:
            raise ReplanError(provider.reason or "no planning provider is available")
        context_size = sum(
            len(value.encode("utf-8"))
            for value in (
                current_brief,
                current_plan,
                current_graph_yaml,
                supplied_brief,
                supplied_plan,
                supplied_graph_yaml,
                requested_change,
                json.dumps(dict(state_summary), sort_keys=True),
            )
        )
        if context_size > _MAX_REPLAN_CONTEXT_BYTES:
            raise ReplanError(
                f"replanning context requires {context_size} bytes, exceeding the "
                f"{_MAX_REPLAN_CONTEXT_BYTES}-byte safety budget; provide a complete "
                "candidate PLAN.graph.yaml or reduce the requested document scope"
            )

        skills = [item.as_mapping() for item in self.skill_catalog.materialize("replan", ["ai-replan"])]
        adapter = self.adapter_builder(provider.config, workdir=workdir, read_only=True)
        handoff = StructuredHandoff(
            work_package_id="TASK-REPLAN",
            stage="replan",
            summary=self._summary(
                project_id=project_id,
                task_id=task_id,
                allowed_repositories=allowed_repositories,
                current_brief=current_brief,
                current_plan=current_plan,
                current_graph_yaml=current_graph_yaml,
                state_summary=state_summary,
                requested_change=requested_change,
                supplied_brief=supplied_brief,
                supplied_plan=supplied_plan,
                supplied_graph_yaml=supplied_graph_yaml,
            ),
            requirements=[
                "Preserve completed package semantics and never reinterpret completed work.",
                "Use new package IDs for remediation when a completed or started package needs changed work.",
                "Keep the graph acyclic and within the existing task repository scope.",
                "Return at least one complete, independently verifiable work package.",
                "Do not mutate files or execute project commands.",
            ],
            acceptance_criteria=[
                {
                    "id": "coherent_definition",
                    "description": "BRIEF.md, PLAN.md, and PLAN.graph.yaml are mutually coherent.",
                },
                {
                    "id": "safe_package_mapping",
                    "description": "Started/completed package history is preserved or explicitly superseded.",
                },
            ],
            expected_output_schema={
                "type": "object",
                "required": [
                    "consistent",
                    "consistency_summary",
                    "brief_markdown",
                    "plan_markdown",
                    "plan_graph",
                    "package_mapping",
                    "change_summary",
                ],
                "properties": {
                    "consistent": {"type": "boolean"},
                    "consistency_summary": {"type": "string"},
                    "brief_markdown": {"type": "string"},
                    "plan_markdown": {"type": "string"},
                    "plan_graph": {
                        "type": "object",
                        "required": ["schema_version", "work_packages"],
                        "properties": {
                            "schema_version": {"type": "integer", "const": 1},
                            "source_document": {"type": "string"},
                            "work_packages": {
                                "type": "array",
                                "minItems": 1,
                                "items": {
                                    "type": "object",
                                    "required": [
                                        "id",
                                        "title",
                                        "dependencies",
                                        "requirements",
                                        "acceptance_criteria",
                                        "affected_repositories",
                                        "risk",
                                        "priority",
                                        "verification_profile",
                                    ],
                                    "properties": {
                                        "id": {"type": "string"},
                                        "title": {"type": "string"},
                                        "dependencies": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                        },
                                        "requirements": {
                                            "type": "array",
                                            "minItems": 1,
                                            "items": {"type": "string"},
                                        },
                                        "acceptance_criteria": {
                                            "type": "array",
                                            "minItems": 1,
                                            "items": {
                                                "type": "object",
                                                "required": ["id", "description"],
                                                "properties": {
                                                    "id": {"type": "string"},
                                                    "description": {"type": "string"},
                                                },
                                                "additionalProperties": False,
                                            },
                                        },
                                        "affected_repositories": {
                                            "type": "array",
                                            "minItems": 1,
                                            "items": {
                                                "type": "string",
                                                "enum": list(allowed_repositories),
                                            },
                                        },
                                        "risk": {"type": "string"},
                                        "priority": {"type": "integer"},
                                        "verification_profile": {"type": "string"},
                                    },
                                    "additionalProperties": True,
                                },
                            },
                        },
                        "additionalProperties": False,
                    },
                    "package_mapping": {"type": "object"},
                    "change_summary": {"type": "string"},
                },
                "additionalProperties": False,
            },
            working_directory=str(workdir),
            read_only=True,
            required_isolation="provider_policy",
            workflow_skills=skills,
            execution_context={
                "project": project_id,
                "task_id": task_id,
                "repositories": list(allowed_repositories),
                "mode": "task_definition_replan",
            },
        ).for_attempt(1)
        try:
            result = adapter.execute(handoff)
        except AgentExecutionError as exc:
            raise ReplanError(
                f"replanning provider {provider.provider.name!r} failed "
                f"({exc.classification}): {exc}"
            ) from exc
        if not isinstance(result, Mapping):
            raise ReplanError("replanning provider returned a non-mapping result")
        payload = _parse_payload(str(result.get("final_message", "")))
        if payload["consistent"] is not True:
            summary = payload["consistency_summary"]
            raise ReplanConsistencyError(
                "candidate task definition is materially inconsistent"
                + (f": {summary}" if summary else "")
            )
        graph_yaml = yaml.safe_dump(payload["plan_graph"], sort_keys=False, width=1000)
        return AgentReplanProposal(
            consistent=True,
            consistency_summary=payload["consistency_summary"],
            brief_markdown=payload["brief_markdown"],
            plan_markdown=payload["plan_markdown"],
            plan_graph_yaml=graph_yaml,
            package_mapping=payload["package_mapping"],
            change_summary=payload["change_summary"],
            provider_id=provider.provider.provider_id,
        )

    @staticmethod
    def _summary(**values: Any) -> str:
        return (
            "Produce a safe candidate revision of the task definition. Return exactly one JSON "
            "object matching the requested schema. Existing supplied candidate documents are "
            "authoritative and must not be silently rewritten; when they conflict, set "
            "consistent=false. If PLAN.md changes without a supplied PLAN.graph.yaml, derive a "
            "new executable graph. package_mapping maps an old started package ID to a NEW "
            "replacement package ID when that started package is intentionally superseded. "
            "Completed packages must remain present with unchanged semantic contracts; new "
            "requirements affecting completed work belong in remediation packages with new IDs.\n\n"
            f"Project: {values['project_id']}\nTask: {values['task_id']}\n"
            f"Allowed repositories: {json.dumps(values['allowed_repositories'])}\n"
            f"Requested change: {values['requested_change'] or '(validate supplied candidate)'}\n"
            f"Runtime package state: {json.dumps(values['state_summary'], sort_keys=True)}\n\n"
            f"CURRENT BRIEF.md:\n{values['current_brief']}\n\n"
            f"CURRENT PLAN.md:\n{values['current_plan']}\n\n"
            f"CURRENT PLAN.graph.yaml:\n{values['current_graph_yaml']}\n\n"
            f"SUPPLIED CANDIDATE BRIEF.md:\n{values['supplied_brief'] or '(not supplied)'}\n\n"
            f"SUPPLIED CANDIDATE PLAN.md:\n{values['supplied_plan'] or '(not supplied)'}\n\n"
            f"SUPPLIED CANDIDATE PLAN.graph.yaml:\n{values['supplied_graph_yaml'] or '(not supplied)'}"
        )


def _parse_payload(text: str) -> dict[str, Any]:
    stripped = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.DOTALL)
    if fenced:
        stripped = fenced.group(1).strip()
    try:
        raw = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ReplanError(f"replanning provider returned invalid JSON: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ReplanError("replanning provider output must be a JSON object")
    required = {
        "consistent",
        "consistency_summary",
        "brief_markdown",
        "plan_markdown",
        "plan_graph",
        "package_mapping",
        "change_summary",
    }
    missing = sorted(required - set(raw))
    if missing:
        raise ReplanError("replanning provider output is missing: " + ", ".join(missing))
    unexpected = sorted(set(raw) - required)
    if unexpected:
        raise ReplanError("replanning provider output has unexpected fields: " + ", ".join(unexpected))
    if type(raw["consistent"]) is not bool:
        raise ReplanError("replanning provider field consistent must be a boolean")
    for name in ("consistency_summary", "brief_markdown", "plan_markdown", "change_summary"):
        if not isinstance(raw[name], str):
            raise ReplanError(f"replanning provider field {name} must be a string")
    if not isinstance(raw["plan_graph"], Mapping):
        raise ReplanError("replanning provider field plan_graph must be an object")
    if not isinstance(raw["package_mapping"], Mapping):
        raise ReplanError("replanning provider field package_mapping must be an object")
    mapping: dict[str, str] = {}
    for old, new in raw["package_mapping"].items():
        if not isinstance(old, str) or not isinstance(new, str):
            raise ReplanError("package_mapping keys and values must be strings")
        old_id = old.strip()
        new_id = new.strip()
        if not old_id or not new_id:
            raise ReplanError("package_mapping IDs cannot be empty")
        if old_id == new_id:
            raise ReplanError("package_mapping replacements must use a new package ID")
        mapping[old_id] = new_id
    return {
        "consistent": raw["consistent"],
        "consistency_summary": raw["consistency_summary"].strip(),
        "brief_markdown": raw["brief_markdown"],
        "plan_markdown": raw["plan_markdown"],
        "plan_graph": dict(raw["plan_graph"]),
        "package_mapping": mapping,
        "change_summary": raw["change_summary"].strip(),
    }


__all__ = ["AgentReplanProposal", "AgentReplanner"]
