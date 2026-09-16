"""Multi-dimensional project readiness evaluation."""

from __future__ import annotations

import shutil
from pathlib import Path

from execraft.model_registry import ModelRegistryError, load_model_route_registry
from execraft.onboarding.models import (
    Evidence,
    ReadinessCheck,
    ReadinessReport,
    ReadinessStatus,
)
from execraft.onboarding.execution_inventory import ExecutionInventory, ExecutionInventoryReport
from execraft.orchestrate.verification import VerificationRegistry
from execraft.project import ProjectDescriptor, ProjectError, resolve_project_source_root


class ReadinessService:
    """Evaluate project prerequisites without executing project or provider code."""

    def __init__(self, *, execution_inventory: ExecutionInventory | None = None) -> None:
        self._execution = execution_inventory or ExecutionInventory()

    def evaluate(
        self,
        project: ProjectDescriptor,
        *,
        source_root: Path | None = None,
    ) -> ReadinessReport:
        checks: list[ReadinessCheck] = [
            ReadinessCheck(
                id="descriptor",
                status=ReadinessStatus.READY,
                summary="Project descriptor is structurally valid",
                evidence=(
                    Evidence(
                        id="descriptor-path",
                        subject="project",
                        field="descriptor",
                        value=str(project.directory / "project.yaml"),
                        source="project-loader",
                        confidence=1.0,
                    ),
                ),
            )
        ]

        try:
            resolved_source = resolve_project_source_root(project, source_root)
        except ProjectError as exc:
            checks.append(
                ReadinessCheck(
                    id="source",
                    status=ReadinessStatus.BLOCKED,
                    summary="Project source root is unavailable",
                    details=(str(exc),),
                )
            )
            resolved_source = None
        else:
            checks.append(
                ReadinessCheck(
                    id="source",
                    status=ReadinessStatus.READY,
                    summary=f"Source root resolved to {resolved_source}",
                )
            )

        checks.append(self._repository_check(project, resolved_source))
        checks.append(self._workspace_check())
        checks.append(self._verification_check(project))
        execution_report = self._execution.inspect(project)
        checks.append(self._execution_check(execution_report))
        checks.append(self._model_registry_check(project, execution_report))

        dependencies = {
            item.id: item
            for item in checks
            if item.id
            in {
                "descriptor",
                "source",
                "repositories",
                "workspace",
                "verification",
                "execution",
                "model_registry",
            }
        }
        blocked = tuple(
            f"{item.id}: {item.summary}"
            for item in dependencies.values()
            if item.blocks
        )
        checks.append(
            ReadinessCheck(
                id="orchestration",
                status=ReadinessStatus.BLOCKED if blocked else ReadinessStatus.READY,
                summary=(
                    "Orchestration prerequisites are satisfied"
                    if not blocked
                    else "Orchestration prerequisites are incomplete"
                ),
                details=blocked,
            )
        )
        return ReadinessReport(project_id=project.id, checks=tuple(checks))

    @staticmethod
    def _repository_check(
        project: ProjectDescriptor, source_root: Path | None
    ) -> ReadinessCheck:
        if source_root is None:
            return ReadinessCheck(
                id="repositories",
                status=ReadinessStatus.BLOCKED,
                summary="Repository paths cannot be evaluated without a source root",
            )
        issues: list[str] = []
        evidence: list[Evidence] = []
        for repository in project.repositories:
            path = (source_root / repository.path).resolve()
            try:
                path.relative_to(source_root)
            except ValueError:
                issues.append(f"Repository {repository.id} escapes source root: {repository.path}")
                continue
            present = path.is_dir()
            git_marker = present and ((path / ".git").is_dir() or (path / ".git").is_file())
            evidence.append(
                Evidence(
                    id=f"repository-{repository.id}-path",
                    subject=f"repository:{repository.id}",
                    field="path",
                    value=str(path),
                    source="project-descriptor",
                    confidence=1.0,
                )
            )
            if repository.required and not present:
                issues.append(f"Required repository {repository.id} is missing: {path}")
            elif present and not git_marker:
                issues.append(f"Repository {repository.id} is not a Git worktree: {path}")
        return ReadinessCheck(
            id="repositories",
            status=ReadinessStatus.BLOCKED if issues else ReadinessStatus.READY,
            summary=(
                f"{len(project.repositories)} repository path(s) are available"
                if not issues
                else "Repository topology has blocking issues"
            ),
            details=tuple(issues),
            evidence=tuple(evidence),
        )

    @staticmethod
    def _workspace_check() -> ReadinessCheck:
        git_path = shutil.which("git") or ""
        return ReadinessCheck(
            id="workspace",
            status=ReadinessStatus.READY if git_path else ReadinessStatus.BLOCKED,
            summary=(
                f"Git executable available at {git_path}"
                if git_path
                else "Git executable is unavailable"
            ),
            evidence=(
                Evidence(
                    id="workspace-git-binary",
                    subject="workspace",
                    field="git_binary",
                    value=git_path,
                    source="path-resolution",
                    confidence=1.0,
                ),
            ),
        )

    @staticmethod
    def _verification_check(project: ProjectDescriptor) -> ReadinessCheck:
        path = project.configured_path("verification_file")
        if path is None or not path.is_file():
            return ReadinessCheck(
                id="verification",
                status=ReadinessStatus.BLOCKED,
                summary="Verification registry is missing",
                details=("Configure verification_file in project.yaml.",),
            )
        try:
            registry = VerificationRegistry.load(path)
        except (OSError, ValueError) as exc:
            return ReadinessCheck(
                id="verification",
                status=ReadinessStatus.BLOCKED,
                summary="Verification registry is invalid",
                details=(str(exc),),
            )
        enabled = [command for command in registry.commands if command.enabled]
        if enabled:
            return ReadinessCheck(
                id="verification",
                status=ReadinessStatus.READY,
                summary=f"{len(enabled)} verification command(s) are enabled",
                details=tuple(command.id or command.command for command in enabled),
            )
        if registry.require_commands:
            return ReadinessCheck(
                id="verification",
                status=ReadinessStatus.BLOCKED,
                summary="Verification commands are required but none are enabled",
                details=("Review and enable a discovered or project-owned command.",),
            )
        return ReadinessCheck(
            id="verification",
            status=ReadinessStatus.DISABLED,
            summary="Verification commands are optional and currently disabled",
            required=False,
        )

    @staticmethod
    def _execution_check(report: ExecutionInventoryReport) -> ReadinessCheck:
        details = tuple(item.message for item in report.findings) + tuple(report.warnings)
        if report.ready_profiles:
            return ReadinessCheck(
                id="execution",
                status=ReadinessStatus.READY,
                summary=(
                    f"{len(report.ready_profiles)} enabled execution profile(s) "
                    "have their local runtime prerequisites"
                ),
                details=tuple(
                    f"{item.id}: {item.runtime_kind} / {item.model or 'runtime default'}"
                    for item in report.ready_profiles
                ),
                evidence=report.evidence,
            )
        return ReadinessCheck(
            id="execution",
            status=ReadinessStatus.BLOCKED,
            summary=(
                "Enabled execution profiles are not locally runnable"
                if report.enabled_profiles
                else "No execution profile is enabled"
            ),
            details=details,
            evidence=report.evidence,
        )

    @staticmethod
    def _model_registry_check(
        project: ProjectDescriptor, execution_report: ExecutionInventoryReport
    ) -> ReadinessCheck:
        runtime_by_id = {item.id: item for item in execution_report.runtimes}
        required = any(
            profile.enabled
            and (runtime_by_id.get(profile.runtime_id) is not None)
            and runtime_by_id[profile.runtime_id].kind == "native"
            and runtime_by_id[profile.runtime_id].adapter == "opencode"
            for profile in execution_report.profiles
        )
        opencode_dir = project.configured_path("opencode_dir")
        if opencode_dir is None:
            return ReadinessCheck(
                id="model_registry",
                status=ReadinessStatus.DISABLED,
                summary="No model endpoint registry is configured",
                required=False,
            )
        registry_path = opencode_dir / "providers.yaml"
        try:
            registry = load_model_route_registry(registry_path)
        except ModelRegistryError as exc:
            return ReadinessCheck(
                id="model_registry",
                status=ReadinessStatus.BLOCKED if required else ReadinessStatus.WARNING,
                summary=(
                    "Model endpoint registry is invalid"
                    if required
                    else "Optional Native/OpenCode model registry is invalid"
                ),
                details=(str(exc),),
                required=required,
            )
        return ReadinessCheck(
            id="model_registry",
            status=ReadinessStatus.READY,
            summary=(
                f"Model endpoint registry is valid: {len(registry.endpoints)} endpoint(s)"
            ),
            details=(str(registry_path),),
            required=required,
        )
