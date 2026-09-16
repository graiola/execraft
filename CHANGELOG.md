# Changelog

All notable public changes to **Execraft** are documented here.

The project uses semantic version tags (`vX.Y.Z`).

## [Unreleased]

No public changes recorded yet.

## [0.1.0] - 2026-09-16

### Added

- first-class Project Execution with canonical Phases, Gates, Milestones, and Task eligibility;
- Project Roadmaps with canonical Project Execution projection and presentation exports;
- operator-focused local GUI for Roadmap, Project Execution, Tasks, Work Packages, agents, execution health, and workspace changes;
- crash-safe Roadmap/Project Execution coordination with durable intent, reconciliation, forensic diagnostics, and bounded recovery actions;
- typed Gate evidence, human decisions, Milestone achievement baselines, and project delivery candidates;
- Native and documented extension/runtime boundaries, including optional OpenClaw integration;
- deterministic browser regression coverage across responsive Roadmap and Project Execution surfaces.

### Changed

- clarified Project versus Task terminology: Project Milestone/Gate/Phase are distinct from Task Work Packages, Checks, and Holds;
- improved packaging, CI, release validation, documentation structure, and public repository metadata;
- public distribution, Python import namespace, and CLI command are all `execraft`.

### Security and reliability

- strengthened lock ordering, optimistic revision checks, durable recovery, scope validation, and fail-closed coordination behavior;
- kept browser DTOs free of credential material and preserved explicit operator acknowledgement for execution-changing actions.

[Unreleased]: https://github.com/graiola/execraft/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/graiola/execraft/releases/tag/v0.1.0
