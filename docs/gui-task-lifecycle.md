# GUI plan and task-lifecycle workbench

The dashboard composes task-definition import, the canonical replanning transaction,
task completion, and workspace lifecycle safety through one surface. The browser does not
implement a second lifecycle engine: every mutation
delegates to the same Python services used by the CLI.

## Plan view

Open an active task and choose **Plan**. The page lazily loads the
accepted `BRIEF.md`, `PLAN.md`, and `PLAN.graph.yaml`; these files are not added to
the normal three-second dashboard polling payload.

The header shows:

- task lifecycle status;
- accepted definition revision and integrity state;
- completion transaction status/phase;
- workspace/runtime/tombstone state.

The document editors are staging editors. Typing in them never writes the live
dossier. **Stage candidate** sends only documents that changed relative to the
accepted revision, plus the optional natural-language change request and explicit
active-package supersession map, to the canonical `ReplanService`.

`ai-replan` remains read-only. Explicit operator document content is authoritative;
the agent may only fill a graph that was not supplied by the operator. Structural-
only validation remains an explicit opt-in and is visibly weaker than semantic
validation.

## Candidate impact and application

A staged candidate is durable under `revisions/pending-*`. The GUI displays:

- unified diffs for all three task-definition documents;
- deterministic package classifications;
- added/removed/completed/active package counts;
- explicit blockers and warnings;
- consistency mode and agent identity;
- package supersession mappings.

The **Apply candidate** button is enabled only when deterministic impact analysis
reports the candidate as applicable. Clicking it still does not bypass replanning safety:
agent invocations must be quiescent, commit transactions must be clear, changed
active packages must satisfy supersession rules, workspace safety must be proven,
and the publication is crash-recoverable.

An interrupted `replan-transaction.yaml` is surfaced prominently. **Recover
interrupted replan** calls the same recovery routine as:

```bash
execraft task replan <task-id> --recover
```

`ReplanService` decides whether the durable transaction must be rolled back or
completed forward.

## Manual file edits and definition drift

If `BRIEF.md`, `PLAN.md`, or `PLAN.graph.yaml` was edited outside Execraft, the Plan
view shows `TASK_DEFINITION_DRIFT`. **Adopt current files** stages those exact live files
with the canonical `from_current_files` path; it never silently blesses drift. The
candidate must still pass semantic or explicitly accepted structural validation
and deterministic impact analysis before application.

## Completion and cleanup

The completion panel runs the task-completion dry-run preflight when the Plan view is
loaded. It lists the exact intended phases before enabling **Complete & clean
workspace**:

1. create/reuse and verify the immutable completion archive;
2. stop task-owned runtime resources;
3. remove registered task-owned Git worktrees through Git;
4. remove the generated workspace shell when policy allows it.

The destructive action remains gated by workspace-lifecycle and task-completion checks. Dirty worktrees, active
orchestrators, Git operations, ownership mismatches, stale archives, incomplete
acceptance evidence, or invalid completion state fail closed. A failed completion
leaves its durable report and can be resumed safely.

The GUI never deletes task branches. It explicitly shows retained resources,
including the live dossier, task branches, immutable archive, and workspace
registry tombstone.

## Workspace ownership

Deep Git inspection is lazy and runs only when the Plan view is loaded.
Normal dashboard polling reads only the lightweight workspace registry summary.
The ownership panel shows for each repository:

- source and worktree paths;
- mutability;
- expected/current branch;
- worktree existence;
- dirty state;
- in-progress Git operation, if any;
- inspection failures.

This makes it possible to verify exactly what task completion will retire without turning the
main dashboard poll into repeated `git status` work.

## Revision history

Accepted revision directories remain immutable and are shown newest-first. The
view exposes the candidate ID, change request, generation source, consistency mode,
and revision path when available. The GUI does not rewrite or prune revision
history.

## API surface

The local authenticated dashboard uses these task-scoped endpoints:

- `GET /api/task/lifecycle`
- `GET /api/task/replan/candidate?candidate_id=...`
- `POST /api/task/replan/candidate`
- `POST /api/task/replan/apply`
- `POST /api/task/replan/recover`
- `POST /api/task/complete`

Mutation booleans use strict JSON boolean decoding. The server never treats the
strings `"true"` or `"false"` as authorization.

## Upstream synchronization

The Plan workbench can stage a repository-sync Work Package before an unstarted package, inspect ahead/behind
divergence, refresh configured upstream refs on demand, and show the durable transaction state. A rollback action is exposed
only before the first merge commit; forward-only transactions must be resumed. All graph mutation still goes through
`ReplanService`, and rollback runs through the canonical orchestrator CLI path rather than a second GUI Git implementation.
See [Repository synchronization Work Packages](repository-synchronization.md).

## Work Package Pause & Sync

The Work Package **Overview** side inspector exposes repository synchronization without requiring the operator to open the Plan workbench first. Graph cards stay intentionally compact; synchronization and other administrative controls live in the inspector:

- pending top-level Work Packages show **Sync before**;
- started top-level Work Packages show **Pause & Sync**;
- completed, generated-shard, and repository-sync Work Packages cannot schedule another Work Package sync.

The separate repository-synchronization dialog shows every task-owned repository, keeps the task branch read-only, and lets the operator choose an upstream remote branch per repository. The configured base branch is the default. Non-base selections are highlighted as explicit operator overrides. The operator can refresh remote branch discovery, preview exact divergence, choose AI-vs-human conflict handling, and decide whether development should resume automatically after the sync.

The browser only queues a durable request. If orchestration is already running, the request is consumed at a safe scheduler
boundary. If it is idle, the dashboard starts the normal orchestration driver after queueing. In both cases replanning owns graph
publication and repository synchronization owns Git mutation; no browser endpoint performs a direct merge.

Card divergence badges are intentionally cache-backed. Normal three-second dashboard polling never executes `git status`, `git fetch`, or remote branch discovery. The badge updates after the operator explicitly opens or previews the synchronization dialog.
