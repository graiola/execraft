# Project Gates

A `ProjectGate` decides whether execution may cross a project boundary. It never
modifies code and never schedules Work Packages.

Schema v1 uses a typed `criteria.all` composite. Supported evaluators are Task
completion, Task verification, Task artifact, ProjectGate reference, and human
approval. An unrestricted expression language is intentionally absent.

Every evaluation computes a canonical SHA-256 fingerprint from the Gate
criterion definition and the exact automatic evidence consumed. Human approval,
rejection, and waiver records bind to that fingerprint. If relevant evidence or
the Gate control contract changes, the old authorization no longer satisfies
the current Gate.

Runtime states are `WAITING`, `READY`, `EVALUATING`, `AWAITING_DECISION`,
`PASSED`, `FAILED`, `WAIVED`, and `CANCELLED`. `WAIVED` is never rewritten as
`PASSED`; eligibility accepts either only when its fingerprint is current.
Decision and waiver records require an actor, and waivers additionally require a
reason.

## Automatic-mode boundary

Human criteria are a hard Automatic execution boundary. Automatic cycles may
evaluate a Gate into `AWAITING_DECISION`, but they never approve, reject, or waive
it. Only an explicit operator action can create that decision record, and the
record authorizes downstream execution only while its evidence fingerprint remains
current.
