"""Versioned task-definition replanning with deterministic state reconciliation.

The service deliberately separates three responsibilities:

* a read-only agent may *propose* or semantically validate a candidate;
* deterministic impact analysis decides whether durable history can be kept;
* publication occurs under the same driver/orchestrator locks used by execution.

Completed package semantics are immutable. Started packages are either carried
forward unchanged or explicitly superseded by a new package ID after workspace
cleanliness is proven. Pending packages may be freely replaced by the candidate
execution graph.
"""

from __future__ import annotations

import json
import shutil
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

import yaml

from execraft.onboarding.plan_validation import PlanGraphValidationError, validate_plan_graph_mapping
from execraft.persistence import (
    FileLock,
    LockBusyError,
    LockLevel,
    atomic_write_json,
    atomic_write_text,
    atomic_write_yaml,
    sha256_file,
)
from execraft.plan_contract import (
    DeclarativePlanGraphError,
    validate_declarative_plan_graph_mapping,
)
from execraft.onboarding.start_models import ProviderChoice
from execraft.onboarding.task_definition import (
    TaskDefinitionDriftError,
    TaskDefinitionError,
    TaskDefinitionInput,
    TaskDefinitionService,
)
from execraft.orchestrate.checkpoints import StateCheckpointStore
from execraft.orchestrate.directives import WorkPackageDirectiveQueue
from execraft.orchestrate.identity import resolve_storage_identity
from execraft.orchestrate.invocations import AgentInvocationStore
from execraft.orchestrate.journal import EventJournal
from execraft.orchestrate.models import (
    PlanGraph,
    TaskExecutionState,
    TaskExecutionStateRecord,
    WorkPackageStage,
    utc_now,
)
from execraft.orchestrate.normalizer import load_plan_graph_file
from execraft.orchestrate.task_status import write_runtime_status
from execraft.orchestrate.transactions import CommitJournal
from execraft.repository_sync.transaction import RepositorySyncTransactionStore
from execraft.project import ProjectDescriptor
from execraft.workspace.task_git import (
    TASK_STATUSES,
    TaskManifest,
    git_operation,
    working_tree_dirty,
    write_manifest,
)
from execraft.workspace.workspace_git import load_workspace

from .agent import AgentReplanner
from .impact import (
    analyze_impact,
    impact_from_mapping,
    reconcile_state,
    render_impact_report,
)
from .models import (
    ReplanApplyResult,
    ReplanCandidate,
    ReplanConflictError,
    ReplanConsistencyError,
    ReplanError,
    ReplanImpact,
    ReplanTransactionError,
)

_REPLAN_SCHEMA_VERSION = 1
_CANDIDATE_SCHEMA_VERSION = 1
_REPLAN_TRANSACTION_FILE = "replan-transaction.yaml"


@dataclass(frozen=True)
class ReplanInputs:
    """Operator input for one candidate revision."""

    requested_change: str = ""
    definition: TaskDefinitionInput = TaskDefinitionInput()
    from_current_files: bool = False
    package_mapping: Mapping[str, str] | None = None
    allow_structural_consistency: bool = False
    allow_incomplete_definition: bool = False
    preserve_current_brief: bool = False


class ReplanService:
    """Create, inspect, apply, and recover versioned task-definition revisions."""

    def __init__(
        self,
        *,
        control_root: Path,
        state_root: Path,
        project: ProjectDescriptor,
        manifest: TaskManifest,
        dossier: Path,
        agent_replanner: AgentReplanner | None = None,
    ) -> None:
        self.control_root = Path(control_root).expanduser().resolve()
        self.state_root = Path(state_root).expanduser().resolve()
        self.project = project
        self.manifest = manifest
        self.dossier = Path(dossier).expanduser().resolve()
        self.agent_replanner = agent_replanner or AgentReplanner()
        self.definitions = TaskDefinitionService()
        self.identity = resolve_storage_identity(
            self.state_root,
            project_id=project.id,
            task_id=manifest.id,
        )
        self.state_dir = self.identity.state_dir
        self.journal = EventJournal(self.identity.journal_path)
        self.checkpoints = StateCheckpointStore(
            self.state_dir / "orchestration-checkpoints.sqlite3"
        )
        self.commit_journal = CommitJournal(self.state_dir / "commit-journal.json")
        self.repository_sync_transactions = RepositorySyncTransactionStore(self.state_dir)
        self.invocations = AgentInvocationStore(self.state_dir / "agent-invocations.sqlite3")
        self.directives = WorkPackageDirectiveQueue(self.state_dir / "work-package-directives.json")

    @property
    def allowed_repositories(self) -> set[str]:
        return {item.id for item in self.manifest.repositories}

    def completed_package_ids(self) -> frozenset[str]:
        """Return an advisory snapshot of packages completed in runtime state.

        Replanning callers sometimes need completion status to construct a
        declarative graph without consulting legacy ``stage`` fields embedded
        in PLAN.graph.yaml. Candidate application still performs the canonical
        impact analysis under replan locks, so this snapshot cannot bypass definition
        immutability checks if execution changes concurrently.
        """

        state = self._load_state_or_plan()
        return frozenset(
            package.id
            for package in state.plan_graph.work_packages
            if package.stage == WorkPackageStage.COMPLETED
        )

    def create_candidate(
        self,
        inputs: ReplanInputs,
        *,
        provider: ProviderChoice | None = None,
        workdir: Path | None = None,
    ) -> ReplanCandidate:
        """Create a durable candidate without mutating live accepted state."""

        with self._exclusive_replan_locks():
            self._require_no_incomplete_transaction()
            self._require_replan_lifecycle()
            self._require_quiescent_execution(for_application=False)
            baseline_snapshot = self.definitions.ensure_revision_baseline(
                self.dossier,
                adopted_at=utc_now(),
                allow_incomplete=inputs.allow_incomplete_definition,
            )
            integrity = self.definitions.integrity_report(self.dossier)
            if not inputs.from_current_files and not integrity.ok:
                raise TaskDefinitionDriftError(
                    "TASK_DEFINITION_DRIFT: live definition differs from the accepted revision; "
                    "use --from-current-files to stage the manual edits"
                )
            # Revision history is a correctness boundary, not merely an audit
            # convenience.  Never overwrite an accepted definition unless its
            # current historical snapshot still exists.  A manually drifted
            # A revision-1 task created before baseline metadata is the sole exception because
            # the original bytes are no longer recoverable; the accepted hashes
            # remain in DEFINITION.yaml and the new candidate records that fact.
            if (
                integrity.ok
                or integrity.revision > 1
                or baseline_snapshot is not None
            ):
                self.definitions.require_revision_snapshot(
                    self.dossier,
                    max(1, integrity.revision),
                    allow_incomplete=inputs.allow_incomplete_definition,
                )
            history_gap = (
                integrity.revision == 1
                and not integrity.ok
                and baseline_snapshot is None
            )
            state = self._load_state_or_plan(
                allow_missing_graph=inputs.allow_incomplete_definition
            )
            current = self._read_live_documents(
                require_complete=not inputs.allow_incomplete_definition
            )
            current = {
                name: current.get(name, "")
                for name in ("BRIEF.md", "PLAN.md", "PLAN.graph.yaml")
            }
            if inputs.allow_incomplete_definition and not current["BRIEF.md"].strip():
                raise ReplanError("initial plan generation requires a non-empty BRIEF.md")
            supplied = inputs.definition
            if inputs.from_current_files:
                candidate_docs = dict(current)
            else:
                candidate_docs = {
                    "BRIEF.md": supplied.brief_markdown or current["BRIEF.md"],
                    "PLAN.md": supplied.plan_markdown or current["PLAN.md"],
                    "PLAN.graph.yaml": supplied.plan_graph_yaml or current["PLAN.graph.yaml"],
                }

            explicit_documents = (
                set(candidate_docs)
                if inputs.from_current_files
                else set(supplied.documents())
            )
            document_origins = {
                name: (
                    "manual_edit_adopted"
                    if inputs.from_current_files
                    else "operator_supplied"
                    if name in explicit_documents
                    else "carried_forward"
                )
                for name in candidate_docs
            }
            document_sources = dict(supplied.sources)

            requested_change = " ".join(inputs.requested_change.split())
            mapping = self._normalize_mapping(inputs.package_mapping or {})
            consistency_mode = "structural"
            consistency_summary = "Candidate graph passed deterministic structural validation."
            generated_by = "operator"
            provider_id = ""

            plan_changed = candidate_docs["PLAN.md"] != current["PLAN.md"]
            graph_supplied = bool(supplied.plan_graph_yaml) or inputs.from_current_files
            requires_agent = (
                bool(requested_change)
                or plan_changed
                or bool(supplied.brief_markdown)
                or inputs.from_current_files
            )
            if provider is not None and provider.available and requires_agent:
                proposal = self.agent_replanner.propose(
                    provider=provider,
                    workdir=(Path(workdir).resolve() if workdir else self.dossier),
                    project_id=self.project.id,
                    task_id=self.manifest.id,
                    allowed_repositories=tuple(sorted(self.allowed_repositories)),
                    current_brief=current["BRIEF.md"],
                    current_plan=current["PLAN.md"],
                    current_graph_yaml=current["PLAN.graph.yaml"],
                    state_summary=self._state_summary(state),
                    requested_change=requested_change,
                    supplied_brief=(candidate_docs["BRIEF.md"] if supplied.has_brief or inputs.from_current_files else ""),
                    supplied_plan=(candidate_docs["PLAN.md"] if supplied.has_plan or inputs.from_current_files else ""),
                    supplied_graph_yaml=(candidate_docs["PLAN.graph.yaml"] if graph_supplied else ""),
                )
                self._require_agent_respected_explicit_documents(
                    proposal,
                    candidate_docs,
                    explicit_documents,
                )
                if requested_change and not supplied.supplied and not inputs.from_current_files:
                    candidate_docs["BRIEF.md"] = (
                        current["BRIEF.md"]
                        if inputs.preserve_current_brief
                        else proposal.brief_markdown
                    )
                    candidate_docs["PLAN.md"] = proposal.plan_markdown
                    candidate_docs["PLAN.graph.yaml"] = proposal.plan_graph_yaml
                    document_origins = {
                        name: "agent_proposed" for name in candidate_docs
                    }
                    if inputs.preserve_current_brief:
                        document_origins["BRIEF.md"] = "carried_forward"
                else:
                    # Explicit operator documents remain authoritative. The AI
                    # may fill only a graph that the operator did not supply.
                    if not graph_supplied and plan_changed:
                        candidate_docs["PLAN.graph.yaml"] = proposal.plan_graph_yaml
                        document_origins["PLAN.graph.yaml"] = "agent_generated"
                mapping = self._normalize_mapping({**proposal.package_mapping, **mapping})
                consistency_mode = "agent"
                consistency_summary = proposal.consistency_summary or proposal.change_summary
                generated_by = "agent"
                provider_id = proposal.provider_id
            elif requires_agent and not inputs.allow_structural_consistency:
                if plan_changed and not graph_supplied:
                    raise ReplanError(
                        "PLAN.md changed but no PLAN.graph.yaml or planning provider is available; "
                        "supply the graph or select a provider"
                    )
                raise ReplanError(
                    "BRIEF/PLAN changes require ai-replan semantic consistency validation; "
                    "select a planning provider or explicitly use structural-only mode"
                )
            elif requires_agent:
                consistency_summary = (
                    "Operator explicitly accepted structural-only validation; semantic BRIEF/PLAN "
                    "coherence was not checked by ai-replan."
                )

            candidate_docs = self._normalize_candidate_documents(candidate_docs)
            graph = self._validate_candidate_graph(candidate_docs["PLAN.graph.yaml"])
            impact = analyze_impact(
                state,
                graph,
                mapping,
                current_revision=self._current_revision(),
                historical_package_ids=self.definitions.load_metadata(self.dossier).get(
                    "package_ids_seen", []
                ),
            )
            if history_gap:
                impact = replace(
                    impact,
                    warnings=impact.warnings
                    + (
                        "accepted revision 1 predates durable revision snapshots and the live "
                        "definition already drifted; original accepted bytes cannot be recovered, "
                        "so DEFINITION.yaml hashes are the remaining historical anchor",
                    ),
                )
            candidate_id = f"r{integrity.revision + 1:04d}-{uuid.uuid4().hex[:10]}"
            path = self.dossier / "revisions" / f"pending-{candidate_id}"
            self._write_candidate(
                path,
                candidate_id=candidate_id,
                revision=integrity.revision + 1,
                documents=candidate_docs,
                impact=impact,
                consistency_mode=consistency_mode,
                consistency_summary=consistency_summary,
                package_mapping=mapping,
                document_origins=document_origins,
                document_sources=document_sources,
                requested_change=requested_change,
                generated_by=generated_by,
                provider_id=provider_id,
                accepts_live_drift=inputs.from_current_files,
                state=state,
                definition_metadata=self.definitions.load_metadata(self.dossier),
            )
            self.journal.append(
                "task_replan_candidate_created",
                {
                    "candidate_id": candidate_id,
                    "revision": integrity.revision + 1,
                    "applicable": impact.applicable,
                    "consistency_mode": consistency_mode,
                    "package_mapping": dict(mapping),
                },
            )
            return self.load_candidate(candidate_id)

    def load_candidate(self, candidate_id: str) -> ReplanCandidate:
        path = self._candidate_path(candidate_id)
        metadata_path = path / "CANDIDATE.yaml"
        if not metadata_path.is_file():
            raise ReplanError(f"replan candidate not found: {candidate_id}")
        raw = self._read_yaml(metadata_path)
        if type(raw.get("schema_version")) is not int or raw["schema_version"] != _CANDIDATE_SCHEMA_VERSION:
            raise ReplanError(f"unsupported replan candidate schema: {metadata_path}")
        recorded_id = raw.get("candidate_id")
        if not isinstance(recorded_id, str) or recorded_id != candidate_id:
            raise ReplanError(
                f"candidate identity mismatch: requested {candidate_id!r}, metadata has {recorded_id!r}"
            )
        revision_raw = raw.get("revision")
        if type(revision_raw) is not int or revision_raw < 2:
            raise ReplanError(f"candidate revision is invalid: {revision_raw!r}")
        if path.name != f"pending-{recorded_id}":
            raise ReplanError(f"candidate path identity mismatch: {path}")
        accepts_live_drift = raw.get("accepts_live_drift", False)
        if type(accepts_live_drift) is not bool:
            raise ReplanError("candidate accepts_live_drift must be a boolean")
        origins = self._normalize_string_mapping(
            raw.get("document_origins") or {}, label="document_origins"
        )
        expected_documents = {"BRIEF.md", "PLAN.md", "PLAN.graph.yaml"}
        if set(origins) != expected_documents:
            raise ReplanError("candidate document_origins must describe all definition documents")
        allowed_origins = {
            "manual_edit_adopted",
            "operator_supplied",
            "carried_forward",
            "agent_proposed",
            "agent_generated",
        }
        invalid_origins = sorted(set(origins.values()) - allowed_origins)
        if invalid_origins:
            raise ReplanError(
                "candidate document_origins contains unsupported values: "
                + ", ".join(invalid_origins)
            )
        sources = self._normalize_string_mapping(
            raw.get("document_sources") or {}, label="document_sources"
        )
        if set(sources) - expected_documents:
            raise ReplanError("candidate document_sources contains unknown document names")
        mapping_raw = self._normalize_string_mapping(
            raw.get("package_mapping") or {}, label="package_mapping"
        )
        mapping = self._normalize_mapping(mapping_raw)
        self._verify_candidate_integrity(path, raw)
        impact_raw = json.loads((path / "IMPACT.json").read_text(encoding="utf-8"))
        if not isinstance(impact_raw, Mapping):
            raise ReplanError("candidate IMPACT.json must contain an object")
        impact = impact_from_mapping(impact_raw)
        if impact.candidate_revision != revision_raw or impact.current_revision + 1 != revision_raw:
            raise ReplanError(
                "candidate impact revision metadata is inconsistent with CANDIDATE.yaml"
            )
        return ReplanCandidate(
            candidate_id=recorded_id,
            revision=revision_raw,
            path=path,
            brief_markdown=(path / "BRIEF.md").read_text(encoding="utf-8"),
            plan_markdown=(path / "PLAN.md").read_text(encoding="utf-8"),
            plan_graph_yaml=(path / "PLAN.graph.yaml").read_text(encoding="utf-8"),
            impact=impact,
            consistency_mode=str(raw.get("consistency_mode", "")),
            consistency_summary=str(raw.get("consistency_summary", "")),
            package_mapping=mapping,
            document_origins=origins,
            document_sources=sources,
            requested_change=str(raw.get("requested_change", "")),
            generated_by=str(raw.get("generated_by", "operator")),
            provider_id=str(raw.get("provider_id", "")),
            accepts_live_drift=accepts_live_drift,
        )

    def apply_candidate(self, candidate_id: str) -> ReplanApplyResult:
        """Publish one candidate through a crash-recoverable definition transaction."""

        with self._exclusive_replan_locks():
            self._require_no_incomplete_transaction()
            self._require_replan_lifecycle()
            self._require_quiescent_execution(for_application=True)
            candidate = self.load_candidate(candidate_id)
            integrity = self.definitions.integrity_report(self.dossier)
            if not integrity.ok:
                if not candidate.accepts_live_drift or not self._live_matches_candidate(candidate):
                    self.definitions.require_integrity(self.dossier)
            current_metadata = self.definitions.load_metadata(self.dossier)
            current_revision = int(current_metadata.get("revision", 1) or 1)
            if candidate.revision != current_revision + 1:
                raise ReplanConflictError(
                    f"candidate {candidate_id} targets revision {candidate.revision}, but current "
                    f"revision is {current_revision}; regenerate the candidate"
                )
            state = self._load_state_or_plan(allow_missing_graph=True)
            graph = self._validate_candidate_graph(candidate.plan_graph_yaml)
            impact = analyze_impact(
                state,
                graph,
                candidate.package_mapping,
                current_revision=self._current_revision(),
                historical_package_ids=current_metadata.get("package_ids_seen", []),
            )
            if not impact.applicable:
                raise ReplanConflictError(
                    "replan candidate is blocked: " + "; ".join(impact.blockers)
                )
            self._require_active_supersession_safe(impact)
            migrated = reconcile_state(
                state,
                graph,
                impact,
                candidate.package_mapping,
            )
            previous_definition_sha = self.definitions.definition_sha256(self.dossier)
            superseded = tuple(
                item.package_id
                for item in impact.packages
                if item.classification == "active_superseded"
            )
            transaction = self._begin_transaction(
                candidate,
                state,
                current_metadata,
                superseded_active_packages=superseded,
            )
            committed = False
            try:
                self._publish_documents(candidate)
                self._write_definition_metadata(
                    candidate,
                    current_metadata,
                    previous_state=state,
                    migrated_state=migrated,
                )
                state_path = self._write_state_projection(migrated)
                invalidated = self._invalidate_context_capsules()
                self._supersede_invalid_directives(
                    {package.id for package in migrated.plan_graph.work_packages}
                )
                revision_path = self._finalize_revision(candidate, transaction)
                transaction = self._mark_transaction_committed(
                    transaction,
                    invalidated_context_capsules=invalidated,
                    state_path=state_path,
                )
                committed = True
                self._complete_committed_transaction(transaction, state=migrated)
            except Exception as exc:
                if committed or self._transaction_status() == "committed":
                    raise ReplanTransactionError(
                        "task definition revision was durably committed but post-commit "
                        "bookkeeping did not finish; run 'execraft task replan "
                        f"{self.manifest.id} --recover': {exc}"
                    ) from exc
                try:
                    self._rollback_transaction(transaction)
                except Exception as rollback_exc:
                    raise ReplanTransactionError(
                        "replan publication failed and automatic rollback was incomplete; "
                        f"run 'execraft task replan {self.manifest.id} --recover': {rollback_exc}"
                    ) from exc
                raise ReplanTransactionError(
                    f"replan publication failed and was rolled back: {exc}"
                ) from exc

            definition_sha = self.definitions.definition_sha256(self.dossier)
            return ReplanApplyResult(
                candidate_id=candidate.candidate_id,
                revision=candidate.revision,
                revision_path=revision_path,
                previous_definition_sha256=previous_definition_sha,
                definition_sha256=definition_sha,
                state_path=state_path,
                invalidated_capsules=invalidated,
                superseded_active_packages=superseded,
                previous_task_status=str(transaction.get("manifest_status_before", "")),
                task_status=self.manifest.status,
            )

    def recover_incomplete(self) -> bool:
        """Recover a crash-interrupted replan by rollback or forward completion."""

        with self._exclusive_replan_locks():
            marker = self.state_dir / _REPLAN_TRANSACTION_FILE
            if not marker.is_file():
                return False
            transaction = self._read_transaction_marker(marker)
            candidate_id = str(transaction.get("candidate_id", ""))
            if str(transaction.get("status", "")) == "committed":
                self._require_committed_transaction_integrity(transaction)
                state = self._load_state_or_plan()
                self._complete_committed_transaction(transaction, state=state)
                action = "complete_commit"
            else:
                self._rollback_transaction(transaction)
                action = "rollback"
            self.journal.append(
                "task_replan_recovered",
                {"candidate_id": candidate_id, "action": action},
            )
            return True

    def _require_replan_lifecycle(self) -> None:
        unsafe = {"closed", "merged", "abandoned", "integrating"}
        if self.manifest.status in unsafe:
            raise ReplanConflictError(
                f"cannot replan task from lifecycle status {self.manifest.status!r}"
            )

    def _replan_manifest_target_status(self) -> str:
        if self.manifest.status in {"review", "approved", "blocked"}:
            return "in_progress"
        return self.manifest.status

    def _complete_manifest_transition(self, transaction: Mapping[str, Any]) -> None:
        before = str(transaction.get("manifest_status_before", ""))
        target = str(transaction.get("manifest_status_after", ""))
        if not before or not target:
            raise ReplanTransactionError("replan transaction is missing task lifecycle metadata")
        if self.manifest.status not in {before, target}:
            raise ReplanTransactionError(
                "task lifecycle changed while replan publication was incomplete: "
                f"expected {before!r} or {target!r}, found {self.manifest.status!r}"
            )
        self.manifest.status = target
        try:
            write_manifest(self.control_root, self.manifest)
        except Exception as exc:
            raise ReplanTransactionError(
                f"cannot publish replanned task lifecycle status {target!r}: {exc}"
            ) from exc

    def _require_quiescent_execution(self, *, for_application: bool) -> None:
        """Require a provider/transaction quiescence boundary for replanning.

        Acquiring the orchestrator locks prevents *new* scheduling, while the
        durable invocation ledger catches provider work that survived a driver
        interruption. Applying a revision additionally requires that no commit
        transaction is in flight, regardless of which package is being edited.
        """

        running = self.invocations.list_running(limit=50)
        if running:
            owners = ", ".join(
                f"{item.package_id}:{item.stage}:{item.invocation_id[:10]}"
                for item in running[:10]
            )
            suffix = "" if len(running) <= 10 else f" (+{len(running) - 10} more)"
            raise ReplanConflictError(
                "cannot replan while provider invocations are still running; "
                "stop/cancel the active work or recover interrupted invocations first: "
                f"{owners}{suffix}"
            )
        if not for_application:
            return
        pending = self.commit_journal.pending_transactions()
        if pending:
            ids = ", ".join(item.transaction_id for item in pending)
            raise ReplanConflictError(
                "cannot apply a task replan while commit transactions are pending: " + ids
            )
        sync_pending = self.repository_sync_transactions.pending_transactions()
        if sync_pending:
            ids = ", ".join(item.transaction_id for item in sync_pending)
            raise ReplanConflictError(
                "cannot apply a task replan while repository-sync transactions are pending: " + ids
            )

    def _require_active_supersession_safe(self, impact: ReplanImpact) -> None:
        if not impact.requires_clean_workspace:
            return
        try:
            workspace = load_workspace(self.control_root, self.manifest.id)
        except Exception as exc:
            raise ReplanConflictError(
                "cannot supersede started packages without a registered workspace whose cleanliness can be proven"
            ) from exc
        dirty: list[str] = []
        operations: list[str] = []
        for item in workspace.repositories:
            if str(item.get("mutability", "task_owned")) != "task_owned":
                continue
            path = Path(str(item.get("worktree_path", ""))).expanduser().resolve()
            if not path.is_dir():
                raise ReplanConflictError(
                    f"cannot prove workspace safety; missing worktree for {item.get('id')}: {path}"
                )
            operation = git_operation(path)
            if operation:
                operations.append(f"{item.get('id')}:{operation}")
            if working_tree_dirty(path):
                dirty.append(str(item.get("id", "")))
        if operations:
            raise ReplanConflictError(
                "cannot supersede started packages during Git operations: " + ", ".join(operations)
            )
        if dirty:
            raise ReplanConflictError(
                "cannot supersede started packages with dirty task worktrees: " + ", ".join(sorted(dirty))
            )

    def _validate_candidate_graph(self, graph_yaml: str) -> PlanGraph:
        try:
            raw = yaml.safe_load(graph_yaml) or {}
        except yaml.YAMLError as exc:
            raise ReplanError(f"candidate PLAN.graph.yaml is invalid YAML: {exc}") from exc
        if not isinstance(raw, Mapping):
            raise ReplanError("candidate PLAN.graph.yaml must contain a mapping")
        self._validate_declarative_graph(raw)
        try:
            return validate_plan_graph_mapping(raw, allowed_repositories=self.allowed_repositories)
        except PlanGraphValidationError as exc:
            raise ReplanError(f"invalid candidate PLAN.graph.yaml: {exc}") from exc

    @staticmethod
    def _validate_declarative_graph(raw: Mapping[str, Any]) -> None:
        """Forbid runtime/evidence state from entering an accepted revision."""

        try:
            validate_declarative_plan_graph_mapping(raw)
        except DeclarativePlanGraphError as exc:
            raise ReplanError(str(exc)) from exc


    def _read_live_documents(self, *, require_complete: bool = True) -> dict[str, str]:
        try:
            return self.definitions.live_documents(
                self.dossier, require_complete=require_complete
            )
        except TaskDefinitionError as exc:
            raise ReplanError(str(exc)) from exc

    def _load_state_or_plan(
        self, *, allow_missing_graph: bool = False
    ) -> TaskExecutionStateRecord:
        path = self.state_dir / "state.json"
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ReplanError(f"cannot read orchestration state {path}: {exc}") from exc
            if not isinstance(raw, Mapping):
                raise ReplanError(f"orchestration state must be an object: {path}")
            return TaskExecutionStateRecord.from_mapping(dict(raw))
        graph_path = self.dossier / "PLAN.graph.yaml"
        if allow_missing_graph and not graph_path.is_file():
            return TaskExecutionStateRecord(
                project_id=self.project.id,
                state=TaskExecutionState.INITIALIZING,
                plan_graph=PlanGraph(work_packages=[]),
            )
        graph, report = load_plan_graph_file(graph_path)
        if report.has_errors():
            raise ReplanError("current PLAN.graph.yaml is invalid: " + report.summary())
        return TaskExecutionStateRecord(
            project_id=self.project.id,
            state=TaskExecutionState.INITIALIZING,
            plan_graph=graph,
            total_packages=len(graph.work_packages),
        )

    def _current_revision(self) -> int:
        metadata = self.definitions.load_metadata(self.dossier)
        return max(1, int(metadata.get("revision", 1) or 1)) if metadata else 1

    def _write_candidate(
        self,
        path: Path,
        *,
        candidate_id: str,
        revision: int,
        documents: Mapping[str, str],
        impact: ReplanImpact,
        consistency_mode: str,
        consistency_summary: str,
        package_mapping: Mapping[str, str],
        document_origins: Mapping[str, str],
        document_sources: Mapping[str, str],
        requested_change: str,
        generated_by: str,
        provider_id: str,
        accepts_live_drift: bool,
        state: TaskExecutionStateRecord,
        definition_metadata: Mapping[str, Any],
    ) -> None:
        if path.exists():
            raise ReplanError(f"candidate path already exists: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.parent / f".{path.name}.tmp-{uuid.uuid4().hex}"
        if temporary.exists():
            raise ReplanError(f"candidate staging path already exists: {temporary}")
        temporary.mkdir(parents=False, exist_ok=False)
        try:
            for name, content in documents.items():
                atomic_write_text(temporary / name, content)
            atomic_write_json(temporary / "IMPACT.json", impact.as_mapping(), trailing_newline=True)
            atomic_write_json(temporary / "STATE.before.json", state.as_mapping(), trailing_newline=True)
            atomic_write_yaml(temporary / "DEFINITION.before.yaml", definition_metadata)
            atomic_write_yaml(temporary / "TASK.before.yaml", self.manifest.as_mapping())
            atomic_write_text(
                temporary / "REPLAN_REPORT.md",
                render_impact_report(impact, consistency_mode, consistency_summary),
            )
            document_names = ("BRIEF.md", "PLAN.md", "PLAN.graph.yaml")
            artifact_names = (
                "IMPACT.json",
                "STATE.before.json",
                "DEFINITION.before.yaml",
                "TASK.before.yaml",
                "REPLAN_REPORT.md",
            )
            metadata = {
                "schema_version": _CANDIDATE_SCHEMA_VERSION,
                "candidate_id": candidate_id,
                "revision": revision,
                "created_at": utc_now(),
                "generated_by": generated_by,
                "provider_id": provider_id,
                "accepts_live_drift": bool(accepts_live_drift),
                "requested_change": requested_change,
                "consistency_mode": consistency_mode,
                "consistency_summary": consistency_summary,
                "package_mapping": dict(package_mapping),
                "document_origins": dict(document_origins),
                "document_sources": dict(document_sources),
                "documents": {
                    name: sha256_file(temporary / name) for name in document_names
                },
                "artifacts": {
                    name: sha256_file(temporary / name) for name in artifact_names
                },
            }
            atomic_write_yaml(temporary / "CANDIDATE.yaml", metadata)
            temporary.replace(path)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    @staticmethod
    def _normalize_candidate_documents(documents: Mapping[str, str]) -> dict[str, str]:
        """Apply the same bounded UTF-8/text contract used by initial task imports."""

        validated = TaskDefinitionInput.from_contents(
            brief_markdown=str(documents.get("BRIEF.md", "")),
            plan_markdown=str(documents.get("PLAN.md", "")),
            plan_graph_yaml=str(documents.get("PLAN.graph.yaml", "")),
        )
        if not validated.has_brief or not validated.has_plan or not validated.has_plan_graph:
            raise ReplanError("candidate revision must contain BRIEF.md, PLAN.md, and PLAN.graph.yaml")
        return {
            "BRIEF.md": validated.brief_markdown,
            "PLAN.md": validated.plan_markdown,
            "PLAN.graph.yaml": validated.plan_graph_yaml,
        }

    @staticmethod
    def _verify_candidate_integrity(path: Path, metadata: Mapping[str, Any]) -> None:
        """Reject edited candidate files after impact analysis was recorded."""

        expected_files = {
            "BRIEF.md",
            "PLAN.md",
            "PLAN.graph.yaml",
            "IMPACT.json",
            "STATE.before.json",
            "DEFINITION.before.yaml",
            "TASK.before.yaml",
            "REPLAN_REPORT.md",
            "CANDIDATE.yaml",
        }
        actual_files = {item.name for item in path.iterdir()}
        if actual_files != expected_files:
            unexpected = sorted(actual_files - expected_files)
            missing = sorted(expected_files - actual_files)
            details = []
            if missing:
                details.append("missing=" + ",".join(missing))
            if unexpected:
                details.append("unexpected=" + ",".join(unexpected))
            raise ReplanError(
                "candidate directory contents changed after staging ("
                + "; ".join(details)
                + ")"
            )

        inventories = {
            "documents": {"BRIEF.md", "PLAN.md", "PLAN.graph.yaml"},
            "artifacts": {
                "IMPACT.json",
                "STATE.before.json",
                "DEFINITION.before.yaml",
                "TASK.before.yaml",
                "REPLAN_REPORT.md",
            },
        }
        for inventory_name, required in inventories.items():
            expected = metadata.get(inventory_name) or {}
            if not isinstance(expected, Mapping):
                raise ReplanError(
                    f"candidate {inventory_name} inventory is invalid: {path / 'CANDIDATE.yaml'}"
                )
            if set(str(name) for name in expected) != required:
                raise ReplanError(
                    f"candidate {inventory_name} inventory is incomplete or unexpected"
                )
            for name in sorted(required):
                artifact = path / name
                if not artifact.is_file() or artifact.is_symlink():
                    raise ReplanError(f"candidate artifact is missing or unsafe: {artifact}")
                actual = sha256_file(artifact)
                recorded = str(expected.get(name, ""))
                if actual != recorded:
                    raise ReplanError(
                        f"candidate {name} changed after staging; regenerate the candidate before applying"
                    )

    def _live_matches_candidate(self, candidate: ReplanCandidate) -> bool:
        try:
            live = self._read_live_documents()
        except ReplanError:
            return False
        return (
            live["BRIEF.md"] == candidate.brief_markdown
            and live["PLAN.md"] == candidate.plan_markdown
            and live["PLAN.graph.yaml"] == candidate.plan_graph_yaml
        )

    def _publish_documents(self, candidate: ReplanCandidate) -> None:
        # The executable graph is the commit marker for a coherent definition:
        # publish prose first and graph last.
        atomic_write_text(self.dossier / "BRIEF.md", candidate.brief_markdown)
        atomic_write_text(self.dossier / "PLAN.md", candidate.plan_markdown)
        atomic_write_text(self.dossier / "PLAN.graph.yaml", candidate.plan_graph_yaml)

    def _write_definition_metadata(
        self,
        candidate: ReplanCandidate,
        previous: Mapping[str, Any],
        *,
        previous_state: TaskExecutionStateRecord,
        migrated_state: TaskExecutionStateRecord,
    ) -> None:
        metadata = dict(previous)
        history = list(metadata.get("history") or [])
        history.append(
            {
                "revision": int(previous.get("revision", 1) or 1),
                "definition_sha256": str(previous.get("definition_sha256", "")),
                "superseded_at": utc_now(),
                "superseded_by": candidate.revision,
                "revision_path": str(
                    previous.get(
                        "revision_path",
                        f"revisions/revision-{int(previous.get('revision', 1) or 1):04d}",
                    )
                ),
            }
        )
        metadata.update(
            {
                "schema_version": max(1, int(metadata.get("schema_version", 1) or 1)),
                "revision": candidate.revision,
                "updated_at": utc_now(),
                "current": self.definitions.current_hashes(self.dossier),
                "definition_sha256": self.definitions.definition_sha256(self.dossier),
                "history": history[-100:],
                "revision_path": f"revisions/revision-{candidate.revision:04d}",
                "package_ids_seen": sorted(
                    {
                        str(item).strip()
                        for item in (previous.get("package_ids_seen") or [])
                        if str(item).strip()
                    }
                    | {item.id for item in previous_state.plan_graph.work_packages}
                    | {item.id for item in migrated_state.plan_graph.work_packages}
                ),
                "replan": {
                    "candidate_id": candidate.candidate_id,
                    "consistency_mode": candidate.consistency_mode,
                    "consistency_summary": candidate.consistency_summary,
                    "generated_by": candidate.generated_by,
                    "provider_id": candidate.provider_id,
                    "package_mapping": dict(candidate.package_mapping),
                },
            }
        )
        previous_sources = dict(metadata.get("sources") or {})
        current_hashes = self.definitions.current_hashes(self.dossier)
        sources: dict[str, dict[str, Any]] = {}
        previous_revision = int(previous.get("revision", 1) or 1)
        for name in ("BRIEF.md", "PLAN.md", "PLAN.graph.yaml"):
            origin = str(candidate.document_origins.get(name, "replanned"))
            if origin == "carried_forward":
                entry = dict(previous_sources.get(name) or {})
                entry.update(
                    {
                        "sha256": current_hashes[name],
                        "carried_forward_from_revision": previous_revision,
                    }
                )
            else:
                entry = {
                    "origin": origin,
                    "revision": candidate.revision,
                    "sha256": current_hashes[name],
                    "candidate_id": candidate.candidate_id,
                }
                source = str(candidate.document_sources.get(name, "")).strip()
                if source:
                    entry["source"] = source
                if origin in {"agent_proposed", "agent_generated"}:
                    entry["generated_by"] = "ai-replan"
                    entry["provider_id"] = candidate.provider_id
            sources[name] = entry
        metadata["sources"] = sources
        atomic_write_yaml(self.dossier / "DEFINITION.yaml", metadata)

    def _write_state_projection(self, state: TaskExecutionStateRecord) -> Path | None:
        """Publish only the JSON projection before the transaction commit point.

        Checkpoints and journal events are deliberately post-commit so rollback
        cannot leave a newer recoverable checkpoint behind an older restored
        ``state.json``.
        """

        path = self.state_dir / "state.json"
        if not path.exists() and state.state == TaskExecutionState.INITIALIZING:
            return None
        atomic_write_json(path, state.as_mapping(), trailing_newline=True)
        return path

    def _complete_committed_transaction(
        self,
        transaction: Mapping[str, Any],
        *,
        state: TaskExecutionStateRecord,
    ) -> None:
        """Idempotently finish bookkeeping after the durable commit point."""

        candidate_id = str(transaction.get("candidate_id", ""))
        revision = int(transaction.get("revision", 0))
        state_path_text = str(transaction.get("state_path", "")).strip()
        state_path = Path(state_path_text).expanduser().resolve() if state_path_text else None
        if state_path is not None:
            expected_state = (self.state_dir / "state.json").resolve()
            if state_path != expected_state:
                raise ReplanTransactionError(
                    f"transaction state path mismatch: expected {expected_state}, found {state_path}"
                )
            event = self._ensure_journal_event(
                "task_replan_state_migrated",
                candidate_id,
                {
                    "candidate_id": candidate_id,
                    "revision": revision,
                    "packages": len(state.plan_graph.work_packages),
                    "completed_packages": state.completed_packages,
                },
            )
            self.checkpoints.save(
                state.as_mapping(),
                journal_sequence=event.sequence,
                reason="task_replan",
            )

        self._complete_manifest_transition(transaction)
        definition_sha = self.definitions.definition_sha256(self.dossier)
        self._ensure_journal_event(
            "task_replan_applied",
            candidate_id,
            {
                "candidate_id": candidate_id,
                "revision": revision,
                "previous_definition_sha256": str(
                    transaction.get("definition_sha256", "")
                ),
                "definition_sha256": definition_sha,
                "superseded_active_packages": list(
                    transaction.get("superseded_active_packages") or []
                ),
                "invalidated_context_capsules": int(
                    transaction.get("invalidated_context_capsules", 0) or 0
                ),
                "task_status_before": str(transaction.get("manifest_status_before", "")),
                "task_status_after": str(transaction.get("manifest_status_after", "")),
            },
        )
        write_runtime_status(
            self.dossier,
            state,
            task_id=self.manifest.id,
            project_id=self.project.id,
            state_root=self.state_root,
        )
        self._cleanup_transaction(transaction)

    def _ensure_journal_event(
        self,
        event_type: str,
        candidate_id: str,
        payload: Mapping[str, Any],
    ):
        for entry in self.journal.read():
            if entry.event_type != event_type:
                continue
            if str(entry.payload.get("candidate_id", "")) == candidate_id:
                return entry
        return self.journal.append(event_type, dict(payload))

    def _invalidate_context_capsules(self) -> int:
        directory = self.state_dir / "context-capsules"
        if not directory.is_dir():
            return 0
        count = 0
        for path in directory.glob("*.json"):
            path.unlink(missing_ok=True)
            count += 1
        return count

    def _supersede_invalid_directives(self, valid_package_ids: set[str]) -> None:
        pending = self.directives.pending()
        stale = {item.id for item in pending if item.package_id not in valid_package_ids}
        if stale:
            self.directives.resolve(
                stale,
                status="rejected",
                result="package removed or superseded by accepted task replan",
            )

    def _begin_transaction(
        self,
        candidate: ReplanCandidate,
        state: TaskExecutionStateRecord,
        definition_metadata: Mapping[str, Any],
        *,
        superseded_active_packages: Sequence[str],
    ) -> dict[str, Any]:
        del state  # state.json is snapshotted from disk to preserve exact bytes.
        marker = self.state_dir / _REPLAN_TRANSACTION_FILE
        backup_root = self._backup_root()
        backup = backup_root / candidate.candidate_id
        if backup.exists():
            self._safe_remove_directory(backup, backup_root, label="replan backup")
        backup.mkdir(parents=False, exist_ok=False)
        try:
            for name in ("BRIEF.md", "PLAN.md", "PLAN.graph.yaml", "DEFINITION.yaml"):
                source = self.dossier / name
                if source.is_file():
                    shutil.copy2(source, backup / name)
            state_path = self.state_dir / "state.json"
            if state_path.is_file():
                shutil.copy2(state_path, backup / "state.json")
            directives_path = self.directives.path
            if directives_path.is_file():
                shutil.copy2(directives_path, backup / "work-package-directives.json")
            backup_files = {
                item.name: sha256_file(item)
                for item in sorted(backup.iterdir(), key=lambda value: value.name)
                if item.is_file() and not item.is_symlink()
            }
            if len(backup_files) != len(list(backup.iterdir())):
                raise ReplanTransactionError(
                    f"replan backup contains unsafe non-file entries: {backup}"
                )
            payload = {
                "schema_version": _REPLAN_SCHEMA_VERSION,
                "candidate_id": candidate.candidate_id,
                "revision": candidate.revision,
                "status": "prepared",
                "started_at": utc_now(),
                "backup_path": str(backup),
                "candidate_path": str(candidate.path),
                "revision_path": str(
                    self._revision_root() / f"revision-{candidate.revision:04d}"
                ),
                "had_state": state_path.is_file(),
                "had_directives": directives_path.is_file(),
                "backup_files": backup_files,
                "manifest_status_before": self.manifest.status,
                "manifest_status_after": self._replan_manifest_target_status(),
                "definition_sha256": str(definition_metadata.get("definition_sha256", "")),
                "superseded_active_packages": list(superseded_active_packages),
                "invalidated_context_capsules": 0,
                "state_path": "",
            }
            atomic_write_yaml(marker, payload)
            return payload
        except Exception:
            if backup.is_dir() and not backup.is_symlink():
                shutil.rmtree(backup, ignore_errors=True)
            marker.unlink(missing_ok=True)
            raise

    def _mark_transaction_committed(
        self,
        transaction: Mapping[str, Any],
        *,
        invalidated_context_capsules: int,
        state_path: Path | None,
    ) -> dict[str, Any]:
        """Persist the point after which recovery must complete forward."""

        payload = dict(transaction)
        payload.update(
            {
                "status": "committed",
                "committed_at": utc_now(),
                "invalidated_context_capsules": int(invalidated_context_capsules),
                "state_path": str(state_path.resolve()) if state_path else "",
            }
        )
        revision_path = self._transaction_revision_path(payload)
        applied_path = revision_path / "APPLIED.yaml"
        applied = self._read_yaml(applied_path)
        transaction_info = dict(applied.get("transaction") or {})
        transaction_info.update(
            {
                "status": "committed",
                "committed_at": payload["committed_at"],
                "definition_sha256_before": str(payload.get("definition_sha256", "")),
                "definition_sha256_after": self.definitions.definition_sha256(self.dossier),
            }
        )
        applied["transaction"] = transaction_info
        atomic_write_yaml(applied_path, applied)
        atomic_write_yaml(self.state_dir / _REPLAN_TRANSACTION_FILE, payload)
        return payload

    def _read_transaction_marker(self, marker: Path) -> dict[str, Any]:
        raw = self._read_yaml(marker)
        schema = raw.get("schema_version")
        if type(schema) is not int or schema != _REPLAN_SCHEMA_VERSION:
            raise ReplanTransactionError(
                f"unsupported replan transaction schema in {marker}: {schema!r}"
            )
        status = raw.get("status")
        if status not in {"prepared", "committed"}:
            raise ReplanTransactionError(
                f"invalid replan transaction status in {marker}: {status!r}"
            )
        candidate_id = raw.get("candidate_id")
        revision = raw.get("revision")
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            raise ReplanTransactionError(f"replan transaction candidate ID is invalid: {marker}")
        if type(revision) is not int or revision < 2:
            raise ReplanTransactionError(f"replan transaction revision is invalid: {marker}")
        before = raw.get("manifest_status_before")
        after = raw.get("manifest_status_after")
        if before not in TASK_STATUSES or after not in TASK_STATUSES:
            raise ReplanTransactionError(
                f"replan transaction task lifecycle metadata is invalid: {marker}"
            )
        if before in {"closed", "merged", "abandoned", "integrating"}:
            raise ReplanTransactionError(
                f"replan transaction cannot originate from lifecycle status {before!r}"
            )
        expected_after = "in_progress" if before in {"review", "approved", "blocked"} else before
        if after != expected_after:
            raise ReplanTransactionError(
                "replan transaction contains an invalid lifecycle transition: "
                f"{before!r} -> {after!r}; expected {expected_after!r}"
            )
        return raw

    def _verify_transaction_backup(
        self,
        backup: Path,
        transaction: Mapping[str, Any],
    ) -> None:
        expected = transaction.get("backup_files") or {}
        if not isinstance(expected, Mapping) or not expected:
            raise ReplanTransactionError("replan transaction backup inventory is missing")
        actual_names = {item.name for item in backup.iterdir()}
        expected_names = {str(name) for name in expected}
        if actual_names != expected_names:
            raise ReplanTransactionError(
                "replan transaction backup contents changed; refusing unsafe rollback"
            )
        for name in sorted(expected_names):
            artifact = backup / name
            if not artifact.is_file() or artifact.is_symlink():
                raise ReplanTransactionError(
                    f"replan transaction backup artifact is unsafe: {artifact}"
                )
            if sha256_file(artifact) != str(expected.get(name, "")):
                raise ReplanTransactionError(
                    f"replan transaction backup artifact changed: {artifact}"
                )

    def _require_committed_transaction_integrity(
        self, transaction: Mapping[str, Any]
    ) -> None:
        revision = int(transaction.get("revision", 0) or 0)
        metadata = self.definitions.load_metadata(self.dossier)
        if int(metadata.get("revision", 0) or 0) != revision:
            raise ReplanTransactionError(
                "committed replan transaction does not match live DEFINITION.yaml revision"
            )
        try:
            self.definitions.require_integrity(self.dossier)
            self.definitions.require_revision_snapshot(self.dossier, revision)
        except TaskDefinitionError as exc:
            raise ReplanTransactionError(
                f"committed replan transaction integrity check failed: {exc}"
            ) from exc

    def _transaction_status(self) -> str:
        marker = self.state_dir / _REPLAN_TRANSACTION_FILE
        if not marker.is_file():
            return ""
        try:
            return str(self._read_transaction_marker(marker).get("status", ""))
        except ReplanError:
            return ""

    def _cleanup_transaction(self, transaction: Mapping[str, Any]) -> None:
        backup = self._transaction_backup_path(transaction)
        backup_root = self._backup_root()
        if backup.is_dir():
            self._safe_remove_directory(backup, backup_root, label="replan backup")
        (self.state_dir / _REPLAN_TRANSACTION_FILE).unlink(missing_ok=True)

    def _rollback_transaction(self, transaction: Mapping[str, Any]) -> None:
        marker = self.state_dir / _REPLAN_TRANSACTION_FILE
        backup = self._transaction_backup_path(transaction)
        if not backup.is_dir() or backup.is_symlink():
            raise ReplanTransactionError(
                f"cannot recover replan transaction; backup is missing or unsafe: {backup}"
            )
        self._verify_transaction_backup(backup, transaction)
        candidate_path = self._transaction_candidate_path(transaction)
        revision_path = self._transaction_revision_path(transaction)
        if revision_path.is_dir():
            if candidate_path.exists():
                raise ReplanTransactionError(
                    "cannot recover replan transaction because both candidate and revision "
                    f"paths exist: {candidate_path}, {revision_path}"
                )
            revision_path.replace(candidate_path)
            (candidate_path / "APPLIED.yaml").unlink(missing_ok=True)
            (candidate_path / "DEFINITION.accepted.yaml").unlink(missing_ok=True)
        for name in ("BRIEF.md", "PLAN.md", "PLAN.graph.yaml", "DEFINITION.yaml"):
            source = backup / name
            if source.is_file():
                shutil.copy2(source, self.dossier / name)
            else:
                (self.dossier / name).unlink(missing_ok=True)
        state_path = self.state_dir / "state.json"
        backup_state = backup / "state.json"
        if backup_state.is_file():
            shutil.copy2(backup_state, state_path)
        elif not bool(transaction.get("had_state", False)):
            state_path.unlink(missing_ok=True)
        directives_path = self.directives.path
        backup_directives = backup / "work-package-directives.json"
        if backup_directives.is_file():
            shutil.copy2(backup_directives, directives_path)
        elif not bool(transaction.get("had_directives", False)):
            directives_path.unlink(missing_ok=True)
        self._safe_remove_directory(
            backup,
            self._backup_root(),
            label="replan backup",
        )
        marker.unlink(missing_ok=True)

    def _finalize_revision(
        self,
        candidate: ReplanCandidate,
        transaction: Mapping[str, Any],
    ) -> Path:
        final = self._transaction_revision_path(transaction)
        expected = (self._revision_root() / f"revision-{candidate.revision:04d}").resolve()
        if final != expected:
            raise ReplanTransactionError(
                f"transaction revision path mismatch: expected {expected}, found {final}"
            )
        if final.exists():
            raise ReplanTransactionError(f"revision path already exists: {final}")
        candidate.path.replace(final)
        accepted_definition = final / "DEFINITION.accepted.yaml"
        shutil.copy2(self.dossier / "DEFINITION.yaml", accepted_definition)
        atomic_write_yaml(
            final / "APPLIED.yaml",
            {
                "schema_version": 1,
                "candidate_id": candidate.candidate_id,
                "revision": candidate.revision,
                "applied_at": utc_now(),
                "accepted_definition_metadata_sha256": sha256_file(accepted_definition),
                "transaction": {
                    "status": "pending_commit",
                    "definition_sha256_before": str(
                        transaction.get("definition_sha256", "")
                    ),
                    "definition_sha256_after": self.definitions.definition_sha256(
                        self.dossier
                    ),
                },
            },
        )
        return final

    def _backup_root(self) -> Path:
        root = self.state_dir / "replan-backups"
        if root.is_symlink():
            raise ReplanTransactionError(
                f"replan backup directory cannot be a symbolic link: {root}"
            )
        root.mkdir(parents=True, exist_ok=True)
        resolved = root.resolve()
        if resolved.parent != self.state_dir.resolve():
            raise ReplanTransactionError(
                f"replan backup directory escapes task state: {root}"
            )
        return resolved

    def _transaction_backup_path(self, transaction: Mapping[str, Any]) -> Path:
        raw = str(transaction.get("backup_path", "")).strip()
        if not raw:
            raise ReplanTransactionError("replan backup path is missing")
        path = Path(raw).expanduser().resolve()
        root = self._backup_root()
        self._require_direct_child(path, root, label="replan backup")
        expected = str(transaction.get("candidate_id", "")).strip()
        if not expected or path.name != expected:
            raise ReplanTransactionError(
                f"replan backup identity mismatch: expected {expected!r}, found {path.name!r}"
            )
        return path

    def _transaction_candidate_path(self, transaction: Mapping[str, Any]) -> Path:
        raw = str(transaction.get("candidate_path", "")).strip()
        if not raw:
            raise ReplanTransactionError("replan candidate path is missing")
        path = Path(raw).expanduser().resolve()
        root = self._revision_root()
        self._require_direct_child(path, root, label="replan candidate")
        expected = f"pending-{str(transaction.get('candidate_id', '')).strip()}"
        if path.name != expected:
            raise ReplanTransactionError(
                f"replan candidate identity mismatch: expected {expected!r}, found {path.name!r}"
            )
        return path

    def _transaction_revision_path(self, transaction: Mapping[str, Any]) -> Path:
        raw = str(transaction.get("revision_path", "")).strip()
        if not raw:
            raise ReplanTransactionError("task revision path is missing")
        path = Path(raw).expanduser().resolve()
        root = self._revision_root()
        self._require_direct_child(path, root, label="task revision")
        revision = int(transaction.get("revision", 0) or 0)
        expected = f"revision-{revision:04d}" if revision > 0 else ""
        if not expected or path.name != expected:
            raise ReplanTransactionError(
                f"task revision identity mismatch: expected {expected!r}, found {path.name!r}"
            )
        return path

    @staticmethod
    def _require_direct_child(path: Path, root: Path, *, label: str) -> None:
        if not str(path):
            raise ReplanTransactionError(f"{label} path is missing")
        if path.parent != root:
            raise ReplanTransactionError(f"{label} path escapes its managed root: {path}")

    @staticmethod
    def _safe_remove_directory(path: Path, root: Path, *, label: str) -> None:
        if path.is_symlink():
            raise ReplanTransactionError(f"refusing to remove symbolic-link {label}: {path}")
        resolved = path.resolve()
        if resolved.parent != root.resolve():
            raise ReplanTransactionError(f"refusing to remove {label} outside managed root: {path}")
        shutil.rmtree(resolved)

    def _require_no_incomplete_transaction(self) -> None:
        marker = self.state_dir / _REPLAN_TRANSACTION_FILE
        if not marker.is_file():
            return
        raw = self._read_transaction_marker(marker)
        raise ReplanTransactionError(
            "REPLAN_TRANSACTION_INCOMPLETE: a prior publication did not finish "
            f"(candidate {raw.get('candidate_id', '')}); run task replan --recover first"
        )

    def _candidate_path(self, candidate_id: str) -> Path:
        safe = str(candidate_id).strip()
        if not safe or Path(safe).name != safe or "/" in safe or "\\" in safe:
            raise ReplanError(f"invalid candidate ID: {candidate_id!r}")
        root = self._revision_root()
        path = root / f"pending-{safe}"
        if path.is_symlink():
            raise ReplanError(f"candidate path must not be a symbolic link: {path}")
        if path.exists() and path.resolve().parent != root:
            raise ReplanError(f"candidate path escapes the revision root: {path}")
        return path

    def _revision_root(self) -> Path:
        root = self.dossier / "revisions"
        if root.is_symlink():
            raise ReplanError(f"task revision directory must not be a symbolic link: {root}")
        root.mkdir(parents=True, exist_ok=True)
        resolved = root.resolve()
        if resolved.parent != self.dossier:
            raise ReplanError(f"task revision directory escapes the dossier: {root}")
        return resolved

    @contextmanager
    def _exclusive_replan_locks(self) -> Iterator[None]:
        """Hold driver + orchestrator locks for the entire snapshot/mutation operation."""

        try:
            with FileLock(
                self.state_dir / "orchestrator-driver.lock",
                level=LockLevel.DRIVER,
                timeout=0.0,
            ):
                with FileLock(
                    self.state_dir / "orchestrator.lock",
                    level=LockLevel.ORCHESTRATOR,
                    timeout=0.0,
                ):
                    yield
        except LockBusyError as exc:
            raise ReplanConflictError(
                "cannot replan while the task orchestrator/daemon is active; stop the run and retry"
            ) from exc

    @staticmethod
    def _require_agent_respected_explicit_documents(
        proposal: Any,
        candidate_docs: Mapping[str, str],
        explicit_documents: set[str],
    ) -> None:
        """Ensure semantic validation was performed against the operator's exact input."""

        if "BRIEF.md" in explicit_documents and proposal.brief_markdown != candidate_docs["BRIEF.md"]:
            raise ReplanConsistencyError(
                "ai-replan rewrote explicitly supplied BRIEF.md; candidate validation is invalid"
            )
        if "PLAN.md" in explicit_documents and proposal.plan_markdown != candidate_docs["PLAN.md"]:
            raise ReplanConsistencyError(
                "ai-replan rewrote explicitly supplied PLAN.md; candidate validation is invalid"
            )
        if "PLAN.graph.yaml" not in explicit_documents:
            return
        try:
            expected = yaml.safe_load(candidate_docs["PLAN.graph.yaml"]) or {}
            proposed = yaml.safe_load(proposal.plan_graph_yaml) or {}
        except yaml.YAMLError as exc:
            raise ReplanConsistencyError(
                f"ai-replan returned an invalid graph while validating explicit input: {exc}"
            ) from exc
        if proposed != expected:
            raise ReplanConsistencyError(
                "ai-replan rewrote explicitly supplied PLAN.graph.yaml; candidate validation is invalid"
            )

    @staticmethod
    def _normalize_string_mapping(
        raw: Mapping[str, Any],
        *,
        label: str,
    ) -> dict[str, str]:
        if not isinstance(raw, Mapping):
            raise ReplanError(f"candidate {label} must be a mapping")
        result: dict[str, str] = {}
        for key, value in raw.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ReplanError(f"candidate {label} keys and values must be strings")
            if key.strip():
                result[key.strip()] = value.strip()
        return result

    @staticmethod
    def _normalize_mapping(raw: Mapping[str, Any]) -> dict[str, str]:
        result: dict[str, str] = {}
        for old, new in raw.items():
            old_id = str(old).strip()
            new_id = str(new).strip()
            if not old_id or not new_id:
                raise ReplanError("package mapping IDs cannot be empty")
            if old_id == new_id:
                raise ReplanError("package replacements must use a new package ID")
            result[old_id] = new_id
        return result

    @staticmethod
    def _read_yaml(path: Path) -> dict[str, Any]:
        if path.is_symlink():
            raise ReplanError(f"replan metadata cannot be a symbolic link: {path}")
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise ReplanError(f"cannot read {path}: {exc}") from exc
        if not isinstance(raw, Mapping):
            raise ReplanError(f"{path} must contain a mapping")
        return dict(raw)

    @staticmethod
    def _state_summary(state: TaskExecutionStateRecord) -> dict[str, Any]:
        return {
            "project_state": state.state.value,
            "packages": [
                {
                    "id": item.id,
                    "stage": item.stage.value,
                    "status": item.status,
                    "requirements": list(item.requirements),
                    "acceptance_criteria": [criterion.description for criterion in item.acceptance_criteria],
                    "affected_repositories": list(item.affected_repositories),
                    "parent_id": item.parent_id,
                    "shard_key": item.shard_key,
                    "generated_by": item.generated_by,
                }
                for item in state.plan_graph.work_packages
            ],
        }



__all__ = ["ReplanInputs", "ReplanService"]
