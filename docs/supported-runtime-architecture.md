# Supported runtime architecture

This document defines the supported runtime boundary for `Execraft` releases.

## Supported execution model

The scheduler selects an **agent profile**. A profile names:

- a runtime;
- optional model route;
- optional execution target;
- capabilities, priority, complexity limits, and concurrency policy.

The runtime executes a normalized `RuntimeExecutionRequest` and returns a
`RuntimeExecutionResult`. Orchestration remains authoritative for workflow state,
verification, review, retries, commits, and recovery.

## Supported runtimes

### Native

Native runtimes are the default execution path. Built-in adapters include Codex,
Claude Code, Antigravity CLI, and OpenCode. Registered third-party runtime kinds
may use the documented runtime registry seam.

### OpenClaw

OpenClaw is optional and is installed through the `openclaw` extra. Supported
OpenClaw execution uses the public Gateway API with either:

- a local target; or
- a remote inference endpoint while the runtime itself remains local/self-hosted.

Validated compatibility:

- OpenClaw release: **2026.7.1-2**
- Gateway protocol: **4**

Managed and external Gateway modes are supported. Managed mode owns the Gateway
process/config lifecycle; external mode connects to operator-owned infrastructure.

## Experimental-disabled definitions

The configuration reader retains compatibility for two definitions that are not
part of normal release execution:

- OpenClaw specialist/sub-agent delegation;
- `remote_runtime` full-runtime placement.

They remain readable for configuration compatibility but fail closed when normal
execution attempts to use them. Remote **inference endpoints** are supported and
are distinct from remote full-runtime placement.

## Security boundary

Runtime output is evidence, not control-plane authority. Runtime code cannot:

- mutate orchestration state directly;
- bypass repository scope policy;
- approve its own verification/review result;
- own task lifecycle or commits;
- expose credential values through GUI/read-model payloads.

Managed OpenClaw runs are constrained by the runtime security policy and isolated
execution root. See [`openclaw-security.md`](openclaw-security.md).

## Packaging

Native execution has no OpenClaw dependency. OpenClaw dependencies are optional:

```bash
pip install 'execraft[openclaw]'
```

See [`runtime-configuration.md`](runtime-configuration.md),
[`runtime-execution.md`](runtime-execution.md), and
[`adding-a-runtime.md`](adding-a-runtime.md).
