from __future__ import annotations

from types import SimpleNamespace

import pytest

from execraft.orchestrate.operator_risk_cli import run_accept_risk_cli


class _Orchestrator:
    def __init__(self) -> None:
        self.calls = []

    def accept_operator_risk(self, package_id, *, reason, expected_sequence=None):
        self.calls.append((package_id, reason, expected_sequence))
        return {"package_id": package_id, "decision_id": "risk-1", "next_stage": "verify"}


def _args(**overrides):
    values = {
        "package_id": "WP3",
        "acknowledge_unverified": True,
        "accept_reason": "operator accepts residual risk",
        "expected_action_sequence": 7,
        "project_id": "project",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_accept_risk_cli_delegates_policy_and_renders_resume(capsys):
    orchestrator = _Orchestrator()
    assert run_accept_risk_cli(orchestrator, _args(), task_id="task-1") == 0
    assert orchestrator.calls == [("WP3", "operator accepts residual risk", 7)]
    output = capsys.readouterr().out
    assert "risk-1" in output
    assert "execraft orchestrate run --project project --task-id task-1" in output


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"package_id": ""}, "requires --package-id"),
        ({"acknowledge_unverified": False}, "requires --acknowledge-unverified"),
        ({"accept_reason": "  "}, "requires --accept-reason"),
    ],
)
def test_accept_risk_cli_validates_required_explicit_acknowledgement(overrides, message):
    with pytest.raises(ValueError, match=message):
        run_accept_risk_cli(_Orchestrator(), _args(**overrides), task_id="task-1")
