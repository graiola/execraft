# Project Execution recovery

Project Execution keeps definition and runtime state in separate durable
domains. Definition and runtime JSON/YAML writes use Execraft atomic persistence
and process-shared locks.

The outer lock order is:

```text
PROJECT_EXECUTOR -> DRIVER -> ORCHESTRATOR -> LIFECYCLE -> REPOSITORY_REF -> PROJECT_COORDINATOR -> RECORD
```

An Assisted Task start follows:

```text
1. persist start intent
2. call TaskExecutionPort.start(task_id)
3. observe canonical Task state
4. persist resolved/failed/uncertain intent
5. append project-domain event
```

On restart, an unresolved intent is reconciled only from Task observation. If
the Task is running or terminal, the intent resolves. If the Task still appears
not started, the intent becomes `uncertain` and is never replayed automatically.
An explicit retry supersedes the old intent with a new auditable intent.

The project journal is append-only and fsync-backed. A crash-truncated final
JSONL record is ignored while earlier complete records remain readable.

## Automatic execution recovery

Automatic execution uses the same durable Task-start intents as Assisted mode. An
unresolved or `uncertain` start intent consumes a Task concurrency slot until Task
observation proves what happened. This deliberately favors duplicate-start safety
over filling every available slot after a crash. Automatic cycles never retry such
an intent blindly.

`automatic_cycle()` records cycle metadata only after its Task actions and refreshed
observations are durably reconciled. A foreground Automatic runner stores no
scheduler authority in memory: after process restart it can invoke another cycle,
which reconstructs capacity from canonical Task outcomes plus durable unresolved
intents.

A Task launch exception stops the current cycle. If Task state proves the launch
actually crossed its durable boundary, the intent resolves to the observed outcome;
otherwise it remains uncertain and later capacity remains reserved until an
operator deliberately resolves/retries it.

## Delivery recovery

Project Delivery uses a separate `delivery.json` state file in the same
Project Execution state directory. Candidate publication and operation-state
changes take the shared short-lived `RECORD` lock, but the external
`DeliveryProvider` call is always made **after** the intent has been persisted
and **without** holding that filesystem lock.

A delivery operation therefore follows:

```text
persist PENDING operation + stable operation_id
        ↓
DeliveryProvider.deliver(request)
        ↓
persist SUCCEEDED / FAILED
```

If the adapter raises after submission may have occurred, the durable operation
becomes `UNCERTAIN`. Restart/retry logic first calls
`DeliveryProvider.reconcile()` for the original operation ID. A missing or
ambiguous reconciliation result keeps the operation uncertain; it never causes
a second blind external submission. Terminal `SUCCEEDED` and `FAILED` records
are immutable.


## Roadmap / Project Execution definition coordination

Canonical Roadmap gestures that must update both `PROJECT_EXECUTION.yaml` and
Roadmap v2 layout use a separate durable coordination intent under:

```text
<state-root>/roadmap-coordination/<project>/pending.json
<state-root>/roadmap-coordination/<project>/journal.jsonl
```

`PROJECT_COORDINATOR` is an outer lock for this narrow operation and may invoke
the independent `RECORD` repositories. It does not replace either repository's
own lock. A crash after one document is written is recovered by comparing the
current revision and content digest with the recorded before/desired images.
Only exact known states are replayed. Divergence fails closed instead of
performing a best-effort rollback or overwriting newer work.

### Operator-visible coordination conflicts

A pending canonical Roadmap intent is exposed as typed coordination state instead
of making the project unreadable. The read-only diagnosis includes:

```text
operation_id / operation / phase
Roadmap: before | applied | divergent
Project Execution: before | applied | divergent
expected/current/desired revisions
recorded result revisions
created_at / updated_at
safe_actions
automatic_action_available
```

Reads still finish a known partial write automatically when the current durable
state exactly matches the recorded intent. If either side is `divergent`, reads
stop automatic recovery but continue to return the current Roadmap and Project
Execution projections with the conflict attached. Mutations in either domain are
blocked until the pending coordination state is resolved.

Resolution never accepts arbitrary replacement content and has no force/overwrite
operation. The coordinator derives the only admissible actions from the exact
persisted state:

```text
retry_roll_forward
    only when each side is exact before/applied and replaying recorded desired
    content cannot overwrite an unrecorded mutation

accept_applied
    only when both sides already match the recorded desired result

abort
    only when both sides still match the exact recorded before image

finalize_terminal
    only when a crash left an already-complete/already-aborted terminal intent
    awaiting journal/unlink cleanup; it performs no domain write
```

A genuinely divergent state exposes no destructive shortcut. The operator must
resolve the newer durable document through a normal revisioned edit; subsequent
inspection may then establish a provably safe state.

## Coordination forensics and manual convergence

Pending Roadmap coordination intents created after the R8 hardening tranche also
capture a **bounded semantic forensic record**. This record is diagnostic only;
it is not a second project definition and cannot be submitted back as replacement
content. It retains only the subjects changed by the coordinated operation:

```text
Roadmap
  item ID / kind / project_asset_id
  lane / order

Project Execution
  asset ID / kind / title
  canonical schedule
  bounded criterion/task/requirement counts
```

The Project Execution workspace can therefore compare three read-only images:

```text
recorded before
recorded desired
current durable state
```

Each domain also reports before/desired/current revisions and semantic content
digests. Pending intents written by older R6/R7 versions remain readable; when
no semantic forensic metadata exists, the UI states that limitation explicitly
and still shows revision/digest provenance.

A later normal revisioned edit can safely converge a previously divergent
document to the exact recorded desired semantic content. The coordinator reports
that state as `converged` when the digest is exact but the revision is newer than
the originally expected result revision. `converged` is not treated as an
unrecorded merge: it is accepted only because the full semantic digest equals the
persisted desired image. Both sides must still classify as desired-equivalent
(`applied` or `converged`) before `accept_applied` can finalize the intent.

The terminal coordination journal is exposed read-only and bounded to the newest
requested entries (maximum 100). Journal rows answer which operation completed or
aborted, when it was finalized, and which Roadmap / Project Execution revisions
resulted. Journal history never authorizes recovery and is not replay input.
