---
name: ai-status
description: Report grounded task, workspace, Git, verification, and review status.
argument-hint: "[task-id]"
roles:
  - status
---

Run `execraft task status <task-id>` and `execraft workspace status <task-id>`. Summarize branch/head/dirty state per repository, current work package and stage, verification results, review findings, resource pressure, and blockers. Do not infer completion from agent prose.
