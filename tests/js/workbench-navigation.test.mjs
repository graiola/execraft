import assert from "node:assert/strict";
import test from "node:test";

import {
  WorkbenchNavigation,
  workflowViewportState,
} from "../../src/execraft/assets/gui/workbench-navigation.js";

test("selection is state only and does not request navigation", () => {
  const navigation = new WorkbenchNavigation({ view: "graph" });
  navigation.select("WP17");

  assert.equal(navigation.snapshot().selectedId, "WP17");
  assert.equal(navigation.consumeNavigationRequest(), null);
});

test("render-safe active updates do not navigate when follow active is off", () => {
  const navigation = new WorkbenchNavigation({ activeIds: ["WP17"] });
  navigation.setActive(["WP18"]);

  assert.deepEqual(navigation.snapshot().activeIds, ["WP18"]);
  assert.equal(navigation.consumeNavigationRequest(), null);
});

test("follow active emits exactly one explicit locate request per primary transition", () => {
  const navigation = new WorkbenchNavigation({
    activeIds: ["WP17"],
    followActive: true,
  });

  navigation.setActive(["WP18"]);
  const request = navigation.consumeNavigationRequest();
  assert.equal(request.kind, "locate");
  assert.equal(request.targetId, "WP18");
  assert.equal(request.reason, "follow-active");
  assert.equal(navigation.consumeNavigationRequest(), null);

  navigation.setActive(["WP18"]);
  assert.equal(navigation.consumeNavigationRequest(), null);
});

test("explicit locate is distinct from selected Work Package", () => {
  const navigation = new WorkbenchNavigation({ selectedId: "WP17" });
  navigation.requestLocate("WP12", { reason: "operator" });

  assert.equal(navigation.snapshot().selectedId, "WP17");
  assert.equal(navigation.consumeNavigationRequest().targetId, "WP12");
});

test("viewport state is serializable and independent from Work Package state", () => {
  const viewport = workflowViewportState({ x: 120, y: 35, scale: 0.8 });
  assert.deepEqual(viewport, { x: 120, y: 35, scale: 0.8 });

  const navigation = new WorkbenchNavigation({ viewport });
  navigation.select("WP17");
  assert.deepEqual(navigation.snapshot().viewport, viewport);
});

test("graph is the contract default and view changes never imply navigation", () => {
  const navigation = new WorkbenchNavigation();
  assert.equal(navigation.snapshot().viewMode, "graph");

  navigation.setViewMode("list");
  assert.equal(navigation.snapshot().viewMode, "list");
  assert.equal(navigation.consumeNavigationRequest(), null);
});

test("follow active policy can be toggled without moving the current viewport", () => {
  const navigation = new WorkbenchNavigation({
    activeIds: ["WP17"],
    viewport: { x: 240, y: 90, scale: 0.72 },
  });

  navigation.setFollowActive(true);
  assert.equal(navigation.consumeNavigationRequest(), null);
  assert.deepEqual(navigation.snapshot().viewport, {
    x: 240,
    y: 90,
    scale: 0.72,
  });
});
