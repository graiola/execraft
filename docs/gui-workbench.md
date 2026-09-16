# GUI workbench

The dashboard is a task-bound operations workbench. It presents orchestration
state but does not create a second state machine or bypass existing mutation
checks.

The header uses the packaged `execraft-mark.png` symbol with the accessible product
name rendered as live HTML. Responsive sizing belongs in `gui.css`; do not create
alternate branded copies of the product mark.

## Frontend boundaries

The workbench deliberately uses dependency-free browser modules:

- `app.js` composes controllers and renders task-specific orchestration data;
- `ui-shell.js` owns application/tab navigation, Action Center projection, task switching, and commands; local Graph/List and Follow Active preferences remain workbench state in `app.js`;
- `workbench-navigation.js` owns selected/active Work Package state, Graph/List mode, Follow Active policy, serializable viewport state, and explicit locate requests;
- `workflow.js` owns the Graph and compact List projections, cards, topology layout, and SVG connections;
- `workflow-viewport.js` owns graph pan/zoom input and reports presentation-only viewport state back to `WorkbenchNavigation`;
- `work-package-inspector.js` owns non-modal Work Package detail lifecycle, tab state, inspector scroll, and focus restoration;
- `work-package-execution.js` projects contextual role routing into presentation-only **Execution Lanes** and delegates every mutation to the existing runtime-selection/package-policy endpoints;
- `execution-health-view.js` owns the compact Run health summary, lane/infrastructure attention presentation, and explicit health-drawer lifecycle;
- `agent-workforce-view.js` owns the task-centric **Agents** workforce projection: Working / Needs attention / Available workers plus bounded operator actions;
- `runtime-control.js` owns passive runtime/model/location diagnostics and advanced configuration inventory inside that drawer or Project Settings; it does not select a package or change Work Package routing;
- `agent-console.js` owns live agent observation and bounded interaction;
- `archive-view.js`, `log-controller.js`, and `run-control.js` own their focused
  project/archive, diagnostics, and run-control surfaces;
- `ui-utils.js` is the single source for HTML escaping, formatting, required
  element lookup, and resilient local-storage access.

Do not add endpoint calls or scheduling decisions to presentational modules.
All mutations continue to pass through the server's audited endpoints. Immediate
runtime mutations use canonical orchestration commands; future Work Package
directives use a flock-protected sidecar queue and are consumed by the active
orchestrator only at deterministic package boundaries. New packaged files must be
added to both the server's strict asset allowlist and the setuptools package-data
patterns.



## Project creation profiles

New-project selectors show only profiles intended for current project creation.
Compatibility-only versions such as `standard@1` remain readable for historical
projects and migrations but are not offered for greenfield/adoption choices. A
project descriptor without a versioned profile is shown as **unversioned
compatibility**, not as a recommended "Legacy profile".

## Roadmap Task duration

Roadmap Task bars expose planning duration directly. Dragging the whole bar
preserves duration; dragging either edge changes it. The Selection HUD also
provides exact Start, Target, and inclusive Duration fields plus Clear schedule.
Duration is derived from Start/Target rather than persisted separately, and these
Roadmap edits remain planning-only. Keyboard users can focus a resize handle and
use arrow keys (or Shift+arrow for seven days).

## Project Execution workspace

Project-level execution is intentionally outside the task-bound Run workbench.
When a project is open, **Execution** sits beside **Roadmap** and **Tasks** and is
implemented by `project-execution-view.js`. It consumes the typed
`/api/project-execution/...` surface and presents canonical Phases, Gates and
Milestones without reusing the Task Work Package inspector.

The project view may start a **Task as a whole** in Assisted mode. In opt-in
Automatic mode it exposes bounded Task/Phase concurrency and failure policy plus
an explicit **Run automatic cycle** action. Status refreshes never start Tasks.
Project-started Task drivers are owned independently of the selected Task inspector,
so multiple Automatic Tasks can run without forcing browser-focus changes. The
project view never selects or mutates Work Packages. Gate approvals/rejections and
waivers are audited project-domain actions bound to the current evidence fingerprint.
Canonical asset edits are optimistic-revision writes. A Roadmap v2 node that
references a project asset uses the same canonical metadata API for inline
rename/date edits so Roadmap YAML remains reference-only.

Runtime-provider **Execution readiness** under Project Settings is a separate
surface (`projectExecutionReadinessView`); it must not be conflated with the
Project Execution domain workspace (`projectExecutionView`).

## Refresh lifecycle

Snapshot and workspace refreshes are single-flight: a slow request is reused
instead of allowing interval calls to overlap. The dashboard reduces its refresh
cadence while the page is hidden, suspends workspace refreshes outside
**Changes**, and polls logs only while **Logs & diagnostics** is active and
following. Workflow, configuration-menu, and workspace DOM are rendered only when their
relevant input signature changes. Execution health is progressively disclosed:
the compact **Execution health** row stays current, the lane-first drawer renders
only when requested, and raw profile/node maintenance cards are not built until
**Advanced profile maintenance** is expanded. This preserves focus and reduces
layout work without weakening durable-state freshness.

## Primary operator journeys

### Run or resume

1. Read the Action Center for the current operation, reported blocker, and next
   safe action. Work Package, agent/stage, elapsed time, secondary shortcuts, and
   parallel invocation rows are intentionally under **Details** so normal running
   state stays compact. While agent attempts are open, the invocation ledger
   is authoritative, so failover and shard execution appear immediately even
   before successful-agent attribution is persisted back to the package.
2. Use the primary run/stop/agent actions. Their enabled state still comes from
   the backend run-control and execution-context contracts.
3. Use the default **Graph** as the Work Package workspace. Selection highlights a
   Work Package and its relationship context without moving the viewport. Switch to
   **List** when a compact topological scan is preferable.
4. Use the Work Package card's state-sensitive **Pause/Resume**, **Sync**, **Execution**, and **Agent** shortcuts for frequent operations. Clicking the card body always opens **Overview**; **Execution** is a distinct explicit shortcut.
5. Use the separate **Agents** task view to manage the workforce: see who is Working, who Needs attention, who is Available, open an agent session, and invoke contextual Promote / Doctor / Reset / Diagnostics actions.
6. Open **Execution health** only when lane, capacity, node, token, or infrastructure diagnostic detail is needed. Healthy state remains a single quiet row; raw profile/runtime/model/target information remains progressively disclosed.

### Plan future Work Package Holds

1. Open any unstarted queued Work Package.
2. Use **Plan first** to require validated decomposition before implementation,
   or **Pause entry** to stop the driver when that Work Package first becomes ready.
3. The badges show the durable desired state immediately. **Pending sync** means
   an active driver has not yet consumed the sidecar command.
4. Remove either directive from the same card before it is reached. Once a planned
   pause is reached, use the Action Center's Resume action or start a new run.

### Inspect active work

1. Open **Agents** for the task workforce view. Working cards identify the current Work Package and lane; attention cards surface health reasons; available cards show workers ready for scheduler assignment.
2. Use **Open** for the persistent agent workbench. Native profiles expose contextual **Doctor**, **Promote**, and **Reset health** actions when their safety conditions permit; OpenClaw workers link to infrastructure diagnostics instead of pretending to support Native maintenance.
3. Work Package **Agent** shortcuts remain available for direct package-scoped access without visiting the workforce view first.
2. Use the fixed right-side workbench for normal inspection, or maximize it for
   a focused review. Closing the workbench does not terminate the selected session.
3. Use **Conversation** and **Changes** for routine work. Open **Advanced** only
   for session management, Terminal, raw activity, or the durable result artifact.
4. Standalone terminal controls remain capability-gated and audit-logged.

### Navigate a large workflow

The Run view opens in **Graph** mode on a clean browser profile. Graph/List is a
local GUI preference, so choosing **List** is restored on reload without changing
project configuration. The first Graph opening for a task fits the workflow once;
subsequent renders preserve pan and zoom.

Selection and navigation are intentionally separate:

- selecting a Work Package updates relationship highlighting and details but never
  recenters the Graph or scrolls the List;
- the three-second dashboard refresh updates data without changing document
  scroll, Graph pan/zoom, or selected Work Package;
- **Locate** explicitly centers the selected Work Package, or the primary active
  Work Package when nothing is selected;
- **Follow active** is off by default. When enabled, a change in the primary
  active Work Package emits one explicit locate operation. Enabling the toggle by
  itself does not move the viewport.

Graph gestures follow a single-scroll-owner rule:

1. Drag empty workflow space with the primary mouse button or pen to pan in both
   directions. Card controls remain clickable and never initiate a pan.
2. A normal vertical mouse-wheel or trackpad gesture belongs to the page. Use
   Shift+wheel for explicit horizontal Graph panning. Native horizontal trackpad
   motion remains available.
3. Hold `Ctrl`/`Cmd` while wheeling to zoom around the pointer.
4. Use the compact toolbar to zoom, fit the complete graph, reset to 100% at the
   origin, or explicitly Locate the selected/active Work Package.
5. For keyboard navigation, focus the workflow, use arrow keys to pan, `+`/`-`
   to zoom, and `0` to reset. Hold Shift with an arrow for a larger pan step.

Work Package details use a non-modal **side inspector**. Opening the inspector never
changes document scroll or Graph pan/zoom, and closing it deliberately returns
focus to the stable Graph/List workbench target with `preventScroll`. The selected
Work Package remains selected after close.

The inspector has three stable tabs:

- **Overview** — lifecycle state, acceptance progress, dependencies, dependents,
  parent/shards, requirements, acceptance criteria, and collapsed technical scope;
- **Execution** — current/next agent-role routing plus a contextual lane-first editor. **Automatic**, **Prefer**, and **Force** keep their existing scheduler semantics, while the normal UI chooses an **Execution Lane** (`runtime + model route + target`) rather than raw profile IDs;
- **Evidence** — execution trace, implementation summary, review findings, and
  recent durable stage evidence.

The active tab and inspector scroll position are presentation state. Snapshot
refreshes may update the content, but must not switch tabs, reset the inspector
scroll position, or rebuild/recenter the Graph. On narrow screens the same
inspector becomes a full-width sheet without changing its information model.

The viewport transform is presentation-only: it does not change graph topology,
Work Package state, or SVG connector coordinates. Declared dependencies render as graph-aware orthogonal routes with the existing
active/selected emphasis. Aligned relationships are straight, duplicate and
collinear points are removed, and ordered card-edge ports prevent terminal
stacks. Fan-out and fan-in relationships in the same column pair share compact
trunks and vertical buses rather than drawing nearly coincident independent
lines. The workflow board remains layered above the connection SVG, so long
routes can cross behind intermediate columns without perimeter detours.

The workbench intentionally has one docked layout. Removing layout switching,
collapse state, and manual resizing keeps the operator model and client state
small; **Maximize** remains available when more space is useful.

### Route Work Package execution

Routing is Work Package-contextual. Open a Work Package and use **Execution**; the package is already known and is never selected again in the infrastructure panel. Choose the role, then:

- **Automatic** clears that role's explicit profile preference and leaves candidate ordering to the scheduler;
- **Prefer** resolves the selected lane back to matching scheduler profiles while preserving compatible fallbacks;
- **Force** uses the same matching profile pool but persists the role as binding;
- **Apply routing to direct child shards** keeps the existing explicit shard-propagation semantics.

An **Execution Lane** is presentation-only grouping by runtime, model route, and target. OpenClaw role-specific profiles that resolve to the same tuple therefore appear as one operator object even though their canonical profile IDs remain separate scheduler identities. Lane IDs must never be persisted as scheduler configuration. Raw profile IDs, exact runtime/model-route/target IDs, and role skill policy remain under **Advanced profile and skill policy** for troubleshooting.

The global **Execution infrastructure** area is passive: it shows lane/configuration inventory and runs diagnostics only when explicitly requested. It no longer contains package/role routing controls.

### Resolve a Supervisor decision

Idle Supervisor state is not rendered as a separate dashboard panel. Active or
queued recovery appears inside the Action Center as the current operational
context. A human-required question is shown directly there; read each consequence,
add guidance if needed, and submit through the existing audited Supervisor
endpoint. Provider identity, budgets, incident IDs, and pause/open-console actions
remain under **Recovery details**.

### Review changes and advanced configuration

Use **Changes**, the Action Center shortcut, or `Ctrl/Cmd+K` to inspect
task-owned repository changes. Inspection remains available while a driver is
active, but destructive and commit actions continue to obey backend safety
checks. Raw validated YAML is intentionally secondary: open **More → Advanced
configuration** only when structured project settings are insufficient.

### Retire or restore inactive work

Return to the project workspace and open **Archive**. The active catalog section
can retire any non-current task or project whose driver is stopped. Archived
dossiers remain inspectable with an integrity report and bounded previews;
reactivation is available only after SHA-256 verification succeeds. This is a
reversible catalog operation, not the immutable completion evidence archive. See
[`catalog_archive.md`](catalog_archive.md).

### Open another task

The application owns at most one task dashboard and its process controller. The
task selector asks `ControlCenterService` to open the selected task only after
safe-switch checks pass; the previous dashboard is closed after the replacement
is ready. Selecting **Project workspace** follows the same ownership check instead
of bypassing a running GUI-owned orchestrator.

## Keyboard and accessibility behavior

- The four primary task views (**Run**, **Agents**, **Plan**, **Changes**) and agent
  workbench tabs use `tablist`/`tab` semantics.
- Arrow keys, Home, and End move between primary tabs. Secondary task utilities
  are available from **More** and do not enlarge the tab sequence.
- `Ctrl/Cmd+K` opens the command palette.
- Focusable controls use a visible two-pixel focus ring.
- Status and toast changes use live regions.
- Reduced-motion preferences disable nonessential animation globally.
- Escape closes the active agent workbench without terminating its session.
- In **Graph** mode the workflow supports arrow-key panning and `+`, `-`, and `0` zoom controls.

## Project-independent home

The browser application now opens safely with zero registered projects and no
selected task. Project registration, structured settings, catalog archive, greenfield
creation, task creation, and resumable start journals live in a separate
application shell. Opening a task instantiates the existing task-bound dashboard;
returning home or switching tasks is blocked while that dashboard owns a running
orchestrator. See
[`gui-onboarding.md`](gui-onboarding.md).

## Browser checks

`tests/test_gui_browser.py` provides an optional deterministic Playwright
fixture server. It exercises the project onboarding mode switch, project workspace,
compact Action Center, default Graph/persisted List switch, viewport stability, Follow Active, wheel ownership, active task catalog,
keyboard tabs, command palette, focused agent workbench, Advanced tools, maximize
behavior, and a screenshot capture.
Archive, Work Package card, and pause behavior
also have deterministic backend and asset tests that do not require a browser
binary.

For a fast browser-independent frontend verification pass, run JavaScript syntax
checks followed by the focused GUI backend suites:

```bash
for file in src/execraft/assets/gui/*.js; do node --check "$file"; done
python -m pytest tests/test_gui.py tests/test_gui_workspace_changes.py
```

Run the Playwright journey separately after installing both the test/browser
extras and Chromium:

```bash
python -m pip install -e '.[test,browser]'
python -m playwright install chromium
python -m pytest -q tests/test_gui_browser.py
```

React/TypeScript, React Flow/ELK, xterm.js, Monaco, SSE/WebSocket transport, and
multi-task backend tenancy remain later migration phases. The current slice
deliberately improves information architecture and testability without changing
orchestration semantics.

### Dependency line detail

In **Graph** mode, the graph toolbar defaults to **Compact** dependency lines. Compact mode
hides direct links that are already implied by another visible dependency path,
which is especially important for generated shard chains whose durable records
may list all previous shards. **All links** restores the complete declared graph
for contract inspection. This is a rendering preference only; orchestration
state and package dependencies are unchanged.

## Export actions

Project Roadmap, Project Execution, and Task workbench views expose read-only
export actions. The browser fetches the protected binary export endpoint with
the dashboard token and downloads the returned SVG/PDF. The GUI does not build
or serialize diagrams itself; all presentation semantics live in the backend
export projection/rendering subsystem. See
[`project-exports.md`](project-exports.md).
