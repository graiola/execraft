"""Resumable one-command project/task/workspace/planning workflow.

The start workflow composes project discovery, task-definition, workspace, and
planning services. Durable phases are journaled independently, project discovery never
executes source code, and external planning providers are restricted to
read-only execution.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

import yaml

from execraft.control_plane import ControlPlaneHome
from execraft.onboarding.planning import DraftPlanService
from execraft.onboarding.selection import ProviderSelector, RepositorySelector
from execraft.onboarding.service import OnboardingOutcome, OnboardingService
from execraft.onboarding.task_definition import (
    PreparedTaskDefinition,
    TaskDefinitionService,
)
from execraft.onboarding.start_models import (
    DraftPlanArtifact,
    ImportedPlanConsistencyError,
    PlannerMode,
    ProviderChoice,
    RepositoryScope,
    StartOutcome,
    StartRequest,
    StartStep,
    StartWorkflowError,
    StepStatus,
    TaskIntent,
)
from execraft.orchestrate.task_status import sync_runtime_status
from execraft.persistence.atomic import atomic_write_yaml
from execraft.project import (
    ProjectDescriptor,
    ProjectNotFoundError,
    load_project,
    resolve_current_project,
)
from execraft.workspace.lifecycle import WorkspacePreparationResult, prepare_workspace
from execraft.workspace.task_git import (
    TaskManifest,
    local_manifest_registry_path,
    project_task_directory,
    validate_task_id,
    write_manifest,
)
from execraft.workspace.workspace_git import default_workspace_path

@dataclass
class StartJournal:
    """Atomic, resumable record for one start workflow."""

    path: Path
    project_id: str
    task_id: str
    intent_sha256: str
    source_root: str
    description: str = ""
    request: dict[str, Any] = field(default_factory=dict)
    steps: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load_or_create(
        cls,
        *,
        state_root: Path,
        project_id: str,
        task_id: str,
        intent: TaskIntent,
        source_root: Path,
        request: StartRequest | None = None,
    ) -> "StartJournal":
        path = state_root / "starts" / project_id / f"{task_id}.yaml"
        if path.is_file():
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, Mapping):
                raise StartWorkflowError(f"start journal {path} must contain a mapping")
            expected = {
                "project": project_id,
                "task_id": task_id,
                "intent_sha256": intent.intent_sha256,
                "source_root": str(source_root),
            }
            mismatches = [
                f"{key}={raw.get(key)!r} (expected {value!r})"
                for key, value in expected.items()
                if str(raw.get(key, "")) != value
            ]
            if mismatches:
                raise StartWorkflowError(
                    f"start journal {path} does not match this workflow: "
                    + "; ".join(mismatches)
                )
            raw_steps = raw.get("steps") or {}
            if not isinstance(raw_steps, Mapping):
                raise StartWorkflowError(f"start journal {path} has invalid steps")
            return cls(
                path=path,
                project_id=project_id,
                task_id=task_id,
                intent_sha256=intent.intent_sha256,
                source_root=str(source_root),
                description=str(raw.get("description", "")) or intent.description,
                request=(
                    dict(raw.get("request") or {})
                    if isinstance(raw.get("request") or {}, Mapping)
                    else {}
                ),
                steps={
                    str(key): dict(value)
                    for key, value in raw_steps.items()
                    if isinstance(value, Mapping)
                },
            )
        return cls(
            path=path,
            project_id=project_id,
            task_id=task_id,
            intent_sha256=intent.intent_sha256,
            source_root=str(source_root),
            description=intent.description,
            request=_journal_request_mapping(
                request,
                request_sha256=intent.intent_sha256,
                resolved_title=intent.title,
            ),
        )

    def update(
        self,
        step_id: str,
        status: StepStatus,
        summary: str,
        **details: Any,
    ) -> None:
        self.steps[step_id] = {
            "status": status.value,
            "summary": summary,
            "details": details,
        }
        self.write()

    def write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "project": self.project_id,
            "task_id": self.task_id,
            "intent_sha256": self.intent_sha256,
            "source_root": self.source_root,
            "description": self.description,
            "request": self.request,
            "steps": self.steps,
        }
        atomic_write_yaml(self.path, payload, sort_keys=False, width=1000)



def _journal_request_mapping(
    request: StartRequest | None,
    *,
    request_sha256: str = "",
    resolved_title: str = "",
) -> dict[str, Any]:
    if request is None:
        return {}
    return {
        # Persist the resolved title so an imported-definition start can be
        # resumed without retransmitting the original documents merely to
        # re-derive an H1/package title.
        "title": resolved_title or request.title,
        "repositories": list(request.repository_ids),
        "provider_id": request.provider_id,
        "planner": request.planner_mode.value,
        "project_template": request.project_template,
        "task_template": request.task_template,
        "workspace_root": str(request.workspace_root) if request.workspace_root else "",
        "policy": request.policy_profile,
        "no_workspace": request.no_workspace,
        "reuse_in_place": request.reuse_in_place,
        "accept_decisions": request.accept_decisions,
        "require_provider": request.require_provider,
        "force_plan": request.force_plan,
        "expected_request_sha256": request_sha256 or request.expected_request_sha256,
        "definition_imports": sorted(request.task_definition.documents()),
        "definition_sources": dict(request.task_definition.sources),
    }


class StartWorkflowService:
    """Coordinate project initialization, task creation, workspace, and planning."""

    def __init__(
        self,
        *,
        home: ControlPlaneHome,
        onboarding: OnboardingService,
        repository_selector: RepositorySelector | None = None,
        provider_selector: ProviderSelector | None = None,
        planner: DraftPlanService | None = None,
    ) -> None:
        self.home = home
        self.onboarding = onboarding
        self.repository_selector = repository_selector or RepositorySelector()
        self.provider_selector = provider_selector or ProviderSelector()
        self.planner = planner or DraftPlanService()
        self.task_definitions = TaskDefinitionService()

    def preview(self, request: StartRequest) -> StartOutcome:
        return self._execute(request, dry_run=True)

    def run(self, request: StartRequest) -> StartOutcome:
        return self._execute(request, dry_run=False)

    def _execute(self, request: StartRequest, *, dry_run: bool) -> StartOutcome:
        source_root = request.source_root.expanduser().resolve()
        definition = request.task_definition
        description = " ".join(request.description.split()) or definition.intent_text()
        if not description:
            raise StartWorkflowError(
                "start requires a task description, BRIEF.md, PLAN.md, or PLAN.graph.yaml"
            )
        identity_material = description
        digest_override = ""
        if definition.supplied:
            identity_material = f"{description}\0{definition.source_fingerprint()}"
        elif request.expected_request_sha256:
            digest_override = request.expected_request_sha256
        intent = TaskIntent.create(
            description,
            title=request.title or definition.suggested_title(),
            task_id=request.task_id,
            identity_material=identity_material,
            digest_override=digest_override,
        )
        if (
            definition.supplied
            and request.expected_request_sha256
            and request.expected_request_sha256 != intent.intent_sha256
        ):
            raise StartWorkflowError(
                "imported task definition does not match the expected request SHA-256"
            )
        project, project_outcome, project_step = self._resolve_project(
            request,
            source_root=source_root,
            dry_run=dry_run,
        )
        intent = self._resolve_task_identity(project, intent)
        scope = self.repository_selector.select(
            project,
            intent.description,
            request.repository_ids,
        )
        workspace_root = (
            None
            if request.no_workspace
            else (
                request.workspace_root.expanduser().resolve()
                if request.workspace_root
                else default_workspace_path(self.home.root, intent.task_id, project_id=project.id)
            )
        )
        provider = self.provider_selector.select(
            project,
            workdir=workspace_root or source_root,
            requested=request.provider_id,
        )
        if request.require_provider and not provider.available:
            raise StartWorkflowError(provider.reason)
        planning_already_executable = definition.has_plan_graph
        if (
            request.planner_mode is PlannerMode.AGENT
            and not planning_already_executable
            and not provider.available
        ):
            raise StartWorkflowError(provider.reason)

        task_outcome: OnboardingOutcome | None = None
        steps: list[StartStep] = [project_step]
        if dry_run:
            dossier = project_task_directory(self.home.root, project.id, intent.task_id)
            if dossier.is_dir() and (dossier / "TASK.yaml").is_file():
                manifest = self._load_existing_task(dossier)
                self._validate_task_reuse(
                    manifest=manifest,
                    dossier=dossier,
                    project=project,
                    intent=intent,
                    scope=scope,
                )
                steps.append(
                    StartStep(
                        "task",
                        StepStatus.REUSED,
                        "Existing task dossier will be reused",
                    )
                )
            else:
                task_outcome = self.onboarding.create_task(
                    control_root=self.home.root,
                    project=project,
                    task_id=intent.task_id,
                    title=intent.title,
                    repository_ids=scope.repository_ids,
                    brief=intent.description,
                    template_id=request.task_template,
                    state_root=self.home.state_dir,
                    definition=definition,
                    request_sha256=intent.intent_sha256,
                    dry_run=True,
                )
                steps.append(
                    StartStep("task", StepStatus.READY, "Task dossier will be created")
                )
            steps.append(
                StartStep(
                    "workspace",
                    StepStatus.SKIPPED if request.no_workspace else StepStatus.READY,
                    "Workspace creation disabled"
                    if request.no_workspace
                    else "Task worktrees and generated shell will be prepared",
                )
            )
            prepared_preview = self.task_definitions.prepare(
                definition=definition,
                title=intent.title,
                fallback_brief=intent.description,
                request_sha256=intent.intent_sha256,
                created_at="preview",
                allowed_repositories=set(scope.repository_ids),
            )
            plan_artifact = self._preview_plan(
                intent=intent,
                scope=scope,
                prepared=prepared_preview,
            )
            steps.append(
                StartStep(
                    "plan",
                    StepStatus.READY,
                    "A validated draft plan will be generated",
                    {
                        "mode": request.planner_mode.value,
                        "provider": provider.provider.name if provider.provider else "",
                    },
                )
            )
            return StartOutcome(
                project_id=project.id,
                task_id=intent.task_id,
                source_root=source_root,
                workspace_root=workspace_root,
                provider=provider,
                repository_scope=scope,
                steps=tuple(steps),
                project_plan=project_outcome.plan if project_outcome else None,
                task_plan=task_outcome.plan if task_outcome else None,
                plan_artifact=plan_artifact,
                applied=False,
            )

        self.home.ensure_layout()
        journal = StartJournal.load_or_create(
            state_root=self.home.state_dir,
            project_id=project.id,
            task_id=intent.task_id,
            intent=intent,
            source_root=source_root,
            request=request,
        )
        journal.update("project", project_step.status, project_step.summary)

        try:
            manifest, task_step, task_outcome = self._ensure_task(
                project=project,
                intent=intent,
                scope=scope,
                request=request,
            )
        except Exception as exc:
            journal.update("task", StepStatus.FAILED, str(exc))
            raise
        steps.append(task_step)
        journal.update("task", task_step.status, task_step.summary)

        workspace_result: WorkspacePreparationResult | None = None
        if request.no_workspace:
            workspace_step = StartStep(
                "workspace",
                StepStatus.SKIPPED,
                "Workspace creation disabled",
            )
        else:
            assert workspace_root is not None
            try:
                workspace_result = prepare_workspace(
                    control_root=self.home.root,
                    manifest=manifest,
                    project=project,
                    source_root_override=source_root,
                    workspace_root=workspace_root,
                    reuse_in_place=request.reuse_in_place,
                    policy_profile=request.policy_profile or None,
                    bind_source=True,
                    reuse_existing=True,
                )
            except Exception as exc:
                journal.update("workspace", StepStatus.FAILED, str(exc))
                raise
            workspace_step = StartStep(
                "workspace",
                StepStatus.COMPLETED if workspace_result.created else StepStatus.REUSED,
                (
                    "Workspace prepared"
                    if workspace_result.created
                    else "Existing ready workspace reused"
                ),
                {"path": str(workspace_result.workspace_root)},
            )
        steps.append(workspace_step)
        journal.update("workspace", workspace_step.status, workspace_step.summary)

        dossier = project_task_directory(self.home.root, project.id, intent.task_id)
        try:
            plan_artifact, plan_step = self._ensure_plan(
                project=project,
                intent=intent,
                scope=scope,
                provider=provider,
                request=request,
                workspace_root=(
                    workspace_result.workspace_root
                    if workspace_result is not None
                    else source_root
                ),
                dossier=dossier,
            )
        except Exception as exc:
            journal.update("plan", StepStatus.FAILED, str(exc))
            raise
        steps.append(plan_step)
        journal.update(
            "plan",
            plan_step.status,
            plan_step.summary,
            **dict(plan_step.details),
        )

        try:
            if manifest.status in {"draft", "briefed"}:
                manifest.status = "planned"
                write_manifest(self.home.root, manifest)
            sync_runtime_status(
                dossier,
                task_id=manifest.id,
                project_id=manifest.project,
                state_root=self.home.state_dir,
            )
        except Exception as exc:
            journal.update("complete", StepStatus.FAILED, str(exc))
            raise
        journal.update("complete", StepStatus.COMPLETED, "Start workflow completed")
        return StartOutcome(
            project_id=project.id,
            task_id=intent.task_id,
            source_root=source_root,
            workspace_root=workspace_root,
            provider=provider,
            repository_scope=scope,
            steps=tuple(steps),
            project_plan=project_outcome.plan if project_outcome else None,
            task_plan=task_outcome.plan if task_outcome else None,
            plan_artifact=plan_artifact,
            journal_path=journal.path,
            applied=True,
        )

    def _resolve_project(
        self,
        request: StartRequest,
        *,
        source_root: Path,
        dry_run: bool,
    ) -> tuple[ProjectDescriptor, OnboardingOutcome | None, StartStep]:
        try:
            project = resolve_current_project(
                self.home.root,
                project_id=request.project_id or None,
                cwd=source_root,
            )
            return (
                project,
                None,
                StartStep("project", StepStatus.REUSED, "Registered project reused"),
            )
        except ProjectNotFoundError:
            pass
        report = self.onboarding.inspect_project(source_root)
        if not report.can_scaffold:
            messages = "; ".join(item.message for item in report.findings)
            raise StartWorkflowError(messages or "source tree cannot be initialized")
        if request.project_id and request.project_id != report.project_id:
            raise StartWorkflowError(
                f"requested project {request.project_id!r} differs from discovered "
                f"project {report.project_id!r}"
            )
        outcome = self.onboarding.create_project(
            report=report,
            output_dir=self.home.projects_dir,
            template_id=request.project_template,
            register=not dry_run,
            dry_run=dry_run,
            accept_decisions=request.accept_decisions,
        )
        if dry_run:
            with tempfile.TemporaryDirectory(prefix="execraft-start-project-") as temporary:
                temporary_parent = Path(temporary)
                staged = self.onboarding.create_project(
                    report=report,
                    output_dir=temporary_parent,
                    template_id=request.project_template,
                    register=False,
                    dry_run=False,
                    # The temporary descriptor is used only to calculate the
                    # remainder of the preview. Pending decisions remain present
                    # on ``outcome.plan`` and are never accepted persistently.
                    accept_decisions=True,
                )
                assert staged.path is not None
                loaded = load_project(staged.path.parent)
                project = replace(loaded, directory=self.home.projects_dir / report.project_id)
            if outcome.plan.can_apply:
                status = StepStatus.READY
                summary = "Project descriptor will be generated and registered"
            else:
                status = StepStatus.BLOCKED
                pending = ", ".join(
                    finding.code for finding in outcome.plan.pending_decisions
                )
                summary = (
                    "Project discovery requires explicit decisions"
                    + (f": {pending}" if pending else "")
                )
        else:
            assert outcome.path is not None
            project = load_project(outcome.path.parent)
            status = StepStatus.COMPLETED
            summary = "Project descriptor generated and registered"
        return project, outcome, StartStep("project", status, summary)

    def _resolve_task_identity(
        self,
        project: ProjectDescriptor,
        intent: TaskIntent,
    ) -> TaskIntent:
        base = intent.task_id
        candidate = base
        index = 2
        while (
            project_task_directory(self.home.root, project.id, candidate).exists()
            or local_manifest_registry_path(self.home.root, candidate).exists()
        ):
            journal_path = self.home.state_dir / "starts" / project.id / f"{candidate}.yaml"
            if journal_path.is_file():
                raw = yaml.safe_load(journal_path.read_text(encoding="utf-8")) or {}
                if not isinstance(raw, Mapping):
                    raise StartWorkflowError(
                        f"start journal {journal_path} must contain a mapping"
                    )
                if str(raw.get("intent_sha256", "")) == intent.intent_sha256:
                    return intent.with_task_id(candidate)
            if intent.explicit_task_id:
                return intent.with_task_id(candidate)
            candidate = validate_task_id(f"{base[:54]}-{index}")
            index += 1
        return intent.with_task_id(candidate)

    @staticmethod
    def _load_existing_task(dossier: Path) -> TaskManifest:
        task_path = dossier / "TASK.yaml"
        try:
            raw = yaml.safe_load(task_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise StartWorkflowError(f"cannot read existing task manifest {task_path}: {exc}") from exc
        if not isinstance(raw, Mapping):
            raise StartWorkflowError(f"existing task manifest {task_path} must be a mapping")
        return TaskManifest.from_mapping(raw)

    @staticmethod
    def _validate_task_reuse(
        *,
        manifest: TaskManifest,
        dossier: Path,
        project: ProjectDescriptor,
        intent: TaskIntent,
        scope: RepositoryScope,
    ) -> None:
        problems: list[str] = []
        if manifest.project != project.id:
            problems.append(f"project is {manifest.project!r}, expected {project.id!r}")
        if manifest.title != intent.title:
            problems.append(f"title is {manifest.title!r}, expected {intent.title!r}")
        if manifest.status in {"merged", "closed", "abandoned"}:
            problems.append(
                f"lifecycle status {manifest.status!r} is terminal and cannot be restarted"
            )
        recorded_scope = tuple(item.id for item in manifest.repositories)
        if recorded_scope != scope.repository_ids:
            problems.append(
                "repository scope is "
                f"{list(recorded_scope)!r}, expected {list(scope.repository_ids)!r}"
            )
        metadata = TaskDefinitionService.load_metadata(dossier)
        stored_request_sha = str(metadata.get("request_sha256", ""))
        imported_fingerprint = str(metadata.get("source_fingerprint", ""))
        if stored_request_sha and stored_request_sha != intent.intent_sha256 and imported_fingerprint:
            problems.append(
                "task definition request SHA-256 differs from the original start request"
            )
        elif not stored_request_sha or stored_request_sha != intent.intent_sha256:
            brief_path = dossier / "BRIEF.md"
            try:
                brief = brief_path.read_text(encoding="utf-8")
            except OSError as exc:
                problems.append(f"brief cannot be read: {exc}")
            else:
                normalized_brief = " ".join(brief.split())
                if intent.description not in normalized_brief:
                    problems.append("brief does not contain the normalized start intent")
        if problems:
            raise StartWorkflowError(
                f"existing task {intent.task_id!r} does not match this start request: "
                + "; ".join(problems)
            )

    def _ensure_task(
        self,
        *,
        project: ProjectDescriptor,
        intent: TaskIntent,
        scope: RepositoryScope,
        request: StartRequest,
    ) -> tuple[TaskManifest, StartStep, OnboardingOutcome | None]:
        dossier = project_task_directory(self.home.root, project.id, intent.task_id)
        if dossier.is_dir() and (dossier / "TASK.yaml").is_file():
            manifest = self._load_existing_task(dossier)
            self._validate_task_reuse(
                manifest=manifest,
                dossier=dossier,
                project=project,
                intent=intent,
                scope=scope,
            )
            return (
                manifest,
                StartStep("task", StepStatus.REUSED, "Existing task dossier reused"),
                None,
            )
        outcome = self.onboarding.create_task(
            control_root=self.home.root,
            project=project,
            task_id=intent.task_id,
            title=intent.title,
            repository_ids=scope.repository_ids,
            brief=intent.description,
            template_id=request.task_template,
            state_root=self.home.state_dir,
            definition=request.task_definition,
            request_sha256=intent.intent_sha256,
            dry_run=False,
        )
        manifest = self._load_existing_task(dossier)
        return (
            manifest,
            StartStep("task", StepStatus.COMPLETED, "Task dossier created"),
            outcome,
        )

    def _preview_plan(
        self,
        *,
        intent: TaskIntent,
        scope: RepositoryScope,
        prepared: PreparedTaskDefinition,
    ) -> DraftPlanArtifact:
        fallback = (
            "Preview uses deterministic validation; providers are never invoked during dry-run."
        )
        if prepared.plan_graph_yaml:
            raw = yaml.safe_load(prepared.plan_graph_yaml) or {}
            artifact = DraftPlanArtifact(
                markdown=prepared.plan_markdown,
                graph=raw if isinstance(raw, Mapping) else {},
                generated_by="imported",
                consistency_mode="structural",
                consistency_summary="Imported executable graph validated during preview.",
            )
            self.planner.validate(
                artifact, allowed_repositories=set(scope.repository_ids)
            )
            return artifact
        if prepared.imported_plan:
            return self.planner.local_graph_from_imported_plan(
                intent=intent,
                plan_markdown=prepared.plan_markdown,
                repositories=scope.repository_ids,
                fallback_reason=fallback,
            )
        return self.planner.local_draft(
            intent=intent,
            repositories=scope.repository_ids,
            fallback_reason=fallback,
        )

    def _ensure_plan(
        self,
        *,
        project: ProjectDescriptor,
        intent: TaskIntent,
        scope: RepositoryScope,
        provider: ProviderChoice,
        request: StartRequest,
        workspace_root: Path,
        dossier: Path,
    ) -> tuple[DraftPlanArtifact, StartStep]:
        graph_path = dossier / "PLAN.graph.yaml"
        markdown_path = dossier / "PLAN.md"
        if not request.force_plan and graph_path.is_file() and markdown_path.is_file():
            raw = yaml.safe_load(graph_path.read_text(encoding="utf-8")) or {}
            artifact = DraftPlanArtifact(
                markdown=markdown_path.read_text(encoding="utf-8"),
                graph=raw if isinstance(raw, Mapping) else {},
                generated_by="existing",
            )
            try:
                self.planner.validate(
                    artifact,
                    allowed_repositories=set(scope.repository_ids),
                )
            except StartWorkflowError:
                pass
            else:
                return (
                    artifact,
                    StartStep(
                        "plan",
                        StepStatus.REUSED,
                        "Existing valid draft plan reused",
                    ),
                )

        imported_plan = self.task_definitions.imported_plan(dossier)
        artifact: DraftPlanArtifact
        if imported_plan:
            plan_markdown = markdown_path.read_text(encoding="utf-8")
            brief_markdown = (dossier / "BRIEF.md").read_text(encoding="utf-8")
            if request.planner_mode is PlannerMode.LOCAL:
                artifact = self.planner.local_graph_from_imported_plan(
                    intent=intent,
                    plan_markdown=plan_markdown,
                    repositories=scope.repository_ids,
                )
            elif request.planner_mode is PlannerMode.AGENT:
                artifact = self.planner.agent_graph_from_imported_plan(
                    intent=intent,
                    brief_markdown=brief_markdown,
                    plan_markdown=plan_markdown,
                    project=project,
                    repository_scope=scope,
                    workspace_root=workspace_root,
                    provider=provider,
                )
            elif provider.available:
                try:
                    artifact = self.planner.agent_graph_from_imported_plan(
                        intent=intent,
                        brief_markdown=brief_markdown,
                        plan_markdown=plan_markdown,
                        project=project,
                        repository_scope=scope,
                        workspace_root=workspace_root,
                        provider=provider,
                    )
                except ImportedPlanConsistencyError:
                    raise
                except StartWorkflowError as exc:
                    artifact = self.planner.local_graph_from_imported_plan(
                        intent=intent,
                        plan_markdown=plan_markdown,
                        repositories=scope.repository_ids,
                        fallback_reason=str(exc),
                    )
            else:
                artifact = self.planner.local_graph_from_imported_plan(
                    intent=intent,
                    plan_markdown=plan_markdown,
                    repositories=scope.repository_ids,
                    fallback_reason=provider.reason,
                )
            self.planner.publish_graph(
                dossier=dossier,
                artifact=artifact,
                allowed_repositories=set(scope.repository_ids),
            )
        else:
            if request.planner_mode is PlannerMode.LOCAL:
                artifact = self.planner.local_draft(
                    intent=intent,
                    repositories=scope.repository_ids,
                )
            elif request.planner_mode is PlannerMode.AGENT:
                artifact = self.planner.agent_draft(
                    intent=intent,
                    project=project,
                    repository_scope=scope,
                    workspace_root=workspace_root,
                    provider=provider,
                )
            elif provider.available:
                try:
                    artifact = self.planner.agent_draft(
                        intent=intent,
                        project=project,
                        repository_scope=scope,
                        workspace_root=workspace_root,
                        provider=provider,
                    )
                except StartWorkflowError as exc:
                    artifact = self.planner.local_draft(
                        intent=intent,
                        repositories=scope.repository_ids,
                        fallback_reason=str(exc),
                    )
            else:
                artifact = self.planner.local_draft(
                    intent=intent,
                    repositories=scope.repository_ids,
                    fallback_reason=provider.reason,
                )
            self.planner.publish(
                dossier=dossier,
                artifact=artifact,
                allowed_repositories=set(scope.repository_ids),
            )
        self.task_definitions.refresh_generated_plan(
            dossier,
            generated_by=artifact.generated_by,
            provider_id=artifact.provider_id,
            consistency_mode=artifact.consistency_mode,
            consistency_summary=artifact.consistency_summary,
        )
        summary = (
            f"Draft plan generated by provider {artifact.provider_id}"
            if artifact.generated_by in {"agent", "agent-import"}
            else "Validated deterministic draft plan generated"
        )
        return (
            artifact,
            StartStep(
                "plan",
                StepStatus.COMPLETED,
                summary,
                artifact.as_mapping(),
            ),
        )



__all__ = [
    "DraftPlanArtifact",
    "DraftPlanService",
    "ImportedPlanConsistencyError",
    "PlannerMode",
    "ProviderChoice",
    "ProviderSelector",
    "RepositoryScope",
    "RepositorySelector",
    "StartOutcome",
    "StartRequest",
    "StartStep",
    "StartWorkflowError",
    "StepStatus",
    "TaskIntent",
]
