"""Focused tests for the extracted shard-wave coordinator boundary."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from execraft.orchestrate.models import TaskExecutionState
from execraft.orchestrate.shard_wave import ShardWaveCoordinator


class _Journal:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict]] = []

    def append(self, event_type: str, payload: dict) -> None:
        self.entries.append((event_type, payload))


class _Host:
    def __init__(self, state_dir: Path, wave: dict | None = None) -> None:
        self._state_dir = state_dir
        self._wave = dict(wave or {})
        self._journal = _Journal()
        self._state_record = SimpleNamespace(error_message="")
        self.progress: list[tuple[str, dict]] = []
        self.transitions: list[TaskExecutionState] = []

    def _active_parallel_wave(self) -> dict:
        return dict(self._wave)

    def _set_active_parallel_wave(self, wave: dict | None) -> None:
        self._wave = dict(wave or {})

    def _emit_progress(self, event_type: str, **payload: object) -> None:
        self.progress.append((event_type, dict(payload)))

    def transition_to(self, state: TaskExecutionState) -> None:
        self.transitions.append(state)

    def _build_parallel_wave(self, ready: list, deferred_ids: set[str]) -> list:
        return []


def test_coordinator_declines_when_no_parallel_wave_is_available(tmp_path):
    coordinator = ShardWaveCoordinator(_Host(tmp_path))

    assert coordinator.run([], set()) is False


def test_interrupted_wave_is_cleared_and_escalated_for_human_review(tmp_path):
    host = _Host(
        tmp_path,
        {
            "wave_id": "wave-stale",
            "package_ids": ["WP1__a", "WP1__b"],
            "isolations": [],
        },
    )

    assert ShardWaveCoordinator(host).recover_interrupted() is True

    assert host._wave == {}
    assert host.transitions == [TaskExecutionState.HUMAN_REQUIRED]
    assert host._journal.entries == [
        (
            "parallel_wave_interrupted",
            {
                "wave_id": "wave-stale",
                "package_ids": ["WP1__a", "WP1__b"],
            },
        ),
        (
            "human_intervention_required",
            {
                "package_id": "WP1__a",
                "stage": "parallel_state_recovery",
                "blocked_requirement": "interrupted parallel shard wave: WP1__a, WP1__b",
                "evidence": [
                    "the driver stopped before the active parallel wave reached its durable boundary"
                ],
                "recommended_decision": (
                    "let the Supervisor reconcile completed shard results and retry "
                    "only unfinished work"
                ),
            },
        ),
    ]
    assert host.progress[0][0] == "human_required"
    assert "WP1__a, WP1__b" in host._state_record.error_message


def test_shard_wave_host_protocol_completeness():
    """Verify that ShardWaveHost Protocol declares all required host methods and attributes."""
    import typing

    from execraft.orchestrate.orchestrator import ProjectOrchestrator
    from execraft.orchestrate.shard_wave import ShardWaveHost

    protocol_annotations = typing.get_type_hints(ShardWaveHost)

    expected_methods = {
        "save_state",
        "transition_to",
        "_active_parallel_wave",
        "_set_active_parallel_wave",
        "_emit_progress",
        "_build_parallel_wave",
        "_validate_clean_start",
        "_advance_package_stage",
        "_prepare_parallel_write_isolation",
        "_cleanup_parallel_isolation",
        "escalate_scope_failure",
        "_validated_prompt",
        "_parallel_dirty_owners",
        "_set_parallel_dirty_owners",
        "_find_adapter",
        "_agent_metadata",
        "_parallel_workspace_digest",
        "_invoke_parallel_candidate",
        "_repository_worktree_fingerprint",
        "_apply_parallel_failure",
        "_release_parallel_dirty_ownership",
        "_validate_parallel_result",
        "_apply_parallel_isolated_delta",
        "_apply_parallel_success",
    }

    for method_name in expected_methods:
        assert hasattr(
            ShardWaveHost, method_name
        ), f"Missing method {method_name} on ShardWaveHost"
        assert hasattr(
            ProjectOrchestrator, method_name
        ), f"Missing method {method_name} on ProjectOrchestrator"

    expected_attributes = {
        "_state_dir",
        "_invocation_project_id",
        "task_id",
        "_journal",
        "_agent_invocations",
        "_state_record",
    }

    for attr_name in expected_attributes:
        assert (
            attr_name in protocol_annotations
        ), f"Missing attribute annotation {attr_name} on ShardWaveHost"


def test_shard_wave_host_static_methods_match_orchestrator_contract():
    """Keep protocol descriptors aligned with the façade implementation."""
    import inspect

    from execraft.orchestrate.orchestrator import ProjectOrchestrator
    from execraft.orchestrate.shard_wave import ShardWaveHost

    for method_name in ("_validated_prompt", "_agent_metadata"):
        assert isinstance(inspect.getattr_static(ShardWaveHost, method_name), staticmethod)
        assert isinstance(
            inspect.getattr_static(ProjectOrchestrator, method_name), staticmethod
        )
