---
name: ai-workspace-destroy
description: Safely retire an archived task workspace without losing product changes.
argument-hint: "[task-id]"
roles:
  - workspace
---

Use `execraft workspace stop <task-id>` when only task-owned runtime resources must
be shut down; this command retains all Git worktrees and the generated shell.

Before destructive retirement, verify that the task is complete and create its
immutable archive with `execraft task close <task-id> --archive`. Then run
`execraft workspace destroy <task-id> --remove-shell`. The deterministic preflight
must confirm an idle orchestrator, no pending commit transaction, a valid
ownership marker, an unpinned workspace, clean repositories, no Git operation,
registered worktrees, and expected task branches. Worktrees must be removed
through Git, never by deleting their directories directly.

Use `--force` only for operator-approved disaster recovery after explaining that
it may discard uncommitted changes or leave runtime resources. Force must never
bypass an active orchestrator, marker, path-routing, worktree-registration, or
branch-ownership integrity.
Never use broad Docker pruning or delete task branches as part of workspace
retirement.
