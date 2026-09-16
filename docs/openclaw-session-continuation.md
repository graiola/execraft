# OpenClaw session continuation

OpenClaw sessions are disposable optimization state. Durable Execraft task state,
repository contents, plans, and structured handoffs remain authoritative.

## Binding

A reusable session is bound to the execution context that created it, including
project/task/package role, runtime/model identity, workspace root, and context
fingerprints. A continuation attempt must prove the stored session matches the
current execution context.

## Invalidation

Execraft cold-reconstructs instead of continuing when compatibility cannot be
proven, including cases such as:

- missing or unreachable session;
- changed model/runtime/target identity;
- changed workspace/context epoch;
- changed selected workflow-skill content;
- unsafe or stale runtime metadata.

## Continuation payload

A compatible continuation sends only the bounded delta needed for the next turn.
The complete durable handoff remains available for cold reconstruction.

## Parallel work

Disposable parallel shard sessions are not promoted into long-lived authoritative
state. Reuse remains scoped to contexts where the binding can be proven exact.

## Telemetry

Invocation/runtime telemetry records whether a session was reused or
cold-reconstructed without making session state part of orchestration truth.

See [`openclaw-runtime-execution.md`](openclaw-runtime-execution.md) and
[`openclaw-context-optimization.md`](openclaw-context-optimization.md).
