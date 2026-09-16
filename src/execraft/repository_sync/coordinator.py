"""Control-plane coordinator for Work Package card Pause & Sync requests.

The orchestrator only decides *when* a safe boundary is reached.  Once its run
locks are released this coordinator performs the normal definition revision,
so card-triggered synchronization has exactly the same versioning, drift,
rollback, and package-identity guarantees as an explicit ``task sync-before``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from execraft.project import ProjectDescriptor
from execraft.onboarding.task_definition import TaskDefinitionService
from execraft.orchestrate.identity import resolve_storage_identity
from execraft.replan.service import ReplanApplyResult, ReplanInputs, ReplanService
from execraft.workspace.task_git import TaskManifest

from .card_intent import (
    RepositorySyncCardIntentError,
    RepositorySyncCardIntentStore,
)
from .card_request import RepositorySyncCardRequest, RepositorySyncCardRequestError
from .planning import (
    RepositorySyncInsertion,
    build_sync_after_definition,
    build_sync_before_definition,
)


class RepositorySyncCoordinationError(RuntimeError):
    """Raised when a safe-boundary request cannot be converted into definition state."""


@dataclass(frozen=True)
class RepositorySyncCoordinationResult:
    command_id: str
    request: RepositorySyncCardRequest
    insertion: RepositorySyncInsertion
    replan: ReplanApplyResult

    def as_mapping(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "package_id": self.request.package_id,
            "mode": self.request.mode,
            "repositories": list(self.request.repositories),
            "source_branches": dict(self.request.source_branches),
            "remote": self.request.remote,
            "conflict_policy": self.request.conflict_policy,
            "auto_resume": self.request.auto_resume,
            "sync_package_id": self.insertion.sync_package_id,
            "revision": self.replan.revision,
            "candidate_id": self.replan.candidate_id,
            "definition_sha256": self.replan.definition_sha256,
        }


class RepositorySyncCoordinator:
    """Apply one paused card request through the canonical definition transaction."""

    def __init__(
        self,
        *,
        control_root: Path,
        state_root: Path,
        project: ProjectDescriptor,
        manifest: TaskManifest,
        dossier: Path,
    ) -> None:
        self.control_root = Path(control_root).resolve()
        self.state_root = Path(state_root).expanduser().resolve()
        self.project = project
        self.manifest = manifest
        self.dossier = Path(dossier).resolve()
        self.definitions = TaskDefinitionService()
        identity = resolve_storage_identity(
            self.state_root,
            project_id=self.project.id,
            task_id=self.manifest.id,
        )
        self.intents = RepositorySyncCardIntentStore(
            identity.state_dir / "repository-sync-card-intents"
        )

    def _safe_sync_package_id(self, request: RepositorySyncCardRequest) -> str:
        if request.sync_package_id:
            return request.sync_package_id
        metadata = self.definitions.load_metadata(self.dossier)
        seen = {str(item) for item in metadata.get("package_ids_seen", []) if str(item)}
        base = f"{request.package_id}-SYNC"
        if base not in seen:
            return base
        index = 2
        while f"{base}-{index}" in seen:
            index += 1
        return f"{base}-{index}"

    def apply_waiting(self, waiting: Mapping[str, Any]) -> RepositorySyncCoordinationResult:
        package_id = str(waiting.get("package_id", "")).strip()
        command_id = str(waiting.get("command_id", "")).strip()
        raw_request = waiting.get("repository_sync")
        if not package_id or not command_id or not isinstance(raw_request, Mapping):
            raise RepositorySyncCoordinationError(
                "operator-paused state is missing repository synchronization request metadata"
            )
        try:
            request = RepositorySyncCardRequest.from_parameters(
                raw_request,
                manifest=self.manifest,
                package_id=package_id,
            )
            documents = self.definitions.live_documents(
                self.dossier, require_complete=True
            )
            service = ReplanService(
                control_root=self.control_root,
                state_root=self.state_root,
                project=self.project,
                manifest=self.manifest,
                dossier=self.dossier,
            )
            completed_package_ids = service.completed_package_ids()
            sync_package_id = self._safe_sync_package_id(request)
            self.intents.prepare(
                command_id=command_id,
                package_id=request.package_id,
                sync_package_id=sync_package_id,
                request=request.as_parameters(),
            )
            if request.mode == "before":
                insertion = build_sync_before_definition(
                    brief_markdown=documents["BRIEF.md"],
                    plan_markdown=documents["PLAN.md"],
                    plan_graph_yaml=documents["PLAN.graph.yaml"],
                    before_package_id=request.package_id,
                    repositories=request.repositories,
                    source_branches=request.source_branches,
                    remote=request.remote,
                    conflict_policy=request.conflict_policy,
                    sync_package_id=sync_package_id,
                )
            else:
                insertion = build_sync_after_definition(
                    brief_markdown=documents["BRIEF.md"],
                    plan_markdown=documents["PLAN.md"],
                    plan_graph_yaml=documents["PLAN.graph.yaml"],
                    after_package_id=request.package_id,
                    repositories=request.repositories,
                    source_branches=request.source_branches,
                    remote=request.remote,
                    conflict_policy=request.conflict_policy,
                    sync_package_id=sync_package_id,
                    completed_package_ids=completed_package_ids,
                )
            candidate = service.create_candidate(
                ReplanInputs(
                    requested_change=(
                        f"Pause & Sync request: insert {insertion.sync_package_id} "
                        f"{request.mode} {request.package_id} using operator-selected upstream refs."
                    ),
                    definition=insertion.definition,
                    allow_structural_consistency=True,
                ),
                provider=None,
                workdir=self.dossier,
            )
            result = service.apply_candidate(candidate.candidate_id)
            intent = self.intents.load(command_id)
            if intent is None:
                raise RepositorySyncCoordinationError(
                    "card sync intent disappeared after replanning"
                )
            self.intents.save(
                intent.advanced(
                    "replanned",
                    candidate_id=candidate.candidate_id,
                    revision=result.revision,
                )
            )
            return RepositorySyncCoordinationResult(
                command_id=command_id,
                request=request,
                insertion=insertion,
                replan=result,
            )
        except RepositorySyncCoordinationError:
            raise
        except (
            RepositorySyncCardIntentError,
            RepositorySyncCardRequestError,
            OSError,
            ValueError,
            RuntimeError,
        ) as exc:
            raise RepositorySyncCoordinationError(str(exc)) from exc

    def _install_policy_and_release_boundary(
        self,
        *,
        intent: Any,
        request: RepositorySyncCardRequest,
        sync_package_id: str,
        orchestrator: Any,
    ) -> None:
        """Idempotently install resume policy, then release the original hold."""

        if intent.phase == "complete":
            return
        if not request.auto_resume and intent.phase in {"prepared", "replanned"}:
            orchestrator.load_state()
            orchestrator.schedule_pause_after_completion(
                sync_package_id,
                reason=(
                    "Pause & Sync completed; operator requested inspection "
                    "before continuing development"
                ),
            )
            intent = intent.advanced("execution_policy_installed")
            self.intents.save(intent)

        if intent.phase != "boundary_released":
            orchestrator.load_state()
            orchestrator.acknowledge_repository_sync_boundary(
                command_id=intent.command_id,
                sync_package_id=sync_package_id,
            )
            intent = intent.advanced("boundary_released")
            self.intents.save(intent)

        self.intents.save(intent.advanced("complete"))

    def install_execution_policy(
        self, result: RepositorySyncCoordinationResult, orchestrator: Any
    ) -> None:
        """Durably install post-sync resume/pause policy and release its boundary."""

        try:
            intent = self.intents.load(result.command_id)
            if intent is None:
                raise RepositorySyncCoordinationError(
                    "card sync intent is missing while installing execution policy"
                )
            self._install_policy_and_release_boundary(
                intent=intent,
                request=result.request,
                sync_package_id=result.insertion.sync_package_id,
                orchestrator=orchestrator,
            )
        except RepositorySyncCoordinationError:
            raise
        except (RepositorySyncCardIntentError, OSError, ValueError, RuntimeError) as exc:
            raise RepositorySyncCoordinationError(str(exc)) from exc

    def recover_execution_policies(self, orchestrator: Any) -> list[str]:
        """Recover publication/policy crashes before any new pipeline run starts."""

        try:
            pending = self.intents.pending()
            if not pending:
                return []
            documents = self.definitions.live_documents(
                self.dossier, require_complete=True
            )
            graph = yaml.safe_load(documents["PLAN.graph.yaml"]) or {}
            if not isinstance(graph, Mapping):
                raise RepositorySyncCoordinationError(
                    "task graph must be a mapping while recovering card sync intent"
                )
            raw_packages = graph.get("work_packages", [])
            if not isinstance(raw_packages, list):
                raise RepositorySyncCoordinationError(
                    "task graph work_packages must be a list while recovering card sync intent"
                )
            package_ids = {
                str(item.get("id", ""))
                for item in raw_packages
                if isinstance(item, Mapping)
            }
            recovered: list[str] = []
            for intent in pending:
                if intent.sync_package_id not in package_ids:
                    # The definition service never published the candidate. The original waiting
                    # request remains authoritative and normal coordination will
                    # retry it using this same durable command identity.
                    continue
                request = RepositorySyncCardRequest.from_parameters(
                    intent.request,
                    manifest=self.manifest,
                    package_id=intent.package_id,
                )
                self._install_policy_and_release_boundary(
                    intent=intent,
                    request=request,
                    sync_package_id=intent.sync_package_id,
                    orchestrator=orchestrator,
                )
                recovered.append(intent.command_id)
            return recovered
        except RepositorySyncCoordinationError:
            raise
        except (
            RepositorySyncCardIntentError,
            RepositorySyncCardRequestError,
            OSError,
            ValueError,
            RuntimeError,
            yaml.YAMLError,
        ) as exc:
            raise RepositorySyncCoordinationError(str(exc)) from exc


__all__ = [
    "RepositorySyncCoordinationError",
    "RepositorySyncCoordinationResult",
    "RepositorySyncCoordinator",
]
