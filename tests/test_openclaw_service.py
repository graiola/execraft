from __future__ import annotations

from pathlib import Path

from execraft.runtime.openclaw_auth import GatewayCredential
from execraft.runtime.openclaw_service import OpenClawGatewayService
from execraft.runtime_config import (
    OpenClawMode,
    OpenClawRuntimeOptions,
    RuntimeConfig,
    RuntimeKind,
)
from tests.fakes.openclaw_gateway import FakeDeviceStore, ScriptedGatewayConnection


def _credential(_ref: str, _kind: str):
    return GatewayCredential(kind="token", value="shared-token")


def _runtime(options: OpenClawRuntimeOptions) -> RuntimeConfig:
    return RuntimeConfig(id="oc", kind=RuntimeKind.OPENCLAW, openclaw=options)


def _service(tmp_path: Path, connection: ScriptedGatewayConnection, options=None):
    options = options or OpenClawRuntimeOptions(
        mode=OpenClawMode.EXTERNAL,
        auth_ref="TOKEN",
    )
    return OpenClawGatewayService(
        _runtime(options),
        state_root=tmp_path,
        credential_resolver=_credential,
        connection_factory=lambda _url, _timeout: connection,
        device_store=FakeDeviceStore(),
        payload_signer=lambda _identity, _payload: "signature",
    )


def test_external_service_probe_reports_healthy_version_and_methods(tmp_path: Path) -> None:
    service = _service(tmp_path, ScriptedGatewayConnection())
    try:
        result = service.probe()
        assert result.status == "healthy"
        assert result.protocol == 4
        assert result.version == "2026.7.1-2"
        assert "health" in result.methods
    finally:
        service.stop()


def test_external_service_probe_reports_pairing_required(tmp_path: Path) -> None:
    service = _service(
        tmp_path,
        ScriptedGatewayConnection(
            connect_error={
                "code": "PAIRING_REQUIRED",
                "message": "approval needed",
                "details": {"requestId": "pair-7"},
            }
        ),
    )
    result = service.probe()
    assert result.status == "pairing_required"
    assert "pair-7" in result.detail
    service.stop()


def test_external_service_probe_reports_incompatible_version(tmp_path: Path) -> None:
    service = _service(tmp_path, ScriptedGatewayConnection(version="2026.7.2"))
    result = service.probe()
    assert result.status == "incompatible"
    service.stop()
