# Work Package and shard execution policy

`Execraft` stores an execution policy on each Work Package or generated shard. The
policy lets an operator influence **which eligible agent is tried first** and
**which workflow skills are embedded into the handoff** for each orchestration
role. It does not bypass agent/runtime/model/target health, task complexity, reviewer independence,
repository scope, verification, or concurrency controls.

## Roles and defaults

The role registry is defined once in
`src/execraft/orchestrate/execution_policy.py` and is exposed to the CLI and GUI.
The current roles are:

| Role | Required capability | Default workflow skill | Read-only |
|---|---|---|---|
| `decompose` | `decompose` | `ai-plan` | yes |
| `implement` | `implement` | `ai-implement` | no |
| `review` | `review` | `ai-review` | yes |
| `fix_review` | `fix_review` | `ai-fix-review` | no |
| `final_review` | `review` | `ai-review` | yes |

Agent preferences are ranked soft hints unless a role is explicitly marked
binding by **Force** routing. Skill selections are ordered process instructions
embedded into the runtime-neutral `StructuredHandoff` before the task contract. An absent or empty skill selection uses the role defaults above.
Selecting one or more skills replaces the defaults for that role.

## GUI workflow

1. Open **Run**, select the Work Package or shard, and open the inspector's
   **Execution** tab. Graph mode exposes **Execution** directly on eligible cards.
2. Choose the role and one of the existing routing policies:
   - **Automatic** clears that role's explicit agent preference;
   - **Prefer** ranks profiles matching the selected Execution Lane before other
     compatible candidates;
   - **Force** persists the same matching profile pool as binding for that role.
3. For **Prefer** or **Force**, choose an **Execution Lane**. A lane is a
   presentation-only grouping of profiles sharing runtime + model route + target;
   the backend translates it through the existing selection API into canonical
   profile preferences.
4. Enable **Apply routing to direct child shards** when the parent choice should
   be propagated to existing direct shards.
5. Preview the change, then apply it at the next invocation boundary. A live
   invocation is never hot-migrated; a dashboard-owned run may expose
   **Cancel & switch**, while an external driver remains authoritative.
6. Open **Advanced profile and skill policy** only when raw profile ordering or
   role-specific skill selection is required. Saving this raw policy remains
   disabled while orchestration is active.

The GUI obtains role metadata, lane projections, and skill compatibility from the
backend snapshot; it does not maintain a second scheduling model. Lane IDs are
never persisted. Future generated shards continue to inherit the parent policy
automatically.

## CLI workflow

Configure both agents and skills for one role:

```bash
execraft orchestrate policy \
  --project sample \
  --task-id my_task \
  --package-id implementation \
  --role review \
  --agent local-coder \
  --agent cloud-reviewer \
  --skill ai-review \
  --apply-to-shards
```

Restore automatic agent ordering while leaving skill selection unchanged:

```bash
execraft orchestrate policy \
  --project sample \
  --task-id my_task \
  --package-id implementation \
  --role review \
  --clear-agents
```

Restore canonical role skills while leaving agent order unchanged:

```bash
execraft orchestrate policy \
  --project sample \
  --task-id my_task \
  --package-id implementation \
  --role review \
  --clear-skills
```

`execraft orchestrate prefer` remains as a backward-compatible agent-only command.
New automation and operator workflows should use `orchestrate policy`.

## Agent selection semantics

The scheduler first removes candidates that fail any authoritative condition:

- agent/runtime/model/target health and cooldown;
- required capability;
- package complexity ceiling and capability weight policy;
- current-attempt exclusions;
- reviewer independence;
- resource and concurrency-group constraints;
- parallel-wave repository/conflict constraints.

It then moves eligible preferred agent IDs to the front in declared order. A
preferred agent that is disabled, unavailable, over its complexity ceiling, or
otherwise unsafe is skipped immediately. The remaining eligible ring is retained
as fallback, so a preference cannot pin a Work Package to an unavailable agent.

Persisted assignments are revalidated immediately before execution. Changing a
future-stage policy clears only assignments that have not already executed; audit
history and completed attribution are preserved.

When top-tier agent profiles are temporarily unavailable, operators can use a bounded
[temporary agent-profile promotion](provider-promotions.md) to raise only a secondary
profile's absolute complexity ceiling. Promotions do not change capability
weights or static configuration, are fallback-only by default, and are
hot-reloaded by waiting runs.

### Single-agent review fallback

Independent review is preferred, not universally required. Configure:

```yaml
scheduling:
  allow_same_provider_review: true
```

When only one eligible agent profile exists, ordinary packages may reuse it for
primary review, review fixes, and final review. Each relaxation is journaled. If
an independent reviewer becomes available, the scheduler prefers it automatically.

Set the compatibility-named option to `false` to require agent-profile separation. Package-level hard
requirements still take precedence: repository-sync packages with
`require_independent_review: true` never use the same-agent fallback.

## Workflow skill catalog

Skills are Markdown files with YAML front matter:

```markdown
---
name: domain-review
description: Review domain-specific safety invariants.
roles:
  - review
  - final_review
---

Inspect the domain invariants and cite concrete evidence for every conclusion.
```

Built-ins live under:

```text
src/execraft/assets/skills/<skill-id>/SKILL.md
```

Project overlays live under the configured `skills_dir`, normally:

```text
projects/<project-id>/skills/<skill-id>/SKILL.md
```

A project skill with the same ID intentionally overrides the built-in. Legacy
project skills without `roles` remain selectable for every execution role. New
skills should declare explicit roles. Role names are validated, project skill directories and `SKILL.md` files must
not be symlinks, each skill is bounded to 16 KiB of instructions, and one
selected role composition is bounded to 32 KiB before an execution agent is invoked.

Skill instructions are lower priority than the structured task handoff and
repository policy. The prompt explicitly tells the model to treat selected
skills as binding process instructions unless they conflict with those stronger
contracts.

## State and audit

A package stores only explicit selections:

```yaml
agent_preferences:
  review:
    - local-coder
    - cloud-reviewer
skill_preferences:
  review:
    - ai-review
    - domain-review
```

Empty mappings mean automatic agents and canonical default skills. Updates are
persisted in `state.json` and journaled as `execution_policy_updated`, including
the affected parent/shard IDs.

Plan YAML and persisted state fail closed on unknown roles or non-list values;
invalid policy data is no longer silently discarded.

## Legacy schema-v3 reusable profiles

Current schema-v4 configuration is documented in `runtime-configuration.md`. During
the compatibility window, `agents.yaml` schema version 3 supports reusable provider-
named profiles. This keeps
identical Ollama worker policies consistent while preserving a unique compatibility `provider_id`,
model endpoint and concurrency group per logical agent:

```yaml
schema_version: 3
profiles:
  qwen-coder-worker:
    adapter: opencode
    enabled: true
    binary: opencode
    capabilities: [decompose, implement, review, fix_review]
    timeout_seconds: 7200
    inactivity_timeout_seconds: 1200
    output_silence_timeout_seconds: 1200
providers:
  worker_1_coder:
    extends: qwen-coder-worker
    provider_id: worker-1
    model: ollama-worker-1/qwen3-coder:30b
    concurrency_group: worker-1-gpu
  worker_2_coder:
    extends: qwen-coder-worker
    provider_id: worker-2
    model: ollama-worker-2/qwen3-coder:30b
    concurrency_group: worker-2-gpu
  local_coder:
    extends: qwen-coder-worker
    provider_id: local-coder
    model: ollama-local/qwen3-coder:30b
    concurrency_group: local-gpu
```

Profiles may extend another profile. Mapping values are merged recursively;
lists and scalar values replace their parent. Every inheritance graph and every
field that is meaningful in a partial profile is validated even when unused;
concrete compatibility provider entries, including disabled ones, receive full structural validation.
Inheritance cycles and unknown parents fail validation, and string booleans such
as `enabled: "false"` are rejected instead of being interpreted as truthy.

## OpenCode output boundary

OpenCode JSON mode is an event stream. `Execraft` groups text events by
`messageID` and accepts only text correlated with the final `step_finish` as
`final_message`. Intermediate planning/tool narration is no longer concatenated
or reused after a later zero-token terminal step. A terminal step without its
own assistant message is a transport protocol failure, not a misleading
missing-property schema failure. Transport diagnostics retain counts, the final
message ID, and the raw stdout digest without exposing the transcript in
progress logs.

A model that still ignores the required JSON schema fails closed and triggers the
normal bounded failover path. Every review stage uses one canonical model
contract: `verdict`, `findings`, `observations`, and `summary`. The model object
is closed, every property is required for strict Structured Outputs
compatibility, and `observations` is an empty array when there are no
non-blocking notes.

Agent preferences remain soft for normal workflow roles. Explicit
`final_review` and `fix_review` agent lists are different: they are binding
ordered reviewer/fixer pools. If every agent profile in the applicable pool is
unavailable, the package waits instead of silently falling through to an
unlisted check agent.
Execution `ok` belongs to the provider envelope rather than being duplicated in
model JSON.

Prompt-only providers receive at most one same-provider format repair after a
contract-invalid response. That repair clears prior attempt metadata, embeds the
previous response directly, and can be routed through a configured tool-free
provider-native agent such as `ai-contract`. This prevents a formatter from
re-reading artifact paths or repeating an expensive repository pass.

Before schema validation, `Execraft` can normalize lossless provider-neutral
representations of that same contract: a verdict object with `decision`/`reason`,
structured finding objects, common verdict spellings, and an unambiguous common
finding-field alias. Approved findings become non-blocking observations.
`changes_required` without a non-empty finding, contradictory aliases, a missing
decision, or prose without a contract remains invalid. Schema and cross-field
validation both run before the invocation ledger is marked completed, so these
failures consume the bounded agent attempt/failover budget instead of entering a
post-success human escalation.

## Troubleshooting

**A preferred agent is not selected**

Run `execraft agents status --project <project>` and inspect complexity, health,
capabilities, exclusions and concurrency groups. Preferences reorder only the
eligible set.

**A selected skill is missing from the GUI**

Validate its front matter, directory/name match and `roles`. Restart the GUI
after changing the catalog because the catalog is loaded once per server process.

**A parent policy did not affect existing shards**

Save again with **Apply to existing direct shards**, or use
`--apply-to-shards`. Shards created after the policy update inherit it without
that flag.

**A local agent stays alive but produces no output**

Configure `output_silence_timeout_seconds`. It is independent of total runtime
and process activity and forces bounded failover when stdout/stderr remains
silent.
