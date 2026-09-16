# Reversible project and task catalog archive

The project workspace **Archive** view retires inactive project or task dossiers from normal active
selectors without deleting them. It is intentionally separate from the immutable
completion evidence archive managed by `execraft task close --archive`.

## Two archive concepts

| Archive | Purpose | Location | Reactivation |
|---|---|---|---|
| Catalog archive | Hide inactive project/task dossiers while preserving them for later work | `projects/.archive/` | Yes |
| Completion evidence archive | Preserve verified final evidence before workspace disposal | state archive root, normally `~/.local/state/execraft/archives/` | No; immutable evidence bundle |

A catalog archive does not claim that a task is complete. It is a reversible
catalog operation for merged, superseded, paused, or historical work.

## Storage layout

```text
projects/.archive/
├── tasks/<project-id>/<task-id>/
└── projects/<project-id>/
```

Every archived directory contains `.archive.yaml` with:

- schema and archive IDs;
- project/task identity and original relative path;
- archive timestamp and operator reason;
- a SHA-256 and size inventory for every regular file;
- total file count and bytes.

Symlinks are rejected so an archive cannot silently capture content outside its
dossier.

## Archive view

The project workspace **Archive** view lists archived tasks and projects separately. Selecting an item
shows:

- metadata, archive reason, and retained runtime-state summary;
- SHA-256 integrity verification with missing, changed, or unexpected files;
- bounded previews of key files such as `TASK.yaml`, `project.yaml`, `BRIEF.md`,
  `PLAN.md`, `HANDOFF.md`, and `REVIEW.md`;
- a **Reactivate** action enabled only when integrity verification succeeds;
- a **Delete permanently…** action for irreversible removal.

The collapsed **Active catalog** section can archive or permanently delete inactive entries. The task currently open in the application and the currently focused project cannot be archived or deleted. A project cannot be archived or deleted while any task driver lock is held.

## Permanent removal

Permanent removal is intentionally separate from archival and task completion. It removes Execraft-owned lifecycle data for an explicit task or project and cannot be undone. The GUI requires the operator to type the exact ID. The CLI provides:

```text
execraft task delete <task-id> --project <project-id> --dry-run
execraft task delete <task-id> --project <project-id> --yes
execraft project delete <project-id> --dry-run
execraft project delete <project-id> --yes
```

`--delete-branches` is optional. It removes only local task branches whose workspace record proves that Execraft created them; pre-existing and protected branches are preserved. Source repositories are never recursively deleted. Externally registered project descriptors and their project directories are also preserved when deleting the project from Execraft.

The removal service cleans active/catalog-archived dossiers that Execraft owns, task/workspace registries, collision-safe orchestration state and journals, start journals, completion archives (including a custom `--archive-root`), and matching completion-index records. Active workspaces are retired through the existing guarded workspace lifecycle before their registry records are removed.

Deleting an individual task from inside an archived project is rejected because doing so would invalidate the archived project's integrity inventory; reactivate the project first or delete the archived project as a whole.

## Reactivation

Reactivation verifies the recorded inventory before moving the dossier back to
its original active location. It fails closed when:

- an active destination already exists;
- a project is still archived when one of its tasks is being restored;
- any archived file is missing, modified, or unexpected.

After a successful restore, `.archive.yaml` is moved into
`.execraft-archive-history/<archive-id>.yaml` with a `reactivated_at` timestamp. This
preserves the catalog operation history without exposing the restored dossier as
still archived.

## Repository state

Catalog archive contents are runtime/project state, not a fixed part of the product
documentation. The active and archived task lists shown by the CLI/GUI are derived
from the current control-plane catalog. Use the project workspace Archive view or catalog/archive
commands to inspect the state of a particular installation instead of relying on
historical task names in this document.
