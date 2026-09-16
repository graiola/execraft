# Runtime execution

`Execraft` separates orchestration policy from execution backends. The scheduler
selects an agent profile; the selected runtime executes a normalized request.

## Contract

`RuntimeExecutionRequest` carries the durable execution context needed by a
runtime, including capability/role, repository workspace, prompt/handoff data,
execution identity, and optional continuation state.

`RuntimeExecutionResult` carries runtime output, usage/telemetry, failure
classification, and an optional opaque session reference. The orchestrator owns
all state transitions after the runtime returns.

## Execution identity

Every attempt has a normalized identity with the dimensions needed for
scheduling, diagnostics, and health isolation:

- candidate/profile;
- runtime;
- model route/provider/model when configured;
- execution target;
- concurrency group.

Do not infer physical placement from provider aliases. Model routes and targets
are the canonical topology.

## Native runtime

Native adapters run the configured CLI backend inside the task workspace. They
continue to own backend-specific command construction, process interaction,
structured-output capture, and optional live-session behavior. Process/PTY
supervision itself is owned by `execraft.process`.

## OpenClaw runtime

`OpenClawAgentRuntime` uses the public Gateway contract. It supports cold
execution and controlled package/role continuation. Continuation is an
optimization only: if the exact session cannot be proven compatible, Execraft
reconstructs from durable handoff/task/workspace state.

Workflow skills may be projected into Execraft-owned runtime state. Context/cache
optimization can compact a compatible session, but a post-compaction guard must
prove it remains reusable.

## Failure ownership

Transport, runtime, model-route, target, provider, structured-output, and
workflow failures remain distinct. Failure translation produces scheduler-level
categories without moving scheduler types into lower-level agent modules.

## Compatibility

Schema v1-v3 execution configuration remains readable/migratable. Schema v4 is
the canonical runtime/profile/model-route/target representation. Legacy provider
names remain compatibility aliases where documented; new code should use agent
profile terminology.

See [`runtime-configuration.md`](runtime-configuration.md),
[`model-routing.md`](model-routing.md), and
[`supported-runtime-architecture.md`](supported-runtime-architecture.md).
