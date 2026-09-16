---
name: ai-implement
description: Implement one approved work package with tests and evidence.
argument-hint: "[task-id]"
roles:
  - implement
---

Implement the selected task or work package.

1. Run `execraft task status <task-id>` and `execraft workspace status <task-id>`. Work only in registered task-owned worktrees.
2. Read `AGENTS.md` and the generated package context capsule supplied in the handoff. Treat its requirements, acceptance criteria, scoped plan section, decisions, and evidence as the default task context. Read additional dossier material only when the capsule references it or a concrete ambiguity cannot be resolved from code.
3. Validate assumptions against code before editing. Keep changes within the declared repositories and scope.
4. Add or update deterministic tests and product documentation with the implementation.
5. Run targeted checks frequently, then `execraft workspace verify <task-id> --profile focused` and the required broader profile.
6. Return material decisions, completed-slice evidence, and risks as structured completion evidence. Do not read or append the complete `HANDOFF.md` during ordinary implementation; orchestration persists current evidence separately and keeps `HANDOFF.md` as a human archive. `RUNTIME_STATUS.md` is generated from durable state.
7. Return structured completion evidence; do not claim success from prose alone.

Do not commit, push, rewrite history, bypass verification, or modify generated workspace files.
