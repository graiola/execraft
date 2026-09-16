# OpenClaw context optimization

Context optimization reduces repeated prompt/runtime context without changing the
authoritative Execraft state model.

## Principles

- Session reuse is optional and disposable.
- Durable task/workspace/structured-handoff state always wins.
- Compaction is bounded by configured thresholds.
- A compacted session must pass an authoritative compatibility guard before reuse.
- Missing telemetry is reported as unknown rather than fabricated as zero usage.

## Optimization mechanisms

Managed OpenClaw execution can combine:

- compatible persistent sessions;
- delta handoffs;
- projected workflow skills;
- public session telemetry;
- bounded session compaction;
- cold reconstruction when any reuse assumption fails.

## Configuration

Optimization policy is runtime-specific schema-v4 configuration. Defaults are
conservative; an operator can disable continuation/compaction without changing
workflow semantics.

## Diagnostics

Execraft records provider/runtime usage where available and reports cache/context
behavior through invocation telemetry. Normal diagnostics do not require storing
full prompts or private runtime transcripts.

See [`openclaw-session-continuation.md`](openclaw-session-continuation.md) and
[`context-budgeting.md`](context-budgeting.md).
