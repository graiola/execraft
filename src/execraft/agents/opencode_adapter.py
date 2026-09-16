"""OpenCode CLI adapter for the generic Execraft agent protocol.

OpenCode emits a JSONL event stream. This module owns the provider-specific
command and event protocol while orchestration consumes only normalized agent
results, health, and capability state.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from execraft.agents.effort import EffortPolicy
from execraft.agents.heartbeat import AgentHeartbeatEmitter, run_with_heartbeat
from execraft.agents.output_classification import (
    AgentOutputClassifier,
)
from execraft.agents.opencode_events import OpenCodeEventBridge
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

OpenCodeRunner = Callable[..., "subprocess.CompletedProcess[str]"]

# OpenCode variants are provider-specific. ``minimal`` has no counterpart in the
# orchestrator ladder, so the mapped set stops at the three levels that do.
_SUPPORTED_EFFORT = ("low", "medium", "high")

_DEFAULT_CAPABILITIES = {
    AgentCapability.IMPLEMENT,
    AgentCapability.REVIEW,
    AgentCapability.FIX_REVIEW,
}



def _default_runner(
    args: list[str],
    *,
    cwd: Path,
    timeout: int,
    heartbeat_interval: float = 0.0,
    heartbeat_callback=None,
    output_classifier=None,
    output_callback=None,
    terminal_callback=None,
    inactivity_timeout: float | None = None,
    output_silence_timeout: float | None = None,
    first_output_timeout: float | None = None,
    max_output_bytes: int | None = None,
    input_data: str | bytes | None = None,
    environment: Mapping[str, str] | None = None,
) -> "subprocess.CompletedProcess[str]":
    return managed_run(
        args,
        cwd=str(cwd),
        timeout=timeout,
        heartbeat_interval=heartbeat_interval,
        heartbeat_callback=heartbeat_callback,
        output_classifier=output_classifier,
        output_callback=output_callback,
        terminal_callback=terminal_callback,
        inactivity_timeout=inactivity_timeout,
        output_silence_timeout=output_silence_timeout,
        first_output_timeout=first_output_timeout,
        max_output_bytes=max_output_bytes,
        input_data=input_data,
        environment=environment,
    )


class OpenCodeAgentAdapter(AgentAdapter):
    """Drives the real `opencode run` CLI as an orchestrator agent.

    Every call to `execute()` invokes the actual CLI (or an injected
    `runner`, for tests) — no simulated mode. Tests exercise this class
    exclusively through an injected fake runner using output captured from
    two real, user-approved live calls, so the automated suite never
    spends on a real model call itself.
    """

    def __init__(
        self,
        *,
        provider_id: str = "opencode",
        capabilities: set[AgentCapability] | None = None,
        model: str = "",
        auto_approve: bool = True,
        workdir: Path | None = None,
        timeout_seconds: int = 600,
        inactivity_timeout_seconds: int = 900,
        output_silence_timeout_seconds: int = 0,
        first_output_timeout_seconds: int = 0,
        max_output_bytes: int = 64 * 1024 * 1024,
        max_internal_retry_delay_seconds: int = 120,
        agent_by_capability: Mapping[AgentCapability, str] | None = None,
        effort: str = "",
        effort_by_capability: Mapping[str, str] | None = None,
        format_repair_agent: str = "",
        runner: OpenCodeRunner | None = None,
        binary: str | None = None,
        config_path: Path | None = None,
    ):
        self._provider_id = provider_id
        self._capabilities = capabilities or set(_DEFAULT_CAPABILITIES)
        self._model = model
        self._auto_approve = auto_approve
        self._workdir = workdir or Path.cwd()
        self._timeout_seconds = timeout_seconds
        self._inactivity_timeout_seconds = inactivity_timeout_seconds
        self._output_silence_timeout_seconds = output_silence_timeout_seconds
        self._first_output_timeout_seconds = first_output_timeout_seconds
        self._max_output_bytes = max(1024, int(max_output_bytes))
        self._max_internal_retry_delay_seconds = max_internal_retry_delay_seconds
        self._agent_by_capability = dict(agent_by_capability or {})
        self._effort = EffortPolicy(
            default=effort,
            by_capability=effort_by_capability,
            supported=_SUPPORTED_EFFORT,
        )
        self._format_repair_agent = format_repair_agent.strip()
        self._runner = runner or _default_runner
        self._runner_is_injected = runner is not None
        self._binary_explicit = binary is not None
        self._binary = binary or "opencode"
        self._config_path = config_path
        self._last_availability = Availability.AVAILABLE
        self._endpoint_probe: Callable[[], Any] | None = None
        self._endpoint_model_id = ""
        self._endpoint_probe_ttl_seconds = 30.0
        self._endpoint_probe_expires_at = 0.0
        self._endpoint_probe_result: Any = None
        self._endpoint_probe_lock = threading.Lock()
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
            read_only_enforcement="provider_policy",
            workspace_write=True,
            network_isolation=False,
            command_allowlist=False,
            structured_output=True,
            structured_output_enforcement="prompt_only",
            streaming=True,
            raw_output_streaming=True,
            semantic_streaming=True,
            provider_native_steering=False,
            interactive_pty=False,
            session_resume=False,
        )

    @property
    def interaction_mode(self) -> str:
        return "conversation"

    @property
    def streaming_interaction(self) -> bool:
        return True

    @property
    def steering_supported(self) -> bool:
        return False

    @property
    def control_mode(self) -> str:
        return "observation"

    @property
    def transport(self) -> str:
        return "opencode-run-jsonl"

    @property
    def adapter_name(self) -> str:
        return "opencode"

    @property
    def model(self) -> str:
        return self._model

    @property
    def binary(self) -> str:
        return self._binary

    @property
    def availability(self) -> Availability:
        # Injected runners are deterministic test/plugin transports and do not
        # require the provider binary to exist on the host. Real adapters still
        # fail closed when the executable is unavailable.
        if (
            not self._runner_is_injected or self._binary_explicit
        ) and shutil.which(self._binary) is None:
            return Availability.DISABLED
        if self._last_availability != Availability.AVAILABLE:
            return self._last_availability
        endpoint_availability = self._endpoint_availability()
        if endpoint_availability is not None:
            return endpoint_availability
        return self._last_availability

    def declares_configured_model(self) -> bool:
        """Return whether the config handed to the CLI declares the pinned model.

        ``run`` receives ``--model provider/model`` and resolves it against the
        workspace ``opencode.json``. A model that the config does not declare is
        rejected as ``Model not found`` no matter how healthy the endpoint is,
        so this distinguishes a genuinely wrong model id from a workspace config
        that has since been re-synced with the project registry.
        """

        provider_id, separator, model_id = self._model.partition("/")
        if not separator or self._config_path is None:
            return False
        if not self._config_path.is_file():
            return False
        try:
            config = json.loads(self._config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        provider = (config.get("provider") or {}).get(provider_id)
        if not isinstance(provider, dict):
            return False
        models = provider.get("models")
        return isinstance(models, dict) and model_id in models

    def configure_endpoint_probe(
        self,
        probe: Callable[[], Any],
        *,
        model_id: str,
        initial_result: Any = None,
        ttl_seconds: float = 30.0,
    ) -> None:
        """Attach cached endpoint/model discovery to scheduler availability."""

        self._endpoint_probe = probe
        self._endpoint_model_id = str(model_id).strip()
        self._endpoint_probe_ttl_seconds = max(1.0, float(ttl_seconds))
        self._endpoint_probe_result = initial_result
        self._endpoint_probe_expires_at = (
            time.monotonic() + self._endpoint_probe_ttl_seconds
            if initial_result is not None
            else 0.0
        )

    def _endpoint_availability(self) -> Availability | None:
        probe = self._endpoint_probe
        if probe is None:
            return None
        now = time.monotonic()
        with self._endpoint_probe_lock:
            if self._endpoint_probe_result is None or now >= self._endpoint_probe_expires_at:
                try:
                    self._endpoint_probe_result = probe()
                except Exception as exc:  # pragma: no cover - defensive plugin boundary
                    self._endpoint_probe_result = {"reachable": False, "error": str(exc)}
                self._endpoint_probe_expires_at = (
                    time.monotonic() + self._endpoint_probe_ttl_seconds
                )
            result = self._endpoint_probe_result

        if isinstance(result, Mapping):
            reachable = bool(result.get("reachable", False))
            models = result.get("models", ())
        else:
            reachable = bool(getattr(result, "reachable", False))
            models = getattr(result, "models", ())
        if not reachable:
            return Availability.NETWORK_TRANSIENT
        if self._endpoint_model_id and self._endpoint_model_id not in set(models or ()):
            return Availability.DISABLED
        return Availability.AVAILABLE

    def reset_transient_availability(self) -> None:
        if self._last_availability.is_transient:
            self._last_availability = Availability.AVAILABLE

    def _agent_for_stage(self, stage: str) -> str:
        capability = _capability_for_stage(stage)
        if capability is None:
            return ""
        return self._agent_by_capability.get(capability, "")

    def configure_heartbeat(self, callback, *, interval_seconds: float = 30.0) -> None:
        """Configure observational subprocess heartbeats for this adapter."""
        self._heartbeat.configure(callback, interval_seconds=interval_seconds)

    def configure_output(self, callback) -> None:
        """Configure a read-only observer for supervised provider output."""
        self._heartbeat.configure_output(callback)

    def configure_interaction(self, callback) -> None:
        """Attach the provider-neutral OpenCode event observer."""
        self._heartbeat.configure_interaction(callback)

    def execute(self, handoff: StructuredHandoff) -> dict[str, Any]:
        prompt = _build_prompt(handoff)
        workdir = (
            Path(handoff.working_directory).resolve()
            if handoff.working_directory
            else self._workdir
        )
        prompt_file = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix="execraft-opencode-handoff-",
            suffix=".md",
            delete=False,
        )
        try:
            prompt_file.write(prompt)
            prompt_file.flush()
            os.fsync(prompt_file.fileno())
        finally:
            prompt_file.close()
        prompt_path = Path(prompt_file.name)
        prompt_path.chmod(0o600)
        args = [
            self._binary,
            "run",
            "Read and follow the attached Execraft handoff file exactly.",
            "--file",
            str(prompt_path),
            "--format",
            "json",
            "--dir",
            str(workdir),
        ]
        if self._model:
            args += ["--model", self._model]
        # OpenCode calls this "variant" and routes it to whatever the underlying
        # provider exposes; a local model that has no variant simply ignores it.
        variant = self._effort.for_handoff(handoff.execution_context)
        if variant:
            args += ["--variant", variant]
        format_repair = handoff.execution_context.get("format_repair") is True
        provider_agent = (
            self._format_repair_agent
            if format_repair and self._format_repair_agent
            else self._agent_for_stage(handoff.stage)
        )
        if provider_agent:
            args += ["--agent", provider_agent]
        if self._auto_approve and not handoff.read_only:
            args.append("--auto")

        output_classifier = AgentOutputClassifier(
            adapter=self.adapter_name,
            provider_id=self._provider_id,
            max_internal_retry_delay_seconds=self._max_internal_retry_delay_seconds,
        )
        raw_output_callback = self._heartbeat.output_callback_for(handoff)
        event_bridge = OpenCodeEventBridge(
            self._heartbeat.interaction_callback_for(handoff)
        )

        def observe_output(stream: str, text: str) -> None:
            # OpenCode stdout is JSONL and is rendered through semantic events.
            # Persisting it again as raw Activity output doubles disk/DOM work
            # during long coding sessions; stderr remains available verbatim.
            if stream == "stderr" and raw_output_callback is not None:
                raw_output_callback(stream, text)
            event_bridge.feed(stream, text)

        try:
            environment = None
            if self._config_path is not None and self._config_path.is_file():
                environment = dict(os.environ)
                environment["OPENCODE_CONFIG"] = str(self._config_path)
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
                max_output_bytes=self._max_output_bytes,
                interactive_terminal=False,
                output_callback=observe_output,
                environment=environment,
            )
            event_bridge.finish()
        except ManagedProcessTerminated as exc:
            event_bridge.finish()
            self._last_availability, error = managed_process_failure(exc)
            raise error from exc
        except subprocess.TimeoutExpired as exc:
            event_bridge.finish()
            self._last_availability = Availability.BUSY
            raise AgentExecutionError(
                f"opencode run timed out after {self._timeout_seconds}s",
                classification="timeout",
            ) from exc
        finally:
            prompt_path.unlink(missing_ok=True)

        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        try:
            events = _parse_events(stdout)
        except ValueError as exc:
            raise AgentExecutionError(
                str(exc),
                classification="transport_protocol_error",
                persistent=False,
            ) from exc
        event_error = _extract_error_message(events)

        # OpenCode's JSON mode is an event stream. A session.error event is a
        # failed transport even if a provider/plugin accidentally leaves the
        # process exit code at zero. Never reinterpret it as a successful run.
        if completed.returncode != 0 or event_error:
            error_message = (
                event_error
                or stderr.strip()
                or f"opencode run exited {completed.returncode}"
            )
            self._last_availability, error = provider_failure(
                self.adapter_name, error_message
            )
            raise error

        self._last_availability = Availability.AVAILABLE
        terminal_message = _extract_terminal_message(events)
        if terminal_message is None:
            has_terminal_step = any(
                event.get("type") == "step_finish" for event in events
            )
            raise AgentExecutionError(
                (
                    "OpenCode terminal step contained no assistant message"
                    if has_terminal_step
                    else "OpenCode JSONL ended without a terminal step"
                ),
                classification="transport_protocol_error",
                persistent=False,
                artifact_payload={
                    "transport": _transport_summary(events, stdout, stderr),
                },
            )
        return {
            "ok": True,
            "work_package_id": handoff.work_package_id,
            "final_message": terminal_message[1],
            "usage": _extract_usage(events),
            "cost": _extract_cost(events),
            "transport": _transport_summary(events, stdout, stderr),
        }


def _capability_for_stage(stage: str) -> AgentCapability | None:
    normalized = stage.strip().lower()
    if normalized in {"fix_review", "scope_recovery"} or normalized.endswith(
        "_fix_review"
    ):
        return AgentCapability.FIX_REVIEW
    if normalized in {"review", "final_review"} or normalized.endswith(
        "_review"
    ):
        return AgentCapability.REVIEW
    if normalized == "decompose" or normalized.endswith("_decompose"):
        return AgentCapability.DECOMPOSE
    if normalized == "implement" or normalized.endswith("_implement"):
        return AgentCapability.IMPLEMENT
    if normalized == "verify" or normalized.endswith("_verify"):
        return AgentCapability.VERIFY
    if normalized == "plan":
        return AgentCapability.PLAN
    if normalized == "brief":
        return AgentCapability.BRIEF
    if normalized == "close":
        return AgentCapability.CLOSE
    return None


def _build_prompt(handoff: StructuredHandoff) -> str:
    return build_agent_prompt(handoff)


def _parse_events(stdout: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"OpenCode emitted malformed JSONL at line {len(events) + 1}"
            ) from exc
        if not isinstance(event, dict):
            raise ValueError(
                f"OpenCode emitted a non-object JSONL record at line {len(events) + 1}"
            )
        events.append(event)
    return events


def _extract_text_messages(
    events: list[dict[str, Any]],
) -> list[tuple[str, str]]:
    """Group OpenCode text events into ordered assistant messages.

    ``opencode run --format json`` emits an event stream, not one response
    object. Long tool-using sessions contain many text messages. Concatenating
    every text event turns the complete transcript into ``final_message`` and
    can make a valid final JSON response impossible to extract. Message IDs are
    stable within one assistant message, so preserve their order and join only
    parts belonging to the same message.
    """

    chunks: dict[str, list[str]] = {}
    last_position: dict[str, int] = {}
    anonymous_index = 0
    for event_index, event in enumerate(events):
        if event.get("type") != "text":
            continue
        part = event.get("part")
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if not isinstance(text, str) or not text:
            continue
        message_id = str(part.get("messageID") or event.get("messageID") or "").strip()
        if not message_id:
            # Without a message ID there is no safe way to infer whether two
            # separated text events belong together. Keep each event isolated
            # so the last event remains the final candidate.
            anonymous_index += 1
            message_id = f"anonymous-{anonymous_index}"
        chunks.setdefault(message_id, []).append(text)
        last_position[message_id] = event_index
    message_ids = sorted(chunks, key=last_position.__getitem__)
    return [
        (message_id, "".join(chunks[message_id]))
        for message_id in message_ids
    ]


def _extract_terminal_message(
    events: list[dict[str, Any]],
) -> tuple[str, str] | None:
    """Return text belonging to the final completed OpenCode step.

    A tool-heavy session can end with a zero-token ``step_finish`` after an
    earlier progress message. Reusing that stale text as the final answer turns
    a truncated transport into a misleading structured-output validation
    failure. Correlate the last terminal step with its message ID instead.
    """

    finish_index = next(
        (
            index
            for index in range(len(events) - 1, -1, -1)
            if events[index].get("type") == "step_finish"
        ),
        -1,
    )
    if finish_index < 0:
        return None
    finish_part = events[finish_index].get("part")
    finish_part = finish_part if isinstance(finish_part, dict) else {}
    message_id = str(
        finish_part.get("messageID") or events[finish_index].get("messageID") or ""
    ).strip()
    messages = _extract_text_messages(events[: finish_index + 1])
    if message_id:
        return next(
            (message for message in reversed(messages) if message[0] == message_id),
            None,
        )

    step_start_index = next(
        (
            index
            for index in range(finish_index - 1, -1, -1)
            if events[index].get("type") == "step_start"
        ),
        0,
    )
    messages = _extract_text_messages(events[step_start_index : finish_index + 1])
    return messages[-1] if messages else None


def _extract_usage(events: list[dict[str, Any]]) -> dict[str, Any]:
    for event in reversed(events):
        if event.get("type") == "step_finish":
            tokens = event.get("part", {}).get("tokens")
            if isinstance(tokens, dict):
                return tokens
    return {}


def _extract_cost(events: list[dict[str, Any]]) -> float | None:
    for event in reversed(events):
        if event.get("type") == "step_finish":
            cost = event.get("part", {}).get("cost")
            if isinstance(cost, (int, float)):
                return float(cost)
    return None


def _extract_error_message(events: list[dict[str, Any]]) -> str:
    for event in events:
        if event.get("type") == "error":
            error = event.get("error")
            if not isinstance(error, dict):
                return ""
            data = error.get("data")
            if isinstance(data, dict) and data.get("message"):
                return str(data["message"])
            return str(error.get("message", ""))
    return ""



def _transport_summary(
    events: list[dict[str, Any]], stdout: str, stderr: str
) -> dict[str, Any]:
    event_types = [str(event.get("type", "unknown")) for event in events]
    sessions = sorted(
        {str(event.get("sessionID")) for event in events if event.get("sessionID")}
    )
    text_messages = _extract_text_messages(events)
    terminal_message = _extract_terminal_message(events)
    return {
        "event_count": len(events),
        "event_types": event_types,
        "text_event_count": sum(1 for item in event_types if item == "text"),
        "assistant_message_count": len(text_messages),
        "final_message_id": terminal_message[0] if terminal_message else "",
        "error_event_count": sum(1 for item in event_types if item == "error"),
        "session_ids": sessions,
        "stdout_bytes": len(stdout.encode("utf-8", errors="replace")),
        "stdout_sha256": hashlib.sha256(stdout.encode("utf-8", errors="replace")).hexdigest(),
        "stderr_preview": stderr.strip().replace("\x00", "")[:500],
    }
