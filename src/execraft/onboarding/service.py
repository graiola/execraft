"""Shared application service for project and task onboarding."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from execraft.onboarding.discovery import DiscoveryEngine, DiscoveryReport
from execraft.onboarding.models import CreationPlan, ReadinessReport
from execraft.onboarding.profiles import (
    ProjectProfileCatalog,
    ProjectTemplateContext,
    default_profile_catalog,
)
from execraft.onboarding.readiness import ReadinessService
from execraft.onboarding.templates import TaskTemplateContext, TemplateCatalog
from execraft.onboarding.task_definition import (
    TaskDefinitionInput,
    TaskDefinitionService,
)
from execraft.onboarding.transactions import (
    ProjectCreationTransaction,
    TaskCreationTransaction,
)
from execraft.orchestrate.task_status import sync_runtime_status
from execraft.project import (
    ProjectDescriptor,
    ProjectRepository,
    register_project_descriptor,
    validate_project_id,
)
from execraft.workspace.task_git import (
    TaskGitError,
    RepositorySpec,
    TaskManifest,
    local_manifest_registry_path,
    project_task_directory,
    utc_now,
    validate_manifest,
    validate_task_id,
)


@dataclass(frozen=True)
class OnboardingOutcome:
    """Result returned by a dry-run or committed onboarding operation."""

    plan: CreationPlan
    path: Path | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    @property
    def applied(self) -> bool:
        return self.path is not None

    def as_mapping(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "path": str(self.path) if self.path is not None else "",
            "plan": self.plan.as_mapping(),
            "details": dict(self.details),
        }


class OnboardingService:
    """Coordinate discovery, templates, atomic creation, and readiness.

    The service contains application workflow only.  File rendering belongs to
    templates, atomicity belongs to transactions, and environment inspection
    belongs to dedicated discovery/readiness collaborators.
    """

    def __init__(
        self,
        *,
        templates: TemplateCatalog,
        discovery: DiscoveryEngine | None = None,
        readiness: ReadinessService | None = None,
        profile_catalog: ProjectProfileCatalog | None = None,
    ) -> None:
        self.templates = templates
        self.discovery = discovery or DiscoveryEngine()
        self.readiness = readiness or ReadinessService()
        self.profile_catalog = profile_catalog or default_profile_catalog()
        self.task_definitions = TaskDefinitionService()

    def inspect_project(self, source_root: Path) -> DiscoveryReport:
        return self.discovery.inspect(source_root)

    def create_project(
        self,
        *,
        report: DiscoveryReport,
        output_dir: Path,
        template_id: str = "standard",
        register: bool = False,
        dry_run: bool = False,
        accept_decisions: bool = False,
        feature_ids: Sequence[str] = (),
        include_devcontainer: bool = False,
    ) -> OnboardingOutcome:
        project_id = validate_project_id(report.project_id)
        descriptor = self.templates.get(kind="project", template_id=template_id)
        target = output_dir.expanduser().resolve() / project_id
        resolved_features = tuple(feature_ids)
        if descriptor.reference != "standard@1":
            profile = self.profile_catalog.profile(descriptor.reference)
            resolved_features = tuple(
                feature.reference
                for feature in self.profile_catalog.detect_features(
                    report,
                    profile=profile,
                    requested=tuple(feature_ids),
                    include_devcontainer=include_devcontainer,
                )
            )

        def materialize(destination: Path) -> None:
            self.templates.materialize(
                kind="project",
                template_id=descriptor.reference,
                context=ProjectTemplateContext(
                    report=report,
                    profile_reference=descriptor.reference,
                    requested_features=tuple(feature_ids),
                    include_devcontainer=include_devcontainer,
                ),
                destination=destination,
            )

        finalizers = ()
        if register:
            finalizers = (
                lambda published: register_project_descriptor(
                    published / "project.yaml",
                    source_root=Path(report.source_root),
                ),
            )

        with ProjectCreationTransaction(
            kind="project",
            identifier=project_id,
            target=target,
            materializer=materialize,
            evidence=tuple(report.evidence),
            findings=tuple(report.findings),
            metadata={
                "template": descriptor.reference,
                "source_root": report.source_root,
                "repositories": [item.id for item in report.repositories],
                "features": list(resolved_features),
                "devcontainer": any(
                    item.split("@", 1)[0] == "devcontainer" for item in resolved_features
                ),
            },
            finalizers=finalizers,
            accept_decisions=accept_decisions,
        ) as transaction:
            plan = transaction.prepare()
            if dry_run:
                return OnboardingOutcome(plan=plan)
            result = transaction.apply()
            details: dict[str, Any] = {"template": descriptor.reference}
            if result.finalizer_results:
                details["registration"] = str(result.finalizer_results[0])
            return OnboardingOutcome(plan=plan, path=result.path / "project.yaml", details=details)

    def create_task(
        self,
        *,
        control_root: Path,
        project: ProjectDescriptor,
        task_id: str,
        title: str,
        branch: str = "",
        repository_ids: Sequence[str] = (),
        brief: str = "",
        template_id: str = "standard",
        state_root: Path,
        definition: TaskDefinitionInput | None = None,
        request_sha256: str = "",
        dry_run: bool = False,
    ) -> OnboardingOutcome:
        normalized_task_id = validate_task_id(task_id)
        selected = self._select_repositories(project, repository_ids)
        task_branch = branch.strip() or f"task/{normalized_task_id}"
        timestamp = utc_now()
        imported = definition or TaskDefinitionInput()
        resolved_title = title.strip() or imported.suggested_title()
        if not resolved_title and not imported.supplied:
            raise TaskGitError("TASK.yaml title cannot be empty")
        if not request_sha256:
            identity = "\0".join(
                part for part in (brief.strip(), imported.source_fingerprint()) if part
            )
            request_sha256 = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        prepared_definition = self.task_definitions.prepare(
            definition=imported,
            title=resolved_title,
            fallback_brief=brief,
            request_sha256=request_sha256,
            created_at=timestamp,
            allowed_repositories={item.id for item in selected},
        )
        manifest = TaskManifest(
            schema_version=2,
            id=normalized_task_id,
            project=project.id,
            title=prepared_definition.title,
            status="draft",
            created_at=timestamp,
            last_updated=timestamp,
            branch_name=task_branch,
            merge_strategy="squash",
            repositories=[
                RepositorySpec(
                    id=item.id,
                    base_branch=item.base_branch,
                    task_branch=task_branch,
                    role=item.role,
                    required=item.required,
                    mutability=item.mutability,
                )
                for item in selected
            ],
        )
        validate_manifest(manifest)
        descriptor = self.templates.get(kind="task", template_id=template_id)
        target = project_task_directory(control_root, project.id, normalized_task_id)
        template_directory = project.configured_path("task_templates")

        def materialize(destination: Path) -> None:
            self.templates.materialize(
                kind="task",
                template_id=descriptor.reference,
                context=TaskTemplateContext(
                    title=prepared_definition.title,
                    brief=brief,
                    template_directory=template_directory,
                ),
                destination=destination,
            )
            self.task_definitions.materialize(destination, prepared_definition)
            (destination / "TASK.yaml").write_text(
                yaml.safe_dump(manifest.as_mapping(), sort_keys=False, width=1000),
                encoding="utf-8",
            )

        registry_path = local_manifest_registry_path(control_root, normalized_task_id)
        if registry_path.exists():
            raise ValueError(
                f"task manifest registry already exists: {registry_path}"
            )

        def publish_registry(published: Path) -> Path:
            registry_path.parent.mkdir(parents=True, exist_ok=True)
            content = (published / "TASK.yaml").read_text(encoding="utf-8")
            descriptor_fd: int | None = None
            try:
                descriptor_fd = os.open(
                    registry_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                with os.fdopen(descriptor_fd, "w", encoding="utf-8") as handle:
                    descriptor_fd = None
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
            except Exception:
                if descriptor_fd is not None:
                    os.close(descriptor_fd)
                registry_path.unlink(missing_ok=True)
                raise
            return registry_path

        def sync_status(published: Path) -> Path:
            return sync_runtime_status(
                published,
                task_id=normalized_task_id,
                project_id=project.id,
                state_root=state_root,
            ).path

        def rollback_registry(_published: Path) -> None:
            registry_path.unlink(missing_ok=True)

        with TaskCreationTransaction(
            kind="task",
            identifier=f"{project.id}/{normalized_task_id}",
            target=target,
            materializer=materialize,
            metadata={
                "template": descriptor.reference,
                "project": project.id,
                "task_id": normalized_task_id,
                "branch": task_branch,
                "repositories": [item.id for item in selected],
                "definition_imports": sorted(imported.documents()),
                "definition_source_fingerprint": imported.source_fingerprint(),
            },
            finalizers=(publish_registry, sync_status),
            rollback_finalizers=(rollback_registry,),
        ) as transaction:
            plan = transaction.prepare()
            if dry_run:
                return OnboardingOutcome(plan=plan)
            result = transaction.apply()
            return OnboardingOutcome(
                plan=plan,
                path=result.path,
                details={
                    "template": descriptor.reference,
                    "manifest_registry": str(result.finalizer_results[0]),
                    "runtime_status": str(result.finalizer_results[1]),
                    "definition": str(result.path / "DEFINITION.yaml"),
                },
            )

    def evaluate_readiness(
        self,
        project: ProjectDescriptor,
        *,
        source_root: Path | None = None,
    ) -> ReadinessReport:
        return self.readiness.evaluate(project, source_root=source_root)

    @staticmethod
    def _select_repositories(
        project: ProjectDescriptor,
        repository_ids: Sequence[str],
    ) -> tuple[ProjectRepository, ...]:
        requested = tuple(dict.fromkeys(item.strip() for item in repository_ids if item.strip()))
        if not requested:
            return tuple(project.repositories)
        known = {item.id: item for item in project.repositories}
        unknown = [item for item in requested if item not in known]
        if unknown:
            raise ValueError(
                "unknown project repositories: " + ", ".join(sorted(unknown))
            )
        selected_ids = set(requested)
        selected_ids.update(item.id for item in project.repositories if item.required)
        return tuple(item for item in project.repositories if item.id in selected_ids)
