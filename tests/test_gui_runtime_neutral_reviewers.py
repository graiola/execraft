"""The operator surfaces must not silently drop non-Native candidates.

Adding an OpenClaw runtime to a project used to break every provider-oriented
GUI surface outright, because they went through the Native-only projection,
which fails closed on a mixed topology. Once that is fixed the subtler problem
remains: a Native-only listing omits the OpenClaw candidate entirely, so an
operator cannot select a reviewer that is configured and healthy.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from execraft.agents.config import parse_agent_configs, project_native_agent_configs
from execraft.agents.config_errors import AgentConfigError

ROOT = Path(__file__).resolve().parents[1]


def _mixed_agents() -> dict[str, Any]:
    return {
        "schema_version": 4,
        "runtimes": {
            "native-codex": {"kind": "native", "adapter": "codex", "binary": "codex"},
            "openclaw-local": {
                "kind": "openclaw",
                "mode": "managed",
                "gateway": "ws://127.0.0.1:18789",
                "auth_kind": "none",
            },
        },
        "execution_targets": {
            "local-ollama": {"kind": "local", "endpoint": "http://127.0.0.1:11434/v1"}
        },
        "model_routes": {
            "route": {
                "provider": "ollama",
                "model": "qwen3-coder:30b",
                "endpoint": "http://127.0.0.1:11434/v1",
                "default_target": "local-ollama",
            }
        },
        "agents": {
            "codex": {"runtime": "native-codex", "capabilities": ["review", "implement"]},
            "openclaw-local-review": {
                "runtime": "openclaw-local",
                "capabilities": ["review"],
                "model_route": "route",
            },
        },
    }


def test_native_only_projection_fails_closed_on_a_mixed_topology() -> None:
    """This is why the provider-oriented surfaces could not use it."""

    with pytest.raises(AgentConfigError, match="legacy provider-only API"):
        parse_agent_configs(_mixed_agents(), include_disabled=True)


def test_provider_surfaces_keep_working_and_report_what_they_omit() -> None:
    native, note = project_native_agent_configs(_mixed_agents())

    assert [item.provider_id for item in native] == ["codex"]
    assert "openclaw-local-review" in note
    assert "openclaw" in note


class _Health:
    status = "healthy"
    reason = ""
    is_available = True


class _HealthStore:
    def get(self, _candidate_id: str) -> _Health:
        return _Health()


class _Service:
    """Minimal stand-in for the dashboard surface the listing reads."""

    def __init__(self, root: Path, project_id: str) -> None:
        self.root = root
        self.project_id = project_id
        self.health_store = _HealthStore()


def test_reviewer_picker_includes_candidates_on_every_runtime() -> None:
    from execraft.gui.runtime_topology import review_capable_candidates

    rows = review_capable_candidates(_Service(ROOT, "sample"), "sample")

    by_id = {row["id"]: row for row in rows}
    assert "openclaw-local-review" in by_id, (
        "a configured, review-capable OpenClaw candidate must be selectable"
    )
    openclaw = by_id["openclaw-local-review"]
    assert openclaw["runtime_kind"] == "openclaw"
    assert openclaw["runtime_id"] == "openclaw-local"
    # The picker labels rows by model, and candidates on different runtimes can
    # share one, so each row must say which runtime it would run on.
    assert openclaw["model"]
    assert any(row["runtime_kind"] == "native" for row in rows)


def test_shipped_sample_project_exposes_the_openclaw_reviewer() -> None:
    raw = yaml.safe_load(
        (ROOT / "projects/sample/agents.yaml").read_text(encoding="utf-8")
    )
    profiles = raw["agents"]

    assert "openclaw-local-review" in profiles
    assert profiles["openclaw-local-review"]["runtime"] == "openclaw-local"
    assert raw["runtimes"]["openclaw-local"]["kind"] == "openclaw"
