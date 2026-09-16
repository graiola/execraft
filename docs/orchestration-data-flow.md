# Orchestration data flow and durable provenance

This document describes the runtime contract between the project orchestrator,
provider agents, reviewers, fixers, and the autonomous Supervisor. It also
explains the durable records added to make retries, failover, review, and crash
recovery reproducible.

## Control-plane boundaries

The orchestrator is the only component allowed to advance work-package state,
select providers, apply verification policy, reconcile repository scope, and
commit changes. Provider agents do not send messages directly to one another.
They communicate through explicit handoffs and durable control-plane records;
the Git workspace remains the product-data surface, not the source of hidden
control-plane memory.

```text
PLAN + task dossier + project configuration
                    |
                    v
              Scheduler
                    |
                    v
       AgentContextAssembler
                    |
                    v
          StructuredHandoff v2
                    |
                    v
       provider adapter / agent
                    |
          +---------+---------+
          |                   |
          v                   v
   provider result        Git workspace
          |                   |
          +---------+---------+
                    v
      invocation ledger + artifact
                    |
          verification / review
                    |
          fix, commit, or escalate
                    |
                    v
               Supervisor
```

## Structured handoff v2

Every agent call receives a `StructuredHandoff` with a stable schema version,
unique handoff ID, causal parent invocation, triggering journal event, attempt
number, bounded attempt history, exact skill manifest, execution context, and
required isolation level. The serialized handoff is hashed with SHA-256 before
execution.

The context assembler adds stage-relevant evidence instead of requiring the next
agent to infer all history from the working tree:

- previous implementation result;
- failed verification commands, exit codes, and bounded output;
- previous review verdict and findings;
- prior invocation attempts and failure classifications;
- workspace diff summary and digest;
- selected skill IDs, versions, hashes, and selection reasons.

The same contract is used for serial execution and parallel shards.

## Invocation ledger

Each task owns an `agent-invocations.sqlite3` database under its state directory.
SQLite WAL mode and full synchronous durability allow concurrent readers and
parallel shard writers without JSON lost-update races.

One row records:

- project, task, work package, stage, capability, and attempt;
- provider ID, adapter, and model;
- parent invocation and triggering event;
- exact handoff and handoff hash;
- exact skill snapshot;
- adapter isolation capabilities;
- workspace digests before and after execution;
- result artifact, normalized result, validation errors, or failure;
- start, completion, duration, and terminal status.

Rows are append-oriented and terminal: a completed invocation cannot later be
reclassified as failed. A local journal/state projection failure after provider
completion is therefore surfaced as an orchestration persistence error and does
not poison provider health.

Inspect recent calls with:

```bash
execraft orchestrate trace --project sample --task-id my_task
execraft orchestrate trace --project sample --task-id my_task --limit 20
execraft orchestrate trace --project sample --task-id my_task --include-handoff
```

`--include-handoff` can expose bounded source excerpts and task context; use it
only where the output can be handled as private engineering data.

## Causal retries and failover

A failed attempt is appended to the work package and invocation ledger before
another provider is selected. The next handoff identifies the previous
invocation and includes a bounded failure history. This prevents a fallback
agent from blindly repeating work and preserves the distinction between:

- provider transport failures;
- invalid structured output;
- capability or isolation-policy exclusion;
- verification failures;
- review findings;
- local orchestration persistence failures.

Verification failures are stored as structured context with the command,
repository, exit code, status, and bounded stdout/stderr. The following
implementation attempt receives that context explicitly.

## Skill provenance and override policy

Skills remain prompt instructions, not executable plugins. Every materialized
skill now records its ID, declared version, SHA-256 content hash, source,
selection reason, and rendered size.

A project skill may not silently shadow a built-in skill. An intentional override
must declare it in the front matter:

```yaml
---
id: code-review
version: 2
overrides: builtin
---
```

This makes prompt behavior auditable and prevents accidental instruction drift.
Skill content remains bounded per skill and per invocation.

## Read-only enforcement

Adapters declare execution capabilities instead of relying on a generic boolean:

- `advisory`: the prompt asks the provider not to write;
- `provider_policy`: the provider exposes a planning/read-only mode;
- `hard`: the adapter enforces a filesystem sandbox;
- workspace write, network isolation, command allowlist, structured output, and
  streaming support are declared separately.

Projects choose the minimum accepted level:

```yaml
scheduling:
  minimum_read_only_enforcement: provider_policy
```

The scheduler excludes adapters that cannot satisfy the handoff. Use `hard` for
environments where review must be enforced by the operating boundary rather
than provider behavior.

## State checkpoints and journal durability

`state.json` remains the human-readable compatibility projection. The durable
source for recovery is `orchestration-checkpoints.sqlite3`, which stores the
serialized state, its hash, the observed journal sequence, reason, and timestamp.
Identical state/sequence pairs are deduplicated and retention is bounded.

If `state.json` is missing or malformed, the orchestrator restores the latest
valid checkpoint and emits `state_projection_recovered`.

The event journal now serializes append operations with an inter-process file
lock, assigns monotonic sequence numbers while holding that lock, atomically
replaces the file, and fsyncs both data and parent directory. Parallel processes
therefore cannot allocate the same sequence or overwrite each other's events.

## Collision-safe task storage

State directories are owned by `(project_id, task_id)`, not by task ID alone.
For backward compatibility, the first project claiming a legacy task directory
keeps it and receives an atomic owner marker. A conflicting project receives a
deterministic namespaced directory. CLI, GUI, runtime status, archive, journal,
checkpoint, and invocation paths all resolve through the same identity service.

## Project descriptor path bases

Newly bootstrapped projects declare:

```yaml
path_base: project_directory
```

All generated paths are local to the descriptor directory. This makes
`execraft project bootstrap --output <directory>` valid outside the Execraft checkout.
Legacy descriptors omit `path_base` and continue resolving paths from the
registry root.

## Process startup grace

The stdout/stderr silence watchdog now has a separate `startup_grace_period`.
The initial silence deadline is the greater of the normal silence timeout and
the startup grace. After the first output, the ordinary silence timeout applies.
This preserves hung-provider detection without killing a CLI during cold start.

## Supervisor flow

The Supervisor remains a bounded recovery authority, not a second independent
orchestrator. It receives the incident, state, journal excerpts, plan/task
artifacts, workspace summary, available providers and skills, and completed
specialist delegations. It may return `resolved`, `delegate`, `ask_human`, or
`blocked`.

Delegations use the same structured handoff and invocation ledger as ordinary
agent calls. A Supervisor decision is reconciled by the orchestrator and must
still pass scope, verification, review, and commit readiness checks.

## Archive contents

Task archives now include consistent SQLite snapshots of:

- `agent-invocations.sqlite3`;
- `orchestration-checkpoints.sqlite3`.

The archive manager uses SQLite's backup API so WAL-backed databases are copied
as coherent snapshots rather than as potentially incomplete raw files.


## Automatic Project-level flow

```text
PROJECT_EXECUTION.yaml policy
        +
Task outcomes / Gate state / durable start intents
        │
        ▼
AutomaticExecutionPlanner   (pure selection)
        │
        ▼
ProjectExecutionEngine.automatic_cycle
        │ persist start intent
        ▼
TaskExecutionPort.start(task_id)
        │
        ▼
canonical Task Run lifecycle
        │
        ▼
Task orchestrator → Work Packages
```

The Project planner does not see agent, Stage, review-loop, or Work Package
concurrency. Reconciliation/status reads stop before the side-effecting cycle.

## Project delivery flow

Delivery is downstream of Project Execution and never enters Task orchestration:

```text
achieved ProjectMilestone baseline
        │
        ▼
DeliveryCandidate (canonical baseline digest)
        │
        ▼
durable delivery operation intent
        │
        ▼
DeliveryProvider
        │
        ▼
DeliveryResult / external references
```

The delivery provider sees no Work Package scheduler API. Conversely, the
Milestone definition sees no provider credentials, registry, CI/CD, Docker, or
deployment configuration.
