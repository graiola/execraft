"""Contract tests for the shared local/CI preflight runner."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_preflight_module():
    path = ROOT / "tools" / "preflight.py"
    spec = importlib.util.spec_from_file_location("execraft_preflight", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_default_preflight_contains_all_mandatory_a0_checks(tmp_path):
    preflight = _load_preflight_module()

    steps = preflight.build_steps(bytecode_cache=tmp_path / "pycache")
    names = [step.name for step in steps]

    assert names == [
        "architecture boundaries",
        "documentation contracts",
        "Ruff correctness lint",
        "Ruff incremental bugbear lint",
        "Python compilation",
        "focused regression tests",
    ]
    assert steps[1].command[-1] == "tools/check_docs.py"
    assert steps[2].command[-4:] == ("check", "src", "tests", "tools")
    assert "ruff" in Path(steps[2].command[0]).name or "ruff" in steps[2].command
    assert steps[3].command[steps[3].command.index("--select") + 1] == "B"
    assert steps[4].environment["PYTHONPYCACHEPREFIX"].endswith("pycache")
    assert set(preflight.FOCUSED_TESTS).issubset(set(steps[5].command))
    for test_spec in preflight.FOCUSED_TESTS:
        test_path = test_spec.split("::", 1)[0]
        assert (ROOT / test_path).is_file(), test_spec
    assert not any("milestone" in spec.casefold() for spec in preflight.FOCUSED_TESTS)


def test_preflight_stops_after_first_failure(monkeypatch):
    preflight = _load_preflight_module()
    executed: list[str] = []

    def fake_run(step):
        executed.append(step.name)
        return 7 if step.name == "first" else 0

    monkeypatch.setattr(preflight, "_run_step", fake_run)
    steps = (
        preflight.PreflightStep("first", ("first",)),
        preflight.PreflightStep("second", ("second",)),
    )

    assert preflight.run_preflight(steps) == 7
    assert executed == ["first"]
