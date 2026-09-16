"""Tests for provider-neutral structured agent output extraction."""

import json
from pathlib import Path

import pytest

from execraft.orchestrate.scheduler import AgentExecutionError, enforce_semantic_output_limit
from execraft.orchestrate.structured_output import (
    StructuredOutputError,
    acceptance_evidence_output_schema,
    extract_structured_object,
    normalize_structured_result,
    review_output_schema,
)


FIXTURES = Path(__file__).parent / "fixtures" / "structured_reviews"


def test_semantic_output_limit_rejects_complete_structure_without_truncation() -> None:
    payload = {"ok": True, "summary": "evidence " * 200}
    original = dict(payload)

    with pytest.raises(AgentExecutionError) as raised:
        enforce_semantic_output_limit(payload, 20)

    assert raised.value.classification == "output_budget_exceeded"
    assert "hard_limit=20" in str(raised.value)
    assert payload == original


def test_semantic_output_target_is_not_a_hard_limit() -> None:
    payload = {"ok": True, "summary": "compact"}

    assert enforce_semantic_output_limit(payload, 100) < 100


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_extracts_exact_json_object() -> None:
    payload = extract_structured_object(
        '{"ok": true, "verdict": "approved", "findings": [], "summary": "clean"}'
    )
    assert payload == {
        "ok": True,
        "verdict": "approved",
        "findings": [],
        "summary": "clean",
    }


def test_extracts_json_from_markdown_fence() -> None:
    payload = extract_structured_object(
        """Review complete.
```json
{"ok": true, "verdict": "changes_required", "findings": ["Fix routing"], "summary": "one issue"}
```""",
        preferred_keys=("verdict", "findings"),
    )
    assert payload is not None
    assert payload["verdict"] == "changes_required"
    assert payload["findings"] == ["Fix routing"]


def test_prefers_outer_contract_over_nested_evidence_object() -> None:
    payload = extract_structured_object(
        "Result: "
        '{"ok": true, "status": "implemented", "summary": "done", '
        '"acceptance_evidence": {"criterion": "verified"}}',
        preferred_keys=("status", "acceptance_evidence"),
    )
    assert payload is not None
    assert payload["status"] == "implemented"
    assert payload["acceptance_evidence"] == {"criterion": "verified"}


def test_implementation_contract_accepts_legacy_evidence_map_as_strict_entries() -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["ok", "status", "summary", "acceptance_evidence"],
        "properties": {
            "ok": {"type": "boolean", "const": True},
            "status": {"type": "string", "enum": ["implemented"]},
            "summary": {"type": "string", "minLength": 1},
            "acceptance_evidence": acceptance_evidence_output_schema(),
        },
    }

    payload = normalize_structured_result(
        {
            "ok": True,
            "status": "implemented",
            "summary": "done",
            "acceptance_evidence": {"criterion": "verified"},
        },
        schema,
    )

    assert payload["acceptance_evidence"] == [
        {"criterion_id": "criterion", "evidence": "verified"}
    ]


def test_unwraps_conventional_review_envelope() -> None:
    payload = extract_structured_object(
        '{"review": {"ok": true, "verdict": "approved", '
        '"findings": [], "summary": "clean"}}',
        preferred_keys=("verdict", "findings"),
    )
    assert payload is not None
    assert payload["verdict"] == "approved"
    assert payload["summary"] == "clean"


def test_rejects_text_without_json_object() -> None:
    assert extract_structured_object("Everything looks good.") is None


def test_validates_review_contract_subset() -> None:
    from execraft.orchestrate.structured_output import validate_structured_object

    schema = {
        "type": "object",
        "required": ["ok", "verdict", "findings", "summary"],
        "properties": {
            "ok": {"const": True},
            "verdict": {"enum": ["approved", "changes_required"]},
            "findings": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
            },
            "summary": {"type": "string", "minLength": 1},
        },
    }

    assert validate_structured_object(
        {
            "ok": True,
            "verdict": "approved",
            "findings": [],
            "summary": "clean",
        },
        schema,
    ) == []


def test_validation_reports_empty_or_malformed_contract() -> None:
    from execraft.orchestrate.structured_output import validate_structured_object

    schema = {
        "type": "object",
        "required": ["ok", "verdict", "summary"],
        "properties": {
            "ok": {"const": True},
            "verdict": {"enum": ["approved", "changes_required"]},
            "summary": {"type": "string", "minLength": 1},
        },
    }

    empty_errors = validate_structured_object(None, schema)
    assert empty_errors == ["$: expected object, got null"]

    errors = validate_structured_object(
        {"ok": False, "verdict": "maybe", "summary": ""}, schema
    )
    assert "$.ok: expected constant True" in errors
    assert "$.verdict: value 'maybe' is not one of ['approved', 'changes_required']" in errors
    assert "$.summary: string shorter than minLength=1" in errors


def test_validates_additional_property_values() -> None:
    from execraft.orchestrate.structured_output import validate_structured_object

    schema = {
        "type": "object",
        "additionalProperties": {"type": "string", "minLength": 1},
    }

    assert validate_structured_object({"wp10": "verified"}, schema) == []
    assert validate_structured_object({"wp10": ""}, schema) == [
        "$.wp10: string shorter than minLength=1"
    ]


def test_validates_array_cardinality_uniqueness_and_string_maximum() -> None:
    from execraft.orchestrate.structured_output import validate_structured_object

    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["ids", "summary"],
        "properties": {
            "ids": {
                "type": "array",
                "minItems": 2,
                "maxItems": 2,
                "uniqueItems": True,
                "items": {"type": "string"},
            },
            "summary": {"type": "string", "maxLength": 4},
        },
    }

    assert validate_structured_object(
        {"ids": ["a"], "summary": "longer"}, schema
    ) == [
        "$.ids: array shorter than minItems=2",
        "$.summary: string longer than maxLength=4",
    ]
    assert validate_structured_object(
        {"ids": ["a", "a", "b"], "summary": "ok"}, schema
    ) == [
        "$.ids: array longer than maxItems=2",
        "$.ids: array items are not unique",
    ]


def test_review_schema_is_closed_strict_contract_without_transport_ok() -> None:
    schema = review_output_schema()

    assert schema["additionalProperties"] is False
    assert schema["required"] == [
        "verdict",
        "findings",
        "observations",
        "summary",
    ]
    assert "ok" not in schema["properties"]


def test_observed_qwen_30b_prose_is_rejected_for_retry() -> None:
    result = _fixture("qwen30_prose.json")

    with pytest.raises(StructuredOutputError, match="missing required property"):
        normalize_structured_result(result, review_output_schema())


def test_observed_qwen_30b_approved_with_findings_is_rejected() -> None:
    result = _fixture("qwen30_verdict_object.json")

    with pytest.raises(
        StructuredOutputError,
        match="approved contradicts non-empty blocking findings",
    ):
        normalize_structured_result(result, review_output_schema())


def test_observed_qwen_9b_findions_alias_preserves_blocking_findings() -> None:
    result = _fixture("qwen9_findings.json")

    payload = normalize_structured_result(result, review_output_schema())

    assert payload["verdict"] == "changes_required"
    assert payload["findings"] == [
        "F-001 | HIGH | Transport validation is missing.",
        "F-002 | HIGH | Route comparison is case-sensitive.",
    ]
    assert "findions" not in payload
    assert "ok" not in payload
    assert payload["summary"] == "Transport requirements remain incomplete."


def test_observed_qwen_fixer_missing_one_required_id_retries() -> None:
    result = _fixture("qwen_fix_missing_finding_id.json")
    expected_ids = [f"RF-{index:03d}" for index in range(1, 8)]
    schema = {
        "type": "object",
        "additionalProperties": True,
        "required": ["status", "resolved_finding_ids"],
        "properties": {
            "status": {"enum": ["fixed"]},
            "resolved_finding_ids": {
                "type": "array",
                "items": {"enum": expected_ids},
                "minItems": len(expected_ids),
                "maxItems": len(expected_ids),
                "uniqueItems": True,
            },
        },
    }

    with pytest.raises(StructuredOutputError, match="minItems=7"):
        normalize_structured_result(result, schema)


def test_changes_required_without_findings_fails_semantic_validation() -> None:
    result = {
        "ok": True,
        "final_message": json.dumps(
            {
                "verdict": "changes_required",
                "findings": [],
                "summary": "There are blockers, but none were reported.",
            }
        ),
    }

    with pytest.raises(
        StructuredOutputError,
        match="changes_required requires at least one",
    ):
        normalize_structured_result(result, review_output_schema())


def test_approved_findings_are_rejected_as_contradictory() -> None:
    result = {
        "ok": True,
        "final_message": json.dumps(
            {
                "verdict": "approved",
                "findings": ["Consider documenting the optional path"],
                "summary": "Approved with a note",
            }
        ),
    }

    with pytest.raises(
        StructuredOutputError,
        match="approved contradicts non-empty blocking findings",
    ):
        normalize_structured_result(result, review_output_schema())


def test_known_wrapper_returns_only_nested_contract() -> None:
    result = {
        "ok": True,
        "final_message": json.dumps(
            {
                "response": {
                    "verdict": "approved",
                    "findings": [],
                    "observations": [],
                    "summary": "Clean",
                },
                "debug": "provider metadata must not enter the contract",
            }
        ),
    }

    payload = normalize_structured_result(result, review_output_schema())

    assert payload == {
        "verdict": "approved",
        "findings": [],
        "observations": [],
        "summary": "Clean",
    }


def test_missing_observations_becomes_required_empty_array() -> None:
    result = {
        "ok": True,
        "final_message": json.dumps(
            {
                "ok": True,
                "verdict": "approved",
                "findings": [],
                "summary": "Clean",
            }
        ),
    }

    payload = normalize_structured_result(result, review_output_schema())

    assert payload == {
        "verdict": "approved",
        "findings": [],
        "observations": [],
        "summary": "Clean",
    }


def test_closed_review_contract_rejects_unknown_model_fields() -> None:
    result = {
        "ok": True,
        "final_message": json.dumps(
            {
                "verdict": "approved",
                "findings": [],
                "observations": [],
                "summary": "Clean",
                "provider_note": "not part of the contract",
            }
        ),
    }

    with pytest.raises(StructuredOutputError, match="unexpected property"):
        normalize_structured_result(result, review_output_schema())


def test_conflicting_canonical_and_alias_findings_are_not_repaired() -> None:
    result = {
        "ok": True,
        "final_message": json.dumps(
            {
                "verdict": "changes_required",
                "findings": ["canonical"],
                "findions": ["different"],
                "summary": "conflict",
            }
        ),
    }

    with pytest.raises(StructuredOutputError, match="contradictory review findings"):
        normalize_structured_result(result, review_output_schema())
