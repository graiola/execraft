import json
import subprocess
from pathlib import Path

from execraft.agents.config import parse_agent_configs
from execraft.agents.diagnostics import (
    _contains_ready_object,
    _parse_model_list,
    diagnose_agents,
)


class Runner:
    def __init__(self, *, fail_zen=False):
        self.calls = []
        self.fail_zen = fail_zen

    def __call__(self, args, *, cwd, timeout):
        self.calls.append(list(args))
        if args[1] == "models":
            provider = args[2]
            models = {
                "opencode": "opencode/deepseek-v4-flash-free\n",
                "opencode-go": "opencode-go/deepseek-v4-flash\n",
            }
            return subprocess.CompletedProcess(args, 0, models[provider], "")
        model = args[args.index("--model") + 1]
        if self.fail_zen and model.startswith("opencode/"):
            return subprocess.CompletedProcess(
                args,
                1,
                '{"type":"error","error":{"data":{"message":"rate limit exceeded"}}}\n',
                "",
            )
        stdout = (
            '{"type":"text","part":{"text":"{\\"ok\\":true,'
            '\\"status\\":\\"ready\\",\\"summary\\":\\"agent ready\\"}"}}\n'
            '{"type":"step_finish","part":{"tokens":{},"cost":0}}\n'
        )
        return subprocess.CompletedProcess(args, 0, stdout, "")


def _configs():
    return parse_agent_configs(
        {
            "schema_version": 2,
            "providers": {
                "zen": {
                    "adapter": "opencode",
                    "enabled": True,
                    "provider_id": "opencode-zen-free",
                    "binary": "opencode-test",
                    "model": "opencode/deepseek-v4-flash-free",
                    "capabilities": ["review"],
                },
                "go": {
                    "adapter": "opencode",
                    "enabled": True,
                    "provider_id": "opencode-go",
                    "binary": "opencode-test",
                    "model": "opencode-go/deepseek-v4-flash",
                    "capabilities": ["implement", "review", "fix_review"],
                },
            },
        }
    )


def test_doctor_discovers_each_provider_model_independently(monkeypatch):
    monkeypatch.setattr("execraft.agents.diagnostics.shutil.which", lambda _: "/bin/fake")
    runner = Runner()

    results = diagnose_agents(_configs(), refresh_models=True, runner=runner)

    assert all(item.healthy for item in results)
    assert all(item.model_available is True for item in results)
    assert [call[:3] for call in runner.calls] == [
        ["opencode-test", "models", "opencode"],
        ["opencode-test", "models", "opencode-go"],
    ]
    assert all("--refresh" in call for call in runner.calls)


def test_live_smoke_results_are_independent(monkeypatch):
    monkeypatch.setattr("execraft.agents.diagnostics.shutil.which", lambda _: "/bin/fake")
    results = diagnose_agents(_configs(), smoke_test=True, runner=Runner(fail_zen=True))

    zen, go = results
    assert zen.smoke_test == "failed"
    assert not zen.healthy
    assert go.smoke_test == "passed"
    assert go.healthy


def test_successful_live_smoke_clears_stale_provider_health(monkeypatch, tmp_path):
    from execraft.orchestrate.provider_health import ProviderHealthStore

    monkeypatch.setattr("execraft.agents.diagnostics.shutil.which", lambda _: "/bin/fake")
    store = ProviderHealthStore(tmp_path / "provider-health.json")
    store.mark_failure("opencode-go", reason="quota_exhausted", detail="old quota")

    result = diagnose_agents(
        [_configs()[1]],
        smoke_test=True,
        runner=Runner(),
        health_store=store,
    )[0]

    assert result.smoke_test == "passed"
    assert result.health_status == "available"
    assert result.health_reason == ""
    assert result.healthy is True
    assert store.get("opencode-go").is_available is True


def test_ready_object_parser_accepts_bounded_markdown_wrapper():
    assert _contains_ready_object(
        'Result:\n```json\n{"ok": true, "status": "ready", "summary": "ok"}\n```'
    )


def test_model_parser_ignores_ansi_and_accepts_provider_model_tokens():
    assert _parse_model_list("\x1b[32mopencode/deepseek-v4-flash-free\x1b[0m\n") == {
        "opencode/deepseek-v4-flash-free"
    }


def test_model_discovery_failure_is_unhealthy(monkeypatch):
    monkeypatch.setattr("execraft.agents.diagnostics.shutil.which", lambda _: "/bin/fake")

    def failed(args, *, cwd, timeout):
        return subprocess.CompletedProcess(args, 1, "", "not authenticated")

    results = diagnose_agents(_configs(), runner=failed)
    assert all(item.model_available is False for item in results)
    assert all(not item.healthy for item in results)


def test_antigravity_smoke_test_uses_headless_text(monkeypatch):
    monkeypatch.setattr("execraft.agents.diagnostics.shutil.which", lambda _: "/bin/fake")
    configs = parse_agent_configs(
        {
            "schema_version": 2,
            "providers": {
                "antigravity": {
                    "adapter": "antigravity-cli",
                    "enabled": True,
                    "provider_id": "antigravity",
                    "binary": "agy-test",
                    "capabilities": ["implement", "review", "fix_review"],
                }
            },
        }
    )
    calls = []

    def runner(args, *, cwd, timeout):
        calls.append(list(args))
        stdout = json.dumps(
            {"ok": True, "status": "ready", "summary": "agent ready"}
        )
        return subprocess.CompletedProcess(args, 0, stdout, "")

    result = diagnose_agents(configs, smoke_test=True, runner=runner)[0]

    assert result.smoke_test == "passed"
    assert result.healthy is True
    assert calls[0][0] == "agy-test"
    assert calls[0][1] == "-p"
    assert "-o" not in calls[0]
    assert "--dangerously-skip-permissions" not in calls[0]


def test_remote_registry_probe_bypasses_global_opencode_model_discovery(
    tmp_path, monkeypatch
):
    from execraft.agents.diagnostics import EndpointProbeResult
    from execraft.agents.opencode_registry import load_opencode_provider_registry

    registry_file = tmp_path / "providers.yaml"
    registry_file.write_text(
        """schema_version: 1
endpoints:
  gpu-laptop:
    provider_id: ollama-gpu-a
    base_url: http://10.42.0.107:11434/v1
    models:
      qwen3.5:9b:
        name: Qwen 3.5 9B
""",
        encoding="utf-8",
    )
    registry = load_opencode_provider_registry(registry_file)
    configs = parse_agent_configs(
        {
            "schema_version": 2,
            "providers": {
                "local-reviewer": {
                    "adapter": "opencode",
                    "enabled": True,
                    "provider_id": "opencode-ollama-gpu-a",
                    "binary": "opencode-test",
                    "model": "ollama-gpu-a/qwen3.5:9b",
                    "capabilities": ["review"],
                }
            },
        }
    )
    monkeypatch.setattr("execraft.agents.diagnostics.shutil.which", lambda _: "/bin/fake")

    calls = []

    def runner(args, *, cwd, timeout):
        calls.append(list(args))
        raise AssertionError("opencode model discovery should not run for registry endpoints")

    result = diagnose_agents(
        configs,
        runner=runner,
        opencode_registry=registry,
        endpoint_probe=lambda endpoint: EndpointProbeResult(
            models=frozenset({"qwen3.5:9b"})
        ),
    )[0]

    assert result.healthy is True
    assert result.model_available is True
    assert result.endpoint_reachable is True
    assert result.endpoint_id == "gpu-laptop"
    assert result.endpoint_url == "http://10.42.0.107:11434/v1"
    assert result.target_id == "gpu-laptop"
    assert result.target_kind == "inference_endpoint"
    assert result.provider_family == "ollama"
    assert result.provider_alias == "ollama-gpu-a"
    assert result.api_family == "openai-compatible"
    assert result.target_concurrency_group == "gpu-laptop"
    assert calls == []


def test_remote_registry_smoke_test_writes_minimal_opencode_config(
    tmp_path, monkeypatch
):
    from execraft.agents.diagnostics import EndpointProbeResult
    from execraft.agents.opencode_registry import load_opencode_provider_registry

    registry_file = tmp_path / "providers.yaml"
    registry_file.write_text(
        """schema_version: 1
endpoints:
  gpu-laptop:
    provider_id: ollama-gpu-a
    base_url: http://10.42.0.107:11434/v1
    models:
      qwen3.5:9b:
        name: Qwen 3.5 9B
""",
        encoding="utf-8",
    )
    registry = load_opencode_provider_registry(registry_file)
    configs = parse_agent_configs(
        {
            "schema_version": 2,
            "providers": {
                "local-reviewer": {
                    "adapter": "opencode",
                    "enabled": True,
                    "provider_id": "opencode-ollama-gpu-a",
                    "binary": "opencode-test",
                    "model": "ollama-gpu-a/qwen3.5:9b",
                    "capabilities": ["review"],
                }
            },
        }
    )
    monkeypatch.setattr("execraft.agents.diagnostics.shutil.which", lambda _: "/bin/fake")

    def runner(args, *, cwd, timeout):
        config = json.loads((Path(cwd) / "opencode.json").read_text())
        assert config["provider"]["ollama-gpu-a"]["options"]["baseURL"] == (
            "http://10.42.0.107:11434/v1"
        )
        assert "--auto" not in args
        stdout = (
            '{"type":"text","part":{"text":"{\\"ok\\":true,'
            '\\"status\\":\\"ready\\",\\"summary\\":\\"agent ready\\"}"}}\n'
            '{"type":"step_finish","part":{"tokens":{},"cost":0}}\n'
        )
        return subprocess.CompletedProcess(args, 0, stdout, "")

    result = diagnose_agents(
        configs,
        runner=runner,
        smoke_test=True,
        opencode_registry=registry,
        endpoint_probe=lambda endpoint: EndpointProbeResult(
            models=frozenset({"qwen3.5:9b"})
        ),
    )[0]

    assert result.smoke_test == "passed"
    assert result.healthy is True


def test_remote_registry_unreachable_endpoint_is_unhealthy(tmp_path, monkeypatch):
    from execraft.agents.diagnostics import EndpointProbeResult
    from execraft.agents.opencode_registry import load_opencode_provider_registry

    registry_file = tmp_path / "providers.yaml"
    registry_file.write_text(
        """schema_version: 1
endpoints:
  gpu-laptop:
    provider_id: ollama-gpu-a
    base_url: http://10.42.0.107:11434/v1
    models:
      qwen3.5:9b: {}
""",
        encoding="utf-8",
    )
    registry = load_opencode_provider_registry(registry_file)
    configs = parse_agent_configs(
        {
            "schema_version": 2,
            "providers": {
                "local-reviewer": {
                    "adapter": "opencode",
                    "enabled": True,
                    "binary": "opencode-test",
                    "model": "ollama-gpu-a/qwen3.5:9b",
                    "capabilities": ["review"],
                }
            },
        }
    )
    monkeypatch.setattr("execraft.agents.diagnostics.shutil.which", lambda _: "/bin/fake")

    result = diagnose_agents(
        configs,
        opencode_registry=registry,
        endpoint_probe=lambda endpoint: EndpointProbeResult(error="connection refused"),
    )[0]

    assert result.endpoint_reachable is False
    assert result.model_available is False
    assert result.healthy is False
    assert "connection refused" in result.details[0]


def test_endpoint_model_parser_accepts_openai_and_ollama_shapes():
    from execraft.agents.diagnostics import _extract_endpoint_models

    assert _extract_endpoint_models({"data": [{"id": "qwen3.5:9b"}]}) == {
        "qwen3.5:9b"
    }
    assert _extract_endpoint_models(
        {"models": [{"name": "devstral:latest"}, {"model": "qwen:7b"}]}
    ) == {"devstral:latest", "qwen:7b"}


def test_claude_smoke_test_runs_when_explicitly_requested(monkeypatch, tmp_path):
    monkeypatch.setattr("execraft.agents.diagnostics.shutil.which", lambda _: "/bin/fake")
    configs = parse_agent_configs(
        {
            "schema_version": 2,
            "providers": {
                "claude": {
                    "adapter": "claude-code",
                    "enabled": True,
                    "provider_id": "claude-code",
                    "binary": "claude-test",
                    "capabilities": ["implement", "review", "fix_review"],
                }
            },
        }
    )
    calls = []

    def runner(args, *, cwd, timeout):
        calls.append(list(args))
        if args[1:3] == ["auth", "status"]:
            return subprocess.CompletedProcess(
                args, 0, '{"loggedIn":true,"authMethod":"claude.ai"}', ""
            )
        stdout = json.dumps(
            {
                "is_error": False,
                "result": '{"ok":true,"status":"ready","summary":"agent ready"}',
                "terminal_reason": "completed",
                "usage": {},
            }
        )
        return subprocess.CompletedProcess(args, 0, stdout, "")

    result = diagnose_agents(
        configs,
        smoke_test=True,
        runner=runner,
        workdir=tmp_path,
    )[0]

    assert result.authentication_status == "authenticated"
    assert result.smoke_test == "passed"
    assert result.healthy is True
    assert calls[0] == ["claude-test", "auth", "status"]
    assert calls[1][0] == "claude-test"
    assert "--permission-mode" in calls[1]
    assert calls[1][calls[1].index("--permission-mode") + 1] == "plan"


def test_claude_authentication_failure_is_reported_without_live_smoke(
    monkeypatch, tmp_path
):
    monkeypatch.setattr("execraft.agents.diagnostics.shutil.which", lambda _: "/bin/fake")
    configs = parse_agent_configs(
        {
            "schema_version": 2,
            "providers": {
                "claude": {
                    "adapter": "claude-code",
                    "enabled": True,
                    "provider_id": "claude-code",
                    "binary": "claude-test",
                    "capabilities": ["implement", "review"],
                }
            },
        }
    )
    calls = []

    def runner(args, *, cwd, timeout):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 1, "", "Not logged in")

    result = diagnose_agents(
        configs, smoke_test=True, runner=runner, workdir=tmp_path
    )[0]

    assert result.authentication_status == "unauthenticated"
    assert result.smoke_test == "skipped_authentication_required"
    assert result.healthy is False
    assert calls == [["claude-test", "auth", "status"]]
    assert "run 'claude auth login'" in result.details[-1]


def test_legacy_claude_cli_without_auth_status_remains_usable(monkeypatch, tmp_path):
    monkeypatch.setattr("execraft.agents.diagnostics.shutil.which", lambda _: "/bin/fake")
    configs = parse_agent_configs(
        {
            "schema_version": 2,
            "providers": {
                "claude": {
                    "adapter": "claude-code",
                    "enabled": True,
                    "provider_id": "claude-code",
                    "binary": "claude-test",
                    "capabilities": ["review"],
                }
            },
        }
    )

    def runner(args, *, cwd, timeout):
        return subprocess.CompletedProcess(args, 2, "", "unknown command: auth")

    result = diagnose_agents(configs, runner=runner, workdir=tmp_path)[0]

    assert result.authentication_status == "unsupported"
    assert result.healthy is True
    assert "does not support 'auth status'" in result.details[-1]
