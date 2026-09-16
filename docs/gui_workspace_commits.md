# Inspecting and committing task-workspace changes from the GUI

The local dashboard includes a **Changes** view for inspecting and manually
resolving dirty-workspace changes without switching between several Git
terminals. When `scheduling.automatic_recovery: true`, the normal workflow first
uses the orchestrator's agent-assisted workspace-recovery transaction. This view
is the explicit operator fallback, not a generic shell and not a replacement for
verification, review, scope, or commit readiness checks.

The view is intentionally review-first: repository names are compact filters,
changed files are the primary list, **All / Modified / Added / Deleted** narrows
that list, and the selected path opens directly beside it. Commit controls stay
collapsed until the operator is ready to finalize reviewed selections.

## Intended workflow

For an eligible scope check, use **Recover workspace / Resume** first. The selected
fixer can restore accidental changes or retain legitimate exact paths, after which
the orchestrator reruns checks and commits automatically. Use the manual workflow
below only when recovery is disabled, unavailable, exhausted, or intentionally
overridden by an operator.

1. Stop or wait for the active orchestration driver. Inspection remains available
   during a run, but AI message generation and commits are disabled.
2. Open **Changes** and select a task-owned repository.
3. Open every relevant changed path and inspect its staged, working-tree, or
   untracked-file view.
4. Select only the paths that belong in the same logical commit. Generated
   untracked files that must not be committed can instead be selected and removed
   with **Delete untracked**.
5. Either write the commit subject/body manually or choose a review-capable agent
   and select **Generate message**.
6. Review and edit the proposed message. AI generation never authorizes a commit.
7. Confirm that the selected diffs and available verification evidence were
   reviewed, then select **Commit selected**.
8. Return to **Run** and use **Reconcile / Resume** when the original Check/Hold is
   now obsolete or start the next normal run.

The dashboard creates one commit per selected repository and never pushes.
Multi-repository transactions are recorded in the normal commit journal.

## What can be inspected

The GUI reads only repositories registered in the active task workspace. It uses
`git status --porcelain=v2 -z` so spaces, renames, deletions, staged changes,
working-tree changes, untracked files, and conflicts remain distinguishable.

For a selected path it displays:

- the cached/staged diff;
- the working-tree diff;
- a bounded preview for an untracked regular file;
- the original path for a rename;
- truncation and binary indicators.

Diffs are bounded to protect the browser and server. Large or binary changes
still require external inspection before the review acknowledgement is checked.

## Removing generated untracked files

**Delete untracked** is a deliberately narrower operation than Git restore or
clean. It physically removes only explicitly selected paths that the current
porcelain snapshot still classifies as untracked files or symlinks. A typical use
is deleting `__pycache__/*.pyc` before preparing a commit.

Deletion uses the same task-workspace boundaries as commits:

- the orchestration driver must be idle;
- the repository must be `task_owned` and on its expected task branch;
- the browser must provide the current repository status digest;
- every selected path must still exist in the current untracked set;
- tracked modifications, staged paths, conflicts, and directories are rejected;
- empty parent directories are removed only up to, but never including, the
  repository root.

The server validates every selected repository before deleting the first file.
Successful and failed cleanup operations are appended to the GUI audit log as
`workspace_untracked_deleted` and `workspace_delete_failed`. The action does not
stage, commit, push, or silently discard tracked work.

## Commit safety boundaries

A commit is rejected when any of the following is true:

- an orchestration driver is active, including a driver launched outside the GUI;
- the repository is not `task_owned`;
- the repository is not on its task branch;
- a selected path is no longer modified;
- the repository status digest changed after the browser loaded it;
- merge conflicts are present;
- an already-staged path exists outside the selection;
- no selected change produces staged content;
- the operator did not acknowledge review;
- the subject or body exceeds the configured bounds.

The status digest includes the current `HEAD` and complete porcelain-v2 status.
This makes an old browser selection fail closed when another terminal, agent, or
editor changes the worktree after inspection.

The dashboard never exposes arbitrary Git arguments. It does not reset, restore,
checkout, rebase, merge, amend, force, or push. The only destructive filesystem
action is the explicit, digest-checked removal of selected untracked files or
symlinks described above. Unselected worktree changes remain untouched.

## AI commit-message generation

Expand **Commit reviewed changes** after inspecting the selected diffs. **Generate
message** sends only bounded excerpts for the selected paths to one
configured, currently available, review-capable agent. The adapter is constructed
in read-only mode and receives a strict `{subject, body}` output contract.

The agent is instructed to summarize already-reviewed changes. It must not perform
a new code review, modify files, run tools, or authorize the commit. The framework
validates the response, then places it in editable fields. The operator remains
responsible for correctness and must explicitly confirm the final commit.

Selecting a remote provider can disclose the selected diff excerpts to that
provider. Select a local Ollama/Qwen review agent when the diff must remain inside
the AI cluster.

This differs deliberately from allowing an agent to execute `git commit`: the
agent proposes text; the dashboard stages the exact operator selection and creates
the commit deterministically.

## Audit and recovery

Dashboard commit activity is appended to:

```text
~/.local/state/execraft/projects/<task-id>/gui-commit-audit.jsonl
```

Transactions and repository snapshots are recorded in:

```text
~/.local/state/execraft/projects/<task-id>/commit-journal.json
```

A multi-repository commit cannot be atomically rolled back across independent Git
repositories. The dashboard therefore preflights every selected repository before
creating the first commit. If a later repository still fails, it reports the
commits already created and records the partial transaction; inspect the journal
instead of blindly retrying.

A successful commit does not automatically clear every `human_required` state.
The normal scope reconciliation remains authoritative and revalidates the clean
workspace and current package state under the orchestration lock.
