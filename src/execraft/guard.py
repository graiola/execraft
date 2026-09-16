"""Boundary guardrails: detect AI-only paths in product repositories."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterator

# Patterns that match AI-only paths in product repositories.
# These are checked as `.gitignore`-style globs relative to the repo root.
BANNED_PATH_PATTERNS: list[str] = [
    "tools/ai/**",
    "docs/ai/**",
    ".agents/**",
    ".claude/**",
    ".codex/**",
    ".gemini/**",
    ".opencode/**",
    "AGENTS.md",
    "CLAUDE.md",
    "opencode.json",
    "opencode.jsonc",
    ".ai-task.env",
    ".ai-workspace/**",
    ".execraft/**",
    ".ai-task/**",
]

# Content patterns that indicate AI-only implementation (NOT product discussion).
# These are regex patterns matched against file content.
BANNED_CONTENT_PATTERNS: list[re.Pattern] = [
    re.compile(r"tools/ai/verify\.sh"),
    re.compile(r"\.agents/skills/"),
    re.compile(r"Execraft.*orchestrat"),
]


def is_banned_path(relative_path: str) -> bool:
    """Check if *relative_path* matches any banned pattern.

    Supports ``**`` globs (multi-segment wildcard) and ``*`` (single-segment).
    """
    from fnmatch import fnmatch

    for pattern in BANNED_PATH_PATTERNS:
        if fnmatch(relative_path, pattern):
            return True
    return False


def scan_repository(
    root: Path,
    *,
    include_content_check: bool = False,
) -> Iterator[tuple[str, str]]:
    """Yield ``(path, reason)`` for every banned file/directory in *root*.

    Only checks tracked (Git-committed) paths when *root* is a Git working
    tree. When *include_content_check* is true, also scans file content for
    banned patterns.
    """
    git_dir = root / ".git"
    have_git = git_dir.is_dir() or git_dir.is_file()

    if have_git:
        yield from _scan_git_tracked(root, include_content=include_content_check)
    else:
        yield from _scan_filesystem(root, include_content=include_content_check)


def _scan_git_tracked(root: Path, *, include_content: bool) -> Iterator[tuple[str, str]]:
    """Use ``git ls-files`` to list tracked paths only."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=str(root),
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return
    if result.returncode != 0:
        return

    for line in result.stdout.splitlines():
        path = line.strip()
        if not path:
            continue
        full = root / path
        if is_banned_path(path):
            yield (path, "banned path pattern")
        elif include_content and full.is_file():
            reason = _check_content(full)
            if reason:
                yield (path, reason)


def _scan_filesystem(root: Path, *, include_content: bool) -> Iterator[tuple[str, str]]:
    """Walk the filesystem for banned paths."""
    for current, dirs, files in os.walk(str(root)):
        rel = os.path.relpath(current, str(root))
        if rel == ".":
            rel = ""
        for name in files:
            path = os.path.join(rel, name) if rel else name
            if is_banned_path(path):
                yield (path, "banned path pattern")
            elif include_content:
                full = Path(current) / name
                reason = _check_content(full)
                if reason:
                    yield (path, reason)
        for name in dirs:
            path = os.path.join(rel, name) if rel else name
            if is_banned_path(path + "/"):
                yield (path + "/", "banned path pattern")


def _check_content(filepath: Path) -> str | None:
    """Check file content for banned patterns. Returns reason or None."""
    try:
        text = filepath.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for pattern in BANNED_CONTENT_PATTERNS:
        if pattern.search(text):
            return f"banned content pattern: {pattern.pattern}"
    return None


def report(
    root: Path,
    *,
    include_content_check: bool = False,
    verbose: bool = False,
) -> int:
    """Scan *root* and print findings. Returns 0 if clean, 1 if violations found."""
    findings = list(scan_repository(root, include_content_check=include_content_check))
    if not findings:
        print(f"OK: {root} — no AI-only paths detected")
        return 0

    print(f"VIOLATIONS: {root}")
    for path, reason in sorted(findings):
        print(f"  {path}  ({reason})")
    print(f"Total: {len(findings)} violation(s)")
    return 1
