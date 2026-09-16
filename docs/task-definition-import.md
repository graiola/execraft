# Task-definition import

Execraft can create a task from an existing `BRIEF.md`, `PLAN.md`, and optionally
`PLAN.graph.yaml`. Imported documents live in the control-plane task dossier;
they are never copied into product repositories or task worktrees.

## Supported inputs

| Imported input | Creation behavior |
| --- | --- |
| `BRIEF.md` | Preserve the brief and generate `PLAN.md` plus `PLAN.graph.yaml` during `execraft start`. |
| `PLAN.md` | Preserve the plan, derive a minimal brief, and generate only `PLAN.graph.yaml`. |
| `BRIEF.md` + `PLAN.md` | Preserve both files and generate only the executable graph. In provider planning mode, the provider also checks semantic coherence. |
| `PLAN.md` + `PLAN.graph.yaml` | Preserve both, derive a minimal brief, and validate the graph without requiring an AI call. |
| all three | Preserve all three and validate the executable graph without requiring an AI call. |

`execraft task new` performs the dossier import but does not invoke a planning
provider. `execraft start` additionally ensures that a valid executable graph
exists.

## CLI

Start directly from existing documents:

```bash
execraft start \
  --brief-file ./BRIEF.md \
  --plan-file ./PLAN.md \
  --planner auto
```

A natural-language positional description is optional when at least one task
definition document is imported:

```bash
execraft start --plan-file ./PLAN.md --planner local
```

Import an already reviewed executable graph:

```bash
execraft start \
  --brief-file ./BRIEF.md \
  --plan-file ./PLAN.md \
  --plan-graph-file ./PLAN.graph.yaml \
  --planner agent
```

The last command does not require the agent merely because `--planner agent`
was selected: a complete valid imported graph is already executable. Use
`--require-agent` when provider availability itself is an operational
requirement.

Create only the dossier:

```bash
execraft task new imported-task \
  --project my-project \
  --brief-file ./BRIEF.md \
  --plan-file ./PLAN.md
```

When `--title` is omitted, Execraft prefers the first Markdown H1 from the imported
brief or plan, then a package title from an imported graph, then a bounded semantic
excerpt. Without an imported definition, `task new` still requires an explicit
title.

## Preservation and safety

Imported text is preserved exactly except that CRLF/CR line endings are
normalized to LF. Execraft:

- accepts UTF-8 regular files only;
- rejects symbolic links;
- rejects empty imported documents;
- bounds every imported document to 2 MiB;
- validates imported YAML before publication;
- rejects executable graphs that are cyclic, incomplete, or reference
  repositories outside the selected task scope;
- never silently overwrites an existing task dossier;
- publishes the dossier through the existing atomic task transaction.

When `PLAN.md` is imported and its graph is missing, planning writes only
`PLAN.graph.yaml`; the imported Markdown is not rewritten or whitespace-normalized
again.

## Provenance

Every newly created task contains `DEFINITION.yaml`. It records:

- schema and definition revision;
- source fingerprint;
- original start-request SHA-256;
- origin and SHA-256 for `BRIEF.md`, `PLAN.md`, and `PLAN.graph.yaml`;
- generated-provider metadata when planning creates a missing plan or graph;
- current definition hashes;
- consistency-check mode and summary when applicable.

Original imported documents are copied to:

```text
imports/revision-0001/BRIEF.md
imports/revision-0001/PLAN.md
imports/revision-0001/PLAN.graph.yaml
```

Only files actually imported are present in that directory. This immutable
revision-1 snapshot allows later replanning work to distinguish user-supplied
content from generated content.

## Planning and consistency

For an imported `PLAN.md` without a graph:

- `--planner local` performs structural validation and emits a conservative
  single-package graph while preserving the plan;
- `--planner agent` requires a safe read-only planning provider, asks it to
  verify BRIEF/PLAN semantic coherence, and asks it to return only the graph;
- `--planner auto` prefers the same semantic check and graph generation, but may
  fall back to the deterministic graph if the provider itself is unavailable or
  fails operationally.

A provider finding that `BRIEF.md` and `PLAN.md` materially contradict each
other is a semantic failure, not an operational provider failure. `auto` mode
therefore stops instead of hiding that finding behind a local fallback.

A fully imported valid `PLAN.graph.yaml` is treated as the executable contract
and is reused without an AI call.

## Resumability

The start journal stores the accepted request SHA-256 and import provenance,
not duplicate copies of potentially large imported documents. After the dossier
exists, a resumable GUI or CLI flow can safely reuse it by presenting the
expected request hash. Execraft verifies that hash against `DEFINITION.yaml`
before reusing the task.

Changing `BRIEF.md` or `PLAN.md` after execution has started is intentionally
outside this import feature. That operation requires the versioned replanning
workflow so completed and active package state can be reconciled safely.

## Revisions after task creation

`DEFINITION.yaml` revision 1 is the baseline for active-task change detection.
Do not update imported or generated `BRIEF.md`, `PLAN.md`, or `PLAN.graph.yaml`
in place while orchestration is running. Replanning verifies their accepted hashes on
resume and routes intentional changes through the versioned replanning flow.
See [Versioned task replanning](task-replanning.md).
