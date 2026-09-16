# OpenClaw model projection

Managed OpenClaw configuration is derived from Execraft's canonical model routes
and execution targets. Projection is disposable runtime state, not a second source
of truth.

## Inputs

Projection consumes schema-v4 runtime configuration:

- the selected OpenClaw runtime;
- model routes;
- execution targets;
- agent profiles that reference those routes/targets.

## Output

The generated OpenClaw config contains provider/model entries and agent model
selections needed by the managed Gateway. Endpoint and provider-family metadata
remain consistent with the canonical Execraft route.

Credential references can be resolved at runtime, but resolved secret values are
not written to the generated config or browser payloads.

## Placement validation

Projection accepts supported model-serving targets (`local` and
`inference_endpoint`) and fails closed for incompatible placement. A
`remote_runtime` target is not a model-serving endpoint and is experimental-disabled
for normal release execution.

## External Gateway ownership

An explicit external Gateway config path is operator-owned and is never overwritten.
Projection applies only to Execraft-managed runtime configuration.

## Discovery

When the Gateway exposes public model-discovery methods, Execraft may compare the
projected model with the runtime catalog for diagnostics. Discovery is advisory;
actual execution results remain authoritative.

See [`model-routing.md`](model-routing.md),
[`runtime-configuration.md`](runtime-configuration.md), and
[`openclaw-gateway.md`](openclaw-gateway.md).
