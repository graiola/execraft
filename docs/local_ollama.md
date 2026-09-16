# Local Ollama

Ollama can be configured as a runtime-neutral model route backed by a local
execution target. The same model route can be consumed by Native/OpenCode or by a
managed OpenClaw runtime.

## Example

```yaml
execution_targets:
  local-ollama:
    kind: local
    endpoint: http://127.0.0.1:11434/v1
    concurrency_group: local-ollama

model_routes:
  local-qwen:
    provider: ollama
    model: qwen3-coder:30b-32k
    endpoint: http://127.0.0.1:11434/v1
    api_family: openai-compatible
    context_window: 32768
    default_target: local-ollama
```

A Native/OpenCode agent profile references the route and target normally. An
OpenClaw profile may reference the same route; managed OpenClaw configuration is
then projected from the canonical route.

## Health and capacity

Target health and concurrency are tracked independently from model-provider and
runtime health. This prevents a failed local endpoint from disabling unrelated
routes/runtimes.

## Environment overrides

Projects may use environment-backed endpoint configuration where supported by the
legacy OpenCode registry. New schema-v4 configuration should keep endpoint
ownership in the model route/target definitions.

See [`model-routing.md`](model-routing.md) and
[`openclaw-model-projection.md`](openclaw-model-projection.md).
