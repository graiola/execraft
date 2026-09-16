# Application boundaries

`execraft` keeps application boundaries explicit, but it does not wrap ordinary
Python relationships in registries, factories, or forwarding façades unless
runtime substitution is actually required.

## CLI

`execraft.cli` owns top-level command composition and composes its parser from
`execraft.cli_parsers`. Top-level dispatch is a plain mapping from the public
command/alias to its handler; there is no command registry or dependency-injection
container. `_build_orchestrator` remains the explicit orchestration composition
seam used by tests and callers.

Configuration interpretation belongs to `execraft.cli_config`. Orchestration command
behavior belongs to `execraft.cli_orchestration`, policy translation to
`execraft.cli_orchestration_policy`, and human-readable rendering to
`execraft.cli_orchestration_output`. These modules are direct functions rather than
manager/service wrappers. Parser modules must not import command execution. The
architecture check enforces this dependency direction and keeps the remaining CLI
composition module from regrowing historical responsibilities.

## Process execution

`execraft.process` is the single owner of OS subprocess supervision, process-group
lifecycle, bounded output capture, interactive PTY launch, and process telemetry.
Agent adapters and live sessions depend on this boundary directly. Orchestration
policy does not re-export process lifecycle APIs and does not own a compatibility
subprocess implementation.

The GUI manual console uses the same `execraft.process` PTY launcher but keeps its
terminal-session state in the GUI/agent-console domain. PTY descriptor/ioctl
coordination remains specialized OS code; it is not modeled as a persistence lock.

## Shared infrastructure

`execraft.persistence` owns atomic replacement, parent-directory fsync, the ordered
process-safe `FileLock`, and read-only lock-state probing. Domain stores do not
implement local `flock` variants. The declared acquisition order is
PROJECT_EXECUTOR → DRIVER → ORCHESTRATOR → LIFECYCLE → REPOSITORY_REF →
PROJECT_COORDINATOR → RECORD. Specialized terminal and
catalog-compatibility locks remain local only where their semantics differ from a
persistence resource lock.

`execraft.network.is_loopback_host` owns loopback classification for runtime, model
routing, GUI exposure, and compatibility projections. It normalizes host names and
uses `ipaddress` so the complete IPv4 127/8 range and IPv6 loopback have one
consistent meaning across the application.

## GUI

The GUI transport remains separate from domain routes. `ControlCenterService`
owns project/task context and project-independent catalog archive operations; a
`DashboardService` is created only after a task is opened. The application
depends on one `TaskDashboard` protocol containing only the three operations and
identity fields it actually consumes; it does not split that surface into
one-method interface fragments. Project-home routes own onboarding/session/archive
HTTP dispatch, project-roadmap routes own planning CRUD and task-link dispatch,
while task-dashboard routes own task/run/workspace/configuration dispatch. Route
modules must not import the HTTP transport server.

`execraft.roadmap` is a project-level planning domain. Its repository owns versioned
atomic YAML and optimistic concurrency; its projection joins canonical task and
runtime state; its service owns planning mutations. It deliberately does not
depend on task-dashboard execution controls and roadmap `blocks` relations do not
become orchestrator dependencies.

`execraft.export` is a read-only presentation boundary. Domain-aware projection code
may read Roadmap, Project Execution, and Task state, but SVG/PDF/layout renderers
consume only immutable export presentation models. Renderers must not import
canonical domain repositories or GUI state; `tools/check_architecture.py` enforces
that direction.

Persistent GUI configuration uses the same atomic persistence primitive as the
rest of the application so mode preservation, replacement, and directory fsync
semantics have one owner.

## Orchestration

`ProjectOrchestrator` owns the package state machine and durable orchestration
lifecycle. Cohesive subdomains live in canonical modules and are called
directly rather than through forwarding methods:

- `scope_recovery` owns declared-scope evaluation, reconciliation, cleanup, and
  agent-assisted scope recovery.
- `provider_failover` owns provider progression, contract-repair retry,
  exclusion relaxation, and failover.
- `supervisor_coordinator` owns supervisor rounds and bounded delegation.
- `supervisor_contract` compiles permissive Supervisor prose/JSON into the strict internal decision model; execution effects remain validated by orchestrator-owned checks.
- `agent_attempt` owns one provider attempt and its durable invocation/health
  bookkeeping.
- `package_finalization` owns commit/finalization policy and assessments.

The orchestrator exposes only the few scope/supervisor operations that form a
meaningful external façade. Internal orchestration code calls the owning
component directly instead of maintaining duplicate pass-through APIs.

Provider failure classifications map to `Availability` in one scheduler-owned
contract. Adapters may add transport-specific diagnostics, but they do not each
maintain copies of the provider-neutral availability table.

## Progress reporting

Human progress formatting is deterministic and direct. The reporter formats
known event types through one formatter and silently ignores unknown events, as
before. There is no event registry because the application has only one
formatter and no runtime handler substitution requirement.

## Replanning and persistence

`ReplanService` owns the complete replan transaction lifecycle: prepare,
durable commit marker, rollback, integrity verification, and crash recovery.
Those operations are private implementation details of the service rather than
a second coordinator that forwards calls back to its host.

Atomic file replacement is owned by `execraft.persistence.atomic`. Archive,
onboarding, replan, and GUI code use that implementation instead of maintaining
local variants with subtly different fsync, permission, and symlink behavior.
Streaming file hashing is similarly owned by `execraft.persistence.files.sha256_file`
so integrity checks do not duplicate whole-file reads or hashing loops.

## Maintained extension seams

Extension points stay deliberately small and explicit:

- **CLI:** add argument construction to the appropriate `execraft.cli_parsers`
  domain module and map a genuinely new top-level command directly in
  `execraft.cli._command_handlers()`. Do not introduce a command registry or
  factory merely to hold static dispatch.
- **GUI API:** keep route behavior HTTP-independent and compose a new domain
  route through `RouteGroup` in `execraft.gui.routes.router`. Route modules must
  not import the transport server.
- **Agent providers:** implement the existing `AgentAdapter` contract, extend
  provider configuration validation, and construct the adapter in
  `execraft.runtime.native.build_native_runtime`. Provider-neutral availability and
  retry semantics remain scheduler-owned instead of being copied into adapters.
- **Onboarding:** add reusable discovery detectors, templates, profiles, or
  features through their existing catalogs. Creation/publication remains owned
  by onboarding transactions rather than by the extension itself.
- **Persistence:** reuse the shared atomic-write and hashing primitives before
  adding a local filesystem helper.
- **Project delivery:** implement external distribution/deployment integrations
  behind `execraft.project_execution.delivery.DeliveryProvider`. Canonical
  `ProjectMilestone` and `PROJECT_EXECUTION.yaml` must not acquire provider,
  credential, registry, or deployment-backend fields. Logical target IDs are
  resolved by the adapter/configuration layer.

These are composition seams, not invitations to add speculative interfaces.
When a feature has one implementation and no runtime substitution requirement,
prefer a direct function/module relationship.

## Architecture debt ceilings

`tools/check_architecture.py` is a non-regression check, not an endorsement of
large modules. Existing oversized files/functions have explicit ceilings; when
a module shrinks, its ceiling is lowered. New oversized modules/functions
fail the check. This allows incremental simplification without requiring a risky
repository-wide rewrite.
