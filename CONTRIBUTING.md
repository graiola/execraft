# Contributing to Execraft

Thank you for considering a contribution to **Execraft**.

## Before you start

- Read [`docs/architectural-invariants.md`](docs/architectural-invariants.md) before changing orchestration, persistence, recovery, or workspace ownership.
- Keep Project Execution and Task Execution responsibilities separate.
- Preserve fail-closed behavior at scope, verification, review, commit, and recovery boundaries.
- Do not add private project names, organization branding, credentials, personal infrastructure paths, or customer data to examples, tests, fixtures, or documentation.
- Prefer small, focused changes with regression coverage over broad rewrites.

## Development setup

```bash
git clone https://github.com/graiola/execraft.git
cd execraft
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test,browser,quality]'
python tools/preflight.py
```

For browser tests, install the matching Chromium build:

```bash
python -m playwright install chromium
```

## Validation

Run the fast mandatory check first:

```bash
python tools/preflight.py
```

For release-level validation:

```bash
mypy
python -m pytest -q --ignore=tests/test_gui_browser.py
find src/execraft/assets/gui -name '*.js' -print0 | xargs -0 -n1 node --check
python -m pytest -q tests/test_gui_browser.py
python -m pytest -q tests/test_gui_project_surfaces_browser.py
python -m build
```

See [`docs/quality-checks.md`](docs/quality-checks.md) for the authoritative contract.

## Pull requests

A good pull request should:

1. explain the user-visible or architectural problem;
2. keep the change within the owning domain boundary;
3. include tests for behavior that changed;
4. update documentation when public behavior changes;
5. pass the repository preflight and relevant focused suites;
6. avoid committing generated state, virtual environments, credentials, or local path bindings.

## Compatibility

Do not remove compatibility behavior solely because it looks old. Durable compatibility
requirements are tracked in [`docs/compatibility-ledger.md`](docs/compatibility-ledger.md).
When compatibility is removed, update the owning tests and documentation in the same
change.

## Reporting bugs and proposing features

Use GitHub Issues for reproducible bugs and feature proposals. Include the Execraft
version, Python version, platform, relevant command, and the smallest safe reproduction.
Never attach secrets, provider credentials, or private repository contents.

For vulnerabilities, use the private process in [`SECURITY.md`](SECURITY.md).
