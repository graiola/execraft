---
name: ai-workspace-create
description: Create a disposable multi-repository workspace shell.
argument-hint: "[task-id]"
roles:
  - workspace
---

Create the workspace with `execraft workspace start <task-id> --source-root <bound-source-root> --workspace-root <path> --policy <profile>`. Then run `execraft workspace status <task-id>` and open it with `execraft code <task-id>`. Do not create AI files in product repositories.
