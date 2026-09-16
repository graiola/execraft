import assert from "node:assert/strict";
import test from "node:test";

import {
  fittedTimelineDomain,
  parseRoadmapDay,
  resizeSchedule,
  rowDropIntent,
  scheduleDurationDays,
  scheduleSummary,
  scheduleWithDuration,
  shiftSchedule,
} from "../../src/execraft/assets/gui/roadmap-interactions.js";

test("fitted timeline focuses scheduled work and fits it to the available track", () => {
  const start = parseRoadmapDay("2026-10-01");
  const target = parseRoadmapDay("2026-12-31");
  const domain = fittedTimelineDomain({
    minimumDay: start,
    maximumDay: target,
    viewportWidth: 1180,
    labelWidth: 196,
  });

  assert.ok(domain.start < start);
  assert.ok(domain.end > target);
  assert.equal(domain.fit, true);
  assert.ok(domain.width >= 976);
  assert.ok(domain.pixelsPerDay >= 1.25);
  assert.ok(domain.pixelsPerDay <= 18);
  assert.equal(fittedTimelineDomain({ minimumDay: NaN, maximumDay: target }), null);
});

test("phase group headers are view-only row-drop boundaries", () => {
  const rows = [
    { type: "group", groupKind: "phase", phase: { id: "foundation", title: "Foundation" } },
    { type: "item", item: { id: "task-a", lane: "Payload" } },
  ];
  assert.deepEqual(
    rowDropIntent({ rows, clientY: 10, bodyTop: 0, rowHeight: 32 }),
    { lane: "General", targetItemId: "", placement: "end", rowIndex: 0 },
  );
});

test("roadmap duration is inclusive and never persisted separately", () => {
  const schedule = { start: "2026-09-11", target: "2026-09-24" };
  assert.equal(scheduleDurationDays(schedule), 14);
  assert.equal(scheduleSummary(schedule), "2026-09-11 → 2026-09-24 · 14 days");
});

test("changing duration preserves start and recomputes target", () => {
  assert.deepEqual(
    scheduleWithDuration({ start: "2026-09-11", target: "2026-09-24" }, 20),
    { start: "2026-09-11", target: "2026-09-30" },
  );
});

test("moving a task preserves its duration", () => {
  const shifted = shiftSchedule({ start: "2026-09-11", target: "2026-09-24" }, 6);
  assert.deepEqual(shifted, { start: "2026-09-17", target: "2026-09-30" });
  assert.equal(scheduleDurationDays(shifted), 14);
});

test("edge resizing changes duration and clamps before crossing", () => {
  assert.deepEqual(
    resizeSchedule({ start: "2026-09-11", target: "2026-09-20" }, "target", 4),
    { start: "2026-09-11", target: "2026-09-24" },
  );
  assert.deepEqual(
    resizeSchedule({ start: "2026-09-11", target: "2026-09-20" }, "start", 99),
    { start: "2026-09-20", target: "2026-09-20" },
  );
});

test("roadmap date parser rejects rollover dates and duration validates bounds", () => {
  assert.equal(parseRoadmapDay("2026-02-31"), null);
  assert.throws(() => scheduleWithDuration({ start: "2026-09-11" }, 0), /whole number/);
  assert.throws(() => scheduleWithDuration({}, 5), /Set a task start date/);
});
