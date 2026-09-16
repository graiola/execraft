# Workspace lifecycle safety

Task workspaces contain disposable orchestration files, but their Git worktrees
may contain valuable uncommitted product changes. Execraft therefore separates
reversible runtime shutdown from destructive workspace retirement and sends every
retirement path through the same deterministic lifecycle service.

## Lifecycle commands

### Stop runtime resources

```bash
execraft workspace stop <task-id>
```

`stop` shuts down only the runtime resources owned by the workspace. It retains:

- every Git worktree and task branch;
- the generated workspace shell;
- the workspace registry record;
- the task dossier and orchestration state.

For Docker/Compose workspaces, Execraft selects containers by the exact recorded
Compose project and requires the `execraft.managed=true` ownership label before it
removes anything. It removes the corresponding Compose networks after the
containers stop. Named volumes are retained. Execraft never uses a broad Docker
prune operation.

Runtime shutdown is blocked while the orchestrator is active or a commit
transaction is pending. `--dry-run` prints the checks and exact resources without
changing them.

### Destroy a completed workspace

```bash
execraft task close <task-id> --archive
execraft workspace destroy <task-id> --remove-shell
```

Normal destruction requires a checksum-verified completion archive. The
preflight also requires:

- a valid task and workspace ownership marker;
- a completion archive that still matches the live dossier and final task-branch commits;
- an idle orchestrator;
- no pending commit transaction;
- an unpinned workspace;
- clean task-owned repositories;
- no merge, rebase, cherry-pick, bisect, or other Git operation;
- every generated path to remain inside the recorded workspace shell;
- every worktree to be registered by the recorded source repository;
- every worktree to remain on its recorded task branch.

The service stops runtime resources first, removes generated worktrees with
`git worktree remove`, and runs `git worktree prune`. It then writes a registry
tombstone. With `--remove-shell`, the generated shell is deleted last and only
when its top-level entries are all known Execraft artifacts or registered
worktrees. Product source repositories and task branches are never deleted.

Without `--remove-shell`, the empty generated shell and its ownership marker are
retained. Repeating destruction after a completed retirement is safe and
idempotent.

## Force recovery

```bash
execraft workspace destroy <task-id> --force [--remove-shell]
```

`--force` is a disaster-recovery tool, not the normal completion path. It may
bypass archive, pin, dirty-tree, Git-operation, and runtime-shutdown policy
checks. It can therefore discard uncommitted changes and leave runtime resources
for manual cleanup.

Force mode cannot bypass an active orchestrator or ownership integrity. The
service holds the task's real orchestrator lock throughout the mutating operation,
preventing a restart between preflight and worktree removal. Execraft also refuses
to operate when the marker and registry disagree, a repository route escapes the
workspace, a path is not a worktree of the recorded source repository, or the
checked-out branch does not match the recorded task branch.

## Automatic stale-workspace cleanup

Disk-pressure cleanup does not call `shutil.rmtree()` on workspace candidates.
A stale candidate is delegated to the same lifecycle service used by the CLI.
Automatic retirement requires all normal destroy checks, including a verified
archive, and always requests shell removal. A pinned, active, dirty, unarchived,
malformed, or ownership-ambiguous workspace is preserved with a diagnostic.
When no lifecycle service is configured, stale workspaces are preserved.

Age is only a candidate-selection signal; it is never authorization to delete a
workspace.

## Restart and partial-failure behavior

The registry is the durable transaction marker:

1. runtime resources are stopped;
2. worktrees are detached through Git;
3. the registry is tombstoned as `removed`;
4. the optional shell is deleted last.

An already-absent generated worktree is treated as a completed step. If shell
removal is interrupted after the tombstone is written, rerunning the command
finishes the shell cleanup. If the workspace is already fully retired, rerunning
the command returns success without touching any repository.

If runtime shutdown or Git removal fails in normal mode, later destructive steps
are not attempted. The error is reported and the remaining workspace is
preserved for inspection.

## Retained records

Safe retirement intentionally retains:

- the immutable completion archive;
- the versioned task dossier and `COMPLETION.yaml`;
- task branches and their commits;
- the workspace registry tombstone;
- orchestration and audit state according to normal retention policy.

Task branches should be deleted only through a separate merge-aware policy that
proves their commits remain reachable.
