from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from execraft.runtime.openclaw_discovery import discover_projected_models
from execraft.runtime.openclaw_projection import OpenClawConfigProjection


@dataclass(frozen=True)
class _Features:
    methods: tuple[str, ...]


@dataclass(frozen=True)
class _Hello:
    features: _Features


class _Client:
    def __init__(self, *, methods=("models.list",), payload=None) -> None:
        self.hello = _Hello(_Features(tuple(methods)))
        self.payload = payload if payload is not None else {"models": []}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def connect(self):
        return self.hello

    def request(self, method, params=None, **_kwargs):
        self.calls.append((method, dict(params or {})))
        return self.payload


def _projection(*models: str) -> OpenClawConfigProjection:
    return OpenClawConfigProjection(
        runtime_id="openclaw",
        config={},
        providers=(),
        models=tuple(sorted(models)),
        agents=(),
        credential_refs=(),
    )


def test_discovery_matches_projected_provider_catalog() -> None:
    client = _Client(
        payload={
            "models": [
                {
                    "provider": "ollama-node",
                    "id": "qwen3-coder:30b",
                    "available": True,
                }
            ]
        }
    )
    result = discover_projected_models(
        client, _projection("ollama-node/qwen3-coder:30b"), refresh=True
    )
    assert result.supported is True
    assert result.matches_projection is True
    assert result.discovered == ("ollama-node/qwen3-coder:30b",)
    assert result.unavailable == ()
    assert result.missing == ()
    assert client.calls == [("models.list", {"view": "provider-config", "refresh": True})]


def test_discovery_keeps_missing_and_unavailable_advisory() -> None:
    client = _Client(
        payload={
            "models": [
                {"provider": "custom", "id": "coder", "available": False},
            ]
        }
    )
    result = discover_projected_models(
        client,
        _projection("custom/coder", "openai/gpt-5.6"),
    )
    assert result.supported is True
    assert result.matches_projection is False
    assert result.unavailable == ("custom/coder",)
    assert result.missing == ("openai/gpt-5.6",)
    assert "advisory" in result.warning
    assert client.calls == [("models.list", {"view": "provider-config"})]


def test_discovery_fails_open_when_gateway_does_not_advertise_models_list() -> None:
    client = _Client(methods=("health",))
    result = discover_projected_models(client, _projection("openai/gpt-5.6"))
    assert result.supported is False
    assert result.matches_projection is False
    assert result.missing == ()
    assert "does not advertise" in result.warning
    assert client.calls == []
