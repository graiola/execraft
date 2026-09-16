# Package workspace finalization

Work-package completion has a deterministic workspace barrier after final review
and acceptance evidence. The barrier is framework-owned; it is not an agent or
Supervisor cleanup task.

## Standard Work Packages and shards

For an ordinary package or `standard_shard`, `ready_to_commit` now performs:

1. remove only allow-listed **untracked** verification/runtime artifacts;
2. validate repository and exact write-scope ownership;
3. create or recover the durable multi-repository commit transaction;
4. perform automatic Git commits when `commit.mode: automatic`;
5. assert the package workspace is clean, excluding repositories still owned by
   active parallel siblings;
6. record `package_workspace_finalized` and mark the package completed.

Tracked files and unknown untracked files are never deleted by finalization.
A Git hook or later process that dirties the repository after `git commit` is
detected before the package can complete.

## Parallel shards and aggregate parents

Parallel write shards still execute in isolated worktrees and apply their deltas
to the task worktrees. Each `standard_shard` then verifies, reviews, and creates
its own commit transaction.

The aggregate parent does not create a duplicate implementation commit. If a
post-shard `fix_review` legitimately changes source, however, Execraft records the
exact dirty paths and content fingerprints at the fixer boundary and creates one
**aggregate repair commit** containing only that retained fixer delta. This is a
new ownership transaction, not a replay of child implementation commits.

Before completion the aggregate requires:

- every referenced child shard to exist and be completed;
- every completed `standard_shard` to have a committed transaction when
  automatic commits are enabled;
- review-only shards to be completed, without requiring a commit;
- no active parallel wave or repository ownership for its children;
- deterministic cleanup of allow-listed untracked artifacts;
- an exact fingerprint match before committing any recorded post-shard review-fix
  delta;
- a clean workspace across all configured task repositories.

Success records `aggregate_workspace_finalized`. When an aggregate repair commit
is needed, `aggregate_review_fix_delta` records the ownership boundary and
`aggregate_review_fix_committed` records its transaction. Tasks that were already
in flight before this evidence existed may recover it only when the current
package workspace digest exactly equals the durable `workspace_after_digest` of
the last completed `fix_review` invocation; recovery is journaled as
`aggregate_review_fix_delta_recovered`.

Any remaining tracked or unknown untracked path records
`workspace_finalization_failed` and stops before the aggregate is marked
completed.

## Supervisor boundary

Finalization failures are not Supervisor incidents by default. A Work Package must
not rely on a privileged AI cleanup pass to establish its own commit boundary.
The operator receives exact child-transaction and dirty-path evidence instead.
Projects may explicitly opt into Supervisor repair, but this is discouraged:

```yaml
scheduling:
  workspace_finalization:
    allow_supervisor_repair: false
```

## Configuration

The default policy is enabled even when the section is omitted:

```yaml
scheduling:
  workspace_finalization:
    enabled: true
    cleanup_untracked_artifacts: true
    require_clean_after_automatic_commit: true
    require_aggregate_child_commits: true
    allow_supervisor_repair: false
    cleanup_patterns:
      - __pycache__/**
      - '**/__pycache__/**'
      - .pytest_cache/**
      - '**/.pytest_cache/**'
      - .mypy_cache/**
      - '**/.mypy_cache/**'
      - .ruff_cache/**
      - '**/.ruff_cache/**'
      - htmlcov/**
      - '**/htmlcov/**'
      - playwright-report/**
      - '**/playwright-report/**'
      - CMakeFiles/**
      - '**/CMakeFiles/**'
      - CMakeCache.txt
      - '**/CMakeCache.txt'
```

Boolean values are strictly typed. Quoted values such as `enabled: "false"` are
rejected.

Broad directories such as `build/**`, `install/**`, and `log/**` are not default
cleanup patterns because those names may contain intentional files. A project
may add them only after confirming they are disposable in every registered
repository.

## Durable events

The event journal may contain:

- `workspace_finalization_artifacts_removed`
- `package_workspace_finalized`
- `aggregate_review_fix_delta`
- `aggregate_review_fix_delta_recovered`
- `aggregate_review_fix_committed`
- `aggregate_workspace_finalized`
- `workspace_finalization_failed`

The commit journal remains authoritative for child, ordinary package, and
post-shard aggregate repair transactions. Aggregates with no repair delta retain
`transaction_id: null`; aggregates that own a review-fix delta record the repair
transaction ID.
