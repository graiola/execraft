# CI and structural maintenance

The project architecture, packaging contract, generated templates, and browser
surface are enforced as executable release checks. One shared fast preflight is
used locally and in CI, while slower typing, full-suite, browser, and packaging
checks remain separate for clear failure classification.

## CI pipelines

`.github/workflows/ci.yml` contains four independent jobs.

### Quality

The quality job installs both the `test` and `quality` extras and starts with the
same mandatory fast preflight used by developers:

```bash
python tools/preflight.py
```

The shared runner fails fast over six checks:

```text
architecture boundaries
documentation contracts
Ruff correctness lint
Ruff incremental Bugbear lint
Python compilation
focused regression tests
```

Compilation uses a temporary `PYTHONPYCACHEPREFIX`, so the preflight does not
leave bytecode artifacts in the checkout. After that shared contract passes, CI
runs the slower quality-specific checks:

```bash
mypy
find src/execraft/assets/gui -name '*.js' -print0 | xargs -0 -n1 node --check
```

Ruff enforces correctness-critical parser and name-resolution rules over the
complete tree, with an incremental Bugbear check over `tools/` and selected
orchestration modules. Mypy is deliberately gradual: the typed scope currently
covers CLI parsers, GUI routes, acceptance logic, and provider-wait planning.
The documentation check validates classification, local links, active-topic index
coverage, and retired workbench terminology without external packages.
Expanding that list is a forward-compatible tightening; existing untyped
orchestration code is not hidden behind blanket `ignore_errors` configuration.

Push CI follows the branch filters declared in `.github/workflows/ci.yml` and
`.github/workflows/gui-browser.yml`. Pull requests and manual dispatches remain
branch-independent. Keep the two workflow files aligned when branch policy changes;
do not infer current branch policy from task IDs used in examples or historical work.

### Tests

The non-browser suite runs on Python 3.10 and 3.12. Browser journeys remain in
the dedicated Chromium workflow so a browser installation failure cannot hide a
backend regression.

### Generated templates

Every versioned project profile and source template is created through the
normal onboarding services. The matrix verifies:

- descriptor/source/repository/workspace readiness;
- exact profile and feature versions;
- managed-file SHA-256 provenance;
- Git initialization and `main` branch creation;
- conflict-free idempotent profile upgrades.

### Packaging

The package job builds both wheel and source distribution, installs the wheel in
an isolated virtual environment outside the checkout, checks packaged GUI and
typing assets, exercises the CLI entry point, and runs the installed-control-plane
acceptance test.

## Architectural guardrails

`tools/check_architecture.py` enforces the maintained source boundaries:

- `execraft.cli` composes parsers from `execraft.cli_parsers`;
- GUI HTTP transport remains bounded and delegates API behavior to
  `execraft.gui.routes`;
- provider wait scheduling remains delegated to
  `execraft.orchestrate.agent_wait`;
- parser modules cannot import command execution;
- route modules cannot import the HTTP server.

The normal test suite imports and executes the same checker, and
`tools/preflight.py` invokes it directly, so local validation and CI use one
source of truth.

## Mandatory local preflight

Install the test and quality dependencies once:

```bash
python -m pip install -e '.[test,quality]'
```

Then run this before submitting changes:

```bash
python tools/preflight.py
```

The focused test list is intentionally explicit and version-controlled in the
runner. It covers architecture/documentation boundaries plus onboarding, bootstrap, sharding,
GUI backend, start-workflow, CI-contract, and preflight-contract regressions.
The script fails on the first broken check so the root cause is not buried under
later failures.

## Full local release checks

With all test and quality dependencies installed:

```bash
python -m pip install -e '.[test,browser,quality]'
python tools/preflight.py
mypy
python -m pytest -q --ignore=tests/test_gui_browser.py
find src/execraft/assets/gui -name '*.js' -print0 | xargs -0 -n1 node --check
python -m playwright install chromium
python -m pytest -q tests/test_gui_browser.py
python -m build
```

`tools/preflight.py` is intentionally faster than the complete release matrix.
The full non-browser, browser, typing, JavaScript, and packaging checks remain
separate so failures are easy to classify.

## Browser-test dependency

Playwright has two independent prerequisites:

1. the Python package, installed by the `browser` extra; and
2. the Chromium browser binary managed by Playwright.

Install both before running `tests/test_gui_browser.py` locally:

```bash
python -m pip install -e '.[test,browser]'
python -m playwright install chromium
python -m pytest -q tests/test_gui_browser.py
```

On Linux CI, the browser workflow installs Chromium and its system dependencies
with:

```bash
python -m playwright install --with-deps chromium
```

If the `playwright` Python package is absent, the optional browser test module is
skipped during collection. If the Python package is installed but Chromium is
missing, Playwright reaches browser-fixture startup and reports a missing
executable. That environment failure is not an application assertion failure;
install Chromium and rerun the journey.

## Local environment note

A constrained or offline package index may not provide Ruff, Mypy, or Playwright
browser binaries. `tools/preflight.py` requires the `test` and `quality` extras;
if those packages cannot be installed, run the available compilation and
architecture checks separately and rely on the committed GitHub workflows for
the authoritative lint/type/browser checks. Chromium itself is intentionally not
bundled with Execraft.

## Repository hygiene and clean source snapshots

A working checkout can contain large ignored runtime state that is not part of
the source distribution. Typical examples include `.venv/`, Python/test/lint
caches, `*.egg-info/`, `.execraft/`, `.registry/`, task dossiers under
`projects/*/tasks/`, and generated workspace-shell directories. These paths are
covered by `.gitignore`; they must not be copied into a release or review bundle
simply because they exist in the checkout.

Inspect ignored material before cleanup:

```bash
git status --short --ignored
git clean -ndX
```

`git clean -ndX` is a **preview only**. Review the list before any destructive
cleanup, especially in a developer checkout that may contain intentionally
ignored local configuration.

For source-only handoff archives, prefer the tracked tree instead of zipping the
working directory:

```bash
git archive --format=zip --output ../execraft-source.zip HEAD
```

When the bundle must include uncommitted patch work, create a normal Git patch
(or commit on a review branch) alongside a tracked-tree archive rather than
including virtual environments, caches, generated task state, or VCS internals.
This keeps review artifacts deterministic and prevents stale local dependencies
from being mistaken for repository requirements.
