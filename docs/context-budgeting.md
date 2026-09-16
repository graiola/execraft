# Package Context and Token Budgeting

`Execraft` builds agent prompts from package-scoped context blocks. It no longer
uses the complete append-only task dossier as the default context for every
implementation, review, retry, or Supervisor incident.

## Context model

The runtime state directory contains generated capsules:

```text
<state-dir>/projects/<task-id>/context-capsules/<package-id>.json
<state-dir>/projects/<task-id>/context-capsules/<package-id>--<shard>.json
```

A capsule is a deterministic, versioned projection of the active work package:

- objective, requirements, acceptance criteria, dependencies, risk, and scope;
- the exact matching `PLAN.md` section only when the graph still uses a legacy
  `PLAN.md` pointer;
- applicable entries from the optional `DECISIONS.yaml` index;
- compact current implementation, review, verification, and invocation evidence;
- source references, SHA-256 fingerprints, and a capsule digest.

Capsules are written atomically under a process lock. They live in orchestration
state rather than a product repository, so refreshes cannot create false dirty
workspace incidents. Parallel shards receive separate files and preserve shard
attribution.

`HANDOFF.md` remains a human-readable historical archive. It is not loaded into
ordinary prompts. The SQLite invocation ledger is the canonical structured
execution-evidence store; it retains exact handoffs, terminal results, failures,
context manifests, token usage, and artifact references without copying that
history into later calls.

## Context planning

Context is represented as typed blocks with a source, scope, priority, required
flag, stable deduplication key, estimated size, and truncation policy. Before a
provider call, the planner:

1. normalizes and deduplicates candidate blocks;
2. orders mandatory blocks before optional blocks;
3. selects package-, capability-, stage-, and incident-relevant evidence;
4. bounds optional prose or logs at semantic block boundaries;
5. rejects the prompt if mandatory material exceeds the hard limit;
6. persists an inclusion/exclusion report in the invocation handoff.

Requirements, acceptance criteria, JSON schemas, and other mandatory structured
contracts are never silently truncated by a provider.

The estimator is intentionally conservative (`UTF-8 bytes / 3.5` plus framing)
when a provider tokenizer is unavailable. The estimator name is persisted with
the budget report so measured and estimated values are distinguishable.

## Capability budgets

Defaults are defined for planning, decomposition, implementation, review,
review fixes, verification, supervision, and closing. Override them in the
project agent configuration:

```yaml
scheduling:
  token_budgets:
    implement:
      input_target: 12000
      input_hard_limit: 24000
      output_target: 1500
      output_hard_limit: 4000
    review:
      input_target: 16000
      input_hard_limit: 30000
      output_target: 1500
      output_hard_limit: 4000
    supervise:
      input_target: 24000
      input_hard_limit: 48000
      output_target: 3000
      output_hard_limit: 6000
```

All values must be positive, targets cannot exceed hard limits, and unknown
capability keys fail configuration loading. The final rendered prompt is checked
again after provider selection and before an invocation row is opened.

## Supervisor profiles

The Supervisor receives a bounded incident profile rather than the complete
project dossier. Examples:

- dirty workspace: candidate paths, status, diff/stat, ownership, and commit state;
- verification failure: failed command, bounded error evidence, package criteria,
  and latest relevant implementation state;
- review exhaustion: unresolved findings, latest review evidence, and bounded diff;
- commit or transaction failure: Git and transaction records;
- requirement ambiguity: package capsule and the relevant bounded brief section.

The Supervisor can inspect the real workspace when a concrete missing fact
requires it. Unrelated historical documents are not preloaded.

## Prompt caching

Prompt rendering puts stable content first:

1. orchestration contract;
2. workflow skills;
3. output JSON Schema;
4. dynamic invocation identity and package content;
5. transient attempt and failure evidence.

Stable maps are serialized with sorted keys and compact separators. Handoff IDs,
attempts, timestamps, and transient failures no longer invalidate the beginning
of otherwise identical prompts.

Ordering the prompt is only half of it — a provider cache is only reused if the
bytes *before* the prompt are also stable, and if the session lives long enough
to be read again.

### Stable system prefix

Claude Code's default system prompt embeds per-machine sections: working
directory, environment info, memory paths, and git status. The orchestrator
drives many invocations against one workspace whose git status changes after
every write stage, so those sections invalidate the cached system prefix on
essentially every call. The adapter passes
`--exclude-dynamic-system-prompt-sections`, which moves them into the first user
message. A CLI that does not recognize the flag falls back automatically.

### Session reuse

A new provider process starts with a cold cache and re-derives repository
context from scratch. Where a retry is a continuation rather than new work, the
orchestrator hands the prior session back to the adapter:

```json
"execution_context": {
  "resume_session": {"provider_id": "claude-code", "session_id": "..."}
}
```

Session IDs are provider-scoped, so an adapter ignores a request recorded
against a different provider. Resuming is strictly an optimization — a stale or
unknown session falls back to a cold invocation rather than failing the attempt.

This is currently used for the structured-output *format repair* retry, where
the provider already did the work and only missed the wire contract. Resuming
lets it correct its own answer without re-reading the repository.

## Usage accounting

Provider adapters are normalized into a common usage record:

- prompt bytes and estimated input tokens;
- reported input, cached input, cache creation/read, output, and reasoning tokens;
- total tokens, reported cost, currency, provider, model, capability, package,
  shard, stage, attempt, and invocation ID;
- the original provider usage payload for diagnostics.

The SQLite ledger schema migrates in place. Existing rows receive an empty usage
mapping. Cached-token buckets remain separate to avoid accidental double
counting across providers with different accounting semantics.

Inspect usage and capsules with:

```bash
execraft orchestrate context \
  --project sample --task-id feature_auth --package-id implementation

execraft orchestrate context \
  --project sample --task-id feature_auth --package-id implementation \
  --rebuild-context

execraft orchestrate usage \
  --project sample --task-id feature_auth

execraft orchestrate usage \
  --project sample --task-id feature_auth --package-id implementation
```

The GUI **Execution health** drawer exposes task/capacity metrics including total
normalized tokens, cache hit rate, and tokens spent on failed attempts. The quiet
Run health row does not carry token detail. The dashboard snapshot also exposes provider,
capability, status, prompt-size percentile, and context-block aggregates.

### Reading the cache report

`orchestrate usage` returns a `cache` block derived from the separate
cache-creation and cache-read buckets:

```json
"cache": {
  "status": "reported",
  "hit_rate": 0.82,
  "reuse_ratio": 4.5,
  "cache_creation_tokens": 20000,
  "cache_read_tokens": 90000,
  "uncached_input_tokens": 1900
}
```

- `hit_rate` — share of prompt input served from cache at read prices.
- `reuse_ratio` — reads per write. **Below 1.0 means cached prefixes are being
  written more often than they are reused, which costs more than not caching at
  all.** That is the signature of a prefix being invalidated on every call.
- `status: "unreported"` means the provider reports no cache buckets, which is
  not the same as a cache that stopped working.

Cache-creation and cache-read are kept as separate buckets on purpose: a
regression shows up as reads collapsing while writes stay flat, which a single
combined "cached tokens" number hides.

## Reasoning effort

Every supported CLI exposes a knob trading reasoning depth against tokens and
latency, spelled differently by each:

| Provider    | CLI surface                              | Levels                |
| ----------- | ---------------------------------------- | --------------------- |
| Claude Code | `--effort <level>`                       | low, medium, high, xhigh, max |
| Codex       | `-c model_reasoning_effort="<level>"`    | low, medium, high     |
| Antigravity | `--effort <level>`                       | low, medium, high     |
| OpenCode    | `--variant <level>`                      | provider-specific     |

Configure it once in the orchestrator's own vocabulary; each adapter maps it
onto what its CLI accepts. A level stronger than a provider's ladder clamps down
(`xhigh` becomes `high`) rather than failing the package:

```yaml
providers:
  claude:
    effort: high
    effort_by_capability:
      review: medium
      decompose: medium
      fix_review: low
```

Omitting both keys leaves the provider default untouched, which is not the same
as `low` — the adapter omits the flag entirely. An unknown level, or a level for
an undeclared capability, fails configuration loading rather than at dispatch.

## Decision index

Projects may add `DECISIONS.yaml` beside `PLAN.md`:

```yaml
decisions:
  - id: authoritative-execution-routing
    title: Runtime boundary owns provider execution
    summary: Concrete adapter execution must stay behind the runtime boundary.
    status: active
    packages: [implementation, review]
    scope: [backend, frontend]
    source: BRIEF.md#provider-runtime-ownership
```

Entries with no package or scope apply globally. Superseded and rejected entries
are excluded. Capsule generation never invents a decision when extraction is
ambiguous.

## Legacy migration

No destructive migration is required. Existing projects continue to work:

- actionable graph requirements are used directly;
- a generic `PLAN.md` requirement activates a bounded legacy fallback containing
  only the matching package section;
- a package with deferred requirements remains operable but is marked
  `legacy_fallback: true` for inspection;
- original dossier files are never rewritten.

Improve a legacy graph incrementally by replacing generic `PLAN.md` pointers with
actual package requirements and scope. Once the graph is self-sufficient, the
capsule omits the plan section automatically.

## Security and integrity

Capsule identifiers are path-safe, generated paths are rooted in runtime state,
source provenance is retained, YAML and JSON are schema-checked, and all project
content is explicitly treated as untrusted task data. Context records do not
execute commands contained in logs or evidence. Provider prompts should still be
redacted by the existing secret-handling boundary before external transport.

## Troubleshooting

A `ContextBudgetError` reports the estimated size, hard limit, and largest
contributors. Use `orchestrate context` to inspect the capsule and `orchestrate
trace --include-handoff` to inspect the exact block manifest. Increase a hard
limit only after confirming mandatory content is legitimately large; prefer
splitting an oversized package or replacing broad prose with structured graph
requirements.
