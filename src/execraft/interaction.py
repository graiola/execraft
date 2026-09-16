"""Provider-neutral live interaction descriptions.

Provider protocols expose rich but inconsistent payloads.  This module turns
those payloads into compact, durable activity records that answer the operator's
most useful questions: what is the agent doing, on which target, and with which
command/query.  Raw provider metadata is retained separately for diagnostics.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_MAX_SUMMARY_CHARS = 2_000
_MAX_TARGET_CHARS = 1_000
_MAX_COMMAND_CHARS = 4_000
_JSON_FIELD_PATTERN = re.compile(
    r'"(?P<key>file_path|path|command|cmd|query|pattern|description|prompt|url|glob|old_string|new_string)"\s*:\s*"(?P<value>(?:\\.|[^"\\])*)"'
)


@dataclass(frozen=True)
class ActivityDescription:
    """Human-oriented summary for one provider tool or work event."""

    title: str
    summary: str = ""
    category: str = "tool"
    operation: str = ""
    target: str = ""
    command: str = ""

    def as_mapping(self) -> dict[str, str]:
        return {
            "title": self.title,
            "summary": self.summary,
            "category": self.category,
            "operation": self.operation,
            "target": self.target,
            "command": self.command,
        }


def normalize_interaction_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a bounded interaction payload enriched for presentation.

    The function is intentionally provider-agnostic.  Controllers may emit only
    a tool name plus raw metadata; the console store still persists a readable
    operation, target and summary.  Explicit controller fields always win.
    """

    normalized = dict(payload)
    kind = _text(normalized.get("kind", "status")).lower() or "status"
    title = _text(normalized.get("title", ""))
    text = _text(normalized.get("text", ""))
    data = normalized.get("data")
    data_map = dict(data) if isinstance(data, Mapping) else {}

    if kind in {"tool", "tool_input_delta", "tool_output", "tool_progress"}:
        tool_input = extract_tool_input(data_map)
        if kind == "tool_input_delta" and text:
            partial = parse_partial_json_fields(text)
            tool_input = {**tool_input, **partial}
        raw_tool_name = _tool_name(data_map) or title
        description = describe_tool_activity(raw_tool_name, tool_input)
    elif kind == "file_change":
        description = describe_file_change(data_map)
    elif kind == "plan":
        description = ActivityDescription(
            title=title or "Update plan",
            summary=text or _plan_summary(data_map),
            category="plan",
            operation="plan",
        )
    elif kind == "diff":
        description = ActivityDescription(
            title=title or "Update working diff",
            summary=_diff_summary(text),
            category="change",
            operation="diff",
        )
    elif kind in {"assistant", "assistant_delta"}:
        description = ActivityDescription(
            title=title or "Agent message",
            summary=_first_line(text),
            category="message",
            operation="explain",
        )
    elif kind in {"reasoning", "reasoning_delta"}:
        description = ActivityDescription(
            title=title or "Reasoning summary",
            summary=_first_line(text),
            category="reasoning",
            operation="reason",
        )
    elif kind in {"error", "approval", "question", "status", "session", "operator", "operator_ack"}:
        description = ActivityDescription(
            title=title or kind.replace("_", " ").title(),
            summary=_first_line(text),
            category=kind,
            operation=kind,
        )
    else:
        description = ActivityDescription(
            title=title or kind.replace("_", " ").title(),
            summary=_first_line(text),
            category=kind,
            operation=kind,
        )

    explicit = {
        "summary": _text(normalized.get("summary", "")),
        "category": _text(normalized.get("category", "")),
        "operation": _text(normalized.get("operation", "")),
        "target": _text(normalized.get("target", "")),
        "command": _text(normalized.get("command", "")),
    }
    visible_title = title
    if kind in {"tool", "tool_input_delta", "tool_output", "tool_progress"} and (
        not title or _raw_tool_title(title, raw_tool_name)
    ):
        visible_title = description.title
    normalized["title"] = _bounded(visible_title or description.title, _MAX_SUMMARY_CHARS)
    normalized["summary"] = _bounded(explicit["summary"] or description.summary, _MAX_SUMMARY_CHARS)
    normalized["category"] = _bounded(explicit["category"] or description.category, 128)
    normalized["operation"] = _bounded(explicit["operation"] or description.operation, 128)
    normalized["target"] = _bounded(explicit["target"] or description.target, _MAX_TARGET_CHARS)
    normalized["command"] = _bounded(explicit["command"] or description.command, _MAX_COMMAND_CHARS)
    normalized["parent_item_id"] = _bounded(
        _text(
            normalized.get("parent_item_id")
            or data_map.get("parent_tool_use_id")
            or data_map.get("parentToolUseId")
        ),
        512,
    )
    normalized["data"] = data_map
    return normalized


def extract_tool_input(data: Mapping[str, Any]) -> dict[str, Any]:
    """Extract the most likely tool input object from provider metadata."""

    candidates: list[Any] = [
        data.get("input"),
        data.get("arguments"),
        data.get("args"),
        data.get("parameters"),
    ]
    part = data.get("part")
    if isinstance(part, Mapping):
        part_state = part.get("state")
        if isinstance(part_state, Mapping):
            candidates.extend(
                [part_state.get("input"), part_state.get("arguments")]
            )
        candidates.extend(
            [part.get("input"), part.get("arguments"), part.get("args"), part_state]
        )
    state = data.get("state")
    if isinstance(state, Mapping):
        candidates.extend([state.get("input"), state.get("arguments")])
    for candidate in candidates:
        if isinstance(candidate, Mapping):
            return dict(candidate)
    # Some protocols place tool fields directly on the item.
    direct_keys = {
        "file_path", "filePath", "path", "command", "cmd", "query", "pattern", "glob",
        "description", "prompt", "url", "cwd", "patch", "old_string", "new_string",
    }
    return {key: data[key] for key in direct_keys if key in data}


def parse_partial_json_fields(value: str) -> dict[str, str]:
    """Recover useful string fields from an incomplete JSON object."""

    text = str(value or "")
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        parsed = None
    if isinstance(parsed, Mapping):
        return {str(key): item for key, item in parsed.items()}
    recovered: dict[str, str] = {}
    for match in _JSON_FIELD_PATTERN.finditer(text):
        raw = match.group("value")
        try:
            recovered[match.group("key")] = json.loads(f'"{raw}"')
        except json.JSONDecodeError:
            recovered[match.group("key")] = raw
    return recovered


def describe_tool_activity(name: str, tool_input: Mapping[str, Any] | None) -> ActivityDescription:
    """Describe a tool invocation without exposing an unreadable JSON blob."""

    raw_name = _text(name) or "Tool"
    lowered = raw_name.lower().replace("_", "").replace("-", "")
    values = dict(tool_input or {})
    path = _path_value(values)
    command = _command_value(values)
    query = _first_value(values, "query", "pattern", "glob", "search", "needle")
    description = _first_value(values, "description", "prompt", "task", "instruction")
    url = _first_value(values, "url", "uri")

    if command or lowered in {"bash", "shell", "terminal", "command", "commandexecution", "exec"}:
        operation, title = _command_operation(command)
        target = _command_target(command)
        summary = command or description
        if values.get("cwd"):
            summary = f"{summary}  (cwd: {values['cwd']})" if summary else f"cwd: {values['cwd']}"
        return ActivityDescription(
            title=title,
            summary=_bounded(summary, _MAX_SUMMARY_CHARS),
            category="command",
            operation=operation,
            target=_bounded(target, _MAX_TARGET_CHARS),
            command=_bounded(command, _MAX_COMMAND_CHARS),
        )

    if lowered in {"read", "readfile", "cat", "view", "open", "fetch"}:
        return _targeted("Inspect file", "read", "read", path or url, description)
    if lowered in {"glob", "find", "list", "ls", "tree"}:
        return _targeted("Find files", "search", "find_files", query or path, description)
    if lowered in {"grep", "ripgrep", "rg", "search", "codesearch"}:
        target = query
        if path and query:
            target = f"{query} in {path}"
        return _targeted("Search code", "search", "search_code", target or path, description)
    if lowered in {"write", "writefile", "create", "createfile"}:
        return _targeted("Write file", "change", "write", path, description)
    if lowered in {"edit", "multiedit", "replace", "applypatch", "patch", "strreplace"}:
        return _targeted("Edit file", "change", "edit", path, description)
    if lowered in {"websearch", "searchweb", "google", "browsersearch"}:
        return _targeted("Search the web", "research", "web_search", query, description)
    if lowered in {"webfetch", "fetchurl", "browseropen"}:
        return _targeted("Open web resource", "research", "web_fetch", url, description)
    if lowered in {"task", "agent", "subagent", "delegate", "dispatch"}:
        agent = _first_value(values, "subagent_type", "agent", "agent_type", "name")
        target = agent or description
        return _targeted("Delegate work", "delegate", "delegate", target, description)
    if lowered in {"todo", "todowrite", "updateplan", "plan"}:
        return _targeted("Update plan", "plan", "plan", description, description)
    if lowered in {"notebookedit", "editnotebook"}:
        return _targeted("Edit notebook", "change", "edit_notebook", path, description)

    target = path or query or url or description
    summary = _compact_mapping(values) or target
    return ActivityDescription(
        title=_humanize(raw_name),
        summary=_bounded(summary, _MAX_SUMMARY_CHARS),
        category="tool",
        operation="tool",
        target=_bounded(target, _MAX_TARGET_CHARS),
    )


def describe_file_change(data: Mapping[str, Any]) -> ActivityDescription:
    changes = data.get("changes") if isinstance(data.get("changes"), Sequence) else []
    paths: list[str] = []
    for change in changes:
        if not isinstance(change, Mapping):
            continue
        value = _first_value(change, "path", "file_path", "file", "filename")
        if value:
            paths.append(value)
    unique_paths = list(dict.fromkeys(paths))
    if len(unique_paths) == 1:
        summary = unique_paths[0]
    elif unique_paths:
        summary = f"{len(unique_paths)} files: " + ", ".join(unique_paths[:4])
    else:
        summary = "Working tree changed"
    return ActivityDescription(
        title="Apply file changes",
        summary=_bounded(summary, _MAX_SUMMARY_CHARS),
        category="change",
        operation="file_change",
        target=_bounded(unique_paths[0] if len(unique_paths) == 1 else "", _MAX_TARGET_CHARS),
    )


def _targeted(title: str, category: str, operation: str, target: str, description: str) -> ActivityDescription:
    summary = target or description
    return ActivityDescription(
        title=title,
        summary=_bounded(summary, _MAX_SUMMARY_CHARS),
        category=category,
        operation=operation,
        target=_bounded(target, _MAX_TARGET_CHARS),
    )


def _command_operation(command: str) -> tuple[str, str]:
    lowered = command.strip().lower()
    if re.search(r"(^|[;&|]\s*)(pytest|python\s+-m\s+pytest|npm\s+(run\s+)?test|pnpm\s+test|yarn\s+test|cargo\s+test|go\s+test|ctest|mvn\s+test|gradle\s+test)", lowered):
        return "test", "Run tests"
    if re.search(r"(^|[;&|]\s*)(ruff|flake8|pylint|mypy|eslint|prettier|black|cargo\s+(fmt|clippy))", lowered):
        return "validate", "Run quality checks"
    if re.search(r"(^|[;&|]\s*)(git\s+(status|diff|log|show|branch)|gh\s+(pr|issue)\s+(view|list|checks))", lowered):
        return "inspect_repository", "Inspect repository"
    if re.search(r"(^|[;&|]\s*)(rg|grep|find)\b", lowered):
        return "search_code", "Search code"
    if re.search(r"(^|[;&|]\s*)(cat|sed|head|tail|less)\b", lowered):
        return "read", "Inspect file"
    if re.search(r"(^|[;&|]\s*)(git\s+(add|commit|checkout|switch|merge|rebase|push)|gh\s+pr\s+create)", lowered):
        return "repository_change", "Update repository"
    if re.search(r"(^|[;&|]\s*)(python|node|npm|pnpm|yarn|cargo|go|make|cmake|docker)\b", lowered):
        return "run", "Run development command"
    return "command", "Run command"


def _command_target(command: str) -> str:
    if not command:
        return ""
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = command.split()
    for token in reversed(tokens):
        if token.startswith("-") or token in {"&&", "||", "|", ";"}:
            continue
        if "/" in token or Path(token).suffix:
            return token
    return ""


def _tool_name(data: Mapping[str, Any]) -> str:
    part = data.get("part")
    if isinstance(part, Mapping):
        return _text(part.get("tool") or part.get("name"))
    return _text(data.get("tool") or data.get("name") or data.get("type"))


def _raw_tool_title(title: str, tool_name: str) -> bool:
    """Return whether a provider title is merely an unhelpful tool label."""

    canonical_title = re.sub(r"[^a-z0-9]", "", title.lower())
    canonical_tool = re.sub(r"[^a-z0-9]", "", tool_name.lower())
    return bool(
        canonical_title
        and (
            canonical_title == canonical_tool
            or canonical_title in {
                "tool", "command", "commandexecution", "shell", "bash",
                "read", "write", "edit", "grep", "glob", "task",
            }
        )
    )


def _path_value(values: Mapping[str, Any]) -> str:
    return _first_value(
        values,
        "file_path",
        "filePath",
        "path",
        "filename",
        "file",
        "notebook_path",
    )


def _command_value(values: Mapping[str, Any]) -> str:
    value = values.get("command", values.get("cmd", ""))
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return " ".join(_text(part) for part in value)
    return _text(value)


def _first_value(values: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = values.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            if value.strip():
                return value.strip()
        elif isinstance(value, (int, float, bool)):
            return str(value)
    return ""


def _compact_mapping(values: Mapping[str, Any]) -> str:
    visible: list[str] = []
    for key, value in values.items():
        if value in (None, "", [], {}):
            continue
        if isinstance(value, (Mapping, list, tuple)):
            rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        else:
            rendered = str(value)
        visible.append(f"{key}={rendered}")
        if len(visible) >= 4:
            break
    return " · ".join(visible)


def _plan_summary(data: Mapping[str, Any]) -> str:
    steps = data.get("steps") if isinstance(data.get("steps"), Sequence) else []
    active: list[str] = []
    for step in steps:
        if not isinstance(step, Mapping):
            continue
        status = _text(step.get("status") or step.get("state")).lower()
        if status in {"in_progress", "in progress", "running", "active"}:
            active.append(_text(step.get("step") or step.get("text") or step.get("description")))
    if active:
        return "Current: " + "; ".join(item for item in active if item)
    return f"{len(steps)} planned step{'s' if len(steps) != 1 else ''}" if steps else ""


def _diff_summary(value: str) -> str:
    if not value:
        return "Working diff updated"
    additions = 0
    deletions = 0
    files: set[str] = set()
    for line in value.splitlines():
        if line.startswith("+++ b/") or line.startswith("--- a/"):
            files.add(line[6:])
        elif line.startswith("+") and not line.startswith("+++"):
            additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1
    parts = []
    if files:
        parts.append(f"{len(files)} file{'s' if len(files) != 1 else ''}")
    if additions or deletions:
        parts.append(f"+{additions}/-{deletions}")
    return " · ".join(parts) or "Working diff updated"


def _first_line(value: str) -> str:
    for line in str(value or "").splitlines():
        if line.strip():
            return _bounded(line.strip(), _MAX_SUMMARY_CHARS)
    return ""


def _humanize(value: str) -> str:
    spaced = re.sub(r"(?<!^)(?=[A-Z])", " ", value.replace("_", " ").replace("-", " "))
    return " ".join(spaced.split()).capitalize() or "Tool"


def _bounded(value: Any, limit: int) -> str:
    text = _text(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _text(value: Any) -> str:
    return value if isinstance(value, str) else "" if value is None else str(value)
