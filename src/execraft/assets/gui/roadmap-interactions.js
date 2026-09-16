/** Pure geometry and scheduling helpers for the direct-manipulation roadmap canvas. */

const DAY_MS = 24 * 60 * 60 * 1000;
const MAX_DURATION_DAYS = 3650;

export function parseRoadmapDay(value) {
  if (!value) return null;
  const parts = String(value).split("-").map(Number);
  if (parts.length !== 3 || parts.some((part) => !Number.isFinite(part))) return null;
  const [year, month, day] = parts;
  const timestamp = Date.UTC(year, month - 1, day);
  const date = new Date(timestamp);
  if (
    date.getUTCFullYear() !== year
    || date.getUTCMonth() !== month - 1
    || date.getUTCDate() !== day
  ) return null;
  return Math.floor(timestamp / DAY_MS);
}

export function formatRoadmapDay(day) {
  if (!Number.isFinite(day)) return "";
  return new Date(day * DAY_MS).toISOString().slice(0, 10);
}

export function addRoadmapDays(value, delta) {
  const day = parseRoadmapDay(value);
  return day === null ? "" : formatRoadmapDay(day + Number(delta || 0));
}

export function scheduleWindow(schedule = {}) {
  const start = parseRoadmapDay(schedule?.start);
  const target = parseRoadmapDay(schedule?.target);
  if (start === null && target === null) return null;
  const normalizedStart = start ?? target;
  const normalizedTarget = target ?? start;
  return {
    start: normalizedStart,
    target: normalizedTarget,
    point: start === null || target === null || normalizedStart === normalizedTarget,
    durationDays: normalizedTarget - normalizedStart + 1,
  };
}

export function scheduleDurationDays(schedule = {}) {
  return scheduleWindow(schedule)?.durationDays || 0;
}

export function scheduleWithDuration(schedule = {}, durationDays) {
  const rawDuration = Number(durationDays);
  if (!Number.isInteger(rawDuration) || rawDuration < 1 || rawDuration > MAX_DURATION_DAYS) {
    throw new Error(`Task duration must be a whole number from 1 to ${MAX_DURATION_DAYS} days`);
  }
  const window = scheduleWindow(schedule);
  if (!window) throw new Error("Set a task start date before changing its duration");
  const start = parseRoadmapDay(schedule?.start) ?? window.start;
  return {
    start: formatRoadmapDay(start),
    target: formatRoadmapDay(start + rawDuration - 1),
  };
}

export function shiftSchedule(schedule = {}, deltaDays) {
  const delta = Number(deltaDays || 0);
  return {
    ...(schedule?.start ? { start: addRoadmapDays(schedule.start, delta) } : {}),
    ...(schedule?.target ? { target: addRoadmapDays(schedule.target, delta) } : {}),
  };
}

export function resizeSchedule(schedule = {}, edge, deltaDays) {
  const window = scheduleWindow(schedule);
  if (!window) return {};
  const delta = Number(deltaDays || 0);
  let start = window.start;
  let target = window.target;
  if (edge === "start") start = Math.min(target, start + delta);
  else if (edge === "target") target = Math.max(start, target + delta);
  else throw new Error(`Unsupported roadmap resize edge: ${edge}`);
  return { start: formatRoadmapDay(start), target: formatRoadmapDay(target) };
}

export function scheduleSummary(schedule = {}) {
  const window = scheduleWindow(schedule);
  if (!window) return "unscheduled";
  const start = formatRoadmapDay(window.start);
  const target = formatRoadmapDay(window.target);
  const range = start === target ? start : `${start} → ${target}`;
  return `${range} · ${window.durationDays} day${window.durationDays === 1 ? "" : "s"}`;
}

/**
 * Build a plan-focused timeline domain that uses the available viewport instead
 * of forcing unrelated dates (notably "today") into the initial view.
 *
 * The returned track width excludes the sticky label column.  A small bounded
 * padding keeps point assets and resize handles away from the viewport edges,
 * while the pixels-per-day clamp prevents tiny plans from becoming enormous or
 * long plans from becoming unreadably compressed.
 */
export function fittedTimelineDomain({
  minimumDay,
  maximumDay,
  viewportWidth,
  labelWidth = 0,
  minimumTrackWidth = 320,
  minimumPixelsPerDay = 1.25,
  maximumPixelsPerDay = 18,
} = {}) {
  if (!Number.isFinite(minimumDay) || !Number.isFinite(maximumDay)) return null;
  const minimum = Math.min(minimumDay, maximumDay);
  const maximum = Math.max(minimumDay, maximumDay);
  const contentDays = Math.max(1, maximum - minimum + 1);
  const paddingDays = Math.max(3, Math.ceil(contentDays * 0.06));
  const start = minimum - paddingDays;
  const end = maximum + paddingDays;
  const totalDays = Math.max(1, end - start + 1);
  const trackWidth = Math.max(minimumTrackWidth, Number(viewportWidth || 0) - Number(labelWidth || 0) - 8);
  const pixelsPerDay = Math.max(
    minimumPixelsPerDay,
    Math.min(maximumPixelsPerDay, trackWidth / totalDays),
  );
  return {
    start,
    end,
    width: Math.max(trackWidth, Math.ceil(totalDays * pixelsPerDay)),
    pixelsPerDay,
    minimumDays: totalDays,
    paddingDays,
    fit: true,
  };
}

export function clientToTimelineDay({ clientX, canvasRect, labelWidth, domainStart, pixelsPerDay }) {
  const x = clientX - canvasRect.left - labelWidth;
  return Math.round(domainStart + Math.max(0, x / pixelsPerDay));
}

export function rowDropIntent({ rows, clientY, bodyTop, rowHeight, movingItemId = "" }) {
  if (!rows.length) return { lane: "General", targetItemId: "", placement: "end", rowIndex: 0 };
  const raw = Math.floor((clientY - bodyTop) / rowHeight);
  const rowIndex = Math.max(0, Math.min(rows.length - 1, raw));
  const row = rows[rowIndex];
  if (row.type !== "item") {
    return { lane: row.lane || "General", targetItemId: "", placement: "end", rowIndex };
  }
  if (row.item.id === movingItemId) {
    return { lane: row.item.lane || "General", targetItemId: "", placement: "end", rowIndex };
  }
  const rowTop = bodyTop + rowIndex * rowHeight;
  const placement = clientY < rowTop + rowHeight / 2 ? "before" : "after";
  return {
    lane: row.item.lane || "General",
    targetItemId: row.item.id,
    placement,
    rowIndex,
  };
}

export function laneDropIntent({ rows, clientY, bodyTop, rowHeight, movingLane = "" }) {
  const groups = [];
  for (let index = 0; index < rows.length; index += 1) {
    const row = rows[index];
    if (row.type !== "lane") continue;
    const nextLaneIndex = rows.findIndex((candidate, candidateIndex) => candidateIndex > index && candidate.type === "lane");
    groups.push({
      lane: row.lane,
      startIndex: index,
      endIndex: nextLaneIndex >= 0 ? nextLaneIndex - 1 : rows.length - 1,
    });
  }
  if (groups.length < 2) {
    return { targetLane: "", placement: "before", rowIndex: 0 };
  }
  const pointerRow = Math.max(0, Math.min(rows.length - 0.001, (clientY - bodyTop) / rowHeight));
  let target = groups.find((group) => pointerRow >= group.startIndex && pointerRow < group.endIndex + 1);
  if (!target) target = pointerRow < groups[0].startIndex ? groups[0] : groups[groups.length - 1];
  if (!target || target.lane === movingLane) {
    const own = groups.find((group) => group.lane === movingLane);
    return { targetLane: "", placement: "before", rowIndex: own?.startIndex ?? 0 };
  }
  const midpoint = (target.startIndex + target.endIndex + 1) / 2;
  const placement = pointerRow < midpoint ? "before" : "after";
  return {
    targetLane: target.lane,
    placement,
    rowIndex: placement === "before" ? target.startIndex : target.endIndex,
  };
}

export function connectorCurve(x1, y1, x2, y2) {
  const direction = Math.max(34, Math.abs(x2 - x1) * 0.42);
  const c1 = x1 + direction;
  const c2 = x2 - direction;
  return `M ${x1} ${y1} C ${c1} ${y1}, ${c2} ${y2}, ${x2} ${y2}`;
}

export function visualItemId(kind) {
  const nonce = globalThis.crypto?.getRandomValues
    ? Array.from(globalThis.crypto.getRandomValues(new Uint32Array(2)), (value) => value.toString(36)).join("")
    : Math.random().toString(36).slice(2);
  return `${String(kind || "item").replace(/_/g, "-")}-${Date.now().toString(36)}-${nonce.slice(0, 10)}`;
}
