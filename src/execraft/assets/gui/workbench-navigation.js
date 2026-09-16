/**
 * Presentation-only navigation state for the graph-first Run workbench.
 *
 * This module deliberately owns no DOM and no orchestration semantics.  It is a
 * contract boundary between data/render refresh and explicit viewport movement.
 * GUI-B makes this controller authoritative for workflow selection, active-state
 * following, view mode, and explicit locate intent.
 */

export const WORKFLOW_VIEW_MODES = Object.freeze(["graph", "list"]);
export const WORKFLOW_SCALE_MIN = 0.08;
export const WORKFLOW_SCALE_MAX = 1.6;

function normalizeId(value) {
  return String(value || "").trim();
}

function normalizeIds(values) {
  return [...new Set((values || []).map(normalizeId).filter(Boolean))];
}

function finiteNumber(value, fallback) {
  const candidate = Number(value);
  return Number.isFinite(candidate) ? candidate : fallback;
}

/**
 * Normalize the serializable graph viewport contract.
 *
 * `x` and `y` are presentation scroll offsets in CSS pixels; `scale` is the
 * graph scale factor.  No workPackage identity is stored here, which prevents a
 * render refresh from smuggling a navigation intent into viewport state.
 */
export function workflowViewportState(value = {}) {
  return Object.freeze({
    x: Math.max(0, finiteNumber(value.x, 0)),
    y: Math.max(0, finiteNumber(value.y, 0)),
    scale: Math.min(
      WORKFLOW_SCALE_MAX,
      Math.max(WORKFLOW_SCALE_MIN, finiteNumber(value.scale, 1)),
    ),
  });
}

function viewMode(value) {
  const normalized = String(value || "").trim().toLowerCase();
  if (!WORKFLOW_VIEW_MODES.includes(normalized)) {
    throw new Error(`Unsupported workflow view mode: ${value}`);
  }
  return normalized;
}

/**
 * Explicit navigation intent separated from selection and rendering.
 *
 * The controller is intentionally small.  It does not know how a graph locates
 * a workPackage; callers consume `navigationRequest` and choose the appropriate
 * Graph/List implementation.  Selection and active-state updates never request
 * navigation unless Follow Active is enabled and the primary active workPackage
 * actually changes.
 */
export class WorkbenchNavigation {
  constructor({
    selectedId = "",
    activeIds = [],
    view = "graph",
    followActive = false,
    viewport = {},
  } = {}) {
    this.selectedId = normalizeId(selectedId);
    this.activeIds = normalizeIds(activeIds);
    this.viewMode = viewMode(view);
    this.followActive = Boolean(followActive);
    this.viewport = workflowViewportState(viewport);
    this.navigationRequest = null;
    this.navigationSequence = 0;
  }

  snapshot() {
    return Object.freeze({
      selectedId: this.selectedId,
      activeIds: Object.freeze([...this.activeIds]),
      viewMode: this.viewMode,
      followActive: this.followActive,
      viewport: this.viewport,
      navigationRequest: this.navigationRequest,
    });
  }

  select(id) {
    this.selectedId = normalizeId(id);
    return this.snapshot();
  }

  clearSelection() {
    this.selectedId = "";
    return this.snapshot();
  }

  setViewMode(value) {
    this.viewMode = viewMode(value);
    return this.snapshot();
  }

  setFollowActive(enabled) {
    this.followActive = Boolean(enabled);
    return this.snapshot();
  }

  setActive(ids) {
    const previousPrimary = this.activeIds[0] || "";
    this.activeIds = normalizeIds(ids);
    const primary = this.activeIds[0] || "";
    if (this.followActive && primary && primary !== previousPrimary) {
      this.requestLocate(primary, { reason: "follow-active" });
    }
    return this.snapshot();
  }

  setViewport(value) {
    this.viewport = workflowViewportState(value);
    return this.snapshot();
  }

  requestLocate(id, { reason = "operator" } = {}) {
    const targetId = normalizeId(id);
    if (!targetId) return null;
    this.navigationSequence += 1;
    this.navigationRequest = Object.freeze({
      sequence: this.navigationSequence,
      kind: "locate",
      targetId,
      reason: normalizeId(reason) || "operator",
    });
    return this.navigationRequest;
  }

  consumeNavigationRequest() {
    const request = this.navigationRequest;
    this.navigationRequest = null;
    return request;
  }
}
