# Installable control-plane home and project registry

`execraft` separates executable package code from mutable user data. A wheel may be
installed in a virtual environment, system Python, `pipx`, or another isolated
location; no command depends on the package's `site-packages` path or on an
`Execraft` source checkout.

## Home resolution

The active control-plane root is resolved in this order:

1. `EXECRAFT_CONTROL_ROOT`;
2. compatibility aliases `EXECRAFT_WORKFLOW_ROOT` or `AI_WORKFLOW_ROOT`;
3. an `Execraft` source checkout containing the current directory;
4. `$XDG_DATA_HOME/execraft/control`, or `~/.local/share/execraft/control`.

The source-checkout rule preserves the existing developer workflow. Normal wheel
usage outside that checkout selects the XDG home. `execraft home --json` exposes the
resolved root, origin, configuration directory, state directory, and any legacy
migration candidates.

The default layout is:

```text
$XDG_DATA_HOME/execraft/control/
  projects/                   descriptors created by `execraft init`
  .registry/
    tasks/                    compatibility task index
    workspaces/               workspace records when the home is not a Git repo
    active-task

$XDG_CONFIG_HOME/execraft/
  projects/<project-id>.yaml  descriptor/source registrations

$XDG_STATE_HOME/execraft/
  projects/                   orchestration state
  archives/                   immutable completion archives
```

The private `.registry` indexes replace the old assumption that the control
plane itself has a Git common directory. Legacy Git-local task/workspace indexes
remain readable where applicable.

## Project registration

A project descriptor remains portable and self-contained. The host-local
registration points to it and optionally to the machine's source checkout:

```yaml
schema_version: 2
project: sample
descriptor: /absolute/path/to/sample/project.yaml
source_root: /absolute/path/to/sample/source
```

Registration files are written atomically. Descriptor paths and source roots
must be absolute. The descriptor is loaded and its project ID must match the
registration filename before it can be used.

Schema-version 1 source-only bindings remain supported:

```yaml
schema_version: 1
project: sample
source_root: /absolute/path/to/sample/source
```

Registering the descriptor upgrades that file to schema version 2 while
preserving the source binding.

## Project lookup

Project lookup is independent from `<control-root>/projects`:

- an explicit schema-v2 registration has precedence;
- otherwise the legacy `<control-root>/projects/<id>/project.yaml` route is used;
- project listing merges and de-duplicates both catalogs;
- a stale registered descriptor fails with an actionable error rather than
  silently selecting another same-ID project.

Current-directory inference chooses the deepest matching route among:

- a `project.yaml` at or above the current directory;
- a registered descriptor directory;
- a registered source root.

Equal-depth matches from different projects are rejected as ambiguous. Commands
continue to accept `--project` as an explicit override.

## Migration

`execraft home migrate --from /path/to/legacy-control-plane` registers every valid
`projects/*/project.yaml` descriptor from the old checkout. It does not copy or
move project dossiers. This makes the descriptors discoverable by a wheel-run
CLI after the operator leaves the legacy checkout or removes the legacy root
environment variable.

Migration intentionally rejects conflicting same-ID descriptor routes. An
operator must resolve that conflict rather than silently replacing one project
with another.

## Installed subprocesses

The GUI and its child orchestration commands propagate `EXECRAFT_CONTROL_ROOT`.
They add `<control-root>/src` to `PYTHONPATH` only when that directory exists,
which preserves source-checkout development without breaking wheel installs.

## Compatibility boundaries

Whole-project catalog archival is restricted to descriptors managed under the
active control-plane `projects/` directory. Externally registered task dossiers
can be archived and restored individually; moving an arbitrary external project
directory as a side effect of a catalog action is rejected.
