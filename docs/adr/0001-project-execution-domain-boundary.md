# ADR 0001 — Project Execution domain boundary

Status: Accepted

## Context

Execraft already has a mature Task orchestrator. A Project needs coordination across
multiple canonical Tasks without creating a second owner for Work Packages,
agents, verification loops, repositories, or Task state.

## Decision

Introduce `execraft.project_execution` as a distinct domain above Tasks. Project
Execution owns ProjectPhase, ProjectGate, ProjectMilestone, project-level Task
dependencies, eligibility, evidence-bound decisions, runtime reconciliation and
project event history. It references canonical Tasks by ID and communicates with
Task execution only through `TaskExecutionPort`.

The Task orchestrator remains the sole owner of TaskExecution, WorkPackage,
WorkPackageStage, agent assignment, review/retry loops, verification execution,
Task repository transactions, and PLAN mutation.

The architecture checker permits only the explicitly documented storage-identity
read adapter (`task_projection.py -> execraft.orchestrate.identity`) across this
boundary. Task control functions are injected as port callbacks.

## Consequences

Project Execution can be tested without Task scheduler internals, Task execution
can evolve independently, and a Project restart cannot directly manipulate a
Work Package. Cross-domain actions use durable intent → Task action → observation
→ durable resolution semantics.
