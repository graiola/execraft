from execraft.orchestrate.scheduler import StructuredHandoff, build_agent_prompt


def test_prompt_keeps_skills_and_schema_in_stable_prefix() -> None:
    common = dict(
        work_package_id="WP10",
        stage="review",
        summary="Review package",
        workflow_skills=[
            {
                "id": "ai-review",
                "version": "1",
                "content_hash": "abc",
                "instructions": "Review deterministically.",
            }
        ],
        expected_output_schema={"type": "object", "required": ["verdict"]},
    )
    first = build_agent_prompt(
        StructuredHandoff(**common, handoff_id="one", attempt=1)
    )
    second = build_agent_prompt(
        StructuredHandoff(**common, handoff_id="two", attempt=2)
    )

    first_prefix = first.split("Handoff ID:", 1)[0]
    second_prefix = second.split("Handoff ID:", 1)[0]
    assert first_prefix == second_prefix
    assert "Review deterministically." in first_prefix
    assert '"required": ["verdict"]' in first_prefix
