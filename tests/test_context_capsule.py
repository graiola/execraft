from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from execraft.orchestrate.context import AgentContextAssembler
from execraft.orchestrate.context_budget import estimate_tokens
from execraft.orchestrate.context_capsule import (
    ContextCapsuleError,
    PackageContextCapsuleStore,
)
from execraft.orchestrate.invocations import AgentInvocationStore
from execraft.orchestrate.journal import EventJournal
from execraft.orchestrate.models import AcceptanceCriterion, WorkPackage
from execraft.orchestrate.scheduler import AgentCapability, StructuredHandoff, build_agent_prompt


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=path, check=True)
    return path


def _package() -> WorkPackage:
    return WorkPackage(
        id="WP10",
        title="Runtime-neutral execution",
        requirements=["Implement the scope documented in PLAN.md for WP10."],
        acceptance_criteria=[
            AcceptanceCriterion(id="AC-1", description="Core is runtime-neutral")
        ],
        affected_repositories=["core"],
        read_scope=["core:src/**"],
        write_scope=["core:src/**"],
    )


def test_capsule_extracts_only_matching_plan_section_and_reuses_digest(tmp_path: Path) -> None:
    dossier = tmp_path / "dossier"
    dossier.mkdir()
    (dossier / "PLAN.md").write_text(
        "# Plan\n\n"
        "## WP09 — Old\n\n" + "irrelevant history\n" * 2000 + "\n"
        "## WP10 — Runtime-neutral execution\n\n"
        "### Objective\n\nKeep execution runtime-neutral.\n\n"
        "### Requirements\n\n- Remove concrete imports.\n\n"
        "## WP11 — Later\n\nDo something else.\n",
        encoding="utf-8",
    )
    (dossier / "BRIEF.md").write_text("locked decisions\n" * 1000, encoding="utf-8")
    (dossier / "DECISIONS.yaml").write_text(
        "decisions:\n"
        "  - id: runtime-ownership\n"
        "    title: Runtime ownership\n"
        "    summary: The runtime boundary owns execution.\n"
        "    status: active\n"
        "    packages: [WP10]\n",
        encoding="utf-8",
    )
    store = PackageContextCapsuleStore(
        dossier_dir=dossier,
        output_dir=tmp_path / "state" / "context",
        invocations=AgentInvocationStore(tmp_path / "state" / "invocations.sqlite3"),
        project_id="task",
    )

    first, path = store.generate(_package())
    second, second_path = store.generate(_package())

    assert path == second_path
    assert first.capsule_sha256 == second.capsule_sha256
    assert "WP10" in first.plan_section
    assert "Keep execution runtime-neutral" in first.plan_section
    assert "irrelevant history" not in first.plan_section
    assert "WP11" not in first.plan_section
    assert first.decisions[0]["id"] == "runtime-ownership"
    assert first.legacy_fallback is True
    assert store.validate(path).capsule_sha256 == first.capsule_sha256


def test_capsule_uses_shard_specific_safe_paths(tmp_path: Path) -> None:
    package = _package()
    package.shard_key = "api/contracts"
    store = PackageContextCapsuleStore(
        dossier_dir=None,
        output_dir=tmp_path / "context",
        invocations=AgentInvocationStore(tmp_path / "invocations.sqlite3"),
        project_id="task",
    )

    _, path = store.generate(package)

    assert path.parent == (tmp_path / "context").resolve()
    assert path.name == "WP10--api-contracts.json"
    package.id = "../escape"
    safe_path = store.path_for(package)
    assert safe_path.parent == (tmp_path / "context").resolve()
    assert ".." not in safe_path.name


def test_capsule_digest_detects_tampering(tmp_path: Path) -> None:
    store = PackageContextCapsuleStore(
        dossier_dir=None,
        output_dir=tmp_path / "context",
        invocations=AgentInvocationStore(tmp_path / "invocations.sqlite3"),
        project_id="task",
    )
    _, path = store.generate(_package())
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["objective"] = "tampered"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ContextCapsuleError, match="digest mismatch"):
        store.validate(path)


def test_package_context_reduces_large_dossier_prompt_by_more_than_eighty_percent(
    tmp_path: Path,
) -> None:
    dossier = tmp_path / "dossier"
    dossier.mkdir()
    relevant = (
        "## WP10 — Runtime-neutral execution\n\n"
        "### Objective\n\nKeep execution runtime-neutral.\n\n"
        "### Requirements\n\n- Remove concrete runtime imports.\n"
    )
    (dossier / "PLAN.md").write_text(
        "# Plan\n\n" + ("historical plan material\n" * 12000) + relevant + "\n## WP11\nLater\n",
        encoding="utf-8",
    )
    (dossier / "BRIEF.md").write_text("brief history\n" * 8000, encoding="utf-8")
    (dossier / "HANDOFF.md").write_text("handoff history\n" * 16000, encoding="utf-8")
    (dossier / "REVIEW.md").write_text("review history\n" * 4000, encoding="utf-8")
    repo = _git_repo(tmp_path / "repo")
    assembler = AgentContextAssembler(
        project_id="task",
        repository_paths={"core": repo},
        journal=EventJournal(tmp_path / "journal.json"),
        invocations=AgentInvocationStore(tmp_path / "invocations.sqlite3"),
        dossier_dir=dossier,
        context_dir=tmp_path / "context",
    )
    handoff = StructuredHandoff(
        work_package_id="WP10",
        stage="implement",
        summary="Implement runtime-neutral execution",
        requirements=list(_package().requirements),
        acceptance_criteria=[{"id": "AC-1", "description": "Core is runtime-neutral"}],
    )

    enriched = assembler.enrich(handoff, _package(), AgentCapability.IMPLEMENT)
    new_tokens = estimate_tokens(build_agent_prompt(enriched))
    legacy_tokens = estimate_tokens(
        "\n".join(path.read_text(encoding="utf-8") for path in sorted(dossier.glob("*.md")))
    )

    assert new_tokens < legacy_tokens * 0.20
    assert "HANDOFF.md" not in enriched.bounded_excerpts
    assert "REVIEW.md" not in enriched.bounded_excerpts
    assert enriched.budget_report["rendered_prompt_estimated_tokens"] == new_tokens
    assert enriched.output_token_target == 1500
    assert enriched.output_token_budget == 1500
    assert enriched.output_token_hard_limit == 4000
    assert "output target of 1500 tokens" in build_agent_prompt(enriched)
