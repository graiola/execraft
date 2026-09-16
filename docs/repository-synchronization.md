# Repository synchronization Work Packages

Long-running Execraft tasks can diverge substantially from the authoritative branches they started from. Repository synchronization Work Packages provide an explicit, reviewable convergence point **inside** the task graph without rebasing or rewriting task history.

A synchronization Work Package is not final integration. Final integration moves task history toward the authoritative integration branch; repository synchronization moves a pinned authoritative upstream commit into the task branch so later Work Packages develop against fresher dependencies.

## Safety model

Repository synchronization is deterministic-first:

1. Execraft validates every selected repository is `task_owned`, on the exact `TASK.yaml` task branch, clean, and free of another Git operation.
2. It takes a repository-level ref lock shared across tasks using the same Execraft state root.
3. It fetches the declared remote branch and resolves `FETCH_HEAD` to an immutable commit SHA.
4. **All** selected source SHAs and target checkpoints are persisted before any task worktree is mutated.
5. Execraft runs `git merge --no-commit --no-ff <pinned-sha>` itself.
6. Clean candidates skip implementation-agent work and proceed directly to verification/review.
7. Conflicted candidates may invoke the dedicated `ai-repository-sync` skill. The agent may edit files but is forbidden from changing Git state. Execraft checks conflict markers and stages the candidate itself.
8. Verification freezes the exact candidate Git tree. Any staged, unstaged, or untracked mutation after that point blocks commit.
9. Independent review is required by default.
10. Execraft creates explicit merge commits and records the upstream SHA and resulting task SHA as durable evidence.

Automatic rebase is intentionally unsupported. Existing task commits may already be referenced by package evidence, review artifacts, invocation records, archives, and commit journals; rewriting those commits would invalidate lifecycle provenance.

## Inserting a sync before a Work Package

Use the versioned replanning path rather than editing the accepted graph directly:

```bash
execraft task sync-before feature_auth \
  --before deploy \
  --repositories infrastructure backend frontend \
  --source-branch backend=feature/api-cleanup
```

This stages a candidate revision. Review the normal replanning diff/impact report, then apply it:

```bash
execraft task replan feature_auth --candidate <candidate-id> --apply
```

Or stage and apply in one explicit command:

```bash
execraft task sync-before feature_auth \
  --before deploy \
  --repositories infrastructure backend frontend \
  --source-branch backend=feature/api-cleanup \
  --apply
```

When `--repositories` is omitted, Execraft uses the target Work Package's `affected_repositories`. When `--source-branch` is omitted for a selected repository, the source branch comes from that repository's `TASK.yaml base_branch`; Execraft does **not** assume `master` globally.

The graph transformation transfers the target's previous dependencies to the new sync package and rewires the target to depend on that package:

```text
prepare -> deploy

becomes

prepare -> deploy-sync -> deploy
```

Completed package IDs and evidence are not rewritten. The transformation is a normal versioned task-definition revision.

## PLAN.graph.yaml representation

```yaml
- id: deploy-sync
  title: Synchronize upstream repositories before deploy
  kind: repository_sync
  dependencies: [prepare]
  affected_repositories:
    - infrastructure
    - backend
    - frontend
  repository_sync:
    strategy: merge
    remote: origin
    conflict_policy: ai_resolve
    require_independent_review: true
    repositories:
      infrastructure: {}
      backend:
        source_branch: feature/api-cleanup
      frontend: {}
  risk: high
  verification_profile: integration
```

`repository_sync.repositories` must exactly match `affected_repositories`. Sync packages cannot be decomposed or sharded because the durable Git transaction is already the atomic orchestration unit.

## Inspecting divergence and transaction state

```bash
execraft orchestrate sync \
  --project sample \
  --task-id feature_auth \
  --package-id deploy-sync
```

This uses existing remote-tracking refs and does not fetch. To refresh the authoritative source first:

```bash
execraft orchestrate sync \
  --project sample \
  --task-id feature_auth \
  --package-id deploy-sync \
  --refresh
```

The report includes source branch/SHA, target SHA, merge base, ahead/behind counts, dirty/Git-operation state, and the durable synchronization transaction when present.

The Plan workbench exposes the same information lazily. Routine dashboard polling does not fetch upstream refs or execute deep Git inspection.

## Transaction and crash recovery

A transaction is stored under the task orchestration state directory:

```text
<state>/projects/<task>/repository-sync/<package>-<hash>.yaml
```

It records every source SHA before mutation. Until the first merge commit is created, the transaction is rollback-capable. After the first repository commits, it becomes **forward-only**: later recovery must finish the remaining repositories and may not reset already-recorded task history.

Normal `execraft orchestrate run` resumes prepared merge state and reconciles a merge commit that was created immediately before a crash but not yet written to the journal.

Before the first commit, an operator can explicitly abort the synchronization candidate:

```bash
execraft orchestrate sync \
  --project sample \
  --task-id feature_auth \
  --package-id deploy-sync \
  --rollback
```

Rollback holds the same driver/orchestrator locks as execution, runs `git merge --abort` only for Execraft's pinned merge state, restores the package to `PREPARE`, clears stale verification/review acceptance state, and reopens `HUMAN_REQUIRED` orchestration to `RUNNING`. It is refused after any repository merge commit exists.

The dashboard shows a rollback action only while the transaction is still rollback-capable. Forward-only transactions are shown as resume-only.

If `conflict_policy: human` is used, edit the conflicted product files but do not stage them. Then ask the control plane to validate and stage the resolution:

```bash
execraft orchestrate sync \
  --project sample \
  --task-id feature_auth \
  --package-id deploy-sync \
  --accept-resolution
```

The dashboard exposes the same **Accept edited resolution** action while a rollback-capable transaction contains conflicts. The control plane rejects unrelated workspace ownership violations and leftover textual conflict markers before staging.

## AI conflict resolution

The dedicated `ai-repository-sync` skill is intentionally narrow. The AI may resolve textual or semantic compatibility conflicts in package-owned files, but it must not run state-changing Git commands, including merge, abort, add, reset, checkout, switch, rebase, cherry-pick, commit, or push.

The handoff includes bounded transaction/conflict context instead of the complete task history, preserving the token-budget architecture. After edits, Execraft:

- rejects leftover conflict markers;
- owns `git add -A`;
- checks the index contains no unmerged paths;
- runs configured verification;
- freezes the exact verified tree;
- requires independent review unless explicitly disabled in the package spec;
- rejects any candidate mutation after verification.

## Divergence policy

Divergence measurement is always available through explicit inspection. Optional scheduling policy can warn or block ordinary Work Package starts:

```yaml
scheduling:
  repository_sync:
    divergence:
      warn_behind_commits: 10
      require_sync_behind_commits: 30
      refresh_before_check: false
    automatic_merge: false
```

The default thresholds are disabled for backward compatibility. A warning is journaled but does not stop work. A required threshold transitions the task to `HUMAN_REQUIRED` and recommends inserting a sync Work Package.

`automatic_merge: true` is rejected. A divergence threshold can never silently mutate source history.

`refresh_before_check: true` performs a fetch before each checked Work Package start and should be enabled only when that network behavior is desired. Otherwise the Check uses local remote-tracking refs and fails closed only when a required synchronization threshold is configured and cannot be evaluated.

## Multi-task behavior

Repository-level fetch/ref locks are keyed by Git common-directory identity and live at the shared Execraft state root. This matters because different task worktrees can share one underlying clone and therefore share `refs/remotes/*`.

Repository synchronization uses authoritative repository branches. It does not implicitly merge one active task branch into
another active task branch. Cross-task integration should remain an explicit dependency/integration decision rather than
creating hidden task coupling.

## Lifecycle integration

Repository-sync transactions participate in the existing lifecycle checks:

- Replanning is blocked while a sync transaction is pending.
- Workspace destruction is blocked while a sync transaction is pending.
- Task archival/completion is blocked while a sync transaction is pending.
- Completed sync transaction journals are copied into the immutable task archive.
- Sync evidence is persisted in package implementation state and the event journal and therefore becomes available to later package context capsules without replaying raw Git logs.

This keeps synchronization consistent with the same provenance, cleanup, and token-usage guarantees as normal Execraft work.

## Work Package Pause & Sync

The Work Package **Details** inspector exposes the same synchronization engine directly, and eligible Graph cards may surface the action as a shortcut. It does **not** let the browser run Git commands
and it does not merge underneath a running provider. The action writes a durable Work Package directive; the orchestration
driver consumes that directive only at a deterministic safe boundary and then releases its locks before the normal replanning
transaction inserts the synchronization package.

For an unstarted Work Package, the inspector shows **Sync before**. The selected Work Package is never started until its prerequisites are complete and the synchronization request reaches its boundary:

```text
prepare -> deploy

operator: Sync before deploy

prepare -> deploy-sync -> deploy
```

For a Work Package that has already started, the inspector shows **Pause & Sync**. Execraft lets that Work Package finish normally, commits/finalizes its work, pauses before downstream packages can start, and then inserts the synchronization barrier after it:

```text
deploy (running) -> release

operator: Pause & Sync

deploy (complete) -> deploy-sync -> release
```

The synchronization package is prioritized over unrelated ready development packages once the barrier has been accepted, preventing ordinary scheduler work stealing from delaying the requested convergence point.

### Selecting source branches

The dialog lists task-owned repositories and their remote branches. The **target branch is never editable**: it is always the repository's `TASK.yaml task_branch`. Only the upstream source branch can be changed.

The configured `TASK.yaml base_branch` is selected by default. Choosing another remote branch is allowed, but the dialog displays an explicit non-base warning and the resulting transaction records:

- `configured_base_branch`;
- the selected `source_branch` and pinned source SHA;
- `source_selection: operator_override`.

This makes intentional feature/release-branch dependencies auditable. Selecting a branch never passes a mutable branch name
to the eventual merge: the synchronization service still fetches and pins the exact source commit before worktree mutation.

**Refresh remote branches** uses `git ls-remote --heads` under the shared repository-ref lock. It does not change the task branch or broadly rewrite remote-tracking state. **Preview divergence** fetches/pins only the selected refs and reports ahead/behind counts against the fixed task branch.

### Resume policy

The card dialog provides two post-sync behaviors:

- **Resume automatically** — after the synchronization package passes merge, verification and review, normal development continues.
- **Stay paused** — Execraft installs a runtime-only one-shot `pause_after_completion` Hold on the generated synchronization package. The sync completes and is committed, then the project enters `operator_paused` for inspection before downstream development resumes.

The one-shot pause is orchestration state, not task-definition content, and is cleared when the operator resumes.

### Recovery and idempotency

Card requests are persisted in the flock-protected Work Package directive sidecar, including repository selection, branch
overrides, conflict policy and resume policy. A request can therefore survive a dashboard restart or driver crash. When a prior
run already reached the safe boundary, the next `execraft orchestrate run` detects the paused request, applies the task-definition
revision, and continues from the same durable intent rather than asking the browser to reconstruct it.

Repeated requests for the same card supersede the older pending directive. Generated synchronization IDs are collision-safe (`deploy-sync`, `deploy-sync-2`, ...), including IDs retained in task-definition history, so periodic synchronization does not reuse a historical package identity.

### Legacy task-definition compatibility

Older tasks may have a historical hybrid `PLAN.graph.yaml` where
orchestration fields such as `stage`/`status` and acceptance evidence such as
`verified`/`evidence` were stored beside the declarative package contract.
Current replanning intentionally rejects those fields in a *new* accepted revision.

Card-driven synchronization therefore projects historical hybrid graphs to the
modern declarative contract before constructing the replanning candidate. The
projection removes only fields known to be runtime-owned. Unknown fields are
preserved and are still rejected by current validation, so compatibility cannot silently drop
new or unsupported task semantics. Package completion used by **Pause & Sync**
dependency rewiring comes from the durable orchestration state rather than from
the legacy graph fields.

The source historical graph and its accepted revision snapshot remain in task
history; only the newly published revision is declarative. No Git fetch or merge
is attempted until that task-definition revision has validated and applied successfully.

New Execraft-generated tasks no longer emit runtime/evidence defaults in
`PLAN.graph.yaml`. Local and provider-generated planning output is normalized to
`id`, dependencies, requirements, acceptance criteria, repository scope and the
other declarative planning fields. Explicit imports of older graph files remain
supported for archival/migration compatibility and are projected safely when a
repository-sync revision needs to be created.
