# Project Execution

![Project Execution interface](assets/project-execution.png)


Project Execution is Execraft's project-level control domain. It coordinates whole
canonical Tasks while leaving Work Package execution to the existing Task
orchestrator.

## Ownership

```text
Project
├── Roadmap                 planning/view projection
└── Project Execution       executable project control graph
    ├── ProjectPhase
    ├── ProjectGate
    ├── ProjectMilestone
    └── Task references ───────────────┐
                                      ▼
                                    Task
                                      ▼
                                TaskExecution
                                      ▼
                                  WorkPackage
```

The canonical definition is `<project>/PROJECT_EXECUTION.yaml`. Runtime evidence
is stored separately under `<state-root>/project-execution/<project-id>/` as
`state.json` plus `journal.jsonl`.

Task entries contain only project metadata: Phase ownership, `required`, Task
prerequisites, and ProjectGate prerequisites. Task title, repository scope,
status, PLAN graph, Work Packages, verification configuration and agent policy
are never copied into Project Execution.

## Schema v1

```yaml
schema_version: 1
project: sample
revision: 7
mode: assisted
policy:
  maximum_parallel_tasks: 2
  maximum_parallel_tasks_per_phase: 1
  maximum_active_phases: 1
  task_failure_behavior: stop_new

phases:
  - id: foundation
    title: Foundation
    entry_gates: []
    exit_gates: [foundation-accepted]
    tasks: [backend-api, frontend-ui]
    milestones: [foundation-mvp]

tasks:
  backend-api:
    phase: foundation
    required: true
    requires: {tasks: [], gates: []}
  frontend-ui:
    phase: foundation
    required: true
    requires: {tasks: [backend-api], gates: []}

gates:
  - id: foundation-accepted
    title: Foundation Accepted
    criteria:
      all:
        - {type: task_completion, task_id: backend-api}
        - {type: task_completion, task_id: frontend-ui}

milestones:
  - id: foundation-mvp
    title: Foundation MVP
    requires:
      tasks: [backend-api, frontend-ui]
      gates: [foundation-accepted]
      milestones: []
    delivery: {policy: candidate}
```

The repository validates the complete graph before an optimistic-revision
atomic write. Missing references, duplicate/global asset ID collisions, Task,
Gate and Milestone cycles, inconsistent Phase ownership, invalid Phase entry
boundaries, unsupported evaluator types and attempts to make a Task depend on a
Milestone are rejected.

## Execution modes

`observe` evaluates and persists project state but cannot start Tasks.
`assisted` additionally permits an explicit operator action to start an eligible
Task. `automatic` is opt-in and starts only whole canonical Tasks through the
`TaskExecutionPort`; it never enters Work Package scheduling.

Automatic execution is intentionally split into two operations:

- `reconcile()` is observational in every mode, so status/GET requests can never
  start work as a side effect; and
- `automatic_cycle()` is the explicit side-effecting scheduler cycle. A foreground
  runner may repeat durable cycles until the project completes, enters a Hold, or
  reaches a control boundary such as a human Gate.

The v1 policy is bounded at Project/Task level only:

- `maximum_parallel_tasks` is the global running/reserved Task cap;
- `maximum_parallel_tasks_per_phase` limits concurrent Tasks within one Phase;
- `maximum_active_phases` limits how many Phases may have execution in flight; and
- `task_failure_behavior` is `stop_new`, `hold`, or `continue`.

`stop_new` blocks new Task starts after any required Task failure but does not
pause work already running. `hold` additionally places Project Execution on a
durable project Hold, again without reaching into running Tasks. `continue` allows
unrelated eligible Tasks to progress; ordinary Task prerequisites still block
dependents of the failed Task. For migration, readers accept the P10/P11
`stop_on_task_failure` boolean, but canonical writers emit only the new policy.

## Project Execution workspace

The Project workspace has three separate operator surfaces: **Roadmap** for
planning/layout, **Execution** for canonical project control, and **Tasks** for
canonical Task dossiers. The Execution view is backed only by
`/api/project-execution/...` routes and never manipulates Work Packages.

The Execution workspace shows:

- execution mode and project hold state;
- deterministic Phase lifecycle and health;
- ProjectGate state, evidence fingerprint, typed criteria, evaluation history,
  human decisions and waivers;
- ProjectMilestone requirements, schedule health and immutable achievement
  baseline;
- ready/blocked Tasks with structured eligibility reasons; and
- aggregated project-level attention for failed/decision-required Gates, held
  execution, failed Tasks, unhealthy Phases and late Milestones.

Operators may initialize an unconfigured project, edit canonical assets, assign
Task project metadata, make human Gate decisions, explicitly waive Gates, start
eligible Tasks in Assisted mode, and configure/run explicit Automatic cycles. Gate criteria remain typed (`task_completion`,
`task_verification`, `task_artifact`, `project_gate`, `human_approval`) and are
combined with `ALL`; the GUI does not introduce a second expression language.

Assisted and Automatic Task starts cross the anti-corruption boundary through
`TaskExecutionPort.start()`. In the GUI adapter that action delegates to the same
Task dashboard `start_run()` lifecycle used by the normal Run action, including
first-run initialization. Project-started driver dashboards are retained in a
separate pool from the operator-selected Task view, so bounded parallel Automatic
execution does not change browser focus or terminate another launched Task. The
Project Execution engine records/reconciles its start intent; it does not select a
Work Package, agent, review loop or verification stage.

Control actions that can change runtime execution (initialize, mode/policy changes,
Automatic cycle, hold/resume, Task start, Gate decision, waiver) require explicit
browser acknowledgement. Merely loading or refreshing Project Execution remains
side-effect free. Definition mutations use `expected_revision`.
## Delivery extension

Project Execution ends its Milestone responsibility at immutable baseline
achievement. P13 adds a separate provider-neutral Delivery layer that may derive
a deterministic candidate from an achieved `delivery.policy: candidate`
baseline and hand that candidate to a `DeliveryProvider` adapter.

Provider credentials, registry/deployment configuration and vendor-specific
destination semantics are not stored in `PROJECT_EXECUTION.yaml`. Delivery
operation intents/results are durable in `delivery.json`, alongside but separate
from `state.json` and the Project journal. See
[`project-delivery.md`](project-delivery.md).
