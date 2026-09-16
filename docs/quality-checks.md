# Quality Checks

This document describes the quality checks that are actually enforced by the
repository. The executable sources of truth are `pyproject.toml`,
`tools/preflight.py`, `tools/check_architecture.py`, `tools/check_docs.py`, and the GitHub workflows.

## Configuration ownership

`pyproject.toml` is the repository's single source of truth for package metadata,
setuptools discovery/package data, optional dependencies, entry points, pytest,
Ruff, and mypy configuration. Do not add a root `pytest.ini`, `setup.cfg`, or
`setup.py` that duplicates those settings. If a future compatibility requirement
really needs one of those files, document the ownership boundary here and keep a
single authoritative value for each setting.

This rule applies to the `execraft` repository itself. Onboarding and project
inspection may still recognize `pytest.ini`, `setup.cfg`, or `setup.py` in
external product repositories as valid ecosystem signals.

## Test execution and isolation

Pytest configuration lives in `[tool.pytest.ini_options]` in `pyproject.toml`:

- tests are discovered under `tests/`;
- `src/` is added to the Python import path;
- strict marker validation is enabled;
- `-ra` reports skips and other non-pass outcomes.

Tests that create files, Git repositories, worktrees, configuration, or durable
state must use caller-owned disposable locations, normally pytest's `tmp_path`.
CLI-style tests must redirect process-wide execraft config/state homes when those
paths are exercised. Tests must not write to the repository root or rely on a
shared hardcoded `/tmp` location. Pure in-memory tests do not need a temporary
path fixture.

Capability-dependent integration tests may skip only when the environment truly
lacks the required capability (for example, loopback socket creation in a
restricted sandbox); an application assertion failure must remain a failure.
Parallel execution is not a documented guarantee unless and until the suite is
explicitly validated under a parallel runner.

The normal backend CI matrix runs the non-browser suite on Python 3.10 and 3.12.
Browser journeys are deliberately separate because Chromium is an external
runtime dependency.

## Documentation contracts

`tools/check_docs.py` is dependency-free and runs in the mandatory preflight. It
enforces:

- local Markdown links and image paths resolve inside the repository;
- every top-level documentation topic is indexed from `docs/README.md`;
- retired GUI terminology does not reappear in maintained documentation.

The release tree contains maintained product/operator documentation only. Private
project records, temporary evidence, and engineering-program records do not belong
in `docs/`.

## Static analysis: Ruff

`[tool.ruff.lint]` in `pyproject.toml` enables the repository-wide
correctness-critical baseline:

- `E4`, `E7`, `E9`;
- `F63`, `F7`, `F82`.

`tools/preflight.py` additionally runs Ruff Bugbear (`B`) over `tools/` and a
small set of typed orchestration modules. Existing coverage may expand but must
not silently contract.

Potential future rule families such as import ordering, naming, or pyupgrade are
not current checks and must not be documented as if they already run.

## Type checking: mypy

`[tool.mypy]` in `pyproject.toml` owns the gradual typed scope. It currently
covers:

```text
src/execraft/cli_parsers
src/execraft/gui/routes
src/execraft/orchestrate/acceptance.py
src/execraft/orchestrate/agent_wait.py
```

The configured scope requires typed function definitions, checks untyped bodies,
forbids implicit Optional, and reports redundant casts and stale ignores.
Expansion should preserve existing strictness rather than adding blanket ignores.

## Architecture budgets

`tools/check_architecture.py` is the authoritative non-regression check for source
shape. It enforces, among other boundaries:

- new Python modules stay at or below 800 lines unless an explicit recorded
  baseline already exists;
- new functions stay at or below 150 lines unless present in the recorded
  baseline allow-list;
- CLI parsing remains delegated to `execraft.cli_parsers`;
- GUI transport remains separate from `execraft.gui.routes`;
- package finalization and provider-wait logic remain delegated to their focused
  modules.

The baseline allow-lists are technical debt ceilings, not target sizes. New code
must not grow an existing ceiling merely to pass the checker.

## Mandatory local preflight

Install the test and quality extras, then run the same fast check used by CI:

```bash
python -m pip install -e '.[test,quality]'
python tools/preflight.py
```

Preflight runs, in order:

1. architecture-boundary checks;
2. dependency-free documentation contracts;
3. repository-wide Ruff correctness lint;
4. incremental Bugbear lint;
5. side-effect-free Python compilation using a temporary bytecode cache;
6. the version-controlled focused regression test set.

The repository does not currently ship a pre-commit hook configuration or a
Makefile-based quality contract. Do not describe `make test`, `make lint`, or
pre-commit hooks as required project checks unless those mechanisms are actually
added.

## Full release verification

The maintained local equivalent of CI is:

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

The package workflow additionally installs the built wheel into an isolated
virtual environment outside the checkout and validates packaged GUI and typing
assets. See `ci-and-maintenance.md` for the complete release contract.

## Enforcement policy

- Pull requests run the CI workflows.
- Push CI covers the integration branches configured in the workflows.
- `execraft workspace verify` applies project/task verification profiles; those
  commands are project configuration, not a replacement for this repository's
  own CI checks.
- Reducing lint/type/architecture coverage requires an explicit rationale in the
  same change and an update to the owning executable/configuration contract and
  this document. Baseline allow-lists must not be expanded merely to make new
  code pass.

## Deterministic GUI browser lane

The dedicated GUI workflow pins the browser harness through the `browser`
optional dependency (`playwright==1.57.0`) and runs on the fixed Ubuntu 24.04
runner family. `python -m playwright install --with-deps chromium` therefore
installs the Chromium revision matched to that Playwright release together with
its browser dependencies/fonts. The lane runs both the HTTP dashboard journeys
and the network-free production-surface suite, including responsive and
100/250/500-block Roadmap scale contracts.

### Coordination forensic browser contract

The network-free production-surface Playwright suite also exercises the R8
coordination diagnostics at 1440×900. It verifies the read-only three-way
Before/Recorded desired/Current comparison, bounded terminal history rendering,
and absence of editable recovery fields or embedded force/overwrite controls.
Semantic DOM/geometry assertions remain the primary browser regression mechanism;
pixel screenshot baselines are intentionally not mandatory because the pinned
browser lane already provides deterministic rendering while avoiding raster/font
churn as a correctness gate.
