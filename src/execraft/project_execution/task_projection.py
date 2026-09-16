"""Filesystem adapter implementing the Project Execution Task port.

This is intentionally the only Project Execution module aware of current Task
manifest/runtime persistence shapes. Control actions are injected, keeping the
Project Executor independent from Work Package orchestration.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
import json
from pathlib import Path

import yaml

from execraft.orchestrate.identity import resolve_storage_identity
from execraft.persistence.files import sha256_file
from execraft.project import ProjectDescriptor
from execraft.workspace.task_git import TaskGitError, TaskManifest

from .task_port import (
    TaskActionResult,
    TaskEvidence,
    TaskExecutionSummary,
    TaskOutcome,
    TaskStartResult,
    TaskVerificationSummary,
)

StartAction = Callable[[str], TaskStartResult]
TaskAction = Callable[[str], TaskActionResult]

_TERMINAL_STATES = {
    "completed": TaskOutcome.COMPLETED,
    "failed": TaskOutcome.FAILED,
    "cancelled": TaskOutcome.CANCELLED,
}
_ACTIVE_EXECUTION_STATES = {
    "validating_plan",
    "running",
    "waiting_for_agent",
    "waiting_for_environment",
    "resource_maintenance",
    "paused_low_disk",
    "operator_paused",
    "recovering",
    "final_validation",
    "human_required",
    "supervising",
    "waiting_for_human_decision",
}
_PASSED_VERIFICATION_OUTCOMES = {"passed", "approved", "success", "completed"}
_FAILED_VERIFICATION_OUTCOMES = {"failed", "error", "rejected"}
_ARTIFACT_KEYS = ("artifact_id", "artifact", "artifact_path")


class FilesystemTaskExecutionPort:
    """Project-facing Task adapter backed by existing Execraft state files."""

    def __init__(
        self,
        *,
        project: ProjectDescriptor,
        state_root: Path,
        start_action: StartAction | None = None,
        pause_action: TaskAction | None = None,
        resume_action: TaskAction | None = None,
    ) -> None:
        self.project = project
        self.state_root = Path(state_root).expanduser().resolve()
        self._start = start_action
        self._pause = pause_action
        self._resume = resume_action

    def describe(self, task_id: str) -> TaskExecutionSummary:
        manifest = self._manifest(task_id)
        state = self._state(task_id)
        state_name = str(state.get("state", ""))
        dossier = self._dossier(task_id)
        return TaskExecutionSummary(
            task_id=task_id,
            exists=manifest is not None,
            active=bool(
                manifest and manifest.status not in {"archived", "cancelled"}
            ),
            executable=bool(
                manifest
                and (dossier / "PLAN.graph.yaml").is_file()
                and not (dossier / "PLAN.graph.yaml").is_symlink()
            ),
            execution_state=state_name,
            outcome=self._outcome(state_name),
            title=manifest.title if manifest else "",
        )

    def start(self, task_id: str) -> TaskStartResult:
        summary = self.describe(task_id)
        if summary.outcome in {
            TaskOutcome.RUNNING,
            TaskOutcome.COMPLETED,
            TaskOutcome.FAILED,
            TaskOutcome.CANCELLED,
        }:
            return TaskStartResult(
                accepted=False,
                already_started=True,
                message=f"Task {task_id} is already {summary.outcome.value}",
            )
        if self._start is None:
            return TaskStartResult(
                accepted=False,
                message="Task start action is not configured",
            )
        return self._start(task_id)

    def pause(self, task_id: str) -> TaskActionResult:
        if self._pause is None:
            return TaskActionResult(False, "Task pause action is not configured")
        return self._pause(task_id)

    def resume(self, task_id: str) -> TaskActionResult:
        if self._resume is None:
            return TaskActionResult(False, "Task resume action is not configured")
        return self._resume(task_id)

    def outcome(self, task_id: str) -> TaskOutcome:
        return self.describe(task_id).outcome

    def evidence(self, task_id: str) -> TaskEvidence:
        manifest = self._manifest(task_id)
        dossier = self._dossier(task_id)
        state = self._state(task_id)
        outcome = self._outcome(str(state.get("state", "")))
        verification, artifacts = self._verification_evidence(state)

        return TaskEvidence(
            task_id=task_id,
            outcome=outcome,
            task_digest=self._digest(dossier / "TASK.yaml"),
            plan_digest=self._digest(dossier / "PLAN.graph.yaml"),
            verification=verification,
            repository_revisions={
                repository.id: repository.latest_commit
                for repository in (manifest.repositories if manifest else [])
                if repository.latest_commit
            },
            artifacts=artifacts,
            details={
                "execution_state": str(state.get("state", "")),
                "completed_packages": int(state.get("completed_packages", 0) or 0),
                "total_packages": int(state.get("total_packages", 0) or 0),
            },
        )

    def _dossier(self, task_id: str) -> Path:
        return self.project.directory / "tasks" / task_id

    def _manifest(self, task_id: str) -> TaskManifest | None:
        path = self._dossier(task_id) / "TASK.yaml"
        if not path.is_file() or path.is_symlink():
            return None
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, Mapping):
                return None
            return TaskManifest.from_mapping(raw)
        except (OSError, ValueError, yaml.YAMLError, TaskGitError):
            return None

    def _state(self, task_id: str) -> Mapping[str, object]:
        try:
            identity = resolve_storage_identity(
                self.state_root,
                project_id=self.project.id,
                task_id=task_id,
                create=False,
            )
            path = identity.state_dir / "state.json"
            if not path.is_file() or path.is_symlink():
                return {}
            raw = json.loads(path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, Mapping) else {}
        except (OSError, ValueError, TypeError):
            return {}

    @staticmethod
    def _outcome(state: str) -> TaskOutcome:
        normalized = state.strip().lower()
        if normalized in _TERMINAL_STATES:
            return _TERMINAL_STATES[normalized]
        if normalized in _ACTIVE_EXECUTION_STATES:
            return TaskOutcome.RUNNING
        if normalized in {"", "initializing"}:
            return TaskOutcome.NOT_STARTED
        return TaskOutcome.UNKNOWN

    @staticmethod
    def _digest(path: Path) -> str:
        if not path.is_file() or path.is_symlink():
            return ""
        return f"sha256:{sha256_file(path)}"

    @classmethod
    def _verification_evidence(
        cls,
        state: Mapping[str, object],
    ) -> tuple[TaskVerificationSummary, tuple[str, ...]]:
        statuses: list[str] = []
        artifacts: set[str] = set()
        graph = state.get("plan_graph") or {}
        packages = graph.get("work_packages", []) if isinstance(graph, Mapping) else []
        if not isinstance(packages, list):
            packages = []

        for package in packages:
            if not isinstance(package, Mapping):
                continue
            cls._collect_package_verification(package, statuses, artifacts)

        failed = sum(value in _FAILED_VERIFICATION_OUTCOMES for value in statuses)
        passed = sum(value in _PASSED_VERIFICATION_OUTCOMES for value in statuses)
        if failed:
            outcome = "failed"
        elif statuses and passed == len(statuses):
            outcome = "passed"
        else:
            outcome = "unknown"

        verification = TaskVerificationSummary(
            outcome=outcome,
            passed=passed,
            failed=failed,
            total=len(statuses),
        )
        return verification, tuple(sorted(artifacts))

    @staticmethod
    def _collect_package_verification(
        package: Mapping[str, object],
        statuses: list[str],
        artifacts: set[str],
    ) -> None:
        last_verification = package.get("last_verification") or {}
        if isinstance(last_verification, Mapping):
            status = str(
                last_verification.get(
                    "status",
                    last_verification.get("outcome", ""),
                )
            ).strip().lower()
            if status:
                statuses.append(status)

        for detail_key in ("last_implementation", "last_verification"):
            detail = package.get(detail_key) or {}
            if not isinstance(detail, Mapping):
                continue
            for artifact_key in _ARTIFACT_KEYS:
                value = str(detail.get(artifact_key, "")).strip()
                if value:
                    artifacts.add(value)
