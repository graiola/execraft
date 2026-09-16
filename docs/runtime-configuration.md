# Runtime configuration

Schema v4 is the canonical execution configuration for new projects. It separates
runtime backend, model route, execution target, and scheduler profile.

```yaml
schema_version: 4
runtimes:
  native-opencode:
    kind: native
    adapter: opencode
    binary: opencode

execution_targets:
  local-ollama:
    kind: local
    endpoint: http://127.0.0.1:11434/v1
    concurrency_group: local-ollama

model_routes:
  qwen-local:
    provider: ollama
    model: qwen3-coder:30b-32k
    endpoint: http://127.0.0.1:11434/v1
    api_family: openai-compatible
    default_target: local-ollama

agents:
  local-coder:
    runtime: native-opencode
    model_route: qwen-local
    target: local-ollama
    capabilities: [implement, review]
    priority: 100
```

## Runtimes

A runtime entry identifies an execution backend. Built-in kinds are `native` and
`openclaw`; registered extensions may add runtime kinds through the runtime
registry. Runtime-specific configuration stays under the runtime entry.

## Model routes and targets

Model routes own provider/model/API metadata. Execution targets own placement and
capacity metadata. Do not duplicate endpoint/target ownership on agent profiles
except through documented compatibility fields.

Supported target kinds are `local` and `inference_endpoint`. The historical
`remote_runtime` value remains readable but is experimental-disabled in normal
execution.

## Agent profiles

Agent profiles are scheduler identities. They declare capabilities, priority,
complexity limits, concurrency policy, runtime, and optional model route/target.
Profiles may also contain backend policy such as timeouts, sandbox mode, live
sessions, and capability-specific worker selection.

## OpenClaw fields

An OpenClaw runtime can declare managed or external Gateway ownership, Gateway
URL, version policy, authentication kind/reference, executable, and bounded
optimization/security settings. Managed model/provider configuration is projected
from canonical model routes and targets; resolved credentials are never persisted
into generated configuration.

## Compatibility

Schema v1-v3 remain readable and migratable. Compatibility projection is
one-directional: old configuration is normalized into the canonical model; new
code should not create additional legacy provider-only state.

Normal release execution supports Native and optional OpenClaw with local or
inference-endpoint targets. OpenClaw sub-agent delegation and full remote-runtime
placement remain experimental-disabled definitions.

See [`compatibility-ledger.md`](compatibility-ledger.md) for retained legacy
surfaces and [`supported-runtime-architecture.md`](supported-runtime-architecture.md)
for the support boundary.
