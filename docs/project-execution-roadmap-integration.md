# Project Execution and Roadmaps

Roadmaps are planning/view surfaces; Project Execution is the canonical project
control graph. Roadmap schema v2 therefore stores only references for canonical
Project assets.

```yaml
schema_version: 2
items:
  - id: roadmap-node-g1
    kind: gate
    project_asset_id: integration-ready
    lane: Payload
    order: 30
```

A v2 `phase`, `gate`, or `milestone` node does not persist title, description,
criteria, canonical schedule, lifecycle state, or Milestone baseline. Those are
projected from `PROJECT_EXECUTION.yaml` and Project Execution runtime. Task nodes
continue to reference `task_id`; `planned_task` remains roadmap-local.

## v1 migration

Migration is explicit and previewable. Each v1 Phase/Gate/Milestone is promoted
to a canonical Project asset, and its Roadmap node is replaced by a
`project_asset_id`. Equivalent titles are never guessed to be the same identity.
Generated IDs are allocated deterministically from Roadmap ID + item ID across
all v1 Roadmaps, independent of migration order.

A prepared migration marker records source revision/digest and the complete
identity map before canonical assets are created. If the process dies between
asset creation and Roadmap rewrite, rerunning resumes that identity map. If the
source Roadmap changed out-of-band after preparation, migration fails closed.
Existing partially created assets must exactly match the prepared definition.

Legacy planning-only Gates are promoted conservatively with a human-approval
criterion; migration must not silently turn planning metadata into execution
authorization.

## P11 editing contract

Roadmap v2 projections include `project_execution_revision`. When the operator
renames or reschedules a canonical Phase/Gate/Milestone from the Roadmap canvas,
the browser writes the canonical Project Execution asset under that revision and
then reloads the Roadmap projection. The Roadmap node continues to persist only
`project_asset_id`, lane and order.

Stale Project Execution revisions fail closed rather than allowing an old
Roadmap browser tab to overwrite newer canonical project-control data. Typed
metadata update endpoints preserve Gate criteria, Phase boundaries and Milestone
requirements/delivery policy while changing presentation metadata or schedule.

## Coordinated canonical Roadmap mutations

A Roadmap v2 gesture can legitimately touch two durable documents when it
changes both canonical Project-asset schedule and Roadmap-local presentation
metadata. For example, moving a Gate can change:

```text
PROJECT_EXECUTION.yaml     canonical Gate target
roadmaps/<id>.yaml         lane / order only
```

These writes are coordinated by `RoadmapCanonicalCoordinator`. The coordinator
does **not** merge ownership: Project Execution still owns Phase/Gate/Milestone
business state and Roadmap still owns planning/view layout.

The crash-safe flow is:

```text
validate exact Roadmap + Project Execution revisions/content
        ↓
persist roadmap-coordination/<project>/pending.json
        ↓
write PROJECT_EXECUTION.yaml
        ↓
persist observed Project Execution revision
        ↓
write Roadmap v2 layout
        ↓
persist observed Roadmap revision
        ↓
append compact coordination journal record + clear pending intent
```

Recovery classifies each durable side as `before`, `applied`, or `divergent`
using both optimistic revision and a semantic SHA-256 content fingerprint. It
only rolls forward from the exact recorded `before` image. A side that changed
outside the intent is never overwritten automatically; reconciliation fails
closed and leaves the pending intent available for diagnosis.

Roadmap reads and Project Execution GUI context creation both attempt pending
coordination recovery, so either project workspace may be the first surface
opened after process restart.

## Coordination observability and safe resolution

The Project Execution API exposes the persisted Roadmap/Project Execution
coordination state at:

```text
GET  /api/project-execution/coordination?project_id=<project>
POST /api/project-execution/coordination/resolve
```

The GET operation is read-only and never performs recovery writes. Project
workspace snapshots also include a `coordination` object. Safe, non-divergent
partial writes may be completed automatically while normal project views load;
genuine divergence remains visible in both Roadmap and Project Execution.

The Project Execution workspace shows the operation ID, operation type, per-domain
state and revisions, timestamps, and any typed safe action. The Roadmap shows a
compact conflict notice and directs the operator to Project Execution for the full
diagnosis.

There is deliberately no generic `force`, overwrite, hidden merge, or speculative
rollback action. A pending divergent coordination intent blocks further Roadmap
and Project Execution mutations so subsequent work cannot compound an ambiguous
cross-domain state.

## R8 forensic coordination inspection

Project Execution adds a read-only forensic surface for the rare case where a
pending canonical Roadmap mutation cannot be reconciled automatically. The
pending status now includes bounded semantic `forensics` metadata with:

```text
Before | Recorded desired | Current
```

for both Roadmap-local placement and canonical Project Execution asset metadata.
The UI does not expose raw desired documents, arbitrary mapping editors, merge
expressions, or force/overwrite controls.

Recent terminal coordination history is available separately at:

```text
GET /api/project-execution/coordination/history
    ?project_id=<project>
    &limit=<1..100>
```

Keeping history separate from the normal workspace snapshot bounds payload size
and makes it explicit that journal entries are audit information, not current
execution state.

Manual convergence is recognized by semantic identity rather than by pretending
later revision numbers are the original coordinated write. A document whose
content digest exactly equals the persisted desired image at a later revision is
classified `converged`; any other later revision remains `divergent`. The same
coordinator lock and server-derived `safe_actions` checks apply before a pending
intent can be finalized.
