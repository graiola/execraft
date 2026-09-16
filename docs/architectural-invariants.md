# Architectural Invariants

These invariants must hold across all execraft components and are enforced through code review, automated tests, and quality checks.

## Repository Isolation

**INV-001:** Task worktrees operate in isolation from each other and from the main repository.
**Enforcement:** `execraft.workspace.lifecycle.validate_workspace_location()` validates the workspace shell, and
`create_workspace_entries()` creates task worktrees through the Git-worktree helpers. Isolation-sensitive tests use
`tmp_path` to verify no cross-worktree pollution.
**Violation consequence:** Concurrent tasks could corrupt each other's work; repository state becomes unpredictable.

**INV-002:** Worktrees are never nested under other worktrees or under the main repository root.
**Enforcement:** `execraft.workspace.lifecycle.validate_workspace_location()` rejects workspace shells inside the control-plane
or project source roots; task worktrees are created under the generated workspace shell instead of beneath source repositories.
**Violation consequence:** Git operations fail or affect wrong repository; `git clean` could delete unrelated work.

## State Directory Hierarchy

**INV-003:** State directories (`.execraft/`, task dossiers, context capsules) exist outside repository worktrees.
**Enforcement:** Orchestration config validates `state_dir` is not a subdirectory of any tracked repository.
**Violation consequence:** `git clean -fdx` would delete orchestration state; task continuity breaks.

**INV-004:** Ephemeral artifacts (scratch files, repair attempts) have durable provenance linking them to an invocation.
**Enforcement:** `execraft.orchestrate.scope_recovery.ScopeRecoveryCoordinator` stores scratch provenance under orchestration
state and requires active provenance before supervisor-driven cleanup of allow-listed untracked artifacts.
**Violation consequence:** Unknown files in worktrees block automation and require human review.

## Handoff Contract Stability

**INV-005:** Handoff schema version must match between orchestrator and provider adapter.
**Enforcement:** Schema version header in `CLAUDE.md` workflow skills. Provider rejects mismatched handoffs.
**Violation consequence:** Provider cannot parse handoff; task fails at delegation boundary.

**INV-006:** Package context capsules are immutable after generation.
**Enforcement:** Capsule SHA-256 hash stored in handoff metadata. Provider verifies hash on load.
**Violation consequence:** Task execution uses stale or corrupted context; acceptance evidence becomes invalid.

## Verification Before Integration

**INV-007:** No workspace commits to main repository without passing focused verification profile.
**Enforcement:** `execraft workspace finalize` blocks on failed verification. GUI shows verification status before commit action.
**Violation consequence:** Broken code reaches main repository; downstream tasks fail.

**INV-008:** Aggregate Work Packages synthesize evidence from completed child shards, not from child criterion naming patterns.
**Enforcement:** Aggregate evidence collection reads shard completion records and verification results, not criterion IDs.
**Violation consequence:** Renaming criteria breaks aggregate evidence; false negatives pause human review unnecessarily.

## Lock Hierarchy

**INV-009:** Locks are acquired in consistent order: project lock → task lock → workspace lock.
**Enforcement:** Documented in `docs/lock-hierarchy.md`. Code review enforces order.
**Violation consequence:** Deadlock between concurrent operations; orchestrator hangs.

## Test Isolation

**INV-010:** Tests that create filesystem, repository, or control-plane state use caller-owned disposable locations; no test writes durable state to the repository root or a shared hardcoded temp path.
**Enforcement:** Test helpers accept pytest `tmp_path` subdirectories, CLI-style tests redirect execraft config/state homes, and focused regressions cover isolation-sensitive workflows.
**Violation consequence:** Test pollution, order-dependent failures, developer-state interference, and unsafe parallelization.

## Quality Check Non-Regression

**INV-011:** Ruff select rules, mypy coverage, and architecture budgets do not silently contract.
**Enforcement:** `pyproject.toml` and architecture-checker changes are reviewed for reductions; any intentional reduction carries an explicit rationale and updates `docs/quality-checks.md`.
**Violation consequence:** Code quality regresses silently; bugs reappear in unchecked modules or architectural debt ceilings grow unnoticed.

## Project Planning Ownership

**INV-012:** Project roadmaps are planning views over canonical tasks, not an execution scheduler or duplicate task database.
**Enforcement:** Linked roadmap items persist only task identity plus roadmap-local schedule/lane/order metadata; `RoadmapProjection` resolves task status and progress dynamically; roadmap relations are never consumed by the orchestrator; destructive roadmap operations preserve linked tasks.
**Violation consequence:** Task state can diverge between planning and execution, or a GUI planning edit can unexpectedly change runtime behavior.


## Automatic Project Execution

Automatic Project Execution is an opt-in policy over **Tasks**, never Work Packages.
Its planner is pure and side-effect free. Only `ProjectExecutionEngine.automatic_cycle`
may translate a selected Task into a durable start intent and `TaskExecutionPort.start`
call. Project status/reconciliation paths remain observational. Running Tasks and
unresolved start intents consume bounded concurrency; a Project failure policy may
block future starts or place the Project on Hold, but it cannot pause/cancel a
running Task or reach into Task scheduling internals.
## Provider-Neutral Project Delivery

**INV-013:** A `ProjectMilestone` freezes an achieved baseline but never owns a
delivery provider, deployment backend, registry, credentials, or external
target configuration.
**Enforcement:** `execraft.project_execution.delivery` derives immutable candidates
from achieved baselines and calls external systems only through
`DeliveryProvider`; the architecture checker rejects provider/task-runtime
implementation imports from the delivery package; regression tests pin the
`ProjectMilestone` field contract.
**Violation consequence:** Historical Project Execution definitions become
provider-specific, baseline reproducibility depends on mutable deployment
configuration, and delivery concerns can leak back into Task/Milestone control.

**INV-014:** Delivery side effects are intent-backed and unresolved outcomes are
never blindly replayed.
**Enforcement:** `delivery.json` records the operation before the provider call,
terminal results are immutable, adapter exceptions become `UNCERTAIN`, and
recovery uses `DeliveryProvider.reconcile()` with the original operation/provider
identity.
**Violation consequence:** A crash or lost response can publish/deploy the same
baseline more than once without an auditable operator decision.

## Canonical Project/Task Terminology

**INV-015:** Phase, Gate and Milestone are Project Execution concepts; Task Execution uses Task, Work Package, Stage, Check, Hold, Verification and Attempt. Historical Task spellings are read-only compatibility and may not be emitted by active writers.
**Enforcement:** `tools/check_architecture.py` rejects retired Task symbols, old writable/API surfaces, Task-domain uses of Project-only nouns outside the explicit compatibility readers, and retired milestone GUI assets. `tools/check_docs.py` rejects links/names for retired Task milestone files. Compatibility readers normalize historical journal event names, PLAN headings, directive filenames and repository-sync configuration without rewriting immutable history.
**Violation consequence:** Project and Task execution regain conflicting meanings for Gate/Milestone, GUI/API contracts become ambiguous, and migrated historical state can leak deprecated vocabulary back into new writes.
