---
name: ai-switch
description: Select the active task in a generated workspace.
argument-hint: "[task-id]"
roles:
  - switch
---

Validate the target with `execraft task status <task-id>`, then run `execraft task switch <task-id> --workspace-root <workspace-root>`. Re-run task and workspace status. Never edit active-task pointers by hand.
