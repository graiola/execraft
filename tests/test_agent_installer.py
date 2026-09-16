"""Unit tests for the non-root Ubuntu agent installer."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from execraft.agents import installer
from execraft.agents.installer import AgentInstaller, _ensure_profile_path, _node_major, select_specs


class _Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _Runner:
    def __init__(self):
        self.calls = []

    def __call__(self, args, *, env=None):
        self.calls.append(list(args))
        if list(args) == ["node", "--version"]:
            return _Completed(stdout="v20.18.0\n")
        if args[0] in {"npm", "bash"}:
            return _Completed(stdout="installed\n")
        return _Completed(stdout="1.2.3\n")


def _prepare(monkeypatch):
    monkeypatch.setattr(installer.platform, "system", lambda: "Linux")
    monkeypatch.setattr(installer, "_ubuntu_like", lambda: True)
    monkeypatch.setattr(
        installer.shutil, "which", lambda binary, path=None: f"/usr/bin/{binary}"
    )


def test_select_specs_preserves_catalog_order_and_all():
    assert [item.id for item in select_specs(["antigravity", "codex"])] == [
        "codex",
        "antigravity",
    ]
    assert [item.id for item in select_specs(["all"])] == [
        "codex",
        "claude",
        "opencode",
        "antigravity",
    ]
    with pytest.raises(ValueError, match="unknown agents"):
        select_specs(["gemini"])


def test_node_major_parser():
    assert _node_major("v20.18.0") == 20
    assert _node_major("22.1.0") == 22
    assert _node_major("garbage") is None


def test_profile_path_update_is_idempotent(tmp_path):
    profile = tmp_path / ".profile"
    bin_dir = tmp_path / ".local" / "bin"
    assert _ensure_profile_path(profile, bin_dir) is True
    assert _ensure_profile_path(profile, bin_dir) is False
    assert profile.read_text().count(str(bin_dir)) == 1


def test_npm_dry_run_uses_user_prefix_without_sudo(tmp_path, monkeypatch):
    runner = _Runner()
    _prepare(monkeypatch)
    prefix = tmp_path / "prefix"
    instance = AgentInstaller(
        prefix=prefix, runner=runner, env={"PATH": os.environ.get("PATH", "")}
    )

    result = instance.install(select_specs(["codex"]), dry_run=True)

    assert result[0].detail == (
        f"npm install --global --prefix {prefix.resolve()} @openai/codex@latest"
    )
    assert "sudo" not in result[0].detail


def test_antigravity_dry_run_uses_official_installer(monkeypatch):
    runner = _Runner()
    _prepare(monkeypatch)
    instance = AgentInstaller(prefix=Path.home() / ".local", runner=runner)

    result = instance.install(select_specs(["antigravity"]), dry_run=True)

    detail = result[0].detail
    assert detail.startswith("bash -lc ")
    assert "https://antigravity.google/cli/install.sh" in detail
    assert "--skip-path" not in detail
    assert "--skip-aliases" not in detail
    assert "| bash" in detail
    assert "bash -s" not in detail
    assert "sudo" not in detail
    assert ["node", "--version"] not in runner.calls


def test_antigravity_rejects_nonstandard_prefix(tmp_path, monkeypatch):
    _prepare(monkeypatch)
    instance = AgentInstaller(prefix=tmp_path / "prefix", runner=_Runner())

    with pytest.raises(RuntimeError, match="targets ~/.local"):
        instance.install(select_specs(["antigravity"]), dry_run=True)


def test_install_verifies_selected_binary(tmp_path, monkeypatch):
    runner = _Runner()
    _prepare(monkeypatch)
    instance = AgentInstaller(prefix=tmp_path / "prefix", runner=runner)

    result = instance.install(select_specs(["codex"]))

    assert result[0].installed is True
    assert result[0].version == "1.2.3"
    assert any(call[0] == "npm" and "@openai/codex@latest" in call for call in runner.calls)
