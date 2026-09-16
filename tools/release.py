#!/usr/bin/env python3
"""Bump the project version and publish a matching vX.Y.Z release tag.

The Release workflow refuses a tag that does not equal ``v`` plus
``project.version`` in ``pyproject.toml`` (see ``.github/workflows/release.yml``),
so a release is exactly this sequence: run the mandatory preflight, bump the
version, commit, push ``main``, and push a ``vX.Y.Z`` tag on that commit.
Performing it by hand invites the two failure modes this script prevents:
releasing from a dirty or unsynced checkout, and pushing a tag the version
guard will reject (or moving a tag without meaning to).

Run from any directory inside the checkout::

    python tools/release.py 0.2.1                # bump, push, tag, release
    python tools/release.py 0.2.1 --dry-run      # print the plan, change nothing
    python tools/release.py 0.2.1 --force        # move an existing vX.Y.Z tag

``--force`` exists to repair a mis-tagged release: it deletes the existing
remote tag and re-creates it on the new release commit, which re-triggers the
Release workflow. It never touches any other tag.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
BRANCH = "main"
_VERSION_RE = re.compile(r"\d+\.\d+\.\d+")
_VERSION_KEY_RE = re.compile(r'version\s*=\s*"([^"]*)"')


def _fail(message: str) -> None:
    print(f"release error: {message}", file=sys.stderr)
    raise SystemExit(2)


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", *args], cwd=ROOT, text=True, capture_output=True, check=False
    )
    if check and completed.returncode != 0:
        _fail(
            f"git {' '.join(args)} failed: "
            f"{(completed.stderr or completed.stdout).strip()}"
        )
    return completed


def _version_line_index(lines: Sequence[str]) -> int | None:
    """Return the index of the ``version`` key inside the ``[project]`` table."""

    in_project = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_project = stripped == "[project]"
            continue
        if in_project and _VERSION_KEY_RE.match(line):
            return index
    return None


def _version_key(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def _remote_tag_commit(tag: str) -> str | None:
    """Return the commit a remote tag resolves to, peeling annotated tags."""

    ref = f"refs/tags/{tag}"
    completed = _git("ls-remote", "origin", ref, ref + "^{}")
    commit = None
    for line in completed.stdout.splitlines():
        sha, _, name = line.partition("\t")
        name = name.strip()
        if name == ref:
            commit = sha.strip()
        elif name == ref + "^{}":
            commit = sha.strip()
    return commit


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bump the project version and publish a vX.Y.Z release tag."
    )
    parser.add_argument("version", help="release version, e.g. 0.2.1 (v prefix optional)")
    parser.add_argument(
        "--force",
        action="store_true",
        help="delete an existing remote vX.Y.Z tag and re-create it on the "
        "new release commit (required to repair a mis-tagged release)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the release plan without modifying anything",
    )
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="skip the mandatory preflight gate (CI still runs it on the push)",
    )
    args = parser.parse_args(argv)

    version = args.version.strip().removeprefix("v")
    if not _VERSION_RE.fullmatch(version):
        _fail(f"version {args.version!r} must be three dot-separated numbers, e.g. 0.2.1")
    tag = f"v{version}"

    if _git("status", "--porcelain").stdout.strip():
        _fail("working tree is not clean; commit or stash first")
    branch = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if branch != BRANCH:
        _fail(f"releases are cut from {BRANCH!r}; current branch is {branch!r}")
    _git("fetch", "origin", "--tags")
    local_head = _git("rev-parse", "HEAD").stdout.strip()
    remote_branch = _git("rev-parse", f"origin/{BRANCH}").stdout.strip()
    if local_head != remote_branch:
        _fail(f"{BRANCH} is not in sync with origin/{BRANCH}; push or pull first")

    text = PYPROJECT.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    index = _version_line_index(lines)
    if index is None:
        _fail("pyproject.toml has no version key in [project]")
    current = _VERSION_KEY_RE.match(lines[index]).group(1)  # type: ignore[union-attr]
    if current == version:
        _fail(f"pyproject.toml is already at version {version}")
    if _version_key(version) <= _version_key(current):
        _fail(f"version {version} is not greater than the current version {current}")

    new_lines = list(lines)
    new_lines[index] = f'version = "{version}"\n'
    new_text = "".join(new_lines)
    bumped = new_text.splitlines(keepends=True)
    if _VERSION_KEY_RE.match(bumped[index]).group(1) != version:  # type: ignore[union-attr]
        _fail("internal error: version rewrite did not apply cleanly")

    remote_target = _remote_tag_commit(tag)
    if remote_target is not None and remote_target == local_head and not args.force:
        _fail(f"tag {tag} already exists on this commit; nothing to release")

    print(f"Release plan for {tag}:", flush=True)
    print(f"  - preflight {'(skipped)' if args.skip_preflight else '(mandatory gate)'}", flush=True)
    print(f"  - bump pyproject.toml version {current} -> {version}", flush=True)
    print(f"  - commit 'Release {version}' on {BRANCH}", flush=True)
    print(f"  - push {BRANCH} to origin", flush=True)
    if remote_target is not None:
        print(f"  - delete existing remote tag {tag} (at {remote_target[:12]})", flush=True)
    print(f"  - create annotated tag {tag} on the release commit and push it", flush=True)
    print("  - the Release workflow builds and publishes the artifacts", flush=True)
    if args.dry_run:
        return 0
    if remote_target is not None and not args.force:
        _fail(f"remote tag {tag} already exists; pass --force to move it onto this release")

    if not args.skip_preflight:
        print("\n==> preflight", flush=True)
        completed = subprocess.run(
            [sys.executable, "tools/preflight.py"], cwd=ROOT, check=False
        )
        if completed.returncode != 0:
            _fail(f"preflight exited with code {completed.returncode}; "
                  "fix it or pass --skip-preflight")

    PYPROJECT.write_text(new_text, encoding="utf-8")
    _git("add", "pyproject.toml")
    _git("commit", "-m", f"Release {version}")
    _git("push", "origin", BRANCH)

    release_commit = _git("rev-parse", "HEAD").stdout.strip()
    if remote_target is not None:
        _git("push", "origin", f":refs/tags/{tag}")
    local_probe = _git(
        "rev-parse", "--verify", "--quiet", f"{tag}^{{commit}}", check=False
    )
    if local_probe.returncode == 0 and local_probe.stdout.strip() != release_commit:
        _git("tag", "-d", tag)
    if local_probe.returncode != 0 or local_probe.stdout.strip() != release_commit:
        _git("tag", "-a", tag, "-m", f"Release {version}", release_commit)
    _git("push", "origin", f"refs/tags/{tag}")

    print(f"\nReleased {tag} from commit {release_commit[:12]} on origin/{BRANCH}.")
    print("The Release workflow now verifies the tag, builds, smoke-tests, and publishes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
