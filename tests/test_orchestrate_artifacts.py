"""Tests for durable full-fidelity agent result artifacts."""

from __future__ import annotations

import hashlib
import json
import stat

from execraft.orchestrate.artifacts import AgentArtifactStore


def test_agent_artifact_store_preserves_long_structured_output(tmp_path) -> None:
    final_message = "```json\n" + json.dumps(
        {
            "verdict": "changes_required",
            "findings": ["finding-" + ("x" * 7000)],
            "summary": "review complete",
        }
    ) + "\n```"
    store = AgentArtifactStore(tmp_path / "artifacts")

    reference = store.persist(
        project_id="sample",
        package_id="WP10",
        stage="review",
        capability="review",
        agent_id="claude-code",
        result={"ok": True, "final_message": final_message},
    )

    encoded = reference.path.read_bytes()
    record = json.loads(encoded)
    assert record["result"]["final_message"] == final_message
    assert reference.sha256 == hashlib.sha256(encoded).hexdigest()
    assert reference.size_bytes == len(encoded)
    assert stat.S_IMODE(reference.path.stat().st_mode) == 0o600
