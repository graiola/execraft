from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from execraft.orchestrate.identity import (
    namespaced_storage_key,
    resolve_storage_identity,
)


def test_first_project_keeps_legacy_task_path_and_reuses_it(tmp_path: Path):
    first = resolve_storage_identity(
        tmp_path, project_id="alpha", task_id="shared"
    )
    repeated = resolve_storage_identity(
        tmp_path, project_id="alpha", task_id="shared"
    )

    assert first.storage_key == "shared"
    assert first.legacy_compatible is True
    assert repeated == first
    assert (first.state_dir / ".execraft-project-id").read_text().strip() == "alpha"
    assert first.journal_path == tmp_path / "journals" / "shared.json"


def test_conflicting_project_gets_deterministic_namespaced_path(tmp_path: Path):
    first = resolve_storage_identity(
        tmp_path, project_id="alpha", task_id="shared"
    )
    second = resolve_storage_identity(
        tmp_path, project_id="beta", task_id="shared"
    )

    assert first.state_dir != second.state_dir
    assert second.storage_key == namespaced_storage_key("beta", "shared")
    assert second.legacy_compatible is False
    assert second.journal_path.name == f"{second.storage_key}.json"
    assert (second.state_dir / ".execraft-project-id").read_text().strip() == "beta"


def test_concurrent_projects_cannot_claim_the_same_legacy_path(tmp_path: Path):
    def resolve(project: str):
        return resolve_storage_identity(
            tmp_path, project_id=project, task_id="same-task"
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        identities = list(pool.map(resolve, ["alpha", "beta"]))

    assert len({item.state_dir for item in identities}) == 2
    assert sum(item.legacy_compatible for item in identities) == 1
    owners = {
        (item.state_dir / ".execraft-project-id").read_text().strip()
        for item in identities
    }
    assert owners == {"alpha", "beta"}


def test_empty_project_id_preserves_historical_behavior(tmp_path: Path):
    identity = resolve_storage_identity(
        tmp_path, project_id="", task_id="legacy"
    )

    assert identity.state_dir == tmp_path / "projects" / "legacy"
    assert identity.journal_path == tmp_path / "journals" / "legacy.json"
    assert not (identity.state_dir / ".execraft-project-id").exists()


def test_empty_owner_marker_is_rejected_as_corrupt(tmp_path: Path):
    marker = tmp_path / "projects" / "shared" / ".execraft-project-id"
    marker.parent.mkdir(parents=True)
    marker.write_text("", encoding="utf-8")

    with pytest.raises(ValueError, match="corrupt empty orchestration owner marker"):
        resolve_storage_identity(
            tmp_path, project_id="alpha", task_id="shared"
        )
