import json

from execraft.model_registry import ModelEndpoint
from execraft.targets.config import ExecutionTargetKind
from execraft.targets.health import extract_model_ids, ollama_runtime_url, probe_model_endpoint


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit=None):
        return json.dumps(self.payload).encode("utf-8")


def _endpoint(provider_family="ollama"):
    return ModelEndpoint(
        endpoint_id="gpu",
        provider_family=provider_family,
        provider_alias="runtime-alias",
        name="GPU",
        base_url="http://10.0.0.2:11434/v1",
        base_url_source="test",
        models={"qwen": {}},
        target_id="gpu",
        target_kind=ExecutionTargetKind.INFERENCE_ENDPOINT,
    )


def test_model_id_parser_accepts_openai_and_ollama_payloads():
    assert extract_model_ids({"data": [{"id": "a"}, {"id": "b"}]}) == {"a", "b"}
    assert extract_model_ids({"models": [{"name": "a"}, {"model": "b"}]}) == {"a", "b"}


def test_ollama_runtime_probe_uses_provider_family_not_alias_prefix():
    assert ollama_runtime_url(_endpoint()) == "http://10.0.0.2:11434/api/ps"
    assert ollama_runtime_url(_endpoint("custom")) == ""


def test_generic_probe_reports_discovered_and_loaded_models():
    calls = []

    def opener(request, timeout):
        calls.append((request.full_url, timeout))
        if request.full_url.endswith("/v1/models"):
            return _Response({"data": [{"id": "qwen"}, {"id": "other"}]})
        return _Response({"models": [{"name": "qwen"}]})

    result = probe_model_endpoint(
        _endpoint(),
        opener=opener,
        probe_loaded_models=True,
        max_response_bytes=100_000,
    )
    assert result.reachable is True
    assert result.models == frozenset({"qwen", "other"})
    assert result.loaded_models == frozenset({"qwen"})
    assert result.loaded_models_supported is True
    assert [item[0] for item in calls] == [
        "http://10.0.0.2:11434/v1/models",
        "http://10.0.0.2:11434/api/ps",
    ]


def test_loaded_model_probe_failure_is_diagnostic_only():
    calls = 0

    def opener(request, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _Response({"data": [{"id": "qwen"}]})
        raise OSError("runtime state unavailable")

    result = probe_model_endpoint(_endpoint(), opener=opener, probe_loaded_models=True)
    assert result.reachable is True
    assert result.models == frozenset({"qwen"})
    assert result.loaded_models_error == "runtime state unavailable"
