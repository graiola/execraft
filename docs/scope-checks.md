# Repository scope checks

`Execraft` uses repository and path scopes to keep generated shards attributable.
The Scope Check remains fail-closed for broad, destructive, protected, or cross-boundary
changes, but routine supporting edits no longer need to interrupt the workflow.

## Clean-start preflight

Before a serial write package invokes an agent, every task-owned repository must
be clean. The escalation records the concrete dirty paths, not only the repository
ID. This prevents pre-existing work from being attributed to the next agent and
avoids spending provider time before a deterministic precondition is satisfied.

Parallel write shards already run in isolated Git worktrees. Their delta is
computed from the recorded isolation baseline before it is applied to the source
worktree.

## Declared scope matching

Scope entries support:

- exact files, such as `repo/backend/api.py`;
- glob patterns, such as `repo/backend/tests/**`;
- existing directories, such as `repo/backend`, which recursively cover
  descendants.

Directory declarations were historically treated as exact filenames. The current
matcher resolves an existing directory as a recursive boundary, which removes the
need to enumerate every file below a deliberately scoped component.

## Bounded automatic expansion

Generated shards may add exact paths automatically when all applicable limits are
satisfied. The default safe categories are:

- tests and test support;
- `docs/verification/**` evidence;
- Python package registration metadata (`__init__.py`, `py.typed`);
- a source file in the same directory as an explicitly declared source file.

The expansion is exact-path only. It does not turn a newly observed file into a
broad wildcard. Every decision is persisted as `write_scope_auto_expanded` with
classification, changed-line count, and the admitted paths.

Configure the policy in `projects/<project-id>/agents.yaml`:

```yaml
scheduling:
  scope_policy:
    enabled: true
    max_files: 8
    max_changed_lines: 800
    allow_tests: true
    allow_verification_docs: true
    allow_package_metadata: true
    allow_adjacent_source: true
    deny_patterns:
      - .github/**
      - '**/.github/**'
      - PLAN.md
      - BRIEF.md
      - TASK.yaml
      - '**/pyproject.toml'
      - '**/security/**'
```

Protected files, cross-component changes, binary/oversized deltas, and changes
outside affected repositories are never admitted by this lightweight expansion.
They are passed to automatic workspace recovery when enabled, or left for an
explicit operator decision.

## Automatic workspace recovery

Workspace ownership failures use one recovery transaction. Set the single
user-facing switch in `projects/<project>/agents.yaml`:

```yaml
scheduling:
  automatic_recovery: true
  scope_recovery:
    max_resume_attempts: 2
    prefer_reviewer: true
    allow_implementer_fallback: true
    allow_cross_repository: true
    cleanup_untracked_artifacts: true
    cleanup_patterns:
      - __pycache__/**
      - '**/__pycache__/**'
      - '*.pyc'
      - '**/*.pyc'
      - .pytest_cache/**
      - '**/.pytest_cache/**'
      - .mypy_cache/**
      - '**/.mypy_cache/**'
      - .ruff_cache/**
      - '**/.ruff_cache/**'
    max_files: 64
    max_repositories: 8
    max_changed_lines: 10000
    max_excerpt_bytes: 120000
```

`automatic_recovery` controls both the initial Check and resume from a persisted
`human_required` Hold. The older nested `enabled` and `auto_resume` keys are
still parsed for compatibility, but new configurations should not use them.

The recovery transaction classifies the complete multi-repository dirty
workspace once and reuses that snapshot for reporting, agent handoff, Check
validation, retry fingerprints, and commit preparation. A dirty path is either:

- already owned by the package;
- outside the package's exact `write_scope`;
- in a configured repository not yet owned by the package;
- clean-start contamination; or
- owned by an active parallel sibling and therefore ignored by this package.

Recovery proceeds in this order:

1. The framework deletes only configured **untracked regular files and
   symlinks**, such as Python and test caches. It never deletes tracked content.
2. If candidates remain, a `fix_review`-capable agent receives every exact path,
   its relationship to the package, bounded excerpts, requirements, and
   acceptance criteria. The reviewer is preferred; the implementer can be used
   only when configured as a fallback.
3. The agent repairs accidental edits and returns an exact contract:
   `retain_paths` for legitimate changes and `discard_paths` for changes it
   restored or removed. Unknown paths, newly created candidates, protected
   retained paths, oversized deltas, and ambiguous output are rejected.
4. Retained paths acquire exact package ownership. When they belong to another
   configured repository, that repository is added to
   `affected_repositories`; broad directory or wildcard ownership is never
   inferred.
5. If ownership expands after review, final review, or `ready_to_commit`, the
   package is rewound to regression verification. A recovery reviewer is removed
   from subsequent reviewer roles so it cannot approve its own repair.
6. After verification, the configured review checks, and acceptance evidence pass, the
   orchestrator commits all owned repositories in its ordinary atomic
   multi-repository transaction. The recovery agent never commits, pushes, or
   changes branches.

The last rule is intentional: recovery is fully automatic from the operator's
perspective, while commit ownership remains centralized so commit journals,
branch guards, rollback, verification evidence, and no-push guarantees cannot be
bypassed by a provider CLI.

Persisted scope checks are retried through the same transaction. Retry budgets are
keyed to the package, stage, exact candidate paths, and their content digests. A
real workspace change therefore opens a fresh bounded attempt, while an
ineffective agent cannot loop forever. Provider quota or cooldown creates a
durable `waiting_for_agent` record that resumes the recovery stage itself rather
than rerunning implementation.

Clean-start remains the attribution boundary: generated untracked artifacts may
be removed automatically, but pre-existing tracked work cannot be silently
assigned to a package before that package starts. All other post-agent ownership
failures, including dirty configured repositories outside the original package,
are eligible when `allow_cross_repository` is enabled. Protected paths may be
restored by the fixer but can never be retained automatically.

## Inspecting a Scope Check

```bash
execraft orchestrate scope \
  --project sample \
  --task-id my_task \
  --package-id feature__api
```

The report includes:

- affected repositories and declared exact scope;
- ordinary write-scope violations;
- the complete unowned workspace candidate set and each relationship;
- candidate repositories, cleanup paths, retry fingerprint, and retry budget;
- category, changed-line count, and automatic-expansion eligibility;
- whether `Run / Resume` can invoke automatic workspace recovery.

When automatic recovery is available, the report prints the normal `orchestrate
run` command. After reviewing a remaining manual candidate set:

```bash
execraft orchestrate scope \
  --project sample \
  --task-id my_task \
  --package-id feature__api \
  --accept-scope
```

The command uses the same ownership mutation as automatic recovery: it adds only
the exact current paths, acquires explicitly selected configured repositories,
and resumes at verification. It does not invoke an implementation/fix agent.
Clean-start contamination cannot be adopted through this command.

## Reconciling a stale Scope Check

A scope escalation can become obsolete when the operator commits or restores the
changes, or when a previous recovery already completed the shard. The durable
project state may still be `human_required` even though:

- the referenced package is `completed`;
- the workspace is clean;
- current scope violations are zero.

Use either command:

```bash
execraft orchestrate scope \
  --project sample \
  --task-id my_task \
  --package-id feature__api \
  --reconcile-scope
```

or the backwards-compatible `--accept-scope`. When there is nothing left to
approve, `--accept-scope` performs reconciliation instead of failing with a stage
error.

`orchestrate run` also reconciles this narrow stale condition automatically under
the exclusive orchestration lock. Reconciliation distinguishes the Check that
originally failed:

- a **clean-start** Check still requires the whole task workspace to be clean;
- a post-agent **write-scope** Check may retain dirty files inside the package's
  affected repositories when every path is covered by the declared scope. This
  is the expected state at `ready_to_commit`, and the framework resumes directly
  into its framework-owned commit transaction;
- dirty configured repositories outside the package scope enter the automatic
  workspace-recovery transaction when cross-repository recovery is enabled;
- repositories owned by active parallel siblings remain isolated and are never
  acquired by another package.

It never auto-resumes verification failures, product decisions, protected paths
that remain dirty, unknown repositories, or genuinely unresolved tracked
clean-start contamination.

The GUI displays **Recover workspace / Resume** for an eligible active Scope Check/Hold and
**Reconcile / Resume** only when no recovery work remains. The CLI remains
authoritative and refuses the run if the workspace condition is still present.

When the Check/Hold is caused by legitimate uncommitted changes, the GUI **Changes**
view can inspect exact diffs, commit only selected paths, and optionally ask a
read-only review agent to propose the commit message. Committing never bypasses
scope reconciliation: return to **Run** and resume so the CLI can revalidate
the now-current workspace under its exclusive lock. See
[`gui_workspace_commits.md`](gui_workspace_commits.md).

## Relevant durable events

- `write_scope_auto_expanded`: bounded safe exact paths were admitted;
- `write_scope_expansion_approved`: an operator approved remaining exact paths;
- `scope_recovery_artifacts_removed`: configured untracked artifacts were deleted;
- `scope_recovery_started`: a fixer received the bounded recovery handoff;
- `scope_recovery_completed`: the workspace and exact scope were resolved;
- `scope_recovery_incomplete`: the bounded attempt left violations for an operator;
- `scope_recovery_resume_started`: a persisted scope check entered one bounded
  automatic retry;
- `scope_recovery_resume_failed`: the retry returned without resolving the Check/Hold;
- `scope_recovery_wait_scheduled`: provider availability delayed the current
  recovery attempt without losing its stage or candidate set;
- `repository_scope_check_reconciled`: a stale scope/clean-start escalation was
  cleared after revalidation.
