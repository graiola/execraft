"""Deterministic in-memory OpenClaw Gateway protocol fake for WP5 tests."""

from __future__ import annotations

import json
import queue
from dataclasses import dataclass
from typing import Any, Mapping

from execraft.runtime.openclaw_auth import DeviceIdentity, StoredDeviceToken


@dataclass
class FakeDeviceStore:
    stored_token: StoredDeviceToken | None = None
    saved_token: str = ""
    saved_scopes: tuple[str, ...] = ()

    def load_or_create_identity(self) -> DeviceIdentity:
        return DeviceIdentity(
            device_id="a" * 64,
            public_key="ZmFrZS1wdWJsaWMta2V5",
            private_key_pem=b"fake-private-key",
        )

    def load_token(self) -> StoredDeviceToken | None:
        return self.stored_token

    def save_token(self, token: str, scopes) -> None:
        self.saved_token = token
        self.saved_scopes = tuple(sorted(scopes))


class ScriptedGatewayConnection:
    def __init__(
        self,
        *,
        version: str = "2026.7.1-2",
        protocol: int = 4,
        connect_error: Mapping[str, Any] | None = None,
        device_token: str = "device-token-1",
        health_payload: Mapping[str, Any] | None = None,
        emit_event_before_health: bool = False,
        chat_abort_supported: bool = True,
        session_keys: set[str] | None = None,
        agent_workspaces: Mapping[str, str] | None = None,
        granted_scopes: tuple[str, ...] = (
            # A real Gateway grants admin to an owner-authenticated operator;
            # Execraft needs it in managed mode to bind agent workspaces.
            "operator.read", "operator.write", "operator.approvals", "operator.admin"
        ),
    ) -> None:
        self.version = version
        self.protocol = protocol
        self.connect_error = dict(connect_error or {}) or None
        self.device_token = device_token
        self.health_payload = dict(health_payload or {"status": "ok"})
        self.emit_event_before_health = emit_event_before_health
        self.chat_abort_supported = chat_abort_supported
        self.session_keys = set(session_keys or set())
        self.agent_workspaces = dict(agent_workspaces or {})
        self.approval_resolutions: list[dict[str, Any]] = []
        self.granted_scopes = tuple(granted_scopes)
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self._queue: queue.Queue[str | BaseException] = queue.Queue()
        self._queue.put(
            json.dumps(
                {
                    "type": "event",
                    "event": "connect.challenge",
                    "payload": {"nonce": "nonce-123", "ts": 1_800_000_000_000},
                }
            )
        )

    def send(self, data: str) -> None:
        frame = json.loads(data)
        self.sent.append(frame)
        if frame.get("method") == "connect":
            if self.connect_error is not None:
                self._queue.put(
                    json.dumps(
                        {
                            "type": "res",
                            "id": frame["id"],
                            "ok": False,
                            "error": self.connect_error,
                        }
                    )
                )
                return
            self._queue.put(
                json.dumps(
                    {
                        "type": "res",
                        "id": frame["id"],
                        "ok": True,
                        "payload": {
                            "type": "hello-ok",
                            "protocol": self.protocol,
                            "server": {"version": self.version, "connId": "conn-1"},
                            "features": {
                                "methods": [
                                    "health", "chat.abort", "sessions.abort",
                                    "sessions.describe", "agent.wait", "agents.list",
                                    "agents.update", "approval.resolve"
                                ],
                                "events": [
                                    "shutdown", "agent", "exec.approval.requested",
                                    "plugin.approval.requested"
                                ],
                            },
                            "snapshot": {},
                            "auth": {
                                "role": "operator",
                                "scopes": list(self.granted_scopes),
                                "deviceToken": self.device_token,
                            },
                            "policy": {
                                "maxPayload": 1024,
                                "maxBufferedBytes": 4096,
                                "tickIntervalMs": 15000,
                            },
                        },
                    }
                )
            )
            return
        if frame.get("method") == "health":
            if self.emit_event_before_health:
                self._queue.put(
                    json.dumps(
                        {
                            "type": "event",
                            "event": "agent",
                            "payload": {"runId": "run-1", "stream": "assistant"},
                            "seq": 7,
                        }
                    )
                )
            self._queue.put(
                json.dumps(
                    {
                        "type": "res",
                        "id": frame["id"],
                        "ok": True,
                        "payload": self.health_payload,
                    }
                )
            )
            return
        if frame.get("method") == "agents.update":
            params = frame.get("params") or {}
            agent_id = str(params.get("agentId", ""))
            workspace = str(params.get("workspace", ""))
            if agent_id and workspace:
                self.agent_workspaces[agent_id] = workspace
            self._queue.put(
                json.dumps(
                    {
                        "type": "res", "id": frame["id"], "ok": True,
                        "payload": {"agentId": agent_id, "workspace": workspace},
                    }
                )
            )
            return
        if frame.get("method") == "agents.list":
            agents = [
                {"id": agent_id, "workspace": workspace}
                for agent_id, workspace in sorted(self.agent_workspaces.items())
            ]
            self._queue.put(
                json.dumps(
                    {
                        "type": "res", "id": frame["id"], "ok": True,
                        "payload": {"agents": agents},
                    }
                )
            )
            return
        if frame.get("method") == "approval.resolve":
            params = dict(frame.get("params") or {})
            self.approval_resolutions.append(params)
            self._queue.put(
                json.dumps(
                    {
                        "type": "res", "id": frame["id"], "ok": True,
                        "payload": {"resolved": True, **params},
                    }
                )
            )
            return
        if frame.get("method") == "sessions.describe":
            key = str((frame.get("params") or {}).get("key", ""))
            self._queue.put(
                json.dumps(
                    {
                        "type": "res",
                        "id": frame["id"],
                        "ok": True,
                        "payload": {"session": {"key": key}} if key in self.session_keys else {},
                    }
                )
            )
            return
        if frame.get("method") == "chat.abort" and not self.chat_abort_supported:
            self._queue.put(
                json.dumps(
                    {
                        "type": "res",
                        "id": frame["id"],
                        "ok": False,
                        "error": {"code": "NOT_FOUND", "message": "unknown method"},
                    }
                )
            )
            return
        if frame.get("method") in {"sessions.abort", "chat.abort"}:
            self._queue.put(
                json.dumps(
                    {
                        "type": "res",
                        "id": frame["id"],
                        "ok": True,
                        "payload": {"aborted": True},
                    }
                )
            )
            return
        self._queue.put(
            json.dumps(
                {
                    "type": "res",
                    "id": frame["id"],
                    "ok": False,
                    "error": {"code": "NOT_FOUND", "message": "unknown method"},
                }
            )
        )

    def recv(self, timeout: float | None = None) -> str:
        try:
            item = self._queue.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError from exc
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._queue.put(OSError("closed"))

    def emit(self, name: str, payload: Mapping[str, Any]) -> None:
        self._queue.put(json.dumps({"type": "event", "event": name, "payload": payload}))


class AgentRunGatewayConnection(ScriptedGatewayConnection):
    """Scripted Gateway that implements the two-stage agent contract."""

    def __init__(
        self,
        *,
        final_status: str = "ok",
        hold_wait_until_abort: bool = False,
        agent_workspaces: Mapping[str, str] | None = None,
        granted_scopes: tuple[str, ...] = (
            # A real Gateway grants admin to an owner-authenticated operator;
            # Execraft needs it in managed mode to bind agent workspaces.
            "operator.read", "operator.write", "operator.approvals", "operator.admin"
        ),
    ) -> None:
        super().__init__(agent_workspaces=agent_workspaces)
        self.final_status = final_status
        self.hold_wait_until_abort = hold_wait_until_abort
        self._pending_wait_frame: dict[str, Any] | None = None
        self.run_id = "run-wp7-1"
        self.session_key = "agent:impl:execraft-cold"
        self.aborted = False

    def send(self, data: str) -> None:
        frame = json.loads(data)
        if frame.get("method") == "agent":
            self.sent.append(frame)
            self._queue.put(
                json.dumps(
                    {
                        "type": "res",
                        "id": frame["id"],
                        "ok": True,
                        "payload": {
                            "status": "accepted",
                            "runId": self.run_id,
                            "sessionKey": self.session_key,
                        },
                    }
                )
            )
            self._queue.put(
                json.dumps(
                    {
                        "type": "event",
                        "event": "agent",
                        "payload": {
                            "runId": self.run_id,
                            "seq": 1,
                            "stream": "assistant",
                            "data": {"text": "working"},
                        },
                        "seq": 1,
                    }
                )
            )
            self._queue.put(
                json.dumps(
                    {
                        "type": "res",
                        "id": frame["id"],
                        "ok": True,
                        "payload": {
                            "runId": self.run_id,
                            "status": self.final_status,
                            "result": {
                                "payloads": [{"text": "completed by OpenClaw"}],
                                "meta": {
                                    "usage": {"input": 120, "output": 30, "total": 150},
                                    "provider": "ollama",
                                    "model": "qwen3-coder",
                                },
                            },
                        },
                    }
                )
            )
            return
        if frame.get("method") == "agent.wait":
            self.sent.append(frame)
            if self.hold_wait_until_abort:
                self._pending_wait_frame = frame
                return
            self._queue_wait_response(frame, self.final_status)
            return
        if frame.get("method") in {"sessions.abort", "chat.abort"}:
            self.aborted = True
            super().send(data)
            if self._pending_wait_frame is not None:
                wait_frame = self._pending_wait_frame
                self._pending_wait_frame = None
                self._queue_wait_response(wait_frame, "cancelled")
            return
        super().send(data)

    def _queue_wait_response(self, frame: Mapping[str, Any], status: str) -> None:
        self._queue.put(
            json.dumps(
                {
                    "type": "res",
                    "id": frame["id"],
                    "ok": True,
                    "payload": {"runId": self.run_id, "status": status},
                }
            )
        )
