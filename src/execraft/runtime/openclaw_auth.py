"""OpenClaw Gateway device authentication and credential references.

Private device keys and Gateway-issued device tokens are stored under Execraft's
host-local state hierarchy, never in product repositories or portable project
configuration.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class OpenClawAuthDependencyError(RuntimeError):
    pass


class OpenClawCredentialError(RuntimeError):
    pass


@dataclass(frozen=True)
class GatewayCredential:
    kind: str
    value: str


CredentialResolver = Callable[[str, str], GatewayCredential | None]


def resolve_environment_credential(auth_ref: str, auth_kind: str) -> GatewayCredential | None:
    """Resolve an explicit environment credential reference.

    ``env:NAME`` and ``NAME`` are accepted for compatibility. Empty refs or
    ``auth_kind=none`` intentionally resolve to no credential.
    """

    if auth_kind == "none":
        return None
    name = auth_ref[4:] if auth_ref.startswith("env:") else auth_ref
    name = name.strip()
    if not name:
        raise OpenClawCredentialError("OpenClaw auth_ref is empty")
    value = os.environ.get(name, "")
    if not value:
        raise OpenClawCredentialError(
            f"OpenClaw credential environment variable {name!r} is not set"
        )
    return GatewayCredential(kind=auth_kind, value=value)


@dataclass(frozen=True)
class DeviceIdentity:
    device_id: str
    public_key: str
    private_key_pem: bytes


@dataclass(frozen=True)
class StoredDeviceToken:
    token: str
    scopes: tuple[str, ...]


class DeviceIdentityStore:
    """Persist one OpenClaw device identity and token per configured runtime."""

    def __init__(self, state_dir: Path) -> None:
        self._dir = Path(state_dir).expanduser().resolve()
        self._identity_path = self._dir / "device-identity.pem"
        self._token_path = self._dir / "device-token.json"

    @property
    def directory(self) -> Path:
        return self._dir

    def load_or_create_identity(self) -> DeviceIdentity:
        serialization, ed25519 = _crypto()
        self._dir.mkdir(parents=True, exist_ok=True)
        if self._identity_path.is_file():
            private_key = serialization.load_pem_private_key(
                self._identity_path.read_bytes(), password=None
            )
            if not isinstance(private_key, ed25519.Ed25519PrivateKey):
                raise OpenClawCredentialError(
                    f"OpenClaw device key is not Ed25519: {self._identity_path}"
                )
        else:
            private_key = ed25519.Ed25519PrivateKey.generate()
            pem = private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
            _atomic_private_write(self._identity_path, pem)
        private_bytes = self._identity_path.read_bytes()
        public_raw = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return DeviceIdentity(
            device_id=hashlib.sha256(public_raw).hexdigest(),
            public_key=_base64url(public_raw),
            private_key_pem=private_bytes,
        )

    def load_token(self) -> StoredDeviceToken | None:
        if not self._token_path.is_file():
            return None
        try:
            raw = json.loads(self._token_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OpenClawCredentialError(
                f"invalid OpenClaw device token store: {self._token_path}"
            ) from exc
        token = str(raw.get("token", "")).strip() if isinstance(raw, dict) else ""
        if not token:
            return None
        scopes = raw.get("scopes", []) if isinstance(raw, dict) else []
        return StoredDeviceToken(
            token=token,
            scopes=tuple(str(item) for item in scopes if str(item)),
        )

    def save_token(self, token: str, scopes: tuple[str, ...] | frozenset[str]) -> None:
        value = token.strip()
        if not value:
            return
        payload = json.dumps(
            {"token": value, "scopes": sorted(set(scopes))},
            indent=2,
            sort_keys=True,
        ).encode("utf-8") + b"\n"
        self._dir.mkdir(parents=True, exist_ok=True)
        _atomic_private_write(self._token_path, payload)


def build_device_auth_payload_v3(
    *,
    identity: DeviceIdentity,
    client_id: str,
    client_mode: str,
    role: str,
    scopes: tuple[str, ...],
    signed_at_ms: int,
    token: str,
    nonce: str,
    platform: str,
    device_family: str,
) -> str:
    return "|".join(
        (
            "v3",
            identity.device_id,
            client_id,
            client_mode,
            role,
            ",".join(scopes),
            str(signed_at_ms),
            token,
            nonce,
            platform.strip(),
            device_family.strip(),
        )
    )


def sign_device_payload(identity: DeviceIdentity, payload: str) -> str:
    serialization, ed25519 = _crypto()
    private_key = serialization.load_pem_private_key(identity.private_key_pem, password=None)
    if not isinstance(private_key, ed25519.Ed25519PrivateKey):
        raise OpenClawCredentialError("OpenClaw device key is not Ed25519")
    return _base64url(private_key.sign(payload.encode("utf-8")))


def _crypto():
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except ImportError as exc:  # pragma: no cover - depends on optional extra.
        raise OpenClawAuthDependencyError(
            "OpenClaw runtime requires the 'openclaw' optional dependency set "
            "(cryptography and websockets)"
        ) from exc
    return serialization, ed25519


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _atomic_private_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()

# ``operator.admin`` is not optional for a Gateway Execraft drives. OpenClaw authorizes
# config mutation behind it, and two controls on the normal execution path are
# config mutations: binding a managed agent workspace to the exact task root
# (``agents.update``, the workspace-binding control) and continuation
# compaction (``sessions.compact``). Compaction runs for external placements
# too, so requesting admin only for managed Gateways would connect happily and
# then fail at the first compaction instead of at the handshake.
_REQUIRED_RUNTIME_SCOPES = frozenset(
    {"operator.read", "operator.write", "operator.approvals", "operator.admin"}
)


def required_runtime_scopes(existing: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Return least runtime scopes including approval and compaction authority."""

    return tuple(sorted(_REQUIRED_RUNTIME_SCOPES.union(existing)))
