"""Canonical names for Native runtime adapters."""

from __future__ import annotations


SUPPORTED_NATIVE_ADAPTERS = frozenset(
    {
        "codex",
        "claude",
        "claude-code",
        "opencode",
        "antigravity",
        "antigravity-cli",
    }
)


def default_native_binary(adapter: str) -> str:
    """Return the historical CLI binary default for a Native adapter."""

    if adapter in {"claude", "claude-code"}:
        return "claude"
    if adapter in {"antigravity", "antigravity-cli"}:
        return "agy"
    return adapter
