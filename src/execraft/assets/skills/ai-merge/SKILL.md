---
name: ai-merge
description: Prepare and perform a controlled multi-repository merge.
argument-hint: "[task-id]"
roles:
  - merge
---

Merge only an approved task with recorded commits and passing integration verification. Inspect the configured merge strategy and target branches, perform non-destructive preparation, and require explicit authorization before finalizing or pushing. `Execraft` must never force-push or reset protected branches.
