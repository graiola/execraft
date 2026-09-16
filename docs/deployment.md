# Deployment and public releases

`Execraft` is a host orchestration tool. Its normal installation should run directly on
the host machine rather than inside a container: it needs ordinary access to Git
worktrees, SSH/Git credentials, provider CLI sessions, PTYs, local model servers, and
sometimes the Docker daemon itself.

The public Python **distribution name, import package, and CLI command are all `execraft`**.
The packaged implementation lives directly under the `execraft` Python namespace.

## Install a release

From a downloaded wheel:

```bash
uv tool install ./execraft-0.1.0-py3-none-any.whl
# or
pipx install ./execraft-0.1.0-py3-none-any.whl
```

After PyPI trusted publishing has been enabled for the repository:

```bash
uv tool install execraft
# or
pipx install execraft
```

A regular virtual environment also remains supported. Python 3.10 or newer and Git are
required. Provider CLIs and their authentication remain explicit host prerequisites;
`execraft-install-agents` can install supported CLI binaries but does not copy or automate
credentials.

After installation, verify host integration:

```bash
execraft --help
execraft home
execraft doctor
```

## Why the wheel is the primary artifact

A container would need to reconstruct the host environment with repository, credential,
SSH-agent, PTY, provider-session, model-server, and possibly Docker socket mounts. That
is useful for controlled CI/demo environments, but it makes the normal workstation path
harder rather than simpler.

A frozen executable would remove the Python prerequisite but would not remove Git,
provider CLIs, provider authentication, model runtimes, or Docker. It would also require
separate platform-specific build/update pipelines. A wheel keeps a single portable
release artifact while preserving direct host integration.

## GitHub release process

`.github/workflows/release.yml` builds releases from version tags. Before creating a
tag, update `project.version` in `pyproject.toml`, update `CHANGELOG.md`, and run the
release checks. Then publish the matching tag:

```bash
git tag v0.1.0
git push origin v0.1.0
```

The release workflow:

1. verifies that the tag matches `project.version`;
2. builds the wheel and source distribution;
3. writes SHA-256 checksums;
4. installs the wheel in a clean virtual environment outside the checkout;
5. smoke-tests `execraft --help` and `execraft doctor`;
6. creates a public GitHub Release containing wheel, source archive, and checksums;
7. optionally publishes the same artifacts to PyPI through trusted publishing.

`workflow_dispatch` may be used to exercise the build/smoke-test pipeline without
publishing a tagged release.

## Optional PyPI publishing

PyPI publishing is deliberately opt-in so a GitHub release does not depend on repository
configuration that may not exist yet. To enable it:

1. create the `execraft` project through PyPI Trusted Publishing for this GitHub repository
   and `.github/workflows/release.yml`;
2. enable GitHub Actions OIDC for the workflow (already requested by the committed
   workflow permissions);
3. create the repository Actions variable `PYPI_PUBLISH=true`;
4. publish a normal matching `vX.Y.Z` tag.

No long-lived PyPI API token is required when trusted publishing is configured.

## First public publication and Git history

The first public repository must be created from a **sanitized source snapshot**. Do not
make a previously private development repository public merely because its current tree
has been cleaned: removed project names, organization references, local infrastructure,
or generated artifacts can still exist in old commits.

For the first public publication, initialize a fresh Git repository from the reviewed
release tree (or use the sanitized release bundle produced during release preparation),
commit that tree as the public history root, and tag it `vX.Y.Z`. Keep the historical
private repository private. Subsequent public development can continue normally from
that clean history.

## Public repository checklist

Before publishing the sanitized repository:

- run `python tools/preflight.py`;
- run the full release checks from [`quality-checks.md`](quality-checks.md);
- confirm `rg -n -i "private organization name" .` returns no private branding;
- inspect `git status --short --ignored` for local state or credentials;
- verify `README.md`, `CHANGELOG.md`, `SECURITY.md`, and `CONTRIBUTING.md`;
- build/install the wheel outside the checkout;
- create the release tag only from the reviewed release commit.

## Containers

If a container image is added later, keep it secondary and purpose-specific: CI
reproduction, demonstrations, or a deliberately sandboxed worker. Do not make container
access a requirement for the normal CLI installation unless the host integration model
changes substantially.
