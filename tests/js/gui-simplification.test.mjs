import assert from "node:assert/strict";

import { ExecutionTraceView } from "../../src/execraft/assets/gui/execution-trace-view.js";
import { compactCount } from "../../src/execraft/assets/gui/ui-utils.js";
import {
  acceptanceCriteriaMarkup,
  hasStructuredContent,
  structuredEvidence,
} from "../../src/execraft/assets/gui/work-package-presenter.js";

assert.equal(hasStructuredContent({}), false);
assert.equal(hasStructuredContent({ ok: true }), true);
assert.match(structuredEvidence("Result", { value: "<unsafe>" }), /&lt;unsafe&gt;/);
assert.match(
  acceptanceCriteriaMarkup([{ id: "A1", description: "Works", verified: true }]),
  /acceptance-item verified/,
);

assert.equal(compactCount(9585), "9,585");
assert.equal(compactCount(375619), "375.6K");
assert.equal(compactCount(24885741), "24.9M");
assert.equal(compactCount(-10), "0");

const trace = new ExecutionTraceView({
  api: async () => ({ lanes: [], summary: {} }),
  agentConsole: { open() {} },
  selectedPackageId: () => "other-package",
});
trace.cache.set("WP1", { trace: {}, loadedAt: Date.now() });
trace.selectedNodes.set("WP1", "node-1");
trace.modes.set("WP1", "attempts");
trace.reset();
assert.equal(trace.cache.size, 0);
assert.equal(trace.selectedNodes.size, 0);
assert.equal(trace.modes.size, 0);

const first = await trace.load("WP1");
assert.deepEqual(first, { lanes: [], summary: {} });
assert.equal(trace.cache.has("WP1"), true);

console.log("GUI simplification contracts: PASS");
