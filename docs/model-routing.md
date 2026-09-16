# Model routing

Model-serving topology is represented independently from runtime selection.
This prevents a scheduler profile, provider alias, or runtime backend from
implicitly owning network placement.

## Model routes

A model route describes the model-facing contract:

- provider family;
- model name;
- optional provider alias;
- endpoint/API family;
- context window;
- default execution target;
- optional credential reference.

Credential references are configuration metadata. Resolved secret values are not
stored in browser/read-model payloads.

## Execution targets

Targets describe placement and shared capacity. Supported target kinds are:

- `local`;
- `inference_endpoint`.

`remote_runtime` remains readable for compatibility but is experimental-disabled
in normal execution.

Target health is tracked separately from runtime and model-route health. The
scheduler uses the normalized target/concurrency group when determining whether
work can run in parallel.

## Compatibility registry

The legacy `opencode/providers.yaml` endpoint format remains readable and is
projected into the canonical model-route/target model. New configuration should
prefer schema-v4 `model_routes` and `execution_targets` in `agents.yaml`.

## OpenClaw

Managed OpenClaw configuration is generated from the same canonical model routes
and targets used by Native/OpenCode execution. Projection never creates a new
source of truth for provider/model placement.

See [`runtime-configuration.md`](runtime-configuration.md) and
[`openclaw-model-projection.md`](openclaw-model-projection.md).
