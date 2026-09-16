from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from execraft.repository_sync import (
    RepositorySyncError,
    RepositorySyncService,
    RepositorySyncSpec,
)
from execraft.repository_sync.planning import (
    build_final_sync_definition,
    build_sync_before_definition,
)
from execraft.repository_sync.policy import RepositorySyncPolicy
from execraft.orchestrate.models import OrchestrateError, WorkPackage, WorkPackageKind
import execraft.repository_sync.service as sync_service_module
from execraft.workspace.task_git import RepositorySpec, TaskManifest


def _run(path: Path, *args: str) -> str:
    completed = subprocess.run(
        list(args), cwd=path, text=True, capture_output=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def _git(path: Path, *args: str) -> str:
    return _run(path, "git", *args)


def _configure(path: Path) -> None:
    _git(path, "config", "user.email", "execraft@example.test")
    _git(path, "config", "user.name", "Execraft test")


def _repository(tmp_path: Path, *, conflict: bool = False, prefix: str = "") -> tuple[Path, str]:
    stem = f"{prefix}-" if prefix else ""
    origin = tmp_path / f"{stem}origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "--initial-branch=master")
    seed = tmp_path / f"{stem}seed"
    _run(tmp_path, "git", "clone", str(origin), str(seed))
    _configure(seed)
    (seed / "shared.txt").write_text("base\n", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "base")
    _git(seed, "push", "origin", "master")

    task = tmp_path / f"{stem}task"
    upstream = tmp_path / f"{stem}upstream"
    _run(tmp_path, "git", "clone", str(origin), str(task))
    _run(tmp_path, "git", "clone", str(origin), str(upstream))
    _configure(task)
    _configure(upstream)
    _git(task, "checkout", "-b", "sample_task")
    if conflict:
        (task / "shared.txt").write_text("task\n", encoding="utf-8")
    else:
        (task / "task.txt").write_text("task\n", encoding="utf-8")
    _git(task, "add", ".")
    _git(task, "commit", "-m", "task")
    if conflict:
        (upstream / "shared.txt").write_text("upstream\n", encoding="utf-8")
    else:
        (upstream / "upstream.txt").write_text("upstream\n", encoding="utf-8")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-m", "upstream")
    source_sha = _git(upstream, "rev-parse", "HEAD")
    _git(upstream, "push", "origin", "master")
    return task, source_sha


def _service(tmp_path: Path, task: Path, *, mutability: str = "task_owned") -> RepositorySyncService:
    manifest = TaskManifest(
        schema_version=2,
        id="sample_task",
        project="sample",
        title="Repository synchronization",
        status="in_progress",
        branch_name="sample_task",
        repositories=[
            RepositorySpec(
                id="core",
                base_branch="master",
                task_branch="sample_task",
                mutability=mutability,
            )
        ],
    )
    return RepositorySyncService(
        state_dir=tmp_path / "state",
        manifest=manifest,
        repository_paths={"core": task},
    )


def test_final_sync_definition_depends_on_every_terminal_branch() -> None:
    graph = """schema_version: 1
work_packages:
  - id: WP1
    title: Root
    dependencies: []
    requirements: [root]
    acceptance_criteria: [{id: root_done, description: root done}]
    affected_repositories: [core]
  - id: M2A
    title: Left
    dependencies: [WP1]
    requirements: [left]
    acceptance_criteria: [{id: left_done, description: left done}]
    affected_repositories: [core]
  - id: M2B
    title: Right
    dependencies: [WP1]
    requirements: [right]
    acceptance_criteria: [{id: right_done, description: right done}]
    affected_repositories: [core]
"""

    insertion = build_final_sync_definition(
        brief_markdown="# Brief\n",
        plan_markdown="# Plan\n",
        plan_graph_yaml=graph,
        repositories=["core"],
    )

    projected = __import__("yaml").safe_load(insertion.definition.plan_graph_yaml)
    final = projected["work_packages"][-1]
    assert insertion.sync_package_id == "FINAL-SYNC"
    assert final["kind"] == "repository_sync"
    assert final["dependencies"] == ["M2A", "M2B"]
    assert "Final repository synchronization" in insertion.definition.plan_markdown


def test_clean_sync_pins_source_and_creates_merge_commit(tmp_path: Path) -> None:
    task, source_sha = _repository(tmp_path)
    service = _service(tmp_path, task)
    spec = RepositorySyncSpec.from_mapping({"repositories": ["core"]})

    prepared = service.prepare(package_id="WP20-SYNC", package_fingerprint="abc", spec=spec)
    assert not prepared.needs_resolution
    item = prepared.transaction.repository("core")
    assert item.source_commit == source_sha
    assert item.status == "merge_ready"
    assert _git(task, "rev-parse", "MERGE_HEAD") == source_sha

    service.mark_verified("WP20-SYNC")
    completed = service.commit("WP20-SYNC", title="Sync before WP20")
    item = completed.repository("core")
    assert completed.complete
    assert completed.forward_only
    assert item.status == "committed"
    assert _git(task, "merge-base", "--is-ancestor", source_sha, "HEAD") == ""
    assert len(_git(task, "show", "-s", "--format=%P", "HEAD").split()) == 2


def test_conflict_requires_resolution_then_control_plane_stages(tmp_path: Path) -> None:
    task, source_sha = _repository(tmp_path, conflict=True)
    service = _service(tmp_path, task)
    spec = RepositorySyncSpec.from_mapping({"repositories": ["core"]})

    prepared = service.prepare(package_id="WP20-SYNC", package_fingerprint="abc", spec=spec)
    assert prepared.needs_resolution
    assert prepared.conflict_paths == {"core": ("shared.txt",)}
    (task / "shared.txt").write_text("task + upstream\n", encoding="utf-8")
    service.accept_resolution("WP20-SYNC")
    assert _git(task, "diff", "--name-only", "--diff-filter=U") == ""
    service.mark_verified("WP20-SYNC")
    completed = service.commit("WP20-SYNC", title="Sync")
    assert completed.complete
    assert _git(task, "merge-base", "--is-ancestor", source_sha, "HEAD") == ""


def test_verification_adopts_a_merge_an_agent_committed_itself(tmp_path: Path) -> None:
    """Verification must be re-entrant after an agent commits the pinned merge.

    A FIX_REVIEW agent resolving an "upstream not integrated" finding can run
    ``git commit`` on the in-progress merge. The commit is exactly the one the
    service would have produced, but the transaction still said ``resolved``,
    so re-entering verification reported the HEAD as changed during sync and
    parked the run in ``human_required`` on every retry.
    """

    task, source_sha = _repository(tmp_path, conflict=True)
    service = _service(tmp_path, task)
    spec = RepositorySyncSpec.from_mapping({"repositories": ["core"]})
    service.prepare(package_id="WP20-SYNC", package_fingerprint="abc", spec=spec)
    (task / "shared.txt").write_text("task + upstream\n", encoding="utf-8")
    service.accept_resolution("WP20-SYNC")
    target_before = service._require_transaction("WP20-SYNC").repository("core").target_before
    _git(task, "commit", "--no-edit")
    merge_sha = _git(task, "rev-parse", "HEAD")
    assert merge_sha != target_before

    verified = service.mark_verified("WP20-SYNC")

    item = verified.repository("core")
    assert item.status == "committed"
    assert item.target_after == merge_sha
    assert verified.forward_only

    completed = service.commit("WP20-SYNC", title="Sync")

    assert completed.complete
    assert _git(task, "rev-parse", "HEAD") == merge_sha
    assert _git(task, "merge-base", "--is-ancestor", source_sha, "HEAD") == ""


def test_verification_still_rejects_an_unrelated_head_change(tmp_path: Path) -> None:
    task, _ = _repository(tmp_path, conflict=True)
    service = _service(tmp_path, task)
    spec = RepositorySyncSpec.from_mapping({"repositories": ["core"]})
    service.prepare(package_id="WP20-SYNC", package_fingerprint="abc", spec=spec)
    (task / "shared.txt").write_text("task + upstream\n", encoding="utf-8")
    service.accept_resolution("WP20-SYNC")
    _git(task, "merge", "--abort")
    (task / "unrelated.txt").write_text("unrelated\n", encoding="utf-8")
    _git(task, "add", ".")
    _git(task, "commit", "-m", "unrelated work")

    with pytest.raises(RepositorySyncError, match="HEAD changed during sync"):
        service.mark_verified("WP20-SYNC")


def test_conflict_resolution_rejects_leftover_markers(tmp_path: Path) -> None:
    task, _ = _repository(tmp_path, conflict=True)
    service = _service(tmp_path, task)
    spec = RepositorySyncSpec.from_mapping({"repositories": ["core"]})
    service.prepare(package_id="WP20-SYNC", package_fingerprint="abc", spec=spec)

    with pytest.raises(RepositorySyncError, match="conflict markers"):
        service.accept_resolution("WP20-SYNC")


def test_sync_refuses_dirty_or_runtime_only_repository(tmp_path: Path) -> None:
    task, _ = _repository(tmp_path)
    (task / "dirty.txt").write_text("dirty", encoding="utf-8")
    spec = RepositorySyncSpec.from_mapping({"repositories": ["core"]})
    with pytest.raises(RepositorySyncError, match="uncommitted changes"):
        _service(tmp_path, task).prepare(
            package_id="WP20-SYNC", package_fingerprint="abc", spec=spec
        )
    _git(task, "clean", "-fd")
    with pytest.raises(RepositorySyncError, match="task_owned"):
        _service(tmp_path, task, mutability="runtime_only").prepare(
            package_id="WP20-SYNC", package_fingerprint="abc", spec=spec
        )


def test_divergence_refresh_reports_upstream_commits(tmp_path: Path) -> None:
    task, _ = _repository(tmp_path)
    service = _service(tmp_path, task)
    spec = RepositorySyncSpec.from_mapping({"repositories": ["core"]})
    row = service.divergence(spec, refresh=True)[0]
    assert row.behind == 1
    assert row.ahead == 1
    assert row.error == ""


def test_sync_before_rewires_target_and_defaults_source_branch() -> None:
    graph = """schema_version: 1\nwork_packages:\n  - id: WP19\n    title: done\n  - id: WP20\n    title: next\n    dependencies: [WP19]\n    affected_repositories: [core, ui]\n    requirements: [work]\n    acceptance_criteria:\n      - id: done\n        description: done\n"""
    insertion = build_sync_before_definition(
        brief_markdown="# Brief\n",
        plan_markdown="# Plan\n",
        plan_graph_yaml=graph,
        before_package_id="WP20",
        repositories=["core", "ui"],
        source_branches={"core": "mission_planner"},
    )
    raw = __import__("yaml").safe_load(insertion.definition.plan_graph_yaml)
    packages = raw["work_packages"]
    sync = next(item for item in packages if item["id"] == "WP20-SYNC")
    target = next(item for item in packages if item["id"] == "WP20")
    assert sync["kind"] == "repository_sync"
    assert sync["dependencies"] == ["WP19"]
    assert target["dependencies"] == ["WP20-SYNC"]
    assert sync["repository_sync"]["repositories"]["core"]["source_branch"] == "mission_planner"
    assert "source_branch" not in sync["repository_sync"]["repositories"]["ui"]


def test_policy_rejects_automatic_merge() -> None:
    with pytest.raises(ValueError, match="intentionally unsupported"):
        RepositorySyncPolicy.from_mapping({"automatic_merge": True})



def test_verified_candidate_rejects_post_verification_mutation(tmp_path: Path) -> None:
    task, _ = _repository(tmp_path)
    service = _service(tmp_path, task)
    spec = RepositorySyncSpec.from_mapping({"repositories": ["core"]})
    service.prepare(package_id="WP20-SYNC", package_fingerprint="abc", spec=spec)
    verified = service.mark_verified("WP20-SYNC")
    assert verified.repository("core").verified_tree

    (task / "post-verify.txt").write_text("unexpected\n", encoding="utf-8")
    with pytest.raises(RepositorySyncError, match="not present in the verified index"):
        service.commit("WP20-SYNC", title="Sync")


def test_precommit_sync_can_roll_back_without_rewriting_task_history(tmp_path: Path) -> None:
    task, _ = _repository(tmp_path)
    before = _git(task, "rev-parse", "HEAD")
    service = _service(tmp_path, task)
    spec = RepositorySyncSpec.from_mapping({"repositories": ["core"]})
    service.prepare(package_id="WP20-SYNC", package_fingerprint="abc", spec=spec)

    rolled_back = service.rollback("WP20-SYNC")
    assert rolled_back.phase == "rolled_back"
    assert not rolled_back.forward_only
    assert _git(task, "rev-parse", "HEAD") == before
    assert _git(task, "status", "--porcelain") == ""


def test_multi_repository_commit_failure_recovers_forward_only(tmp_path: Path, monkeypatch) -> None:
    first, first_source = _repository(tmp_path, prefix="first")
    second, second_source = _repository(tmp_path, prefix="second")
    manifest = TaskManifest(
        schema_version=2,
        id="sample_task",
        project="sample",
        title="Repository synchronization",
        status="in_progress",
        branch_name="sample_task",
        repositories=[
            RepositorySpec(id="core", base_branch="master", task_branch="sample_task", mutability="task_owned"),
            RepositorySpec(id="ui", base_branch="master", task_branch="sample_task", mutability="task_owned"),
        ],
    )
    service = RepositorySyncService(
        state_dir=tmp_path / "state",
        manifest=manifest,
        repository_paths={"core": first, "ui": second},
    )
    spec = RepositorySyncSpec.from_mapping({"repositories": ["core", "ui"]})
    service.prepare(package_id="WP20-SYNC", package_fingerprint="abc", spec=spec)
    service.mark_verified("WP20-SYNC")

    real_git = sync_service_module.git
    failed = False
    def fail_second_commit(path: Path, *args: str, **kwargs):
        nonlocal failed
        if Path(path) == second and args and args[0] == "commit" and not failed:
            failed = True
            raise RuntimeError("simulated second repository commit failure")
        return real_git(path, *args, **kwargs)

    monkeypatch.setattr(sync_service_module, "git", fail_second_commit)
    with pytest.raises(RepositorySyncError, match="ui"):
        service.commit("WP20-SYNC", title="Sync")
    interrupted = service.transactions.load("WP20-SYNC")
    assert interrupted is not None and interrupted.forward_only
    assert interrupted.repository("core").status == "committed"
    assert interrupted.repository("ui").status != "committed"
    first_after = _git(first, "rev-parse", "HEAD")

    monkeypatch.setattr(sync_service_module, "git", real_git)
    completed = service.commit("WP20-SYNC", title="Sync")
    assert completed.complete
    assert _git(first, "rev-parse", "HEAD") == first_after
    assert _git(first, "merge-base", "--is-ancestor", first_source, "HEAD") == ""
    assert _git(second, "merge-base", "--is-ancestor", second_source, "HEAD") == ""


def test_repository_sync_work_package_round_trip_and_invariants() -> None:
    package = WorkPackage.from_mapping(
        {
            "id": "WP20-SYNC",
            "title": "Sync upstream",
            "kind": "repository_sync",
            "affected_repositories": ["core"],
            "repository_sync": {"repositories": ["core"]},
            "requirements": ["merge pinned upstream"],
            "acceptance_criteria": [{"id": "merged", "description": "merged"}],
        }
    )
    assert package.kind == WorkPackageKind.REPOSITORY_SYNC
    assert package.repository_sync is not None
    assert WorkPackage.from_mapping(package.as_mapping()).repository_sync.repository_ids == ("core",)

    with pytest.raises(OrchestrateError, match="cannot request decomposition"):
        WorkPackage.from_mapping(
            {
                **package.as_mapping(),
                "decomposition_required": True,
            }
        )


def test_repository_sync_spec_refuses_rebase_and_duplicate_targets() -> None:
    with pytest.raises(ValueError, match="automatic rebase"):
        RepositorySyncSpec.from_mapping({"strategy": "rebase", "repositories": ["core"]})
    with pytest.raises(ValueError, match="duplicate"):
        RepositorySyncSpec.from_mapping({"repositories": ["core", "core"]})


def test_commit_recovers_when_merge_commit_was_created_before_journal_save(tmp_path: Path) -> None:
    task, source = _repository(tmp_path)
    service = _service(tmp_path, task)
    spec = RepositorySyncSpec.from_mapping({"repositories": ["core"]})
    service.prepare(package_id="WP20-SYNC", package_fingerprint="abc", spec=spec)
    service.mark_verified("WP20-SYNC")

    _git(task, "commit", "-m", "simulated commit before crash")
    committed = _git(task, "rev-parse", "HEAD")
    stale = service.transactions.load("WP20-SYNC")
    assert stale is not None
    assert stale.repository("core").status != "committed"

    recovered = service.commit("WP20-SYNC", title="Sync")
    assert recovered.complete
    assert recovered.repository("core").target_after == committed
    assert _git(task, "rev-parse", "HEAD") == committed
    assert _git(task, "merge-base", "--is-ancestor", source, "HEAD") == ""


def test_completed_source_is_a_noop_without_synthetic_commit(tmp_path: Path) -> None:
    task, source = _repository(tmp_path)
    _git(task, "fetch", "origin", "master")
    _git(task, "merge", "--no-edit", source)
    before = _git(task, "rev-parse", "HEAD")
    service = _service(tmp_path, task)
    spec = RepositorySyncSpec.from_mapping({"repositories": ["core"]})

    prepared = service.prepare(package_id="WP20-SYNC", package_fingerprint="abc", spec=spec)
    assert prepared.transaction.repository("core").status == "noop"
    service.mark_verified("WP20-SYNC")
    completed = service.commit("WP20-SYNC", title="Sync")
    assert completed.complete
    assert not completed.forward_only
    assert _git(task, "rev-parse", "HEAD") == before


def test_started_transaction_rejects_package_definition_change(tmp_path: Path) -> None:
    task, _ = _repository(tmp_path)
    service = _service(tmp_path, task)
    spec = RepositorySyncSpec.from_mapping({"repositories": ["core"]})
    service.prepare(package_id="WP20-SYNC", package_fingerprint="v1", spec=spec)

    with pytest.raises(RepositorySyncError, match="definition changed"):
        service.prepare(package_id="WP20-SYNC", package_fingerprint="v2", spec=spec)


def test_repository_sync_policy_rejects_type_coercion() -> None:
    with pytest.raises(ValueError, match="automatic_merge.*boolean"):
        RepositorySyncPolicy.from_mapping({"automatic_merge": "false"})
    with pytest.raises(ValueError, match="warn_behind_commits.*integer"):
        RepositorySyncPolicy.from_mapping(
            {"divergence": {"warn_behind_commits": True}}
        )
    with pytest.raises(ValueError, match="refresh_before_check.*boolean"):
        RepositorySyncPolicy.from_mapping(
            {"divergence": {"refresh_before_check": "true"}}
        )


def test_repository_sync_spec_rejects_non_string_ref_metadata() -> None:
    with pytest.raises(ValueError, match="repository_sync remote must be a string"):
        RepositorySyncSpec.from_mapping({"remote": 123, "repositories": ["core"]})
    with pytest.raises(ValueError, match="source_branch.*must be a string"):
        RepositorySyncSpec.from_mapping(
            {"repositories": {"core": {"source_branch": 123}}}
        )
    with pytest.raises(ValueError, match="remote for core.*must be a string"):
        RepositorySyncSpec.from_mapping(
            {"repositories": {"core": {"remote": 123}}}
        )


def test_rolled_back_transaction_rechecks_cleanliness_before_retry(tmp_path: Path) -> None:
    task, _ = _repository(tmp_path)
    service = _service(tmp_path, task)
    spec = RepositorySyncSpec.from_mapping({"repositories": ["core"]})
    service.prepare(package_id="WP20-SYNC", package_fingerprint="abc", spec=spec)
    service.rollback("WP20-SYNC")

    (task / "operator-edit.txt").write_text("do not absorb me\n", encoding="utf-8")
    with pytest.raises(RepositorySyncError, match="uncommitted changes"):
        service.prepare(
            package_id="WP20-SYNC", package_fingerprint="abc", spec=spec
        )


def test_transaction_loader_rejects_corrupt_repository_records(tmp_path: Path) -> None:
    task, _ = _repository(tmp_path)
    service = _service(tmp_path, task)
    spec = RepositorySyncSpec.from_mapping({"repositories": ["core"]})
    service.prepare(package_id="WP20-SYNC", package_fingerprint="abc", spec=spec)
    path = service.transactions.path_for("WP20-SYNC")

    import yaml

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["repositories"].append("not-a-mapping")
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    with pytest.raises(Exception, match="repositories must contain mappings"):
        service.transactions.load("WP20-SYNC")


def test_remote_branch_choices_and_non_base_provenance(tmp_path: Path) -> None:
    task, _ = _repository(tmp_path)
    upstream = tmp_path / "upstream"
    _git(upstream, "checkout", "-b", "release/test")
    (upstream / "release.txt").write_text("release\n", encoding="utf-8")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-m", "release")
    release_sha = _git(upstream, "rev-parse", "HEAD")
    _git(upstream, "push", "origin", "release/test")

    service = _service(tmp_path, task)
    branches = service.remote_branches("core", refresh=True)
    by_name = {item.branch: item for item in branches}
    assert by_name["master"].configured_base is True
    assert by_name["release/test"].commit == release_sha

    preview = service.divergence_for_branch(
        "core", remote="origin", source_branch="release/test", refresh=True
    )
    assert preview.source_branch == "release/test"
    spec = RepositorySyncSpec.from_mapping(
        {"repositories": {"core": {"source_branch": "release/test"}}}
    )
    transaction = service.prepare(
        package_id="WP20-SYNC", package_fingerprint="override", spec=spec
    ).transaction
    item = transaction.repository("core")
    assert item.configured_base_branch == "master"
    assert item.source_selection == "operator_override"


def test_sync_after_inserts_barrier_and_rewires_incomplete_dependents() -> None:
    from execraft.repository_sync.planning import build_sync_after_definition
    import yaml

    graph = """schema_version: 1
work_packages:
  - id: WP20
    title: active
    affected_repositories: [core]
    requirements: [work]
    acceptance_criteria:
      - id: done
        description: done
  - id: WP21
    title: next
    dependencies: [WP20]
    affected_repositories: [core]
    requirements: [next]
    acceptance_criteria:
      - id: next_done
        description: next done
"""
    insertion = build_sync_after_definition(
        brief_markdown="# Brief\n",
        plan_markdown="# Plan\n",
        plan_graph_yaml=graph,
        after_package_id="WP20",
        repositories=["core"],
    )
    raw = yaml.safe_load(insertion.definition.plan_graph_yaml)
    packages = raw["work_packages"]
    assert [item["id"] for item in packages] == ["WP20", "WP20-SYNC", "WP21"]
    sync = packages[1]
    assert sync["dependencies"] == ["WP20"]
    assert packages[2]["dependencies"] == ["WP20-SYNC"]


def test_repeated_sync_before_allocates_a_fresh_package_id() -> None:
    graph = """schema_version: 1
work_packages:
  - id: WP20-SYNC
    title: previous sync
    kind: repository_sync
    affected_repositories: [core]
    repository_sync:
      repositories: [core]
    requirements: [sync]
    acceptance_criteria:
      - id: synced
        description: synced
  - id: WP20
    title: next
    dependencies: [WP20-SYNC]
    affected_repositories: [core]
    requirements: [work]
    acceptance_criteria:
      - id: done
        description: done
"""
    insertion = build_sync_before_definition(
        brief_markdown="# Brief\n",
        plan_markdown="# Plan\n",
        plan_graph_yaml=graph,
        before_package_id="WP20",
        repositories=["core"],
    )
    assert insertion.sync_package_id == "WP20-SYNC-2"


def test_remote_branch_discovery_rejects_option_like_remote_name(tmp_path: Path) -> None:
    task, _ = _repository(tmp_path)
    service = _service(tmp_path, task)
    with pytest.raises(ValueError, match="invalid repository_sync remote"):
        service.remote_branches("core", remote="--upload-pack=bad", refresh=True)


def test_card_intent_store_is_idempotent_and_rejects_identity_change(tmp_path: Path) -> None:
    from execraft.repository_sync.card_intent import (
        RepositorySyncCardIntentError,
        RepositorySyncCardIntentStore,
    )

    store = RepositorySyncCardIntentStore(tmp_path / "intents")
    request = {
        "mode": "before",
        "repositories": ["core"],
        "source_branches": {},
        "remote": "origin",
        "conflict_policy": "ai_resolve",
        "auto_resume": True,
    }
    first = store.prepare(
        command_id="command-1",
        package_id="WP20",
        sync_package_id="WP20-SYNC",
        request=request,
    )
    second = store.prepare(
        command_id="command-1",
        package_id="WP20",
        sync_package_id="WP20-SYNC",
        request=request,
    )
    assert second == first

    with pytest.raises(RepositorySyncCardIntentError, match="identity conflicts"):
        store.prepare(
            command_id="command-1",
            package_id="WP20",
            sync_package_id="WP20-SYNC-2",
            request=request,
        )


def test_card_intent_store_rejects_phase_regression_and_revision_change(tmp_path: Path) -> None:
    from execraft.repository_sync.card_intent import (
        RepositorySyncCardIntentError,
        RepositorySyncCardIntentStore,
    )

    store = RepositorySyncCardIntentStore(tmp_path / "intents")
    prepared = store.prepare(
        command_id="command-1",
        package_id="WP20",
        sync_package_id="WP20-SYNC",
        request={"mode": "before"},
    )
    replanned = prepared.advanced("replanned", candidate_id="candidate-1", revision=2)
    store.save(replanned)
    store.save(replanned.advanced("boundary_released"))

    with pytest.raises(RepositorySyncCardIntentError, match="cannot move backwards"):
        store.save(replanned)
    with pytest.raises(RepositorySyncCardIntentError, match="revision cannot change"):
        store.save(
            replanned.advanced("complete", revision=3)
        )


def test_card_intent_store_rejects_malformed_journal(tmp_path: Path) -> None:
    from execraft.repository_sync.card_intent import (
        RepositorySyncCardIntentError,
        RepositorySyncCardIntentStore,
    )

    store = RepositorySyncCardIntentStore(tmp_path / "intents")
    path = store.path_for("command-1")
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"command_id":"command-1","package_id":"WP20",'
        '"sync_package_id":"WP20-SYNC","request":{},'
        '"phase":"not-a-phase"}\n',
        encoding="utf-8",
    )

    with pytest.raises(RepositorySyncCardIntentError, match="unsupported.*phase"):
        store.load("command-1")


def test_sync_before_canonicalizes_legacy_hybrid_graph_for_wp2() -> None:
    """Regression for long-lived tasks created before declarative-only WP2 graphs."""

    from execraft.replan.service import ReplanService
    import yaml

    graph = """schema_version: 1
work_packages:
  - id: M00
    title: historical
    dependencies: []
    requirements: [historical work]
    acceptance_criteria:
      - id: old_done
        description: historical work completed
        verified: true
        evidence: imported historical evidence
    affected_repositories: [core]
    stage: completed
    status: completed
    risk: low
    priority: 30
    verification_profile: focused
  - id: WP20
    title: next
    dependencies: [M00]
    requirements: [next work]
    acceptance_criteria:
      - id: next_done
        description: next work completed
        verified: false
        evidence: ''
    affected_repositories: [core]
    stage: prepare
    status: pending
    risk: medium
    priority: 10
    verification_profile: focused
"""
    insertion = build_sync_before_definition(
        brief_markdown="# Brief\n",
        plan_markdown="# Plan\n",
        plan_graph_yaml=graph,
        before_package_id="WP20",
        repositories=["core"],
    )
    candidate = yaml.safe_load(insertion.definition.plan_graph_yaml)
    packages = {item["id"]: item for item in candidate["work_packages"]}

    assert packages["M00"]["acceptance_criteria"] == [
        {"id": "old_done", "description": "historical work completed"}
    ]
    assert "stage" not in packages["M00"]
    assert "status" not in packages["M00"]
    assert "stage" not in packages["WP20"]
    assert "status" not in packages["WP20"]
    assert packages["WP20"]["dependencies"] == ["WP20-SYNC"]
    # Most importantly, this is exactly the safety validator that produced the
    # GUI-owned driver error in the legacy task.
    ReplanService._validate_declarative_graph(candidate)


def test_sync_after_uses_runtime_completion_snapshot_for_rewiring() -> None:
    from execraft.repository_sync.planning import build_sync_after_definition
    import yaml

    # Modern declarative graphs intentionally contain no stage/status fields.
    graph = """schema_version: 1
work_packages:
  - id: WP20
    title: active
    affected_repositories: [core]
    requirements: [work]
    acceptance_criteria:
      - id: done
        description: done
  - id: WP21
    title: already completed dependent
    dependencies: [WP20]
    affected_repositories: [core]
    requirements: [historical]
    acceptance_criteria:
      - id: historical_done
        description: historical done
  - id: WP22
    title: pending dependent
    dependencies: [WP20]
    affected_repositories: [core]
    requirements: [next]
    acceptance_criteria:
      - id: next_done
        description: next done
"""
    insertion = build_sync_after_definition(
        brief_markdown="# Brief\n",
        plan_markdown="# Plan\n",
        plan_graph_yaml=graph,
        after_package_id="WP20",
        repositories=["core"],
        completed_package_ids=["WP21"],
    )
    candidate = yaml.safe_load(insertion.definition.plan_graph_yaml)
    packages = {item["id"]: item for item in candidate["work_packages"]}

    assert packages["WP21"]["dependencies"] == ["WP20"]
    assert packages["WP22"]["dependencies"] == ["WP20-SYNC"]


def test_git_typed_boundary_backs_repository_sync_helpers(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _configure(tmp_path)
    (tmp_path / "base.txt").write_text("base\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "base")

    assert RepositorySyncService._command_succeeded(
        tmp_path, "merge-base", "--is-ancestor", "HEAD", "HEAD"
    )
    assert not RepositorySyncService._command_succeeded(
        tmp_path, "show", "missing-ref"
    )
    assert not RepositorySyncService._has_unstaged_or_untracked_changes(tmp_path)

    (tmp_path / "untracked.txt").write_text("pending\n", encoding="utf-8")
    assert RepositorySyncService._has_unstaged_or_untracked_changes(tmp_path)
