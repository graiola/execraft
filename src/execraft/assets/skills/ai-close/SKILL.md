---
name: ai-close
description: Close and archive a fully completed task.
argument-hint: "[task-id]"
roles:
  - close
---

Close only after all work packages are committed, reviews approved, required
verification passed, and product documentation updated. Prefer the unified
`execraft task complete <task-id>` transaction: it creates/reuses and verifies the
immutable completion archive before stopping exact task-owned runtime resources,
detaching registered Git worktrees, tombstoning the workspace, and removing only
the generated shell. It preserves the durable dossier, archive, structured state,
task branches, and registry tombstone. Re-run the same command to resume an
interrupted completion. Use `task close --archive`, `workspace stop`, or
`workspace destroy` only when intentionally performing those lower-level steps
separately.
