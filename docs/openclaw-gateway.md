# OpenClaw Gateway

OpenClaw is an optional Execraft runtime accessed through the public Gateway API.
The Gateway is a transport/runtime boundary; it does not own Execraft workflow
state, project configuration, commits, or verification.

## Compatibility

Execraft validates against OpenClaw release **2026.7.1-2** and Gateway protocol
**4**. Version-policy checks fail closed when the connected runtime is outside the
configured compatibility policy.

## Modes

### Managed

Execraft owns the Gateway child process and generated runtime configuration. Managed
state lives under Execraft state/config directories, not inside a product repository.
The child receives a restricted environment and an isolated runtime HOME.

### External

The operator owns Gateway lifecycle and configuration. Execraft only connects to the
configured public endpoint and never rewrites the external config file.

## Authentication

Supported authentication kinds are `none`, `token`, and `password` where the
runtime configuration permits them. Secret references may point to environment
variables; resolved values are never emitted through GUI/read-model payloads.

## Protocol use

The client uses documented public Gateway methods for execution, waiting,
session description/continuation, model discovery where available, cancellation,
and bounded diagnostics. Execraft does not read OpenClaw private databases or
transcripts to infer task completion.

## Model configuration

For managed mode, canonical Execraft model routes and execution targets are
projected into generated OpenClaw configuration. See
[`openclaw-model-projection.md`](openclaw-model-projection.md).

## Failure behavior

Transport/protocol/authentication failures are classified distinctly and returned
to orchestration. They never silently become successful execution results.

See [`openclaw-runtime-execution.md`](openclaw-runtime-execution.md) and
[`openclaw-security.md`](openclaw-security.md).
