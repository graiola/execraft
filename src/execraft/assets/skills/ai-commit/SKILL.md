---
name: ai-commit
description: Create reviewed commits for the selected task.
argument-hint: "[task-id]"
roles:
  - commit
---

Before committing, require clean orchestration checks: acceptance evidence complete, required verification passed, and review approved. Run `execraft task status <task-id>` and inspect each repository diff. Use `execraft task commit <task-id> -m <message> --repositories <ids...>` only after explicit user authorization. Record resulting SHA values in the append-only `HANDOFF.md` engineering history; do not push. Live progress remains generated in `RUNTIME_STATUS.md`.

When the local GUI is used, AI participation is limited to generating a read-only commit-message proposal from the operator-selected diff. The dashboard, not the agent, stages and commits the exact selected paths after explicit review acknowledgement. Never push or treat an AI-generated message as commit authorization.
