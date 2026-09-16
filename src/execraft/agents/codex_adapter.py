"""Codex CLI adapter for the generic Execraft agent protocol.

The adapter owns Codex-specific command syntax, streaming/event parsing, and
provider transport behavior while exposing the provider-neutral ``AgentAdapter``
contract to orchestration.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable
from execraft.agents.codex_live import CodexAppServerController
from execraft.agents.effort import EffortPolicy
from execraft.agents.heartbeat import AgentHeartbeatEmitter, run_with_heartbeat
from execraft.agents.live_session import (
    LiveSessionUnavailable,
    managed_jsonl_session,
)
from execraft.agents.output_classification import (
    AgentOutputClassifier,
)
from execraft.runtime.contracts import RuntimeCapabilities
from execraft.orchestrate.scheduler import (
    AgentAdapter,
    AgentExecutionError,
    AgentCapability,
    Availability,
    StructuredHandoff,
    build_agent_prompt,
)
from execraft.orchestrate.failure_translation import (
    managed_process_failure,
    provider_failure,
)
from execraft.process import ManagedProcessTerminated, managed_run


CodexRunner = Callable[..., "subprocess.CompletedProcess[str]"]

# Codex exposes reasoning depth as a config override rather than a flag, and its
# ladder stops at ``high``. Both the one-shot and app-server entry points accept
# ``-c key=value``, so one spelling covers every transport.
_SUPPORTED_EFFORT = ("low", "medium", "high")


def _effort_config_args(level: str) -> list[str]:
    """Return the ``-c`` override that sets Codex reasoning effort."""

    return ["-c", f'model_reasoning_effort="{level}"'] if level else []

_DEFAULT_CAPABILITIES = {
    AgentCapability.IMPLEMENT,
    AgentCapability.REVIEW,
    AgentCapability.FIX_REVIEW,
}

# Only failure classifications that mean "this provider itself is
# unusable right now" change availability; a one-off tool/verification
# failure does not — the orchestrator's own per-call failover/retry
# (ProjectOrchestrator._execute_agent) already handles that case.



class CodexAgentAdapter(AgentAdapter):
    """Drives the real `codex exec` CLI as an orchestrator agent.

    Every call to `execute()` invokes the actual CLI (or an injected
    `runner`, for tests) — there is no simulated/dry-run mode here. Tests
    exercise this class exclusively through an injected fake `runner` so
    the automated suite never spends on a real model call; a real
    end-to-end run against the live CLI is a manual/opt-in check, the same
    role the Playwright probe plays for the browser adapter.
    """

    def __init__(
        self,
        *,
        provider_id: str = "codex",
        capabilities: set[AgentCapability] | None = None,
        model: str = "",
        effort: str = "",
        effort_by_capability: dict[str, str] | None = None,
        sandbox: str = "workspace-write",
        workdir: Path | None = None,
        timeout_seconds: int = 600,
        inactivity_timeout_seconds: int = 900,
        output_silence_timeout_seconds: int = 0,
        first_output_timeout_seconds: int = 0,
        max_internal_retry_delay_seconds: int = 120,
        runner: CodexRunner | None = None,
        binary: str | None = None,
        live_sessions: bool = True,
    ):
        self._provider_id = provider_id
        self._capabilities = capabilities or set(_DEFAULT_CAPABILITIES)
        self._model = model
        self._effort = EffortPolicy(
            default=effort,
            by_capability=effort_by_capability,
            supported=_SUPPORTED_EFFORT,
        )
        self._sandbox = sandbox
        self._workdir = workdir or Path.cwd()
        self._timeout_seconds = timeout_seconds
        self._inactivity_timeout_seconds = inactivity_timeout_seconds
        self._output_silence_timeout_seconds = output_silence_timeout_seconds
        self._first_output_timeout_seconds = first_output_timeout_seconds
        self._max_internal_retry_delay_seconds = max_internal_retry_delay_seconds
        self._runner = runner or managed_run
        self._runner_is_injected = runner is not None
        self._binary_explicit = binary is not None
        self._binary = binary or "codex"
        self._live_sessions = bool(live_sessions)
        # Reflects the classification of the most recent failure so a
        # scheduler checking availability *before* the next execute() call
        # can skip a provider that just reported auth/quota/rate-limit
        # trouble, without spending on a probe call to find out.
        self._last_availability = Availability.AVAILABLE
        self._heartbeat = AgentHeartbeatEmitter(
            provider_id=self._provider_id, model=self._model
        )

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def capabilities(self) -> set[AgentCapability]:
        return self._capabilities

    @property
    def execution_capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(
            read_only_enforcement="hard",
            workspace_write=True,
            network_isolation=False,
            command_allowlist=True,
            structured_output=True,
            structured_output_enforcement=(
                "native_schema" if self._use_live_session else "prompt_only"
            ),
            streaming=True,
            raw_output_streaming=True,
            semantic_streaming=self._use_live_session,
            provider_native_steering=self._use_live_session,
            interactive_pty=False,
            session_resume=self._use_live_session,
        )

    @property
    def adapter_name(self) -> str:
        return "codex"

    @property
    def model(self) -> str:
        return self._model

    @property
    def binary(self) -> str:
        return self._binary

    @property
    def interaction_mode(self) -> str:
        return "conversation" if self._use_live_session else "activity"

    @property
    def streaming_interaction(self) -> bool:
        return self._use_live_session

    @property
    def steering_supported(self) -> bool:
        return self._use_live_session

    @property
    def control_mode(self) -> str:
        return "live_steering" if self._use_live_session else "none"

    @property
    def transport(self) -> str:
        return "codex-app-server" if self._use_live_session else "codex-exec-json"

    @property
    def _use_live_session(self) -> bool:
        # Injected runners intentionally preserve the historical one-shot
        # contract used by tests and third-party provider plugins.
        return self._live_sessions and not self._runner_is_injected

    @property
    def availability(self) -> Availability:
        # Injected runners are deterministic test/plugin transports and do not
        # require the provider binary to exist on the host. Real adapters still
        # fail closed when the executable is unavailable.
        if (not self._runner_is_injected or self._binary_explicit) and shutil.which(self._binary) is None:
            return Availability.DISABLED
        return self._last_availability

    def reset_transient_availability(self) -> None:
        if self._last_availability.is_transient:
            self._last_availability = Availability.AVAILABLE

    def configure_heartbeat(self, callback, *, interval_seconds: float = 30.0) -> None:
        """Configure observational subprocess heartbeats for this adapter."""
        self._heartbeat.configure(callback, interval_seconds=interval_seconds)

    def configure_output(self, callback) -> None:
        """Configure a read-only observer for supervised provider output."""
        self._heartbeat.configure_output(callback)

    def configure_operator_controls(self, callback) -> None:
        """Attach the Codex app-server steering queue without creating a PTY."""
        self._heartbeat.configure_operator_controls(callback)

    def configure_interaction(self, callback) -> None:
        """Attach the provider-neutral streamed conversation observer."""
        self._heartbeat.configure_interaction(callback)

    def execute(self, handoff: StructuredHandoff) -> dict[str, Any]:
        if self._use_live_session:
            try:
                return self._execute_live(handoff)
            except LiveSessionUnavailable as exc:
                # Compatibility fallback is permitted only before app-server
                # starts model work.  A mid-turn protocol failure must remain a
                # failure; silently replaying the prompt could duplicate edits.
                callback = self._heartbeat.interaction_callback_for(handoff)
                if callback is not None:
                    callback(
                        {
                            "kind": "status",
                            "text": f"Live Codex app-server unavailable; using one-shot compatibility mode: {exc}",
                            "status": "degraded",
                            "provider": "codex",
                            "interaction_mode": "conversation",
                            "streaming": False,
                            "steering_supported": False,
                            "transport": "codex-exec-json",
                            "control_mode": "none",
                        }
                    )
        return self._execute_legacy(handoff)

    def _execute_live(self, handoff: StructuredHandoff) -> dict[str, Any]:
        prompt = _build_prompt(handoff)
        workdir = (
            Path(handoff.working_directory).resolve()
            if handoff.working_directory
            else self._workdir
        )
        sandbox = "read-only" if handoff.read_only else self._sandbox
        controller = CodexAppServerController(
            handoff=handoff,
            prompt=prompt,
            model=self._model,
            sandbox=sandbox,
            workdir=workdir,
            interaction_callback=self._heartbeat.interaction_callback_for(handoff),
            control_callback=self._heartbeat.control_callback_for(handoff),
        )
        classifier = AgentOutputClassifier(
            adapter=self.adapter_name,
            provider_id=self._provider_id,
            max_internal_retry_delay_seconds=self._max_internal_retry_delay_seconds,
        )

        def classify_stderr(stream: str, text: str):
            # stdout is JSON-RPC and may contain model-authored text.  Scanning
            # it as provider diagnostics can create false auth/model failures.
            return classifier(stream, text) if stream == "stderr" else None

        raw_output_callback = self._heartbeat.output_callback_for(handoff)

        def forward_diagnostics(stream: str, text: str) -> None:
            # The JSON-RPC stdout stream is already translated into compact
            # semantic events. Persisting it again as Activity output doubles
            # I/O and makes large sessions sluggish; stderr remains available
            # for provider/runtime diagnostics.
            if stream == "stderr" and raw_output_callback is not None:
                raw_output_callback(stream, text)

        try:
            completed = managed_jsonl_session(
                [
                    self._binary,
                    "app-server",
                    *_effort_config_args(
                        self._effort.for_handoff(handoff.execution_context)
                    ),
                ],
                cwd=workdir,
                timeout=self._timeout_seconds,
                initial_messages=controller.initial_messages,
                message_callback=controller.handle_message,
                input_callback=controller.poll_controls,
                heartbeat_interval=self._heartbeat.interval_seconds,
                heartbeat_callback=self._heartbeat.callback_for(handoff),
                output_classifier=classify_stderr,
                output_callback=forward_diagnostics,
                inactivity_timeout=self._inactivity_timeout_seconds,
                output_silence_timeout=self._output_silence_timeout_seconds,
                startup_timeout=(
                    self._first_output_timeout_seconds
                    or min(float(self._timeout_seconds), 120.0)
                ),
                refresh_timeout_on_protocol_progress=True,
                close_stdin_on_complete=True,
                terminate_on_complete=True,
            )
        except ManagedProcessTerminated as exc:
            self._raise_managed_failure(exc)
        except subprocess.TimeoutExpired as exc:
            self._last_availability = Availability.BUSY
            raise AgentExecutionError(
                f"codex app-server made no protocol progress for {self._timeout_seconds}s",
                classification="timeout",
            ) from exc
        except (OSError, RuntimeError) as exc:
            if not controller.work_started and _is_live_protocol_unavailable(exc):
                raise LiveSessionUnavailable(str(exc)) from exc
            raise

        if not completed.completed_by_protocol:
            detail = controller.error or completed.stderr.strip() or "Codex app-server exited before turn completion"
            if not controller.work_started:
                raise LiveSessionUnavailable(detail)
            self._raise_provider_failure(detail)
        if controller.error:
            self._raise_provider_failure(controller.error)

        self._last_availability = Availability.AVAILABLE
        return {
            "ok": True,
            "work_package_id": handoff.work_package_id,
            "final_message": controller.final_message,
            "usage": controller.usage,
            "thread_id": controller.thread_id,
            "turn_id": controller.turn_id,
            "interaction_mode": "conversation",
        }

    def _execute_legacy(self, handoff: StructuredHandoff) -> dict[str, Any]:
        prompt = _build_prompt(handoff)
        workdir = Path(handoff.working_directory).resolve() if handoff.working_directory else self._workdir
        sandbox = "read-only" if handoff.read_only else self._sandbox
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "last-message.txt"
            args = [
                self._binary,
                "exec",
                "--json",
                "--skip-git-repo-check",
                "-s",
                sandbox,
                "-C",
                str(workdir),
                "-o",
                str(output_path),
            ]
            for root in handoff.additional_writable_roots:
                args += ["--add-dir", root]
            args += _effort_config_args(
                self._effort.for_handoff(handoff.execution_context)
            )
            if self._model:
                args += ["-m", self._model]
            # Real Codex invocations consume the complete prompt from stdin.
            # Keeping model-authored context out of argv avoids the host ARG_MAX
            # limit and prevents the PTY launcher from duplicating megabytes of
            # text in its own command line. Injected runners retain the legacy
            # positional form so third-party test transports remain compatible.
            stdin_payload: str | None = None
            if self._runner_is_injected:
                args.append(prompt)
            else:
                args.append("-")
                stdin_payload = prompt

            output_classifier = AgentOutputClassifier(
                adapter=self.adapter_name,
                provider_id=self._provider_id,
                max_internal_retry_delay_seconds=self._max_internal_retry_delay_seconds,
            )
            try:
                completed = run_with_heartbeat(
                    self._runner,
                    runner_is_injected=self._runner_is_injected,
                    args=args,
                    cwd=workdir,
                    timeout=self._timeout_seconds,
                    emitter=self._heartbeat,
                    handoff=handoff,
                    output_classifier=output_classifier,
                    inactivity_timeout=self._inactivity_timeout_seconds,
                    output_silence_timeout=self._output_silence_timeout_seconds,
                    first_output_timeout=self._first_output_timeout_seconds,
                    stdin_payload=stdin_payload,
                    interactive_terminal=False,
                )
            except ManagedProcessTerminated as exc:
                self._raise_managed_failure(exc)
            except subprocess.TimeoutExpired as exc:
                self._last_availability = Availability.BUSY
                raise AgentExecutionError(
                    f"codex exec timed out after {self._timeout_seconds}s",
                    classification="timeout",
                ) from exc

            events = _parse_events(completed.stdout)

            if completed.returncode != 0:
                error_message = (
                    _extract_error_message(events)
                    or (completed.stderr or "").strip()
                    or f"codex exec exited {completed.returncode}"
                )
                self._raise_provider_failure(error_message)

            self._last_availability = Availability.AVAILABLE
            final_message = output_path.read_text(encoding="utf-8") if output_path.is_file() else ""
            return {
                "ok": True,
                "work_package_id": handoff.work_package_id,
                "final_message": final_message,
                "usage": _extract_usage(events),
            }

    def _raise_managed_failure(self, exc: ManagedProcessTerminated) -> None:
        self._last_availability, error = managed_process_failure(exc)
        raise error from exc

    def _raise_provider_failure(self, message: str) -> None:
        self._last_availability, error = provider_failure(self.adapter_name, message)
        raise error


def _build_prompt(handoff: StructuredHandoff) -> str:
    return build_agent_prompt(handoff)


def _parse_events(stdout: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            events.append(json.loads(stripped))
        except json.JSONDecodeError:
            continue
    return events


def _unwrap_error(message: str) -> str:
    try:
        nested = json.loads(message)
    except (json.JSONDecodeError, TypeError):
        return message
    if isinstance(nested, dict):
        inner = nested.get("error")
        if isinstance(inner, dict) and inner.get("message"):
            return str(inner["message"])
    return message


def _extract_error_message(events: list[dict[str, Any]]) -> str:
    for event in reversed(events):
        if event.get("type") == "turn.failed":
            return _unwrap_error(event.get("error", {}).get("message", ""))
        if event.get("type") == "error":
            return _unwrap_error(event.get("message", ""))
    for event in events:
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "error":
            return _unwrap_error(item.get("message", ""))
    return ""


def _extract_usage(events: list[dict[str, Any]]) -> dict[str, Any]:
    for event in events:
        if event.get("type") == "turn.completed":
            usage = event.get("usage")
            if isinstance(usage, dict):
                return usage
    return {}



def _is_live_protocol_unavailable(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "unknown command",
            "unrecognized subcommand",
            "invalid jsonl",
            "did not return a thread id",
            "exited before turn completion",
            "no such file or directory",
        )
    )
