# Task dossier: {{ title }}

This directory contains the versioned coordination contract for one task.

## Source-of-truth map

| File | Purpose | Authoritative for |
|---|---|---|
| `BRIEF.md` | Intent, constraints, outcomes, and non-goals | What the task must achieve |
| `PLAN.md` | Human-readable design and execution rationale | Why and how the work is organized |
| `PLAN.graph.yaml` | Validated executable work-package graph | Package IDs, dependencies, requirements, acceptance criteria, and verification profiles |
| `TASK.yaml` | Repository/lifecycle manifest | Project, branches, repositories, mutability, and task lifecycle status |
| `REVIEW.md` | Independent review ledger | Findings and evidence-backed resolutions |
| `HANDOFF.md` | Append-only engineering history | Decisions, completed slices, verification evidence, and historical next actions |
| `RUNTIME_STATUS.md` | Generated local status view | Current orchestration state, package, stage, waiting reason, and progress |

`RUNTIME_STATUS.md` is ignored by Git and generated from the durable local state under
`~/.local/state/execraft`. Do not edit it manually and do not infer current progress from
the latest heading in `HANDOFF.md`.

Refresh the local status view with:

```bash
execraft task sync-status <task-id>
```
