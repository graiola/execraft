from __future__ import annotations

from pathlib import Path

import yaml

from execraft.agents.config import parse_execution_config
from execraft.runtime.config_migration import preview_agents_v4_migration


def test_migration_preserves_wp13_policy_without_making_it_topology(tmp_path: Path) -> None:
    agents = tmp_path / "agents.yaml"
    policy = {
        "enabled": False,
        "profiles": {
            "worker": {
                "enabled": False,
                "max_spawn_depth": 1,
                "allowed_tools": ["read", "grep", "glob"],
            }
        },
    }
    agents.write_text(
        yaml.safe_dump(
            {
                "schema_version": 3,
                "subagents": policy,
                "providers": {
                    "worker": {
                        "adapter": "codex",
                        "enabled": True,
                        "provider_id": "worker",
                        "capabilities": ["implement"],
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    preview = preview_agents_v4_migration(agents)
    rendered = yaml.safe_load(preview.rendered_yaml)

    assert rendered["schema_version"] == 4
    assert rendered["subagents"] == policy
    # The normalized execution parser accepts the co-located subagent policy but
    # does not make it another execution-topology source of truth.
    execution = parse_execution_config(rendered, include_disabled=True)
    assert execution.agent("worker").runtime_id.startswith("native-")
