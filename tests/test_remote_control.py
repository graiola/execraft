from datetime import datetime, timedelta, timezone

import pytest

from execraft.remote import (
    Decision,
    DecisionQueue,
    RemoteAuditLog,
    RemoteCommand,
    RemoteControlService,
    RemoteRequest,
    RemoteStoreError,
    ReplayGuard,
)


def _request(command, *, request_id="req-1", actor="user-1", payload=None, age=0):
    return RemoteRequest(
        request_id=request_id,
        actor_id=actor,
        command=command,
        project_id="sample",
        task_id="sample_task",
        created_at=(datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat(),
        payload=payload or {},
    )


def _service(tmp_path, *, read_only=False):
    return RemoteControlService(
        allowed_actor_ids={"user-1"},
        handlers={RemoteCommand.STATUS: lambda request: {"state": "running"}},
        replay_guard=ReplayGuard(tmp_path / "requests.txt"),
        audit_log=RemoteAuditLog(tmp_path / "audit.jsonl"),
        decision_queue=DecisionQueue(tmp_path / "decisions.json"),
        read_only=read_only,
    )


def test_remote_command_enum_has_no_arbitrary_shell():
    assert "shell" not in {item.value for item in RemoteCommand}
    assert "exec" not in {item.value for item in RemoteCommand}


def test_authorized_read_only_command_is_dispatched(tmp_path):
    assert _service(tmp_path).dispatch(_request(RemoteCommand.STATUS)) == {"state": "running"}


def test_unknown_actor_and_replay_are_rejected(tmp_path):
    service = _service(tmp_path)
    with pytest.raises(RemoteStoreError, match="unauthorized"):
        service.dispatch(_request(RemoteCommand.STATUS, actor="other"))
    service.dispatch(_request(RemoteCommand.STATUS))
    with pytest.raises(RemoteStoreError, match="duplicate"):
        service.dispatch(_request(RemoteCommand.STATUS))


def test_read_only_mode_blocks_control(tmp_path):
    service = _service(tmp_path, read_only=True)
    with pytest.raises(RemoteStoreError, match="read-only"):
        service.dispatch(_request(RemoteCommand.PAUSE))


def test_expired_request_is_rejected(tmp_path):
    with pytest.raises(RemoteStoreError, match="expired"):
        _service(tmp_path).dispatch(_request(RemoteCommand.STATUS, age=1000))


def test_decision_requires_current_revision_and_one_time_nonce(tmp_path):
    service = _service(tmp_path)
    queue = service.decision_queue
    queue.put(
        Decision(
            id="DEC-1",
            project_id="sample",
            task_id="sample_task",
            revision=2,
            summary="Choose strategy",
            options=["A", "B"],
            nonce="once",
            expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        )
    )
    with pytest.raises(RemoteStoreError, match="stale"):
        service.dispatch(
            _request(
                RemoteCommand.APPROVE,
                request_id="bad-revision",
                payload={"decision_id": "DEC-1", "revision": 1, "nonce": "once", "option": "A"},
            )
        )
    result = service.dispatch(
        _request(
            RemoteCommand.APPROVE,
            request_id="valid",
            payload={"decision_id": "DEC-1", "revision": 2, "nonce": "once", "option": "B"},
        )
    )
    assert result["status"] == "approved"
    with pytest.raises(RemoteStoreError, match="already resolved"):
        queue.resolve("DEC-1", actor_id="user-1", revision=2, nonce="once", resolution="B")
