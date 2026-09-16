"""Permissive Supervisor decision compilation.

Supervisor reasoning is deliberately less constrained than ordinary package-agent
output.  The model may return JSON, JSON embedded in prose, or a compact plain-text
decision.  This module converts that semantic decision into the strict internal
``SupervisorDecision`` object consumed by the orchestrator.

Only execution effects remain fail-closed: protected paths, scope acquisition,
resume-stage safety, branch/commit ownership, and human approval are enforced by
the orchestrator after compilation.  Formatting mistakes in advisory metadata
must never discard an otherwise usable incident diagnosis or poison provider
health.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable, Mapping

from .structured_output import extract_structured_object
from .supervisor import (
    HumanDecisionRequest,
    IncidentClass,
    SupervisorDecision,
    SupervisorDecisionKind,
)

_DECISION_ALIASES = {
    "resolved": SupervisorDecisionKind.RESOLVED,
    "resolve": SupervisorDecisionKind.RESOLVED,
    "continue": SupervisorDecisionKind.RESOLVED,
    "recover": SupervisorDecisionKind.RESOLVED,
    "recovered": SupervisorDecisionKind.RESOLVED,
    "recover_and_continue": SupervisorDecisionKind.RESOLVED,
    "repair": SupervisorDecisionKind.RESOLVED,
    "repair_and_continue": SupervisorDecisionKind.RESOLVED,
    "accept_with_exception": SupervisorDecisionKind.RESOLVED,
    "override_and_continue": SupervisorDecisionKind.RESOLVED,
    "delegate": SupervisorDecisionKind.DELEGATE,
    "delegation": SupervisorDecisionKind.DELEGATE,
    "ask_human": SupervisorDecisionKind.ASK_HUMAN,
    "ask_operator": SupervisorDecisionKind.ASK_HUMAN,
    "human_required": SupervisorDecisionKind.ASK_HUMAN,
    "operator_decision": SupervisorDecisionKind.ASK_HUMAN,
    "blocked": SupervisorDecisionKind.BLOCKED,
    "block": SupervisorDecisionKind.BLOCKED,
    "pause": SupervisorDecisionKind.BLOCKED,
    "stop": SupervisorDecisionKind.BLOCKED,
}

_HIGH_IMPACT_DECISIONS = {
    "cancel",
    "cancel_package",
    "replan",
    "rollback",
    "rollback_completed_work",
    "rewrite_history",
    "delete",
    "discard_intentional_work",
    "custom",
}

_DECISION_LINE = re.compile(r"^\s*(?:decision|verdict|action)\s*:\s*(.+?)\s*$", re.I)
_CLASSIFICATION_LINE = re.compile(r"^\s*classification\s*:\s*(.+?)\s*$", re.I)
_RESUME_LINE = re.compile(r"^\s*resume(?:_stage| stage)?\s*:\s*(.+?)\s*$", re.I)
_SUMMARY_LINE = re.compile(r"^\s*(?:summary|rationale|reason)\s*:\s*(.+?)\s*$", re.I)
_BULLET = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+(.+?)\s*$")


@dataclass(frozen=True)
class SupervisorDecisionCompilation:
    """One compiled decision plus non-fatal normalization diagnostics."""

    decision: SupervisorDecision
    warnings: tuple[str, ...] = ()
    source: str = "mapping"


def compile_supervisor_decision(
    result: Mapping[str, Any],
    *,
    candidate_paths: Iterable[str] = (),
    require_human_for_high_impact: bool = True,
) -> SupervisorDecisionCompilation:
    """Compile a provider result into the strict internal decision model.

    The compiler tries explicit mappings first, then JSON embedded in the final
    message, then a small plain-text envelope.  Optional metadata is normalized
    or dropped with a warning.  A semantic decision and summary are the only
    irreducible requirements.
    """

    candidates = tuple(dict.fromkeys(str(item).strip() for item in candidate_paths if str(item).strip()))
    mappings: list[tuple[str, Mapping[str, Any]]] = []

    structured = result.get("_execraft_structured_payload")
    if isinstance(structured, Mapping):
        mappings.append(("structured", structured))

    public = {
        str(key): value
        for key, value in result.items()
        if not str(key).startswith("_execraft_")
    }
    if any(key in public for key in ("decision", "summary", "classification", "instructions")):
        mappings.append(("mapping", public))

    texts: list[str] = []
    final_message = result.get("final_message")
    if isinstance(final_message, str) and final_message.strip():
        texts.append(final_message.strip())
    raw_candidates = result.get("structured_output_candidates")
    if isinstance(raw_candidates, list):
        texts.extend(
            str(item).strip()
            for item in reversed(raw_candidates)
            if isinstance(item, str) and item.strip()
        )

    for text in dict.fromkeys(texts):
        parsed = extract_structured_object(
            text,
            preferred_keys=(
                "decision",
                "summary",
                "classification",
                "instructions",
                "actions_taken",
                "human_question",
                "resume_stage",
            ),
        )
        if isinstance(parsed, Mapping):
            mappings.append(("embedded_json", parsed))

    errors: list[str] = []
    for source, mapping in mappings:
        try:
            normalized, warnings = _normalize_mapping(
                mapping,
                candidate_paths=candidates,
                require_human_for_high_impact=require_human_for_high_impact,
            )
            return SupervisorDecisionCompilation(
                SupervisorDecision.from_mapping(normalized),
                tuple(warnings),
                source,
            )
        except ValueError as exc:
            errors.append(f"{source}: {exc}")

    for text in dict.fromkeys(texts):
        try:
            mapping = _parse_freeform_decision(text)
            normalized, warnings = _normalize_mapping(
                mapping,
                candidate_paths=candidates,
                require_human_for_high_impact=require_human_for_high_impact,
            )
            return SupervisorDecisionCompilation(
                SupervisorDecision.from_mapping(normalized),
                tuple([*warnings, "compiled Supervisor decision from free-form text"]),
                "freeform",
            )
        except ValueError as exc:
            errors.append(f"freeform: {exc}")

    detail = "; ".join(errors[-4:]) or "no decision payload or final message was returned"
    raise ValueError(f"Supervisor result has no usable semantic decision: {detail}")


def _normalize_mapping(
    raw: Mapping[str, Any],
    *,
    candidate_paths: tuple[str, ...],
    require_human_for_high_impact: bool,
) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    data = dict(raw)

    nested = data.get("supervisor_decision")
    if isinstance(nested, Mapping):
        data = {**data, **dict(nested)}

    if data.get("ok") is False:
        raise ValueError("Supervisor explicitly reported ok=false")
    data["ok"] = True

    decision_text = _scalar_text(data.get("decision") or data.get("verdict") or data.get("action"))
    if not decision_text:
        decision_text = _infer_decision_text(data)
    token = _decision_token(decision_text)

    if token in _HIGH_IMPACT_DECISIONS:
        if require_human_for_high_impact:
            data["decision"] = SupervisorDecisionKind.ASK_HUMAN.value
            data["human_question"] = _high_impact_question(data, token)
            warnings.append(
                f"high-impact Supervisor decision {token!r} converted to an audited operator approval"
            )
        else:
            data["decision"] = SupervisorDecisionKind.BLOCKED.value
            warnings.append(
                f"high-impact Supervisor decision {token!r} preserved as blocked because no executable state mutation exists"
            )
    else:
        canonical = _DECISION_ALIASES.get(token)
        if canonical is None:
            raise ValueError(f"unrecognized Supervisor decision {decision_text!r}")
        data["decision"] = canonical.value

    classification_text = _scalar_text(data.get("classification"))
    try:
        classification = IncidentClass(classification_text) if classification_text else IncidentClass.UNKNOWN
    except ValueError:
        classification = IncidentClass.UNKNOWN
        warnings.append(f"unknown incident classification {classification_text!r} normalized to 'unknown'")
    data["classification"] = classification.value

    instructions = _string_list(
        data.get("actions_taken")
        if data.get("actions_taken") is not None
        else data.get("instructions")
        if data.get("instructions") is not None
        else data.get("steps")
        if data.get("steps") is not None
        else data.get("actions")
    )
    data["actions_taken"] = instructions[:32]

    summary = _scalar_text(data.get("summary") or data.get("rationale") or data.get("reason"))
    if not summary:
        summary = instructions[0] if instructions else decision_text
    if not summary:
        raise ValueError("Supervisor decision requires a semantic summary")
    data["summary"] = summary[:12000]

    resume = _scalar_text(data.get("resume_stage") or data.get("resume"))
    if resume:
        normalized_resume = resume.strip().lower().replace("-", "_").replace(" ", "_")
        allowed = {
            "prepare",
            "decompose",
            "implement",
            "regression_verify",
            "ready_to_commit",
        }
        if normalized_resume in allowed:
            data["resume_stage"] = normalized_resume
        else:
            data["resume_stage"] = ""
            warnings.append(f"unsupported advisory resume stage {resume!r} ignored")
    else:
        data["resume_stage"] = ""

    evidence, evidence_warnings = _normalize_evidence(data.get("acceptance_evidence"))
    data["acceptance_evidence"] = evidence
    warnings.extend(evidence_warnings)
    data["implementation_summary"] = _scalar_text(data.get("implementation_summary"))[:12000]

    retain, retain_warnings = _normalize_paths(
        data.get("retain_paths"), candidate_paths=candidate_paths, label="retain_paths"
    )
    discard, discard_warnings = _normalize_paths(
        data.get("discard_paths"), candidate_paths=candidate_paths, label="discard_paths"
    )
    warnings.extend(retain_warnings)
    warnings.extend(discard_warnings)
    overlap = set(retain) & set(discard)
    if overlap:
        discard = [item for item in discard if item not in overlap]
        warnings.append(
            "paths listed in both retain_paths and discard_paths were treated as retained: "
            + ", ".join(sorted(overlap))
        )

    if data["decision"] == SupervisorDecisionKind.RESOLVED.value and candidate_paths:
        classified = set(retain) | set(discard)
        inferred = [item for item in candidate_paths if item not in classified]
        if inferred:
            retain.extend(inferred)
            warnings.append(
                "resolved decision omitted workspace classifications; remaining incident candidates were inferred as retained"
            )
    if data["decision"] != SupervisorDecisionKind.RESOLVED.value and (retain or discard):
        # Path classifications are only executable alongside a `resolved`
        # decision. Advisory hints attached to ask_human/blocked/delegate used
        # to survive normalization whenever the incident candidate set was
        # empty -- typically because scope cleanup had already removed the
        # artifacts -- and then tripped the strict boundary in
        # SupervisorDecision.from_mapping. That ValueError is indistinguishable
        # from a genuine parse failure here, so an otherwise valid decision was
        # silently downgraded to a free-form `blocked`. Drop them instead.
        warnings.append(
            f"retain_paths/discard_paths ignored because decision {data['decision']!r} is not 'resolved'"
        )
        retain = []
        discard = []
    data["retain_paths"] = retain
    data["discard_paths"] = discard

    delegations = data.get("delegations")
    if delegations is None:
        delegations = data.get("delegate") if isinstance(data.get("delegate"), list) else []
    data["delegations"] = delegations if isinstance(delegations, list) else []

    if data["decision"] == SupervisorDecisionKind.ASK_HUMAN.value:
        data["human_question"] = _normalize_human_question(
            data.get("human_question"), summary=data["summary"]
        )
    else:
        data["human_question"] = None

    # Unknown/advisory fields intentionally disappear at this compiler boundary.
    return data, warnings


def _normalize_evidence(raw: Any) -> tuple[list[dict[str, str]], list[str]]:
    warnings: list[str] = []
    if raw is None:
        return [], warnings
    if isinstance(raw, Mapping):
        items = [(key, value) for key, value in raw.items()]
    elif isinstance(raw, list):
        items = []
        for item in raw:
            if not isinstance(item, Mapping):
                warnings.append("non-object acceptance_evidence entry ignored")
                continue
            items.append((item.get("criterion_id") or item.get("id"), item.get("evidence") or item.get("value")))
    else:
        return [], ["non-object acceptance_evidence ignored"]

    merged: dict[str, str] = {}
    for raw_id, raw_value in items[:256]:
        criterion_id = _scalar_text(raw_id)[:256]
        evidence = _scalar_text(raw_value)[:8000]
        if not criterion_id or not evidence:
            warnings.append("incomplete acceptance_evidence entry ignored")
            continue
        previous = merged.get(criterion_id)
        if previous is None:
            merged[criterion_id] = evidence
        elif evidence != previous:
            merged[criterion_id] = (previous + "\n\nAdditional evidence: " + evidence)[:8000]
            warnings.append(f"duplicate acceptance criterion {criterion_id!r} merged losslessly")
        else:
            warnings.append(f"duplicate acceptance criterion {criterion_id!r} deduplicated")
    return [
        {"criterion_id": key, "evidence": value}
        for key, value in list(merged.items())[:128]
    ], warnings


def _normalize_paths(
    raw: Any,
    *,
    candidate_paths: tuple[str, ...],
    label: str,
) -> tuple[list[str], list[str]]:
    warnings: list[str] = []
    values = _string_list(raw)
    candidates = set(candidate_paths)
    aliases = {item.replace(":", "/", 1): item for item in candidate_paths if ":" in item}
    normalized: list[str] = []

    for value in values[:512]:
        canonical = value
        if ":" not in canonical:
            canonical = aliases.get(canonical, "")
        if not canonical or ":" not in canonical:
            warnings.append(f"{label} entry {value!r} ignored because it is not repository-qualified")
            continue
        repository_id, relative_path = canonical.split(":", 1)
        if not repository_id or not relative_path or relative_path.startswith("/"):
            warnings.append(f"invalid {label} entry {value!r} ignored")
            continue
        if candidates and canonical not in candidates:
            warnings.append(f"{label} entry {value!r} ignored because it is outside the incident candidate set")
            continue
        if canonical not in normalized:
            normalized.append(canonical)
    return normalized, warnings


def _normalize_human_question(raw: Any, *, summary: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        return _default_approval_question(summary, action="proposed recovery")

    question = _scalar_text(raw.get("question")) or "How should the Supervisor proceed?"
    context = _scalar_text(raw.get("context")) or summary
    options_raw = raw.get("options")
    options: list[dict[str, Any]] = []
    if isinstance(options_raw, list):
        for index, item in enumerate(options_raw[:6], 1):
            if not isinstance(item, Mapping):
                continue
            label = _scalar_text(item.get("label") or item.get("name") or item.get("id"))
            if not label:
                continue
            option_id = _safe_option_id(_scalar_text(item.get("id")) or f"option_{index}")
            if option_id == "stop":
                option_id = "do_not_proceed"
            option = {
                "id": option_id,
                "label": label[:300],
                "consequence": _scalar_text(item.get("consequence") or item.get("description"))[:1200],
                "risk": _normalize_risk(item.get("risk")),
            }
            weight = item.get("weight")
            if isinstance(weight, int) and not isinstance(weight, bool) and 0 <= weight <= 100:
                option["weight"] = weight
            options.append(option)
    if len(options) < 2:
        return _default_approval_question(context, action=question)

    recommended = _safe_option_id(_scalar_text(raw.get("recommended_option")))
    ids = {item["id"] for item in options}
    if recommended not in ids:
        recommended = ""

    supplied_weights = [item.get("weight") for item in options]
    weights_valid = all(isinstance(value, int) for value in supplied_weights) and sum(supplied_weights) == 100
    if weights_valid and recommended:
        highest = max(supplied_weights)
        recommended_weight = next(item.get("weight") for item in options if item["id"] == recommended)
        weights_valid = recommended_weight == highest
    if not weights_valid:
        for item in options:
            item.pop("weight", None)

    # Preserve a fully valid model weighting so the existing fail-closed
    # auto-decision policy can operate. Partial or inconsistent arithmetic is
    # stripped, which forces the question to remain under a human decision Hold.
    return {
        "question": question[:2000],
        "context": context[:8000],
        "options": options,
        "recommended_option": recommended,
    }


def _high_impact_question(data: Mapping[str, Any], token: str) -> Mapping[str, Any]:
    summary = _scalar_text(data.get("summary") or data.get("reason") or data.get("rationale")) or token
    return _default_approval_question(summary, action=token.replace("_", " "))


def _default_approval_question(context: str, *, action: str) -> Mapping[str, Any]:
    return {
        "question": f"The Supervisor proposes to {action}. Should Execraft allow a fresh supervised recovery round with this direction?",
        "context": context[:8000],
        "recommended_option": "",
        "options": [
            {
                "id": "approve_direction",
                "label": "Approve this recovery direction",
                "consequence": "The decision is recorded and supplied to a fresh bounded Supervisor round; normal orchestrator safety checks still apply.",
                "risk": "unknown",
            },
            {
                "id": "keep_paused",
                "label": "Keep the task paused",
                "consequence": "No additional Supervisor recovery action is authorized.",
                "risk": "routine",
            },
        ],
    }


def _parse_freeform_decision(text: str) -> dict[str, Any]:
    decision = ""
    classification = ""
    resume_stage = ""
    summary_parts: list[str] = []
    bullets: list[str] = []
    for line in text.splitlines():
        if match := _DECISION_LINE.match(line):
            decision = match.group(1).strip()
            continue
        if match := _CLASSIFICATION_LINE.match(line):
            classification = match.group(1).strip()
            continue
        if match := _RESUME_LINE.match(line):
            resume_stage = match.group(1).strip()
            continue
        if match := _SUMMARY_LINE.match(line):
            summary_parts.append(match.group(1).strip())
            continue
        if match := _BULLET.match(line):
            bullets.append(match.group(1).strip())

    if not decision:
        decision = _infer_decision_from_text(text)
    if not decision:
        raise ValueError("plain-text Supervisor response has no recognizable decision")
    summary = " ".join(summary_parts).strip()
    if not summary:
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
        summary = paragraphs[0] if paragraphs else text.strip()
    return {
        "decision": decision,
        "classification": classification,
        "summary": summary[:12000],
        "instructions": bullets,
        "resume_stage": resume_stage,
    }


def _infer_decision_text(data: Mapping[str, Any]) -> str:
    combined = " ".join(
        _string_list(data.get("instructions"))
        + [_scalar_text(data.get("summary")), _scalar_text(data.get("reason"))]
    )
    return _infer_decision_from_text(combined)


def _infer_decision_from_text(text: str) -> str:
    lowered = text.casefold()
    if any(token in lowered for token in ("ask the operator", "ask human", "operator decision", "human decision")):
        return "ask_human"
    if "delegate" in lowered:
        return "delegate"
    if any(token in lowered for token in ("blocked", "cannot safely proceed", "remain paused")):
        return "blocked"
    if any(token in lowered for token in ("recover", "resolved", "resume", "continue", "repair")):
        return "resolved"
    return ""


def _decision_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


def _safe_option_id(value: str) -> str:
    token = re.sub(r"[^a-zA-Z0-9_-]+", "_", value.strip()).strip("_").lower()
    if not token:
        return ""
    if token[0].isdigit():
        token = "option_" + token
    return token[:64]


def _normalize_risk(value: Any) -> str:
    token = _decision_token(_scalar_text(value))
    return token if token in {"routine", "destructive", "product", "external", "unknown"} else "unknown"


def _scalar_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value).strip()
    return ""


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        lines = [match.group(1).strip() for line in value.splitlines() if (match := _BULLET.match(line))]
        return lines or ([value.strip()] if value.strip() else [])
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for item in value:
        text = _scalar_text(item)
        if text and text not in result:
            result.append(text)
    return result
