# Project roadmaps

Project roadmaps are the planning layer above executable Execraft tasks. They let
an operator arrange project work on an interactive timeline without creating a
second task database or a second scheduler.

The ownership hierarchy is intentionally explicit:

```text
Project
├── roadmaps/*.yaml             project-level planning views
└── tasks/<task-id>/
    ├── TASK.yaml               canonical task identity and lifecycle
    ├── BRIEF.md
    ├── PLAN.md
    └── PLAN.graph.yaml         executable task-internal work-package graph
```

A roadmap may point at a real task, but it never owns that task's title, status,
repository scope, plan, or runtime state. Those values are projected from the
canonical task dossier when the roadmap is read.

## Invariants

1. **Roadmaps are planning-only.** A roadmap relation does not start, pause,
   unblock, schedule, or control the orchestrator. `PLAN.graph.yaml` remains the
   executable dependency graph inside a task.
2. **Tasks are referenced, not copied.** A linked `task` item persists only
   `task_id` plus roadmap-local schedule/lane/order metadata. Display title,
   lifecycle status, repository scope, current-task state, and runtime progress
   are joined at read time.
3. **Removing planning never removes work.** Removing an item from a roadmap or
   deleting an entire roadmap leaves linked task dossiers untouched.
4. **Permanent task removal is fail-safe.** A task referenced by an active
   project roadmap cannot be permanently deleted until those references are
   removed. Reversible task archive is allowed; the roadmap then displays the
   task as archived.
5. **Edits are conflict checked.** Every durable mutation carries the revision
   observed by the browser. A stale edit is rejected instead of overwriting a
   newer one.
6. **One task appears at most once per roadmap.** The same task may appear on
   different roadmaps, but duplicate links inside one roadmap are rejected.
7. **`blocks` relations are acyclic.** `related` links are informational and do
   not participate in cycle validation.

These rules make a roadmap a view over real work rather than an alternate
source of truth.

## Item kinds and schema v2

Roadmap schema version 2 supports five item kinds:

- `task` — reference to an existing canonical Execraft Task.
- `planned_task` — roadmap-local future work that may later become a Task.
- `milestone` — reference to a canonical `ProjectMilestone`.
- `phase` — reference to a canonical `ProjectPhase`.
- `gate` — reference to a canonical `ProjectGate`.

For Phase/Gate/Milestone nodes, v2 persists `project_asset_id` plus view-only
metadata (`lane`, `order`). Canonical title, description, schedule, state, Gate
criteria, and Milestone baseline come from Project Execution. Roadmap scheduling
for canonical project assets is therefore not duplicated. Task scheduling remains
planning-only and cannot affect Project Executor eligibility.

## Persistence

Each roadmap is a standalone versioned YAML document:

```text
<project>/roadmaps/<roadmap-id>.yaml
```

Example:

```yaml
schema_version: 2
id: platform-2026
title: Platform 2026
revision: 4
created_at: '2026-09-09T08:00:00+00:00'
updated_at: '2026-09-09T08:30:00+00:00'
items:
  - id: task-auth-1
    kind: task
    task_id: feature_auth
    lane: Platform
    order: 10
    schedule:
      start: '2026-09-15'
      target: '2026-09-30'
  - id: milestone-beta
    kind: milestone
    project_asset_id: platform-beta
    lane: Platform
    order: 20
relations:
  - from: task-auth-1
    to: milestone-beta
    kind: blocks
```

Linked Task display data is never copied. Likewise, canonical Project asset
business data is absent from Roadmap v2. Persisting either would allow two
sources of truth.

Roadmap writes use `execraft.persistence.atomic.atomic_write_yaml`. The repository
uses one project-specific `RECORD` lock under the state home:

```text
<state>/roadmap-locks/<project-id>.lock
```

The project descriptor and Task dossiers are never held open while that record
lock is acquired.

## Optimistic concurrency

Each roadmap begins at revision `1`. A mutation submits `expected_revision`.
Under the roadmap lock the repository reloads the current document and accepts
the write only if both revisions match. The successful write increments the
revision before publishing it atomically.

This protects against two browser tabs silently overwriting one another. The
GUI refreshes after a stale-write response and asks the operator to reapply the
intended change to current data.

The GET projection also includes a SHA-256 digest for inspection/provenance.
Revision is the mutation token; the digest is not used as a second lock token.

## Read projection

`RoadmapProjection` builds the browser-facing read model by joining roadmap
metadata with task state:

- active task dossiers from `<project>/tasks`;
- archived task dossiers from
  `<control>/projects/.archive/tasks/<project-id>`;
- the current GUI task identity;
- orchestration progress from the task runtime `state.json` when present.

A linked task therefore exposes values such as:

```json
{
  "task": {
    "id": "feature_auth",
    "title": "OIDC authentication",
    "status": "in_progress",
    "availability": "active",
    "repositories": ["backend", "frontend"],
    "runtime_state": "running",
    "completed_packages": 3,
    "total_packages": 5,
    "progress_percent": 60
  }
}
```

If a referenced dossier is archived, `availability` becomes `archived`. If a
roadmap was manually edited to reference a task that no longer exists, the item
is retained and projected as `missing` instead of disappearing silently.
Malformed task manifests are isolated to that task row and reported as
`invalid`/`unreadable`; they do not make the complete roadmap unusable.

The projection also returns active tasks not linked to the selected roadmap as
`unscheduled_tasks`. Archived tasks are not offered as new unscheduled work.

## GUI interaction

The project workspace is roadmap-first: selecting a project lands directly on
**Roadmap**. The roadmap canvas is designed around direct manipulation rather
than form editing. Dates, row placement, and dependencies are inferred from
visual gestures.

Primary gestures:

- **Drag a bar horizontally** to move its whole schedule. Start and target move
  together, so the planned duration is preserved.
- **Drag a block vertically** to reorder rows or move it into another visible lane.
  The backend performs one atomic semantic move and re-normalises order values.
- **Drag the ⠿ handle on an item row** to swap/reorder the complete row without
  changing its timeline dates. This is useful when only visual execution order
  should change.
- **Drag the ⠿ handle on a lane header** to reorder complete swimlanes. Every
  item in the lane moves as a group, its internal item order and schedule are
  preserved, and a live insertion line shows whether the lane will land before
  or after the target lane. Lane order remains derived from item order; there is
  no second persisted lane-order table that could drift.
- **Drag either bar edge** to resize start/target dates. Selected bars keep the
  handles visible; focused handles also support `←` / `→` in one-day increments
  and `Shift+←` / `Shift+→` in seven-day increments.
- For Task and Planned Task nodes, the **Selection** panel exposes exact Start,
  Target, and Duration controls. Duration is inclusive (`11 Sep → 24 Sep = 14
  days`) and changing it preserves Start while recomputing Target. Duration is
  derived from the two canonical dates and is never persisted as a third field.
- **Drag the right connector dot onto another block's left connector dot** to
  create a directional `blocks` dependency. Invalid cycles are rejected by the
  domain model.
- **Click a dependency path** to select it; the canvas exposes a compact visual
  delete affordance at the connector midpoint.
- **+ Block** opens a compact visual palette for planned tasks, gates, milestones,
  phases, and the existing-task tray. Selecting a new non-task block type enters
  placement mode; the next timeline click places the block at that date/row.
- Newly placed non-task blocks immediately enter **inline rename** mode. No
  schedule/lane/dependency form is required. Double-click or `F2` later repeats
  inline rename.
- **Drag an unscheduled task card** from the block tray directly to its desired
  date/row to link it.
- **Month / Quarter / Year** change timeline scale and **Today** recenters the
  viewport.

Clicking an active linked task still opens its canonical task dashboard. The
selection HUD remains intentionally compact: it exposes contextual actions and,
for Task/Planned Task nodes, one planning-only schedule editor. That editor may
change Roadmap dates/duration but never Task execution eligibility, ProjectGate
state, or Task orchestration. Planned items, gates, milestones, phases, and
unavailable task references select in place because they have no active task
dashboard.

The canvas provides explicit drag targets, snap-row highlighting, connector
hover states, live move/date feedback, gate/milestone shapes, task progress, and
a post-drag click suppression guard. A horizontal-only move preserves vertical
order; only a genuine row change invokes the atomic move operation.

Roadmap zoom/scroll state is retained across `roadmap → task → roadmap`
navigation. The task dashboard exposes **← <project> roadmap** for the return
path.

The frontend remains framework-free. Geometry helpers live in
`roadmap-interactions.js`, while `roadmap-view.js` owns orchestration of DOM and
API interactions. Date geometry uses UTC day indexes to avoid daylight-saving
drift in date-only planning data.

## Planned item to canonical task

A `planned_task` is intentionally cheap to create. Selecting **Create Execraft
task** does not create a special roadmap-owned task implementation.

The browser instead:

1. switches to the existing project **Tasks** composer;
2. pre-fills the task description from the planned item;
3. executes the canonical `/api/onboarding/start/preview` flow;
4. executes `/api/onboarding/start/apply` only when the normal preview is
   applicable and acknowledged;
5. after the real task exists, calls the roadmap link endpoint to convert the
   original roadmap item in place from `planned_task` to `task`.

The roadmap item ID, lane, order, and schedule survive conversion. The title and
description stop being persisted by the roadmap because the new `TASK.yaml`
becomes authoritative.

If another tab changes the roadmap while task creation is in progress, the task
creation still succeeds but the stale roadmap conversion is rejected. The GUI
reports that the canonical task exists but could not be linked automatically;
it does not roll back or delete valid task work merely to satisfy a planning
view.

## Archive and permanent deletion

Roadmaps distinguish reversible catalog lifecycle from destructive removal:

- Archiving a linked task is allowed. Its roadmap point remains and is projected
  with `availability: archived`.
- Reactivating the task automatically makes the same link active again; no
  roadmap rewrite is necessary.
- Removing a roadmap item affects only that roadmap.
- Deleting a roadmap requires explicit acknowledgement and reports
  `tasks_deleted: 0`.
- Permanent deletion of an individual task checks project roadmaps first and is
  rejected while references remain.
- Permanent deletion of an entire project owns the project-level destruction
  transaction, including its roadmap files, so separate item detachment is not
  required.

This prevents a visually convenient planning action from becoming an accidental
destructive task action.

## Relationships

Supported relation kinds are:

```text
blocks
related
```

`blocks` is directional and must remain acyclic. `related` is informational.
Both are **planning metadata only** in schema version 2.

In particular, this is intentionally false:

```text
roadmap blocks relation => orchestrator execution control
```

Cross-Task control lives in the explicit `PROJECT_EXECUTION.yaml` contract; Roadmap relations never become execution dependencies implicitly.

## HTTP API

Reads:

```text
GET /api/roadmaps?project_id=...
GET /api/roadmap?project_id=...&roadmap_id=...
GET /api/roadmap/migration/preview?project_id=...&roadmap_id=...
```

Mutations:

```text
POST /api/roadmap/create
POST /api/roadmap/metadata/update
POST /api/roadmap/item/upsert
POST /api/roadmap/item/delete
POST /api/roadmap/item/move
POST /api/roadmap/lane/move
POST /api/roadmap/item/link-task
POST /api/roadmap/relation/upsert
POST /api/roadmap/relation/delete
POST /api/roadmap/delete
POST /api/roadmap/migrate-v1
```

Roadmap mutation payloads use strict JSON types. In particular,
`expected_revision` and `order` must be JSON integers and `acknowledged` must be
a JSON boolean; string lookalikes such as `"3"` or `"true"` are rejected.

All route dispatch remains HTTP-independent under `execraft.gui.routes.roadmaps`.
The server is transport-only and merely composes that route group.

## Module ownership

Backend:

```text
execraft.roadmap.models       schema and validation
execraft.roadmap.repository   lock, atomic YAML, revision checks
execraft.roadmap.projection   Task + Project Execution read-model joins
execraft.roadmap.migration    deterministic v1 -> v2 promotion/recovery
execraft.roadmap.service      roadmap application operations
execraft.gui.routes.roadmaps  HTTP-independent GUI dispatch
```

Frontend:

```text
assets/gui/roadmap-view.js         canvas composition and API interaction
assets/gui/roadmap-interactions.js pure timeline/row/connector geometry
assets/gui/onboarding-view.js  project workspace composition and canonical task creation
```

`roadmap-view.js` does not own task creation, workspace creation, provider
selection, or orchestration. `onboarding-view.js` composes the existing task
workflow when a planned item is promoted to real work.

## Validation and regression coverage

Focused backend/GUI tests:

```bash
python -m pytest \
  tests/test_roadmap.py \
  tests/test_gui_routes.py \
  tests/test_gui_onboarding.py
```

Static frontend checks:

```bash
for file in src/execraft/assets/gui/*.js; do node --check "$file"; done
```

The browser journey suite requires Playwright Chromium to be installed:

```bash
python -m playwright install chromium
python -m pytest tests/test_gui_browser.py
```

Roadmap regressions cover schema validation, duplicate task links, dotted
canonical task IDs, dependency cycles, stale revisions, task/runtime projection,
archive projection, item/relation deletion, non-destructive roadmap deletion,
strict GUI route payloads, packaged frontend assets, project-home counts, and
the permanent-task-delete guard.

## Migration and deliberate v2 boundaries

Roadmap v1 remains readable only to enable explicit migration. The v1 → v2
migrator previews all promoted assets, allocates deterministic IDs across every
v1 Roadmap in the Project, and writes a prepared migration marker before
creating canonical assets. Restart resumes the prepared identity map; source
revision/digest drift fails closed. Legacy planning-only Gates are promoted as
human-approval ProjectGates so migration cannot silently authorize execution.

Schema version 2 still does not implement fuzzy month/quarter/half-year date
types, automatic critical-path scheduling, portfolio Roadmaps spanning unrelated
Projects, or a second Task status field. Those can evolve independently while
Roadmaps remain planning/view projections and Project Execution remains the
control domain.

## Presentation exports

Roadmap v2 can be exported directly from its canonical read projection as SVG
or PDF. Export is deliberately not implemented as a screenshot of the browser
canvas: the export subsystem resolves the same canonical Task and Project
Execution references and renders an immutable presentation model. See
[`project-exports.md`](project-exports.md).
