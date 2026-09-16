---
name: ai-workspace-sync
description: Refresh workspace metadata and source heads safely.
argument-hint: "[task-id]"
roles:
  - workspace
---

Run `execraft workspace sync <task-id>` and then `execraft workspace status <task-id>`. Do not copy repositories or generated outputs manually; project-specific source synchronization must be a product-owned command invoked through `execraft workspace run`.
