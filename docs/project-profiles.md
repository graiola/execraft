# Project profiles, features, and safe upgrades

Versioned project profiles replace copied universal configuration with an explicit
policy model. A profile defines orchestration policy; composable features add language,
build, runtime, editor, verification, instruction, and optional workspace assets.
Product repositories remain untouched. Everything is rendered into the portable
project descriptor and disposable task workspace shells.

## Profiles

List installed profile versions:

```bash
execraft project profiles
execraft project profiles --json
```

Built-in profiles selectable for a new project are:

- `starter@1`: approval-first, sequential execution, manual commits, supervisor disabled;
- `standard@2`: conservative approval-first adoption profile;
- `standard@3`: balanced policy with bounded parallel package execution;
- `autonomous@1`: supervisor and automatic-commit policy enabled, while destructive
  actions still require a human decision.

`standard@1` is a **compatibility-only** profile retained so older project
descriptors, adoption baselines, and migrations remain reproducible. It is still
resolvable by exact reference, but it is intentionally omitted from New Project
selectors. The GUI must never recommend a compatibility-only profile for
greenfield creation or a newly adopted source tree.

Providers are always generated disabled. Selecting an autonomous profile changes
policy, but does not silently authenticate or enable a provider.

Create a project with an exact profile version:

```bash
execraft init --template starter@1
execraft init --template autonomous@1
```

Unversioned references resolve to the latest installed version. Exact versions
make bootstrap and CI output reproducible.

## Features

List installed features:

```bash
execraft project features
execraft project features --json
```

The built-in catalog includes `core`, `python`, `cmake`, `ros2`, `docker`,
`javascript`, `rust`, `go`, `java`, `multi-repo`, and `devcontainer`.

Language and build features are inferred from bounded, non-executing discovery.
Additional features may be requested explicitly:

```bash
execraft init --feature ros2 --feature docker
```

Feature contributions are merged deterministically and de-duplicated by feature
ID. Verification commands remain disabled until reviewed. Feature detection never
runs source code, package managers, build tools, or containers.

## Provider-neutral instructions

Every profile-backed project generates one canonical
`instructions/AGENTS.md`. It contains project ownership, task-contract, safety,
and ecosystem guidance shared by Codex, Claude Code, OpenCode, and future
providers. Provider shells may reference this file, but must not copy divergent
instructions into separate permanent configuration.

## Optional source container

Add a generated dev-container to disposable task workspace shells:

```bash
execraft init --devcontainer
# or later:
execraft project upgrade PROJECT --devcontainer --dry-run
```

The descriptor stores the definition under `devcontainer/`. Workspace rendering
copies it to `.devcontainer/`. `execraft` never opens the container, downloads an
image, or modifies the product checkout during discovery or upgrade.

## Provenance

Profile-backed projects use project schema version 3 and record:

```yaml
profile: standard@3
features:
  - core@1
  - python@1
generated_with: execraft 0.1.0
provenance_file: .execraft-template.yaml
```

`.execraft-template.yaml` contains the exact profile and feature references plus a
SHA-256 baseline for every managed file. Task dossiers and arbitrary custom files
are excluded.

The baseline allows upgrades to distinguish:

- an unchanged managed file that can be updated safely;
- a new managed file that can be added;
- an obsolete unchanged file that can be deleted;
- a locally modified or locally deleted managed file that requires resolution;
- an unmanaged file that must not be overwritten.

## Checking and applying upgrades

Preview the latest compatible profile and feature versions:

```bash
execraft project check-update PROJECT
execraft project check-update PROJECT --json
```

Preview a profile switch or feature change:

```bash
execraft project upgrade PROJECT \
  --profile autonomous \
  --feature docker \
  --remove-feature javascript \
  --dry-run
```

Apply only after reviewing the file-level plan:

```bash
execraft project upgrade PROJECT --yes
```

By default, any locally modified managed file blocks the upgrade. An operator can
replace those files explicitly:

```bash
execraft project upgrade PROJECT --force-managed --yes
```

The upgrade engine stages the full target profile first, takes backups of touched
files, publishes files atomically, commits provenance last, and restores touched
files on failure. It never traverses or modifies `tasks/`.

## Adopting legacy projects

Schema-1/2 projects have no managed-file baseline. Adoption is therefore an
explicit mutation:

```bash
execraft project upgrade PROJECT \
  --adopt \
  --profile standard@1 \
  --yes
```

Adoption records the current descriptor files as the baseline without modifying
them. The same command then plans and applies the requested/latest upgrade.
Task dossiers are excluded from adoption. For maximum control, adopt first with a
dry-run/check cycle before applying a later profile upgrade.

## JSON automation contract

`check-update` and `upgrade --dry-run --json` report:

- current and target profile;
- current and target feature references;
- provenance state;
- add/update/delete/conflict/unchanged action per managed file;
- baseline, current, and target SHA-256 values;
- `up_to_date`, `can_apply`, change count, and conflict count.

Automation should treat `provenance_missing` and non-zero `conflicts` as a hard
operator Hold.
