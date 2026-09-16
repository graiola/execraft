---
name: ai-plan
description: Turn an approved brief into a dependency-ordered executable plan.
argument-hint: "[task-id]"
roles:
  - plan
  - decompose
---

Plan the selected task.

1. Run `execraft task status <task-id>` and read `DEFINITION.yaml`, `BRIEF.md`, `TASK.yaml`, existing `PLAN.md`, and repository evidence.
2. Define small work packages with stable IDs, dependencies, affected repositories, requirements, acceptance criteria, verification profile, and risk.
3. Maintain the human-readable `PLAN.md` and the machine-readable `PLAN.graph.yaml`; validate with `execraft plan validate --task-id <task-id>`. If `DEFINITION.yaml` marks `PLAN.md` as imported, preserve that Markdown exactly and generate or repair only the executable graph unless an explicit replanning workflow authorizes a definition revision.
4. Keep project-specific commands in the project verification registry or `TASK.yaml`, never in `Execraft` core.
5. Document amendments and compatibility removal tranches.

Do not implement or commit while planning.
