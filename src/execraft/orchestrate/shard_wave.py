"""Execution coordinator for parallel shard waves.

The coordinator owns the wave lifecycle and interruption boundary while the
project orchestrator supplies package, agent, persistence, and scope-policy
operations.  Keeping those concerns behind one collaborator makes the main
state machine a façade instead of a second parallel scheduler.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
import shutil
import subprocess
from typing import Any, Protocol, runtime_checkable
import uuid

from execraft.execution_identity import execution_identity_of

from .context_budget import ContextBudgetError
from .models import TaskExecutionState, WorkPackage, WorkPackageStage, utc_now
from .sharding import ParallelShardCandidate
from .transactions import snapshot_repository


@runtime_checkable
class ShardWaveHost(Protocol):
    """Operations supplied by ``ProjectOrchestrator`` to the coordinator."""

    _state_dir: Path
    _invocation_project_id: str
    task_id: str
    _journal: Any
    _agent_invocations: Any
    _state_record: Any

    def save_state(self, *, reason: str = "state_update") -> Path: ...
    def transition_to(self, new_state: TaskExecutionState) -> None: ...
    def _active_parallel_wave(self) -> dict[str, Any] | None: ...
    def _set_active_parallel_wave(self, wave: dict[str, Any] | None) -> None: ...
    def _emit_progress(self, event_type: str, **kwargs: Any) -> None: ...
    def _build_parallel_wave(
        self, ready: list[WorkPackage], deferred_ids: set[str]
    ) -> list[ParallelShardCandidate]: ...
    def _validate_clean_start(self, package: WorkPackage) -> None: ...
    def _advance_package_stage(
        self, package: WorkPackage, new_stage: WorkPackageStage
    ) -> None: ...
    def _prepare_parallel_write_isolation(
        self, candidate: ParallelShardCandidate, *, wave_id: str
    ) -> ParallelShardCandidate: ...
    def _cleanup_parallel_isolation(self, candidate: ParallelShardCandidate) -> None: ...
    def escalate_scope_failure(self, package: WorkPackage, reason: str) -> None: ...
    @staticmethod
    def _validated_prompt(handoff: Any) -> Any: ...
    def _parallel_dirty_owners(self) -> dict[str, str]: ...
    def _set_parallel_dirty_owners(self, owners: dict[str, str]) -> None: ...
    def _find_adapter(self, agent_id: str) -> Any: ...
    @staticmethod
    def _agent_metadata(adapter: Any) -> dict[str, Any]: ...
    def _parallel_workspace_digest(self, candidate: ParallelShardCandidate) -> str: ...
    def _invoke_parallel_candidate(
        self, adapter: Any, candidate: ParallelShardCandidate
    ) -> tuple[Any, Exception | None, float, Any]: ...
    def _repository_worktree_fingerprint(self, path: Path) -> str: ...
    def _apply_parallel_failure(
        self, candidate: ParallelShardCandidate, exc: Exception, duration: float
    ) -> None: ...
    def _release_parallel_dirty_ownership(self, package: WorkPackage) -> None: ...
    def _validate_parallel_result(
        self, candidate: ParallelShardCandidate, raw: Any, *, duration_seconds: float
    ) -> Any: ...
    def _apply_parallel_isolated_delta(
        self, candidate: ParallelShardCandidate
    ) -> None: ...
    def _apply_parallel_success(
        self, candidate: ParallelShardCandidate, result: Any
    ) -> None: ...


class ShardWaveCoordinator:
    """Coordinate shard selection, isolation, execution, and recovery."""

    def __init__(self, host: ShardWaveHost):
        self._host = host

    def recover_interrupted(self) -> bool:
        host = self._host
        wave = host._active_parallel_wave()
        if not wave:
            return False
        package_ids = [str(item) for item in wave.get("package_ids", [])]
        for item in wave.get("isolations", []) or []:
            if not isinstance(item, dict):
                continue
            source_text = str(item.get("source_repository_path", "")).strip()
            isolation_text = str(item.get("isolation_path", "")).strip()
            if not source_text or not isolation_text:
                continue
            source = Path(source_text)
            isolation = Path(isolation_text)
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(isolation)],
                cwd=source,
                text=True,
                capture_output=True,
                check=False,
            )
            shutil.rmtree(isolation.parent, ignore_errors=True)
        host._journal.append(
            "parallel_wave_interrupted",
            {"wave_id": wave.get("wave_id", ""), "package_ids": package_ids},
        )
        reason = "interrupted parallel shard wave: " + ", ".join(package_ids)
        host._journal.append(
            "human_intervention_required",
            {
                "package_id": package_ids[0] if package_ids else "",
                "stage": "parallel_state_recovery",
                "blocked_requirement": reason,
                "evidence": [
                    "the driver stopped before the active parallel wave reached its durable boundary"
                ],
                "recommended_decision": (
                    "let the Supervisor reconcile completed shard results and retry "
                    "only unfinished work"
                ),
            },
        )
        host._emit_progress(
            "human_required",
            package_id=package_ids[0] if package_ids else "",
            reason=(
                "a parallel shard wave was interrupted; inspect generated changes "
                "before resuming"
            ),
        )
        host._state_record.error_message = reason
        host._set_active_parallel_wave(None)
        host.transition_to(TaskExecutionState.HUMAN_REQUIRED)
        return True

    def run(self, ready: list[WorkPackage], deferred_ids: set[str]) -> bool:
        host = self._host
        wave = host._build_parallel_wave(ready, deferred_ids)
        if len(wave) < 2:
            return False
        starting = [
            candidate.package
            for candidate in wave
            if candidate.package.stage == WorkPackageStage.PREPARE
        ]
        for package in starting:
            host._validate_clean_start(package)
        for package in starting:
            host._advance_package_stage(package, WorkPackageStage.IMPLEMENT)
            host._journal.append(
                "package_started", {"package_id": package.id, "title": package.title}
            )
            host._emit_progress("package_started", package_id=package.id)

        wave_id = f"wave-{uuid.uuid4().hex[:12]}"
        prepared: list[ParallelShardCandidate] = []
        try:
            for candidate in wave:
                prepared.append(
                    host._prepare_parallel_write_isolation(candidate, wave_id=wave_id)
                )
        except Exception as exc:
            for candidate in prepared:
                host._cleanup_parallel_isolation(candidate)
            package = (
                wave[len(prepared)].package
                if len(prepared) < len(wave)
                else wave[0].package
            )
            host.escalate_scope_failure(
                package, f"could not prepare isolated parallel shard worktree: {exc}"
            )
            return True

        try:
            finalized_wave: list[ParallelShardCandidate] = []
            for candidate in prepared:
                parent_invocation_id = candidate.package.last_invocation_id
                attempt_handoff = candidate.handoff.for_attempt(
                    1,
                    parent_invocation_id=parent_invocation_id,
                    attempt_history=[],
                )
                host._validated_prompt(attempt_handoff)
                finalized_wave.append(
                    replace(
                        candidate,
                        handoff=attempt_handoff,
                        parent_invocation_id=parent_invocation_id,
                    )
                )
            wave = finalized_wave
        except ContextBudgetError as exc:
            for candidate in prepared:
                host._cleanup_parallel_isolation(candidate)
            host.escalate_scope_failure(
                prepared[0].package,
                f"parallel shard context budget preflight failed: {exc}",
            )
            return True

        owners = host._parallel_dirty_owners()
        for candidate in wave:
            if not candidate.read_only:
                for repository in candidate.repository_scope:
                    owners[repository] = candidate.package.id
        host._set_parallel_dirty_owners(owners)
        host._set_active_parallel_wave(
            {
                "wave_id": wave_id,
                "started_at": utc_now(),
                "package_ids": [item.package.id for item in wave],
                "agents": [item.agent_id for item in wave],
                "isolations": [
                    {
                        "package_id": item.package.id,
                        "source_repository_path": item.source_repository_path,
                        "isolation_path": item.isolation_path,
                    }
                    for item in wave
                    if item.isolation_path
                ],
            }
        )
        host.save_state()
        host._journal.append(
            "parallel_wave_started",
            {
                "wave_id": wave_id,
                "packages": [item.package.id for item in wave],
                "agents": [item.agent_id for item in wave],
            },
        )
        host._emit_progress(
            "parallel_wave_started",
            wave_id=wave_id,
            packages=[item.package.id for item in wave],
            agents=[item.agent_id for item in wave],
        )
        futures = {}
        # Multiple isolated shards may intentionally target one repository when
        # policy permits it. Track the source state produced by earlier sibling
        # deltas so those orchestrator-owned changes are not mistaken for an
        # external workspace mutation. Each delta still goes through git-apply,
        # which fails closed when siblings touch incompatible content.
        expected_source_fingerprints: dict[str, str] = {}
        with ThreadPoolExecutor(
            max_workers=len(wave), thread_name_prefix="execraft-shard"
        ) as executor:
            for candidate in wave:
                adapter = host._find_adapter(candidate.agent_id)
                if adapter is None:
                    continue
                metadata = host._agent_metadata(adapter)
                parent_invocation_id = candidate.parent_invocation_id
                attempt_handoff = candidate.handoff
                identity = execution_identity_of(adapter)
                invocation = host._agent_invocations.begin(
                    project_id=host._invocation_project_id,
                    task_id=host.task_id,
                    package_id=candidate.package.id,
                    stage=candidate.package.stage.value,
                    capability=candidate.capability.value,
                    attempt=1,
                    agent_id=identity.provider_id,
                    adapter=metadata["adapter"],
                    model=metadata["model"],
                    execution_identity=identity,
                    parent_invocation_id=parent_invocation_id,
                    triggering_event_id=attempt_handoff.triggering_event_id,
                    handoff=attempt_handoff.as_mapping(),
                    skills=attempt_handoff.skill_manifest,
                    isolation=metadata["execution_capabilities"],
                    workspace_before_digest=host._parallel_workspace_digest(candidate),
                )
                candidate.package.last_invocation_id = invocation.invocation_id
                candidate = replace(
                    candidate,
                    handoff=attempt_handoff,
                    invocation_id=invocation.invocation_id,
                    parent_invocation_id=parent_invocation_id,
                    handoff_sha256=invocation.handoff_sha256,
                    workspace_before_digest=invocation.workspace_before_digest,
                )
                host._journal.append(
                    "agent_invocation_started",
                    {
                        "invocation_id": invocation.invocation_id,
                        "parent_invocation_id": parent_invocation_id,
                        "handoff_sha256": invocation.handoff_sha256,
                        "package_id": candidate.package.id,
                        "stage": candidate.package.stage.value,
                        "capability": candidate.capability.value,
                        "agent_id": candidate.agent_id,
                        "attempt": 1,
                        "parallel_wave_id": wave_id,
                        "skill_manifest": attempt_handoff.skill_manifest,
                        "workspace_before_digest": invocation.workspace_before_digest,
                    },
                )
                host._emit_progress(
                    "agent_attempt_started",
                    package_id=candidate.package.id,
                    stage=attempt_handoff.stage,
                    capability=candidate.capability.value,
                    agent_id=candidate.agent_id,
                    invocation_id=invocation.invocation_id,
                    handoff_sha256=invocation.handoff_sha256,
                    **metadata,
                    attempt=1,
                )
                futures[
                    executor.submit(host._invoke_parallel_candidate, adapter, candidate)
                ] = candidate
            host.save_state()
            for future in as_completed(futures):
                candidate = futures[future]
                if candidate.isolation_path and candidate.source_repository_path:
                    source_path = candidate.source_repository_path
                    candidate = replace(
                        candidate,
                        source_fingerprint=expected_source_fingerprints.setdefault(
                            source_path, candidate.source_fingerprint
                        ),
                    )
                raw, error, duration, runtime_session = future.result()
                if error is not None or raw is None:
                    if (
                        candidate.isolation_path
                        and host._repository_worktree_fingerprint(
                            Path(candidate.source_repository_path)
                        )
                        != candidate.source_fingerprint
                    ):
                        host.escalate_scope_failure(
                            candidate.package,
                            "source repository changed outside the isolated parallel worktree",
                        )
                    else:
                        host._apply_parallel_failure(
                            candidate,
                            error or RuntimeError("parallel agent returned no result"),
                            duration,
                        )
                        host._release_parallel_dirty_ownership(candidate.package)
                    host._cleanup_parallel_isolation(candidate)
                    continue
                try:
                    result = host._validate_parallel_result(
                        candidate, raw, duration_seconds=duration,
                        runtime_session=runtime_session,
                    )
                    host._apply_parallel_isolated_delta(candidate)
                    if candidate.isolation_path and candidate.source_repository_path:
                        expected_source_fingerprints[candidate.source_repository_path] = (
                            host._repository_worktree_fingerprint(
                                Path(candidate.source_repository_path)
                            )
                        )
                    host._apply_parallel_success(candidate, result)
                    host._emit_progress(
                        "agent_attempt_finished",
                        package_id=candidate.package.id,
                        stage=candidate.handoff.stage,
                        capability=candidate.capability.value,
                        agent_id=candidate.agent_id,
                        model=host._agent_metadata(
                            host._find_adapter(candidate.agent_id)
                        )["model"],
                        status="completed",
                        duration_seconds=duration,
                        artifact=result.get("_execraft_agent_artifact", {}),
                    )
                except Exception as exc:
                    host._apply_parallel_failure(candidate, exc, duration)
                    if candidate.isolation_path and not snapshot_repository(
                        candidate.source_repository_id,
                        Path(candidate.source_repository_path),
                    ).dirty:
                        host._release_parallel_dirty_ownership(candidate.package)
                finally:
                    host._cleanup_parallel_isolation(candidate)

        host._set_active_parallel_wave(None)
        parallel_root = host._state_dir / "parallel-worktrees"
        shutil.rmtree(parallel_root / wave_id, ignore_errors=True)
        try:
            parallel_root.rmdir()
        except OSError:
            pass
        host.save_state()
        host._journal.append(
            "parallel_wave_finished",
            {"wave_id": wave_id, "packages": [item.package.id for item in wave]},
        )
        host._emit_progress(
            "parallel_wave_finished",
            wave_id=wave_id,
            packages=[item.package.id for item in wave],
        )
        return True
