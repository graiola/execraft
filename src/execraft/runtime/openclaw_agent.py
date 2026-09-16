"""OpenClaw agent execution with continuation and lazy skills.

Execraft remains authoritative for workflow state. Runtime sessions are reusable
only when the control plane supplies a compatible context epoch; if the Gateway
cannot prove that session still exists, execution cold-reconstructs from the
complete durable handoff instead of risking a delta against missing state.
"""

from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping
from urllib.parse import urlsplit

from execraft.agents.profile import AgentProfileConfig
from execraft.execution_identity import ExecutionIdentity
from execraft.network import is_loopback_host
from execraft.orchestrate.scheduler import AgentExecutionError, Availability
from execraft.runtime_config import RuntimeConfig

from .contracts import (
    RuntimeCapabilities,
    RuntimeExecutionRequest,
    RuntimeExecutionResult,
    RuntimeSessionRef,
)
from .openclaw_subagent_turn import (
    prepare_openclaw_subagent_turn,
)
from .subagent_policy import SubagentProfilePolicy
from .openclaw_execution_turn import (
    ActiveOpenClawRun,
    GatewayTurnOutcome,
    build_gateway_runtime_metadata,
    run_gateway_turn,
)
from .openclaw_gateway import (
    OpenClawGatewayDisconnected,
    OpenClawGatewayError,
    OpenClawGatewayRequestError,
    OpenClawGatewayTimeout,
    OpenClawPairingRequired,
    OpenClawVersionMismatch,
)
from .openclaw_service import OpenClawDiagnostic, OpenClawGatewayService
from .openclaw_skill_runtime import OpenClawSkillTurn, prepare_openclaw_skill_turn
from .openclaw_skills import OpenClawSkillProjectionError
from .openclaw_security_turn import (
    resolve_openclaw_security_turn,
)

_SUCCESS_STATUSES = frozenset({"ok", "completed", "complete", "success"})
_TIMEOUT_STATUSES = frozenset({"timeout", "timed_out"})
_CANCELLED_STATUSES = frozenset({"cancelled", "canceled", "aborted"})


class OpenClawRuntimeHost:
    """Share one Gateway service safely across profiles using the same runtime.

    The host reference-counts in-flight runs and serializes first-start/last-stop
    transitions so shared profiles cannot race Gateway lifecycle operations.
    """

    def __init__(self, service: OpenClawGatewayService) -> None:
        self.service = service
        self._condition = threading.Condition(threading.RLock())
        self._active = 0
        # Serializes both startup and last-lease shutdown. A new profile must
        # never race a still-running cleanup or observe an unvalidated service.
        self._transitioning = False
        self._diagnostic: OpenClawDiagnostic | None = None
        self._cleanup_warning = ""

    @contextmanager
    def lease(self) -> Iterator[OpenClawGatewayService]:
        self._acquire()
        try:
            yield self.service
        finally:
            self._release()

    def _acquire(self) -> None:
        with self._condition:
            while self._transitioning:
                self._condition.wait()
            if self._active:
                diagnostic = self._diagnostic
                if diagnostic is None or not diagnostic.healthy:
                    raise RuntimeError("OpenClaw host active without a healthy diagnostic")
                self._active += 1
                return
            self._transitioning = True

        try:
            diagnostic = self.service.start()
        except BaseException:
            self._finish_failed_start()
            raise

        if not diagnostic.healthy:
            self._finish_failed_start()
            raise _diagnostic_error(diagnostic)
        with self._condition:
            self._diagnostic = diagnostic
            self._active = 1
            self._cleanup_warning = ""
            self._transitioning = False
            self._condition.notify_all()

    def _release(self) -> None:
        with self._condition:
            if self._active <= 0:
                raise RuntimeError("OpenClaw host lease accounting underflow")
            self._active -= 1
            if self._active:
                return
            # Block new acquisitions until last-lease shutdown completes.
            self._transitioning = True

        cleanup_warning = self._stop_service()
        with self._condition:
            self._diagnostic = None
            self._cleanup_warning = cleanup_warning
            self._transitioning = False
            self._condition.notify_all()

    def _finish_failed_start(self) -> None:
        cleanup_warning = self._stop_service()
        with self._condition:
            self._diagnostic = None
            self._cleanup_warning = cleanup_warning
            self._transitioning = False
            self._condition.notify_all()

    def _stop_service(self) -> str:
        try:
            self.service.stop()
        except Exception as exc:
            # Never replay a completed turn just because cleanup failed; the
            # next startup/health check decides whether the host is reusable.
            return f"OpenClaw Gateway cleanup failed: {exc}"
        return ""

    @property
    def cleanup_warning(self) -> str:
        with self._condition:
            return self._cleanup_warning


class OpenClawAgentRuntime:
    """Execute one Execraft profile through OpenClaw with safe continuation."""

    def __init__(
        self,
        *,
        profile: AgentProfileConfig,
        runtime: RuntimeConfig,
        identity: ExecutionIdentity,
        host: OpenClawRuntimeHost,
        skill_workspace: Path | None = None,
        subagent_policy: SubagentProfilePolicy | None = None,
    ) -> None:
        if runtime.openclaw is None:
            raise ValueError("OpenClawAgentRuntime requires OpenClaw runtime options")
        self.profile = profile
        self.runtime_config = runtime
        self._identity = identity
        self._host = host
        self._skill_workspace = (
            Path(skill_workspace).expanduser().resolve()
            if skill_workspace is not None
            else None
        )
        self._skill_lock = threading.RLock()
        self._subagent_policy = subagent_policy
        self._availability = Availability.AVAILABLE
        self._active_runs: dict[str, ActiveOpenClawRun] = {}
        self._active_lock = threading.RLock()

    @property
    def runtime_id(self) -> str:
        return self.runtime_config.id

    @property
    def candidate_id(self) -> str:
        return self._identity.candidate_id

    @property
    def provider_id(self) -> str:
        """Legacy scheduler/status projection retained during migration."""
        return self._identity.provider_id

    @property
    def execution_identity(self) -> ExecutionIdentity:
        return self._identity

    @property
    def capabilities(self):
        return self.profile.capabilities

    @property
    def availability(self) -> Availability:
        return self._availability

    @availability.setter
    def availability(self, value: Availability) -> None:
        self._availability = value

    @property
    def model(self) -> str:
        return self._identity.model

    @property
    def adapter_name(self) -> str:
        return "openclaw"

    @property
    def execution_capabilities(self) -> RuntimeCapabilities:
        options = self.runtime_config.openclaw
        managed = bool(options is not None and options.mode.value == "managed")
        sandbox = self.profile.policy.sandbox.strip().lower()
        return RuntimeCapabilities(
            read_only_enforcement="hard" if managed else "provider_policy",
            workspace_write=sandbox != "read-only",
            network_isolation=managed and sandbox != "host-integration",
            command_allowlist=managed,
            structured_output=True,
            structured_output_enforcement="prompt_only",
            streaming=True,
            raw_output_streaming=True,
            semantic_streaming=True,
            provider_native_steering=False,
            interactive_pty=False,
            session_resume=True,
        )

    @property
    def active_runs(self) -> tuple[ActiveOpenClawRun, ...]:
        with self._active_lock:
            return tuple(self._active_runs.values())

    def execute(self, handoff: Any) -> dict[str, Any]:
        """Legacy compatibility call; normal orchestration uses execute_runtime."""
        result = self.execute_runtime(
            RuntimeExecutionRequest(
                identity=self._identity,
                handoff=handoff,
                capability=str(getattr(handoff, "stage", "")),
                package_id=str(getattr(handoff, "work_package_id", "")),
                stage=str(getattr(handoff, "stage", "")),
            )
        )
        return result.output

    def execute_runtime(self, request: RuntimeExecutionRequest) -> RuntimeExecutionResult:
        if request.identity.candidate_id != self.candidate_id:
            raise AgentExecutionError(
                "OpenClaw runtime received a request for a different execution candidate",
                classification="configuration_error",
                persistent=True,
            )
        self._validate_session_ref(request)
        self._assert_local_workspace_runtime()
        # Serialize same-profile turns so their generated skill snapshot is stable.
        with self._skill_lock:
            return self._execute_runtime_with_skills(request)

    def _execute_runtime_with_skills(
        self, request: RuntimeExecutionRequest
    ) -> RuntimeExecutionResult:
        execution_id = _execution_id(request)
        timeout = max(1.0, float(self.profile.policy.timeout_seconds))
        security_turn = resolve_openclaw_security_turn(
            self.runtime_config, self.profile, request
        )
        security_turn, subagent_turn = prepare_openclaw_subagent_turn(
            security_turn, self.profile, request, self._subagent_policy
        )
        runtime_request = security_turn.request
        agent_id = security_turn.agent_id
        skill_turn = _prepare_runtime_skill_turn(
            self.runtime_config, self._skill_workspace, runtime_request
        )
        try:
            outcome = run_gateway_turn(
                host=self._host,
                runtime_config=self.runtime_config,
                runtime_request=runtime_request,
                security_turn=security_turn,
                subagent_turn=subagent_turn,
                subagent_policy=self._subagent_policy,
                skill_turn=skill_turn,
                agent_id=agent_id,
                execution_id=execution_id,
                timeout=timeout,
                cold_session_key=_cold_session_key(agent_id, execution_id),
                agent_params=_agent_params,
                set_active=self._set_active,
                clear_active=self._clear_active,
            )
        except AgentExecutionError:
            raise
        except BaseException as exc:
            self._availability = _availability_for_exception(exc)
            raise _execution_error(exc) from exc
        return self._finish_gateway_turn(
            request=request,
            security_turn=security_turn,
            subagent_turn=subagent_turn,
            skill_turn=skill_turn,
            agent_id=agent_id,
            outcome=outcome,
        )

    def _finish_gateway_turn(
        self,
        *,
        request: RuntimeExecutionRequest,
        security_turn: Any,
        subagent_turn: Any,
        skill_turn: OpenClawSkillTurn,
        agent_id: str,
        outcome: GatewayTurnOutcome,
    ) -> RuntimeExecutionResult:
        status = _terminal_status(outcome.wait_payload, outcome.final_payload)
        if status not in _SUCCESS_STATUSES:
            error = _terminal_error(
                status, outcome.wait_payload, outcome.final_payload
            )
            self._availability = _availability_for_exception(error)
            raise error
        self._availability = Availability.AVAILABLE
        session = RuntimeSessionRef(
            runtime_id=self.runtime_id,
            candidate_id=self.candidate_id,
            session_id=outcome.active.session_key,
            backend="gateway-session-key",
            context_epoch=request.context_epoch,
        )
        final_message = _final_message(outcome.final_payload) or outcome.events.assistant_text()
        if not final_message.strip():
            error = AgentExecutionError(
                "OpenClaw run completed without an assistant result",
                classification="invalid_output",
            )
            self._availability = _availability_for_exception(error)
            raise error
        output = {
            "ok": True,
            "work_package_id": request.package_id or request.handoff.work_package_id,
            "final_message": final_message,
        }
        usage = _extract_usage(outcome.final_payload) or outcome.events.usage()
        if usage:
            output["usage"] = usage
        runtime_meta = build_gateway_runtime_metadata(
            runtime_config=self.runtime_config,
            request=request,
            security_turn=security_turn,
            subagent_turn=subagent_turn,
            skill_turn=skill_turn,
            agent_id=agent_id,
            status=status,
            outcome=outcome,
            cleanup_warning=self._host.cleanup_warning,
            extract_provider_model=_extract_provider_model,
            extract_session_id=_extract_openclaw_session_id,
        )
        return RuntimeExecutionResult(
            output=output,
            identity=self._identity,
            session_ref=session,
            runtime_metadata=runtime_meta,
            rendered_prompt=outcome.turn.prompt,
        )

    def _validate_session_ref(self, request: RuntimeExecutionRequest) -> None:
        session = request.session_ref
        if session is None:
            return
        if (
            session.runtime_id != self.runtime_id
            or session.candidate_id != self.candidate_id
        ):
            raise AgentExecutionError(
                "OpenClaw continuation reference belongs to a different runtime candidate",
                classification="configuration_error",
                persistent=True,
            )
        if not request.context_epoch or session.context_epoch != request.context_epoch:
            raise AgentExecutionError(
                "OpenClaw continuation reference has an incompatible context epoch",
                classification="configuration_error",
                persistent=True,
            )

    def cancel(self, execution_id: str) -> bool:
        """Abort one active run through the Gateway's public stop RPCs."""
        with self._active_lock:
            active = self._active_runs.get(execution_id)
            if active is None:
                active = next(
                    (item for item in self._active_runs.values() if item.run_id == execution_id),
                    None,
                )
        if active is None:
            return False
        response = self._host.service.client.cancel_run(
            active.run_id, session_key=active.session_key
        )
        return _cancel_confirmed(response, active.run_id)

    def session_ref_from_result(self, result: Mapping[str, Any] | None) -> RuntimeSessionRef | None:
        # Normalized runtime dispatch obtains the session ref from
        # RuntimeExecutionResult.  Legacy execute() intentionally does not make
        # a cold run look resumable through provider-shaped result metadata.
        return None

    def _assert_local_workspace_runtime(self) -> None:
        gateway = self.runtime_config.openclaw.gateway  # type: ignore[union-attr]
        host = urlsplit(gateway).hostname or ""
        if not is_loopback_host(host):
            raise AgentExecutionError(
                "OpenClaw execution requires a loopback Gateway because Execraft "
                "remote full-runtime workspace transport is not supported",
                classification="configuration_error",
                persistent=True,
            )

    def _set_active(self, run: ActiveOpenClawRun) -> None:
        with self._active_lock:
            self._active_runs[run.execution_id] = run

    def _clear_active(self, execution_id: str) -> None:
        with self._active_lock:
            self._active_runs.pop(execution_id, None)



def _agent_params(*, prompt: str, agent_id: str, session_key: str, timeout: float) -> dict[str, Any]:
    return {
        "message": prompt,
        "agentId": agent_id,
        "sessionKey": session_key,
        "deliver": False,
        "timeout": int(timeout),
        "cleanupBundleMcpOnRunEnd": True,
    }

def _prepare_runtime_skill_turn(
    runtime: RuntimeConfig,
    workspace: Path | None,
    request: RuntimeExecutionRequest,
) -> OpenClawSkillTurn:
    options = runtime.openclaw
    if options is None:
        raise AgentExecutionError(
            "OpenClaw runtime options are required for skill projection",
            classification="configuration_error",
            persistent=True,
        )
    try:
        return prepare_openclaw_skill_turn(
            options,
            workspace=workspace,
            handoff=request.handoff,
            has_session_ref=request.session_ref is not None,
        )
    except (OpenClawSkillProjectionError, ValueError) as exc:
        raise AgentExecutionError(
            f"OpenClaw skill projection failed: {exc}",
            classification="configuration_error",
            persistent=True,
        ) from exc


def _cancel_confirmed(payload: Any, run_id: str) -> bool:
    if payload is True:
        return True
    if not isinstance(payload, Mapping):
        return False
    if payload.get("aborted") is not True and payload.get("ok") is not True:
        return False
    run_ids = payload.get("runIds")
    return not isinstance(run_ids, list) or run_id in {str(item) for item in run_ids}


def _execution_id(request: RuntimeExecutionRequest) -> str:
    handoff_id = str(getattr(request.handoff, "handoff_id", "")).strip()
    stable = handoff_id or uuid.uuid4().hex
    return f"execraft-{request.identity.candidate_id}-{request.attempt}-{stable}"


def _cold_session_key(profile_id: str, execution_id: str) -> str:
    # Keep each attempt isolated while retaining a recognizable agent namespace.
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in execution_id)
    return f"agent:{profile_id}:execraft-{safe[-96:]}"


def _terminal_status(wait_payload: Any, final_payload: Any) -> str:
    statuses = [
        str(payload.get("status", "")).strip().lower()
        for payload in (wait_payload, final_payload)
        if isinstance(payload, Mapping) and str(payload.get("status", "")).strip()
    ]
    # Fail closed on disagreement between the completion barrier and terminal
    # response. A successful status must never hide a terminal error/timeout.
    for status in statuses:
        if status not in _SUCCESS_STATUSES and status not in {"accepted", "in_flight"}:
            return status
    for status in statuses:
        if status in _SUCCESS_STATUSES:
            return status
    return "completed" if _final_message(final_payload) else "unknown"


def _terminal_error(status: str, wait_payload: Any, final_payload: Any) -> AgentExecutionError:
    error_message, error_kind = _agent_error(final_payload)
    detail = (
        error_message
        or _summary(final_payload)
        or _summary(wait_payload)
        or f"OpenClaw run ended with status {status!r}"
    )
    classification = _classify_agent_error(status, error_kind, detail)
    return AgentExecutionError(
        detail,
        classification=classification,
        persistent=classification in {
            "auth_failure",
            "authentication_required",
            "configuration_error",
            "invalid_model",
        },
        health_dimension=_terminal_failure_dimension(classification),
    )


def _agent_error(payload: Any) -> tuple[str, str]:
    meta, _agent_meta = _result_metadata(payload)
    raw = meta.get("error")
    if isinstance(raw, Mapping):
        return str(raw.get("message", "")).strip(), str(raw.get("kind", "")).strip().lower()
    if isinstance(raw, str):
        return raw.strip(), ""
    return "", ""


def _classify_agent_error(status: str, kind: str, detail: str) -> str:
    text = f"{kind} {detail}".lower()
    if status in _TIMEOUT_STATUSES or "timeout" in kind or "timed out" in text:
        return "timeout"
    if status in _CANCELLED_STATUSES or "abort" in kind or "cancel" in kind:
        return "cancelled"
    if "auth" in kind or "unauthorized" in text or "authentication" in text:
        return "auth_failure"
    if "rate" in kind or "rate limit" in text:
        return "rate_limited"
    if "quota" in kind or "quota" in text:
        return "quota_exhausted"
    if ("model" in kind and any(token in kind for token in ("invalid", "not_found", "unknown"))) or (
        "model" in text and any(token in text for token in ("not found", "invalid model", "unknown model"))
    ):
        return "invalid_model"
    if any(token in text for token in ("connection", "network", "fetch failed", "econn", "socket")):
        return "network_transient"
    return "unclassified"


def _summary(payload: Any) -> str:
    if not isinstance(payload, Mapping):
        return ""
    return str(payload.get("summary", payload.get("message", ""))).strip()


def _final_message(payload: Any) -> str:
    if not isinstance(payload, Mapping):
        return ""
    result = payload.get("result")
    result_map = result if isinstance(result, Mapping) else {}
    payloads = result_map.get("payloads", payload.get("payloads", ()))
    texts: list[str] = []
    if isinstance(payloads, (list, tuple)):
        for item in payloads:
            if isinstance(item, Mapping):
                text = str(item.get("text", "")).strip()
                if text:
                    texts.append(text)
    if texts:
        return "\n\n".join(texts)
    for mapping in (result_map, payload):
        for key in ("final", "finalMessage", "text", "message"):
            value = str(mapping.get(key, "")).strip()
            if value:
                return value
    return ""


def _result_metadata(payload: Any) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    if not isinstance(payload, Mapping):
        return {}, {}
    result = payload.get("result")
    result_map = result if isinstance(result, Mapping) else {}
    meta = result_map.get("meta")
    meta_map = meta if isinstance(meta, Mapping) else {}
    agent_meta = meta_map.get("agentMeta")
    agent_meta_map = agent_meta if isinstance(agent_meta, Mapping) else {}
    return meta_map, agent_meta_map


def _extract_usage(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {}
    result = payload.get("result")
    result_map = result if isinstance(result, Mapping) else {}
    meta_map, agent_meta = _result_metadata(payload)
    for candidate in (
        agent_meta.get("usage"),
        result_map.get("usage"),
        meta_map.get("usage"),
        payload.get("usage"),
    ):
        if isinstance(candidate, Mapping):
            return dict(candidate)
    return {}


def _extract_provider_model(payload: Any) -> tuple[str, str]:
    if not isinstance(payload, Mapping):
        return "", ""
    meta_map, agent_meta = _result_metadata(payload)
    provider = str(
        agent_meta.get("provider", meta_map.get("provider", payload.get("provider", "")))
    ).strip()
    model = str(agent_meta.get("model", meta_map.get("model", payload.get("model", "")))).strip()
    return provider, model


def _extract_openclaw_session_id(payload: Any) -> str:
    _meta, agent_meta = _result_metadata(payload)
    return str(agent_meta.get("sessionId", "")).strip()


def _diagnostic_error(diagnostic: OpenClawDiagnostic) -> AgentExecutionError:
    classification = {
        "pairing_required": "authentication_required",
        "incompatible": "configuration_error",
        "dependency_missing": "environment_failure",
        "credential_error": "auth_failure",
        "not_installed": "environment_failure",
        "config_error": "configuration_error",
        "unavailable": "network_transient",
    }.get(diagnostic.status, "environment_failure")
    return AgentExecutionError(
        f"OpenClaw runtime {diagnostic.runtime_id!r} is not ready: {diagnostic.detail or diagnostic.status}",
        classification=classification,
        persistent=classification in {"auth_failure", "authentication_required", "configuration_error"},
        health_dimension="runtime",
    )


def _execution_error(exc: BaseException) -> AgentExecutionError:
    if isinstance(exc, AgentExecutionError):
        return exc
    if isinstance(exc, OpenClawGatewayTimeout):
        return AgentExecutionError(str(exc), classification="timeout")
    if isinstance(exc, OpenClawPairingRequired):
        return AgentExecutionError(
            str(exc),
            classification="authentication_required",
            persistent=True,
            health_dimension="runtime",
        )
    if isinstance(exc, OpenClawVersionMismatch):
        return AgentExecutionError(
            str(exc),
            classification="configuration_error",
            persistent=True,
            health_dimension="runtime",
        )
    if isinstance(exc, OpenClawGatewayRequestError):
        return _gateway_request_error(exc)
    if isinstance(exc, (OpenClawGatewayDisconnected, OpenClawGatewayError, OSError)):
        return AgentExecutionError(
            str(exc),
            classification="network_transient",
            health_dimension="runtime",
        )
    return AgentExecutionError(str(exc), classification="unclassified")


def _gateway_request_error(exc: OpenClawGatewayRequestError) -> AgentExecutionError:
    """Classify an RPC failure without confusing Gateway and model placement.

    Once the ``agent`` RPC reaches a healthy Gateway, provider/model failures
    belong to the selected model route or inference target. Failures from other
    RPCs are Gateway/control-plane failures and therefore belong to the runtime.
    """

    code = (exc.error.detail_code or exc.error.code).lower()
    message = str(exc).lower()
    model_rpc = exc.method == "agent"
    if "auth" in code or "auth" in message:
        return AgentExecutionError(
            str(exc),
            classification="auth_failure",
            persistent=True,
            health_dimension="model_route" if model_rpc else "runtime",
        )
    if "rate" in code or "rate limit" in message:
        return AgentExecutionError(
            str(exc),
            classification="rate_limited",
            health_dimension="model_route" if model_rpc else "runtime",
        )
    if "quota" in code or "quota" in message:
        return AgentExecutionError(
            str(exc),
            classification="quota_exhausted",
            health_dimension="model_route" if model_rpc else "runtime",
        )
    if "model" in code or "model" in message:
        return AgentExecutionError(
            str(exc),
            classification="invalid_model",
            persistent=True,
            health_dimension="model_route" if model_rpc else "runtime",
        )
    if model_rpc and _looks_like_network_failure(f"{code} {message}"):
        return AgentExecutionError(
            str(exc),
            classification="network_transient",
            health_dimension="target",
        )
    return AgentExecutionError(
        str(exc),
        classification="unclassified",
        health_dimension="runtime" if not model_rpc else "",
    )


def _terminal_failure_dimension(classification: str) -> str:
    if classification == "network_transient":
        return "target"
    if classification in {
        "auth_failure",
        "rate_limited",
        "quota_exhausted",
        "invalid_model",
    }:
        return "model_route"
    return ""


def _looks_like_network_failure(text: str) -> bool:
    lowered = text.lower()
    return any(
        token in lowered
        for token in (
            "connection",
            "network",
            "fetch failed",
            "econn",
            "socket",
            "connection refused",
            "host unreachable",
        )
    )


def _availability_for_exception(exc: BaseException) -> Availability:
    error = _execution_error(exc)
    return {
        "auth_failure": Availability.AUTH_FAILED,
        "authentication_required": Availability.AUTH_FAILED,
        "configuration_error": Availability.DISABLED,
        "invalid_model": Availability.DISABLED,
        "quota_exhausted": Availability.QUOTA_EXHAUSTED,
        "rate_limited": Availability.RATE_LIMITED,
        "network_transient": Availability.NETWORK_TRANSIENT,
        "timeout": Availability.BUSY,
    }.get(error.classification, Availability.AVAILABLE)
