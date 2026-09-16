# Project Phases

A `ProjectPhase` groups a coherent stage of project execution. It owns project
entry/exit boundaries and references canonical Tasks and ProjectMilestones; it
does not execute code.

Phase lifecycle is a deterministic projection:

```text
PLANNED --entry Gates satisfied--> READY --work starts--> ACTIVE
ACTIVE --completion contract satisfied--> COMPLETE
```

Explicit cancellation is the persisted lifecycle override. Health is separate:
`ON_TRACK`, `AT_RISK`, `BLOCKED`, or `LATE`, so `ACTIVE + BLOCKED` is valid.

A Phase becomes complete only when all required Tasks have completed, all exit
Gates currently authorize crossing the boundary, and all required Phase
Milestones are achieved. Optional Tasks do not block completion. A Phase entry
Gate is rejected if its own evidence depends on a Task inside that same Phase,
which prevents a self-blocking boundary.
