---
name: ai-workspace-status
description: Validate a generated workspace and report drift.
argument-hint: "[task-id]"
roles:
  - workspace
---

Run `execraft workspace status <task-id>`. Report repository ownership, branches, heads, dirty state, policy, capabilities, generated-file drift, and missing paths. Treat drift or wrong-branch findings as blockers.
