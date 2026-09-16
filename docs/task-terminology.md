# Task terminology

Execraft uses one vocabulary for Task execution and a separate vocabulary for
Project Execution.

| Domain | Canonical terms | Meaning |
| --- | --- | --- |
| Project | ProjectPhase, ProjectGate, ProjectMilestone | organization, boundary decisions, achieved baselines |
| Task | Task, TaskExecution, WorkPackage, Stage | executable software work |
| Task control | Check, Hold, Verification, Attempt | Task-internal validation and control |

Do not call a Work Package a Milestone. Do not introduce a new Task “Gate” when
the concept is a scope/review/acceptance/commit check or an operator/entry hold.

Compatibility is now strictly bounded to historical input readers. Historical
`milestone_*` and retired task-`*_gate_*` journal events are projected to
canonical Work Package/Check event names for current surfaces without rewriting
the immutable journal. A historical `milestone-directives.json` queue is read
once, validated, atomically migrated to the single writable
`work-package-directives.json` queue, and retired. The historical repository-sync
`refresh_before_gate` key remains accepted only by the repository-sync policy
reader and is always serialized as `refresh_before_check`. Legacy Task PLAN
`Milestone` headings remain readable and produce an explicit normalization
warning; every current PLAN writer emits `Work Package`.

No `ProjectState`, `MilestoneDirective*`, old Task milestone GUI module, legacy
Task milestone API, or compatibility writer remains in canonical source. The
architecture checker enforces these boundaries so retired terminology cannot
spread back into active Task code.
