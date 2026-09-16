import test from "node:test";
import assert from "node:assert/strict";

import { workPackageExecutionInternals } from "../../src/execraft/assets/gui/work-package-execution.js";

const roles = [
  { id: "implement", label: "Implement", capability: "implement" },
  { id: "review", label: "Review", capability: "review" },
  { id: "fix_review", label: "Fix review", capability: "fix_review" },
  { id: "final_review", label: "Final review", capability: "review" },
];
const lane = {
  id: "lane-qwen",
  display_name: "Qwen GPU #1",
  runtime_id: "openclaw-local",
  runtime_kind: "openclaw",
  model_route_id: "qwen-route",
  model_display_name: "Qwen3 Coder 30B",
  target_id: "gpu-1",
  target_display_name: "GPU Node A",
  roles: ["implement", "review", "fix_review", "final_review"],
  profile_ids: ["coder", "reviewer", "fixer"],
};
const snapshot = { execution_roles: roles, execution_lanes: [lane] };

test("configured profile preferences are presented as one lane", () => {
  const packageInfo = {
    agent_preferences: { review: ["reviewer", "coder"] },
    agent_preference_binding_roles: [],
  };
  const route = workPackageExecutionInternals.routingForRole(snapshot, packageInfo, "review");
  assert.equal(route.mode, "prefer");
  assert.equal(route.lane.id, "lane-qwen");
  assert.equal(route.preferredLanes.length, 1);
  assert.deepEqual(route.profileIds, ["reviewer", "coder"]);
});

test("binding role preserves Force semantics", () => {
  const packageInfo = {
    agent_preferences: { review: ["reviewer"] },
    agent_preference_binding_roles: ["review"],
  };
  const route = workPackageExecutionInternals.routingForRole(snapshot, packageInfo, "review");
  assert.equal(route.mode, "force");
  assert.equal(route.lane.id, "lane-qwen");
});

test("empty role preference remains Automatic", () => {
  const route = workPackageExecutionInternals.routingForRole(snapshot, {}, "implement");
  assert.equal(route.mode, "automatic");
  assert.equal(route.lane, null);
});

test("next role projection follows stage boundaries without changing scheduler state", () => {
  assert.deepEqual(workPackageExecutionInternals.nextRoleIds({ stage: "implement" }), ["review"]);
  assert.deepEqual(workPackageExecutionInternals.nextRoleIds({ stage: "review", review_findings: ["x"] }), ["fix_review"]);
  assert.deepEqual(workPackageExecutionInternals.nextRoleIds({ stage: "fix_review" }), ["final_review"]);
  assert.deepEqual(workPackageExecutionInternals.nextRoleIds({ stage: "complete" }), []);
});

test("lane detail uses product-facing runtime, model and target labels", () => {
  assert.equal(
    workPackageExecutionInternals.laneDetail(lane),
    "OpenClaw · Qwen3 Coder 30B · GPU Node A",
  );
});
