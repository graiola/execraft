from __future__ import annotations

import sqlite3
from pathlib import Path

from execraft.agents.config import parse_execution_config
from execraft.orchestrate.runtime_continuation import RuntimeSessionBindingStore
from execraft.orchestrate.usage import normalize_agent_usage
from execraft.runtime.openclaw_optimization import (
    compaction_result_reusable,
    decide_continuation_optimization,
)
from execraft.runtime.openclaw_projection import project_openclaw_config
from execraft.runtime.openclaw_session_rpc import OpenClawSessionSnapshot
from execraft.runtime_config import OpenClawOptimizationOptions
from tests.test_openclaw_continuation import (
    _ContinuationClient,
    _execute,
    _handoff,
    _runner,
)
from tests.test_openclaw_agent_runtime import _runtime


class _OptimizationClient(_ContinuationClient):
    def __init__(self, workspace: Path | None = None) -> None:
        super().__init__(workspace)
        self.rows: dict[str, dict] = {}
        self.compactions: list[tuple[str, dict, float | None]] = []
        self.auto_compact_on_run = False

    def describe_session(self, key: str):
        row = self.rows.get(key)
        if row is not None:
            return dict(row)
        return super().describe_session(key)

    def start_agent(self, params, **kwargs):
        accepted = super().start_agent(params, **kwargs)
        key = params["sessionKey"]
        row = self.rows.setdefault(
            key,
            {
                "key": key,
                "agentId": params.get("agentId", ""),
                # A Gateway that does report occupancy: OpenClaw 2026.7.1-2
                # does not, so its own semantics are covered separately below.
                "contextWindow": 80000,
                "compactionCount": 0,
                "usage": {"contextTokens": 12000, "cacheRead": 0, "cacheWrite": 0},
            },
        )
        if self.auto_compact_on_run and len(self.started) >= 2:
            row["contextTokens"] = 18000
            row["compactionCount"] = int(row.get("compactionCount", 0)) + 1
            self.auto_compact_on_run = False
        return accepted

    def request(self, method, params=None, *, timeout_seconds=None, idempotency_key=""):
        if method != "sessions.compact":
            return super().request(
                method, params, timeout_seconds=timeout_seconds, idempotency_key=idempotency_key
            )
        params = dict(params or {})
        key = params["key"]
        self.compactions.append((method, params, timeout_seconds))
        row = self.rows[key]
        usage = dict(row.get("usage") or {})
        before = int(usage.get("contextTokens", 0))
        usage["contextTokens"] = 18000
        row["usage"] = usage
        row["compactionCount"] = int(row.get("compactionCount", 0)) + 1
        return {
            "compacted": True,
            "result": {"tokensBefore": before, "tokensAfter": 18000},
        }


def test_schema_v4_parses_openclaw_optimization_and_projection() -> None:
    raw = {
        "schema_version": 4,
        "runtimes": {
            "openclaw": {
                "kind": "openclaw",
                "mode": "managed",
                "auth_kind": "none",
                "optimization": {
                    "cache_retention": "long",
                    "context_pruning_mode": "cache-ttl",
                    "context_pruning_ttl": "45m",
                    "compaction_mode": "default",
                    "compaction_timeout_seconds": 240,
                    "context_pressure_ratio": 0.75,
                },
            }
        },
        "execution_targets": {
            "local-ollama": {
                "kind": "local",
                "endpoint": "http://127.0.0.1:11434",
            }
        },
        "model_routes": {
            "qwen": {
                "provider": "ollama",
                "model": "qwen3-coder",
                "endpoint": "http://127.0.0.1:11434",
                "default_target": "local-ollama",
            }
        },
        "agents": {
            "worker": {
                "runtime": "openclaw",
                "model_route": "qwen",
                "capabilities": ["implement"],
            }
        },
    }
    execution = parse_execution_config(raw)
    policy = execution.runtime("openclaw").openclaw.optimization
    assert policy.cache_retention == "long"
    assert policy.context_pruning_mode == "cache-ttl"
    assert policy.context_pressure_ratio == 0.75

    projection = project_openclaw_config(execution, "openclaw")
    defaults = projection.config["agents"]["defaults"]
    assert defaults["params"]["cacheRetention"] == "long"
    assert defaults["contextPruning"] == {"mode": "cache-ttl", "ttl": "45m"}
    assert defaults["compaction"]["mode"] == "default"
    assert defaults["compaction"]["timeoutSeconds"] == 240


def test_openclaw_normalized_cache_fields_are_non_overlapping_even_for_openai() -> None:
    usage = normalize_agent_usage(
        {"usage": {"input": 100, "output": 20, "cacheRead": 400, "cacheWrite": 50}},
        prompt="short prompt",
        provider="openai",
        model="gpt-test",
        capability="implementation",
        package_id="WP10",
    )
    assert usage.input_tokens == 100
    assert usage.uncached_input_tokens == 100
    assert usage.cache_read_tokens == 400
    assert usage.cache_creation_tokens == 50
    assert usage.effective_input_tokens == 550
    assert usage.total_tokens == 570


def test_context_pressure_requests_compaction_and_requires_tokens_after() -> None:
    snapshot = OpenClawSessionSnapshot(
        key="session", context_tokens=75000, context_window=80000, compaction_count=2
    )
    policy = OpenClawOptimizationOptions(context_pressure_ratio=0.80)
    decision = decide_continuation_optimization(
        snapshot=snapshot,
        policy=policy,
        binding_metadata={"reuse_count": 1, "guarded_compaction_count": 2},
        delta_prompt_bytes=1024,
    )
    assert decision.reuse and decision.compact
    assert decision.reason == "context_pressure"
    assert not compaction_result_reusable(tokens_after=None, snapshot=snapshot, policy=policy)
    assert compaction_result_reusable(tokens_after=18000, snapshot=snapshot, policy=policy)


def test_v1_runtime_session_store_migrates_guarded_compaction_boundary(tmp_path: Path) -> None:
    path = tmp_path / "runtime-sessions.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE runtime_sessions (
            package_id TEXT NOT NULL,
            role TEXT NOT NULL,
            candidate_id TEXT NOT NULL,
            runtime_id TEXT NOT NULL,
            model_route_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            context_epoch TEXT NOT NULL,
            session_json TEXT NOT NULL,
            last_invocation_id TEXT NOT NULL,
            last_workspace_digest TEXT NOT NULL,
            reuse_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(package_id, role)
        );
        CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO metadata(key, value) VALUES('schema_version', '1');
        """
    )
    connection.commit()
    connection.close()

    RuntimeSessionBindingStore(path)
    connection = sqlite3.connect(path)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(runtime_sessions)")}
    version = connection.execute(
        "SELECT value FROM metadata WHERE key='schema_version'"
    ).fetchone()[0]
    connection.close()
    assert {
        "guarded_compaction_count",
        "runtime_backend",
        "model_provider",
        "model_name",
        "target_kind",
    }.issubset(columns)
    assert version == "3"


def test_context_pressure_compacts_then_sends_authoritative_guard(tmp_path: Path) -> None:
    runtime, service = _runtime(tmp_path)
    client = _OptimizationClient(tmp_path)
    service.client = client
    store = RuntimeSessionBindingStore(tmp_path / "runtime-sessions.db")
    runner = _runner(tmp_path, store)

    _execute(runner, runtime, _handoff(tmp_path, attempt=1, verification=""), 1, "w0", "w1")
    key = client.started[0][0]["sessionKey"]
    client.rows[key]["usage"] = {**client.rows[key]["usage"], "contextTokens": 75000}
    second = _execute(
        runner,
        runtime,
        _handoff(tmp_path, attempt=2, verification="new evidence"),
        2,
        "w1",
        "w2",
    )

    assert client.started[1][0]["sessionKey"] == key
    assert client.compactions and client.compactions[0][2] == 180.0
    message = client.started[1][0]["message"]
    assert "durable state remains authoritative" in message
    assert "resume safely" in message
    assert second.invocation.runtime_metadata["authoritative_guard"] is True
    assert second.invocation.runtime_metadata["compaction"]["tokens_before"] == 75000
    assert second.invocation.runtime_metadata["compaction"]["tokens_after"] == 18000
    binding = store.get("WP8", "implementer")
    assert binding is not None and binding.guarded_compaction_count == 1


def test_automatic_compaction_is_guarded_on_following_continuation(tmp_path: Path) -> None:
    runtime, service = _runtime(tmp_path)
    client = _OptimizationClient(tmp_path)
    service.client = client
    store = RuntimeSessionBindingStore(tmp_path / "runtime-sessions.db")
    runner = _runner(tmp_path, store)

    _execute(runner, runtime, _handoff(tmp_path, attempt=1, verification=""), 1, "w0", "w1")
    key = client.started[0][0]["sessionKey"]
    client.auto_compact_on_run = True
    second = _execute(
        runner,
        runtime,
        _handoff(tmp_path, attempt=2, verification="evidence-2"),
        2,
        "w1",
        "w2",
    )
    after_auto = store.get("WP8", "implementer")
    assert second.invocation.runtime_metadata["post_session"]["compaction_count"] == 1
    assert second.invocation.runtime_metadata["authoritative_guard"] is False
    assert after_auto is not None and after_auto.guarded_compaction_count == 0

    third = _execute(
        runner,
        runtime,
        _handoff(tmp_path, attempt=3, verification="evidence-3"),
        3,
        "w2",
        "w3",
    )
    assert client.started[2][0]["sessionKey"] == key
    assert third.invocation.runtime_metadata["continuation_decision"] == "observed_compaction"
    assert third.invocation.runtime_metadata["authoritative_guard"] is True
    assert "durable state remains authoritative" in client.started[2][0]["message"]
    guarded = store.get("WP8", "implementer")
    assert guarded is not None and guarded.guarded_compaction_count == 1


def test_unproven_compaction_cold_reconstructs_instead_of_reusing(tmp_path: Path) -> None:
    class _NoAfterClient(_OptimizationClient):
        def request(self, method, params=None, *, timeout_seconds=None, idempotency_key=""):
            if method != "sessions.compact":
                return super().request(
                    method, params, timeout_seconds=timeout_seconds, idempotency_key=idempotency_key
                )
            key = dict(params or {})["key"]
            self.compactions.append((method, dict(params or {}), timeout_seconds))
            self.rows[key]["compactionCount"] += 1
            return {"compacted": True, "result": {"tokensBefore": 75000}}

    runtime, service = _runtime(tmp_path)
    client = _NoAfterClient(tmp_path)
    service.client = client
    store = RuntimeSessionBindingStore(tmp_path / "runtime-sessions.db")
    runner = _runner(tmp_path, store)
    _execute(runner, runtime, _handoff(tmp_path, attempt=1, verification=""), 1, "w0", "w1")
    old_key = client.started[0][0]["sessionKey"]
    client.rows[old_key]["usage"] = {
        **client.rows[old_key]["usage"],
        "contextTokens": 75000,
    }

    second = _execute(
        runner,
        runtime,
        _handoff(tmp_path, attempt=2, verification="evidence"),
        2,
        "w1",
        "w2",
    )
    assert client.started[1][0]["sessionKey"] != old_key
    assert second.invocation.runtime_metadata["cold_reconstruction"] is True
    assert second.invocation.runtime_metadata["continuation_decision"].startswith(
        "compaction_unusable:context_pressure"
    )
    assert "repeated-context" in client.started[1][0]["message"]


def test_automatic_compaction_during_cold_turn_is_not_marked_guarded(tmp_path: Path) -> None:
    class _ColdAutoClient(_OptimizationClient):
        def start_agent(self, params, **kwargs):
            accepted = super().start_agent(params, **kwargs)
            key = params["sessionKey"]
            self.rows[key]["compactionCount"] = 1
            self.rows[key]["contextTokens"] = 18000
            return accepted

    runtime, service = _runtime(tmp_path)
    client = _ColdAutoClient(tmp_path)
    service.client = client
    store = RuntimeSessionBindingStore(tmp_path / "runtime-sessions.db")
    runner = _runner(tmp_path, store)

    first = _execute(
        runner, runtime, _handoff(tmp_path, attempt=1, verification=""), 1, "w0", "w1"
    )
    binding = store.get("WP8", "implementer")
    assert first.invocation.runtime_metadata["post_session"]["compaction_count"] == 1
    assert binding is not None and binding.guarded_compaction_count == 0

    second = _execute(
        runner,
        runtime,
        _handoff(tmp_path, attempt=2, verification="evidence"),
        2,
        "w1",
        "w2",
    )
    assert second.invocation.runtime_metadata["continuation_decision"] == "observed_compaction"
    assert second.invocation.runtime_metadata["authoritative_guard"] is True


def test_reuse_and_delta_thresholds_cold_or_compact_deterministically() -> None:
    snapshot = OpenClawSessionSnapshot(
        key="session", context_tokens=1000, context_window=80000, compaction_count=0
    )
    reuse_limited = OpenClawOptimizationOptions(max_session_reuse_turns=2)
    decision = decide_continuation_optimization(
        snapshot=snapshot,
        policy=reuse_limited,
        binding_metadata={"reuse_count": 2, "guarded_compaction_count": 0},
        delta_prompt_bytes=100,
    )
    assert not decision.reuse and decision.reason == "reuse_limit"

    delta_limited = OpenClawOptimizationOptions(max_delta_prompt_bytes=100)
    decision = decide_continuation_optimization(
        snapshot=snapshot,
        policy=delta_limited,
        binding_metadata={"reuse_count": 0, "guarded_compaction_count": 0},
        delta_prompt_bytes=101,
    )
    assert decision.reuse and decision.compact and decision.reason == "delta_too_large"


# --------------------------------------------------------------------------
# Real OpenClaw 2026.7.1-2 payload semantics
# --------------------------------------------------------------------------

# Captured from a live `sessions.describe` against the pinned release. The two
# token fields do not mean what their names suggest: `contextTokens` is the
# model's context window, and `totalTokens` is cumulative session usage that
# does not fall after a compaction.
_LIVE_DESCRIBE_ROW = {
    "key": "agent:reviewer:dashboard:abc",
    "totalTokens": 8473,
    "totalTokensFresh": True,
    "contextTokens": 32768,
    "compactionCheckpointCount": 2,
}


def test_session_snapshot_reads_the_context_window_not_the_occupancy() -> None:
    snapshot = OpenClawSessionSnapshot.from_row("k", _LIVE_DESCRIBE_ROW)

    assert snapshot.context_window == 32768
    assert snapshot.compaction_count == 2
    # The release exposes no current-occupancy figure. Reporting one would make
    # every session look completely full, so it stays unknown.
    assert snapshot.context_tokens is None
    assert snapshot.context_pressure is None


def test_cumulative_usage_is_never_mistaken_for_occupancy() -> None:
    """`totalTokens` only grows with turns, so it cannot gate session reuse."""

    snapshot = OpenClawSessionSnapshot.from_row("k", _LIVE_DESCRIBE_ROW)

    assert snapshot.context_tokens != _LIVE_DESCRIBE_ROW["totalTokens"]


def test_compaction_without_post_evidence_is_not_reusable() -> None:
    """The pinned release reports `tokensBefore` only.

    Continuation requires positive post-compaction evidence, so a compaction it
    cannot measure must not be treated as sufficient.
    """

    snapshot = OpenClawSessionSnapshot.from_row("k", _LIVE_DESCRIBE_ROW)

    assert (
        compaction_result_reusable(
            tokens_after=None,
            snapshot=snapshot,
            policy=OpenClawOptimizationOptions(),
        )
        is False
    )


def test_explicit_context_window_still_bounds_reuse() -> None:
    snapshot = OpenClawSessionSnapshot.from_row("k", _LIVE_DESCRIBE_ROW)
    policy = OpenClawOptimizationOptions(context_pressure_ratio=0.5)

    assert compaction_result_reusable(
        tokens_after=4_000, snapshot=snapshot, policy=policy
    )
    assert not compaction_result_reusable(
        tokens_after=30_000, snapshot=snapshot, policy=policy
    )
