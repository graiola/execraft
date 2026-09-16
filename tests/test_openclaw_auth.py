from __future__ import annotations

import stat
from pathlib import Path

import pytest

from execraft.runtime.openclaw_auth import (
    DeviceIdentityStore,
    build_device_auth_payload_v3,
    required_runtime_scopes,
    sign_device_payload,
)


def test_real_device_identity_is_stable_private_and_signs_v3(tmp_path: Path) -> None:
    pytest.importorskip("cryptography")
    store = DeviceIdentityStore(tmp_path / "device")
    first = store.load_or_create_identity()
    second = store.load_or_create_identity()
    assert first.device_id == second.device_id
    assert len(first.device_id) == 64
    assert first.public_key
    assert stat.S_IMODE((store.directory / "device-identity.pem").stat().st_mode) == 0o600

    payload = build_device_auth_payload_v3(
        identity=first,
        client_id="cli",
        client_mode="cli",
        role="operator",
        scopes=("operator.read", "operator.write"),
        signed_at_ms=1_800_000_000_000,
        token="shared-token",
        nonce="nonce-123",
        platform="linux",
        device_family="desktop",
    )
    assert payload.startswith("v3|")
    assert "|nonce-123|linux|desktop" in payload
    assert sign_device_payload(first, payload)

    store.save_token("device-token", ("operator.read",))
    assert stat.S_IMODE((store.directory / "device-token.json").stat().st_mode) == 0o600
    assert store.load_token().token == "device-token"


def test_admin_scope_is_required_for_every_gateway_Execraft_drives() -> None:
    """Two controls on the normal execution path are OpenClaw config mutations.

    Binding a managed agent workspace goes through ``agents.update``, and session
    compaction goes through ``sessions.compact``; OpenClaw gates
    both behind ``operator.admin``. Compaction runs for external placements
    too, so requesting admin only for managed Gateways would connect and then
    fail at the first compaction rather than at the handshake.
    """

    scopes = required_runtime_scopes()

    assert "operator.admin" in scopes
    # Approval-resolution authority remains available after binding.
    assert "operator.approvals" in scopes
    assert {"operator.read", "operator.write"}.issubset(scopes)


def test_stored_scopes_are_preserved_when_reusing_a_device_token() -> None:
    scopes = required_runtime_scopes(("operator.pairing",))

    assert "operator.pairing" in scopes
    assert "operator.admin" in scopes
