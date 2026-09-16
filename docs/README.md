# Documentation

This directory contains the maintained product and operator documentation for
`Execraft`. It describes the supported product, operator workflows, extension
points, and release contracts. Private-project records and transient engineering
artifacts are intentionally excluded.


## Public project and releases

- [`../README.md`](../README.md) — project overview and quick start.
- [`../CHANGELOG.md`](../CHANGELOG.md) — public release history.
- [`../CONTRIBUTING.md`](../CONTRIBUTING.md) — contribution workflow and validation.
- [`../SECURITY.md`](../SECURITY.md) — private vulnerability reporting policy.
- [`deployment.md`](deployment.md) — build, GitHub Release, and optional PyPI publishing process.

## Getting started

- [`guide.md`](guide.md) — end-to-end operator workflow.
- [`start-workflow.md`](start-workflow.md) — one-command project/task startup.
- [`onboarding-architecture.md`](onboarding-architecture.md) — project discovery, registration, and onboarding behavior.
- [`control-plane-home.md`](control-plane-home.md) — installed control-plane layout and resolution.
- [`project-profiles.md`](project-profiles.md) — project descriptor/profile lifecycle.
- [`project-roadmaps.md`](project-roadmaps.md) — interactive project-level planning over canonical tasks.
- [`project-exports.md`](project-exports.md) — vector SVG/PDF Roadmap, Project Execution, and Task exports.
- [`project-execution.md`](project-execution.md) — canonical project-level execution graph and ownership boundary.
- [`project-phases.md`](project-phases.md) — deterministic Phase lifecycle and health.
- [`project-gates.md`](project-gates.md) — evidence-bound project boundary decisions.
- [`project-milestones.md`](project-milestones.md) — immutable achieved capability baselines.
- [`project-delivery.md`](project-delivery.md) — provider-neutral delivery candidates, adapters, and recovery.
- [`project-execution-recovery.md`](project-execution-recovery.md) — crash-safe intents and reconciliation.
- [`project-execution-roadmap-integration.md`](project-execution-roadmap-integration.md) — Roadmap v2 canonical asset projection and migration.
- [`task-terminology.md`](task-terminology.md) — reserved Project/Task vocabulary and compatibility policy.

## Task and workspace lifecycle

- [`gui-onboarding.md`](gui-onboarding.md) — GUI onboarding flows.
- [`gui-task-lifecycle.md`](gui-task-lifecycle.md) — task lifecycle controls.
- [`task-definition-import.md`](task-definition-import.md) — importing reviewed task definitions.
- [`task-replanning.md`](task-replanning.md) — versioned plan changes.
- [`task-completion.md`](task-completion.md) — completion and archive prerequisites.
- [`catalog_archive.md`](catalog_archive.md) — reversible catalog archival.
- [`workspace-lifecycle-safety.md`](workspace-lifecycle-safety.md) — worktree ownership and safe destruction.
- [`workspace-finalization.md`](workspace-finalization.md) — final integration and cleanup.
- [`gui_workspace_commits.md`](gui_workspace_commits.md) — reviewed workspace commits.

## Orchestration and recovery

- [`orchestration-data-flow.md`](orchestration-data-flow.md) — runtime control flow and durable provenance.
- [`execution_policy.md`](execution_policy.md) — scheduling, capabilities, execution preferences, and workflow skills.
- [`scope-checks.md`](scope-checks.md) — repository/path write boundaries.
- [`provider-promotions.md`](provider-promotions.md) — bounded temporary execution-profile promotions.
- [`supervisor.md`](supervisor.md) — privileged recovery boundaries.
- [`recovery_playbooks.md`](recovery_playbooks.md) — deterministic recovery paths.
- [`context-budgeting.md`](context-budgeting.md) — context capsules and token budgets.
- [`repository-synchronization.md`](repository-synchronization.md) — upstream synchronization transactions.

## GUI and operator surfaces

- [`gui-workbench.md`](gui-workbench.md) — orchestration workbench responsibilities and interactions.
- [`workflow_gui.md`](workflow_gui.md) — graph/list workflow visualization and navigation.
- [`agent_console.md`](agent_console.md) — live agent observation and interaction.

## Runtimes and model routing

- [`supported-runtime-architecture.md`](supported-runtime-architecture.md) — supported runtime/product boundary.
- [`runtime-configuration.md`](runtime-configuration.md) — agent profiles, runtimes, model routes, targets, and compatibility projection.
- [`runtime-execution.md`](runtime-execution.md) — runtime execution contract and identity.
- [`adding-a-runtime.md`](adding-a-runtime.md) — third-party runtime registration.
- [`model-routing.md`](model-routing.md) — model-route and execution-target ownership.
- [`local_ollama.md`](local_ollama.md) — local Ollama configuration.
- [`satellite_nodes.md`](satellite_nodes.md) — inference endpoint nodes.
- [`satellite_ubuntu.md`](satellite_ubuntu.md) — Ubuntu inference-node setup.
- [`satellite_windows.md`](satellite_windows.md) — Windows inference-node setup.
- [`openclaw-gateway.md`](openclaw-gateway.md) — public Gateway protocol and lifecycle.
- [`openclaw-model-projection.md`](openclaw-model-projection.md) — deterministic model/target projection into OpenClaw.
- [`openclaw-runtime-execution.md`](openclaw-runtime-execution.md) — OpenClaw execution behavior.
- [`openclaw-session-continuation.md`](openclaw-session-continuation.md) — persistent session continuation and invalidation.
- [`openclaw-skill-bridge.md`](openclaw-skill-bridge.md) — workflow-skill projection.
- [`openclaw-context-optimization.md`](openclaw-context-optimization.md) — bounded context/cache optimization.
- [`openclaw-security.md`](openclaw-security.md) — OpenClaw security boundary.

## Architecture, quality, and release

- [`application-boundaries.md`](application-boundaries.md) — CLI, GUI, orchestration, transaction, and extension boundaries.
- [`architectural-invariants.md`](architectural-invariants.md) — non-negotiable system constraints.
- [`lock-hierarchy.md`](lock-hierarchy.md) — process-safe lock ordering.
- [`compatibility-ledger.md`](compatibility-ledger.md) — intentionally retained compatibility behavior.
- [`quality-checks.md`](quality-checks.md) — lint, typing, test, architecture, and documentation checks.
- [`ci-and-maintenance.md`](ci-and-maintenance.md) — local preflight, CI, packaging, and repository hygiene.
- [`deployment.md`](deployment.md) — packaging and release deployment.

## Documentation rules

1. Document current supported behavior and externally relevant constraints.
2. Use generic examples such as project `sample`, task `feature_auth`, and repositories `backend`, `frontend`, or `infrastructure`.
3. Keep private project names, customer data, personal infrastructure, and organization branding out of the repository.
4. Prefer short runnable examples over transcripts and internal engineering records.
5. Keep durable compatibility requirements in [`compatibility-ledger.md`](compatibility-ledger.md).
