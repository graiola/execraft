"""Automatic decomposition and safe parallel-shard scheduling tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from threading import Barrier

import pytest

from execraft.orchestrate.models import (
    AcceptanceCriterion,
    PlanGraph,
    TaskExecutionState,
    WorkPackage,
    WorkPackageStage,
)
from execraft.orchestrate.normalizer import NormalizationReport
from execraft.orchestrate.orchestrator import OrchestrationConfig, ProjectOrchestrator
from execraft.orchestrate.scheduler import AgentCapability, Availability, StructuredHandoff
from execraft.orchestrate.sharding import (
    DecompositionPolicy,
    ParallelShardCandidate,
    ParallelShardPolicy,
    ShardPlanError,
    choose_parallel_candidates,
    validate_decomposition_payload,
)


def _parent(*, stage: WorkPackageStage = WorkPackageStage.IMPLEMENT) -> WorkPackage:
    return WorkPackage(
        id="WP17",
        title="Cross-repository change",
        dependencies=["WP16"],
        requirements=["Extract the provider contract", "Update the UI integration"],
        acceptance_criteria=[
            AcceptanceCriterion(id="contracts", description="Contracts are tested"),
            AcceptanceCriterion(id="ui", description="UI integration is tested"),
        ],
        affected_repositories=["core", "sample"],
        stage=stage,
        complexity=90,
        verification_profile="integration",
        review_findings=["Fix provider error mapping"],
        decomposition_origin_stage=stage.value,
    )


def _valid_payload() -> dict:
    return {
        "ok": True,
        "decision": "shard",
        "reason": "The repositories can be changed independently behind a stable contract.",
        "shards": [
            {
                "id": "contracts",
                "title": "Extract provider contracts",
                "complexity": 40,
                "affected_repositories": ["core"],
                "requirement_indexes": [1],
                "acceptance_criterion_ids": ["contracts"],
                "finding_indexes": [1],
                "depends_on": [],
                "write_scope": ["src/providers/**", "tests/providers/**"],
                "conflict_keys": ["provider-contract"],
                "parallel_safe": True,
            },
            {
                "id": "ui",
                "title": "Update UI integration",
                "complexity": 45,
                "affected_repositories": ["sample"],
                "requirement_indexes": [2],
                "acceptance_criterion_ids": ["ui"],
                "finding_indexes": [],
                "depends_on": [],
                "write_scope": ["src/integrations/**", "tests/integrations/**"],
                "conflict_keys": ["ui-integration"],
                "parallel_safe": True,
            },
        ],
    }


def test_decomposition_builds_persistent_children_with_full_coverage():
    result = validate_decomposition_payload(
        _parent(),
        _valid_payload(),
        policy=DecompositionPolicy(maximum_shards=8, maximum_shard_complexity=55),
        generated_by="planner",
    )

    assert result.decision == "shard"
    assert len(result.shards) == 2
    assert result.plan_hash
    contracts, ui = result.shards
    assert contracts.id == "WP17__contracts"
    assert contracts.dependencies == ["WP16"]
    assert contracts.parent_id == "WP17"
    assert contracts.execution_mode == "standard_shard"
    assert contracts.stage == WorkPackageStage.PREPARE
    assert contracts.parallel_safe is True
    assert ui.affected_repositories == ["sample"]


def test_decomposition_rejects_missing_parent_coverage():
    payload = _valid_payload()
    payload["shards"][1]["acceptance_criterion_ids"] = []

    with pytest.raises(ShardPlanError, match="does not cover parent acceptance criteria"):
        validate_decomposition_payload(
            _parent(),
            payload,
            policy=DecompositionPolicy(),
            generated_by="planner",
        )


def test_decomposition_rejects_cycles_and_foreign_repositories():
    payload = _valid_payload()
    payload["shards"][0]["depends_on"] = ["ui"]
    payload["shards"][1]["depends_on"] = ["contracts"]
    with pytest.raises(ShardPlanError, match="cycle detected"):
        validate_decomposition_payload(
            _parent(), payload, policy=DecompositionPolicy(), generated_by="planner"
        )

    payload = _valid_payload()
    payload["shards"][0]["affected_repositories"] = ["unknown"]
    with pytest.raises(ShardPlanError, match="outside the parent"):
        validate_decomposition_payload(
            _parent(), payload, policy=DecompositionPolicy(), generated_by="planner"
        )


def _candidate(
    package_id: str,
    *,
    agent: str,
    group: str,
    repository: str,
    read_only: bool = False,
    conflict_key: str = "",
) -> ParallelShardCandidate:
    package = WorkPackage(
        id=package_id,
        title=package_id,
        stage=WorkPackageStage.REVIEW if read_only else WorkPackageStage.IMPLEMENT,
        affected_repositories=[repository],
        parent_id="WP17",
        shard_key=package_id,
        execution_mode="review_shard" if read_only else "standard_shard",
        parallel_safe=True,
    )
    return ParallelShardCandidate(
        package=package,
        capability=AgentCapability.REVIEW if read_only else AgentCapability.IMPLEMENT,
        agent_id=agent,
        handoff=StructuredHandoff(
            work_package_id=package_id,
            stage=package.stage.value,
            summary=package_id,
        ),
        read_only=read_only,
        repository_scope=frozenset({repository}),
        conflict_keys=frozenset({conflict_key} if conflict_key else set()),
        concurrency_group=group,
    )


def test_parallel_wave_uses_distinct_concurrency_groups_and_write_scopes():
    policy = ParallelShardPolicy(max_workers=4)
    first = _candidate("a", agent="qwen-coder", group="satellite", repository="core")
    same_gpu = _candidate("b", agent="qwen-9b", group="satellite", repository="sample")
    same_repo = _candidate("c", agent="deepseek", group="zen", repository="core")
    independent = _candidate("d", agent="claude", group="claude", repository="sample")

    selected = choose_parallel_candidates(
        [first, same_gpu, same_repo, independent], policy=policy
    )

    assert [item.package.id for item in selected] == ["a", "d"]


def test_read_only_review_shards_can_share_a_repository():
    policy = ParallelShardPolicy(max_workers=3)
    candidates = [
        _candidate("r1", agent="deepseek", group="zen", repository="core", read_only=True),
        _candidate("r2", agent="qwen", group="satellite", repository="core", read_only=True),
    ]

    assert choose_parallel_candidates(candidates, policy=policy) == candidates

class _PlannerAdapter:
    def __init__(self, provider_id: str, payload: dict):
        self._provider_id = provider_id
        self._payload = payload
        self.executions: list[StructuredHandoff] = []

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def availability(self) -> Availability:
        return Availability.AVAILABLE

    @property
    def capabilities(self) -> set[AgentCapability]:
        return {AgentCapability.DECOMPOSE}

    def execute(self, handoff: StructuredHandoff) -> dict:
        self.executions.append(handoff)
        return dict(self._payload)


class _BarrierReviewAdapter:
    def __init__(self, provider_id: str, barrier: Barrier):
        self._provider_id = provider_id
        self._barrier = barrier
        self.executions: list[StructuredHandoff] = []

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def availability(self) -> Availability:
        return Availability.AVAILABLE

    @property
    def capabilities(self) -> set[AgentCapability]:
        return {AgentCapability.REVIEW}

    def execute(self, handoff: StructuredHandoff) -> dict:
        self.executions.append(handoff)
        self._barrier.wait(timeout=3)
        return {
            "ok": True,
            "verdict": "approved",
            "findings": [],
            "summary": f"Reviewed {handoff.work_package_id}",
        }


def _initialized_orchestrator(tmp_path: Path, packages: list[WorkPackage], **config_values):
    config = OrchestrationConfig(
        state_dir=tmp_path / "state",
        auto_commit=False,
        require_verification=False,
        require_repository_changes=False,
        **config_values,
    )
    orchestrator = ProjectOrchestrator("sharding-test", config=config)
    report = NormalizationReport()
    orchestrator.initialize_graph(PlanGraph(work_packages=packages), report)
    orchestrator.transition_to(TaskExecutionState.RUNNING)
    return orchestrator


def test_orchestrator_decomposition_retries_invalid_planner_and_persists_plan(tmp_path):
    parent = _parent(stage=WorkPackageStage.PREPARE)
    parent.decomposition_origin_stage = ""
    invalid = _valid_payload()
    invalid["shards"][1]["acceptance_criterion_ids"] = []
    first = _PlannerAdapter("planner-invalid", invalid)
    second = _PlannerAdapter("planner-valid", _valid_payload())
    setattr(first, "_execraft_capability_weight", 100)
    setattr(second, "_execraft_capability_weight", 90)
    orchestrator = _initialized_orchestrator(
        tmp_path,
        [parent],
        auto_decompose_enabled=True,
        rotate_agents=False,
        max_agent_attempts_per_stage=2,
    )
    orchestrator.register_agent(first)
    orchestrator.register_agent(second)

    orchestrator._enter_decomposition(parent)
    orchestrator._run_decomposition(parent)

    assert parent.decomposition_status == "expanded"
    assert parent.execution_mode == "aggregate"
    assert parent.stage == WorkPackageStage.REGRESSION_VERIFY
    assert parent.decomposition_agent_id == "planner-valid"
    assert len(parent.shard_ids) == 2
    assert first.executions[0].read_only is True
    assert second.executions[0].stage == "decompose"
    assert orchestrator._provider_health.get("planner-invalid").is_available
    contract_events = [
        event
        for event in orchestrator._journal.read()
        if event.event_type == "contract_health_changed"
    ]
    assert contract_events[-1].payload["provider_id"] == "planner-invalid"
    persisted = list((tmp_path / "state" / "projects" / "sharding-test" / "generated-plans" / "WP17").glob("*.json"))
    assert len(persisted) == 1
    payload = json.loads(persisted[0].read_text(encoding="utf-8"))
    assert payload["plan_hash"] == parent.decomposition_plan_hash
    assert {item["id"] for item in payload["shards"]} == set(parent.shard_ids)


def test_parallel_review_wave_executes_two_agents_concurrently(tmp_path):
    barrier = Barrier(2)
    first_agent = _BarrierReviewAdapter("review-a", barrier)
    second_agent = _BarrierReviewAdapter("review-b", barrier)
    setattr(first_agent, "_execraft_concurrency_group", "group-a")
    setattr(second_agent, "_execraft_concurrency_group", "group-b")
    setattr(first_agent, "_execraft_capability_weight", 100)
    setattr(second_agent, "_execraft_capability_weight", 90)

    first = WorkPackage(
        id="WP17__review_a",
        title="Review A",
        stage=WorkPackageStage.REVIEW,
        affected_repositories=["core"],
        parent_id="WP17",
        shard_key="review_a",
        execution_mode="review_shard",
        parallel_safe=True,
        complexity=30,
    )
    second = WorkPackage(
        id="WP17__review_b",
        title="Review B",
        stage=WorkPackageStage.REVIEW,
        affected_repositories=["core"],
        parent_id="WP17",
        shard_key="review_b",
        execution_mode="review_shard",
        parallel_safe=True,
        complexity=30,
    )
    parent = WorkPackage(
        id="WP17",
        title="Aggregate",
        dependencies=[first.id, second.id],
        stage=WorkPackageStage.FINAL_REVIEW,
        execution_mode="aggregate",
        affected_repositories=["core"],
        complexity=90,
    )
    orchestrator = _initialized_orchestrator(
        tmp_path,
        [parent, first, second],
        parallel_shards_enabled=True,
        parallel_shard_max_workers=2,
        rotate_agents=False,
    )
    orchestrator.register_agent(first_agent)
    orchestrator.register_agent(second_agent)

    assert orchestrator._shard_waves.run([first, second], set()) is True

    assert first.stage == WorkPackageStage.COMPLETED
    assert second.stage == WorkPackageStage.COMPLETED
    assert len(first_agent.executions) == 1
    assert len(second_agent.executions) == 1
    assert first_agent.executions[0].read_only is True
    assert second_agent.executions[0].read_only is True
    assert "Reviewed WP17__review_a" in parent.implementation_summary
    assert "Reviewed WP17__review_b" in parent.implementation_summary

class _BarrierWriteAdapter:
    def __init__(self, provider_id: str, barrier: Barrier, filename: str):
        self._provider_id = provider_id
        self._barrier = barrier
        self._filename = filename

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def availability(self) -> Availability:
        return Availability.AVAILABLE

    @property
    def capabilities(self) -> set[AgentCapability]:
        return {AgentCapability.IMPLEMENT}

    def execute(self, handoff: StructuredHandoff) -> dict:
        self._barrier.wait(timeout=3)
        root = Path(handoff.working_directory)
        (root / self._filename).write_text(
            f"generated by {self._provider_id}\n", encoding="utf-8"
        )
        return {
            "ok": True,
            "status": "implemented",
            "summary": f"Created {self._filename}",
            "acceptance_evidence": {},
        }


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=path,
        check=True,
    )
    (path / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=path, check=True)


def test_parallel_write_shards_use_isolated_worktrees_and_apply_deltas(tmp_path):
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    _init_repo(repo_a)
    _init_repo(repo_b)
    barrier = Barrier(2)
    agent_a = _BarrierWriteAdapter("writer-a", barrier, "a.txt")
    agent_b = _BarrierWriteAdapter("writer-b", barrier, "b.txt")
    setattr(agent_a, "_execraft_concurrency_group", "writer-a")
    setattr(agent_b, "_execraft_concurrency_group", "writer-b")
    setattr(agent_a, "_execraft_capability_weight", 100)
    setattr(agent_b, "_execraft_capability_weight", 90)

    shard_a = WorkPackage(
        id="WP17__a",
        title="A",
        stage=WorkPackageStage.PREPARE,
        affected_repositories=["a"],
        parent_id="WP17",
        shard_key="a",
        execution_mode="standard_shard",
        parallel_safe=True,
        write_scope=["a.txt"],
        complexity=30,
    )
    shard_b = WorkPackage(
        id="WP17__b",
        title="B",
        stage=WorkPackageStage.PREPARE,
        affected_repositories=["b"],
        parent_id="WP17",
        shard_key="b",
        execution_mode="standard_shard",
        parallel_safe=True,
        write_scope=["b.txt"],
        complexity=30,
    )
    parent = WorkPackage(
        id="WP17",
        title="Aggregate",
        dependencies=[shard_a.id, shard_b.id],
        stage=WorkPackageStage.REGRESSION_VERIFY,
        execution_mode="aggregate",
        affected_repositories=["a", "b"],
        complexity=90,
    )
    config = OrchestrationConfig(
        state_dir=tmp_path / "state",
        auto_commit=False,
        require_verification=False,
        require_repository_changes=False,
        parallel_shards_enabled=True,
        parallel_shard_max_workers=2,
        rotate_agents=False,
    )
    orchestrator = ProjectOrchestrator(
        "write-wave",
        config=config,
        repository_paths={"a": repo_a, "b": repo_b},
        workspace_root=tmp_path,
    )
    orchestrator.initialize_graph(
        PlanGraph(work_packages=[parent, shard_a, shard_b]), NormalizationReport()
    )
    orchestrator.transition_to(TaskExecutionState.RUNNING)
    orchestrator.register_agent(agent_a)
    orchestrator.register_agent(agent_b)

    assert orchestrator._shard_waves.run([shard_a, shard_b], set()) is True

    assert (repo_a / "a.txt").read_text(encoding="utf-8") == "generated by writer-a\n"
    assert (repo_b / "b.txt").read_text(encoding="utf-8") == "generated by writer-b\n"
    assert shard_a.stage == WorkPackageStage.FAST_VERIFY
    assert shard_b.stage == WorkPackageStage.FAST_VERIFY
    assert not (tmp_path / "state" / "projects" / "write-wave" / "parallel-worktrees").exists()


def test_parallel_scope_rejection_fails_running_invocation_without_crashing_wave(
    tmp_path,
):
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    _init_repo(repo_a)
    _init_repo(repo_b)
    barrier = Barrier(2)
    agent_a = _BarrierWriteAdapter("writer-a", barrier, "pyproject.toml")
    agent_b = _BarrierWriteAdapter("writer-b", barrier, "pyproject.toml")
    for adapter in (agent_a, agent_b):
        setattr(adapter, "_execraft_concurrency_group", adapter.provider_id)

    shard_a = WorkPackage(
        id="WP17__a",
        title="Rejected scope",
        stage=WorkPackageStage.PREPARE,
        affected_repositories=["a"],
        parent_id="WP17",
        shard_key="a",
        execution_mode="standard_shard",
        parallel_safe=True,
        write_scope=["allowed.txt"],
    )
    shard_b = WorkPackage(
        id="WP17__b",
        title="Accepted scope",
        stage=WorkPackageStage.PREPARE,
        affected_repositories=["b"],
        parent_id="WP17",
        shard_key="b",
        execution_mode="standard_shard",
        parallel_safe=True,
        write_scope=["allowed.txt"],
    )
    orchestrator = ProjectOrchestrator(
        "scope-wave",
        config=OrchestrationConfig(
            state_dir=tmp_path / "state",
            auto_commit=False,
            require_verification=False,
            require_repository_changes=False,
            parallel_shards_enabled=True,
            parallel_shard_max_workers=2,
            rotate_agents=False,
        ),
        repository_paths={"a": repo_a, "b": repo_b},
        workspace_root=tmp_path,
    )
    orchestrator.initialize_graph(
        PlanGraph(work_packages=[shard_a, shard_b]), NormalizationReport()
    )
    orchestrator.transition_to(TaskExecutionState.RUNNING)
    orchestrator.register_agent(agent_a)
    orchestrator.register_agent(agent_b)

    assert orchestrator._shard_waves.run([shard_a, shard_b], set()) is True

    records = [
        *orchestrator._agent_invocations.list_for_package("scope-wave", shard_a.id),
        *orchestrator._agent_invocations.list_for_package("scope-wave", shard_b.id),
    ]
    assert [record.status for record in records] == ["failed", "failed"]
    assert all("outside write_scope" in record.failure["error"] for record in records)
    assert (repo_a / "README.md").read_text(encoding="utf-8") == "base\n"
    assert (repo_b / "README.md").read_text(encoding="utf-8") == "base\n"
    assert not any((repo / "pyproject.toml").exists() for repo in (repo_a, repo_b))


def test_parallel_write_shards_can_apply_disjoint_deltas_to_one_repository(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    barrier = Barrier(2)
    agent_a = _BarrierWriteAdapter("writer-a", barrier, "a.txt")
    agent_b = _BarrierWriteAdapter("writer-b", barrier, "b.txt")
    for priority, adapter in enumerate((agent_a, agent_b), start=1):
        setattr(adapter, "_execraft_concurrency_group", adapter.provider_id)
        setattr(adapter, "_execraft_capability_weight", 101 - priority)

    shard_a = WorkPackage(
        id="WP18__a",
        title="A",
        stage=WorkPackageStage.PREPARE,
        affected_repositories=["repo"],
        parent_id="WP18",
        shard_key="a",
        execution_mode="standard_shard",
        parallel_safe=True,
        write_scope=["a.txt"],
        conflict_keys=["file:a.txt"],
        complexity=30,
    )
    shard_b = WorkPackage(
        id="WP18__b",
        title="B",
        stage=WorkPackageStage.PREPARE,
        affected_repositories=["repo"],
        parent_id="WP18",
        shard_key="b",
        execution_mode="standard_shard",
        parallel_safe=True,
        write_scope=["b.txt"],
        conflict_keys=["file:b.txt"],
        complexity=30,
    )
    parent = WorkPackage(
        id="WP18",
        title="Aggregate",
        dependencies=[shard_a.id, shard_b.id],
        stage=WorkPackageStage.REGRESSION_VERIFY,
        execution_mode="aggregate",
        affected_repositories=["repo"],
        complexity=90,
    )
    config = OrchestrationConfig(
        state_dir=tmp_path / "state",
        auto_commit=False,
        require_verification=False,
        require_repository_changes=False,
        parallel_shards_enabled=True,
        parallel_shard_max_workers=2,
        parallel_require_disjoint_repositories_for_writes=False,
        rotate_agents=False,
    )
    orchestrator = ProjectOrchestrator(
        "same-repo-write-wave",
        config=config,
        repository_paths={"repo": repo},
        workspace_root=tmp_path,
    )
    orchestrator.initialize_graph(
        PlanGraph(work_packages=[parent, shard_a, shard_b]), NormalizationReport()
    )
    orchestrator.transition_to(TaskExecutionState.RUNNING)
    orchestrator.register_agent(agent_a)
    orchestrator.register_agent(agent_b)

    assert orchestrator._shard_waves.run([shard_a, shard_b], set()) is True

    assert (repo / "a.txt").read_text(encoding="utf-8") == "generated by writer-a\n"
    assert (repo / "b.txt").read_text(encoding="utf-8") == "generated by writer-b\n"
    assert shard_a.stage == WorkPackageStage.FAST_VERIFY
    assert shard_b.stage == WorkPackageStage.FAST_VERIFY


def test_decomposition_rejects_parent_traversal_in_scopes():
    payload = _valid_payload()
    payload["shards"][0]["write_scope"] = ["../other-repo/**"]
    with pytest.raises(ShardPlanError, match="cannot traverse parents"):
        validate_decomposition_payload(
            _parent(), payload, policy=DecompositionPolicy(), generated_by="planner"
        )


def test_decompose_planning_uses_reduced_capability_complexity(tmp_path):
    parent = _parent(stage=WorkPackageStage.DECOMPOSE)
    planner = _PlannerAdapter("local-planner", _valid_payload())
    setattr(
        planner,
        "_execraft_max_complexity_by_capability",
        {AgentCapability.DECOMPOSE: 80},
    )
    orchestrator = _initialized_orchestrator(
        tmp_path,
        [parent],
        auto_decompose_enabled=True,
        rotate_agents=False,
    )
    orchestrator.register_agent(planner)

    # The parent is complexity 90, but the bounded planning task is scored at
    # 68, so a local planner capped at 80 remains eligible.
    assert orchestrator._capability_complexity(parent, AgentCapability.DECOMPOSE) == 68
    assert (
        orchestrator._select_agent_for_capability(
            AgentCapability.DECOMPOSE,
            package=parent,
        )
        == "local-planner"
    )


class _UnavailableImplementAdapter:
    provider_id = "premium-implementer"
    availability = Availability.QUOTA_EXHAUSTED
    capabilities = {AgentCapability.IMPLEMENT}

    def execute(self, handoff: StructuredHandoff) -> dict:  # pragma: no cover
        raise AssertionError("unavailable adapter must not execute")


def test_auto_decompose_considers_current_provider_availability(tmp_path):
    package = WorkPackage(
        id="WP18",
        title="Moderate package blocked by quota",
        requirements=["Implement the isolated feature"],
        acceptance_criteria=[
            AcceptanceCriterion(id="done", description="Feature is verified")
        ],
        affected_repositories=["core"],
        stage=WorkPackageStage.PREPARE,
        complexity=50,
    )
    unavailable = _UnavailableImplementAdapter()
    setattr(
        unavailable,
        "_execraft_max_complexity_by_capability",
        {AgentCapability.IMPLEMENT: 100},
    )
    orchestrator = _initialized_orchestrator(
        tmp_path,
        [package],
        auto_decompose_enabled=True,
        decompose_complexity_threshold=60,
        decompose_when_no_eligible_provider=True,
        rotate_agents=False,
    )
    orchestrator.register_agent(unavailable)

    assert orchestrator._should_auto_decompose(package) is True


def test_mandatory_decomposition_runs_with_automatic_policy_disabled(tmp_path):
    parent = _parent(stage=WorkPackageStage.PREPARE)
    parent.decomposition_required = True
    parent.decomposition_required_reason = "operator requires explicit shards"
    parent.decomposition_required_at = "2026-08-04T12:00:00+00:00"
    planner = _PlannerAdapter("planner", _valid_payload())
    orchestrator = _initialized_orchestrator(
        tmp_path,
        [parent],
        auto_decompose_enabled=False,
        rotate_agents=False,
    )
    orchestrator.register_agent(planner)

    orchestrator._process_package(parent)

    assert planner.executions
    assert parent.decomposition_status == "expanded"
    assert parent.decomposition_required is False
    assert parent.decomposition_required_consumed_at
    requested = [
        event
        for event in orchestrator._journal.read()
        if event.event_type == "package_decomposition_requested"
    ]
    assert requested[-1].payload["trigger"] == "mandatory"
    consumed = [
        event
        for event in orchestrator._journal.read()
        if event.event_type == "mandatory_decomposition_consumed"
    ]
    assert consumed[-1].payload["outcome"] == "expanded"
