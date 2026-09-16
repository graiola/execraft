---
name: ai-brief
description: Create or refine a task brief from concrete repository evidence.
argument-hint: "[task-id]"
roles:
  - brief
---

Create or refine the selected task brief.

1. Resolve the task with `execraft task status <task-id>` and inspect the registered project descriptor and `DEFINITION.yaml`.
2. Read the current dossier and relevant repositories; do not invent architecture or paths.
3. Update `BRIEF.md` with goals, non-goals, constraints, current evidence, risks, and measurable outcomes. If `DEFINITION.yaml` marks the brief as imported and execution has begun, do not silently rewrite it; definition changes require the explicit replanning lifecycle.
4. Keep host paths, credentials, generated state, and provider transcripts out of versioned documents.
5. Record unresolved product decisions explicitly instead of hiding them in implementation notes.

Do not modify product code, commit, push, or run destructive commands.
