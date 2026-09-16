"""Safe draft-plan generation and atomic publication for onboarding."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import yaml

from execraft.runtime.native import build_native_runtime
from execraft.onboarding.start_models import (
    DraftPlanArtifact,
    ImportedPlanConsistencyError,
    ProviderChoice,
    RepositoryScope,
    StartWorkflowError,
    TaskIntent,
)
from execraft.orchestrate.models import PlanGraph
from execraft.plan_contract import (
    DeclarativePlanGraphError,
    canonicalize_legacy_plan_graph_mapping,
    validate_declarative_plan_graph_mapping,
)
from execraft.onboarding.plan_validation import (
    PlanGraphValidationError,
    validate_plan_graph_mapping,
)
from execraft.orchestrate.scheduler import AgentExecutionError, StructuredHandoff
from execraft.project import ProjectDescriptor
from execraft.persistence import fsync_directory


# Keep imported-document AI review bounded.  The deterministic importer accepts
# larger files for archival fidelity, but sending multi-megabyte plans to a
# provider would defeat context budgeting and can exceed model windows.
_MAX_AGENT_IMPORT_CONTEXT_BYTES = 256 * 1024


def _normalize_generated_plan_graph(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Return declarative-only graph output from an Execraft planner.

    Older planning prompts/providers may still emit runtime defaults such as
    ``stage``, ``status``, ``verified`` or ``evidence``. Those values have no
    authority during task creation, so discard the known legacy fields and
    reject every other non-declarative extension. This guarantees that tasks
    generated from the current definition format can be replanned without migration.
    """

    if not isinstance(raw, Mapping):
        raise StartWorkflowError("generated plan graph must be a mapping")
    projection = canonicalize_legacy_plan_graph_mapping(raw)
    try:
        validate_declarative_plan_graph_mapping(projection.graph)
    except DeclarativePlanGraphError as exc:
        raise StartWorkflowError(f"generated plan is not declarative: {exc}") from exc
    return projection.graph


class DraftPlanService:
    """Generate, validate, and atomically publish executable draft plans."""

    def __init__(
        self,
        *,
        adapter_builder: Callable[..., Any] = build_native_runtime,
    ) -> None:
        self.adapter_builder = adapter_builder

    def local_draft(
        self,
        *,
        intent: TaskIntent,
        repositories: Sequence[str],
        fallback_reason: str = "",
    ) -> DraftPlanArtifact:
        repository_list = list(repositories)
        graph: dict[str, Any] = {
            "schema_version": 1,
            "source_document": "PLAN.md",
            "work_packages": [
                {
                    "id": "WP01",
                    "title": intent.title,
                    "dependencies": [],
                    "requirements": [intent.description],
                    "acceptance_criteria": [
                        {
                            "id": "wp01_requested_behavior",
                            "description": (
                                "The requested behavior is implemented across the selected "
                                "repository scope without unrelated regressions."
                            ),
                        },
                        {
                            "id": "wp01_verification",
                            "description": (
                                "Relevant automated verification passes and durable evidence "
                                "is recorded in the task dossier."
                            ),
                        },
                    ],
                    "affected_repositories": repository_list,
                    "risk": "medium",
                    "priority": 100,
                    "verification_profile": "focused",
                    "parallel_safe": False,
                }
            ],
        }
        markdown = (
            f"# Plan: {intent.title}\n\n"
            "## Status\n\n"
            "This is a valid bootstrap draft generated from the initial request. "
            "Review and decompose it before implementation when the change spans "
            "multiple independently verifiable concerns.\n\n"
            "## Intent\n\n"
            f"{intent.description}\n\n"
            "## Repository scope\n\n"
            + "\n".join(f"- `{repository}`" for repository in repository_list)
            + "\n\n## Work packages\n\n"
            f"### WP01 — {intent.title}\n\n"
            "Implement the requested behavior, preserve existing contracts, and "
            "record verification evidence.\n"
        )
        artifact = DraftPlanArtifact(
            markdown=markdown,
            graph=graph,
            generated_by="local",
            fallback_reason=fallback_reason,
        )
        self.validate(artifact, allowed_repositories=set(repository_list))
        return artifact

    def agent_draft(
        self,
        *,
        intent: TaskIntent,
        project: ProjectDescriptor,
        repository_scope: RepositoryScope,
        workspace_root: Path,
        provider: ProviderChoice,
    ) -> DraftPlanArtifact:
        if not provider.available or provider.config is None or provider.provider is None:
            raise StartWorkflowError(provider.reason)
        adapter = self.adapter_builder(
            provider.config,
            workdir=workspace_root,
            read_only=True,
        )
        repository_map = [
            {
                "id": item.id,
                "path": item.path,
                "role": item.role,
                "required": item.required,
                "mutability": item.mutability,
            }
            for item in project.repositories
            if item.id in repository_scope.repository_ids
        ]
        handoff = StructuredHandoff(
            work_package_id="START-PLAN",
            stage="plan",
            summary=(
                "Create a complete but appropriately scoped executable implementation "
                "plan for the task intent. Return exactly one JSON object with keys "
                "plan_markdown and plan_graph. plan_graph must contain schema_version=1 "
                "and a non-empty work_packages list. Every work package needs id, title, "
                "dependencies, requirements, acceptance_criteria, affected_repositories, "
                "risk, priority, and verification_profile. Do not modify files or execute "
                "project commands.\n\n"
                f"Task title: {intent.title}\n"
                f"Task intent: {intent.description}\n"
                f"Repository map: {json.dumps(repository_map, sort_keys=True)}"
            ),
            requirements=[intent.description],
            acceptance_criteria=[
                {
                    "id": "planning_contract",
                    "description": (
                        "The returned graph is executable, complete, acyclic, "
                        "and bounded to selected repositories."
                    ),
                }
            ],
            expected_output_schema={
                "type": "object",
                "required": ["plan_markdown", "plan_graph"],
                "properties": {
                    "plan_markdown": {"type": "string"},
                    "plan_graph": {"type": "object"},
                },
                "additionalProperties": False,
            },
            working_directory=str(workspace_root),
            read_only=True,
            required_isolation="provider_policy",
            execution_context={
                "project": project.id,
                "task_id": intent.task_id,
                "repositories": list(repository_scope.repository_ids),
            },
        ).for_attempt(1)
        try:
            result = adapter.execute(handoff)
        except AgentExecutionError as exc:
            raise StartWorkflowError(
                f"planning provider {provider.provider.name!r} failed "
                f"({exc.classification}): {exc}"
            ) from exc
        if not isinstance(result, Mapping):
            raise StartWorkflowError(
                f"planning provider {provider.provider.name!r} returned a non-mapping result"
            )
        final_message = str(result.get("final_message", "")).strip()
        payload = _parse_agent_plan_payload(final_message)
        artifact = DraftPlanArtifact(
            markdown=str(payload.get("plan_markdown", "")).strip(),
            graph=_normalize_generated_plan_graph(payload.get("plan_graph") or {}),
            generated_by="agent",
            provider_id=provider.provider.provider_id,
        )
        self.validate(
            artifact,
            allowed_repositories=set(repository_scope.repository_ids),
        )
        return artifact

    def local_graph_from_imported_plan(
        self,
        *,
        intent: TaskIntent,
        plan_markdown: str,
        repositories: Sequence[str],
        fallback_reason: str = "",
    ) -> DraftPlanArtifact:
        """Build a conservative executable graph without rewriting imported PLAN.md."""

        repository_list = list(repositories)
        graph: dict[str, Any] = {
            "schema_version": 1,
            "source_document": "PLAN.md",
            "work_packages": [
                {
                    "id": "WP01",
                    "title": intent.title,
                    "dependencies": [],
                    "requirements": [
                        "Implement the imported PLAN.md exactly within the selected repository scope."
                    ],
                    "acceptance_criteria": [
                        {
                            "id": "wp01_imported_plan",
                            "description": (
                                "The implementation satisfies the imported PLAN.md without silently "
                                "dropping its requirements or constraints."
                            ),
                        },
                        {
                            "id": "wp01_verification",
                            "description": (
                                "Relevant automated verification passes and durable evidence is "
                                "recorded in the task dossier."
                            ),
                        },
                    ],
                    "affected_repositories": repository_list,
                    "risk": "medium",
                    "priority": 100,
                    "verification_profile": "focused",
                    "parallel_safe": False,
                }
            ],
        }
        artifact = DraftPlanArtifact(
            markdown=plan_markdown,
            graph=graph,
            generated_by="local-import",
            fallback_reason=fallback_reason,
            consistency_mode="structural",
            consistency_summary=(
                "Imported BRIEF.md/PLAN.md were structurally validated; semantic consistency "
                "was not delegated to a provider."
            ),
        )
        self.validate(artifact, allowed_repositories=set(repository_list))
        return artifact

    def agent_graph_from_imported_plan(
        self,
        *,
        intent: TaskIntent,
        brief_markdown: str,
        plan_markdown: str,
        project: ProjectDescriptor,
        repository_scope: RepositoryScope,
        workspace_root: Path,
        provider: ProviderChoice,
    ) -> DraftPlanArtifact:
        """Validate BRIEF/PLAN coherence and generate only the missing executable graph."""

        context_bytes = len(brief_markdown.encode("utf-8")) + len(
            plan_markdown.encode("utf-8")
        )
        if context_bytes > _MAX_AGENT_IMPORT_CONTEXT_BYTES:
            raise StartWorkflowError(
                "imported BRIEF.md and PLAN.md require "
                f"{context_bytes} bytes of provider context, exceeding the "
                f"{_MAX_AGENT_IMPORT_CONTEXT_BYTES}-byte import consistency budget; "
                "supply PLAN.graph.yaml or use local/auto planning"
            )
        if not provider.available or provider.config is None or provider.provider is None:
            raise StartWorkflowError(provider.reason)
        adapter = self.adapter_builder(provider.config, workdir=workspace_root, read_only=True)
        repository_map = [
            {
                "id": item.id,
                "path": item.path,
                "role": item.role,
                "required": item.required,
                "mutability": item.mutability,
            }
            for item in project.repositories
            if item.id in repository_scope.repository_ids
        ]
        handoff = StructuredHandoff(
            work_package_id="START-IMPORT-PLAN",
            stage="plan",
            summary=(
                "Validate that the supplied BRIEF.md and PLAN.md are semantically coherent, then "
                "derive the executable PLAN.graph.yaml without rewriting either imported document. "
                "Return exactly one JSON object with keys consistent, consistency_summary, and "
                "plan_graph. Set consistent=false if the documents materially contradict each "
                "other. plan_graph must contain schema_version=1 and a non-empty work_packages "
                "list. Every package needs id, title, dependencies, requirements, "
                "acceptance_criteria, affected_repositories, risk, priority, and "
                "verification_profile. Do not modify files or execute project commands.\n\n"
                f"Task title: {intent.title}\n"
                f"Repository map: {json.dumps(repository_map, sort_keys=True)}\n\n"
                f"BRIEF.md:\n{brief_markdown}\n\nPLAN.md:\n{plan_markdown}"
            ),
            requirements=[
                "Preserve the imported PLAN.md as the authoritative human-readable plan.",
                "Reject material contradictions between BRIEF.md and PLAN.md.",
            ],
            acceptance_criteria=[
                {
                    "id": "import_consistency",
                    "description": "The imported documents are coherent and the graph covers the plan.",
                }
            ],
            expected_output_schema={
                "type": "object",
                "required": ["consistent", "consistency_summary", "plan_graph"],
                "properties": {
                    "consistent": {"type": "boolean"},
                    "consistency_summary": {"type": "string"},
                    "plan_graph": {"type": "object"},
                },
                "additionalProperties": False,
            },
            working_directory=str(workspace_root),
            read_only=True,
            required_isolation="provider_policy",
            execution_context={
                "project": project.id,
                "task_id": intent.task_id,
                "repositories": list(repository_scope.repository_ids),
                "mode": "imported_plan_graph_generation",
            },
        ).for_attempt(1)
        try:
            result = adapter.execute(handoff)
        except AgentExecutionError as exc:
            raise StartWorkflowError(
                f"planning provider {provider.provider.name!r} failed "
                f"({exc.classification}): {exc}"
            ) from exc
        if not isinstance(result, Mapping):
            raise StartWorkflowError(
                f"planning provider {provider.provider.name!r} returned a non-mapping result"
            )
        final_message = str(result.get("final_message", "")).strip()
        payload = _parse_agent_import_graph_payload(final_message)
        if not bool(payload.get("consistent")):
            summary = str(payload.get("consistency_summary", "")).strip()
            raise ImportedPlanConsistencyError(
                "imported BRIEF.md and PLAN.md are materially inconsistent"
                + (f": {summary}" if summary else "")
            )
        artifact = DraftPlanArtifact(
            markdown=plan_markdown,
            graph=_normalize_generated_plan_graph(payload.get("plan_graph") or {}),
            generated_by="agent-import",
            provider_id=provider.provider.provider_id,
            consistency_mode="agent",
            consistency_summary=str(payload.get("consistency_summary", "")).strip(),
        )
        self.validate(artifact, allowed_repositories=set(repository_scope.repository_ids))
        return artifact

    @staticmethod
    def validate(
        artifact: DraftPlanArtifact,
        *,
        allowed_repositories: set[str],
    ) -> PlanGraph:
        if not artifact.markdown.strip():
            raise StartWorkflowError("generated PLAN.md is empty")
        if not isinstance(artifact.graph, Mapping):
            raise StartWorkflowError("generated plan graph must be a mapping")
        try:
            return validate_plan_graph_mapping(
                artifact.graph, allowed_repositories=allowed_repositories
            )
        except PlanGraphValidationError as exc:
            raise StartWorkflowError(f"invalid generated plan: {exc}") from exc

    def publish_graph(
        self,
        *,
        dossier: Path,
        artifact: DraftPlanArtifact,
        allowed_repositories: set[str],
    ) -> Path:
        """Publish only PLAN.graph.yaml, preserving imported PLAN.md exactly."""

        self.validate(artifact, allowed_repositories=allowed_repositories)
        graph_text = yaml.safe_dump(
            dict(artifact.graph),
            sort_keys=False,
            width=1000,
        )
        path = dossier / "PLAN.graph.yaml"
        _write_bytes_atomic(path, graph_text.encode("utf-8"))
        fsync_directory(dossier)
        return path

    def publish(
        self,
        *,
        dossier: Path,
        artifact: DraftPlanArtifact,
        allowed_repositories: set[str],
    ) -> tuple[Path, Path]:
        self.validate(artifact, allowed_repositories=allowed_repositories)
        graph_text = yaml.safe_dump(
            dict(artifact.graph),
            sort_keys=False,
            width=1000,
        )
        # PLAN.graph.yaml is the executable commit marker, so Markdown is
        # replaced first and the graph last. A process interruption can at worst
        # leave new prose beside the previous executable graph, never a partially
        # committed executable graph beside missing prose.
        files = (
            (dossier / "PLAN.md", artifact.markdown.rstrip() + "\n"),
            (dossier / "PLAN.graph.yaml", graph_text),
        )
        originals: dict[Path, bytes | None] = {
            path: path.read_bytes() if path.is_file() else None for path, _ in files
        }
        temporaries: dict[Path, Path] = {}
        replaced: list[Path] = []
        try:
            for path, content in files:
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=f".{path.name}.",
                    suffix=".tmp",
                    dir=str(dossier),
                )
                temporary = Path(temporary_name)
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                temporaries[path] = temporary
            for path, _content in files:
                os.replace(temporaries[path], path)
                replaced.append(path)
            fsync_directory(dossier)
        except Exception:
            for temporary in temporaries.values():
                temporary.unlink(missing_ok=True)
            for path in reversed(replaced):
                original = originals[path]
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    _write_bytes_atomic(path, original)
            fsync_directory(dossier)
            raise
        finally:
            for temporary in temporaries.values():
                temporary.unlink(missing_ok=True)
        return dossier / "PLAN.md", dossier / "PLAN.graph.yaml"



def _write_bytes_atomic(path: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.restore.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _parse_agent_plan_payload(text: str) -> Mapping[str, Any]:
    if not text:
        raise StartWorkflowError("planning provider returned an empty response")
    candidates = [text.strip()]
    fenced = re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    candidates[:0] = [item.strip() for item in fenced]
    first = text.find("{")
    last = text.rfind("}")
    if first >= 0 and last > first:
        candidates.append(text[first : last + 1])
    errors: list[str] = []
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError as exc:
            errors.append(str(exc))
            continue
        if not isinstance(payload, Mapping):
            errors.append("top-level response is not an object")
            continue
        if "plan_markdown" not in payload or "plan_graph" not in payload:
            errors.append("response lacks plan_markdown or plan_graph")
            continue
        return payload
    raise StartWorkflowError(
        "planning provider did not return the required JSON object: "
        + "; ".join(errors[-3:])
    )


def _parse_agent_import_graph_payload(text: str) -> Mapping[str, Any]:
    if not text:
        raise StartWorkflowError("planning provider returned an empty response")
    candidates = [text.strip()]
    fenced = re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    candidates[:0] = [item.strip() for item in fenced]
    first = text.find("{")
    last = text.rfind("}")
    if first >= 0 and last > first:
        candidates.append(text[first : last + 1])
    errors: list[str] = []
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError as exc:
            errors.append(str(exc))
            continue
        if not isinstance(payload, Mapping):
            errors.append("top-level response is not an object")
            continue
        if not {"consistent", "consistency_summary", "plan_graph"}.issubset(payload):
            errors.append("response lacks consistent, consistency_summary, or plan_graph")
            continue
        if not isinstance(payload.get("consistent"), bool):
            errors.append("consistent must be a boolean")
            continue
        if not isinstance(payload.get("consistency_summary"), str):
            errors.append("consistency_summary must be a string")
            continue
        if not isinstance(payload.get("plan_graph"), Mapping):
            errors.append("plan_graph must be an object")
            continue
        return payload
    raise StartWorkflowError(
        "planning provider did not return the required import-plan JSON object: "
        + "; ".join(errors[-3:])
    )

__all__ = ["DraftPlanService"]
