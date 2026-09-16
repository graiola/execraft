"""Read models and narrowly-scoped mutations for the GUI project home.

This service owns project catalog projection, runtime-neutral readiness/execution views,
verification approval, task review, and resumable onboarding-session discovery.
It has no HTTP or browser dependencies and can therefore be reused by the CLI,
unit tests, or future transports.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import yaml

from execraft.control_plane import ControlPlaneHome
from execraft.gui.contracts import ActiveTaskRef, ControlCenterError
from execraft.onboarding import ProviderInventory, default_greenfield_catalog, default_profile_catalog
from execraft.onboarding.execution_inventory import ExecutionInventory
from execraft.onboarding.service import OnboardingService
from execraft.orchestrate.normalizer import load_plan_graph_file
from execraft.orchestrate.verification import VerificationRegistry
from execraft.project import (
    ProjectCatalogEntry,
    ProjectError,
    list_project_catalog,
    load_project_registration,
    load_registered_project,
)
from execraft.workspace.task_git import TaskGitError, TaskManifest, validate_task_id

ActiveTaskGetter = Callable[[], ActiveTaskRef | None]


class ProjectHomeService:
    """Build project-home projections and apply verification approvals."""

    def __init__(
        self,
        *,
        home: ControlPlaneHome,
        onboarding: OnboardingService,
        active_task: ActiveTaskGetter,
        provider_inventory: ProviderInventory | None = None,
        execution_inventory: ExecutionInventory | None = None,
    ) -> None:
        self.home = home
        self.root = home.root
        self.state_root = home.state_dir
        self.onboarding = onboarding
        self.provider_inventory = provider_inventory or ProviderInventory()
        self.execution_inventory = execution_inventory or ExecutionInventory()
        self._active_task = active_task

    def catalog(self) -> list[ProjectCatalogEntry]:
        return list_project_catalog(self.root, skip_invalid=True)

    def projects(self) -> list[dict[str, Any]]:
        return [self.project_summary(entry) for entry in self.catalog()]

    def project_summary(self, entry: ProjectCatalogEntry) -> dict[str, Any]:
        project = entry.project
        registration = entry.registration or load_project_registration(project.id)
        source_root = registration.source_root if registration else None
        try:
            readiness = self.onboarding.evaluate_readiness(
                project,
                source_root=source_root,
            ).as_mapping()
        except Exception as exc:  # One broken project must not break the home page.
            readiness = {
                "project": project.id,
                "ready": False,
                "checks": [
                    {
                        "id": "descriptor",
                        "status": "blocked",
                        "summary": str(exc),
                        "required": True,
                        "details": [],
                        "evidence": [],
                    }
                ],
            }
        tasks = self.project_tasks(project.id)
        roadmap_count = self._roadmap_count(project.directory)
        active = self._active_task()
        return {
            "id": project.id,
            "description": project.description,
            "profile": project.profile,
            "features": list(project.features),
            "generated_with": project.generated_with,
            "descriptor": str(project.directory / "project.yaml"),
            "source_root": str(source_root) if source_root else "",
            "origin": entry.origin,
            "repositories": [
                {
                    "id": item.id,
                    "role": item.role,
                    "required": item.required,
                    "base_branch": item.base_branch,
                }
                for item in project.repositories
            ],
            "readiness": readiness,
            "tasks": tasks,
            "task_count": len(tasks),
            "roadmap_count": roadmap_count,
            "active": bool(active and active.project_id == project.id),
        }

    @staticmethod
    def _roadmap_count(project_directory: Path) -> int:
        """Count durable roadmap documents without parsing them on home refreshes."""

        root = project_directory / "roadmaps"
        if not root.is_dir():
            return 0
        return sum(
            1
            for path in root.glob("*.yaml")
            if path.is_file() and not path.is_symlink()
        )

    def project_tasks(self, project_id: str) -> list[dict[str, Any]]:
        project = load_registered_project(self.root, project_id)
        tasks_root = project.directory / "tasks"
        rows: list[dict[str, Any]] = []
        active = self._active_task()
        directories = sorted(tasks_root.iterdir()) if tasks_root.is_dir() else []
        for directory in directories:
            manifest_path = directory / "TASK.yaml"
            if not directory.is_dir() or not manifest_path.is_file():
                continue
            try:
                raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
                manifest = TaskManifest.from_mapping(raw)
                plan_path = directory / "PLAN.graph.yaml"
                modified = max(
                    manifest_path.stat().st_mtime,
                    plan_path.stat().st_mtime if plan_path.is_file() else 0.0,
                )
                rows.append(
                    {
                        "id": manifest.id,
                        "title": manifest.title or manifest.id,
                        "status": manifest.status or "draft",
                        "repositories": [item.id for item in manifest.repositories],
                        "modified_at": datetime.fromtimestamp(
                            modified, timezone.utc
                        ).isoformat(),
                        "has_plan": plan_path.is_file(),
                        "current": bool(
                            active
                            and active.project_id == project_id
                            and active.task_id == manifest.id
                        ),
                    }
                )
            except (
                OSError,
                ValueError,
                ProjectError,
                TaskGitError,
                yaml.YAMLError,
            ) as exc:
                rows.append(
                    {
                        "id": directory.name,
                        "title": directory.name,
                        "status": "invalid",
                        "repositories": [],
                        "modified_at": "",
                        "has_plan": False,
                        "current": False,
                        "error": str(exc),
                    }
                )
        rows.sort(
            key=lambda item: (item.get("modified_at", ""), item["id"]),
            reverse=True,
        )
        return rows

    def project_readiness(self, project_id: str) -> dict[str, Any]:
        project = load_registered_project(self.root, project_id)
        registration = load_project_registration(project.id)
        source = registration.source_root if registration else None
        return self.onboarding.evaluate_readiness(
            project,
            source_root=source,
        ).as_mapping()

    def project_providers(self, project_id: str) -> dict[str, Any]:
        project = load_registered_project(self.root, project_id)
        return self.provider_inventory.inspect(project).as_mapping()

    def project_execution(self, project_id: str) -> dict[str, Any]:
        """Return the passive runtime-neutral execution readiness inventory."""

        project = load_registered_project(self.root, project_id)
        return self.execution_inventory.inspect(project).as_mapping()

    def verification_snapshot(self, project_id: str) -> dict[str, Any]:
        project = load_registered_project(self.root, project_id)
        path = project.configured_path("verification_file")
        if path is None:
            raise ControlCenterError("project has no verification_file")
        registry = VerificationRegistry.load(path)
        raw = path.read_bytes() if path.is_file() else b""
        return {
            "project": project.id,
            "path": str(path),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "require_commands": registry.require_commands,
            "commands": [
                {"index": index, **command.as_mapping()}
                for index, command in enumerate(registry.commands)
            ],
            "known_failures": [item.as_mapping() for item in registry.known_failures],
        }

    def update_verification(
        self,
        project_id: str,
        *,
        expected_sha256: str,
        enabled_indexes: Sequence[int],
        require_commands: bool,
        acknowledged: bool = False,
    ) -> dict[str, Any]:
        if not acknowledged:
            raise ControlCenterError(
                "verification approval requires explicit acknowledgement"
            )
        project = load_registered_project(self.root, project_id)
        path = project.configured_path("verification_file")
        if path is None or not path.is_file():
            raise ControlCenterError("project has no readable verification_file")
        current = path.read_bytes()
        actual = hashlib.sha256(current).hexdigest()
        if not expected_sha256 or actual != expected_sha256:
            raise ControlCenterError(
                "verification configuration changed since it was loaded; "
                "refresh before saving"
            )
        registry = VerificationRegistry.load(path)
        selected = {int(item) for item in enabled_indexes}
        invalid = selected - set(range(len(registry.commands)))
        if invalid:
            raise ControlCenterError(
                "unknown verification command indexes: "
                + ", ".join(str(item) for item in sorted(invalid))
            )
        for index, command in enumerate(registry.commands):
            command.enabled = index in selected
        registry.require_commands = require_commands
        registry.save(path)
        return {
            "verification": self.verification_snapshot(project_id),
            "readiness": self.project_readiness(project_id),
        }

    def task_review(self, project_id: str, task_id: str) -> dict[str, Any]:
        project = load_registered_project(self.root, project_id)
        safe_task_id = validate_task_id(task_id)
        dossier = project.directory / "tasks" / safe_task_id
        manifest_path = dossier / "TASK.yaml"
        if not manifest_path.is_file():
            raise ControlCenterError(f"task dossier not found: {dossier}")
        raw_manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        manifest = TaskManifest.from_mapping(raw_manifest)
        brief_path = dossier / "BRIEF.md"
        plan_md_path = dossier / "PLAN.md"
        plan_graph_path = dossier / "PLAN.graph.yaml"
        graph: dict[str, Any] | None = None
        plan_error = ""
        if plan_graph_path.is_file():
            try:
                normalized, report = load_plan_graph_file(plan_graph_path)
                graph = {
                    "work_packages": [
                        item.as_mapping() for item in normalized.work_packages
                    ],
                    "normalization": {
                        "packages_found": report.packages_found,
                        "cycles_detected": list(report.cycles_detected),
                        "missing_dependencies": list(report.missing_dependencies),
                        "missing_acceptance_criteria": list(
                            report.missing_acceptance_criteria
                        ),
                        "duplicate_ids": list(report.duplicate_ids),
                        "warnings": list(report.warnings),
                        "errors": list(report.errors),
                        "has_errors": report.has_errors(),
                    },
                }
            except Exception as exc:  # Keep the review usable for a broken plan.
                plan_error = str(exc)
        journal_path = self.state_root / "starts" / project_id / f"{safe_task_id}.yaml"
        journal = read_yaml_mapping(journal_path)
        return {
            "project": project_id,
            "task_id": safe_task_id,
            "manifest": manifest.as_mapping(),
            "brief": (
                brief_path.read_text(encoding="utf-8")
                if brief_path.is_file()
                else ""
            ),
            "plan_markdown": (
                plan_md_path.read_text(encoding="utf-8")
                if plan_md_path.is_file()
                else ""
            ),
            "plan_graph": graph,
            "plan_error": plan_error,
            "paths": {
                "dossier": str(dossier),
                "manifest": str(manifest_path),
                "brief": str(brief_path),
                "plan": str(plan_graph_path),
                "journal": str(journal_path),
            },
            "journal": journal,
            "resumable": journal_resumable(journal),
        }

    def onboarding_sessions(self) -> list[dict[str, Any]]:
        root = self.state_root / "starts"
        rows: list[dict[str, Any]] = []
        if not root.is_dir():
            return rows
        for path in sorted(root.glob("*/*.yaml")):
            raw = read_yaml_mapping(path)
            if not raw:
                continue
            steps = raw.get("steps") if isinstance(raw.get("steps"), Mapping) else {}
            statuses = [
                str(item.get("status", ""))
                for item in steps.values()
                if isinstance(item, Mapping)
            ]
            rows.append(
                {
                    "project_id": str(raw.get("project", path.parent.name)),
                    "task_id": str(raw.get("task_id", path.stem)),
                    "source_root": str(raw.get("source_root", "")),
                    "steps": dict(steps),
                    "status": _session_status(steps, statuses),
                    "resumable": journal_resumable(raw),
                    "modified_at": datetime.fromtimestamp(
                        path.stat().st_mtime, timezone.utc
                    ).isoformat(),
                    "path": str(path),
                }
            )
        rows.sort(key=lambda item: item["modified_at"], reverse=True)
        return rows

    def template_catalog(self) -> dict[str, Any]:
        return {
            "onboarding": [
                {
                    "id": item.id,
                    "version": item.version,
                    "reference": item.reference,
                    "kind": item.kind,
                    "description": item.description,
                }
                for item in self.onboarding.templates.descriptors()
            ],
            "greenfield": [
                {
                    "id": item.id,
                    "version": item.version,
                    "reference": item.reference,
                    "description": item.description,
                }
                for item in default_greenfield_catalog().templates()
            ],
            "profiles": [
                {
                    "id": item.id,
                    "version": item.version,
                    "reference": item.reference,
                    "description": item.description,
                    "autonomous": item.autonomous,
                    "parallelism": item.parallelism,
                    "automatic_commits": item.automatic_commits,
                    "new_project_selectable": item.new_project_selectable,
                }
                for item in default_profile_catalog().profiles()
            ],
            "new_project_profiles": [
                {
                    "id": item.id,
                    "version": item.version,
                    "reference": item.reference,
                    "description": item.description,
                    "autonomous": item.autonomous,
                    "parallelism": item.parallelism,
                    "automatic_commits": item.automatic_commits,
                }
                for item in default_profile_catalog().new_project_profiles()
            ],
            "features": [
                {
                    "id": item.id,
                    "version": item.version,
                    "reference": item.reference,
                    "description": item.description,
                    "technologies": list(item.technologies),
                }
                for item in default_profile_catalog().features()
            ],
        }


def read_yaml_mapping(path: Path) -> dict[str, Any]:
    """Read a mapping without allowing a corrupt journal to break the home."""

    if not path.is_file():
        return {}
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def journal_resumable(journal: Mapping[str, Any]) -> bool:
    steps = journal.get("steps")
    if not isinstance(steps, Mapping):
        return False
    complete = steps.get("complete")
    return not (
        isinstance(complete, Mapping)
        and str(complete.get("status", "")) == "completed"
    )


def _session_status(steps: Mapping[str, Any], statuses: Sequence[str]) -> str:
    if "failed" in statuses:
        return "failed"
    complete = steps.get("complete")
    if (
        isinstance(complete, Mapping)
        and str(complete.get("status", "")) == "completed"
    ):
        return "completed"
    return "incomplete"


__all__ = ["ProjectHomeService", "journal_resumable", "read_yaml_mapping"]
