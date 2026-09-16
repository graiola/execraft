# Persistence and lock hierarchy

All process-shared state uses `execraft.persistence`. Atomic writers create a
unique temporary file beside the destination, flush it, replace the destination,
and fsync the parent directory. Existing file permissions are retained and
symlink destinations are rejected.

Locks must be acquired from outermost to innermost and released in reverse:

1. `PROJECT_EXECUTOR`
2. `DRIVER`
3. `ORCHESTRATOR`
4. `LIFECYCLE`
5. `REPOSITORY_REF`
6. `PROJECT_COORDINATOR`
7. `RECORD`

Acquiring an earlier lock level while holding a later one fails before any
filesystem lock is attempted. Reacquiring the same resource in the same thread is
reported as busy rather than blocking recursively. Lock files are persistent and
must be regular files owned by the current user. Platforms without `fcntl`
advisory locking fail closed. Callers that cannot wait use `try_file_lock`;
bounded waits use `FileLock` with `timeout`. Read-only status surfaces use
`file_lock_is_held` instead of implementing their own advisory-lock probe. A
timeout never grants partial ownership.

Project roadmap persistence uses a project-specific `RECORD` lock under
`<state>/roadmap-locks/<project-id>.lock`. A roadmap operation does not acquire
repository/workspace/task lifecycle locks while holding this record lock. Task
execution state is projected after reads rather than mutated under the roadmap
lock, preserving the global lock order.

`PROJECT_COORDINATOR` is a narrow outer lock used only when one canonical
Roadmap gesture legitimately has to update both `PROJECT_EXECUTION.yaml` and a
Roadmap v2 layout document. It may acquire each independent `RECORD` repository
in sequence. Ordinary single-domain Roadmap or Project Execution writes do not
need this coordination lock.


Project Execution owns the new outer `PROJECT_EXECUTOR` level because an
Assisted/Automatic project action may call a Task operation that acquires
`DRIVER`. Project definition writes remain ordinary project-specific `RECORD`
writes and are never performed while holding a later Task lock.

Project Delivery does not add a new lock level. `delivery.json` candidate and
operation transactions use short `RECORD` locks. External `DeliveryProvider`
calls execute only after the durable intent transaction has released its lock;
provider latency therefore cannot hold the Project Execution filesystem record
lock or invert the Task lock hierarchy.
