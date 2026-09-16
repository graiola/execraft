# Project visualization and export

`Execraft` can render presentation-quality project artifacts without taking a
browser screenshot or changing execution state. Exporters read canonical
Roadmap, Project Execution, and Task projections and convert them into an
immutable presentation model before rendering.

## Architectural boundary

The supported flow is:

```text
Roadmap / Project Execution / Task
                │
                ▼
       ExportProjectionBuilder
                │
                ▼
       presentation contracts
          │               │
          ▼               ▼
     SVG renderer      PDF renderer
```

Renderers must not read repositories, runtime state, Task dossiers, or GUI DOM
state directly. They depend only on `execraft.export` presentation contracts. This
keeps SVG and PDF semantically identical and allows future renderers to be
added without duplicating domain queries.

Exports are read-only. Generating a report never starts a Task, reconciles
Project Execution, changes a Roadmap, or creates Project Execution runtime
state.

## Roadmap poster

Roadmaps can be exported as native SVG or a vector A3-style PDF poster. The
layout preserves Roadmap lanes, canonical Task titles and runtime progress,
Phase/Gate/Milestone state, dates, and planning relationships.

SVG is the preferred format for presentation and design workflows because it
is resolution independent and can be edited in normal vector tools.

```bash
execraft export roadmap \
  --project sample \
  --roadmap platform-2026 \
  --format svg \
  --theme dark \
  --output platform-2026.svg
```

Available SVG themes are `dark`, `light`, and `executive`.

For a poster PDF:

```bash
execraft export roadmap \
  --project sample \
  --roadmap platform-2026 \
  --format pdf \
  --output platform-2026.pdf
```

## Project Execution report

A Project Execution report is a multi-page PDF containing an executive summary
plus canonical Phase, Gate, Milestone, and Task status.

```bash
execraft export project \
  --project sample \
  --format pdf \
  --output sample-project-report.pdf
```

The report consumes existing durable Project Execution observations. It does
not call the Project executor's side-effecting or reconciling operations.

## Task report

Task exports summarize the canonical Task and its Work Packages. The detailed
PDF includes Task execution progress and Work Package stage, risk, and
acceptance-criterion progress. The SVG form is useful as a compact visual
summary for reviews and documentation.

```bash
execraft export task \
  --project sample \
  --task feature_auth \
  --format pdf
```

```bash
execraft export task \
  --project sample \
  --task feature_auth \
  --format svg \
  --theme executive
```

## GUI

The Project Roadmap toolbar exposes SVG/PDF export. Project Execution exposes a
PDF report action. An open Task exposes PDF and SVG summary actions. Browser
exports use the authenticated GUI API and binary download responses; export
endpoints are protected by the same dashboard token as other privileged read
surfaces.

## Renderer constraints

- SVG/PDF rendering is deterministic and offline.
- The core implementation adds no browser, Cairo, wkhtmltopdf, or external PDF
  dependency.
- PDF uses vector primitives and standard built-in PDF fonts.
- Renderers never mutate domain state.
- Presentation models are immutable and are the only renderer input.
- Provider-specific delivery behavior remains separate from exports.
