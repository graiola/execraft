from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

from execraft import cli
from execraft.cli_parsers import build_parser
from execraft.runtime.openclaw_service import OpenClawDiagnostic


def _mapping() -> dict:
    return {
        "schema_version": 4,
        "runtimes": {
            "oc": {
                "kind": "openclaw",
                "mode": "external",
                "gateway": "ws://127.0.0.1:18789",
                "auth_ref": "TOKEN",
            }
        },
        "execution_targets": {"local": {"kind": "local"}},
        "model_routes": {
            "model": {"provider": "openai", "model": "gpt", "default_target": "local"}
        },
        "agents": {
            "implementer": {
                "runtime": "oc",
                "model_route": "model",
                "capabilities": ["implement"],
            }
        },
    }


def test_parser_exposes_openclaw_doctor() -> None:
    args = build_parser().parse_args(["openclaw", "doctor", "--runtime", "oc", "--json"])
    assert args.command == "openclaw"
    assert args.action == "doctor"
    assert args.runtime_id == "oc"
    assert args.json is True


def test_openclaw_doctor_reports_configured_runtime(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(cli, "repository_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "resolve_current_project", lambda _root, project_id=None: SimpleNamespace(id="p"))
    monkeypatch.setattr(cli, "_project_config_path", lambda *_args: tmp_path / "agents.yaml")
    monkeypatch.setattr(cli, "_load_yaml_mapping", lambda *_args, **_kwargs: _mapping())

    class FakeService:
        def __init__(
            self, runtime, *, state_root, config_payload=None, credential_env_refs=()
        ):
            self.runtime = runtime
            self.state_root = state_root
            self.config_payload = config_payload
            self.credential_env_refs = tuple(credential_env_refs)

        def probe(self):
            return OpenClawDiagnostic(
                runtime_id=self.runtime.id,
                mode="external",
                status="healthy",
                gateway="ws://127.0.0.1:18789",
                protocol=4,
                version="2026.7.1-2",
                methods=("health",),
            )

        def stop(self):
            pass

    import execraft.runtime.openclaw_service as service_module

    monkeypatch.setattr(service_module, "OpenClawGatewayService", FakeService)
    args = argparse.Namespace(
        action="doctor",
        project_id=None,
        runtime_id="oc",
        state_dir=tmp_path / "state",
        json=True,
    )
    assert cli.cmd_openclaw(args) == 0
    output = capsys.readouterr().out
    assert '"status": "healthy"' in output
    assert '"version": "2026.7.1-2"' in output
