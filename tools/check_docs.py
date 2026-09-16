#!/usr/bin/env python3
"""Validate release-facing Markdown documentation.

The checker is intentionally dependency-free so local and CI preflight use the
same rules. It validates local links, documentation-index coverage, and retired
GUI terminology across every maintained Markdown document.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
DOC_INDEX = ROOT / "docs" / "README.md"

_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
_RETIRED_PHRASES = (
    "Diagnostics → Providers",
    "Diagnostics -> Providers",
    "Current assignments",
    "System Health",
    "Milestone dialog",
    "Milestone modal",
    "Timeline (default)",
    "Timeline as the default",
    "milestone-presenter.js",
    "milestone-execution.js",
    "milestone-inspector.js",
    "test_gui_milestone_inspector_regression.py",
    "repository synchronization milestone",
    "scope_gates.md",
    "quality-gates.md",
)


def _all_markdown_paths() -> set[str]:
    paths = {"README.md"}
    paths.update(path.relative_to(ROOT).as_posix() for path in (ROOT / "docs").rglob("*.md"))
    return paths


def _destination_path(source: Path, destination: str) -> Path | None:
    destination = destination.strip()
    if destination.startswith("<") and destination.endswith(">"):
        destination = destination[1:-1]
    destination = destination.split(maxsplit=1)[0]
    parsed = urlsplit(destination)
    if parsed.scheme or parsed.netloc or destination.startswith(("mailto:", "#")):
        return None
    raw_path = unquote(parsed.path)
    if not raw_path:
        return None
    return (source.parent / raw_path).resolve()


def _local_links(path: Path) -> list[tuple[str, Path]]:
    text = path.read_text(encoding="utf-8")
    links: list[tuple[str, Path]] = []
    for match in _LINK_RE.finditer(text):
        destination = match.group(1)
        target = _destination_path(path, destination)
        if target is not None:
            links.append((destination, target))
    return links


def _check_links(paths: set[str]) -> list[str]:
    errors: list[str] = []
    for rel in sorted(paths):
        source = ROOT / rel
        for destination, target in _local_links(source):
            try:
                target.relative_to(ROOT)
            except ValueError:
                errors.append(f"{rel}: local link escapes repository: {destination}")
                continue
            if not target.exists():
                errors.append(f"{rel}: broken local link: {destination}")
    return errors


def _check_index() -> list[str]:
    indexed_targets = {
        target.relative_to(ROOT).as_posix()
        for _, target in _local_links(DOC_INDEX)
        if target.exists() and target.is_file()
    }
    errors: list[str] = []
    for path in sorted((ROOT / "docs").glob("*.md")):
        rel = path.relative_to(ROOT).as_posix()
        if rel == "docs/README.md":
            continue
        if rel not in indexed_targets:
            errors.append(f"{rel}: topic is missing from docs/README.md")
    return errors


def _check_retired_phrases(paths: set[str]) -> list[str]:
    errors: list[str] = []
    for rel in sorted(paths):
        text = (ROOT / rel).read_text(encoding="utf-8")
        for phrase in _RETIRED_PHRASES:
            if phrase.casefold() in text.casefold():
                errors.append(f"{rel}: retired documentation phrase remains: {phrase!r}")
    return errors


def run_checks() -> list[str]:
    paths = _all_markdown_paths()
    return [
        *_check_links(paths),
        *_check_index(),
        *_check_retired_phrases(paths),
    ]


def main() -> int:
    try:
        errors = run_checks()
    except OSError as exc:
        print(f"documentation check error: {exc}", file=sys.stderr)
        return 2
    if errors:
        for error in errors:
            print(f"DOC ERROR: {error}", file=sys.stderr)
        print(f"documentation checks: FAIL ({len(errors)} issue(s))", file=sys.stderr)
        return 1
    print("documentation checks: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
