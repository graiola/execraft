"""Non-destructive diagnostics for configured agent instances."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from execraft.agents.antigravity_cli_adapter import AntigravityCliAgentAdapter
from execraft.agents.claude_code_adapter import ClaudeCodeAgentAdapter
from execraft.agents.config import AgentProviderConfig
from execraft.agents.opencode_adapter import OpenCodeAgentAdapter
from execraft.agents.opencode_registry import (
    OpenCodeEndpoint,
    OpenCodeProviderRegistry,
)
from execraft.orchestrate.provider_health import ProviderHealthStore
from execraft.orchestrate.scheduler import StructuredHandoff
from execraft.targets.health import TargetProbeResult, extract_model_ids, probe_model_endpoint
from execraft.process import managed_run

DiagnosticRunner = Callable[..., "subprocess.CompletedProcess[str]"]
EndpointProbe = Callable[[Any], "EndpointProbeResult"]


def _default_runner(
    args: list[str], *, cwd: Path, timeout: int
) -> "subprocess.CompletedProcess[str]":
    return managed_run(args, cwd=str(cwd), timeout=timeout)


EndpointProbeResult = TargetProbeResult


@dataclass
class AgentDiagnostic:
    name: str
    adapter: str
    provider_id: str
    binary: str
    model: str
    binary_available: bool
    model_available: bool | None = None
    smoke_test: str = "not_requested"
    authentication_status: str = "not_checked"
    details: list[str] = field(default_factory=list)
    health_status: str = "available"
    health_reason: str = ""
    unavailable_until: str = ""
    endpoint_id: str = ""
    endpoint_url: str = ""
    endpoint_url_source: str = ""
    endpoint_reachable: bool | None = None
    target_id: str = ""
    target_kind: str = ""
    provider_family: str = ""
    provider_alias: str = ""
    api_family: str = ""
    target_concurrency_group: str = ""

    @property
    def healthy(self) -> bool:
        if self.health_status != "available":
            return False
        if not self.binary_available:
            return False
        if self.authentication_status == "unauthenticated":
            return False
        if self.endpoint_reachable is False:
            return False
        if self.model_available is False:
            return False
        return self.smoke_test not in {"failed", "invalid_output"}

    def as_mapping(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "adapter": self.adapter,
            "provider_id": self.provider_id,
            "binary": self.binary,
            "model": self.model,
            "binary_available": self.binary_available,
            "model_available": self.model_available,
            "smoke_test": self.smoke_test,
            "authentication_status": self.authentication_status,
            "healthy": self.healthy,
            "details": list(self.details),
            "health_status": self.health_status,
            "health_reason": self.health_reason,
            "unavailable_until": self.unavailable_until,
            "endpoint_id": self.endpoint_id,
            "endpoint_url": self.endpoint_url,
            "endpoint_url_source": self.endpoint_url_source,
            "endpoint_reachable": self.endpoint_reachable,
            "target_id": self.target_id,
            "target_kind": self.target_kind,
            "provider_family": self.provider_family,
            "provider_alias": self.provider_alias,
            "api_family": self.api_family,
            "target_concurrency_group": self.target_concurrency_group,
        }


def diagnose_agents(
    configs: list[AgentProviderConfig],
    *,
    refresh_models: bool = False,
    smoke_test: bool = False,
    runner: DiagnosticRunner | None = None,
    timeout_seconds: int = 120,
    health_store: ProviderHealthStore | None = None,
    opencode_registry: OpenCodeProviderRegistry | None = None,
    endpoint_probe: EndpointProbe | None = None,
    workdir: Path | None = None,
) -> list[AgentDiagnostic]:
    """Diagnose binaries, models, remote endpoints, and optional live calls.

    OpenCode providers declared in ``providers.yaml`` are probed directly via
    their OpenAI-compatible ``/models`` endpoint.  Other OpenCode providers keep
    using ``opencode models`` for backwards compatibility.
    """

    runner = runner or _default_runner
    endpoint_probe = endpoint_probe or probe_openai_compatible_endpoint
    registry = opencode_registry or OpenCodeProviderRegistry()
    model_registry = registry.model_registry
    diagnostic_workdir = (workdir or Path.cwd()).resolve()
    results: list[AgentDiagnostic] = []
    model_cache: dict[tuple[str, str, bool], tuple[set[str], str]] = {}
    endpoint_cache: dict[str, EndpointProbeResult] = {}

    for config in configs:
        binary_available = shutil.which(config.binary) is not None
        health = health_store.get(config.provider_id) if health_store else None
        result = AgentDiagnostic(
            name=config.name,
            adapter=config.adapter,
            provider_id=config.provider_id,
            binary=config.binary,
            model=config.model,
            binary_available=binary_available,
            health_status=health.status if health else "available",
            health_reason=health.reason if health else "",
            unavailable_until=health.unavailable_until if health else "",
        )
        _append_health_detail(result, health)
        if not binary_available:
            result.details.append(f"binary not found on PATH: {config.binary}")
            results.append(result)
            continue

        if config.adapter == "opencode":
            endpoint = model_registry.endpoint_for_model(config.model)
            if endpoint is not None:
                _diagnose_registry_model(
                    config,
                    result,
                    endpoint,
                    endpoint_probe,
                    endpoint_cache,
                )
            else:
                _diagnose_opencode_cli_model(
                    config,
                    result,
                    runner,
                    diagnostic_workdir,
                    timeout_seconds,
                    refresh_models,
                    model_cache,
                )

            if smoke_test:
                if result.model_available is False or result.endpoint_reachable is False:
                    result.smoke_test = "skipped_model_missing"
                else:
                    _run_opencode_smoke_test(
                        config,
                        result,
                        runner,
                        timeout_seconds,
                        registry=registry,
                    )
        elif config.adapter in {"claude", "claude-code"}:
            _diagnose_claude_authentication(
                config,
                result,
                runner,
                diagnostic_workdir,
                timeout_seconds,
            )
            if smoke_test:
                if result.authentication_status == "unauthenticated":
                    result.smoke_test = "skipped_authentication_required"
                else:
                    _run_claude_smoke_test(
                        config,
                        result,
                        runner,
                        timeout_seconds,
                        workdir=diagnostic_workdir,
                    )
        elif config.adapter in {"antigravity", "antigravity-cli"} and smoke_test:
            _run_antigravity_smoke_test(config, result, runner, timeout_seconds)

        if health_store is not None and result.smoke_test == "passed":
            # A requested live smoke test is the controlled recovery probe for
            # an expired cooldown. Its successful provider round-trip is
            # stronger evidence than the stale failure record, so return the
            # provider to rotation and make this diagnostic internally
            # consistent.
            health_store.mark_available(config.provider_id)
            result.health_status = "available"
            result.health_reason = ""
            result.unavailable_until = ""

        results.append(result)
    return results



def _diagnose_claude_authentication(
    config: AgentProviderConfig,
    result: AgentDiagnostic,
    runner: DiagnosticRunner,
    workdir: Path,
    timeout_seconds: int,
) -> None:
    """Check Claude Code login state without issuing a model request.

    Newer Claude Code releases expose ``claude auth status`` as a cheap,
    machine-readable preflight. Older releases are left usable: an
    unrecognised subcommand is reported as ``unsupported`` rather than as an
    authentication failure. Raw command output is deliberately not retained
    because it can contain account-identifying metadata.
    """

    auth_timeout = max(1, min(timeout_seconds, 15))
    args = [config.binary, "auth", "status"]
    try:
        completed = runner(args, cwd=workdir, timeout=auth_timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        result.authentication_status = "unknown"
        result.details.append(f"Claude authentication preflight failed: {exc}")
        return

    if completed.returncode == 0:
        result.authentication_status = "authenticated"
        return

    output = f"{completed.stdout or ''}\n{completed.stderr or ''}".lower()
    unsupported_markers = (
        "unknown command",
        "unrecognized command",
        "unrecognised command",
        "unexpected argument",
        "invalid command",
    )
    if any(marker in output for marker in unsupported_markers):
        result.authentication_status = "unsupported"
        result.details.append(
            "Claude CLI does not support 'auth status'; update Claude Code "
            "to enable non-interactive authentication diagnostics"
        )
        return

    result.authentication_status = "unauthenticated"
    result.details.append(
        "Claude CLI reports no active login; run 'claude auth login' in the "
        "same user environment used by Execraft"
    )

def _append_health_detail(result: AgentDiagnostic, health: Any) -> None:
    if health is None or health.is_available:
        return
    detail = f"provider health: {health.status} ({health.reason})"
    if health.unavailable_until:
        detail += f" until {health.unavailable_until}"
    result.details.append(detail)


def _diagnose_registry_model(
    config: AgentProviderConfig,
    result: AgentDiagnostic,
    endpoint: Any,
    endpoint_probe: EndpointProbe,
    endpoint_cache: dict[str, EndpointProbeResult],
) -> None:
    result.endpoint_id = endpoint.endpoint_id
    result.endpoint_url = endpoint.base_url
    result.endpoint_url_source = endpoint.base_url_source
    result.target_id = endpoint.target_id
    result.target_kind = getattr(endpoint.target_kind, "value", str(endpoint.target_kind))
    result.provider_family = endpoint.provider_family
    result.provider_alias = endpoint.provider_alias
    result.api_family = endpoint.api_family
    result.target_concurrency_group = endpoint.concurrency_group
    probe = endpoint_cache.get(endpoint.models_url)
    if probe is None:
        probe = endpoint_probe(endpoint)
        endpoint_cache[endpoint.models_url] = probe
    result.endpoint_reachable = probe.reachable
    if not probe.reachable:
        result.model_available = False
        result.details.append(
            f"endpoint probe failed for {endpoint.endpoint_id}: {probe.error}"
        )
        return

    model_id = config.model.split("/", 1)[1]
    declared = model_id in endpoint.models
    reported = model_id in probe.models
    result.model_available = declared and reported
    if not declared:
        result.details.append(
            f"model {model_id!r} is not declared for endpoint {endpoint.endpoint_id!r}"
        )
    elif not reported:
        result.details.append(
            f"model {model_id!r} was not reported by {endpoint.models_url}"
        )


def _diagnose_opencode_cli_model(
    config: AgentProviderConfig,
    result: AgentDiagnostic,
    runner: DiagnosticRunner,
    workdir: Path,
    timeout_seconds: int,
    refresh_models: bool,
    model_cache: dict[tuple[str, str, bool], tuple[set[str], str]],
) -> None:
    provider = config.model_provider
    cache_key = (config.binary, provider, refresh_models)
    if cache_key not in model_cache:
        args = [config.binary, "models", provider]
        if refresh_models:
            args.append("--refresh")
        completed = runner(args, cwd=workdir, timeout=timeout_seconds)
        models = _parse_model_list(completed.stdout)
        error = ""
        if completed.returncode != 0:
            error = (
                (completed.stderr or "").strip()
                or f"model discovery exited {completed.returncode}"
            )
        model_cache[cache_key] = (models, error)
    models, discovery_error = model_cache[cache_key]
    if discovery_error:
        result.model_available = False
        result.details.append(f"model discovery failed: {discovery_error}")
        return
    result.model_available = config.model in models
    if not result.model_available:
        result.details.append(
            f"configured model not reported by 'opencode models {provider}'"
        )


def probe_openai_compatible_endpoint(endpoint: Any) -> EndpointProbeResult:
    """Compatibility wrapper over runtime-neutral target probing."""

    generic = endpoint.as_model_endpoint() if hasattr(endpoint, "as_model_endpoint") else endpoint
    return probe_model_endpoint(generic)



def _extract_endpoint_models(payload: Any) -> set[str] | None:
    """Compatibility wrapper for historical diagnostics tests/extensions."""

    return extract_model_ids(payload)


_ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _parse_model_list(stdout: str) -> set[str]:
    models: set[str] = set()
    for line in _ANSI_ESCAPE.sub("", stdout).splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        for token in stripped.replace("*", " ").split():
            candidate = token.strip("` ,")
            if "/" in candidate and not candidate.startswith(("http://", "https://")):
                provider, _, model = candidate.partition("/")
                if provider and model:
                    models.add(candidate)
                    break
    return models


def _run_opencode_smoke_test(
    config: AgentProviderConfig,
    result: AgentDiagnostic,
    runner: DiagnosticRunner,
    timeout_seconds: int,
    *,
    registry: OpenCodeProviderRegistry,
) -> None:
    with tempfile.TemporaryDirectory(prefix="execraft-opencode-doctor-") as directory:
        workdir = Path(directory)
        endpoint = registry.endpoint_for_model(config.model)
        if endpoint is not None:
            opencode_config = registry.minimal_opencode_config(endpoint.provider_id)
            (workdir / "opencode.json").write_text(
                json.dumps(opencode_config, indent=2) + "\n",
                encoding="utf-8",
            )
        adapter = OpenCodeAgentAdapter(
            provider_id=config.provider_id,
            capabilities=set(config.capabilities),
            model=config.model,
            auto_approve=False,
            workdir=workdir,
            timeout_seconds=min(config.timeout_seconds, timeout_seconds),
            first_output_timeout_seconds=min(
                config.first_output_timeout_seconds or timeout_seconds,
                timeout_seconds,
            ),
            runner=runner,
            binary=config.binary,
            agent_by_capability=dict(config.agent_by_capability),
            format_repair_agent=config.format_repair_agent,
        )
        handoff = StructuredHandoff(
            work_package_id="agent-doctor",
            stage="probe",
            summary=(
                "Return exactly one JSON object with ok=true, status='ready', "
                "and summary='agent ready'. Do not use tools or modify files."
            ),
            expected_output_schema={
                "type": "object",
                "required": ["ok", "status", "summary"],
                "properties": {
                    "ok": {"const": True},
                    "status": {"const": "ready"},
                    "summary": {"type": "string"},
                },
            },
        )
        try:
            payload = adapter.execute(handoff)
        except Exception as exc:
            result.smoke_test = "failed"
            result.details.append(f"smoke test failed: {exc}")
            return
        message = str(payload.get("final_message", "")).strip()
        if not _contains_ready_object(message):
            result.smoke_test = "invalid_output"
            result.details.append("smoke test returned no valid ready JSON object")
            return
        result.smoke_test = "passed"


def _run_claude_smoke_test(
    config: AgentProviderConfig,
    result: AgentDiagnostic,
    runner: DiagnosticRunner,
    timeout_seconds: int,
    *,
    workdir: Path,
) -> None:
    """Run the live probe requested by ``agents doctor --smoke-test``.

    Claude diagnostics previously reported ``not_requested`` even when the
    operator explicitly opted into a live call. Use read-only plan permissions
    and a no-tools prompt so the probe cannot modify the repository.
    """

    def adapter_runner(args, *, cwd, timeout, **_kwargs):
        return runner(args, cwd=Path(cwd), timeout=timeout)

    adapter = ClaudeCodeAgentAdapter(
        provider_id=config.provider_id,
        capabilities=set(config.capabilities),
        model=config.model,
        permission_mode="plan",
        workdir=workdir,
        timeout_seconds=min(config.timeout_seconds, timeout_seconds),
        inactivity_timeout_seconds=min(
            config.inactivity_timeout_seconds, timeout_seconds
        ),
        first_output_timeout_seconds=min(
            config.first_output_timeout_seconds or timeout_seconds,
            timeout_seconds,
        ),
        max_internal_retry_delay_seconds=config.max_internal_retry_delay_seconds,
        runner=adapter_runner,
        binary=config.binary,
    )
    handoff = StructuredHandoff(
        work_package_id="agent-doctor",
        stage="probe",
        summary=(
            "Return exactly one JSON object with ok=true, status='ready', "
            "and summary='agent ready'. Do not use tools or modify files."
        ),
        expected_output_schema={
            "type": "object",
            "required": ["ok", "status", "summary"],
            "properties": {
                "ok": {"const": True},
                "status": {"const": "ready"},
                "summary": {"type": "string"},
            },
        },
    )
    try:
        payload = adapter.execute(handoff)
    except Exception as exc:
        result.smoke_test = "failed"
        result.details.append(f"smoke test failed: {exc}")
        return
    if not _contains_ready_object(str(payload.get("final_message", "")).strip()):
        result.smoke_test = "invalid_output"
        result.details.append("smoke test returned no valid ready JSON object")
        return
    result.smoke_test = "passed"


def _run_antigravity_smoke_test(
    config: AgentProviderConfig,
    result: AgentDiagnostic,
    runner: DiagnosticRunner,
    timeout_seconds: int,
) -> None:
    with tempfile.TemporaryDirectory(prefix="execraft-antigravity-doctor-") as directory:
        adapter = AntigravityCliAgentAdapter(
            provider_id=config.provider_id,
            capabilities=set(config.capabilities),
            model=config.model,
            dangerously_skip_permissions=False,
            sandbox_enabled=False,
            policy_paths=config.policy_paths,
            workdir=Path(directory),
            timeout_seconds=min(config.timeout_seconds, timeout_seconds),
            first_output_timeout_seconds=min(
                config.first_output_timeout_seconds or timeout_seconds,
                timeout_seconds,
            ),
            runner=runner,
            binary=config.binary,
        )
        handoff = StructuredHandoff(
            work_package_id="agent-doctor",
            stage="probe",
            summary=(
                "Return exactly one JSON object with ok=true, status='ready', "
                "and summary='agent ready'. Do not use tools or modify files."
            ),
            expected_output_schema={
                "type": "object",
                "required": ["ok", "status", "summary"],
                "properties": {
                    "ok": {"const": True},
                    "status": {"const": "ready"},
                    "summary": {"type": "string"},
                },
            },
        )
        try:
            payload = adapter.execute(handoff)
        except Exception as exc:
            result.smoke_test = "failed"
            result.details.append(f"smoke test failed: {exc}")
            return
        if not _contains_ready_object(str(payload.get("final_message", "")).strip()):
            result.smoke_test = "invalid_output"
            result.details.append("smoke test returned no valid ready JSON object")
            return
        result.smoke_test = "passed"


def _contains_ready_object(text: str) -> bool:
    decoder = json.JSONDecoder()
    candidates = [text]
    if "```" in text:
        candidates.extend(part.strip() for part in text.split("```") if part.strip())
    for candidate in candidates:
        for offset, character in enumerate(candidate):
            if character != "{":
                continue
            try:
                value, _ = decoder.raw_decode(candidate[offset:])
            except json.JSONDecodeError:
                continue
            if (
                isinstance(value, dict)
                and value.get("ok") is True
                and value.get("status") == "ready"
            ):
                return True
    return False
