# Versioned task replanning

Active task definitions can be changed through a controlled versioned transaction
without rewriting or losing durable orchestration history. Replanning is deliberately split into
a read-only proposal phase and a deterministic publication phase.

## Safety model

`BRIEF.md`, `PLAN.md`, and `PLAN.graph.yaml` form one accepted task-definition
revision. `DEFINITION.yaml` stores their accepted hashes and revision number.
Every orchestration resume verifies those hashes. Manual edits therefore stop
execution with `TASK_DEFINITION_DRIFT` instead of silently running the old graph
against new prose.

The following invariants are enforced by code, not by the model:

- completed package contracts are immutable;
- completed evidence is retained byte-for-byte in migrated state;
- a started package may stay active only if its semantic contract is unchanged;
- changed started work must be removed and explicitly mapped to a genuinely new
  package ID;
- pending packages may be added, removed, split, reordered, or changed;
- the candidate graph must remain valid, acyclic, and within the task's existing
  repository ownership;
- active-package supersession requires clean task-owned worktrees and no Git
  operation; every revision application requires the commit journal to have no
  pending transaction;
- package IDs are permanent identities: retired packages and runtime-generated
  shard IDs cannot be recycled for unrelated work in a later revision;
- no provider invocation may remain in the durable `running` state while a
  candidate is created or applied;
- the driver and orchestrator locks are held while a candidate is snapshotted
  and while a revision is published;
- context capsules are invalidated after publication so later agents cannot use
  stale package context;
- replanning from `review`, `approved`, or `blocked` returns the task lifecycle
  to `in_progress`; `integrating`, `merged`, `closed`, and `abandoned` tasks are
  rejected.

Replanning does not change task repository topology or base branches. Those are
lifecycle migrations rather than ordinary planning changes.

## Candidate workflow

A normal change request creates a candidate but does not mutate the live task:

```bash
execraft task replan my-task \
  --request "Split feature into API and integration packages"
```

Inspect the reported package impact, then apply the candidate explicitly:

```bash
execraft task replan my-task --candidate r0002-0123456789
execraft task replan my-task --candidate r0002-0123456789 --apply
```

`--apply` may also be used with candidate creation when an operator intentionally
wants a single command after reviewing the input policy:

```bash
execraft task replan my-task \
  --request "Add a remediation package for the completed migration" \
  --apply
```

The read-only `ai-replan` skill may propose updated documents and package
mappings. The model never writes the dossier, Git worktrees, state files, or
transaction journal. Deterministic validation always runs after the proposal.

The local dashboard exposes the same transaction under the **Plan** view. It stages edits as candidates, shows deterministic impact/diffs, and delegates apply/recovery to `ReplanService`; see [`gui-task-lifecycle.md`](gui-task-lifecycle.md).

## Replanning from explicit files

An operator can stage replacement documents using the same bounded, UTF-8 and
symlink-safe import contract as initial task creation:

```bash
execraft task replan my-task \
  --brief-file ./BRIEF.md \
  --plan-file ./PLAN.md \
  --plan-graph-file ./PLAN.graph.yaml
```

Explicit operator documents are authoritative. The AI consistency pass may
reject a contradiction, but it does not silently rewrite supplied Markdown.
When `PLAN.md` changes without a supplied graph, a planning provider must derive
the new executable graph.

If no provider is available, `--allow-structural` is an explicit operator escape
hatch for cases where a complete candidate graph has already been supplied:

```bash
execraft task replan my-task \
  --plan-graph-file ./PLAN.graph.yaml \
  --allow-structural
```

This records that semantic BRIEF/PLAN coherence was not checked by AI.

## Started package supersession

A started package is never edited in place. Replace it with a new package ID and
provide an explicit mapping:

```bash
execraft task replan my-task \
  --plan-file ./PLAN.md \
  --plan-graph-file ./PLAN.graph.yaml \
  --supersede feature=feature-api
```

Before application Execraft proves all task-owned worktrees are clean, that no
Git operation is active, and that the commit journal has no pending transaction.
The old runtime/evidence state remains in the revision snapshot.

Completed packages cannot be superseded or removed. If accepted requirements
change completed work, retain the completed package unchanged and create a new
remediation package.

## Manual edits and definition drift

Editing live dossier files directly is supported only as an input to a replan.
The next run/resume fails rather than accepting the change automatically:

```text
TASK_DEFINITION_DRIFT: ...
```

To intentionally adopt the edited files:

```bash
execraft task replan my-task --from-current-files
```

This creates a normal candidate revision. The live files are not considered
accepted until that candidate is applied. Structural-only adoption requires the
same explicit `--allow-structural` acknowledgement when no semantic provider is
available.

## Durable revision layout

Candidates are staged under the task dossier:

```text
revisions/
  revision-0001/
    BRIEF.md
    PLAN.md
    PLAN.graph.yaml
    DEFINITION.yaml
    REVISION.yaml
  pending-r0002-<id>/
    BRIEF.md
    PLAN.md
    PLAN.graph.yaml
    CANDIDATE.yaml
    IMPACT.json
    REPLAN_REPORT.md
    STATE.before.json
    DEFINITION.before.yaml
    TASK.before.yaml
```

Candidate document hashes are recorded in `CANDIDATE.yaml` and checked before
inspection/application. Editing staged candidate files invalidates the
candidate; regenerate it instead.

After successful publication the directory becomes:

```text
revisions/revision-0002/
```

and contains `APPLIED.yaml` plus `DEFINITION.accepted.yaml`, making the accepted
revision independently integrity-checkable. `DEFINITION.yaml` advances to
revision 2 and keeps a bounded history of superseded definition hashes, source
provenance, every package ID ever seen, and replan metadata. Completion archives
copy the whole dossier, including revision history.

Tasks created before revision snapshots existed are adopted lazily before their
first replan. Execraft records that migration explicitly rather than
pretending the baseline was natively versioned.

## Transaction and recovery

Publication updates several durable surfaces: definition documents, provenance,
orchestration state, context capsules, and revision history. A transaction
marker and backup are written before mutation. `PLAN.graph.yaml` is published
last among the three definition documents as the executable-definition commit
marker.

If a process exits mid-publication, orchestration refuses to resume with
`REPLAN_TRANSACTION_INCOMPLETE`. Recover with:

```bash
execraft task replan my-task --recover
```

Recovery follows the durable transaction marker. A `prepared` transaction is
rolled back to the exact backed-up dossier/state/directives and restores a
candidate that had already been moved. Once the marker is `committed`, rollback
is no longer allowed: recovery verifies the accepted revision and completes
forward-only bookkeeping (lifecycle status, journal/checkpoint/runtime status,
and transaction cleanup).

## AI context budget

The replanning agent is deliberately bounded to 384 KiB of current/candidate
contract context. Oversized task definitions fail explicitly instead of
reintroducing the full-dossier token explosion fixed by context budgeting. For
large tasks, provide a complete executable candidate graph and reduce the
semantic change scope.

## Inserting repository synchronization Work Packages

Repository synchronization is modeled as a first-class work package but inserted through the same versioned replanning transaction. Use `execraft task sync-before <task> --before <package>` or the Plan workbench. This preserves completed package identity/evidence and gives the sync its own verification, independent review, transaction, and recovery state. See [Repository synchronization Work Packages](repository-synchronization.md).

## Declarative graph boundary and legacy tasks

Accepted replanning revisions contain only declarative package semantics.
Runtime package state (`stage`, `status`, attempts, reviewers, holds,
etc.) and acceptance evidence (`verified`, `evidence`) live in orchestration
state/evidence stores and are never valid inputs to a new accepted graph revision.

Older task dossiers can still contain those fields because they were authored
before this separation. Compatibility features that transform such a graph
(for example Work Package card repository synchronization) must use the shared
legacy projection in `execraft.plan_contract`; they must not relax current replanning validation.
The projection strips only known runtime-owned fields and leaves unknown fields
visible for strict rejection.

New locally generated and AI-generated task graphs are declarative-only at
creation time, so ordinary future replanning does not require this migration
step. Explicit imports are preserved according to the import provenance rules;
legacy imported graphs remain compatible with synchronization through the same
projection boundary.
