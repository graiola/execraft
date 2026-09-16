# OpenClaw runtime execution

`OpenClawAgentRuntime` is a peer runtime implementation behind the same normalized
execution contract used by Native backends. It communicates only through the
public OpenClaw Gateway API.

## Execution flow

1. The scheduler selects an OpenClaw-backed agent profile.
2. Execraft resolves the profile's runtime, model route, and execution target.
3. Managed mode ensures the Gateway/configuration is ready; external mode verifies
   connectivity to the operator-owned Gateway.
4. The runtime submits the turn and waits through the public Gateway contract.
5. Output, usage, failure information, and optional session metadata return to the
   orchestrator.
6. The orchestrator performs verification/review/state transitions independently.

## Session continuation

A compatible package/role session may be continued using an opaque
`RuntimeSessionRef`. Reuse is allowed only when the stored binding matches the
current runtime/model/target/workspace/context. Otherwise Execraft cold-reconstructs
from durable state.

## Workflow skills and context

Selected workflow skills may be projected into Execraft-owned managed runtime state.
Continuation can use delta handoffs and bounded compaction to reduce repeated
context. These optimizations never replace the complete durable handoff.

## Workspace boundary

Managed execution receives an isolated task execution root. Execraft does not seed
OpenClaw bootstrap state into a product repository. Remote inference endpoints are
supported while the runtime remains local/self-hosted; full remote-runtime
placement is experimental-disabled.

## Cancellation and failures

Cancellation uses the runtime/Gateway cancellation surface where available and
still performs local process/state cleanup. Transport, runtime, route, target,
structured-output, and workflow failures remain separately classified.

## Security

Managed execution projects role-specific sandbox/tool policy, protects credential
values, restricts inherited environment, and fails closed when hard constraints
cannot be proven. See [`openclaw-security.md`](openclaw-security.md).

See also [`openclaw-gateway.md`](openclaw-gateway.md),
[`openclaw-session-continuation.md`](openclaw-session-continuation.md), and
[`openclaw-skill-bridge.md`](openclaw-skill-bridge.md).
