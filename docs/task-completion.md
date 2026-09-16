# Unified task completion

Successful orchestration is finalized through a fail-closed task-level completion transaction.
Package finalization still owns commits and clean repository state; task completion owns
immutable archival and retirement of the disposable ai-workspace.

## Default flow

When `execraft orchestrate run` or `execraft orchestrate daemon` reaches the durable
`completed` orchestration state, the default `scheduling.task_completion` policy runs:

1. validate completion-archive prerequisites;
2. create or reuse the immutable completion archive;
3. verify its checksum manifest;
4. materialize `COMPLETION.yaml` and close `TASK.yaml`;
5. stop exact task-owned runtime resources;
6. remove task-owned Git worktrees with `git worktree remove`/prune semantics;
7. tombstone the workspace registry;
8. remove the generated workspace shell after ownership validation;
9. write `completion-report.yaml` and append completion journal events.

The task branch, live task dossier, immutable archive, orchestration history and
workspace tombstone are retained. Task completion never deletes task branches, pushes, resets, or
removes product repositories.

If any completion Check fails, orchestration remains truthfully `completed` but the driver exits
non-zero and the workspace is preserved at the last safe point. Resume with:

```bash
execraft task complete <task-id>
```

The transaction lives in the task state directory as `completion-transaction.yaml`.
Repeating completion is idempotent and re-verifies the archive and retirement state.

The local dashboard exposes the same dry-run and completion transaction in the **Plan** view, together with
retained-resource and worktree ownership projections; see [`gui-task-lifecycle.md`](gui-task-lifecycle.md).

## Manual completion and preview

Run the same unified path directly:

```bash
execraft task complete <task-id>
```

Preview all archive and non-destructive retirement checks without creating an archive or
changing the workspace:

```bash
execraft task complete <task-id> --dry-run
```

`execraft task close <task-id> --archive` remains available as an archive-only compatibility
operation when the workspace must intentionally remain available. `execraft workspace stop`
and `execraft workspace destroy` remain lower-level lifecycle tools.

## Configuration

Configure completion under `scheduling.task_completion` in `agents.yaml`:

```yaml
scheduling:
  task_completion:
    automatic: true
    require_archive: true
    verify_archive_before_cleanup: true
    stop_runtime: true
    remove_worktrees: true
    remove_workspace_shell: true
    retain_task_branches: true
    retain_live_dossier: true
    retain_workspace_tombstone: true
```

The values above are the defaults. Safety dependencies are validated strictly:

- worktree removal requires an archive and runtime shutdown;
- shell removal requires worktree removal;
- Task completion does not allow disabling branch, dossier, or tombstone retention.

Set only `automatic: false` to keep the old operator-triggered completion cadence while
retaining `execraft task complete` as the canonical transaction.

## Recovery and integrity

A completion record does not authorize deletion by itself. Workspace retirement still
passes workspace-lifecycle ownership, archive freshness, Git branch, clean-tree, pending-operation,
pin, runtime and orchestrator-lock checks. A completed transaction is rechecked on later
invocations; archive tampering, a resurrected shell, or a missing workspace tombstone is
reported as an integrity failure.

An interrupted transaction must be resumed with the same policy. This prevents changing
destructive semantics midway through cleanup. The immutable archive path and manifest
hash are persisted before workspace retirement, so a restart does not duplicate archives.
