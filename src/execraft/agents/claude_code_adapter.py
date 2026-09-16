"""Claude Code adapter with semantic streaming and one-shot compatibility.

The preferred transport is Claude Code's ``stream-json`` protocol, which
provides assistant deltas, thinking summaries, tool activity and queued user
guidance.  Older CLIs are retried without optional subagent forwarding before
the adapter falls back to the durable one-shot JSON contract.  This keeps live
visibility available across mixed developer workstations without pretending
that the CLI offers a hard in-flight interrupt.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping
from execraft.agents.claude_live import ClaudeStreamController
from execraft.agents.effort import EffortPolicy
from execraft.agents.heartbeat import AgentHeartbeatEmitter, run_with_heartbeat
from execraft.agents.live_session import LiveSessionUnavailable, managed_jsonl_session
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


ClaudeRunner = Callable[..., "subprocess.CompletedProcess[str]"]

# Claude Code is the only supported CLI exposing the full five-level ladder.
_SUPPORTED_EFFORT = ("low", "medium", "high", "xhigh", "max")

_DEFAULT_CAPABILITIES = {
    AgentCapability.IMPLEMENT,
    AgentCapability.REVIEW,
    AgentCapability.FIX_REVIEW,
}

# Mirrors codex_adapter.py's mapping: only failure classifications that mean
# the provider itself is unusable right now change availability. A one-off
# request failure (bad model, malformed prompt) does not — the
# orchestrator's own per-call failover already handles that.



class ClaudeCodeAgentAdapter(AgentAdapter):
    """Drives the real `claude` CLI as an orchestrator agent.

    Every call to `execute()` invokes the actual CLI (or an injected
    `runner`, for tests) — no simulated mode. Tests exercise this class
    exclusively through an injected fake runner using output captured from
    two real, user-approved live calls, so the automated suite never
    spends on a real model call itself.
    """

    def __init__(
        self,
        *,
        provider_id: str = "claude-code",
        capabilities: set[AgentCapability] | None = None,
        model: str = "",
        effort: str = "",
        effort_by_capability: dict[str, str] | None = None,
        permission_mode: str = "acceptEdits",
        workdir: Path | None = None,
        timeout_seconds: int = 600,
        inactivity_timeout_seconds: int = 900,
        output_silence_timeout_seconds: int = 0,
        first_output_timeout_seconds: int = 0,
        max_internal_retry_delay_seconds: int = 120,
        runner: ClaudeRunner | None = None,
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
        self._permission_mode = permission_mode
        self._workdir = workdir or Path.cwd()
        self._timeout_seconds = timeout_seconds
        self._inactivity_timeout_seconds = inactivity_timeout_seconds
        self._output_silence_timeout_seconds = output_silence_timeout_seconds
        self._first_output_timeout_seconds = first_output_timeout_seconds
        self._max_internal_retry_delay_seconds = max_internal_retry_delay_seconds
        self._runner = runner or managed_run
        self._runner_is_injected = runner is not None
        self._binary_explicit = binary is not None
        self._binary = binary or "claude"
        self._live_sessions = bool(live_sessions)
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
            read_only_enforcement="provider_policy",
            workspace_write=True,
            network_isolation=False,
            command_allowlist=False,
            structured_output=True,
            structured_output_enforcement="prompt_only",
            streaming=True,
            raw_output_streaming=True,
            semantic_streaming=self._use_live_session,
            provider_native_steering=self._use_live_session,
            interactive_pty=False,
            session_resume=self._use_live_session,
        )

    @property
    def adapter_name(self) -> str:
        return "claude-code"

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
        return "queued_guidance" if self._use_live_session else "none"

    @property
    def transport(self) -> str:
        return "claude-cli-stream-json" if self._use_live_session else "claude-cli-one-shot"

    @property
    def _use_live_session(self) -> bool:
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
        """Attach the Claude stream-JSON steering queue without creating a PTY."""
        self._heartbeat.configure_operator_controls(callback)

    def configure_interaction(self, callback) -> None:
        """Attach the provider-neutral streamed conversation observer."""
        self._heartbeat.configure_interaction(callback)

    def execute(self, handoff: StructuredHandoff) -> dict[str, Any]:
        if self._use_live_session:
            resume_session = self._resume_session_for(handoff)
            if resume_session:
                # Resuming replays the provider's own cached conversation prefix
                # instead of paying a cold cache write and re-deriving repository
                # context. It is strictly an optimization: a stale or unknown
                # session id must fall back to a normal cold invocation rather
                # than fail the attempt.
                try:
                    return self._execute_live(
                        handoff, optional_flags=True, resume_session=resume_session
                    )
                except LiveSessionUnavailable as resume_exc:
                    callback = self._heartbeat.interaction_callback_for(handoff)
                    if callback is not None:
                        callback(
                            {
                                "kind": "status",
                                "text": (
                                    "Claude could not resume the prior session; "
                                    "starting a fresh one"
                                ),
                                "summary": str(resume_exc),
                                "status": "compatibility_retry",
                                "provider": "claude-code",
                                "interaction_mode": "conversation",
                                "streaming": True,
                                "steering_supported": True,
                                "transport": "claude-cli-stream-json",
                                "control_mode": "queued_guidance",
                            }
                        )
            try:
                return self._execute_live(handoff, optional_flags=True)
            except LiveSessionUnavailable as primary_exc:
                callback = self._heartbeat.interaction_callback_for(handoff)
                if callback is not None:
                    callback(
                        {
                            "kind": "status",
                            "text": (
                                "Claude stream-json rejected an optional flag; retrying "
                                "the live protocol without the optional flag set"
                            ),
                            "summary": str(primary_exc),
                            "status": "compatibility_retry",
                            "provider": "claude-code",
                            "interaction_mode": "conversation",
                            "streaming": True,
                            "steering_supported": True,
                            "transport": "claude-cli-stream-json",
                            "control_mode": "queued_guidance",
                        }
                    )
                try:
                    return self._execute_live(handoff, optional_flags=False)
                except LiveSessionUnavailable as exc:
                    if callback is not None:
                        callback(
                            {
                                "kind": "status",
                                "text": (
                                    "Live Claude stream-json unavailable; using one-shot "
                                    f"compatibility mode: {exc}"
                                ),
                                "status": "degraded",
                                "provider": "claude-code",
                                "interaction_mode": "conversation",
                                "streaming": False,
                                "steering_supported": False,
                                "transport": "claude-cli-one-shot",
                                "control_mode": "none",
                            }
                        )
        return self._execute_legacy(handoff)

    def _resume_session_for(self, handoff: StructuredHandoff) -> str:
        """Return a prior session id this adapter may safely resume.

        Session ids are provider-scoped, so a request recorded against another
        provider is ignored rather than passed to a CLI that cannot know it.
        """

        request = handoff.execution_context.get("resume_session")
        if not isinstance(request, Mapping):
            return ""
        if str(request.get("provider_id", "")) != self._provider_id:
            return ""
        return str(request.get("session_id", "")).strip()

    def _execute_live(
        self,
        handoff: StructuredHandoff,
        *,
        optional_flags: bool,
        resume_session: str = "",
    ) -> dict[str, Any]:
        prompt = _build_prompt(handoff)
        workdir = (
            Path(handoff.working_directory).resolve()
            if handoff.working_directory
            else self._workdir
        )
        permission_mode = "plan" if handoff.read_only else self._permission_mode
        args = [
            self._binary,
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--replay-user-messages",
        ]
        if resume_session:
            # ``--fork-session`` keeps the resumed transcript immutable, so the
            # failed attempt referenced by the invocation ledger stays intact
            # and auditable while the repair turn continues from its context.
            args += ["--resume", resume_session, "--fork-session"]
        if optional_flags:
            args.append("--forward-subagent-text")
            # The default system prompt embeds cwd, environment info, memory
            # paths and git status. The orchestrator drives many invocations
            # against one workspace whose git status changes after every write
            # stage, so those sections invalidate the cached system prefix on
            # essentially every call. Moving them into the first user message
            # keeps the system prefix byte-stable across invocations.
            args.append("--exclude-dynamic-system-prompt-sections")
        effort = self._effort.for_handoff(handoff.execution_context)
        if effort:
            args += ["--effort", effort]
        if self._model:
            args += ["--model", self._model]
        if permission_mode:
            args += ["--permission-mode", permission_mode]
        for root in handoff.additional_writable_roots:
            args += ["--add-dir", root]
        if handoff.expected_output_schema:
            args += [
                "--json-schema",
                json.dumps(dict(handoff.expected_output_schema), ensure_ascii=False),
            ]

        controller = ClaudeStreamController(
            handoff=handoff,
            prompt=prompt,
            interaction_callback=self._heartbeat.interaction_callback_for(handoff),
            control_callback=self._heartbeat.control_callback_for(handoff),
        )
        classifier = AgentOutputClassifier(
            adapter=self.adapter_name,
            provider_id=self._provider_id,
            max_internal_retry_delay_seconds=self._max_internal_retry_delay_seconds,
        )

        def classify_stderr(stream: str, text: str):
            return classifier(stream, text) if stream == "stderr" else None

        raw_output_callback = self._heartbeat.output_callback_for(handoff)

        def forward_diagnostics(stream: str, text: str) -> None:
            if stream == "stderr" and raw_output_callback is not None:
                raw_output_callback(stream, text)

        try:
            completed = managed_jsonl_session(
                args,
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
                terminate_on_complete=False,
            )
        except ManagedProcessTerminated as exc:
            self._raise_managed_failure(exc)
        except subprocess.TimeoutExpired as exc:
            self._last_availability = Availability.BUSY
            raise AgentExecutionError(
                f"claude stream-json made no protocol progress for {self._timeout_seconds}s",
                classification="timeout",
            ) from exc
        except (OSError, RuntimeError) as exc:
            if not controller.work_started and _is_live_protocol_unavailable(exc):
                raise LiveSessionUnavailable(str(exc)) from exc
            raise

        if not completed.completed_by_protocol:
            detail = controller.error or completed.stderr.strip() or "Claude stream-json exited before result"
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
            "total_cost_usd": controller.total_cost_usd,
            "session_id": controller.session_id,
            "structured_output": controller.structured_output,
            "interaction_mode": "conversation",
        }

    def _execute_legacy(self, handoff: StructuredHandoff) -> dict[str, Any]:
        prompt = _build_prompt(handoff)
        workdir = Path(handoff.working_directory).resolve() if handoff.working_directory else self._workdir
        permission_mode = "plan" if handoff.read_only else self._permission_mode
        args = [self._binary, "-p"]
        stdin_payload: str | None = None
        if self._runner_is_injected:
            # Preserve the historical plugin runner contract. Production Claude
            # Code invocations use its documented stdin text input instead.
            args.append(prompt)
        else:
            stdin_payload = prompt
        args += ["--output-format", "json"]
        effort = self._effort.for_handoff(handoff.execution_context)
        if effort:
            args += ["--effort", effort]
        if self._model:
            args += ["--model", self._model]
        if permission_mode:
            args += ["--permission-mode", permission_mode]
        for root in handoff.additional_writable_roots:
            args += ["--add-dir", root]

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
                f"claude -p timed out after {self._timeout_seconds}s",
                classification="timeout",
            ) from exc

        payload = _parse_result(completed.stdout)
        is_error = bool(payload.get("is_error")) if payload else completed.returncode != 0

        if completed.returncode != 0 or is_error:
            error_message = (
                (payload.get("result") if payload else "")
                or (completed.stderr or "").strip()
                or f"claude -p exited {completed.returncode}"
            )
            self._raise_provider_failure(error_message)

        self._last_availability = Availability.AVAILABLE
        return {
            "ok": True,
            "work_package_id": handoff.work_package_id,
            "final_message": payload.get("result", "") if payload else "",
            "usage": payload.get("usage", {}) if payload else {},
            "total_cost_usd": payload.get("total_cost_usd") if payload else None,
            "session_id": payload.get("session_id", "") if payload else "",
        }

    def _raise_managed_failure(self, exc: ManagedProcessTerminated) -> None:
        self._last_availability, error = managed_process_failure(exc)
        raise error from exc

    def _raise_provider_failure(self, message: str) -> None:
        self._last_availability, error = provider_failure(self.adapter_name, message)
        raise error


def _build_prompt(handoff: StructuredHandoff) -> str:
    return build_agent_prompt(handoff)


def _parse_result(stdout: str) -> dict[str, Any] | None:
    stripped = stdout.strip()
    if not stripped:
        return None
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None



def _is_live_protocol_unavailable(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "unknown option",
            "unknown argument",
            "invalid jsonl",
            "exited before result",
            "no such file or directory",
            # A resumed session id that the CLI no longer knows about. Treated
            # as "live protocol unavailable" so the caller retries cold instead
            # of failing an otherwise healthy attempt.
            "no conversation found",
            "session not found",
            "invalid session id",
        )
    )
