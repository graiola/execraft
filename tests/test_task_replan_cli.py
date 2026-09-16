"""CLI guardrails for versioned task replanning."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from execraft.cli import _read_replan_request_file, _validate_replan_cli_mode
from execraft.workspace.task_git import TaskGitError


def _args(**overrides) -> Namespace:
    values = {
        "brief_file": None,
        "plan_file": None,
        "plan_graph_file": None,
        "request": "",
        "request_file": None,
        "supersede": [],
        "provider": "",
        "allow_structural": False,
        "from_current_files": False,
        "apply": False,
        "recover": False,
        "candidate": "",
    }
    values.update(overrides)
    return Namespace(**values)


def test_replan_cli_rejects_ambiguous_modes() -> None:
    with pytest.raises(TaskGitError, match="either --request or --request-file"):
        _validate_replan_cli_mode(
            _args(request="change", request_file=Path("request.txt"))
        )
    with pytest.raises(TaskGitError, match="cannot be combined"):
        _validate_replan_cli_mode(
            _args(from_current_files=True, plan_file=Path("PLAN.md"))
        )
    with pytest.raises(TaskGitError, match="standalone"):
        _validate_replan_cli_mode(_args(recover=True, apply=True))
    with pytest.raises(TaskGitError, match="may only be combined"):
        _validate_replan_cli_mode(_args(candidate="r0002-test", provider="planner"))


def test_replan_request_file_is_bounded_utf8_and_normalized(tmp_path: Path) -> None:
    request = tmp_path / "request.txt"
    request.write_bytes(" First\r\nsecond \r".encode("utf-8"))
    assert _read_replan_request_file(request) == "First\nsecond"

    oversized = tmp_path / "oversized.txt"
    oversized.write_bytes(b"x" * 33)
    with pytest.raises(TaskGitError, match="safety limit"):
        _read_replan_request_file(oversized, maximum_bytes=32)


def test_replan_request_file_rejects_symlink_and_invalid_text(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("change\n", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    with pytest.raises(TaskGitError, match="symbolic link"):
        _read_replan_request_file(link)

    invalid = tmp_path / "invalid.txt"
    invalid.write_bytes(b"\xff")
    with pytest.raises(TaskGitError, match="UTF-8"):
        _read_replan_request_file(invalid)

    nul = tmp_path / "nul.txt"
    nul.write_bytes(b"change\x00bad")
    with pytest.raises(TaskGitError, match="NUL"):
        _read_replan_request_file(nul)
