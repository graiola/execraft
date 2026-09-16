from __future__ import annotations

from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

import execraft

from execraft.cli_parsers import build_parser
from execraft.control_plane import (
    CONFIG_HOME_ENV,
    CONTROL_ROOT_ENV,
    STATE_HOME_ENV,
    default_control_root,
    xdg_config_home,
    xdg_state_home,
)


def test_public_distribution_and_version_are_execraft() -> None:
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert metadata["name"] == "execraft"
    assert metadata["version"] == "0.1.0"
    assert execraft.__version__ == "0.1.0"


def test_public_cli_and_environment_names_are_execraft(monkeypatch, tmp_path: Path) -> None:
    parser = build_parser()
    assert parser.prog == "execraft"
    assert CONTROL_ROOT_ENV == "EXECRAFT_CONTROL_ROOT"
    assert CONFIG_HOME_ENV == "EXECRAFT_CONFIG_HOME"
    assert STATE_HOME_ENV == "EXECRAFT_STATE_HOME"

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv(CONTROL_ROOT_ENV, raising=False)
    monkeypatch.delenv(CONFIG_HOME_ENV, raising=False)
    monkeypatch.delenv(STATE_HOME_ENV, raising=False)

    assert default_control_root() == (tmp_path / "data" / "execraft" / "control").resolve()
    assert xdg_config_home() == (tmp_path / "config" / "execraft").resolve()
    assert xdg_state_home() == (tmp_path / "state" / "execraft").resolve()


def test_gui_public_identity_is_execraft() -> None:
    index = Path("src/execraft/assets/gui/index.html").read_text(encoding="utf-8")
    assert "<title>Execraft Control Center</title>" in index
    assert '<h1>Execraft</h1>' in index
    assert '/assets/execraft-mark.png' in index
    assert 'name="execraft-token"' in index
