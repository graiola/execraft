# Inference endpoint nodes

Execraft can route model inference to remote inference endpoints while keeping the
execution runtime and task workspace local. This is different from running the
entire agent runtime on a remote node.

## Configuration

Represent each inference node as an `execution_target` with kind
`inference_endpoint` and give it a stable concurrency group. Model routes then
reference the endpoint and target.

```yaml
execution_targets:
  gpu-node-a:
    kind: inference_endpoint
    endpoint: http://192.0.2.10:11434/v1
    concurrency_group: gpu-node-a
```

Use environment-backed endpoint overrides when host addresses differ between
installations; do not check private infrastructure addresses into project-neutral
configuration.

## Scheduling

The scheduler uses target/concurrency identity to avoid overcommitting shared
hardware. Target health is independent from runtime/model-route health.

## OpenClaw

A local managed OpenClaw runtime may use a remote inference endpoint because only
model serving is remote. Full remote-runtime placement is a separate,
experimental-disabled definition.

See [`satellite_ubuntu.md`](satellite_ubuntu.md),
[`satellite_windows.md`](satellite_windows.md), and
[`model-routing.md`](model-routing.md).
