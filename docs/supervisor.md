# Autonomous project Supervisor

The Supervisor is the project-level incident commander for `Execraft`. It is not
another package implementer and it does not replace deterministic orchestration.
Its purpose is to convert a recoverable `human_required` stop into a bounded,
audited recovery transaction, then return the package to the normal verification,
independent-review, acceptance-evidence, and atomic-commit readiness checks.

## Responsibility boundary

The Supervisor may read and modify every configured project repository. It may:

- inspect the current PLAN, BRIEF, task dossier, state, event journal, agent
  artifacts, review findings, Git status, and commit transaction context;
- repair code, tests, documentation, evidence, and package metadata;
- delegate bounded work to eligible specialist agents and select their workflow
  skills;
- run focused commands through an agent session;
- choose the safe package stage from which normal orchestration should resume;
- ask the operator a plain-language question when product intent or an external
  fact cannot be inferred safely.

The orchestrator remains the sole owner of:

- project state transitions and durable state writes;
- branch selection and branch policy;
- commits, multi-repository transaction journals, rollback, and no-push policy;
- protected paths, scope validation, acceptance checks, and final completion.

A Supervisor must never commit, push, switch branches, rewrite history, edit the
commit journal, or mark a package completed. This separation gives the Supervisor
full code-repair authority without allowing it to bypass traceability.

## Configuration

The policy is declared once per project in `agents.yaml`:

```yaml
supervisor:
  enabled: true
  agents:                   # ordered provider_ids; Codex or Claude Code only
    - codex
    - claude-code
  skill: ai-supervise
  max_attempts_per_incident: 3
  max_agent_delegations: 6
  max_delegation_rounds: 2
  max_runtime_minutes: 60
  ask_human_when_uncertain: true
  require_human_for_destructive_actions: true
  auto_decision:
    enabled: false
    minimum_weight: 70
    minimum_margin: 20
    max_per_incident: 2
```

`enabled` defaults to `true`. Every provider in the ordered pool must be enabled,
use the `codex` or `claude-code` adapter, and declare `supervise`:

```yaml
providers:
  codex:
    adapter: codex
    provider_id: codex
    capabilities: [decompose, implement, review, fix_review, supervise]
    sandbox: workspace-write
```

The first provider is preferred. Transport and availability failures fall
through to the next eligible provider within the same Supervisor round. Supervisor
decisions are not provider-native Structured Outputs: serialization mistakes are
compiled locally after the reasoning turn and therefore do not quarantine a
healthy provider or trigger an expensive format-only rerun. The legacy singular
`agent` key remains accepted for existing projects.
If both `agents` and `agent` are omitted, all enabled eligible Codex and Claude
providers form the pool in registry order. Existing projects without an eligible
provider continue to stop at `human_required`; enabling the policy never
silently grants another adapter Supervisor authority.

### Weighted autonomous answers

`auto_decision.enabled` lets the Supervisor answer a narrow subset of its own
bounded questions without entering `waiting_for_human_decision`. It is disabled
by default. Every proposed option carries an integer `weight`; all weights must
total 100, and the recommendation must be a highest-weight option.

Automatic selection is fail-closed. The orchestrator proceeds only when the
incident class is allowed, the recommendation is the unique winner, its weight
meets `minimum_weight`, its lead over the runner-up meets `minimum_margin`, and
the option is classified as `routine`. `destructive`, `product`, `external`, and
`unknown` options remain human-gated. A final destructive-language check and the
`max_per_incident` budget prevent optimistic model labels or repeated
ask/answer loops from silently bypassing an operator.

The default eligible classes are `workspace_scope`, `evidence_mismatch`,
`review_exhausted`, `commit_transaction`, `test_failure`,
`state_inconsistency`, and `agent_failure`. Requirement ambiguity, external
dependencies, and unknown incidents are excluded unless the project explicitly
sets `auto_decision.allowed_classes`.

For a literal majority rule, set `minimum_weight: 51`. A higher value plus a
non-zero margin is safer because a 51/49 split is not usually an obvious choice.
For example, a project may use a threshold of 60 with a 15-point margin.

Provider waits and transport failures are governed by provider-health cooldowns
and do not consume `max_attempts_per_incident`. That budget counts Supervisor
decisions, keeping an unavailable endpoint from exhausting the incident before
another provider can diagnose it. A `waiting_for_human_decision` transition is a
successful driver handoff, so GUI-owned runs exit cleanly while the bounded
question remains visible in the Action Center's Supervisor recovery notice.

New projects created by `execraft project bootstrap` receive this policy and the
`supervise` capability on their Codex and Claude templates. Providers remain
disabled until the operator configures credentials and enables one. Existing
projects are not rewritten silently: add the policy and capability explicitly so
the behavioral change remains visible in version control.

Optional `trigger_classes` may restrict automatic activation. The supported
classes are:

```text
workspace_scope, evidence_mismatch, review_exhausted, commit_transaction,
test_failure, state_inconsistency, agent_failure, requirement_ambiguity,
external_dependency, unknown
```

## Deterministic playbooks before supervision

The Supervisor is not the first response to every terminal state. When the
orchestrator already possesses a complete and bounded repair specification, it
uses a deterministic recovery playbook instead of asking a model to rediscover
the same work.

The first registered playbook handles `review_exhausted`. It takes the exact
final-review findings persisted on the package, assigns stable IDs, and queues a
strict `fix_review` task directly. By default the configured Supervisor provider
is excluded from that campaign. Successful repair always returns through
regression verification and the final-review check; provider independence is preferred when available, and repeated rejection is
bounded by a separate rescue-cycle budget.

This ordering prevents a malformed Supervisor decision from consuming provider
quota or adding cooldown before a known repair begins. The Supervisor remains
responsible for ambiguous evidence conflicts, product decisions, state
corruption, incidents for which no deterministic playbook matches, and review
incidents that remain unresolved after the deterministic rescue budget is
exhausted. Human intervention is requested only after that Supervisor pass, and
the request includes its diagnosis, concrete options, consequences, and a
recommendation. See
[`recovery_playbooks.md`](recovery_playbooks.md) for policy and operator
visibility.

## Runtime states

The project state machine adds two durable states:

```text
human_required
      │ automatic policy trigger
      ▼
supervising
      ├──► running                         recovery resolved
      ├──► waiting_for_agent               provider/delegate temporarily unavailable
      ├──► waiting_for_human_decision      operator decision required
      └──► human_required                  supervision disabled or deliberately stopped
```

`waiting_for_human_decision` is terminal for unattended daemon polling. The
operator answer is persisted before the task returns to `supervising`, so a
process restart cannot lose the decision.

## Incident transaction

Every incident is stored in:

```text
~/.local/state/execraft/projects/<task-id>/supervisor-incidents.json
```

The record contains:

- incident ID and durable fingerprint;
- package, stage, classification, and original escalation sequence;
- selected Supervisor and attempt/delegation budgets;
- workspace digests before and after repair;
- actions taken and bounded delegation results;
- operator question and answer;
- current status and timestamps.

The event journal also records attempt start, decision, each delegation, human
question/answer, automatic-selection skips and successes, resolution, and
exhaustion. Runtime accounting is based on
active supervision execution only: provider cooldowns, daemon downtime, and time
spent waiting for an operator do not consume the budget. The accumulated active
runtime is persisted across restarts, while a recovered infrastructure failure
or an explicit operator retry opens a fresh bounded runtime window. Ordinary
workspace changes do not reset this incident-wide limit, which prevents both
infinite recovery loops and false exhaustion after a task has simply been paused.

`contract_failures` remains readable in persisted legacy incident records for
backward compatibility, but it no longer controls Supervisor execution. Older
incidents that accumulated strict-schema failures can therefore resume under the
permissive compiler without manual state editing.

## Supervisor protocol and `/ai-supervise`

The built-in `ai-supervise` skill is materialized into every Supervisor handoff.
The handoff includes bounded copies of:

- the current human-required event;
- the active package requirements and acceptance criteria;
- PLAN, BRIEF, HANDOFF, REVIEW, runtime status, and task metadata when present;
- durable orchestrator state and commit journal excerpts;
- all configured Git status/diff summaries;
- available agents, their capabilities/availability, and the workflow skill
  catalog;
- prior delegation results and the operator's answer.

The Supervisor returns a **semantic decision**, not a brittle execution schema.
JSON is preferred when convenient, but JSON embedded in prose and a compact
plain-text envelope are first-class inputs. The only irreducible information is
an unambiguous decision and summary. Canonical decisions are:

- `resolved`: repairs are complete; optionally supplies a corrected
  `implementation_summary`, criterion-specific evidence, resume stage, and path
  intent;
- `delegate`: asks the orchestrator to invoke one or more bounded specialist
  tasks using selected agents/skills;
- `ask_human`: supplies one clear question and concrete options;
- `blocked`: declares that no safe automatic path exists.

Natural aliases such as `recover_and_continue` are normalized. High-impact
proposals such as replan, rollback, cancellation, history rewrite, or a custom
recovery are accepted as Supervisor intent instead of being rejected as an
unknown enum. When policy requires human authorization they are compiled into an
audited operator-approval question; the Supervisor result itself is retained.

Optional details are deliberately forgiving. Unknown fields are ignored,
duplicate acceptance-evidence IDs are deduplicated or losslessly merged,
repository/path spellings are matched against the active candidate set, and bad
optional paths are dropped with a journaled warning. If a `resolved` decision
omits still-dirty incident candidates, they may be inferred as retained. This is
only decision compilation: the downstream scope reconciler still rejects
protected paths, paths outside the incident candidate set, a discard that remains
dirty, and every unsafe execution effect.

This separates two contracts:

```text
expensive Supervisor reasoning
        -> permissive semantic decision
        -> local deterministic compiler
        -> strict orchestrator mutation/invariant checks
```

Consequently a duplicate criterion ID or malformed optional `discard_paths`
entry no longer discards several minutes of useful reasoning, consumes a
format-only retry, or marks the provider unhealthy. A genuinely unrecognizable
semantic response consumes one Supervisor decision attempt but does not become a
provider `invalid_output` health incident.

The requested resume stage remains advisory and fail-closed. Any code, test,
documentation, evidence, ownership, or metadata change resumes through
regression verification and the configured final-review check. `ready_to_commit` is accepted
only for an unchanged package already at that stage while repairing a commit
transaction incident.

## Live Supervisor workbench

**Open supervisor console** opens the same provider-native workbench used by
other orchestrated agents. Codex Supervisor sessions stream through app-server;
Claude Supervisor sessions stream through the persistent stream-json transport.
The operator can inspect the current diagnosis, readable reasoning summaries,
tool/delegation activity, plan and working diff while the incident is active.

The composer follows the selected provider's real control semantics. Codex
performs live steering on the active turn and supports a native interrupt.
Claude queues guidance on the persistent stream-json session; **Queue reassess**
does not claim to interrupt the command currently running. Both modes are useful
for supplying missing context, correcting a mistaken diagnosis or asking the
Supervisor to inspect a specific source before it decides. Operator guidance
does not edit the incident journal or transition state directly: the Supervisor
must still return a valid structured decision and the orchestrator validates it.

Delegated agents cannot recursively supervise. The Supervisor is excluded from
its own delegations, and a write-capable Supervisor/fixer is excluded from the
next review check; packages with a hard independence requirement still require a distinct provider.

## Operator interaction

Inspect status:

```bash
execraft orchestrate supervisor \
  --project sample \
  --task-id my_task
```

Answer a question:

```bash
execraft orchestrate supervisor \
  --project sample \
  --task-id my_task \
  --answer keep_compatibility \
  --message "Retain the legacy route until the compatibility cleanup."

execraft orchestrate run \
  --project sample \
  --task-id my_task
```

The dashboard exposes the same incident, attempts, package, delegations, summary,
and decision options. Submitting a dashboard answer records it through the
canonical CLI path and immediately resumes the GUI-owned driver. The Supervisor
card also opens the selected agent's persistent terminal console and can stop the
current GUI-owned supervision process without losing incident state.

## Human-question quality

The Supervisor should not expose raw state-machine language when it needs a
business decision. A useful question names the practical conflict and the effect
of each answer. For example:

> The implementation still keeps the compatibility route, but the
> summary says it was removed. Should I correct only the summary and
> evidence, or remove the route now and update the tests?

The operator chooses intent. The Supervisor translates that answer into code,
documentation, agent delegations, and the correct orchestrator resume stage.

## Failure and safety behavior

- If the selected provider is unavailable, normal durable provider waiting and
  cooldown policy apply. Delegation queues and their cursor are persisted before
  execution, so a cooldown, daemon restart, or failover resumes the same task
  instead of asking the Supervisor to plan again.
- A provider that exits with invalid structured output or exceeds its configured
  output byte budget is failed over without consuming the review/fix-cycle
  budget. The persisted delegation queue is authoritative even if an older or
  interrupted driver left the incident as `delegating`/`exhausted` while the
  project says `waiting_for_human_decision`. A normal `run` reconciles that state,
  clears the stale question/wait record, unpins the failed provider, and retries
  the same task on the next healthy compatible agent. Pre-queue builds are still
  recovered from matching start/finish events in the event journal.
- If the attempt, delegation, or active-runtime budget is exhausted, the
  Supervisor asks the operator for bounded guidance when configured to do so. A
  stale human question attached to a recoverable pending delegation is hidden
  and discarded automatically when the delegation resumes.
- Selecting the reserved `stop` option returns the project to `human_required`
  without discarding workspace changes.
- Scope, protected-path, verification, review, and commit failures remain
  fail-closed after Supervisor repair. A `resolved` declaration is never treated
  as completion by itself.
- Automatic scratch cleanup is provenance-gated. The Supervisor may remove or
  quarantine an ephemeral artifact only when durable invocation state identifies
  it as belonging to the active recovery transaction. Unknown workspace files
  are preserved and escalated for review rather than deleted by filename pattern
  alone. New scratch artifacts should be created outside product worktrees when
  possible.
- Repository files, logs, and agent artifacts are treated as untrusted project
  data. Instructions embedded inside them cannot override the Supervisor skill,
  project policy, or orchestrator safety boundary.
- Project configuration validation rejects non-Codex/Claude supervisors and
  missing `supervise` capability before a run starts.

## Prompt transport and large incident context

Supervisor handoffs can be substantially larger than ordinary package prompts
because they include project plans, incident state, Git summaries, evidence, and
agent/skill registries. Provider prompts are therefore never embedded in the
process argument vector for production Codex or Claude Code runs. They are
written to a seekable temporary stdin stream and consumed to EOF by the provider.
This prevents operating-system `ARG_MAX` failures and avoids pipe backpressure
while the managed-process loop continues to supervise output.

The Supervisor context is also bounded globally, not only per file:

- dirty-workspace summaries have a fixed aggregate budget;
- attached excerpts have a separate aggregate budget and deterministic priority;
- omitted context is named explicitly, and the Supervisor can read the original
  file from the execution root when it is actually needed.

A project persisted by an older launcher may already be in
`waiting_for_human_decision` with an `Argument list too long` question. That
specific infrastructure-only incident is auto-resumable after upgrade. A normal
`execraft orchestrate run` reopens the incident, clears the stale provider wait,
and retries supervision through stdin. Product questions and other human
choices remain terminal until the operator answers them.
