import assert from "node:assert/strict";
import { summarizeAgentWorkforce } from "../../src/execraft/assets/gui/agent-workforce-view.js";

const snapshot = {
  run: { owned_running: false, external_running: false },
  packages: [
    { id: "WP17", title: "Routing update", stage: "implement" },
  ],
  assignments: [
    { package_id: "WP17", package_title: "Routing update", stage: "implement", agent_id: "coder", parallel: false },
  ],
  execution_lanes: [
    {
      id: "lane-qwen",
      display_name: "Qwen GPU #1",
      runtime_kind: "openclaw",
      model_display_name: "Qwen3 Coder",
      target_display_name: "GPU Node A",
      profile_ids: ["coder", "reviewer"],
    },
  ],
  agents: [
    {
      id: "coder",
      name: "Implementer",
      enabled: true,
      runtime_kind: "openclaw",
      capabilities: ["implementation"],
      health: { status: "available", available: true },
      assignments: [{ package_id: "WP17", stage: "implement" }],
      native_maintenance: false,
    },
    {
      id: "native-reviewer",
      name: "Reviewer",
      enabled: true,
      runtime_kind: "native",
      capabilities: ["review"],
      max_complexity: { review: 80 },
      health: { status: "cooldown", reason: "adapter timeout", failures: 2 },
      assignments: [],
      native_maintenance: true,
      promotions: [],
      action: {},
    },
    {
      id: "idle",
      enabled: true,
      runtime_kind: "native",
      capabilities: ["implementation"],
      health: { status: "available", available: true },
      assignments: [],
      native_maintenance: true,
      max_complexity: { implementation: 100 },
    },
  ],
};

const workforce = summarizeAgentWorkforce(snapshot);
assert.deepEqual(workforce.counts, {
  total: 3,
  working: 1,
  attention: 1,
  available: 1,
  disabled: 0,
});
assert.equal(workforce.groups.working[0].id, "coder");
assert.equal(workforce.groups.working[0].lane_label, "Qwen GPU #1");
assert.equal(workforce.groups.working[0].package_title, "Routing update");
assert.equal(workforce.groups.attention[0].id, "native-reviewer");
assert.equal(workforce.groups.attention[0].health_reason, "adapter timeout");
assert.equal(workforce.groups.attention[0].can_promote, true);
assert.equal(workforce.groups.attention[0].can_doctor, true);
assert.equal(workforce.groups.attention[0].can_reset, true);
assert.equal(workforce.groups.available[0].id, "idle");
assert.equal(workforce.groups.available[0].can_promote, false);

const running = summarizeAgentWorkforce({
  ...snapshot,
  run: { owned_running: true },
});
const reviewer = running.workers.find((item) => item.id === "native-reviewer");
assert.equal(reviewer.can_doctor, false);
assert.equal(reviewer.can_reset, false);
assert.equal(reviewer.can_promote, true);

console.log("agent workforce contracts passed");
