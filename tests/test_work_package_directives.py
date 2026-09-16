from execraft.orchestrate.directives import (
    PAUSE_BEFORE_START,
    REQUIRE_DECOMPOSITION,
    WorkPackageDirectiveQueue,
)


def test_directive_queue_supersedes_pending_value_and_preserves_latest(tmp_path):
    queue = WorkPackageDirectiveQueue(tmp_path / "work-package-directives.json")

    first = queue.enqueue(
        package_id="WP2",
        kind=PAUSE_BEFORE_START,
        enabled=True,
        reason="inspect before launch",
        requested_by="test",
    )
    second = queue.enqueue(
        package_id="WP2",
        kind=PAUSE_BEFORE_START,
        enabled=False,
        requested_by="test",
    )
    queue.enqueue(
        package_id="WP2",
        kind=REQUIRE_DECOMPOSITION,
        enabled=True,
        reason="split the work",
        requested_by="test",
    )

    pending = queue.pending()
    assert len(pending) == 2
    assert {item.kind for item in pending} == {
        PAUSE_BEFORE_START,
        REQUIRE_DECOMPOSITION,
    }
    assert second.id in {item.id for item in pending}
    effective = queue.effective_pending()["WP2"]
    assert effective[PAUSE_BEFORE_START].enabled is False
    assert effective[REQUIRE_DECOMPOSITION].enabled is True

    queue.resolve({second.id}, status="applied", result="removed")
    assert all(item.id != second.id for item in queue.pending())
    assert first.id != second.id


def test_directive_queue_never_truncates_pending_commands(tmp_path):
    queue = WorkPackageDirectiveQueue(tmp_path / "work-package-directives.json")
    queue.history_limit = 2

    pending = queue.enqueue(
        package_id="M-pending",
        kind=PAUSE_BEFORE_START,
        enabled=True,
    )
    for index in range(5):
        command = queue.enqueue(
            package_id=f"M{index}",
            kind=REQUIRE_DECOMPOSITION,
            enabled=True,
        )
        queue.resolve({command.id}, status="applied")

    assert pending.id in {item.id for item in queue.pending()}


def test_directive_queue_round_trips_repository_sync_parameters(tmp_path):
    from execraft.orchestrate.directives import PAUSE_FOR_REPOSITORY_SYNC

    queue = WorkPackageDirectiveQueue(tmp_path / "work-package-directives.json")
    command = queue.enqueue(
        package_id="WP20",
        kind=PAUSE_FOR_REPOSITORY_SYNC,
        enabled=True,
        reason="sync upstream",
        parameters={
            "mode": "before",
            "repositories": ["core"],
            "source_branches": {"core": "release/test"},
            "auto_resume": False,
        },
    )

    restored = WorkPackageDirectiveQueue(tmp_path / "work-package-directives.json").pending()[0]
    assert restored.id == command.id
    assert restored.parameters["mode"] == "before"
    assert restored.parameters["source_branches"] == {"core": "release/test"}
    assert restored.parameters["auto_resume"] is False


def test_directive_queue_rejects_malformed_persisted_parameters(tmp_path):
    import json
    import pytest
    from execraft.orchestrate.directives import WorkPackageDirectiveError

    path = tmp_path / "work-package-directives.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "commands": [
                    {
                        "id": "bad",
                        "package_id": "WP20",
                        "kind": "pause_for_repository_sync",
                        "enabled": True,
                        "parameters": ["not", "a", "mapping"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkPackageDirectiveError, match="parameters must be a mapping"):
        WorkPackageDirectiveQueue(path).pending()


def test_legacy_milestone_directive_queue_migrates_one_way(tmp_path):
    import json

    legacy = tmp_path / "milestone-directives.json"
    canonical = tmp_path / "work-package-directives.json"
    legacy.write_text(
        json.dumps({
            "schema_version": 1,
            "commands": [{
                "id": "legacy-command",
                "package_id": "WP9",
                "kind": PAUSE_BEFORE_START,
                "enabled": True,
                "status": "pending",
            }],
        }),
        encoding="utf-8",
    )

    queue = WorkPackageDirectiveQueue(canonical)
    pending = queue.pending()

    assert [item.id for item in pending] == ["legacy-command"]
    assert canonical.is_file()
    assert not legacy.exists()
    payload = json.loads(canonical.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    assert payload["commands"][0]["package_id"] == "WP9"


def test_historical_milestone_event_names_remain_readable():
    from execraft.orchestrate.event_compat import canonical_task_event_type

    assert canonical_task_event_type("milestone_directive_applied") == "work_package_directive_applied"
    assert canonical_task_event_type("milestone_pause_before_start_reached") == "work_package_pause_before_start_reached"
    assert canonical_task_event_type("work_package_directive_applied") == "work_package_directive_applied"
