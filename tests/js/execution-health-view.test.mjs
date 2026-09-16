import assert from "node:assert/strict";
import test from "node:test";

import { summarizeExecutionHealth } from "../../src/execraft/assets/gui/execution-health-view.js";

function lane(overrides = {}) {
  return {
    id: "lane-qwen",
    display_name: "Qwen GPU #1",
    runtime_kind: "openclaw",
    runtime_id: "openclaw-local",
    target_display_name: "GPU Node A",
    roles: ["implement", "review"],
    profile_ids: ["coder", "reviewer"],
    availability: "ready",
    health: "available",
    ...overrides,
  };
}

test("healthy topology stays compact and lane-first", () => {
  const result = summarizeExecutionHealth({
    agents: [
      { id: "coder", enabled: true, health: { status: "available" } },
      { id: "reviewer", enabled: true, health: { status: "available" } },
    ],
    nodes: [{ id: "local", name: "Local" }, { id: "gpu-1", reachable: true }],
    execution_lanes: [lane()],
    assignments: [],
  });

  assert.equal(result.issues.length, 0);
  assert.equal(result.availableLanes.length, 1);
  assert.match(result.summary, /^Execution healthy/);
  assert.doesNotMatch(result.summary, /coder|reviewer/);
});

test("active assignment resolves its presentation lane without changing profile identity", () => {
  const result = summarizeExecutionHealth({
    agents: [{ id: "coder", enabled: true, health: { status: "available" } }],
    execution_lanes: [lane()],
    assignments: [
      {
        package_id: "WP17",
        package_title: "Routing update",
        stage: "implement",
        agent_id: "coder",
        parallel: true,
      },
    ],
  });

  assert.deepEqual(result.active[0], {
    package_id: "WP17",
    package_title: "Routing update",
    stage: "implement",
    agent_id: "coder",
    parallel: true,
    lane_id: "lane-qwen",
    lane_label: "Qwen GPU #1",
    lane_detail: "OpenClaw · GPU Node A",
  });
});

test("degraded lane and offline node surface as attention instead of healthy noise", () => {
  const result = summarizeExecutionHealth({
    nodes: [{ id: "gpu-2", name: "GPU Node B", url: "http://gpu-2", reachable: false }],
    execution_lanes: [
      lane({
        availability: "degraded",
        health: "cooldown",
        diagnostics_summary: "1/2 health-reported profiles currently available.",
      }),
    ],
  });

  assert.equal(result.issues.length, 2);
  assert.equal(result.severe, true);
  assert.match(result.summary, /2 execution issues/);
  assert.equal(result.availableLanes.length, 1);
});

test("unavailable lane is excluded from available lane list", () => {
  const result = summarizeExecutionHealth({
    execution_lanes: [lane({ availability: "unavailable", health: "failed" })],
  });

  assert.equal(result.availableLanes.length, 0);
  assert.equal(result.issues[0].severity, "bad");
});

test("un-grouped compatibility profile still raises health attention", () => {
  const result = summarizeExecutionHealth({
    agents: [
      {
        id: "legacy-native",
        enabled: true,
        health: { status: "cooldown", reason: "quota" },
      },
    ],
  });

  assert.equal(result.issues.length, 1);
  assert.equal(result.issues[0].kind, "profile");
  assert.match(result.issues[0].title, /legacy-native/);
});


test("active satellite without a loaded model preserves the previous runtime warning", () => {
  const result = summarizeExecutionHealth({
    assignments: [{ package_id: "WP17", stage: "implement", agent_id: "coder" }],
    nodes: [
      {
        id: "gpu-1",
        name: "GPU Node A",
        reachable: true,
        agents: ["coder"],
        loaded_models_supported: true,
        loaded_models: [],
      },
    ],
    execution_lanes: [lane()],
  });

  assert.equal(result.issues.length, 1);
  assert.equal(result.issues[0].kind, "node-runtime");
  assert.match(result.issues[0].title, /no loaded model/);
});
