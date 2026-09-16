"""Tests for PLAN document normalizer and work-package graph builder."""

import pytest

from execraft.orchestrate.models import (
    AcceptanceCriterion,
    WorkPackage,
    WorkPackageKind,
    WorkPackageStage,
)
from execraft.orchestrate.normalizer import (
    normalize_work_packages,
    parse_plan_document,
    plan_graph_from_mapping,
)


class TestParsePlanDocument:
    def test_empty_text(self):
        graph, report = parse_plan_document("")
        assert report.packages_found == 0
        assert not report.has_errors()

    def test_single_package(self):
        text = """## Work package: Initial setup

### Requirements
- Set up project structure
- Configure CI

### Acceptance criteria
- [ ] CI pipeline passes
- [ ] Project structure is valid

Dependencies: none
Risk: low
Priority: 1
"""
        graph, report = parse_plan_document(text)
        assert report.packages_found == 1
        assert not report.has_errors()
        wp = graph.work_packages[0]
        assert wp.id == "initial_setup"
        assert wp.title == "Initial setup"
        assert len(wp.requirements) == 2
        assert len(wp.acceptance_criteria) == 2

    def test_multiple_packages_with_dependencies(self):
        text = """## Package: Foundation

### Requirements
- Core library

### Acceptance criteria
- [ ] Library compiles

---

## Package: Features

Dependencies: Foundation

### Requirements
- Feature implementation

### Acceptance criteria
- [ ] Features work
"""
        graph, report = parse_plan_document(text)
        assert report.packages_found == 2
        assert not report.has_errors()
        wp2 = graph.package_by_id("features")
        assert "foundation" in wp2.dependencies
        assert graph.dependency_ready("foundation") is True
        assert graph.dependency_ready("features") is False

    def test_detects_missing_dependencies(self):
        text = """## Package: Dependent

Dependencies: nonexistent_package

### Acceptance criteria
- [ ] Works
"""
        graph, report = parse_plan_document(text)
        assert len(report.missing_dependencies) > 0

    def test_detects_missing_acceptance_criteria(self):
        text = """## Package: No criteria

### Requirements
- Do something
"""
        graph, report = parse_plan_document(text)
        assert len(report.missing_acceptance_criteria) > 0
        # Previously collected but never made has_errors() true — a plan
        # missing acceptance criteria was silently accepted as valid.
        assert report.has_errors() is True

    def test_missing_dependency_makes_has_errors_true(self):
        text = """## Package: Dependent

Dependencies: nonexistent_package

### Acceptance criteria
- [ ] Works
"""
        graph, report = parse_plan_document(text)
        assert report.has_errors() is True

    def test_detects_duplicate_work_package_ids(self):
        text = """## Package: Setup

### Acceptance criteria
- [ ] First

## Package: Setup

### Acceptance criteria
- [ ] Second (accidentally the same title/ID as the first)
"""
        graph, report = parse_plan_document(text)
        assert report.packages_found == 2
        assert len(report.duplicate_ids) == 1
        assert "setup" in report.duplicate_ids[0]
        assert report.has_errors() is True

    def test_no_duplicate_ids_for_distinct_titles(self):
        text = """## Package: Setup A

### Acceptance criteria
- [ ] First

## Package: Setup B

### Acceptance criteria
- [ ] Second
"""
        graph, report = parse_plan_document(text)
        assert report.duplicate_ids == []
        assert report.has_errors() is False

    def test_parse_risk_and_priority(self):
        text = """## Step: Critical component

Risk: high
Priority: 10

### Acceptance criteria
- [ ] Works
"""
        graph, report = parse_plan_document(text)
        assert report.packages_found == 1
        wp = graph.work_packages[0]
        assert wp.risk == "high"
        assert wp.priority == 10

    def test_parse_affected_repositories(self):
        text = """## Work package: UI changes

Affected repositories: repo-a, repo-b

### Acceptance criteria
- [ ] UI renders
"""
        graph, report = parse_plan_document(text)
        assert len(graph.work_packages[0].affected_repositories) == 2
        assert "repo-a" in graph.work_packages[0].affected_repositories

    def test_legacy_milestone_header_format(self):
        text = """### Milestone: Release v2

### Acceptance criteria
- [ ] All tests pass
"""
        graph, report = parse_plan_document(text)
        assert report.packages_found == 1
        assert "release_v2" in [wp.id for wp in graph.work_packages]

    def test_acceptance_criteria_checkbox_formats(self):
        text = """## Package: Format test

### Acceptance criteria
- [ ] Unchecked
- [x] Checked
- [X] Also checked
- [ ]  With extra pipe | detail
"""
        graph, report = parse_plan_document(text)
        assert report.packages_found == 1
        wp = graph.work_packages[0]
        assert len(wp.acceptance_criteria) == 4

    def test_mixed_content_ignores_non_structural_lines(self):
        text = """## Work package: Core

Some explanatory text that should be ignored.

### Acceptance criteria
- [ ] Criterion one

This text should also be ignored.
"""
        graph, report = parse_plan_document(text)
        assert report.packages_found == 1
        assert len(graph.work_packages[0].acceptance_criteria) == 1

    def test_complex_plan_with_multiple_packages(self):
        text = """
# Project Plan

## Milestone: Phase 1 - Foundation

### Requirements
- Set up build system
- Create core types

### Acceptance criteria
- [ ] Build completes
- [ ] Core types compile

Risk: low
Priority: 1

## Milestone: Phase 2 - Implementation

Dependencies: phase_1___foundation

### Requirements
- Implement features

### Acceptance criteria
- [ ] Features pass tests
- [ ] Documentation written

Risk: medium
Priority: 2
---
## Milestone: Phase 3 - Polish

Dependencies: phase_2___implementation

### Acceptance criteria
- [ ] Performance meets targets

Risk: medium
Priority: 3
"""
        graph, report = parse_plan_document(text)
        assert report.packages_found == 3
        # Verify no cycles
        graph.validate_acyclic()
        # Check dependency chain
        assert graph.dependency_ready("phase_1___foundation") is True
        assert graph.dependency_ready("phase_2___implementation") is False

    def test_cycle_detection_in_plan(self):
        text = """## Package: A

Dependencies: C

### Acceptance criteria
- [ ] Works

## Package: B

Dependencies: A

### Acceptance criteria
- [ ] Works

## Package: C

Dependencies: B

### Acceptance criteria
- [ ] Works
"""
        graph, report = parse_plan_document(text)
        assert len(report.cycles_detected) > 0

    def test_id_derivation_from_title(self):
        text = """## Work package: Complex Feature Name With CAPS & Special Chars!

### Acceptance criteria
- [ ] Works
"""
        graph, report = parse_plan_document(text)
        wp = graph.work_packages[0]
        assert wp.id == "complex_feature_name_with_caps__special_chars"
        assert len(wp.id) <= 64


class TestNormalizeWorkPackages:
    def test_normalize_empty(self):
        graph, report = normalize_work_packages([])
        assert report.packages_found == 0

    def test_normalize_simple_list(self):
        packages = [
            WorkPackage(id="wp-1", title="First", acceptance_criteria=[AcceptanceCriterion(id="ac1", description="Must work")]),
        ]
        graph, report = normalize_work_packages(packages)
        assert report.packages_found == 1
        assert not report.has_errors()

    def test_normalize_missing_dependency(self):
        packages = [
            WorkPackage(id="wp-1", title="First", dependencies=["wp-nonexistent"]),
        ]
        graph, report = normalize_work_packages(packages)
        assert len(report.missing_dependencies) > 0

    def test_normalize_detects_duplicate_ids(self):
        packages = [
            WorkPackage(
                id="wp-1", title="First",
                acceptance_criteria=[AcceptanceCriterion(id="ac1", description="Must work")],
            ),
            WorkPackage(
                id="wp-1", title="Accidentally same ID",
                acceptance_criteria=[AcceptanceCriterion(id="ac2", description="Also works")],
            ),
        ]
        graph, report = normalize_work_packages(packages)
        assert len(report.duplicate_ids) == 1
        assert report.has_errors() is True


def test_load_plan_graph_file_reads_yaml_execution_contract(tmp_path):
    from execraft.orchestrate import load_plan_graph_file

    plan_path = tmp_path / "PLAN.graph.yaml"
    plan_path.write_text(
        """work_packages:
  - id: M00
    title: Bootstrap
    acceptance_criteria:
      - id: ac-bootstrap
        description: Bootstrap completes
  - id: WP01
    title: Implement
    dependencies: [M00]
    acceptance_criteria:
      - id: ac-implement
        description: Implementation completes
""",
        encoding="utf-8",
    )

    graph, report = load_plan_graph_file(plan_path)

    assert not report.has_errors(), report.summary()
    assert [package.id for package in graph.work_packages] == ["M00", "WP01"]
    assert all(package.kind is WorkPackageKind.DEVELOPMENT for package in graph.work_packages)
    assert all(package.stage == WorkPackageStage.PREPARE for package in graph.work_packages)
    assert graph.package_by_id("WP01").dependencies == ["M00"]
    assert {package.id for package in graph.ready_packages()} == {"M00"}


def test_parse_explicit_complexity():
    text = """## Package: Difficult migration

Risk: high
Complexity: 92

### Acceptance criteria
- [ ] Migration is complete
"""
    graph, report = parse_plan_document(text)

    assert not report.has_errors()
    assert graph.work_packages[0].complexity == 92
    assert graph.work_packages[0].complexity_score() == 92


def test_invalid_explicit_complexity_is_reported():
    text = """## Package: Invalid complexity

Complexity: 140

### Acceptance criteria
- [ ] Complete
"""
    with pytest.raises(Exception, match="complexity"):
        parse_plan_document(text)


def test_plan_mapping_rejects_invalid_execution_policy_shape():
    graph, report = plan_graph_from_mapping(
        {
            "work_packages": [
                {
                    "id": "WP1",
                    "title": "Milestone",
                    "acceptance_criteria": [
                        {"id": "AC1", "description": "Works"}
                    ],
                    "agent_preferences": {"review": "not-a-list"},
                }
            ]
        }
    )

    assert graph.work_packages == []
    assert report.has_errors()
    assert "must be a list" in report.errors[0]
