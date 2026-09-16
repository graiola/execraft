# Workflow and Work Package controls

The dashboard workflow has two projections over the same canonical package
state. **Graph** is the default Run workspace and preserves dependency topology,
relationship emphasis, card controls, and explicit pan/zoom navigation. **List**
is the compact topological projection for sequential scanning. Neither projection
implements a scheduler; both are clients of the same durable orchestration state
and audited backend mutations.

The Graph/List preference is stored as local GUI state rather than project
configuration. A clean browser profile starts in Graph; a user-selected List view
is restored on reload.

## Graph (default)

Work Packages are placed in **Step N** columns derived from declared hard
prerequisites. A card in a later column cannot become ready until its listed
prerequisites are complete. The same All / Remaining / Active / Shards filter
applies to both projections. Generated shards keep normal dependency semantics
and identify their parent; parent membership is not represented as a fake
dependency.

Selection is presentation state, not navigation. Clicking a card highlights its
upstream prerequisites and downstream dependents but does not scroll, recenter,
fit, or zoom the workflow. Background snapshot refreshes follow the same rule and
preserve Graph pan/zoom.

Navigation occurs only through an explicit intent:

- **Locate** centers the selected Work Package, or the primary active Work Package when
  nothing is selected;
- **Follow active** is off by default and, when enabled, locates once when the
  primary active Work Package changes;
- **Fit** fits the complete graph; the first Graph opening for a task performs one
  initial fit;
- **Reset** restores the origin and 100% scale;
- keyboard arrow, zoom, and reset controls remain explicit navigation inputs.

The graph keeps horizontal overflow but not an ordinary vertical nested-scroll
surface. Drag empty space with mouse/pen to pan. A vertical wheel or trackpad
gesture scrolls the page; Shift+wheel pans the graph horizontally; native
horizontal trackpad deltas remain available; `Ctrl`/`Cmd`+wheel performs
cursor-anchored zoom. Focused keyboard users can pan with arrow keys, zoom with
`+`/`-`, and reset with `0`.

An SVG connection layer draws orthogonal directional routes for declared
prerequisites. The router simplifies duplicate and collinear points, draws
same-row relationships as one straight segment, distributes ordered ports along
card edges, and uses shared fan-out/fan-in trunks when related lines would nearly
overlap. Cards are reordered inside their topological columns with deterministic
barycenter sweeps to reduce avoidable crossings. The board is stacked above the
SVG, so long routes pass naturally behind intermediate columns and cards without
a perimeter corridor. The dependency path for the selected card is emphasized,
while the path feeding the scheduler's current assignment is highlighted in
gold. Unrelated branches are subdued but remain visible.

## List

List groups Work Packages into **Step N** sections using the same hard prerequisites.
Each row shows ID, title, lifecycle stage, selected/assigned agent, prerequisites,
active/parallel/shard state, and pending scheduling directives. Clicking the row opens
**Overview**; the same compact **Pause/Resume**, **Sync**, **Execution**, and **Agent**
shortcuts used by Graph cards remain available without changing selection/navigation semantics. Switching
Graph/List never implies a Work Package locate; an already established Graph
viewport is retained when returning from List during the same task session.

## Graph Work Package cards

Every Graph card shows a deliberately reduced operator surface:

- Work Package or shard ID, title, lifecycle stage, and status colour;
- the currently selected/assigned agent plus acceptance progress;
- declared prerequisites;
- structural shard/parallel badges and one aggregated directive badge when
  scheduling intent exists;
- an **Action required** badge only for states that genuinely need attention;
- state-sensitive **Pause/Resume** and repository **Sync** shortcuts for high-frequency operations;
- distinct **Execution** and **Agent** shortcuts.

Selecting the card body both highlights its upstream/downstream relationship path
and opens the focused Work Package side inspector on **Overview**. The card intentionally
restores only high-frequency operational actions rather than the old full scheduling
toolbar; decomposition and rarer administrative controls stay in the inspector.
**Execution** is deliberately different from card selection and opens the inspector on
its Execution tab. Selection
still never implies viewport navigation; opening the inspector does not recenter
or resize the Graph.

## Work Package execution trace

The Work Package inspector's **Evidence** tab includes an **Execution trace** built from the
durable agent invocation ledger and the append-only orchestration journal. A
parent Work Package opens one swimlane for the parent and one for each recursively
generated shard, making parallel implementation and review work visible without
merging independent agents into one artificial current stage. Opening a shard
directly keeps the trace scoped to that package.

The default **Flow** projection groups consecutive attempts of the same stage,
uses short unlabeled connectors for normal transitions, and labels only exceptional
branches such as changes requested, verification failure, recovery, or mandatory
decomposition. Nodes keep the stage, responsible agent or deterministic Check/Hold,
outcome, and duration visually dominant. **All attempts** restores every exact
agent attempt when detailed auditing is needed.

Retries, agent fallback, availability waits, operator pauses, Supervisor
recovery, and other exceptional journal events are aggregated under a collapsed
**Diagnostics** disclosure instead of being rendered as dozens of pills below the
flow. Selecting a stage opens a compact inspector; repeated attempts are summarized
there, while timestamps, invocation identity, capability, metrics, skills,
validation errors, failure details, and artifact references remain under a second
progressive-disclosure control. The associated agent output can still be opened
directly.

The compact trace summary prioritizes wall-clock time, cumulative agent work,
agents involved, and review loops while retaining waiting, stage-attempt, and
fallback counts in supporting text. New runs record
`package_stage_transition` events whenever the orchestrator advances a package,
including completion and Supervisor-directed recovery. Provider attempts remain
exact because they come from the SQLite invocation ledger. Runs created before
structured stage transitions were introduced are marked as partial: their agent
attempts remain available, while some deterministic stages or transition
durations cannot be reconstructed.

## Contextual execution routing

The Graph **Execution** shortcut opens the Work Package inspector directly on its
routing workspace. The Work Package/package context is already fixed by the
inspector, so the normal editor asks only for a role, **Automatic / Prefer /
Force**, and an **Execution Lane**. A lane is a presentation grouping of profiles
that share the same runtime, model route, and execution target; it is not a new
scheduler identity or scheduling authority.

The selected lane is translated back through the existing role→profile selection
API. Ranked profile preferences remain soft hints for **Prefer**; **Force** keeps
the existing binding-role semantics. Provider/profile health, capability,
complexity ceilings, reviewer independence, exclusions, repository conflicts,
product-support policy, and concurrency remain authoritative. Explicit shard
propagation is unchanged. Raw profile order and role-specific workflow skills
remain available under **Advanced profile and skill policy** and are composed
into the normal runtime-neutral handoff.

A live invocation is never hot-migrated. The preview reports whether the change
can apply at the next invocation boundary or whether the dashboard-owned driver
can be cancelled and resumed; an external driver continues to block switching.

## Decomposition

For the current Work Package, **Decompose now** invokes the canonical
`execraft orchestrate decompose` path through the backend. It is available only for
eligible parent Work Packages that are not already completed or decomposed, and only
while the driver is stopped. Generated shards appear as normal cards and inherit
parent execution policy unless explicitly overridden.

For an unstarted future Work Package, **Plan first** stores a durable mandatory
decomposition directive. The orchestrator consumes it at the next safe package
boundary and runs the normal validated decomposition stage before implementation,
even when automatic decomposition heuristics are disabled. An atomic planner
decision is still valid and allows the Work Package to continue; an expanded plan
creates the normal scheduler-managed shards. The directive is one-shot and is
marked consumed after either outcome.

## Operator pause and resume

**Pause** stores a durable scheduling hold without rewriting the Work Package stage
or status. The ready queue omits paused work, while its current evidence,
assignments, and history remain inspectable. Pausing a parent from the GUI also
applies the hold to its direct incomplete shards so a partial shard wave cannot
continue unexpectedly. **Resume** clears the hold.

The equivalent CLI commands are:

```bash
execraft orchestrate pause \
  --project sample \
  --task-id feature_auth \
  --package-id implementation \
  --pause-reason "Waiting for external dependency" \
  --apply-to-shards

execraft orchestrate pause \
  --project sample \
  --task-id feature_auth \
  --package-id implementation \
  --resume \
  --apply-to-shards
```

Pause changes are persisted in `state.json` and journaled as
`package_operator_pause_changed` with all affected package IDs.

For an unstarted future Work Package, **Pause entry** schedules a one-shot
`pause_before_start` entry Hold instead of pausing it immediately. When the Work Package
first becomes dependency-ready, the driver stops before decomposition, agent
assignment, or implementation and enters `operator_paused`. The Action Center
then offers **Resume <Work Package>**. Starting a new run or pressing Resume
acknowledges the Hold, clears it, and continues from the same queued Work Package.

Future directives remain editable while a driver is active. The dashboard writes
them to the flock-protected `work-package-directives.json` sidecar queue rather than
racing the driver's authoritative `state.json` checkpoint. The orchestrator
consumes the latest desired value at deterministic scheduler boundaries, journals
the result, and exposes queued-but-not-yet-consumed changes as **Pending sync**.

## Current work and assignments

Packages present in the scheduler assignment set show a prominent **ACTIVE**
banner and **Working now** tag. Their cards, prerequisite path, and final incoming
arrow are highlighted in gold without a distracting continuous pulse. A compact
summary above the Graph/List surface lists active package IDs. The Action Center reads all open
agent invocations from the invocation ledger, so parallel workers retain their
own package, agent, stage, model, and elapsed time instead of being reduced to the
first scheduler assignment. Selecting a running Graph card retargets the primary Action
Center context and opens the same side inspector used by List rows. List inspection
does not require Graph path navigation. A second assignment table is intentionally
not rendered below the workflow: the Graph/List workspace and Work Package inspector
are authoritative for Work Package state, while the **Execution health** drawer shows
active package/stage/lane rows (including parallel mode) when an infrastructure
summary is useful.

## Agent action

**Agent** in either Graph or List opens the persistent agent workbench scoped to the Work Package's package and
stage. It is disabled until an agent is associated with that Work Package. The
workbench exposes Conversation and Changes as the normal views. Terminal, raw
activity, result artifacts, and session management are available under Advanced
when supported by the selected session.

## Safety and accessibility

- Mutation controls are locked while a driver is active; inspection remains
  available.
- Completed Work Packages cannot be paused, decomposed, or reassigned.
- Card controls are ordinary keyboard-focusable buttons with visible focus
  treatment.
- Dragging never begins on a card control, link, form field, or disclosure.
- The workflow viewport exposes a labeled focusable region, visible scale, and
  keyboard equivalents for mouse navigation.
- The Work Package side inspector can be closed with its close control or Escape.
  It is non-modal, has no backdrop, does not resize the Graph, and returns focus
  to the Graph/List workbench with `preventScroll`. Other purpose-specific dialogs
  retain their documented native modal behavior.
- The Execution Health drawer is also non-modal. Its opener exposes
  `aria-expanded`/`aria-controls`; opening focuses the close control without
  scrolling, Escape closes it, and focus returns to the opener with
  `preventScroll`. On narrow screens it becomes a full-width sheet.
- Overview/Execution/Evidence tabs use standard tab semantics, support arrow/Home/End
  keyboard movement, and preserve the active tab and inspector scroll across
  dashboard refreshes.
- Reduced-motion preferences are respected.

## Compact dependency rendering

Graph mode opens in **Compact** link mode. This view performs a display-only
transitive reduction: when a Work Package declares several prerequisites and one
prerequisite already depends on another, the implied direct line is hidden. The
durable package dependencies and scheduler semantics are not modified.

Use **All links** in the workflow toolbar when auditing the raw dependency list
from the plan. The legend reports how many essential links are shown and how
many redundant declared links are hidden by the compact view. Filtering the
workflow recalculates the reduction over the Work Packages that remain visible so
visible cards do not lose their only displayed incoming relationship.

## Accepting a deferred late-stage risk

When a Work Package is blocked at final review, full verification, or acceptance
evidence and the operator intentionally wants to finish it without claiming the
missing check passed, the Action Center may show **Accept & continue** alongside
the normal repair/Supervisor action.

The dialog is intentionally explicit: it shows the blocking requirement,
reviewer evidence, and incomplete acceptance criteria; requires a written reason;
and requires acknowledgement that those items remain unverified. Confirmation
is package-scoped and protected by the current human-action journal sequence, so
an out-of-date dialog cannot disposition a newer incident. On success the
operator decision is audit-logged and orchestration resumes automatically.

This control is not shown for protected repository scope, agent/runtime availability,
invalid agent output, repository synchronization, or commit-transaction checks.
Those conditions must still be repaired through their dedicated workflows.
