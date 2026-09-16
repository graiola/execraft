const COORDINATE_PRECISION = 3;
const ROUTE_EPSILON = 0.01;
const STRAIGHT_ALIGNMENT_TOLERANCE = 24;
const DEFAULT_GRID = 0.5;
const DEFAULT_PORT_INSET = 24;
const DEFAULT_PORT_SPACING = 18;
const DEFAULT_MINIMUM_LEAD = 26;
const DEFAULT_LANE_SPACING = 10;

const APPEARANCE_PRIORITY = {
  muted: 0,
  default: 1,
  "selected-path": 2,
  "active-path": 3,
  "active-terminal": 4,
};

function roundedCoordinate(value) {
  return Number(value.toFixed(COORDINATE_PRECISION));
}

function snapCoordinate(value, grid = DEFAULT_GRID) {
  if (!Number.isFinite(value)) return 0;
  if (!grid) return roundedCoordinate(value);
  return roundedCoordinate(Math.round(value / grid) * grid);
}

function sameCoordinate(left, right) {
  return Math.abs(left - right) <= ROUTE_EPSILON;
}

function samePoint(left, right) {
  return sameCoordinate(left[0], right[0]) && sameCoordinate(left[1], right[1]);
}

function isCollinear(previous, current, next) {
  return (
    (sameCoordinate(previous[0], current[0]) &&
      sameCoordinate(current[0], next[0])) ||
    (sameCoordinate(previous[1], current[1]) &&
      sameCoordinate(current[1], next[1]))
  );
}

/**
 * Snap connector points to a stable sub-pixel grid and remove duplicate or
 * collinear vertices. The returned route never contains zero-length segments.
 */
export function simplifyOrthogonalPoints(points, grid = DEFAULT_GRID) {
  const simplified = [];
  for (const [rawX, rawY] of points || []) {
    const point = [snapCoordinate(rawX, grid), snapCoordinate(rawY, grid)];
    if (simplified.length && samePoint(simplified.at(-1), point)) continue;
    simplified.push(point);
    while (
      simplified.length >= 3 &&
      isCollinear(
        simplified.at(-3),
        simplified.at(-2),
        simplified.at(-1),
      )
    ) {
      simplified.splice(-2, 1);
    }
  }
  return simplified;
}

/**
 * Compact route for an unbundled dependency. Vertically aligned ports are
 * connected by one straight segment; other routes use one midpoint dogleg.
 */
export function orthogonalMidpointRoute(
  startX,
  startY,
  endX,
  endY,
  minimumLead = DEFAULT_MINIMUM_LEAD,
) {
  if (Math.abs(startY - endY) <= STRAIGHT_ALIGNMENT_TOLERANCE) {
    const alignedY = (startY + endY) / 2;
    return simplifyOrthogonalPoints([
      [startX, alignedY],
      [endX, alignedY],
    ]);
  }

  const horizontalRoom = Math.max(0, endX - startX);
  const requestedLead = Math.min(minimumLead, horizontalRoom / 2);
  const midpoint = startX + horizontalRoom / 2;
  const bendX = Math.max(startX + requestedLead, midpoint);
  return simplifyOrthogonalPoints([
    [startX, startY],
    [bendX, startY],
    [bendX, endY],
    [endX, endY],
  ]);
}

export function orthogonalEdgePath(points) {
  return simplifyOrthogonalPoints(points)
    .map(([x, y], index) => `${index ? "L" : "M"} ${x} ${y}`)
    .join(" ");
}

/** Allocate compact, ordered ports over the useful height of one card edge. */
export function orderedPortCoordinates(
  centerY,
  height,
  count,
  { inset = DEFAULT_PORT_INSET, spacing = DEFAULT_PORT_SPACING } = {},
) {
  if (count <= 1) return [snapCoordinate(centerY)];
  const usableHeight = Math.max(0, height - inset * 2);
  const span = Math.min(usableHeight, spacing * (count - 1));
  const start = centerY - span / 2;
  return Array.from({ length: count }, (_, index) =>
    snapCoordinate(start + (span * index) / (count - 1)),
  );
}

function edgeIdentity(edge, index) {
  return edge.key || `${edge.sourceId}>${edge.targetId}#${index}`;
}

function groupBy(items, keyForItem) {
  const groups = new Map();
  for (const item of items) {
    const key = keyForItem(item);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(item);
  }
  return groups;
}

function dependencyReachability(byId) {
  const cache = new Map();
  return (startId, dependencyId) => {
    const key = `${startId}>${dependencyId}`;
    if (cache.has(key)) return cache.get(key);
    const pending = [startId];
    const visited = new Set();
    let found = false;
    while (pending.length && !found) {
      const currentId = pending.pop();
      if (visited.has(currentId)) continue;
      visited.add(currentId);
      const current = byId.get(currentId);
      for (const candidateId of current?.dependencies || []) {
        if (candidateId === dependencyId) {
          found = true;
          break;
        }
        if (byId.has(candidateId) && !visited.has(candidateId)) {
          pending.push(candidateId);
        }
      }
    }
    cache.set(key, found);
    return found;
  };
}

/**
 * Select dependency relations for visualisation without changing the durable
 * package contract. In compact mode, a direct dependency is hidden when a
 * different direct dependency already reaches it through the graph. This is a
 * transitive reduction of each target's declared incoming edges: the workflow
 * keeps the same ordering semantics while avoiding visually redundant links.
 */
export function dependencyEdgesForDisplay(
  packages,
  { visibleIds = null, mode = "essential" } = {},
) {
  const items = packages || [];
  const byId = new Map(items.map((item) => [item.id, item]));
  const visible = visibleIds ? new Set(visibleIds) : new Set(byId.keys());
  const dependsOn = dependencyReachability(byId);
  const edges = [];
  let declaredCount = 0;

  for (const target of items) {
    if (!visible.has(target.id)) continue;
    const directDependencies = [
      ...new Set(
        (target.dependencies || []).filter(
          (dependencyId) =>
            dependencyId !== target.id &&
            visible.has(dependencyId) &&
            byId.has(dependencyId),
        ),
      ),
    ];
    declaredCount += directDependencies.length;

    const displayedDependencies =
      mode === "all"
        ? directDependencies
        : directDependencies.filter(
            (dependencyId) =>
              !directDependencies.some(
                (otherId) =>
                  otherId !== dependencyId &&
                  dependsOn(otherId, dependencyId) &&
                  !dependsOn(dependencyId, otherId),
              ),
          );

    displayedDependencies.forEach((sourceId) =>
      edges.push({ sourceId, targetId: target.id }),
    );
  }

  return {
    mode: mode === "all" ? "all" : "essential",
    edges,
    declaredCount,
    hiddenCount: Math.max(0, declaredCount - edges.length),
  };
}

function strongestAppearance(edges) {
  return edges.reduce((strongest, edge) => {
    const candidate = edge.appearance || "default";
    return (APPEARANCE_PRIORITY[candidate] ?? 1) >
      (APPEARANCE_PRIORITY[strongest] ?? 1)
      ? candidate
      : strongest;
  }, "muted");
}

function bundleCandidates(edges) {
  const candidates = [];
  const sourceGroups = groupBy(
    edges,
    (edge) => `${edge.sourceId}|${edge.targetColumn}`,
  );
  const targetGroups = groupBy(
    edges,
    (edge) => `${edge.sourceColumn}|${edge.targetId}`,
  );
  for (const [key, members] of sourceGroups) {
    if (members.length >= 2) candidates.push({ type: "source", key, members });
  }
  for (const [key, members] of targetGroups) {
    if (members.length >= 2) candidates.push({ type: "target", key, members });
  }
  return candidates.sort(
    (left, right) =>
      right.members.length - left.members.length ||
      Number(left.type === "target") - Number(right.type === "target") ||
      left.key.localeCompare(right.key),
  );
}

function selectDisjointBundles(edges) {
  const assigned = new Map();
  const bundles = [];
  for (const candidate of bundleCandidates(edges)) {
    const members = candidate.members.filter((edge) => !assigned.has(edge.key));
    if (members.length < 2) continue;
    const id = `bundle-${candidate.type}-${bundles.length}`;
    const bundle = { ...candidate, id, members };
    bundles.push(bundle);
    members.forEach((edge) => assigned.set(edge.key, bundle));
  }
  return { bundles, assigned };
}

function unitForEdge(edge, bundle, side) {
  if (bundle?.type === side) return bundle.id;
  return `edge-${edge.key}`;
}

function allocatePorts(edges, assigned, side) {
  const unitsByNode = new Map();
  const nodeKey = side === "source" ? "sourceId" : "targetId";
  const centerKey = side === "source" ? "sourceCenterY" : "targetCenterY";
  const heightKey = side === "source" ? "sourceHeight" : "targetHeight";
  const oppositeCenterKey =
    side === "source" ? "targetCenterY" : "sourceCenterY";

  for (const edge of edges) {
    const bundle = assigned.get(edge.key);
    const unitId = unitForEdge(edge, bundle, side);
    const nodeId = edge[nodeKey];
    if (!unitsByNode.has(nodeId)) unitsByNode.set(nodeId, new Map());
    const nodeUnits = unitsByNode.get(nodeId);
    if (!nodeUnits.has(unitId)) {
      nodeUnits.set(unitId, {
        id: unitId,
        centerY: edge[centerKey],
        height: edge[heightKey],
        sortValues: [],
      });
    }
    nodeUnits.get(unitId).sortValues.push(edge[oppositeCenterKey]);
  }

  const assignments = new Map();
  for (const units of unitsByNode.values()) {
    const ordered = [...units.values()].sort((left, right) => {
      const leftAverage =
        left.sortValues.reduce((sum, value) => sum + value, 0) /
        left.sortValues.length;
      const rightAverage =
        right.sortValues.reduce((sum, value) => sum + value, 0) /
        right.sortValues.length;
      return leftAverage - rightAverage || left.id.localeCompare(right.id);
    });
    const coordinates = orderedPortCoordinates(
      ordered[0].centerY,
      ordered[0].height,
      ordered.length,
    );
    ordered.forEach((unit, index) => assignments.set(unit.id, coordinates[index]));
  }
  return assignments;
}

function assignBundleLanes(bundles, minimumLead, laneSpacing) {
  const byColumnPair = groupBy(
    bundles,
    (bundle) =>
      `${bundle.members[0].sourceColumn}|${bundle.members[0].targetColumn}`,
  );
  const positions = new Map();

  for (const pairBundles of byColumnPair.values()) {
    const sourceBundles = pairBundles
      .filter((bundle) => bundle.type === "source")
      .sort(
        (left, right) =>
          left.members[0].sourceCenterY - right.members[0].sourceCenterY ||
          left.id.localeCompare(right.id),
      );
    const targetBundles = pairBundles
      .filter((bundle) => bundle.type === "target")
      .sort(
        (left, right) =>
          left.members[0].targetCenterY - right.members[0].targetCenterY ||
          left.id.localeCompare(right.id),
      );
    const allMembers = pairBundles.flatMap((bundle) => bundle.members);
    const corridorLeft =
      Math.max(...allMembers.map((edge) => edge.startX)) + minimumLead;
    const corridorRight =
      Math.min(...allMembers.map((edge) => edge.endX)) - minimumLead;
    const total = sourceBundles.length + targetBundles.length;

    if (corridorRight <= corridorLeft || total === 0) {
      const midpoint = snapCoordinate((corridorLeft + corridorRight) / 2);
      pairBundles.forEach((bundle) => positions.set(bundle.id, midpoint));
      continue;
    }

    const requiredSpan = Math.max(0, total - 1) * laneSpacing;
    if (requiredSpan <= corridorRight - corridorLeft) {
      sourceBundles.forEach((bundle, index) =>
        positions.set(bundle.id, snapCoordinate(corridorLeft + index * laneSpacing)),
      );
      targetBundles.forEach((bundle, index) =>
        positions.set(bundle.id, snapCoordinate(corridorRight - index * laneSpacing)),
      );
      continue;
    }

    const ordered = [...sourceBundles, ...targetBundles.reverse()];
    ordered.forEach((bundle, index) => {
      const fraction = (index + 1) / (ordered.length + 1);
      positions.set(
        bundle.id,
        snapCoordinate(corridorLeft + (corridorRight - corridorLeft) * fraction),
      );
    });
  }
  return positions;
}

function memberLabel(edges) {
  return edges.map((edge) => `${edge.sourceId}>${edge.targetId}`).join(",");
}

function routeDescriptor({
  points,
  appearance,
  markerEnd = false,
  kind = "edge",
  members,
}) {
  const simplified = simplifyOrthogonalPoints(points);
  if (simplified.length < 2) return null;
  return {
    points: simplified,
    appearance: appearance || "default",
    markerEnd,
    kind,
    sourceId: members[0].sourceId,
    targetId: members[0].targetId,
    members: memberLabel(members),
  };
}

/**
 * Plan compact orthogonal geometry for a complete workflow.
 *
 * Fan-out edges sharing a source and destination column use one outgoing trunk
 * and one vertical bus. Fan-in edges sharing a target and source column use one
 * incoming trunk and one vertical bus. Every remaining edge gets a simplified
 * midpoint route. Bundles are disjoint so no relationship is rendered twice.
 */
export function bundleOrthogonalEdges(
  inputEdges,
  {
    minimumLead = DEFAULT_MINIMUM_LEAD,
    laneSpacing = DEFAULT_LANE_SPACING,
  } = {},
) {
  const edges = (inputEdges || []).map((edge, index) => ({
    ...edge,
    key: edgeIdentity(edge, index),
  }));
  if (!edges.length) return [];

  const { bundles, assigned } = selectDisjointBundles(edges);
  const sourcePorts = allocatePorts(edges, assigned, "source");
  const targetPorts = allocatePorts(edges, assigned, "target");
  const bundleLanes = assignBundleLanes(bundles, minimumLead, laneSpacing);
  const routes = [];
  const pushRoute = (descriptor) => {
    if (descriptor) routes.push(descriptor);
  };

  for (const bundle of bundles) {
    const appearance = strongestAppearance(bundle.members);
    const busX = bundleLanes.get(bundle.id);
    if (bundle.type === "source") {
      const first = bundle.members[0];
      const startY = sourcePorts.get(bundle.id);
      const targetYs = bundle.members.map((edge) =>
        targetPorts.get(unitForEdge(edge, assigned.get(edge.key), "target")),
      );
      pushRoute(
        routeDescriptor({
          points: [
            [first.startX, startY],
            [busX, startY],
          ],
          appearance,
          kind: "bundle-trunk source-trunk",
          members: bundle.members,
        }),
      );
      pushRoute(
        routeDescriptor({
          points: [
            [busX, Math.min(startY, ...targetYs)],
            [busX, Math.max(startY, ...targetYs)],
          ],
          appearance,
          kind: "bundle-bus source-bus",
          members: bundle.members,
        }),
      );
      bundle.members.forEach((edge, index) => {
        pushRoute(
          routeDescriptor({
            points: [
              [busX, targetYs[index]],
              [edge.endX, targetYs[index]],
            ],
            appearance: edge.appearance,
            markerEnd: true,
            kind: "bundle-branch target-branch",
            members: [edge],
          }),
        );
      });
      continue;
    }

    const first = bundle.members[0];
    const endY = targetPorts.get(bundle.id);
    const sourceYs = bundle.members.map((edge) =>
      sourcePorts.get(unitForEdge(edge, assigned.get(edge.key), "source")),
    );
    bundle.members.forEach((edge, index) => {
      pushRoute(
        routeDescriptor({
          points: [
            [edge.startX, sourceYs[index]],
            [busX, sourceYs[index]],
          ],
          appearance: edge.appearance,
          kind: "bundle-branch source-branch",
          members: [edge],
        }),
      );
    });
    pushRoute(
      routeDescriptor({
        points: [
          [busX, Math.min(endY, ...sourceYs)],
          [busX, Math.max(endY, ...sourceYs)],
        ],
        appearance,
        kind: "bundle-bus target-bus",
        members: bundle.members,
      }),
    );
    pushRoute(
      routeDescriptor({
        points: [
          [busX, endY],
          [first.endX, endY],
        ],
        appearance,
        markerEnd: true,
        kind: "bundle-trunk target-trunk",
        members: bundle.members,
      }),
    );
  }

  for (const edge of edges) {
    if (assigned.has(edge.key)) continue;
    const startY = sourcePorts.get(`edge-${edge.key}`);
    const endY = targetPorts.get(`edge-${edge.key}`);
    pushRoute(
      routeDescriptor({
        points: orthogonalMidpointRoute(
          edge.startX,
          startY,
          edge.endX,
          endY,
          minimumLead,
        ),
        appearance: edge.appearance,
        markerEnd: true,
        members: [edge],
      }),
    );
  }

  return routes;
}

function packageBaseOrder(left, right) {
  return (
    Number(Boolean(left.parent_id)) - Number(Boolean(right.parent_id)) ||
    (right.priority || 0) - (left.priority || 0) ||
    left.id.localeCompare(right.id)
  );
}

function average(values) {
  return values.length
    ? values.reduce((sum, value) => sum + value, 0) / values.length
    : Number.POSITIVE_INFINITY;
}

function positionMap(columns) {
  const positions = new Map();
  for (const items of columns.values()) {
    items.forEach((item, index) => positions.set(item.id, index));
  }
  return positions;
}

/**
 * Reorder cards inside topological columns with two barycenter sweeps. This
 * keeps deterministic priority/id ties while reducing avoidable crossings.
 */
export function orderTopologicalColumns(packages, levelById) {
  const columns = new Map();
  const byId = new Map(packages.map((item) => [item.id, item]));
  const dependents = new Map();
  for (const item of packages) {
    const column = levelById.get(item.id) || 0;
    if (!columns.has(column)) columns.set(column, []);
    columns.get(column).push(item);
    for (const dependencyId of item.dependencies || []) {
      if (!byId.has(dependencyId)) continue;
      if (!dependents.has(dependencyId)) dependents.set(dependencyId, []);
      dependents.get(dependencyId).push(item.id);
    }
  }
  for (const items of columns.values()) items.sort(packageBaseOrder);

  const orderedColumnIds = [...columns.keys()].sort((left, right) => left - right);
  for (let sweep = 0; sweep < 2; sweep += 1) {
    let positions = positionMap(columns);
    for (const columnId of orderedColumnIds) {
      const items = columns.get(columnId);
      items.sort((left, right) => {
        const leftBarycenter = average(
          (left.dependencies || [])
            .filter((id) => positions.has(id))
            .map((id) => positions.get(id)),
        );
        const rightBarycenter = average(
          (right.dependencies || [])
            .filter((id) => positions.has(id))
            .map((id) => positions.get(id)),
        );
        return leftBarycenter - rightBarycenter || packageBaseOrder(left, right);
      });
      positions = positionMap(columns);
    }

    positions = positionMap(columns);
    for (const columnId of [...orderedColumnIds].reverse()) {
      const items = columns.get(columnId);
      items.sort((left, right) => {
        const leftBarycenter = average(
          (dependents.get(left.id) || [])
            .filter((id) => positions.has(id))
            .map((id) => positions.get(id)),
        );
        const rightBarycenter = average(
          (dependents.get(right.id) || [])
            .filter((id) => positions.has(id))
            .map((id) => positions.get(id)),
        );
        return leftBarycenter - rightBarycenter || packageBaseOrder(left, right);
      });
      positions = positionMap(columns);
    }
  }
  return columns;
}

export function edgeAppearance({
  sourceId,
  targetId,
  activeDependencyIds,
  workingIds,
  selectedRelationshipIds,
  hasSelectedFocus,
  hasActiveFocus,
}) {
  const activePath =
    activeDependencyIds.has(sourceId) && activeDependencyIds.has(targetId);
  if (activePath) {
    return workingIds.has(targetId) ? "active-terminal" : "active-path";
  }
  if (
    hasSelectedFocus &&
    selectedRelationshipIds.has(sourceId) &&
    selectedRelationshipIds.has(targetId)
  ) {
    return "selected-path";
  }
  return hasSelectedFocus || hasActiveFocus ? "muted" : "default";
}
