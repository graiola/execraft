from pathlib import Path

import pytest

from execraft.orchestrate.execution_policy import (
    ExecutionPolicyError,
    effective_skill_ids,
    execution_role_metadata,
    normalize_role_mapping,
)
from execraft.orchestrate.scheduler import StructuredHandoff, build_agent_prompt
from execraft.skills import (
    MAX_SELECTED_SKILL_BYTES,
    MAX_SKILL_INSTRUCTION_BYTES,
    SkillCatalog,
    SkillCatalogError,
)


def test_builtin_skill_catalog_exposes_role_compatible_workflows():
    catalog = SkillCatalog.load()

    assert catalog.definition("ai-implement").supports_role("implement")
    assert not catalog.definition("ai-review").supports_role("implement")
    assert [item.id for item in catalog.for_role("final_review")] == ["ai-review"]
    assert [item.id for item in catalog.for_role("supervise")] == ["ai-supervise"]


def test_builtin_supervisor_skill_materializes_as_a_bounded_incident_protocol():
    catalog = SkillCatalog.load()

    skill = catalog.materialize("supervise", ["ai-supervise"])[0]

    assert skill.id == "ai-supervise"
    assert "incident commander" in skill.instructions
    assert "Never commit, push, switch branches" in skill.instructions
    assert "retain_paths" in skill.instructions
    assert "discard_paths" in skill.instructions


def test_builtin_review_skill_declares_the_canonical_cross_field_contract():
    skill = SkillCatalog.load().materialize("final_review", ["ai-review"])[0]

    assert '"verdict": "approved"' in skill.instructions
    assert '"findings": []' in skill.instructions
    assert '"observations": []' in skill.instructions
    assert '"summary":' in skill.instructions
    assert "exactly `approved` or `changes_required`" in skill.instructions
    assert "A changes-required review" in skill.instructions
    assert "non-empty blocking finding" in skill.instructions
    assert "Always return `observations`" in skill.instructions
    assert "Do not emit a second" in skill.instructions
    assert "model-level" in skill.instructions
    assert "`ok`" in skill.instructions
    assert "`rejected`" not in skill.instructions


def test_project_skill_overlays_builtin_and_legacy_unscoped_skill_is_global(tmp_path: Path):
    skill_dir = tmp_path / "skills" / "ai-implement"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: ai-implement\n"
        "description: Project implementation policy\n"
        "overrides: builtin\n"
        "roles:\n"
        "  - implement\n"
        "---\n\n"
        "Use the project-specific implementation sequence.\n",
        encoding="utf-8",
    )
    global_dir = tmp_path / "skills" / "domain-check"
    global_dir.mkdir(parents=True)
    (global_dir / "SKILL.md").write_text(
        "---\n"
        "name: domain-check\n"
        "description: Domain-specific validation\n"
        "---\n\n"
        "Validate domain assumptions.\n",
        encoding="utf-8",
    )

    catalog = SkillCatalog.load(project_skills_dir=tmp_path / "skills")

    assert catalog.definition("ai-implement").source == "project"
    assert "project-specific" in catalog.definition("ai-implement").instructions
    assert catalog.definition("domain-check").supports_role("review")


def test_skill_catalog_rejects_wrong_role_and_name_mismatch(tmp_path: Path):
    catalog = SkillCatalog.load()
    with pytest.raises(SkillCatalogError, match="does not support role"):
        catalog.validate_selection("implement", ["ai-review"])

    skill_dir = tmp_path / "skills" / "wrong-directory"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: another-name\ndescription: Invalid\n---\nBody\n",
        encoding="utf-8",
    )
    with pytest.raises(SkillCatalogError, match="directory/name mismatch"):
        SkillCatalog.load(project_skills_dir=tmp_path / "skills")


def test_execution_role_metadata_and_default_skill_resolution_are_canonical():
    roles = execution_role_metadata()
    assert [item["id"] for item in roles] == [
        "decompose",
        "implement",
        "review",
        "fix_review",
        "final_review",
    ]
    assert effective_skill_ids("implement", []) == ["ai-implement"]
    assert effective_skill_ids("review", ["ai-review"]) == ["ai-review"]
    assert normalize_role_mapping(
        {"review": ["qwen", "qwen", "codex"]}, label="agents"
    ) == {"review": ["qwen", "codex"]}


def test_build_agent_prompt_embeds_selected_skill_instructions_before_task_contract():
    catalog = SkillCatalog.load()
    skill = catalog.materialize("implement", ["ai-implement"])[0]
    prompt = build_agent_prompt(
        StructuredHandoff(
            work_package_id="WP1",
            stage="implement",
            summary="Implement the package",
            workflow_skills=[skill.as_mapping()],
        )
    )

    assert "workflow skill: ai-implement" in prompt
    assert "Implement one approved work package" in prompt
    assert "Run `execraft task status" in prompt
    assert prompt.index("workflow skill: ai-implement") < prompt.index("Task: Implement")


def _write_project_skill(
    root: Path,
    skill_id: str,
    *,
    roles: list[str],
    instructions: str,
) -> None:
    directory = root / skill_id
    directory.mkdir(parents=True)
    role_lines = "".join(f"  - {role}\n" for role in roles)
    (directory / "SKILL.md").write_text(
        "---\n"
        f"name: {skill_id}\n"
        f"description: {skill_id} description\n"
        "roles:\n"
        f"{role_lines}"
        "---\n\n"
        f"{instructions}\n",
        encoding="utf-8",
    )


def test_skill_catalog_rejects_unknown_roles_and_oversized_instructions(tmp_path: Path):
    _write_project_skill(
        tmp_path / "unknown" / "skills",
        "bad-role",
        roles=["implemnt"],
        instructions="Do the work.",
    )
    with pytest.raises(SkillCatalogError, match="unsupported workflow role"):
        SkillCatalog.load(project_skills_dir=tmp_path / "unknown" / "skills")

    _write_project_skill(
        tmp_path / "large" / "skills",
        "large-skill",
        roles=["implement"],
        instructions="x" * (MAX_SKILL_INSTRUCTION_BYTES + 1),
    )
    with pytest.raises(SkillCatalogError, match="maximum is"):
        SkillCatalog.load(project_skills_dir=tmp_path / "large" / "skills")


def test_materialized_skill_selection_has_a_bounded_total_size(tmp_path: Path):
    skill_root = tmp_path / "skills"
    for index in range(3):
        _write_project_skill(
            skill_root,
            f"large-{index}",
            roles=["review"],
            instructions=str(index) * (MAX_SELECTED_SKILL_BYTES // 2),
        )
    catalog = SkillCatalog.load(project_skills_dir=skill_root)

    with pytest.raises(SkillCatalogError, match="selected workflow skills"):
        catalog.materialize("review", ["large-0", "large-1", "large-2"])


def test_skill_catalog_rejects_symlinked_project_skill_directory(tmp_path: Path):
    external_root = tmp_path / "external"
    _write_project_skill(
        external_root,
        "escaped-skill",
        roles=["review"],
        instructions="Do not escape the configured catalog root.",
    )
    project_root = tmp_path / "project-skills"
    project_root.mkdir()
    (project_root / "escaped-skill").symlink_to(
        external_root / "escaped-skill",
        target_is_directory=True,
    )

    with pytest.raises(SkillCatalogError, match="must not be symlinks"):
        SkillCatalog.load(project_skills_dir=project_root)


def test_skill_catalog_rejects_symlinked_skill_file(tmp_path: Path):
    project_root = tmp_path / "project-skills"
    skill_dir = project_root / "linked-file"
    skill_dir.mkdir(parents=True)
    external_file = tmp_path / "external-SKILL.md"
    external_file.write_text(
        "---\n"
        "name: linked-file\n"
        "description: Linked file\n"
        "roles:\n"
        "  - review\n"
        "---\n\n"
        "Do not follow the linked file.\n",
        encoding="utf-8",
    )
    (skill_dir / "SKILL.md").symlink_to(external_file)

    with pytest.raises(SkillCatalogError, match="skill files must not be symlinks"):
        SkillCatalog.load(project_skills_dir=project_root)


def test_role_mapping_rejects_non_list_ranked_values():
    for value in (42, {"codex": True}, {"codex"}):
        with pytest.raises(ExecutionPolicyError, match="must be a list"):
            normalize_role_mapping({"review": value}, label="agents")


def test_skill_catalog_rejects_invalid_skill_ids(tmp_path: Path):
    _write_project_skill(
        tmp_path / "skills",
        "invalid skill",
        roles=["review"],
        instructions="Invalid ID.",
    )
    with pytest.raises(SkillCatalogError, match="invalid workflow skill ID"):
        SkillCatalog.load(project_skills_dir=tmp_path / "skills")


def test_skill_catalog_rejects_symlinked_project_catalog_root(tmp_path: Path):
    real_root = tmp_path / "real-skills"
    _write_project_skill(
        real_root,
        "domain-review",
        roles=["review"],
        instructions="Review the domain.",
    )
    linked_root = tmp_path / "linked-skills"
    linked_root.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(SkillCatalogError, match="catalog root must not be a symlink"):
        SkillCatalog.load(project_skills_dir=linked_root)


def test_skill_materialization_records_version_hash_and_size(tmp_path: Path):
    _write_project_skill(
        tmp_path / "skills",
        "domain-audit",
        roles=["review"],
        instructions="Inspect domain invariants.",
    )
    skill = SkillCatalog.load(project_skills_dir=tmp_path / "skills").materialize(
        "review", ["domain-audit"]
    )[0]

    assert skill.version == "1"
    assert len(skill.content_hash) == 64
    assert skill.instruction_bytes == len(skill.instructions.encode("utf-8"))
    assert skill.as_mapping()["selection_reason"] == "role policy"


def test_project_skill_override_must_be_explicit(tmp_path: Path):
    skill_dir = tmp_path / "skills" / "ai-review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: ai-review\n"
        "description: Accidental collision\n"
        "roles:\n"
        "  - review\n"
        "---\n\n"
        "Review differently.\n",
        encoding="utf-8",
    )

    with pytest.raises(SkillCatalogError, match="overrides: builtin"):
        SkillCatalog.load(project_skills_dir=tmp_path / "skills")
