import shutil
import subprocess
from pathlib import Path

import pytest


NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="Node.js is optional for Python-only installs")
def test_workflow_routing_simplifies_ports_bundles_and_state():
    module_uri = (
        Path("src/execraft/assets/gui/workflow-routing.js").resolve().as_uri()
    )
    script = r"""
      import assert from "node:assert/strict";

      const routing = await import(process.argv[1]);
      assert.deepEqual(
        routing.orthogonalMidpointRoute(10, 20, 110, 80),
        [[10, 20], [60, 20], [60, 80], [110, 80]],
      );
      assert.deepEqual(
        routing.orthogonalMidpointRoute(10, 20, 50, 80),
        [[10, 20], [30, 20], [30, 80], [50, 80]],
      );
      assert.deepEqual(
        routing.orthogonalMidpointRoute(10, 20, 110, 20),
        [[10, 20], [110, 20]],
      );
      assert.deepEqual(
        routing.orthogonalMidpointRoute(10, 20, 110, 23),
        [[10, 21.5], [110, 21.5]],
      );
      assert.deepEqual(
        routing.orthogonalMidpointRoute(10, 20, 110, 42),
        [[10, 31], [110, 31]],
      );
      assert.deepEqual(
        routing.simplifyOrthogonalPoints([
          [10, 20], [10, 20], [40, 20], [60, 20], [60, 80], [60, 80],
        ]),
        [[10, 20], [60, 20], [60, 80]],
      );
      assert.equal(
        routing.orthogonalEdgePath([[10, 20], [40, 20], [60, 20], [60, 80]]),
        "M 10 20 L 60 20 L 60 80",
      );
      assert.deepEqual(
        routing.orderedPortCoordinates(100, 160, 3),
        [82, 100, 118],
      );

      const compactChain = routing.dependencyEdgesForDisplay([
        { id: "a", dependencies: [] },
        { id: "b", dependencies: ["a"] },
        { id: "c", dependencies: ["a", "b"] },
        { id: "d", dependencies: ["a", "b", "c"] },
      ]);
      assert.deepEqual(compactChain.edges, [
        { sourceId: "a", targetId: "b" },
        { sourceId: "b", targetId: "c" },
        { sourceId: "c", targetId: "d" },
      ]);
      assert.equal(compactChain.declaredCount, 6);
      assert.equal(compactChain.hiddenCount, 3);

      const allChain = routing.dependencyEdgesForDisplay(
        [
          { id: "a", dependencies: [] },
          { id: "b", dependencies: ["a"] },
          { id: "c", dependencies: ["a", "b"] },
          { id: "d", dependencies: ["a", "b", "c"] },
        ],
        { mode: "all" },
      );
      assert.equal(allChain.edges.length, 6);
      assert.equal(allChain.hiddenCount, 0);

      const compactDiamond = routing.dependencyEdgesForDisplay([
        { id: "root", dependencies: [] },
        { id: "left", dependencies: ["root"] },
        { id: "right", dependencies: ["root"] },
        { id: "join", dependencies: ["root", "left", "right"] },
      ]);
      assert.deepEqual(compactDiamond.edges, [
        { sourceId: "root", targetId: "left" },
        { sourceId: "root", targetId: "right" },
        { sourceId: "left", targetId: "join" },
        { sourceId: "right", targetId: "join" },
      ]);

      const filteredChain = routing.dependencyEdgesForDisplay(
        [
          { id: "a", dependencies: [] },
          { id: "b", dependencies: ["a"] },
          { id: "c", dependencies: ["a", "b"] },
        ],
        { visibleIds: new Set(["a", "c"]) },
      );
      assert.deepEqual(filteredChain.edges, [
        { sourceId: "a", targetId: "c" },
      ]);

      const fanOut = routing.bundleOrthogonalEdges([
        {
          key: "a>b", sourceId: "a", targetId: "b",
          sourceColumn: 0, targetColumn: 1,
          startX: 100, endX: 200,
          sourceCenterY: 100, targetCenterY: 60,
          sourceHeight: 160, targetHeight: 160,
          appearance: "default",
        },
        {
          key: "a>c", sourceId: "a", targetId: "c",
          sourceColumn: 0, targetColumn: 1,
          startX: 100, endX: 200,
          sourceCenterY: 100, targetCenterY: 140,
          sourceHeight: 160, targetHeight: 160,
          appearance: "selected-path",
        },
      ]);
      assert.equal(fanOut.length, 4);
      assert.deepEqual(fanOut[0].points, [[100, 100], [126, 100]]);
      assert.deepEqual(fanOut[1].points, [[126, 60], [126, 140]]);
      assert.equal(fanOut[0].appearance, "selected-path");
      assert.equal(fanOut.filter((route) => route.markerEnd).length, 2);
      assert.equal(
        fanOut.filter((route) => route.kind.includes("bundle-bus")).length,
        1,
      );

      for (const route of fanOut) {
        for (let index = 1; index < route.points.length; index += 1) {
          assert.notDeepEqual(route.points[index - 1], route.points[index]);
        }
      }

      const fanIn = routing.bundleOrthogonalEdges([
        {
          key: "b>e", sourceId: "b", targetId: "e",
          sourceColumn: 1, targetColumn: 2,
          startX: 300, endX: 400,
          sourceCenterY: 60, targetCenterY: 100,
          sourceHeight: 160, targetHeight: 160,
          appearance: "default",
        },
        {
          key: "c>e", sourceId: "c", targetId: "e",
          sourceColumn: 1, targetColumn: 2,
          startX: 300, endX: 400,
          sourceCenterY: 140, targetCenterY: 100,
          sourceHeight: 160, targetHeight: 160,
          appearance: "active-path",
        },
      ]);
      assert.equal(fanIn.length, 4);
      assert.equal(fanIn.filter((route) => route.markerEnd).length, 1);
      assert.equal(
        fanIn.filter((route) => route.kind.includes("source-branch")).length,
        2,
      );
      assert.equal(
        fanIn.find((route) => route.kind.includes("target-trunk")).appearance,
        "active-path",
      );

      const columns = routing.orderTopologicalColumns(
        [
          { id: "a", dependencies: [], priority: 1 },
          { id: "b", dependencies: [], priority: 2 },
          { id: "c", dependencies: ["a"] },
          { id: "d", dependencies: ["b"] },
        ],
        new Map([["a", 0], ["b", 0], ["c", 1], ["d", 1]]),
      );
      assert.deepEqual(columns.get(0).map((item) => item.id), ["a", "b"]);
      assert.deepEqual(columns.get(1).map((item) => item.id), ["c", "d"]);

      const common = {
        activeDependencyIds: new Set(["a", "b"]),
        workingIds: new Set(["b"]),
        selectedRelationshipIds: new Set(),
        hasSelectedFocus: false,
        hasActiveFocus: true,
      };
      assert.equal(
        routing.edgeAppearance({ sourceId: "a", targetId: "b", ...common }),
        "active-terminal",
      );
      assert.equal(
        routing.edgeAppearance({ sourceId: "x", targetId: "y", ...common }),
        "muted",
      );
    """
    result = subprocess.run(
        [NODE, "--input-type=module", "--eval", script, module_uri],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
