# One-command start workflow

The normal entry point is intent-first:

```bash
cd /path/to/source
execraft start "Describe the outcome to implement"
```

The lower-level `project`, `task`, `workspace`, `plan`, and `orchestrate`
commands remain available for advanced and recovery operations. `start` composes
those domains without bypassing their validation or ownership rules.

## Existing-project lifecycle

A start request performs four durable phases:

| Phase | New state | Reuse rule | Failure behavior |
|---|---|---|---|
| project | descriptor and source registration | current directory resolves to one valid project | no task is published |
| task | dossier, manifest index, initial runtime status | project, task ID and title match | journal records failure; project remains |
| workspace | task branches, worktrees, rendered shell and workspace index | source route, shell route, policy, repository ownership and branches all match | created worktrees and shell are rolled back |
| plan | `PLAN.md` and `PLAN.graph.yaml` | existing graph is non-empty and complete | prior phases remain; plan failure is journaled |

After successful plan publication, draft or briefed tasks move to `planned` and
the runtime status projection is synchronized.

## Preview and confirmation

`execraft start` always constructs the preview before application. During preview:

- source discovery does not import, build, or execute project code;
- configured execution agents are inventoried but never invoked;
- a deterministic local plan is generated in memory to prove that the intended
  repository scope can produce a valid graph;
- project and task creation plans include exact paths, byte sizes and hashes;
- no XDG control-plane or state directory is created solely by the preview.

Apply interactively with the default confirmation or explicitly in automation:

```bash
execraft start "Add readiness endpoint" --yes --json
```

Non-interactive stdin without `--yes` fails closed.

## Task identity

The task ID is derived from significant normalized words in the request. An
operator can override it with `--id`. Generated collisions receive a stable
numeric suffix. A matching journal and normalized-intent SHA-256 resumes the
existing task instead of allocating another ID.

Explicit IDs never silently switch to another task. If an explicit ID already
belongs to different intent or metadata, the workflow fails and requires an
operator decision.

## Repository scope

```bash
execraft start "Update login UI" --repositories web-ui auth
```

Unknown repository IDs fail validation. Required repositories are retained even
when omitted. Automatic selection uses task-token matches against repository
identity, source path, workspace name and role. Required repositories receive a
mandatory score and integration/deployment repositories receive a small policy
weight so cross-repository verification assets are not accidentally omitted.

The JSON result includes every scope score as evidence.

## Planner safety

The planner modes are:

- `auto`: use the strongest eligible planning agent and fall back to a local draft;
- `agent`: require an eligible planning agent and fail on agent/runtime errors;
- `local`: never invoke an execution agent.

An eligible planning agent must be enabled, locally executable, declare `plan` or
`decompose`, be available through its runtime/adapter, and enforce read-only execution
at `provider_policy` or `hard` level. (`provider_policy` is the retained compatibility
name for that isolation class.) Ranking is deterministic:

1. hard read-only enforcement;
2. capability-specific weight;
3. configured priority;
4. stable agent/profile instance name.

The planning handoff explicitly forbids file changes and project-command
execution. It requests one structured JSON object containing `plan_markdown` and
`plan_graph`. The returned graph is validated locally; prompt compliance is not
trusted as validation.

## Local draft

The deterministic fallback is deliberately modest but executable. It creates a
single pending work package containing:

- the original normalized intent as a requirement;
- behavior and verification acceptance criteria;
- the selected repository scope;
- explicit risk, priority and verification profile metadata.

This avoids an empty plan passing validation while allowing a user to refine or
decompose the task before implementation.

## Recovery

Journals are stored at:

```text
~/.local/state/execraft/starts/<project-id>/<task-id>.yaml
```

Each update is fsynced and atomically replaced. The journal validates project,
task, intent digest and source route before reuse. A typical recovery is:

```bash
# Initial agent-backed planning failed after workspace creation
execraft start "Add readiness endpoint" --planner agent

# Repair agent/runtime configuration or switch to deterministic planning
execraft start "Add readiness endpoint" --planner local
```

The second command reuses the project, task and safe workspace and only
replaces the missing/invalid plan phase.

## Greenfield workflow

```bash
execraft new service-name --template python-service
execraft new service-name --start "Add the first capability"
```

Built-in source templates are versioned and can be selected exactly with
`template@version`. `execraft new --list-templates` reports installed options.
Greenfield creation:

1. stages the complete source tree;
2. validates required files and rejects symlinks;
3. publishes the source atomically;
4. creates `main` and an initial commit;
5. discovers and registers the project through normal onboarding;
6. optionally runs the same start workflow for the first task.

Git or registration failure removes the newly published source tree. A failure
in the optional first task does not delete valid source history or its project
registration; rerun `execraft start` inside the generated repository to resume.

## JSON output

The top-level start outcome includes compatibility fields named `provider` /
`provider_id`; these carry the selected execution-agent identity for older API
consumers and should not be interpreted as a model-provider dimension:

```yaml
applied: true
ready: true
project: sample-app
task_id: add-readiness-endpoint
source_root: /absolute/source
workspace_root: /home/user/workspace/ai-workspaces/sample-app/add-readiness-endpoint
provider:
  available: false
  selected: null
  reason: No enabled local provider ...
repository_scope:
  repositories: [sample-app]
  explicit: false
  evidence: []
steps:
  - id: project
    status: completed | reused | ready | failed
    summary: ...
plan:
  generated_by: local | agent | existing
  provider_id: ""
  fallback_reason: ...
  work_packages: 1
journal: /absolute/state/path.yaml
```

`ready` means no composed phase is blocked or failed. It is not a replacement
for `execraft project doctor`, which still reports execution-agent/runtime and
verification readiness for full orchestration.
