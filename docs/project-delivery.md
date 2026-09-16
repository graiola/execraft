# Project Delivery

Project Delivery is a provider-neutral extension downstream of Project
Milestones. It does **not** change the meaning of `ProjectMilestone` and it does
not introduce deployment-provider knowledge into `PROJECT_EXECUTION.yaml`.

The ownership split is:

```text
ProjectMilestone
    │
    │ freezes an immutable reproducible baseline
    ▼
DeliveryCandidate
    │
    │ provider-neutral request
    ▼
DeliveryProvider
    │
    │ materializes/distributes through an external system
    ▼
DeliveryResult
```

A Milestone still records historical capability achievement. The Delivery layer
only acts on a baseline that has already been achieved with:

```yaml
delivery:
  policy: candidate
```

`delivery.policy: none` means the achievement is not eligible to become a
Delivery candidate.

## Provider independence

`ProjectMilestone` contains no provider, registry, environment, CI/CD, Docker,
or external destination fields. Its delivery contract remains only:

```text
none | candidate
```

A `DeliveryTarget` contains a stable logical target ID plus an optional display
label. It intentionally does not persist credentials, provider URLs, registry
configuration, deployment manifests, or vendor-specific options. A concrete
`DeliveryProvider` adapter resolves the logical target through its own external
configuration.

This keeps the canonical Project Execution graph portable and prevents secrets
or provider implementation details from leaking into Milestone history.

## Delivery candidates

`DeliveryService.prepare_candidate(milestone_id)` derives a
`DeliveryCandidate` from the immutable achievement record stored by Project
Execution.

Candidate identity is deterministic from:

```text
Project Milestone identity + canonical baseline SHA-256
```

The candidate stores the complete baseline as canonical JSON and records its
SHA-256 digest. The in-memory `baseline` property returns a fresh decoded copy,
so a provider cannot mutate the candidate snapshot by modifying a Python
mapping.
Candidate validation also rejects provider-specific keys in the baseline
`delivery` section; the only accepted historical delivery metadata is the
canonical `policy` value.

Creation is idempotent. Repeated preparation of the same immutable baseline
returns the same persisted candidate and emits only one
`project_delivery_candidate_created` event.

The **achievement baseline**, not the current editable Milestone definition, is
the historical authority for whether that exact snapshot had `candidate`
delivery policy. Changing the current definition later therefore does not
silently rewrite the delivery semantics of a previously achieved Milestone.

## Provider port

External integrations implement:

```python
class DeliveryProvider(Protocol):
    @property
    def provider_id(self) -> str: ...

    def deliver(self, request: DeliveryRequest) -> DeliveryResult: ...

    def reconcile(self, request: DeliveryRequest) -> DeliveryResult | None: ...
```

`DeliveryRequest.operation_id` is durable and should be used as an idempotency
key when the external system supports one.

`reconcile()` is observational. It must inspect a previously requested operation
without creating another external side effect. `None` means the provider cannot
currently establish the result.

No concrete GitHub, registry, CI/CD, Docker, cloud, or deployment adapter is
part of P13.

## Crash-safe delivery operation

Delivery follows the same intent-before-side-effect rule used by Project Task
startup:

```text
persist delivery operation intent
        ↓
call DeliveryProvider.deliver(operation_id, candidate, target)
        ↓
observe provider result
        ↓
persist terminal result
```

Delivery runtime state is stored separately at:

```text
<state-root>/project-execution/<project-id>/delivery.json
```

The repository uses atomic JSON publication and the shared `RECORD` lock for
short state transactions. The external provider call never executes while that
filesystem lock is held.

If the process or provider fails after the external request may have been
submitted, the operation becomes `UNCERTAIN`. The service will **not** blindly
invoke `deliver()` again. A later call reconciles the original durable
`operation_id` through the original provider adapter first.

Operation states are:

```text
PENDING
SUCCEEDED
FAILED
UNCERTAIN
```

`SUCCEEDED` and `FAILED` records are immutable.

A successful attempt is idempotently reused for the same candidate, logical
target, and provider. A failed terminal result is also reused unless the caller
explicitly asks for a new failed-attempt retry. Unresolved operations always
block blind redispatch.

## Audit events

Project Delivery adds Project-domain audit events:

```text
project_delivery_candidate_created
project_delivery_started
project_delivery_succeeded
project_delivery_failed
project_delivery_uncertain
```

These events contain stable candidate/operation/target/provider identities and
provider-neutral external references. They do not duplicate Task/Work Package
journal semantics.

## Future providers

A provider implementation should live outside the Project Milestone model and
outside canonical Project Execution serialization. It may resolve a logical
`DeliveryTarget` to, for example, an artifact repository, release channel,
deployment environment, or other distribution destination.

Future `deploy` Milestone policy, if introduced, should remain a policy signal;
it should not turn a `ProjectMilestone` into a Docker/GitHub/cloud object. The
Delivery layer remains responsible for materializing and distributing the
immutable baseline.
