# GUI project onboarding and task creation

The local GUI is a project-independent control center. It can start with an
empty control-plane home, register an existing source checkout, create a
versioned greenfield project, prepare the first task, and only then open the
existing task-bound orchestration dashboard.

```bash
execraft gui
```

`--project` focuses a registered project and `--task-id` opens a task directly:

```bash
execraft gui --project sample
execraft gui --project sample --task-id feature_auth
```

The server remains loopback-only unless `--allow-remote` is supplied. The page
token protects requests from unrelated browser origins, while every onboarding
mutation also requires an explicit acknowledgement in the JSON request. The
token is not treated as operator consent.

## Application boundaries

The GUI separates three application scopes:

- `ControlCenterService` owns the browser session: project focus, active task,
  safe task switching, project-independent catalog archive operations, and
  delegation to the task dashboard.
- `ProjectHomeService` projects the registered project catalog, readiness,
  execution-agent/runtime readiness, verification approvals, task reviews, and durable start
  sessions.
- `GuiOnboardingController` translates GUI requests into the project, greenfield,
  and composed start services.

The HTTP server contains no onboarding business logic. The browser and tests
call the same services used by the CLI. Once a task is opened, the existing
`DashboardService` remains the only owner of run/stop controls and orchestration
process state.

A dashboard-owned orchestrator must be stopped before the browser can switch to
another task or return to project home. An externally owned run is never
terminated by a session change.

## Progressive project onboarding

Project onboarding is one **Add project** disclosure rather than three forms shown
at once. The first choice is the starting point:

- **Existing source** for a checkout that Execraft should inspect and register.
- **Project descriptor** for a portable `project.yaml` that already exists.
- **New project** for a repository created from the versioned greenfield catalog.

Only the selected workflow is rendered as active. Profile, feature, template,
planner, dev-container, source-binding, and discovery-decision controls are kept
under **Advanced options** because the defaults are appropriate for the common
case. This is presentation-only progressive disclosure: the browser still calls
the same onboarding services and no safety Check/Hold is skipped.

### Existing source

The normal path is deliberately two-step because discovery evidence can require an
operator decision:

1. Enter the source folder and choose **Inspect project**. Inspection is bounded and
   never executes repository code. It reports repositories, languages, findings,
   generated control-plane files, and whether the source is already registered.
2. **Create project** is enabled only when the inspected creation plan is applicable.
   Apply atomically publishes and registers the descriptor after explicit mutation
   acknowledgement.

Feature-branch base inference, nested repositories, overlapping source paths, and
other `decision_required` findings remain blocked unless the operator explicitly
opens **Advanced options**, accepts those discovery decisions, and inspects again.
A source already registered with the same route is shown as existing rather than
being duplicated.

### Portable descriptor

Enter the existing `project.yaml` path and choose **Register project**. The optional
local source binding is under **Advanced options**. The descriptor is not copied;
the XDG control-plane registry stores the route to it.

### New project

The routine path asks only for the project name, parent folder, and an optional
first-task description. Source template, project profile, extra features, first-task
planner, and dev-container creation are under **Advanced options**.

**Create project** first executes the greenfield preview endpoint internally. If
the source or project creation plan is blocked, the preview evidence is displayed
and apply is not called. When applicable, apply creates the source tree atomically,
initializes Git, registers the project, and optionally starts the first task through
the same composed start workflow. **Preview changes** remains available under
Advanced for operators who want to inspect the exact source/control-plane file plan
before pressing Create.

A failure while preparing the optional first task leaves a valid registered project
and a durable start journal that can be reviewed and resumed.

## Navigation model

The GUI exposes three separate contexts instead of appending project controls to
the global home page:

1. **Projects** is the global catalog. Each project card shows its recent tasks,
   so returning to the catalog does not hide all task information.
2. **Project workspace** manages one project. **Roadmap** is the default view and
   provides interactive project-level planning over canonical tasks;
   **Tasks** remains the secondary inventory/composer surface;
   **Settings** groups readiness, execution, and verification controls;
   **Archive** manages reversible catalog retirement and restoration.
3. **Task dashboard** owns execution and presents four primary views:
   **Run**, **Agents**, **Plan**, and **Changes**. **Agents** is the task workforce view; Logs/diagnostics and raw validated YAML are
   secondary utilities under **More**, rather than permanent task tabs.

A compact context path appears below the application header. **Projects** returns
directly to the catalog, the project selector changes project workspace, and the
task selector either opens a task or returns to the project **Roadmap**. This
combines breadcrumb context and switching controls instead of rendering both.

The catalog, project workspace, and task dashboard are mutually exclusive main
views. Selecting a project no longer scrolls to a panel attached below project
creation forms.

## Project workbench

Selecting **Open roadmap** opens four project-level views with **Roadmap** active:

- **Roadmap**: multiple interactive project planning timelines. Linked task state is
  projected from canonical task dossiers; drag/drop scheduling and planning-only
  dependencies never control the orchestrator. See [`project-roadmaps.md`](project-roadmaps.md).
- **Tasks**: task catalog, brief/plan review, and the composed start form.
- **Settings**: descriptor/readiness information, execution-agent/runtime diagnostics,
  and conflict-checked approval of discovered verification commands. Readiness
  inspection never launches an agent/runtime or performs authentication, and verification can
  only toggle command indexes already present in the project registry.
- **Archive**: reversible retirement, integrity inspection, and restoration of
  inactive project/task dossiers. Archive operations are project-independent
  application services and do not require an open task dashboard.

From Roadmap, clicking an active linked task opens the canonical task dashboard
directly. The roadmap itself is a direct-manipulation canvas: blocks move in
time and between rows by drag/drop, bar edges resize schedules, right-to-left
connector drags create planning dependencies, and Project Gates/Milestones are point
blocks. **+ Block** enters visual placement mode for new planning blocks while
unscheduled task cards can be dragged from the block tray. New non-task blocks
are named inline rather than through a separate item form. The compact selection
HUD is action-only; it does not duplicate date/lane/dependency editors.

The task dashboard exposes a **← <project> roadmap** control. It returns to the
project Roadmap explicitly, independent of any previously selected project tab.

Verification saves carry the SHA-256 digest returned by the read endpoint. If an
external edit changes the file, the save is rejected and the operator must
refresh. JSON booleans and command-index arrays are decoded strictly; strings
such as `"false"` are rejected rather than interpreted through Python
truthiness.

## Task creation and review

The normal task composer is the browser equivalent of:

```bash
execraft start "Add OIDC authentication"
```

For the common case the operator supplies the desired outcome and confirms the
repository scope. Task ID, planner mode, no-workspace mode, explicit preview, and
BRIEF/PLAN/graph imports are grouped under **Advanced options**. Defaults come from
the registered project and the same start-domain logic used by the CLI.

**Create task** does not bypass preview. The browser sends the exact request to
`/api/onboarding/start/preview` first and renders its repository, execution-agent/runtime, workspace,
and step effects. Only when `can_apply` is true does it send the same request to
`/api/onboarding/start/apply` with explicit acknowledgement. A blocked preview stays
visible and no task/workspace state is published. Operators who want to inspect the
preview without creating anything can choose **Advanced options → Preview task**.

The review dialog displays:

- the validated `TASK.yaml` projection;
- the complete `BRIEF.md` intent;
- normalized executable work packages from `PLAN.graph.yaml`;
- plan validation errors without making the rest of the dialog unusable;
- the durable start journal and its exact original request settings.

Incomplete or failed start journals appear on project home. **Review / resume**
reconstructs the original request rather than inventing new defaults, avoiding scope
or agent-profile drift on recovery.

## HTTP surface

Project-independent reads:

```text
GET /api/home
GET /api/projects
GET /api/onboarding/templates
GET /api/onboarding/sessions
GET /api/onboarding/readiness?project_id=...
GET /api/onboarding/providers?project_id=...  # compatibility route: execution-agent inventory
GET /api/onboarding/verification?project_id=...
GET /api/onboarding/task?project_id=...&task_id=...
GET /api/roadmaps?project_id=...
GET /api/roadmap?project_id=...&roadmap_id=...
GET /api/archive
GET /api/archive/item?kind=...&project_id=...&id=...
```

Mutations and previews:

```text
POST /api/session/open
POST /api/session/home
POST /api/session/project
POST /api/session/catalog
POST /api/project/focus
POST /api/onboarding/inspect
POST /api/onboarding/project/create
POST /api/onboarding/project/register
POST /api/onboarding/start/preview
POST /api/onboarding/start/apply
POST /api/onboarding/greenfield/preview
POST /api/onboarding/greenfield/apply
POST /api/onboarding/verification/update
POST /api/roadmap/create
POST /api/roadmap/metadata/update
POST /api/roadmap/item/upsert
POST /api/roadmap/item/delete
POST /api/roadmap/item/link-task
POST /api/roadmap/relation/upsert
POST /api/roadmap/relation/delete
POST /api/roadmap/delete
POST /api/archive/archive
POST /api/archive/reactivate
```

All POST requests require the per-process `X-Execraft-Token`. Mutating endpoints
also require `acknowledged: true`. Preview endpoints do not publish control-home,
project, task, workspace, or source artifacts.

## Validation

Backend and browser-independent checks:

```bash
for file in src/execraft/assets/gui/*.js; do node --check "$file"; done
python -m pytest tests/test_gui.py tests/test_gui_onboarding.py
```

Full Chromium journey:

```bash
python -m pip install -e '.[test,browser]'
python -m playwright install chromium
python -m pytest tests/test_gui_browser.py
```

The repository workflow `.github/workflows/gui-browser.yml` installs Chromium
explicitly before running the browser suite, so a missing developer browser is
not confused with an application failure.
