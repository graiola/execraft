# Onboarding domain architecture

Project and task onboarding are separated from CLI and GUI transport concerns.
Every creation path now follows the same sequence:

```text
inspect -> collect evidence/findings -> select template -> stage tree
        -> validate creation plan -> atomically publish -> finalize registration
```

No project code or execution-agent runtime is invoked during inspection or dry-run.

## Components

### `DiscoveryEngine`

`execraft.onboarding.discovery.DiscoveryEngine` inspects Git metadata and a bounded
file inventory. Detectors receive relative paths and update repository facts;
new ecosystems can be added without changing the application service.

The default inventory prefers:

```text
git ls-files -co --exclude-standard -z
```

This avoids traversing ignored build products and remains bounded to 25,000
paths. A bounded directory walk is used when Git cannot provide an inventory.
Every inferred value is represented as `Evidence` with:

- subject and field;
- proposed value;
- source and location;
- confidence in the range `0.0..1.0`;
- a human-readable rationale.

Discovery outcomes use explicit severities:

- `error`: creation cannot be applied;
- `decision_required`: the plan remains blocked until the operator explicitly
  accepts the recorded choice with `--accept-decisions`;
- `warning`: creation is safe but a later workflow may be blocked;
- `info`: explanatory context only.

`execraft project inspect --json` exposes the complete report.

### `TemplateCatalog`

`execraft.onboarding.templates.TemplateCatalog` stores project and task templates
by kind, ID, and positive integer version. `standard` resolves to the latest
registered version; `standard@1` selects an exact version. The selected template
reference is retained in every creation plan.

Templates only materialize files into an empty staging directory. They cannot
register projects, update task indexes, start workspaces, or execute source-tree
commands. Those responsibilities belong to application services and
transactions.

List installed templates with:

```bash
execraft project templates
execraft project templates --json
```

### `AtomicTreeTransaction`

Project and task trees are staged under the nearest existing ancestor of their
final destination and published with a same-filesystem atomic rename. This keeps
dry-run free of persistent control-plane directories. Before publication, the
transaction:

1. rejects an existing target;
2. validates required files;
3. rejects template-created symbolic links;
4. inventories every file, byte size, and SHA-256 digest;
5. creates an immutable `CreationPlan`;
6. refuses errors and unaccepted `decision_required` findings.

Publication is serialized with a runtime-directory lock keyed by the target
parent. Finalizers run only
after the tree is visible. If registration, manifest indexing, or runtime-status
initialization fails, rollback callbacks remove published indexes and the newly
published tree.

A dry-run performs the same staging and validation, prints the real file plan,
and removes the staging tree without publishing the target.

### `ProviderInventory`

`ProviderInventory` is a compatibility-named service that structurally parses all
enabled and disabled execution-agent declarations in `agents.yaml` and resolves
their configured executables against the current environment. It does not
authenticate an agent/runtime or make network calls. Use
`execraft agents doctor --smoke-test` for an explicit live check.

### `ReadinessService`

Project health is no longer represented by one ambiguous boolean. `project
doctor` reports independently repairable dimensions:

```text
descriptor
source
repositories
workspace
verification
providers
provider_registry
orchestration
```

A descriptor may therefore be registered successfully while orchestration is
blocked because no verification command is approved or no enabled execution agent
is locally runnable.

### `OnboardingService`

`OnboardingService` coordinates the collaborators above. CLI commands call this
service rather than implementing filesystem workflows directly. The same API is
available to the GUI and future RPC frontends:

```python
service.inspect_project(...)
service.create_project(..., dry_run=True)
service.create_task(..., dry_run=True)
service.evaluate_readiness(...)
```

## Project workflow

Inspect without writing:

```bash
cd /path/to/source
execraft project inspect
execraft project inspect --json
```

Preview the actual generated files:

```bash
execraft init --dry-run
execraft init --dry-run --json
```

Plans with `decision_required` findings report `can_apply: false`. After reviewing
the evidence, accept those exact findings explicitly:

```bash
execraft init --dry-run --accept-decisions
execraft init --template standard --accept-decisions
```

Plans without pending decisions apply normally:

```bash
execraft init --template standard
```

An empty non-Git directory now produces a blocking discovery finding and is not
published.

## Task workflow

Preview a task dossier, initial brief, repository scope, manifest, and registry
effects:

```bash
execraft task new auth-refresh \
  --title "Add refresh-token revocation" \
  --brief "Persist token families and revoke the full family on reuse." \
  --repositories api auth \
  --dry-run --json
```

Publish it atomically by removing `--dry-run`. Required project repositories are
always retained; optional repositories follow the explicit selection.

## JSON contracts

Discovery, readiness, and creation plans expose deterministic mappings suitable
for GUI/API use. A creation plan contains:

```yaml
kind: project | task
identifier: logical identity
target: absolute final path
can_apply: true | false
decisions_accepted: true | false
pending_decisions: []
total_bytes: integer
files:
  - path: relative/path
    action: create
    size_bytes: integer
    sha256: full digest
findings: []
evidence: []
metadata:
  template: standard@1
```

The output intentionally omits file contents. SHA-256 and byte size provide a
stable preview while avoiding accidental secret or source disclosure in logs.

## Composed start workflow

The start command composes the control-plane and creation primitives into one
application workflow. The public sequence is:

```text
execraft start <intent>
  -> resolve or inspect/register project
  -> normalize intent and reserve task identity
  -> infer repository scope
  -> select safe read-only planner
  -> create/reuse task transaction
  -> prepare/reuse workspace transaction
  -> generate, validate, and atomically publish plan
  -> update lifecycle and durable journal
```

The implementation is split by responsibility:

- `start_models.py`: immutable intent, scope, execution-agent choice, step, request, and outcome
  contracts (with compatibility-named provider fields where required);
- `selection.py`: bounded repository-scope inference and planning-agent ranking;
- `planning.py`: deterministic and agent-backed draft generation, graph
  validation, and two-file atomic publication;
- `start.py`: orchestration and resumable phase journal;
- `workspace/lifecycle.py`: reusable workspace ownership, creation, rendering,
  and rollback logic shared by the CLI and onboarding workflow;
- `greenfield.py`: versioned source templates, Git initialization, and source to
  project registration transaction.

### Resumability

The journal is written atomically after every durable phase at:

```text
<XDG_STATE_HOME>/execraft/starts/<project>/<task>.yaml
```

A journal is accepted only when project ID, task ID, normalized-intent digest,
and source route all match. Reruns validate and reuse existing resources. Task,
workspace, and plan failures are persisted independently so repair does not
require deleting successful earlier phases.

### Repository selection

Explicit repository IDs are validated against the descriptor. Required
repositories are always included. Without an explicit scope, selection scores
task tokens against repository IDs, paths, workspace names, and roles, while
required and integration repositories receive deterministic policy weights. The
complete evidence is exposed in text and JSON outcomes.

### Planning-agent selection

The compatibility-named provider inventory remains non-authenticating. The selector
considers only enabled planning agents whose executable is present, whose declared
capabilities contain `plan` or `decompose`, and whose adapter/runtime enforces at
least `provider_policy` read-only isolation. Eligible agents are ranked by isolation
strength, capability-specific weight, configured priority, and stable profile name.
Advisory-only adapters are never used for automatic planning.

`auto` mode falls back to a deterministic local draft when no eligible planning
agent exists or the agent/runtime attempt fails. `agent` and `--require-agent`
convert that condition into a blocking error. `--require-provider` remains a
deprecated compatibility alias for `--require-agent`.

### Draft-plan contract

All plans, including local fallback plans, must have non-empty Markdown and at
least one complete work package. Validation rejects cycles, missing requirements
or acceptance criteria, empty affected-repository lists, and repositories outside
the selected task scope. `PLAN.md` and `PLAN.graph.yaml` are staged and replaced
as one rollback-aware publication step.

### Greenfield creation

`execraft new` is intentionally separate from `execraft init`. A greenfield template
is materialized into an atomic source-tree transaction, receives a `main` branch
and initial commit, and is then passed through normal project discovery and
registration. Git identity fallback is supplied only to the commit command and
is never persisted. Source publication is rolled back when Git initialization or
project registration fails.

## Profile and upgrade layer

`execraft.onboarding.profiles` separates orchestration policy from composable
ecosystem features. Profile-backed templates emit schema-3 descriptors and a
managed-file provenance manifest. `execraft.onboarding.upgrades` renders the target
profile into isolation, reconciles it against recorded SHA-256 baselines, and
publishes conflict-free per-file changes transactionally. This layer does not own
source discovery, task creation, agent/runtime authentication, or product-repository
mutation.

## Structural enforcement

The CLI parser, GUI route dispatch, and compatibility-named provider-wait scheduler now have explicit
module boundaries. `tools/check_architecture.py` verifies those boundaries with
AST inspection and is executed by both the normal test suite and CI. Generated
profile/source templates are also treated as executable compatibility contracts:
each is created, registered, checked for readiness and provenance integrity, and
verified to be upgrade-idempotent.
