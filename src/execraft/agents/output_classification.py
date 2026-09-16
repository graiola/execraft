"""Live, provider-aware classification of agent CLI output.

The subprocess supervisor feeds bounded decoded chunks to these classifiers while
an agent is still running.  Terminal conditions such as quota exhaustion,
authentication prompts, or multi-day internal retries are detected immediately
so the whole process group can be stopped instead of waiting for the adapter's
wall-clock timeout.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, time as datetime_time, timedelta, timezone, tzinfo
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from execraft.process import ProcessTerminationSignal

_ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_DURATION_TOKEN = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>seconds?|secs?|minutes?|mins?|hours?|hrs?|days?)",
    re.IGNORECASE,
)

_ABSOLUTE_RETRY_AT = re.compile(
    r"(?:try\s+again|retry|available|resets?|reset)\s+(?:again\s+)?at\s+"
    r"(?P<timestamp>"
    r"(?:[A-Za-z]{3,9}\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\s+"
    r"\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM))"
    r"|(?:\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)"
    r")"
    r"(?:\s*\((?P<timezone>[^)]+)\))?",
    re.IGNORECASE,
)
_TIME_ONLY_RESET = re.compile(
    r"(?:resets?|reset|try\s+again)\s+(?:again\s+)?(?:at\s+)?"
    r"(?P<clock>\d{1,2}(?::\d{2}(?::\d{2})?)?\s*(?:AM|PM))"
    r"(?:\s*\((?P<timezone>[^)]+)\))?",
    re.IGNORECASE,
)
_ORDINAL_SUFFIX = re.compile(r"(?<=\d)(?:st|nd|rd|th)\b", re.IGNORECASE)


@dataclass(frozen=True)
class PatternRule:
    category: str
    pattern: re.Pattern[str]
    summary: str
    persistent: bool = True


@dataclass(frozen=True)
class FailureClassification:
    """Typed diagnosis extracted from provider-authored transport text."""

    category: str
    summary: str
    retry_after_seconds: float | None = None
    persistent: bool = True


_COMMON_RULES = (
    PatternRule(
        "session_limit",
        re.compile(
            r"(?:you(?:'|’)ve\s+)?hit\s+your\s+session\s+limit|"
            r"session\s+limit(?:\s+(?:reached|exceeded))?",
            re.IGNORECASE,
        ),
        "provider session limit reached",
    ),
    PatternRule(
        "quota_exhausted",
        re.compile(
            r"monthly usage limit reached|weekly limit(?:\s+(?:reached|exceeded))?|"
            r"usage limit reached|"
            r"(?:you(?:'|’)ve\s+)?hit\s+your\s+(?:weekly|usage)\s+limit|"
            r"quota (?:is )?exhausted|"
            r"quota exceeded|insufficient credits?|credit balance(?: is)? too low|"
            r"billing limit reached",
            re.IGNORECASE,
        ),
        "provider quota or credit limit reached",
    ),
    PatternRule(
        "authentication_required",
        re.compile(
            r"authentication required|not authenticated|please (?:log|sign) in|"
            r"login required|session expired|invalid api key|missing api key",
            re.IGNORECASE,
        ),
        "provider authentication is required",
    ),
    PatternRule(
        "permission_required",
        re.compile(
            r"permission prompt|approval required|waiting for approval|"
            r"confirm permission|requires interactive approval|"
            r"headless mode cannot prompt|permission that headless mode cannot prompt|"
            r"tool required the [\"']?[a-z0-9_.-]+[\"']? permission|auto-denied",
            re.IGNORECASE,
        ),
        "interactive permission approval is required",
    ),
    PatternRule(
        "invalid_model",
        re.compile(
            r"model .{0,120}(?:not found|does not exist|unavailable|unsupported)|"
            r"unknown model|invalid model",
            re.IGNORECASE,
        ),
        "configured model is unavailable",
    ),
    PatternRule(
        "rate_limited",
        re.compile(
            r"rate limit(?:ed)?|too many requests|http\s*429|request limit exceeded",
            re.IGNORECASE,
        ),
        "provider rate limit reached",
    ),
    PatternRule(
        "network_transient",
        re.compile(
            r"failed to lookup address information|temporary failure in name resolution|"
            r"name or service not known|network is unreachable|"
            r"stream disconnected before completion|connection reset by peer|"
            r"connection (?:timed out|closed unexpectedly)|"
            r"remote end closed connection|dns (?:lookup|resolution) failed|"
            r"temporary network (?:error|failure)|service unavailable",
            re.IGNORECASE,
        ),
        "temporary provider network failure",
    ),
)

_PROVIDER_RULES = {
    "opencode": (
        PatternRule(
            "quota_exhausted",
            re.compile(r"open.?code go.*(?:monthly|usage).*limit", re.IGNORECASE),
            "OpenCode Go monthly usage limit reached",
        ),
    ),
    "codex": (
        PatternRule(
            "permission_required",
            re.compile(r"approval mode.*interactive|waiting for user approval", re.IGNORECASE),
            "Codex is waiting for interactive approval",
        ),
    ),
    "claude-code": (
        PatternRule(
            "quota_exhausted",
            re.compile(
                r"(?:you(?:'|’)ve\s+)?hit\s+your\s+weekly\s+limit|"
                r"usage limit reached|credit balance is too low",
                re.IGNORECASE,
            ),
            "Claude usage or credit limit reached",
        ),
    ),
}

_RETRYING = re.compile(r"retrying\s+in\s+(?:about\s+|~\s*)?(?P<duration>[^\]\)\n\r,.]+)", re.IGNORECASE)
_RETRY_AFTER = re.compile(r"retry[- ]after\s*[:=]?\s*(?P<duration>[^\]\)\n\r,.]+)", re.IGNORECASE)
_RESET_IN = re.compile(r"(?:reset|resets)\s+in\s+(?P<duration>[^\]\)\n\r,.]+)", re.IGNORECASE)
_AUTO_REJECTED_PERMISSION = re.compile(
    r"permission requested:\s*(?P<permission>[a-z0-9_.-]+)"
    r"(?:\s*\((?P<target>[^)]+)\))?;\s*auto-rejecting",
    re.IGNORECASE,
)
_OPENCODE_AGENT_NOT_FOUND = re.compile(
    r"agent\s+[\"']?(?P<agent>[a-z0-9_.-]+)[\"']?\s+not found\.?"
    r"(?:\s+falling back to default agent)?",
    re.IGNORECASE,
)


class AgentOutputClassifier:
    """Incremental classifier for one configured agent instance.

    OpenCode stdout is a JSONL protocol stream.  Only structured ``error``
    events are eligible for provider-failure classification; normal ``text``
    and ``tool_use`` events may legitimately contain phrases such as
    "model unavailable" while reviewing the product's own error handling.
    Stderr remains eligible for startup, permission, quota, and transport
    diagnostics.
    """

    def __init__(
        self,
        *,
        adapter: str,
        provider_id: str,
        max_internal_retry_delay_seconds: float = 120.0,
        buffer_limit: int = 8192,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.adapter = adapter
        self.provider_id = provider_id
        self.max_internal_retry_delay_seconds = max(
            0.0, float(max_internal_retry_delay_seconds)
        )
        self.buffer_limit = max(1024, int(buffer_limit))
        self._buffers: dict[str, str] = {"stdout": "", "stderr": ""}
        self._opencode_stdout_pending = ""
        self._codex_stdout_pending = ""
        self._now_provider = now_provider or (lambda: datetime.now().astimezone())

    def __call__(self, stream: str, chunk: str) -> ProcessTerminationSignal | None:
        cleaned = _ANSI_ESCAPE.sub("", chunk).replace("\r", "\n")
        if stream == "stdout":
            if self.adapter == "opencode":
                return self._classify_opencode_stdout(cleaned)
            if self.adapter == "codex":
                return self._classify_codex_stdout(cleaned)
            if self.adapter in {"claude-code", "antigravity", "antigravity-cli"}:
                # Claude emits a JSON result object while Antigravity emits the
                # final response as plain text. In both cases stdout is
                # model-authored content and may legitimately discuss provider
                # failures in the product under review. Runtime diagnostics
                # belong on stderr; adapters validate the final payload after
                # process exit.
                return None
        return self._classify_text(stream, cleaned)

    def _classify_opencode_stdout(
        self, chunk: str
    ) -> ProcessTerminationSignal | None:
        """Classify only structured OpenCode error events from stdout."""
        self._opencode_stdout_pending += chunk
        lines = self._opencode_stdout_pending.split("\n")
        self._opencode_stdout_pending = lines.pop()
        if len(self._opencode_stdout_pending) > self.buffer_limit:
            self._opencode_stdout_pending = self._opencode_stdout_pending[
                -self.buffer_limit :
            ]

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                # OpenCode diagnostics that are not part of the JSONL protocol
                # are expected on stderr.  Ignoring raw stdout prevents product
                # code, tool output, and model prose from poisoning provider
                # health classification.
                continue
            if not isinstance(event, dict) or event.get("type") != "error":
                continue
            message = _extract_opencode_error_message(event)
            if not message:
                return ProcessTerminationSignal(
                    category="provider_error",
                    summary="OpenCode emitted a structured error event",
                    stream="stdout",
                    excerpt=_bounded_excerpt(stripped, 0, len(stripped)),
                    persistent=False,
                )
            signal = self._classify_text("stdout", message)
            if signal is not None:
                return signal
            return ProcessTerminationSignal(
                category="provider_error",
                summary=message[:240],
                stream="stdout",
                excerpt=_bounded_excerpt(message, 0, len(message)),
                persistent=False,
            )
        return None

    def _classify_codex_stdout(
        self, chunk: str
    ) -> ProcessTerminationSignal | None:
        """Classify only Codex JSONL events that represent transport errors.

        Normal ``item.completed`` agent messages and tool output are product
        content. Scanning them with provider regexes caused phrases such as
        ``invalid model`` in a code review to poison persistent provider
        health. Codex documents error-bearing events separately: top-level
        ``error``, ``turn.failed``, and ``item.completed`` with an error item.
        """
        self._codex_stdout_pending += chunk
        lines = self._codex_stdout_pending.split("\n")
        self._codex_stdout_pending = lines.pop()
        if len(self._codex_stdout_pending) > self.buffer_limit:
            self._codex_stdout_pending = self._codex_stdout_pending[-self.buffer_limit :]

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                # Codex protocol output is JSONL; raw diagnostics belong on
                # stderr. Ignore malformed stdout fragments rather than
                # classifying model/tool prose as provider health failures.
                continue
            if not isinstance(event, dict) or not _is_codex_error_event(event):
                continue
            message = _extract_codex_error_message(event)
            if not message:
                return ProcessTerminationSignal(
                    category="provider_error",
                    summary="Codex emitted a structured error event",
                    stream="stdout",
                    excerpt=_bounded_excerpt(stripped, 0, len(stripped)),
                    persistent=False,
                )
            signal = self._classify_text("stdout", message)
            if signal is not None:
                return signal
            return ProcessTerminationSignal(
                category="provider_error",
                summary=message[:240],
                stream="stdout",
                excerpt=_bounded_excerpt(message, 0, len(message)),
                persistent=False,
            )
        return None

    def _classify_text(
        self, stream: str, chunk: str
    ) -> ProcessTerminationSignal | None:
        current = self._buffers.get(stream, "")
        text = (current + chunk)[-self.buffer_limit :]
        self._buffers[stream] = text

        auto_rejected = _AUTO_REJECTED_PERMISSION.search(text)
        if auto_rejected:
            permission = auto_rejected.group("permission")
            target = (auto_rejected.group("target") or "").strip()
            detail = f" for {target}" if target else ""
            return ProcessTerminationSignal(
                category="permission_required",
                summary=(
                    f"{self.adapter} auto-rejected permission {permission}{detail}"
                ),
                stream=stream,
                excerpt=_bounded_excerpt(
                    text, auto_rejected.start(), auto_rejected.end()
                ),
                persistent=False,
            )

        if self.adapter == "opencode" and stream == "stderr":
            missing_agent = _OPENCODE_AGENT_NOT_FOUND.search(text)
            if missing_agent:
                agent = missing_agent.group("agent")
                return ProcessTerminationSignal(
                    category="invalid_configuration",
                    summary=f"configured OpenCode agent '{agent}' was not found",
                    stream=stream,
                    excerpt=_bounded_excerpt(
                        text, missing_agent.start(), missing_agent.end()
                    ),
                    persistent=False,
                )

        diagnosis = classify_provider_message(
            self.adapter,
            text,
            now=self._now_provider(),
        )
        if diagnosis is not None:
            return ProcessTerminationSignal(
                category=diagnosis.category,
                summary=diagnosis.summary,
                retry_after_seconds=diagnosis.retry_after_seconds,
                stream=stream,
                excerpt=_bounded_excerpt(text, 0, len(text)),
                persistent=diagnosis.persistent,
            )

        retry_match = _RETRYING.search(text)
        if retry_match:
            retry_after = parse_duration_seconds(retry_match.group("duration"))
            if (
                retry_after is not None
                and retry_after > self.max_internal_retry_delay_seconds
            ):
                return ProcessTerminationSignal(
                    category="internal_retry_detected",
                    summary="agent entered an internal retry longer than policy allows",
                    retry_after_seconds=retry_after,
                    stream=stream,
                    excerpt=_bounded_excerpt(
                        text, retry_match.start(), retry_match.end()
                    ),
                    persistent=False,
                )
        return None


def _is_codex_error_event(event: dict[str, Any]) -> bool:
    event_type = str(event.get("type", ""))
    if event_type in {"error", "turn.failed"}:
        return True
    if event_type != "item.completed":
        return False
    item = event.get("item")
    return isinstance(item, dict) and str(item.get("type", "")) == "error"


def _extract_codex_error_message(event: dict[str, Any]) -> str:
    candidates: list[Any] = [event.get("message"), event.get("error")]
    item = event.get("item")
    if isinstance(item, dict):
        candidates.extend([item.get("message"), item.get("error"), item.get("text")])

    while candidates:
        value = candidates.pop(0)
        if isinstance(value, dict):
            candidates.extend(
                value.get(key) for key in ("message", "error", "detail", "data")
            )
            continue
        if not isinstance(value, str) or not value.strip():
            continue
        text = value.strip()
        try:
            nested = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(nested, dict):
            candidates.insert(0, nested)
        else:
            return text
    return ""


def _extract_opencode_error_message(event: dict[str, Any]) -> str:
    error = event.get("error")
    if isinstance(error, dict):
        data = error.get("data")
        if isinstance(data, dict):
            message = data.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
        message = error.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    message = event.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()
    return ""


def classify_provider_message(
    adapter: str,
    text: str,
    *,
    now: datetime | None = None,
) -> FailureClassification | None:
    """Classify provider diagnostics without scanning model-authored payloads.

    ``now`` is injectable so absolute reset timestamps can be tested without
    depending on wall-clock time. Naive values are interpreted in the host's
    local timezone, matching how provider CLIs render reset timestamps.
    """

    rules = _PROVIDER_RULES.get(adapter, ()) + _COMMON_RULES
    for rule in rules:
        if not rule.pattern.search(text):
            continue
        return FailureClassification(
            category=rule.category,
            summary=rule.summary,
            retry_after_seconds=parse_retry_after_seconds(text, now=now),
            persistent=rule.persistent,
        )
    return None


def parse_retry_after_seconds(
    text: str,
    *,
    now: datetime | None = None,
) -> float | None:
    for pattern in (_RESET_IN, _RETRY_AFTER, _RETRYING):
        match = pattern.search(text)
        if not match:
            continue
        value = parse_duration_seconds(match.group("duration"))
        if value is not None:
            return value
    absolute = _parse_absolute_retry_deadline(text, now=now)
    if absolute is not None:
        reference = _coerce_now(now)
        return max(0.0, (absolute - reference).total_seconds())
    return None


def _parse_absolute_retry_deadline(
    text: str,
    *,
    now: datetime | None,
) -> datetime | None:
    reference = _coerce_now(now)

    absolute_match = _ABSOLUTE_RETRY_AT.search(text)
    if absolute_match:
        zone = _resolve_timezone(absolute_match.group("timezone"), reference)
        value = _parse_absolute_timestamp(absolute_match.group("timestamp"), zone)
        if value is not None:
            return value.astimezone(timezone.utc)

    time_match = _TIME_ONLY_RESET.search(text)
    if not time_match:
        return None
    zone = _resolve_timezone(time_match.group("timezone"), reference)
    clock = _parse_clock(time_match.group("clock"))
    if clock is None:
        return None
    local_now = reference.astimezone(zone)
    candidate = datetime.combine(local_now.date(), clock, tzinfo=zone)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def _coerce_now(value: datetime | None) -> datetime:
    current = value or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.replace(tzinfo=_local_timezone())
    return current


def _resolve_timezone(name: str | None, reference: datetime) -> tzinfo:
    if name:
        normalized = name.strip()
        try:
            return ZoneInfo(normalized)
        except ZoneInfoNotFoundError:
            pass
    if reference.tzinfo is not None:
        return reference.tzinfo
    local = datetime.now().astimezone().tzinfo
    if local is not None:
        return local
    return reference.astimezone().tzinfo or timezone.utc


def _local_timezone() -> tzinfo:
    return datetime.now().astimezone().tzinfo or timezone.utc


def _parse_absolute_timestamp(value: str, zone: tzinfo) -> datetime | None:
    cleaned = _ORDINAL_SUFFIX.sub("", value.strip())
    iso_candidate = cleaned.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso_candidate)
    except ValueError:
        parsed = None
    if parsed is not None:
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=zone)

    formats = (
        "%b %d, %Y %I:%M %p",
        "%b %d %Y %I:%M %p",
        "%B %d, %Y %I:%M %p",
        "%B %d %Y %I:%M %p",
        "%b %d, %Y %I:%M:%S %p",
        "%B %d, %Y %I:%M:%S %p",
    )
    for fmt in formats:
        try:
            return datetime.strptime(cleaned, fmt).replace(tzinfo=zone)
        except ValueError:
            continue
    return None


def _parse_clock(value: str) -> datetime_time | None:
    normalized = re.sub(r"(?i)(?<=\d)(am|pm)$", r" \1", value.strip())
    for fmt in ("%I %p", "%I:%M %p", "%I:%M:%S %p"):
        try:
            return datetime.strptime(normalized, fmt).time()
        except ValueError:
            continue
    return None



def parse_duration_seconds(text: str) -> float | None:
    total = 0.0
    matched = False
    for match in _DURATION_TOKEN.finditer(text):
        matched = True
        value = float(match.group("value"))
        unit = match.group("unit").lower()
        if unit.startswith("day"):
            total += timedelta(days=value).total_seconds()
        elif unit.startswith(("hour", "hr")):
            total += timedelta(hours=value).total_seconds()
        elif unit.startswith(("minute", "min")):
            total += timedelta(minutes=value).total_seconds()
        else:
            total += value
    return total if matched else None


def _bounded_excerpt(text: str, start: int, end: int, limit: int = 500) -> str:
    left = max(0, start - 120)
    right = min(len(text), end + 180)
    excerpt = " ".join(text[left:right].split())
    if len(excerpt) > limit:
        return excerpt[: limit - 1] + "…"
    return excerpt
