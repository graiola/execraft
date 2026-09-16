"""Keep the WP6 modularity boundaries executable in the normal test suite."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def test_architecture_boundaries() -> None:
    path = Path(__file__).resolve().parents[1] / "tools" / "check_architecture.py"
    spec = importlib.util.spec_from_file_location("execraft_architecture_check", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.validate() == []
