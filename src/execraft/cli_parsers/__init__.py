"""Composable construction of the public ``execraft`` command tree."""

from __future__ import annotations

import argparse

from .export import add_export_command
from .onboarding import add_onboarding_commands
from .orchestration import add_orchestration_command
from .runtime import add_runtime_commands
from .workspace import add_workspace_commands


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser from domain-owned command groups.

    Keeping parser construction outside :mod:`execraft.cli` prevents command
    execution logic from becoming coupled to a single monolithic argparse
    function and makes help-surface regression tests inexpensive.
    """

    parser = argparse.ArgumentParser(
        prog="execraft",
        description="Deterministic engineering execution for humans and AI agents",
    )
    sub = parser.add_subparsers(dest="command")
    add_onboarding_commands(sub)
    add_workspace_commands(sub)
    add_runtime_commands(sub)
    add_orchestration_command(sub)
    add_export_command(sub)
    return parser


__all__ = ["build_parser"]
