"""Generic model-endpoint probing used by diagnostics and the dashboard."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen

from execraft.model_registry import ModelEndpoint


@dataclass(frozen=True)
class TargetProbeResult:
    """Health/model-discovery result for one inference target endpoint."""

    models: frozenset[str] = frozenset()
    loaded_models: frozenset[str] = frozenset()
    loaded_models_supported: bool = False
    loaded_models_error: str = ""
    error: str = ""
    latency_ms: int = 0

    @property
    def reachable(self) -> bool:
        return not self.error


def probe_model_endpoint(
    endpoint: ModelEndpoint,
    *,
    user_agent: str = "execraft-target-probe/1",
    max_response_bytes: int | None = None,
    timeout_seconds: float | None = None,
    probe_loaded_models: bool = False,
    opener: Callable[..., Any] = urlopen,
) -> TargetProbeResult:
    """Probe model discovery, optionally adding provider-native runtime state.

    OpenAI-compatible model discovery is authoritative for reachability. The
    Ollama-native ``/api/ps`` request is diagnostic only and cannot turn a
    reachable target into an unavailable one.
    """

    started = time.monotonic()
    timeout = timeout_seconds or endpoint.connect_timeout_seconds
    try:
        payload = _read_json(
            endpoint.models_url,
            timeout=timeout,
            user_agent=user_agent,
            max_bytes=max_response_bytes,
            opener=opener,
        )
    except HTTPError as exc:
        return _failed(started, f"HTTP {exc.code}: {exc.reason}")
    except URLError as exc:
        return _failed(started, str(exc.reason))
    except TimeoutError:
        return _failed(started, "connection timed out")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _failed(started, f"invalid JSON response: {exc}")
    except OSError as exc:
        return _failed(started, str(exc))

    models = extract_model_ids(payload)
    if models is None:
        return _failed(started, "response does not contain a supported model list")

    loaded_models: frozenset[str] = frozenset()
    loaded_error = ""
    runtime_url = ollama_runtime_url(endpoint) if probe_loaded_models else ""
    if runtime_url:
        try:
            runtime_payload = _read_json(
                runtime_url,
                timeout=timeout,
                user_agent=user_agent,
                max_bytes=max_response_bytes,
                opener=opener,
            )
            loaded_models = frozenset(extract_model_ids(runtime_payload) or set())
        except (HTTPError, URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError, OSError) as exc:
            loaded_error = str(getattr(exc, "reason", exc))

    return TargetProbeResult(
        models=frozenset(models),
        loaded_models=loaded_models,
        loaded_models_supported=bool(runtime_url),
        loaded_models_error=loaded_error,
        latency_ms=round((time.monotonic() - started) * 1000),
    )


def extract_model_ids(payload: Any) -> set[str] | None:
    """Accept OpenAI ``data[].id`` and Ollama ``models[]`` model lists."""
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if isinstance(data, list):
        return {
            str(item["id"])
            for item in data
            if isinstance(item, dict) and item.get("id")
        }
    models = payload.get("models")
    if isinstance(models, list):
        result: set[str] = set()
        for item in models:
            if isinstance(item, dict):
                value = item.get("id") or item.get("name") or item.get("model")
            else:
                value = item
            if value:
                result.add(str(value))
        return result
    return None


def ollama_runtime_url(endpoint: ModelEndpoint) -> str:
    """Return Ollama's native running-model endpoint from provider semantics."""
    family = str(getattr(endpoint, "provider_family", "")).lower()
    if not family:
        provider_id = str(getattr(endpoint, "provider_id", "")).lower()
        family = "ollama" if provider_id == "ollama" or provider_id.startswith("ollama-") else ""
    if family != "ollama":
        return ""
    parsed = urlparse(endpoint.base_url)
    if not parsed.scheme or not parsed.netloc:
        return ""
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    return urlunparse(parsed._replace(path=f"{path}/api/ps", params="", query="", fragment=""))


def _read_json(
    url: str, *, timeout: float, user_agent: str, max_bytes: int | None, opener: Callable[..., Any]
) -> Any:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": user_agent})
    with opener(request, timeout=timeout) as response:
        data = response.read(max_bytes) if max_bytes is not None else response.read()
    return json.loads(data.decode("utf-8"))


def _failed(started: float, error: str) -> TargetProbeResult:
    return TargetProbeResult(
        error=error,
        latency_ms=round((time.monotonic() - started) * 1000),
    )
