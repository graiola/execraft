"""Declared-scope recovery coordinator for workspace isolation and recovery.

Coordinates declared-scope evaluation, check reconciliation, untracked artifact
cleanup, agent-assisted recovery transactions, and Supervisor-facing recovery.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Mapping

from .models import (
    OrchestrateError,
    TaskExecutionState,
    WorkPackage,
    WorkPackageStage,
    _AgentWaitRequested,
    _StageEscalated,
)
from .operator_action import is_repository_scope_action
from .scheduler import (
    AgentCapability,
    StructuredHandoff,
)
from .scope_policy import (
    ScopePathAssessment,
    classify_scope_path,
    repository_scope_patterns,
)
from .supervisor import (
    IncidentStatus,
    SupervisorDelegation,
    SupervisorIncident,
)
from .workspace_recovery import (
    WorkspaceScopeSnapshot,
    build_workspace_scope_snapshot,
    flatten_dirty_paths,
)

logger = logging.getLogger(__name__)


class ScopeRecoveryCoordinator:
    """Coordinate declared-scope recovery, check reconciliation, cleanup, and Supervisor recovery."""

    def __init__(self, host: Any):
        self._host = host
        self._provenance_dir = self._host._state_dir / "scratch-provenance"
        self._provenance_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _enforces_declared_write_scope(package: WorkPackage) -> bool:
        """Return whether exact generated-shard scope applies to ``package``."""

        return bool(
            package.parent_id
            and package.write_scope
            and package.execution_mode != "review_shard"
        )

    def parallel_sibling_owned_repositories(
        self, package: WorkPackage
    ) -> set[str]:
        """Repositories temporarily owned by an active parallel sibling."""

        if not package.parent_id:
            return set()
        owned: set[str] = set()
        for repository_id, owner_id in self._host._parallel_dirty_owners().items():
            try:
                owner = self._host._state_record.plan_graph.package_by_id(owner_id)
            except OrchestrateError:
                continue
            if (
                owner.stage != WorkPackageStage.COMPLETED
                and owner.parent_id == package.parent_id
            ):
                owned.add(repository_id)
        return owned

    def workspace_scope_snapshot(
        self,
        package: WorkPackage,
        *,
        require_clean_workspace: bool = False,
    ) -> WorkspaceScopeSnapshot:
        """Classify the complete workspace using one shared ownership model."""

        return build_workspace_scope_snapshot(
            dirty_paths=self._host._workspace_dirty_paths(),
            affected_repositories=package.affected_repositories,
            write_scope=package.write_scope,
            repository_roots=self._host._repository_paths,
            enforce_write_scope=self._enforces_declared_write_scope(package),
            require_clean_workspace=require_clean_workspace,
            ignored_parallel_repositories=self.parallel_sibling_owned_repositories(
                package
            ),
        )

    def workspace_recovery_candidates(
        self,
        package: WorkPackage,
        *,
        require_clean_workspace: bool = False,
    ) -> list[str]:
        return self.workspace_scope_snapshot(
            package,
            require_clean_workspace=require_clean_workspace,
        ).candidate_paths

    def declared_write_scope_violations(self, package: WorkPackage) -> list[str]:
        """Return changed paths outside a generated shard declaration.

        Paths are repository-qualified (``repository:path``).  Directory
        declarations are recursive when the declared path exists as a
        directory, while wildcard declarations retain normal glob semantics.
        """

        if not self._host.config.strict_checks or not self._enforces_declared_write_scope(
            package
        ):
            return []
        return [
            item.qualified_path
            for item in self.workspace_scope_snapshot(package).candidates
            if item.relationship == "outside_write_scope"
        ]

    def _scope_changed_lines(self, qualified_path: str) -> int:
        """Return a conservative changed-line count for one path."""

        repository_id, separator, relative = qualified_path.partition(":")
        if not separator:
            return self._host.config.scope_policy.max_changed_lines + 1
        repository = self._host._resolve_repo_path_if_available(repository_id)
        if repository is None:
            return self._host.config.scope_policy.max_changed_lines + 1

        completed = subprocess.run(
            ["git", "diff", "--numstat", "HEAD", "--", relative],
            cwd=repository,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise OrchestrateError(
                f"could not inspect changed lines for {qualified_path}: "
                f"{completed.stderr.strip() or completed.stdout.strip()}"
            )
        total = 0
        for line in completed.stdout.splitlines():
            fields = line.split("\t", 2)
            if len(fields) < 2 or "-" in fields[:2]:
                return self._host.config.scope_policy.max_changed_lines + 1
            total += int(fields[0]) + int(fields[1])

        if total == 0:
            candidate = repository / relative
            if candidate.is_file():
                try:
                    data = candidate.read_bytes()
                except OSError:
                    return self._host.config.scope_policy.max_changed_lines + 1
                if b"\x00" in data:
                    return self._host.config.scope_policy.max_changed_lines + 1
                total = data.count(b"\n") + (
                    1 if data and not data.endswith(b"\n") else 0
                )
        return total

    def scope_assessments(
        self,
        package: WorkPackage,
        violations: list[str] | None = None,
    ) -> list[ScopePathAssessment]:
        assessments: list[ScopePathAssessment] = []
        changed_paths = (
            violations
            if violations is not None
            else self.declared_write_scope_violations(package)
        )
        for qualified in changed_paths:
            repository_id, _, _ = qualified.partition(":")
            patterns = repository_scope_patterns(repository_id, package.write_scope)
            assessments.append(
                classify_scope_path(
                    qualified,
                    patterns=patterns,
                    policy=self._host.config.scope_policy,
                    changed_lines=self._scope_changed_lines(qualified),
                )
            )
        return assessments

    def declared_write_scope_report(self, package_id: str) -> dict[str, Any]:
        """Describe package ownership and the complete dirty workspace delta."""

        package = self._host._state_record.plan_graph.package_by_id(package_id)
        workspace_scope = self.workspace_scope_snapshot(package)
        violations = [
            item.qualified_path
            for item in workspace_scope.candidates
            if item.relationship == "outside_write_scope"
        ]
        assessments = self.scope_assessments(package, violations)
        safe = [item for item in assessments if item.auto_expandable]
        safe_within_budget = (
            self._host.config.scope_policy.enabled
            and len(safe) <= self._host.config.scope_policy.max_files
            and sum(item.changed_lines for item in safe)
            <= self._host.config.scope_policy.max_changed_lines
        )
        return {
            "package_id": package.id,
            "stage": package.stage.value,
            "parent_id": package.parent_id,
            "affected_repositories": list(package.affected_repositories),
            "write_scope": list(package.write_scope),
            "violations": violations,
            "assessments": [item.as_mapping() for item in assessments],
            "auto_expandable_paths": (
                [item.qualified_path for item in safe] if safe_within_budget else []
            ),
            "scope_policy": self._host.config.scope_policy.as_mapping(),
            "scope_recovery": self._host.config.scope_recovery_policy.as_mapping(),
            "workspace_scope": workspace_scope.as_mapping(),
            "reconciliation": (
                self._scope_check_reconciliation_status(package.id)
                if self._host.state == TaskExecutionState.HUMAN_REQUIRED
                else {"can_reconcile": False, "reason": "project is not human_required"}
            ),
            "auto_recovery": (
                self._scope_check_auto_recovery_status(package.id)
                if self._host.state == TaskExecutionState.HUMAN_REQUIRED
                else {"can_recover": False, "reason": "project is not human_required"}
            ),
        }

    def append_exact_scope_paths(
        self,
        package: WorkPackage,
        qualified_paths: list[str],
    ) -> list[str]:
        additions: list[str] = []
        for qualified in qualified_paths:
            repository_id, separator, relative = qualified.partition(":")
            if not separator or repository_id not in package.affected_repositories:
                raise OrchestrateError(
                    f"cannot approve path outside affected repositories: {qualified}"
                )
            declared = f"{repository_id}/{relative}"
            if declared not in package.write_scope:
                package.write_scope.append(declared)
                additions.append(declared)
        return additions

    def append_recovered_scope_paths(
        self,
        package: WorkPackage,
        qualified_paths: list[str],
    ) -> tuple[list[str], list[str]]:
        """Extend package ownership with exact, validated recovery paths."""

        added_repositories: list[str] = []
        for qualified in qualified_paths:
            repository_id, separator, _relative = qualified.partition(":")
            if not separator or repository_id not in self._host._repository_paths:
                raise OrchestrateError(
                    f"cannot recover path from an unknown repository: {qualified}"
                )
            if repository_id not in package.affected_repositories:
                package.affected_repositories.append(repository_id)
                added_repositories.append(repository_id)

        added_paths = self.append_exact_scope_paths(package, qualified_paths)
        if added_repositories:
            # A generated shard whose ownership expanded across repositories is
            # no longer safe for a future parallel wave.
            package.parallel_safe = False
        return added_paths, added_repositories

    def _auto_expand_declared_write_scope(
        self,
        package: WorkPackage,
        violations: list[str],
    ) -> list[str]:
        """Admit bounded safe supporting changes as exact paths."""

        policy = self._host.config.scope_policy
        if not policy.enabled or not violations:
            return []
        assessments = self.scope_assessments(package, violations)
        safe = [item for item in assessments if item.auto_expandable]
        if not safe:
            return []
        if len(safe) > policy.max_files:
            return []
        changed_lines = sum(item.changed_lines for item in safe)
        if changed_lines > policy.max_changed_lines:
            return []

        additions = self.append_exact_scope_paths(
            package,
            [item.qualified_path for item in safe],
        )
        if not additions:
            return []
        payload = {
            "package_id": package.id,
            "added_paths": additions,
            "changed_lines": changed_lines,
            "classifications": [item.as_mapping() for item in safe],
            "remaining_violations": self.declared_write_scope_violations(package),
        }
        self._host.save_state()
        self._host._journal.append("write_scope_auto_expanded", payload)
        self._host._emit_progress("write_scope_auto_expanded", **payload)
        return additions

    def _active_scope_context(
        self,
        package_id: str | None = None,
    ) -> tuple[WorkPackage, dict[str, Any]]:
        if self._host.state != TaskExecutionState.HUMAN_REQUIRED:
            raise OrchestrateError(
                "repository scope can only be reconciled while the project is human_required"
            )
        context = self._host.human_required_report() or {}
        if not is_repository_scope_action(context):
            raise OrchestrateError(
                "the active human-required event is not a write-scope failure"
            )
        active_package_id = str(context.get("package_id", "")).strip()
        if package_id and active_package_id != package_id:
            raise OrchestrateError(
                f"the active repository-scope failure belongs to {active_package_id!r}, "
                f"not {package_id!r}"
            )
        if not active_package_id:
            raise OrchestrateError("the active repository-scope failure has no package ID")
        return self._host._state_record.plan_graph.package_by_id(active_package_id), context

    def _scope_check_reconciliation_status(
        self,
        package_id: str | None = None,
    ) -> dict[str, Any]:
        """Return whether a persisted workspace-ownership check is resolved."""

        try:
            package, context = self._active_scope_context(package_id)
        except OrchestrateError as exc:
            return {"can_reconcile": False, "reason": str(exc)}

        context_text = self._scope_check_context_text(context)
        requires_clean_workspace = (
            package.stage == WorkPackageStage.COMPLETED
            or "workspace must be clean before a new package starts" in context_text
        )
        if requires_clean_workspace:
            dirty = self._host._workspace_dirty_paths()
            if dirty:
                return {
                    "can_reconcile": False,
                    "reason": "workspace is still dirty",
                    "package_id": package.id,
                    "dirty_paths": dirty,
                }
            return {
                "can_reconcile": True,
                "reason": "the recorded clean-workspace condition is no longer present",
                "package_id": package.id,
                "stage": package.stage.value,
                "authorized_dirty_paths": {},
                "context": context,
            }

        snapshot = self.workspace_scope_snapshot(package)
        out_of_scope = [
            item.qualified_path
            for item in snapshot.candidates
            if item.relationship == "outside_write_scope"
        ]
        if out_of_scope:
            return {
                "can_reconcile": False,
                "reason": "write-scope violations remain",
                "package_id": package.id,
                "violations": out_of_scope,
            }

        undeclared: dict[str, list[str]] = {}
        for item in snapshot.candidates:
            if item.relationship == "undeclared_repository":
                undeclared.setdefault(item.repository_id, []).append(
                    item.relative_path
                )
        if undeclared:
            return {
                "can_reconcile": False,
                "reason": "repositories outside the package scope are still dirty",
                "package_id": package.id,
                "dirty_paths": undeclared,
            }

        authorized_dirty: dict[str, list[str]] = {}
        for qualified in snapshot.authorized_paths:
            repository_id, _separator, relative = qualified.partition(":")
            authorized_dirty.setdefault(repository_id, []).append(relative)
        return {
            "can_reconcile": True,
            "reason": "the recorded repository-scope condition is no longer present",
            "package_id": package.id,
            "stage": package.stage.value,
            "authorized_dirty_paths": authorized_dirty,
            "context": context,
        }

    def can_auto_reconcile_scope_check(self) -> bool:
        """Return whether Run / Resume may safely clear a stale scope check."""

        return bool(self._scope_check_reconciliation_status().get("can_reconcile"))

    @staticmethod
    def _scope_check_context_text(context: Mapping[str, Any]) -> str:
        return " ".join(
            [
                str(context.get("reason", "")),
                str(context.get("blocked_requirement", "")),
            ]
            + [str(item) for item in context.get("evidence", [])]
        ).lower()

    def _scope_recovery_candidates_for_context(
        self,
        package: WorkPackage,
        context: Mapping[str, Any],
    ) -> list[str]:
        clean_start = (
            "workspace must be clean before a new package starts"
            in self._scope_check_context_text(context)
        )
        return self.workspace_recovery_candidates(
            package,
            require_clean_workspace=clean_start,
        )

    def _scope_check_next_stage(
        self,
        package: WorkPackage,
        context: Mapping[str, Any],
    ) -> WorkPackageStage:
        next_stage = package.stage
        # A repository-scope stop is raised after the write-capable stage has
        # completed.  Once its delta is approved, restored, or otherwise made
        # clean, never dispatch that writer a second time.  Re-enter at the
        # corresponding verification boundary instead.  Older logic only did
        # this when one particular error contained the words
        # "outside write_scope"; protected-path and cross-repository errors use
        # different wording and consequently looped in fix_review forever.
        if package.stage == WorkPackageStage.IMPLEMENT:
            next_stage = WorkPackageStage.FAST_VERIFY
        elif package.stage == WorkPackageStage.FIX_REVIEW:
            next_stage = WorkPackageStage.REGRESSION_VERIFY
        return next_stage

    def _finalize_resolved_scope_check(
        self,
        package: WorkPackage,
        context: Mapping[str, Any],
        *,
        automatic: bool,
        reason: str,
    ) -> dict[str, Any]:
        """Persist one resolved scope check from HUMAN_REQUIRED or RUNNING."""

        previous_stage = package.stage
        next_stage = self._scope_check_next_stage(package, context)
        if next_stage != previous_stage:
            self._host._advance_package_stage(package, next_stage)
        previous_status = package.status
        if package.stage != WorkPackageStage.COMPLETED:
            package.status = "pending"

        payload = {
            "package_id": package.id,
            "previous_stage": previous_stage.value,
            "next_stage": next_stage.value,
            "previous_status": previous_status,
            "next_status": package.status,
            "automatic": automatic,
            "reason": reason,
        }
        self._host._state_record.error_message = ""
        self._host.save_state()
        self._host._journal.append("repository_scope_check_reconciled", payload)
        self._host._emit_progress("repository_scope_check_reconciled", **payload)
        if self._host.state == TaskExecutionState.HUMAN_REQUIRED:
            self._host.transition_to(TaskExecutionState.RUNNING)
        elif self._host.state != TaskExecutionState.RUNNING:
            raise OrchestrateError(
                "resolved repository-scope check from unexpected project state "
                f"{self._host.state.value}"
            )
        return self.declared_write_scope_report(package.id) | {
            "reconciled": True,
            **payload,
        }

    def _scope_recovery_resume_fingerprint(
        self,
        package: WorkPackage,
        candidates: list[str],
    ) -> str:
        path_states = [
            {
                "path": qualified,
                "digest": self._scope_path_state_digest(qualified),
            }
            for qualified in sorted(candidates)
        ]
        body = json.dumps(
            {
                "package_id": package.id,
                "stage": package.stage.value,
                "path_states": path_states,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(body.encode("utf-8")).hexdigest()[:20]

    def _scope_path_state_digest(self, qualified_path: str) -> str:
        """Hash the exact tracked/untracked state used by the retry budget."""

        repository_id, separator, relative = qualified_path.partition(":")
        if not separator:
            return "invalid"
        repository = self._host._resolve_repo_path_if_available(repository_id)
        if repository is None:
            return "unavailable"

        completed = subprocess.run(
            [
                "git",
                "diff",
                "--binary",
                "--no-ext-diff",
                "HEAD",
                "--",
                relative,
            ],
            cwd=repository,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise OrchestrateError(
                f"could not fingerprint scope path {qualified_path}: "
                + completed.stderr.decode("utf-8", errors="replace").strip()
            )
        digest = hashlib.sha256(completed.stdout)
        if completed.stdout:
            return digest.hexdigest()

        target = repository / relative
        try:
            if target.is_symlink():
                digest.update(b"symlink\0")
                digest.update(os.fsencode(target.readlink()))
                return digest.hexdigest()
            if target.is_file():
                digest.update(b"file\0")
                with target.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                return digest.hexdigest()
            if not target.exists():
                return hashlib.sha256(b"missing").hexdigest()
        except OSError as exc:
            raise OrchestrateError(
                f"could not fingerprint scope path {qualified_path}: {exc}"
            ) from exc
        return hashlib.sha256(b"non-regular").hexdigest()

    def _scope_recovery_resume_attempts(self, fingerprint: str) -> int:
        return sum(
            1
            for entry in self._host._journal.read()
            if entry.event_type == "scope_recovery_resume_started"
            and str(entry.payload.get("fingerprint", "")) == fingerprint
        )

    def _scope_check_auto_recovery_status(
        self,
        package_id: str | None = None,
    ) -> dict[str, Any]:
        """Describe bounded automatic cleanup/recovery for an active scope check."""

        policy = self._host.config.scope_recovery_policy
        if not policy.enabled or not policy.auto_resume:
            return {
                "can_recover": False,
                "reason": "automatic scope recovery is disabled",
            }
        try:
            package, context = self._active_scope_context(package_id)
        except OrchestrateError as exc:
            return {"can_recover": False, "reason": str(exc)}

        context_text = self._scope_check_context_text(context)
        clean_start = "workspace must be clean before a new package starts" in context_text
        snapshot = self.workspace_scope_snapshot(
            package,
            require_clean_workspace=clean_start,
        )
        dirty_qualified = snapshot.candidate_paths
        cleanup_paths = self._scope_artifact_candidates(
            package,
            dirty_qualified,
            affected_only=False,
        )
        if cleanup_paths:
            return {
                "can_recover": True,
                "mode": "deterministic_cleanup",
                "reason": "configured untracked artifacts can be removed",
                "package_id": package.id,
                "cleanup_paths": cleanup_paths,
                "context": context,
            }

        candidates = snapshot.candidate_paths
        if not candidates:
            return {
                "can_recover": False,
                "reason": "no recoverable workspace-scope violations remain",
                "package_id": package.id,
            }
        if clean_start:
            return {
                "can_recover": False,
                "reason": (
                    "tracked clean-start contamination cannot be attributed to a package "
                    "automatically"
                ),
                "package_id": package.id,
                "violations": candidates,
            }
        foreign = [
            item.qualified_path
            for item in snapshot.candidates
            if item.relationship == "undeclared_repository"
        ]
        if foreign and not policy.allow_cross_repository:
            return {
                "can_recover": False,
                "reason": "cross-repository automatic recovery is disabled",
                "package_id": package.id,
                "violations": candidates,
            }
        if package.stage == WorkPackageStage.COMPLETED:
            return {
                "can_recover": False,
                "reason": "completed packages cannot acquire additional workspace changes",
                "package_id": package.id,
                "violations": candidates,
            }

        fingerprint = self._scope_recovery_resume_fingerprint(package, candidates)
        attempts = self._scope_recovery_resume_attempts(fingerprint)
        if attempts >= policy.max_resume_attempts:
            return {
                "can_recover": False,
                "reason": (
                    "automatic scope recovery exhausted "
                    f"{attempts}/{policy.max_resume_attempts} attempts"
                ),
                "package_id": package.id,
                "violations": candidates,
                "fingerprint": fingerprint,
                "attempts": attempts,
                "max_attempts": policy.max_resume_attempts,
            }
        return {
            "can_recover": True,
            "mode": "agent",
            "reason": "the complete out-of-scope workspace delta can be recovered",
            "package_id": package.id,
            "violations": candidates,
            "candidate_repositories": snapshot.candidate_repositories,
            "fingerprint": fingerprint,
            "attempts": attempts,
            "max_attempts": policy.max_resume_attempts,
            "context": context,
        }

    def can_auto_recover_scope_check(self) -> bool:
        return bool(self._scope_check_auto_recovery_status().get("can_recover"))

    def pending_scope_recovery_wait(self) -> dict[str, Any]:
        """Return the durable agent wait that belongs to scope recovery."""

        waits = dict(self._host._state_record.agent_waits or {})
        preferred_id = str((self._host._state_record.waiting or {}).get("package_id", ""))
        ordered = []
        if preferred_id and preferred_id in waits:
            ordered.append(waits[preferred_id])
        ordered.extend(
            value
            for key, value in sorted(waits.items())
            if key != preferred_id
        )
        for waiting in ordered:
            recovery = waiting.get("scope_recovery")
            if isinstance(recovery, Mapping):
                return dict(waiting)
        return {}

    def _mark_scope_recovery_wait(
        self,
        package: WorkPackage,
        *,
        context: Mapping[str, Any],
        fingerprint: str,
        attempt: int,
        violations: list[str],
    ) -> None:
        """Attach recovery intent to an ordinary durable provider wait."""

        waits = dict(self._host._state_record.agent_waits or {})
        waiting = dict(waits.get(package.id) or self._host._state_record.waiting or {})
        waiting["scope_recovery"] = {
            "context": dict(context),
            "fingerprint": fingerprint,
            "attempt": attempt,
            "violations": list(violations),
        }
        waits[package.id] = waiting
        self._host._state_record.agent_waits = waits
        self._host._refresh_wait_summary()
        self._host.save_state()
        self._host._journal.append(
            "scope_recovery_wait_scheduled",
            {
                "package_id": package.id,
                "fingerprint": fingerprint,
                "attempt": attempt,
                "violations": list(violations),
                "next_check_at": str(waiting.get("next_check_at", "")),
            },
        )

    def _finish_scope_recovery_attempt(
        self,
        package: WorkPackage,
        *,
        context: Mapping[str, Any],
        fingerprint: str,
        attempt: int,
        recovered: bool,
        detail: str,
    ) -> bool:
        """Advance a successful attempt or re-escalate an ineffective one."""

        remaining = self._scope_recovery_candidates_for_context(package, context)
        if recovered and not remaining:
            self._finalize_resolved_scope_check(
                package,
                context,
                automatic=True,
                reason="agent-assisted recovery resolved the persisted scope check",
            )
            return True

        if self._host.state != TaskExecutionState.RUNNING:
            raise OrchestrateError(
                "scope recovery returned from unexpected project state "
                f"{self._host.state.value}"
            )
        message = detail or "agent-assisted recovery did not resolve the scope check"
        if remaining:
            message += "; remaining violations: " + ", ".join(remaining)
        self._host._journal.append(
            "scope_recovery_resume_failed",
            {
                "package_id": package.id,
                "fingerprint": fingerprint,
                "attempt": attempt,
                "reason": message,
                "remaining_violations": remaining,
            },
        )
        self.escalate_scope_failure(
            package,
            "automatic scope recovery failed: " + message,
        )
        return False

    def resume_pending_scope_recovery_unlocked(
        self,
        waiting: Mapping[str, Any],
    ) -> bool:
        """Continue scope recovery after a provider-availability wait."""

        package_id = str(waiting.get("package_id", "")).strip()
        recovery = waiting.get("scope_recovery")
        if not package_id or not isinstance(recovery, Mapping):
            return False
        package = self._host._state_record.plan_graph.package_by_id(package_id)
        context = dict(recovery.get("context") or {})
        fingerprint = str(recovery.get("fingerprint", ""))
        attempt = int(recovery.get("attempt", 1))
        violations = self._scope_recovery_candidates_for_context(package, context)
        if not violations:
            self._host._clear_agent_wait(package.id)
            self._host.transition_to(TaskExecutionState.RUNNING)
            self._finalize_resolved_scope_check(
                package,
                context,
                automatic=True,
                reason="scope condition disappeared while waiting for a recovery agent",
            )
            return True

        self._host.transition_to(TaskExecutionState.RUNNING)
        try:
            recovered, detail = self._attempt_agent_scope_recovery(
                package,
                context=context,
            )
        except _AgentWaitRequested:
            self._mark_scope_recovery_wait(
                package,
                context=context,
                fingerprint=fingerprint,
                attempt=attempt,
                violations=violations,
            )
            return False
        except OrchestrateError as exc:
            recovered, detail = False, str(exc)
        return self._finish_scope_recovery_attempt(
            package,
            context=context,
            fingerprint=fingerprint,
            attempt=attempt,
            recovered=recovered,
            detail=detail,
        )

    def auto_recover_scope_check_unlocked(self) -> bool:
        """Resolve an active scope check, returning whether the pipeline may continue."""

        package, context = self._active_scope_context()
        dirty_qualified = flatten_dirty_paths(self._host._workspace_dirty_paths())
        removed = self.cleanup_scope_artifacts(
            package,
            dirty_qualified,
            affected_only=False,
            source="human_required_resume",
        )
        if removed and self.can_auto_reconcile_scope_check():
            self.reconcile_resolved_scope_check_unlocked(automatic=True)
            return True

        status = self._scope_check_auto_recovery_status(package.id)
        if not status.get("can_recover") or status.get("mode") != "agent":
            return False

        violations = [str(item) for item in status.get("violations", [])]
        fingerprint = str(status.get("fingerprint", ""))
        attempt = int(status.get("attempts", 0)) + 1
        self._host._journal.append(
            "scope_recovery_resume_started",
            {
                "package_id": package.id,
                "fingerprint": fingerprint,
                "attempt": attempt,
                "max_attempts": int(status.get("max_attempts", 0)),
                "violations": violations,
            },
        )
        self._host._emit_progress(
            "scope_recovery_resume_started",
            package_id=package.id,
            attempt=attempt,
            max_attempts=int(status.get("max_attempts", 0)),
        )
        self._host.transition_to(TaskExecutionState.RUNNING)
        try:
            recovered, detail = self._attempt_agent_scope_recovery(
                package,
                context=context,
            )
        except _AgentWaitRequested:
            self._mark_scope_recovery_wait(
                package,
                context=context,
                fingerprint=fingerprint,
                attempt=attempt,
                violations=violations,
            )
            return False
        except OrchestrateError as exc:
            recovered, detail = False, str(exc)
        return self._finish_scope_recovery_attempt(
            package,
            context=context,
            fingerprint=fingerprint,
            attempt=attempt,
            recovered=recovered,
            detail=detail,
        )

    def reconcile_resolved_scope_check_unlocked(
        self,
        package_id: str | None = None,
        *,
        automatic: bool = False,
        note: str = "",
    ) -> dict[str, Any]:
        status = self._scope_check_reconciliation_status(package_id)
        if not status.get("can_reconcile"):
            raise OrchestrateError(str(status.get("reason", "scope check is unresolved")))

        package = self._host._state_record.plan_graph.package_by_id(str(status["package_id"]))
        reason = note or str(status["reason"])
        return self._finalize_resolved_scope_check(
            package,
            dict(status.get("context") or {}),
            automatic=automatic,
            reason=reason,
        )

    def reconcile_resolved_scope_check(
        self,
        package_id: str | None = None,
        *,
        note: str = "",
    ) -> dict[str, Any]:
        """Explicitly clear a repository-scope check whose condition is gone."""

        with self._host._exclusive_run_lock():
            return self.reconcile_resolved_scope_check_unlocked(
                package_id,
                automatic=False,
                note=note,
            )

    def approve_declared_write_scope(
        self,
        package_id: str,
        *,
        expected_candidates: list[str] | None = None,
    ) -> dict[str, Any]:
        """Authorize the exact current candidates through the operator check."""

        from .scope_approval import approve_declared_write_scope

        return approve_declared_write_scope(
            self,
            package_id,
            expected_candidates=expected_candidates,
        )

    def _scope_recovery_agent(self, package: WorkPackage) -> tuple[str, str]:
        """Select a write-capable recovery agent, preferring the reviewer.

        A reviewer used for recovery becomes a fixer and is later rebalanced out
        of review roles, preserving independence for the actual code review.
        """

        policy = self._host.config.scope_recovery_policy
        reviewer = package.reviewer_id
        if policy.prefer_reviewer and reviewer:
            reason = self._host._agent_ineligibility_reason(
                reviewer, AgentCapability.FIX_REVIEW, package
            )
            if not reason:
                return reviewer, "reviewer"

        independent = self._host._select_agent_for_capability(
            AgentCapability.FIX_REVIEW,
            package=package,
            exclude_ids={package.agent_id} if package.agent_id else set(),
            preference_role="fix_review",
        )
        if independent:
            return independent, "independent_fixer"

        if policy.allow_implementer_fallback:
            fallback = self._host._select_agent_for_capability(
                AgentCapability.FIX_REVIEW,
                package=package,
                preference_role="fix_review",
            )
            if fallback:
                return fallback, "implementer_fallback"
        return "", "unavailable"

    def scope_path_is_untracked(
        self, repository_id: str, repository: Path, relative: str
    ) -> bool:
        completed = subprocess.run(
            [
                "git",
                "ls-files",
                "--others",
                "--exclude-standard",
                "--",
                relative,
            ],
            cwd=repository,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise OrchestrateError(
                f"could not classify untracked scope path {repository_id}:{relative}: "
                f"{completed.stderr.strip() or completed.stdout.strip()}"
            )
        return relative in {
            line.strip().replace("\\", "/")
            for line in completed.stdout.splitlines()
            if line.strip()
        }

    @staticmethod
    def remove_empty_untracked_parents(repository: Path, path: Path) -> None:
        parent = path.parent
        while parent != repository:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

    def _untracked_artifact_candidates(
        self,
        package: WorkPackage,
        qualified_paths: list[str],
        *,
        patterns: tuple[str, ...],
        affected_only: bool,
    ) -> list[str]:
        """Return allow-listed untracked files without mutating the workspace."""

        candidates: list[str] = []
        affected = set(package.affected_repositories)
        for qualified in qualified_paths:
            repository_id, separator, relative = qualified.partition(":")
            if not separator:
                continue
            if affected_only and repository_id not in affected:
                continue
            if repository_id not in self._host._repository_paths:
                continue
            if not any(
                fnmatch.fnmatchcase(relative, pattern) for pattern in patterns
            ):
                continue
            repository = self._host._resolve_repo_path_if_available(repository_id)
            if repository is None or not self.scope_path_is_untracked(
                repository_id, repository, relative
            ):
                continue
            target = repository / relative
            try:
                target.lstat()
            except FileNotFoundError:
                continue
            if target.is_file() or target.is_symlink():
                candidates.append(qualified)
        return list(dict.fromkeys(candidates))

    def _scope_artifact_candidates(
        self,
        package: WorkPackage,
        qualified_paths: list[str],
        *,
        affected_only: bool,
    ) -> list[str]:
        """Return scope-recovery artifacts without mutating the workspace."""

        policy = self._host.config.scope_recovery_policy
        if not policy.cleanup_untracked_artifacts:
            return []
        return self._untracked_artifact_candidates(
            package,
            qualified_paths,
            patterns=policy.cleanup_patterns,
            affected_only=affected_only,
        )

    def _has_active_provenance(
        self, qualified_path: str, active_invocation_id: str | None
    ) -> bool:
        """Check if artifact has provenance from an active invocation."""
        if active_invocation_id is None:
            return False
        provenance_file = self._provenance_dir / f"{active_invocation_id}.json"
        if not provenance_file.exists():
            return False
        artifacts = json.loads(provenance_file.read_text(encoding="utf-8"))
        return qualified_path in artifacts

    def _remove_untracked_artifacts(
        self,
        package: WorkPackage,
        qualified_paths: list[str],
        *,
        patterns: tuple[str, ...],
        affected_only: bool,
        source: str,
        event_type: str,
    ) -> list[str]:
        """Remove allow-listed artifacts, provenance-gating Supervisor cleanup."""

        supervisor_recovery = source == "supervisor_preflight"
        active_invocation = (
            self._host._get_active_invocation_id(package.id)
            if supervisor_recovery
            else None
        )

        removed: list[str] = []
        escalated: list[str] = []
        for qualified in self._untracked_artifact_candidates(
            package,
            qualified_paths,
            patterns=patterns,
            affected_only=affected_only,
        ):
            repository_id, separator, relative = qualified.partition(":")
            if not separator:
                continue
            repository = self._host._resolve_repo_path_if_available(repository_id)
            if repository is None:
                continue
            target = repository / relative
            try:
                target.lstat()
            except FileNotFoundError:
                continue

            if not supervisor_recovery or self._has_active_provenance(
                qualified, active_invocation
            ):
                target.unlink()
                self.remove_empty_untracked_parents(repository, target)
                removed.append(qualified)
            else:
                escalated.append(qualified)

        if escalated:
            self._host._journal.append(
                "supervisor_escalation_required",
                {
                    "package_id": package.id,
                    "reason": "unknown_untracked_files",
                    "paths": escalated,
                    "source": source,
                },
            )

        if removed:
            payload = {
                "package_id": package.id,
                "paths": removed,
                "patterns": list(patterns),
                "source": source,
            }
            self._host._journal.append(event_type, payload)
            self._host._emit_progress(event_type, **payload)
        return removed

    def cleanup_scope_artifacts(
        self,
        package: WorkPackage,
        violations: list[str],
        *,
        affected_only: bool = True,
        source: str = "scope_violation",
    ) -> list[str]:
        """Delete only configured untracked scope-recovery artifacts."""

        policy = self._host.config.scope_recovery_policy
        if not policy.cleanup_untracked_artifacts:
            return []
        return self._remove_untracked_artifacts(
            package,
            violations,
            patterns=policy.cleanup_patterns,
            affected_only=affected_only,
            source=source,
            event_type="scope_recovery_artifacts_removed",
        )


    def cleanup_finalization_artifacts(
        self,
        package: WorkPackage,
        qualified_paths: list[str],
        *,
        source: str,
        affected_only: bool,
    ) -> list[str]:
        """Delete only configured untracked package-finalization artifacts."""

        policy = self._host.config.workspace_finalization_policy
        if not policy.enabled or not policy.cleanup_untracked_artifacts:
            return []
        return self._remove_untracked_artifacts(
            package,
            qualified_paths,
            patterns=policy.cleanup_patterns,
            affected_only=affected_only,
            source=source,
            event_type="workspace_finalization_artifacts_removed",
        )

    def _scope_recovery_excerpts(
        self, assessments: list[ScopePathAssessment]
    ) -> dict[str, str]:
        """Build bounded textual evidence for the recovery agent."""

        remaining = self._host.config.scope_recovery_policy.max_excerpt_bytes
        excerpts: dict[str, str] = {}
        for assessment in assessments:
            if remaining <= 0:
                break
            repository = self._host._resolve_repo_path_if_available(
                assessment.repository_id
            )
            if repository is None:
                continue
            completed = subprocess.run(
                [
                    "git",
                    "diff",
                    "--no-ext-diff",
                    "--no-color",
                    "HEAD",
                    "--",
                    assessment.relative_path,
                ],
                cwd=repository,
                text=True,
                capture_output=True,
                check=False,
            )
            if completed.returncode != 0:
                body = (completed.stderr or completed.stdout).strip()
            else:
                body = completed.stdout
            if not body:
                target = repository / assessment.relative_path
                try:
                    if target.is_symlink():
                        body = f"Untracked symbolic link -> {target.readlink()}"
                    elif target.is_file():
                        data = target.read_bytes()[: min(remaining, 64_000)]
                        if b"\0" in data:
                            body = (
                                f"Binary untracked file ({target.stat().st_size} bytes)."
                            )
                        else:
                            body = data.decode("utf-8", errors="replace")
                    else:
                        body = "No textual diff or regular-file preview is available."
                except (OSError, IsADirectoryError):
                    body = "No textual diff or regular-file preview is available."
            header = (
                f"category={assessment.category}; reason={assessment.reason}; "
                f"changed_lines={assessment.changed_lines}\n"
            )
            text = header + body
            encoded = text.encode("utf-8", errors="replace")
            if len(encoded) > remaining:
                text = encoded[:remaining].decode("utf-8", errors="replace")
            excerpts[assessment.qualified_path] = text
            remaining -= len(text.encode("utf-8", errors="replace"))
        return excerpts

    def _approve_agent_recovery_paths(
        self,
        package: WorkPackage,
        requested: list[str],
        candidate_paths: set[str],
    ) -> tuple[list[str], list[str]]:
        """Validate and append exact paths proposed by the recovery agent."""

        policy = self._host.config.scope_recovery_policy
        normalized = list(
            dict.fromkeys(
                str(item).strip() for item in requested if str(item).strip()
            )
        )
        unknown = [item for item in normalized if item not in candidate_paths]
        if unknown:
            raise OrchestrateError(
                "scope recovery agent proposed paths outside its candidate set: "
                + ", ".join(unknown)
            )
        if len(normalized) > policy.max_files:
            raise OrchestrateError(
                f"scope recovery proposed {len(normalized)} paths; limit is {policy.max_files}"
            )

        repositories = list(
            dict.fromkeys(item.partition(":")[0] for item in normalized)
        )
        if len(repositories) > policy.max_repositories:
            raise OrchestrateError(
                f"scope recovery proposed {len(repositories)} repositories; "
                f"limit is {policy.max_repositories}"
            )
        foreign = [
            repository_id
            for repository_id in repositories
            if repository_id not in package.affected_repositories
        ]
        if foreign and not policy.allow_cross_repository:
            raise OrchestrateError(
                "scope recovery cannot retain paths from undeclared repositories: "
                + ", ".join(foreign)
            )

        assessments = self.scope_assessments(package, normalized)
        protected = [
            item.qualified_path for item in assessments if item.category == "protected"
        ]
        if protected:
            raise OrchestrateError(
                "scope recovery cannot approve protected paths: "
                + ", ".join(protected)
            )
        changed_lines = sum(item.changed_lines for item in assessments)
        if changed_lines > policy.max_changed_lines:
            raise OrchestrateError(
                f"scope recovery proposed {changed_lines} changed lines; "
                f"limit is {policy.max_changed_lines}"
            )
        return self.append_recovered_scope_paths(package, normalized)

    def _attempt_agent_scope_recovery(
        self,
        package: WorkPackage,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> tuple[bool, str]:
        """Run one bounded agent-assisted workspace recovery transaction.

        The candidate set covers both paths outside a generated shard's exact
        ``write_scope`` and dirty repositories not yet owned by the package.
        The agent repairs or restores files; the orchestrator validates the
        final delta, extends package ownership only with exact paths, reruns
        verification/review when needed, and retains sole ownership of commits.
        """

        policy = self._host.config.scope_recovery_policy
        if not policy.enabled or package.stage == WorkPackageStage.COMPLETED:
            return False, "automatic workspace recovery is not enabled for this package"

        removed_before = self.cleanup_scope_artifacts(
            package,
            flatten_dirty_paths(self._host._workspace_dirty_paths()),
            affected_only=False,
            source="workspace_recovery",
        )
        current_snapshot = self.workspace_scope_snapshot(package)
        current = current_snapshot.candidate_paths
        if not current:
            event = {
                "package_id": package.id,
                "agent_id": "",
                "selection_policy": "deterministic_cleanup",
                "removed_paths": removed_before,
                "added_scope_paths": [],
                "added_repositories": [],
                "remaining_violations": [],
            }
            self._host.save_state()
            self._host._journal.append("scope_recovery_completed", event)
            self._host._emit_progress("scope_recovery_completed", **event)
            return True, "configured untracked artifacts were removed"

        candidate_paths = set(current)
        foreign_repositories = {
            item.repository_id
            for item in current_snapshot.candidates
            if item.relationship == "undeclared_repository"
        }
        if foreign_repositories and not policy.allow_cross_repository:
            return False, (
                "cross-repository automatic recovery is disabled: "
                + ", ".join(sorted(foreign_repositories))
            )
        if len(candidate_paths) > policy.max_files:
            return False, (
                f"workspace recovery found {len(candidate_paths)} candidate paths; "
                f"limit is {policy.max_files}"
            )
        if len(current_snapshot.candidate_repositories) > policy.max_repositories:
            return False, (
                "workspace recovery spans "
                f"{len(current_snapshot.candidate_repositories)} repositories; "
                f"limit is {policy.max_repositories}"
            )

        assessments = self.scope_assessments(package, current)
        assessment_by_path = {item.qualified_path: item for item in assessments}
        relationship_by_path = {
            item.qualified_path: item.relationship
            for item in current_snapshot.candidates
        }
        agent_id, selection_policy = self._scope_recovery_agent(package)
        if not agent_id:
            return False, "no write-capable workspace recovery agent is available"

        output_schema = {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "status",
                "summary",
                "retain_paths",
                "discard_paths",
                "removed_paths",
            ],
            "properties": {
                "status": {"enum": ["resolved"]},
                "summary": {"type": "string", "minLength": 1},
                "retain_paths": {
                    "type": "array",
                    "items": {"type": "string", "enum": sorted(candidate_paths)},
                },
                "discard_paths": {
                    "type": "array",
                    "items": {"type": "string", "enum": sorted(candidate_paths)},
                },
                # Backward-compatible output accepted from older recovery skills.
                "removed_paths": {
                    "type": "array",
                    "items": {"type": "string", "enum": sorted(candidate_paths)},
                },
            },
        }
        handoff = StructuredHandoff(
            work_package_id=package.id,
            stage="scope_recovery",
            summary=(
                "Resolve the complete out-of-scope workspace delta for this package. "
                "Inspect every supplied candidate, including paths in undeclared "
                "repositories. Restore or delete accidental/generated changes; repair "
                "legitimate changes as needed; and return retain_paths only for exact "
                "dirty paths that belong to this package. Every remaining candidate "
                "must be listed in retain_paths. Protected planning, dependency-lock, "
                "CI, and security files may be restored but must never be retained "
                "automatically. Do not commit, push, or change branches: the "
                "orchestrator will rerun required checks and create the atomic commits."
            ),
            unresolved_findings=[
                (
                    f"{path}: relationship={relationship_by_path.get(path, 'unknown')}; "
                    f"category={assessment_by_path[path].category}; "
                    f"reason={assessment_by_path[path].reason}"
                )
                for path in sorted(candidate_paths)
            ],
            repository_diff_summary=(
                f"Affected repositories: {package.affected_repositories}; "
                f"declared write_scope: {package.write_scope}; candidates: "
                + ", ".join(sorted(candidate_paths))
            ),
            bounded_excerpts=self._scope_recovery_excerpts(assessments),
            requirements=list(package.requirements),
            acceptance_criteria=[
                {"id": item.id, "description": item.description}
                for item in package.acceptance_criteria
            ],
            expected_output_schema=output_schema,
            working_directory=(
                str(self._host._workspace_root)
                if self._host._workspace_root is not None
                else self._host._package_working_directory(package, write_capable=True)
            ),
            read_only=False,
            workflow_skills=self._host._workflow_skills_for(
                package, AgentCapability.FIX_REVIEW
            ),
        )
        self._host._journal.append(
            "scope_recovery_started",
            {
                "package_id": package.id,
                "agent_id": agent_id,
                "selection_policy": selection_policy,
                "violations": sorted(candidate_paths),
                "candidate_repositories": current_snapshot.candidate_repositories,
                "removed_before_agent": removed_before,
                "policy": policy.as_mapping(),
                "context": dict(context or {}),
            },
        )
        result = self._host._call_prebuilt_handoff(
            AgentCapability.FIX_REVIEW, agent_id, package, handoff
        )
        payload = self._host._agent_payload(result)
        if payload.get("ok") is not True or payload.get("status") != "resolved":
            return False, "workspace recovery agent returned an invalid resolution contract"

        retain_raw = payload.get("retain_paths") or []
        discard_raw = payload.get("discard_paths")
        if discard_raw is None:
            discard_raw = payload.get("removed_paths") or []
        if not isinstance(retain_raw, list):
            return False, "workspace recovery retain_paths must be a list"
        if not isinstance(discard_raw, list):
            return False, "workspace recovery discard_paths must be a list"

        retain_requested = {str(item) for item in retain_raw}
        discard_reported = {str(item) for item in discard_raw}
        unknown = sorted((retain_requested | discard_reported) - candidate_paths)
        if unknown:
            return False, (
                "workspace recovery agent referenced paths outside its candidate set: "
                + ", ".join(unknown)
            )
        overlap = sorted(retain_requested & discard_reported)
        if overlap:
            return False, (
                "workspace recovery agent both retained and discarded: "
                + ", ".join(overlap)
            )

        # The fixer is not required to explicitly retain routine supporting
        # paths that the deterministic scope policy can safely admit.  Run that
        # policy after generated artifacts have been removed and after the
        # agent's edits, so a single large/binary cache file cannot consume the
        # expansion budget for legitimate tests.
        post_agent_scope_violations = self.declared_write_scope_violations(package)
        if post_agent_scope_violations:
            self._auto_expand_declared_write_scope(
                package, post_agent_scope_violations
            )
        after_agent = self.workspace_scope_snapshot(package)
        removed_after = self.cleanup_scope_artifacts(
            package,
            flatten_dirty_paths(self._host._workspace_dirty_paths()),
            affected_only=False,
            source="workspace_recovery_post_agent",
        )
        if removed_after:
            after_agent = self.workspace_scope_snapshot(package)
        remaining_before_approval = set(after_agent.candidate_paths)
        newly_created = sorted(remaining_before_approval - candidate_paths)
        if newly_created:
            return False, (
                "workspace recovery agent created new out-of-scope paths: "
                + ", ".join(newly_created)
            )
        unresolved = sorted(remaining_before_approval - retain_requested)
        if unresolved:
            return False, (
                "workspace recovery agent left candidate paths without retaining them: "
                + ", ".join(unresolved)
            )
        not_discarded = sorted(discard_reported & remaining_before_approval)
        if not_discarded:
            return False, (
                "workspace recovery agent reported discarded paths that are still dirty: "
                + ", ".join(not_discarded)
            )

        retained_dirty = sorted(retain_requested & remaining_before_approval)
        added_paths, added_repositories = self._approve_agent_recovery_paths(
            package, retained_dirty, candidate_paths
        )
        remaining = self.workspace_recovery_candidates(package)

        executed_by = str(result.get("_execraft_executed_by", agent_id))
        package.last_fixer_id = executed_by
        if package.reviewer_id == executed_by:
            package.reviewer_id = ""
        if package.final_reviewer_id == executed_by:
            package.final_reviewer_id = ""

        previous_stage = package.stage
        if package.stage in {
            WorkPackageStage.REVIEW,
            WorkPackageStage.FINAL_REVIEW,
            WorkPackageStage.READY_TO_COMMIT,
        }:
            # A write-capable recovery session invalidates any prior review,
            # even when it only reports discarded candidates: it had access to
            # package-owned files and therefore must be followed by fresh
            # verification and independent review.
            package.review_findings = []
            package.final_reviewer_id = ""
            package.status = "pending"
            self._host._advance_package_stage(package, WorkPackageStage.REGRESSION_VERIFY)

        event_payload = {
            "package_id": package.id,
            "agent_id": executed_by,
            "selection_policy": selection_policy,
            "summary": str(payload.get("summary", "")),
            "added_scope_paths": added_paths,
            "added_repositories": added_repositories,
            "removed_paths": list(dict.fromkeys(removed_before + removed_after)),
            "agent_reported_discard_paths": sorted(discard_reported),
            "previous_stage": previous_stage.value,
            "next_stage": package.stage.value,
            "remaining_violations": remaining,
        }
        self._host.save_state()
        if remaining:
            self._host._journal.append("scope_recovery_incomplete", event_payload)
            self._host._emit_progress("scope_recovery_incomplete", **event_payload)
            return False, (
                "agent-assisted workspace recovery left violations: "
                + ", ".join(remaining)
            )

        self._host._journal.append("scope_recovery_completed", event_payload)
        self._host._emit_progress("scope_recovery_completed", **event_payload)
        return True, "agent-assisted workspace recovery completed"


    def validate_declared_write_scope(self, package: WorkPackage) -> None:
        """Resolve the complete unauthorized delta after a write-capable agent."""

        recovery_detail = ""
        try:
            violations = self.declared_write_scope_violations(package)
            if violations:
                self._auto_expand_declared_write_scope(package, violations)

            candidates = self.workspace_recovery_candidates(package)
            if candidates and self._host.config.scope_recovery_policy.enabled:
                _recovered, recovery_detail = self._attempt_agent_scope_recovery(
                    package
                )
                # Recovery may create small safe supporting paths while repairing
                # the original delta. Admit those through the ordinary bounded
                # policy, then evaluate the complete workspace once more.
                violations = self.declared_write_scope_violations(package)
                if violations:
                    self._auto_expand_declared_write_scope(package, violations)
            candidates = self.workspace_recovery_candidates(package)
        except OrchestrateError as exc:
            self.escalate_scope_failure(package, str(exc))
            raise _StageEscalated(package.id) from exc

        if candidates:
            assessments = self.scope_assessments(package, candidates)
            detail = ", ".join(
                f"{item.qualified_path} [{item.category}]"
                for item in assessments[:20]
            )
            if recovery_detail:
                detail += f"; recovery: {recovery_detail}"
            self.escalate_scope_failure(
                package,
                "workspace contains changes outside the package ownership: " + detail,
            )
            raise _StageEscalated(package.id)


    def prepare_commit_scope(self, package: WorkPackage) -> bool:
        """Recover any unauthorized delta before opening a commit journal.

        Recovery can either remove accidental changes or explicitly acquire
        exact paths (including paths from another configured repository).  When
        ownership expands after review, the recovery transaction rewinds the
        package to regression verification and this method returns ``False`` so
        the current commit attempt stops cleanly.
        """

        if not self._host.config.strict_checks:
            return True
        candidates = self.workspace_recovery_candidates(package)
        if not candidates:
            return True

        detail = ""
        if self._host.config.scope_recovery_policy.enabled:
            try:
                recovered, detail = self._attempt_agent_scope_recovery(
                    package,
                )
            except _AgentWaitRequested:
                raise
            except OrchestrateError as exc:
                recovered, detail = False, str(exc)
            if recovered and not self.workspace_recovery_candidates(package):
                return package.stage == WorkPackageStage.READY_TO_COMMIT

        remaining = self.workspace_recovery_candidates(package)
        message = "workspace contains changes outside the package ownership"
        if remaining:
            message += ": " + ", ".join(remaining[:20])
        if detail:
            message += "; recovery: " + detail
        self.escalate_scope_failure(package, message)
        raise _StageEscalated(package.id)

    def validate_no_out_of_scope_changes(self, package: WorkPackage) -> None:
        if not self._host.config.strict_checks:
            return
        candidates = self.workspace_recovery_candidates(package)
        if candidates:
            self.escalate_scope_failure(
                package,
                "workspace changed after commit-scope validation: "
                + ", ".join(candidates[:20]),
            )
            raise _StageEscalated(package.id)

    def escalate_scope_failure(self, package: WorkPackage, message: str) -> None:
        clean_start = "workspace must be clean before a new package starts" in message
        if self._host.config.scope_recovery_policy.enabled and not clean_start:
            recommended = (
                "resume to invoke automatic workspace recovery; inspect the exact "
                "candidate set only if the bounded recovery budget is exhausted"
            )
        else:
            recommended = (
                "restore the pre-existing workspace or explicitly authorize the "
                "exact package ownership"
            )
        self._host._journal.append(
            "human_intervention_required",
            {
                "package_id": package.id,
                "stage": "scope",
                "blocked_requirement": "workspace ownership or clean-start check failed",
                "evidence": [message],
                "recommended_decision": recommended,
            },
        )
        self._host._emit_progress(
            "human_required",
            package_id=package.id,
            reason=message,
        )
        self._host.transition_to(TaskExecutionState.HUMAN_REQUIRED)


    def supervisor_recovery_incident(self) -> SupervisorIncident | None:
        """Return the incident represented by a terminal Supervisor wait.

        ``SupervisorIncidentStore.active()`` intentionally excludes exhausted
        incidents. Older builds could nevertheless mark an incident exhausted
        or leave it in ``delegating`` while the project state was persisted as
        ``waiting_for_human_decision``. Terminal-state recovery must therefore
        inspect the newest incident as a fallback instead of assuming the two
        stores are perfectly synchronized. Only the newest incident is
        considered, which prevents an old delegation from being resurrected
        after a later, genuine operator question.
        """

        incident = self._host._supervisor_incidents.active()
        if incident is not None:
            return incident
        recent = self._host._supervisor_incidents.recent(1)
        if not recent or recent[0].status == IncidentStatus.RESOLVED:
            return None
        return recent[0]

    @staticmethod
    def _remaining_persisted_delegations(
        incident: SupervisorIncident,
    ) -> list[dict[str, Any]]:
        """Return the valid unconsumed portion of a persisted delegation queue."""

        start = min(
            max(0, incident.pending_delegation_index),
            len(incident.pending_delegations),
        )
        pending: list[dict[str, Any]] = []
        for raw in incident.pending_delegations[start:]:
            try:
                delegation = SupervisorDelegation.from_mapping(raw)
            except (TypeError, ValueError):
                continue
            pending.append(delegation.as_mapping())
        return pending

    def pending_supervisor_delegation_recovery(
        self,
    ) -> tuple[SupervisorIncident, list[dict[str, Any]], str] | None:
        """Describe a safe terminal-state delegation recovery, if one exists.

        There are two compatible durable formats:

        * current builds persist ``pending_delegations`` and a queue cursor;
        * older builds persisted only journal start/finish events.

        Both mean the Supervisor had already decided what work to delegate. A
        provider failure must resume that work on the next healthy provider,
        not turn it into a new product question for the operator.
        """

        if self._host.state != TaskExecutionState.WAITING_FOR_HUMAN_DECISION:
            return None
        if self._host._supervisor_coordinator.supervisor_adapter() is None:
            return None
        incident = self.supervisor_recovery_incident()
        if incident is None or incident.human_answer:
            return None
        try:
            self._host._state_record.plan_graph.package_by_id(incident.package_id)
        except OrchestrateError:
            return None

        pending = self._remaining_persisted_delegations(incident)
        source = "persisted_queue"
        if not pending:
            pending = self._host._lost_supervisor_delegations(incident)
            source = "journal"
        if not pending:
            return None
        return incident, pending, source

    def can_auto_resume_lost_supervisor_delegation(self) -> bool:
        """Return whether a terminal human wait masks delegated work.

        The old predicate required ``status=waiting_for_human`` and rejected a
        non-empty persisted queue. That excluded the exact state produced by a
        failed delegated provider: the project waited for a human while the
        incident still said ``delegating`` and already contained the task to
        retry. Use the durable queue/journal as the authority instead.
        """

        return self.pending_supervisor_delegation_recovery() is not None
