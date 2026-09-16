# Temporary agent-profile promotions

Temporary agent-profile promotions are an operator-controlled escape hatch for an
execution-agent outage, quota incident, or other temporary availability loss. They
raise one configured agent profile's absolute complexity ceiling for a bounded time
**without changing capability weight or editing `agents.yaml`**.

The filename and persisted event/state names still use `provider` for compatibility
with the legacy provider-oriented API. In current product terminology, the promoted
scheduler identity is an **agent profile**.

Use a promotion when a package is blocked because every normally eligible agent is
unavailable and a healthy secondary profile is excluded only by its configured
complexity ceiling. Promotions are task-scoped, persisted in task runtime state,
hot-reloaded by an active orchestrator, and expire automatically.

## Safety model

A promotion changes only effective `max_complexity` for one agent profile and
capability. All other scheduler policy remains in force, including:

- agent/runtime/model/target availability and health;
- capabilities and binding-role pools;
- reviewer independence;
- weights and deterministic rotation;
- runtime support policy and target admission.

`fallback_only=true` is the default and recommended mode. The scheduler first
tries the normal static agent pool. Only when that pool has no eligible candidate
does it consider fallback-only promotions. An explicitly immediate promotion
(`--no-fallback-only`) participates in the first selection pass but retains the
profile's configured capability weight.

A `review` promotion does **not** apply to the `final_review` quality check unless
`allow_final_review` is explicitly enabled. Promotions have a maximum lifetime of
seven days; prefer the shortest interval that covers the known incident.

## GUI

From a task's **Run** view:

1. open **Execution health → Details**;
2. expand **Advanced profile maintenance**;
3. locate the affected agent profile;
4. choose **Promote** (or **Promotion…** when one is already active).

The promotion dialog lets the operator choose capabilities, temporary complexity
ceiling, bounded duration, task-wide or package scope, fallback-only/immediate
selection, optional final-review coverage, and an audit reason.

The promotion control remains available while the orchestrator is running. This
is intentional: the promotion is a hot scheduler-policy update intended to unblock
`waiting_for_agent`. Native health mutation controls have stricter concurrency
rules and remain disabled while the relevant run/assignment is active.

The Advanced profile card displays both configured and effective ceilings, for
example `review ≤75 →100`, plus expiry and fallback mode. Healthy raw profile cards
remain outside the normal Run surface by design.

## CLI

Create a four-hour fallback-only review promotion for one task:

```bash
execraft agents promote \
  --project sample \
  --task-id feature_auth \
  --agent local-coder \
  --capability review \
  --max-complexity 100 \
  --for 4h \
  --reason "primary reviewers temporarily unavailable"
```

List active promotions:

```bash
execraft agents promotions \
  --project sample \
  --task-id feature_auth
```

Revoke a review promotion:

```bash
execraft agents revoke-promotion \
  --project sample \
  --task-id feature_auth \
  --agent local-coder \
  --capability review
```

Useful optional flags:

- `--package-id PACKAGE` — restrict the override to one package;
- `--no-fallback-only` — make the promoted ceiling participate immediately;
- `--allow-final-review` — opt a review promotion into `final_review`;
- repeated `--capability` — promote several supported capabilities.

If `--capability` is omitted on `promote`, all supported capabilities whose
configured ceiling is below the requested ceiling are promoted.

## Persistence and compatibility names

Promotions are stored in the collision-safe task runtime directory as
`provider-promotions.json`. The compatibility filename is written atomically with
owner-only permissions and is hot-reloaded through a lightweight file-signature
cache.

The task journal retains compatibility event names:

- `provider_promotion_created`;
- `provider_promotion_used` when a package actually requires the raised ceiling;
- `provider_promotion_revoked`.

Creation alone does not imply selection. `provider_promotion_used` records the
package, stage, static/promoted ceilings, task complexity, expiry, and invocation
ID so degraded-policy execution remains attributable.

## Model-provider quota classification

Agent health and promotion policy are complementary. A promotion must not be used
to mask an execution route that is itself unavailable. Existing health
classification can translate provider-specific quota/reset messages into cooldowns
so the candidate pool reflects reality. Here **provider** means the external model
or CLI service that emitted the quota condition, not the scheduler agent-profile
identity.
