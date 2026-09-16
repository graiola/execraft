"""Advisory OpenClaw model-catalog checks for an Execraft projection.

OpenClaw's public ``models.list`` RPC exposes source-authored provider inventory.
That surface is diagnostics only: current Gateway catalogs can lag or
misclassify otherwise runnable routes, so catalog absence never overrides the
canonical Execraft route/target or becomes a scheduling health check.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .openclaw_projection import OpenClawConfigProjection


class _GatewayModelClient(Protocol):
    @property
    def hello(self) -> Any: ...

    def connect(self) -> Any: ...

    def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
        idempotency_key: str = "",
    ) -> Any: ...


@dataclass(frozen=True)
class OpenClawModelDiscovery:
    """Comparison of projected refs with OpenClaw's public provider catalog."""

    supported: bool
    projected: tuple[str, ...]
    discovered: tuple[str, ...] = ()
    unavailable: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    warning: str = ""

    @property
    def matches_projection(self) -> bool:
        return self.supported and not self.unavailable and not self.missing

    def as_mapping(self) -> dict[str, Any]:
        return {
            "supported": self.supported,
            "matches_projection": self.matches_projection,
            "projected": list(self.projected),
            "discovered": list(self.discovered),
            "unavailable": list(self.unavailable),
            "missing": list(self.missing),
            "warning": self.warning,
        }


def discover_projected_models(
    client: _GatewayModelClient,
    projection: OpenClawConfigProjection,
    *,
    refresh: bool = False,
) -> OpenClawModelDiscovery:
    """Compare a projection with ``models.list(view=provider-config)``.

    The check is intentionally advisory. ``refresh=True`` explicitly asks the
    Gateway to rebuild provider discovery; the default uses normal cached/current
    behavior and therefore avoids forcing a provider refresh on every doctor run.
    """

    hello = client.hello or client.connect()
    methods = _feature_methods(hello)
    projected = tuple(sorted(projection.models))
    if "models.list" not in methods:
        return OpenClawModelDiscovery(
            supported=False,
            projected=projected,
            warning="Gateway does not advertise the public models.list RPC",
        )

    params: dict[str, Any] = {"view": "provider-config"}
    if refresh:
        params["refresh"] = True
    payload = client.request("models.list", params)
    rows = payload.get("models", ()) if isinstance(payload, Mapping) else ()
    status: dict[str, bool | None] = {}
    if isinstance(rows, (list, tuple)):
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            ref = _model_ref(row)
            if not ref:
                continue
            available = row.get("available")
            status[ref] = available if isinstance(available, bool) else None

    discovered = tuple(sorted(ref for ref in projected if ref in status))
    unavailable = tuple(sorted(ref for ref in projected if status.get(ref) is False))
    missing = tuple(sorted(ref for ref in projected if ref not in status))
    warning = ""
    if unavailable or missing:
        warning = (
            "OpenClaw model catalog is advisory: one or more projected models were "
            "reported unavailable or absent; verify runtime execution before treating "
            "catalog state as authoritative"
        )
    return OpenClawModelDiscovery(
        supported=True,
        projected=projected,
        discovered=discovered,
        unavailable=unavailable,
        missing=missing,
        warning=warning,
    )


def _feature_methods(hello: Any) -> frozenset[str]:
    features = getattr(hello, "features", None)
    methods = getattr(features, "methods", ())
    return frozenset(str(method) for method in methods)


def _model_ref(row: Mapping[str, Any]) -> str:
    key = row.get("key")
    if isinstance(key, str) and "/" in key:
        return key.strip()
    provider = str(row.get("provider", "")).strip()
    model = row.get("id", row.get("model", ""))
    model_id = str(model).strip()
    if not provider or not model_id:
        return ""
    prefix = f"{provider}/"
    return model_id if model_id.startswith(prefix) else f"{provider}/{model_id}"
