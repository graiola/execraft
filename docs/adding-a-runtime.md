# Adding a runtime

Third-party runtimes integrate through the runtime registry. Add only the pieces
required to build and execute the runtime; do not create a parallel scheduler or
configuration model.

## Required integration

A runtime registration provides:

1. a stable runtime kind;
2. configuration parsing/validation;
3. a builder that returns an `AgentRuntime` implementation;
4. capability information that truthfully describes runtime behavior.

The runtime receives normalized execution requests and returns normalized results.
It does not own task state, commits, verification, review, or scheduling policy.

## Security

If a runtime claims hard sandboxing, the implementation must enforce it. Do not
map an unsupported security mode to a weaker mode silently. Credential material
must stay outside browser/read-model payloads and generated files unless the
runtime contract explicitly requires a protected reference.

## Configuration

Use the existing schema-v4 `runtimes`, `model_routes`, `execution_targets`, and
`agents` sections. Do not add a second provider/target registry for the runtime.
Model-serving topology remains runtime-neutral.

## Tests

A new runtime should cover:

- configuration validation;
- registration/building;
- normalized request/result behavior;
- failure mapping;
- cancellation/process cleanup where applicable;
- claimed security/capability behavior;
- scheduler selection through a normal agent profile.

The shared extension tests in `tests/test_runtime_extension.py` remain a useful
example even though the release contract is defined by the runtime registry, not
by internal implementation labels.
