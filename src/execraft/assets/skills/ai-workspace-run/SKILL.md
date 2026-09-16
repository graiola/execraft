---
name: ai-workspace-run
description: Run an approved command in a registered worktree.
argument-hint: "[task-id]"
roles:
  - workspace
---

Validate first with `execraft workspace status <task-id>`. Execute with `execraft workspace run <task-id> --repository <repository-id> -- <command...>`. Use only product-owned commands allowed by the workspace policy; never expose arbitrary remote shell execution.
