# Operator guide

This guide covers the normal `Execraft` workflow from project onboarding through
task completion. It intentionally avoids duplicating the detailed contracts in
the topic documents linked throughout the guide.

## 1. Check the installation

```bash
execraft home
execraft doctor
```

For execution-agent/runtime checks:

```bash
execraft agents status --project <project-id>
execraft agents doctor --project <project-id> --smoke-test
```

A project is not ready for autonomous orchestration until its descriptor,
repositories, verification commands, and at least one enabled execution agent are
usable.

## 2. Onboard a project

### Existing source tree

Inspect the source without modifying it:

```bash
cd /path/to/project

execraft project inspect
execraft init --dry-run
```

Create and register the descriptor:

```bash
execraft init --template standard
```

Validate the result:

```bash
execraft project validate <project-id>
execraft project doctor <project-id>
```

If a descriptor already exists, register or bind it instead of creating a new
one:

```bash
execraft project register \
  --descriptor /path/to/project.yaml \
  --source /absolute/path/to/project-root

# or
execraft project bind <project-id> --source /absolute/path/to/project-root
```

Host-specific paths belong in local registration state, not in the portable
project descriptor.

### Greenfield project

Preview before creating files:

```bash
execraft new sample-service --template python-service --dry-run
```

Then apply:

```bash
execraft new sample-service --template python-service --yes
```

For onboarding internals, see
[`onboarding-architecture.md`](onboarding-architecture.md) and
[`control-plane-home.md`](control-plane-home.md).

## 3. Start a task

For most work, use the composed command:

```bash
cd /path/to/project
execraft start "Add request tracing to the API"
```

The preview is side-effect free:

```bash
execraft start "Add request tracing" --dry-run --json
```

Useful controls:

```bash
# Local deterministic planning
execraft start "Add request tracing" --planner local

# Require agent-backed planning
execraft start "Add request tracing" --planner agent --require-agent

# Explicit multi-repository scope
execraft start "Update authentication" --repositories backend frontend

# Reuse an existing plan
execraft start --plan-file ./PLAN.md --planner local
```

`execraft start` creates or reuses the project, task dossier, workspace, and
validated execution graph. Use the lower-level commands only when you need
explicit control over individual lifecycle stages.

See [`start-workflow.md`](start-workflow.md) and
[`task-definition-import.md`](task-definition-import.md).

## 4. Create a task manually

A manual task dossier can be useful for reviewed or imported plans:

```bash
execraft task new feature_auth \
  --project sample \
  --title "Add authentication" \
  --brief "Add token-based authentication and preserve current API behavior." \
  --repositories backend frontend
```

Preview the transaction first when needed:

```bash
execraft task new feature_auth \
  --project sample \
  --title "Add authentication" \
  --brief "Add token-based authentication." \
  --dry-run --json
```

Import existing task documents with `--brief-file`, `--plan-file`, or
`--plan-graph-file` rather than copying them into generated state manually.

Inspect task state with:

```bash
execraft task status feature_auth --project sample
execraft task sync-status feature_auth --project sample
```

## 5. Work in an isolated workspace

Create the workspace:

```bash
execraft workspace start feature_auth \
  --workspace-root ~/workspace/ai-workspaces/feature_auth \
  --policy workspace-write
```

Inspect and verify it:

```bash
execraft workspace status feature_auth
execraft workspace verify feature_auth --profile focused
```

Open the generated multi-root VS Code workspace:

```bash
execraft code feature_auth
```

Use the generated **Workspace** root as the control surface. Source edits belong
in the task worktrees, while generated agent/runtime configuration, skills, and the
repository catalog live in the workspace shell.

After changing project or runtime/agent configuration:

```bash
execraft workspace refresh feature_auth
```

Do not edit generated `.agents`, `.claude`, `.codex`, `.opencode`, `.vscode`,
`AGENTS.md`, or `CLAUDE.md` files directly.

See [`workspace-lifecycle-safety.md`](workspace-lifecycle-safety.md).

## 6. Configure verification and execution agents

Verification commands are defined by the project and are always explicit.
Profiles are cumulative:

```text
cheap -> focused -> integration -> full
```

Run a focused check from the workspace with:

```bash
execraft workspace verify feature_auth --profile focused
```

Execution-agent health is separate from project verification:

```bash
execraft agents status --project sample
execraft agents doctor --project sample
execraft agents doctor --project sample --agent codex --smoke-test
```

Use project policy to control capabilities, weights, complexity limits,
concurrency, and agent-profile preferences. See
[`execution_policy.md`](execution_policy.md),
[`quality-checks.md`](quality-checks.md), and
[`satellite_nodes.md`](satellite_nodes.md).

## 7. Run the orchestrator

The orchestrator executes the validated `PLAN.graph.yaml`, not free-form plan
text.

Initialize state:

```bash
execraft orchestrate init \
  --project sample \
  --task-id feature_auth \
  --plan-file projects/sample/tasks/feature_auth/PLAN.graph.yaml
```

Inspect before running:

```bash
execraft orchestrate status --project sample --task-id feature_auth
execraft orchestrate explain --project sample --task-id feature_auth
```

Run:

```bash
execraft orchestrate run --project sample --task-id feature_auth
```

Useful diagnostics:

```bash
execraft orchestrate trace --project sample --task-id feature_auth --limit 20
execraft orchestrate usage --project sample --task-id feature_auth
execraft orchestrate context \
  --project sample --task-id feature_auth --package-id implementation
```

If an execution agent or runtime is temporarily unavailable, normal orchestration can wait and
retry according to project policy. Genuine unsafe or unresolved conditions move
to an explicit operator or Supervisor recovery path.

See [`orchestration-data-flow.md`](orchestration-data-flow.md),
[`supervisor.md`](supervisor.md), and
[`recovery_playbooks.md`](recovery_playbooks.md).

## 8. Control scope and scheduling

Agents do not receive unrestricted write authority. The orchestrator validates
repository and path scope before accepting mutations, and it protects
cross-package isolation during retries and recovery.

Inspect a scope issue with:

```bash
execraft orchestrate scope \
  --project sample --task-id feature_auth --package-id implementation
```

Work Package policy and agent-profile preferences can be changed without editing live
state files:

```bash
execraft orchestrate policy \
  --project sample --task-id feature_auth --package-id implementation \
  --role implement --agent codex --agent claude
```

See [`scope-checks.md`](scope-checks.md) and
[`execution_policy.md`](execution_policy.md).

## 9. Use the local GUI

Launch the control center:

```bash
execraft gui
```

Open a specific task:

```bash
execraft gui --project sample --task-id feature_auth
```

The GUI is a local operator surface for onboarding, plan inspection,
orchestration, agent health, workspace changes, commits, logs, and recovery. It
does not replace the deterministic backend checks.

See [`gui-onboarding.md`](gui-onboarding.md),
[`gui-task-lifecycle.md`](gui-task-lifecycle.md),
[`gui-workbench.md`](gui-workbench.md), and
[`agent_console.md`](agent_console.md).

## 10. Synchronize long-running repositories

When a task must incorporate upstream work, insert an explicit synchronization
Work Package rather than merging ad hoc inside an agent session:

```bash
execraft task sync-before feature_auth \
  --project sample \
  --before deploy \
  --repositories backend frontend \
  --source-branch backend=feature/api-cleanup
```

The synchronization flow records divergence, conflict handling, verification,
and integration state as a durable transaction.

See [`repository-synchronization.md`](repository-synchronization.md).

## 11. Replan safely

Stage a change request without replacing the accepted task definition:

```bash
execraft task replan feature_auth \
  --project sample \
  --request "Split the migration into a compatibility phase and a cleanup phase"
```

Review the candidate before applying it. Replanning preserves provenance and
started-work constraints rather than silently rewriting the active plan.

See [`task-replanning.md`](task-replanning.md).

## 12. Complete and archive

Check readiness first:

```bash
execraft task complete feature_auth --project sample --check
```

Archive and close:

```bash
execraft task complete feature_auth --project sample --archive
```

Verify the resulting archive:

```bash
execraft archive list --project sample
execraft archive show feature_auth --project sample
execraft archive verify feature_auth --project sample
```

Workspace cleanup remains policy-gated after completion. See
[`task-completion.md`](task-completion.md) and
[`workspace-finalization.md`](workspace-finalization.md).

## Troubleshooting checklist

| Symptom | First check |
| --- | --- |
| Project is registered but not ready | `execraft project doctor <project-id>` |
| Execution agent is skipped or unavailable | `execraft agents status --project <project-id>` |
| Live agent/runtime call fails | `execraft agents doctor --project <project-id> --smoke-test` |
| Workspace looks stale | `execraft workspace refresh <task-id>` |
| Verification does not run | check `verification.yaml` and the selected profile |
| Orchestration is blocked | `execraft orchestrate explain --project <project-id> --task-id <task-id>` |
| Need exact prior agent activity | `execraft orchestrate trace --project <project-id> --task-id <task-id>` |
| Scope expansion is blocked | inspect [`scope-checks.md`](scope-checks.md) before accepting recovery |
| Task cannot close | `execraft task complete <task-id> --project <project-id> --check` |

## Advanced topics

Use the topic docs instead of extending this guide with implementation detail:

- [`context-budgeting.md`](context-budgeting.md) — context capsules and token budgets;
- [`execution_policy.md`](execution_policy.md) — execution-agent selection and scheduling;
- [`supervisor.md`](supervisor.md) — privileged recovery boundaries;
- [`recovery_playbooks.md`](recovery_playbooks.md) — deterministic recovery paths;
- [`repository-synchronization.md`](repository-synchronization.md) — upstream integration;
- [`task-replanning.md`](task-replanning.md) — safe plan revision;
- [`ci-and-maintenance.md`](ci-and-maintenance.md) — contributor and CI workflow;
- [`application-boundaries.md`](application-boundaries.md) — maintained extension seams;
- [`architectural-invariants.md`](architectural-invariants.md) — non-negotiable design constraints.
