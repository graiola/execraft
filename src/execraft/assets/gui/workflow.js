import { escapeHtml, truncateText } from "./ui-utils.js";
import {
  bundleOrthogonalEdges,
  dependencyEdgesForDisplay,
  edgeAppearance,
  orderTopologicalColumns,
  orthogonalEdgePath,
} from "./workflow-routing.js";

const STAGE_LABELS = {
  prepare: "Queued",
  decompose: "Planning",
  implement: "Implementing",
  fast_verify: "Fast verify",
  verify: "Verifying",
  regression_verify: "Regression verify",
  review: "Review",
  fix_review: "Fixing review",
  final_review: "Final review",
  full_verify: "Full verify",
  ready_to_commit: "Ready to commit",
  acceptance: "Acceptance",
  commit: "Commit",
  completed: "Completed",
};

function packageVisible(packageInfo, filter) {
  if (filter === "remaining") return packageInfo.stage !== "completed";
  if (filter === "active") {
    return (
      packageInfo.operator_paused ||
      packageInfo.pause_before_start ||
      packageInfo.pause_before_start_reached_at ||
      packageInfo.decomposition_required ||
      !["prepare", "completed"].includes(packageInfo.stage)
    );
  }
  if (filter === "shards") return Boolean(packageInfo.parent_id);
  return true;
}

export function assignedAgent(packageInfo) {
  if (packageInfo.stage === "decompose")
    return packageInfo.decomposition_agent_id;
  if (["review", "final_review"].includes(packageInfo.stage)) {
    return packageInfo.final_reviewer_id || packageInfo.reviewer_id;
  }
  if (packageInfo.stage === "fix_review") return packageInfo.last_fixer_id;
  return packageInfo.agent_id;
}

function stageClass(packageInfo) {
  if (packageInfo.operator_paused) return "paused";
  if (packageInfo.stage === "completed") return "completed";
  if (packageInfo.status === "human_required") return "human-required";
  if (["failed", "error"].includes(packageInfo.status)) return "failed";
  if (["waiting", "blocked"].includes(packageInfo.status)) return "waiting";
  if (!["prepare", "completed"].includes(packageInfo.stage)) return "active";
  return "queued";
}

function topologicalLevels(packages) {
  const byId = new Map(packages.map((item) => [item.id, item]));
  const memo = new Map();
  const level = (item, stack = new Set()) => {
    if (memo.has(item.id)) return memo.get(item.id);
    if (stack.has(item.id)) return 0;
    const nextStack = new Set(stack);
    nextStack.add(item.id);
    let result = 0;
    for (const dependencyId of item.dependencies || []) {
      const dependency = byId.get(dependencyId);
      if (dependency)
        result = Math.max(result, level(dependency, nextStack) + 1);
    }
    memo.set(item.id, result);
    return result;
  };
  packages.forEach((item) => level(item));
  return memo;
}

function dependencyClosure(packages, focusIds) {
  const byId = new Map(packages.map((item) => [item.id, item]));
  const result = new Set(focusIds);
  const visit = (id) => {
    const item = byId.get(id);
    if (!item) return;
    for (const dependencyId of item.dependencies || []) {
      if (result.has(dependencyId)) continue;
      result.add(dependencyId);
      visit(dependencyId);
    }
  };
  focusIds.forEach(visit);
  return result;
}

function relationshipClosure(packages, focusIds) {
  const upstream = dependencyClosure(packages, focusIds);
  const downstream = new Map();
  for (const item of packages) {
    for (const dependencyId of item.dependencies || []) {
      if (!downstream.has(dependencyId)) downstream.set(dependencyId, []);
      downstream.get(dependencyId).push(item.id);
    }
  }
  const related = new Set(upstream);
  const visit = (id) => {
    for (const childId of downstream.get(id) || []) {
      if (related.has(childId)) continue;
      related.add(childId);
      visit(childId);
    }
  };
  focusIds.forEach(visit);
  return related;
}

function stageLabel(packageInfo) {
  return STAGE_LABELS[packageInfo.stage] || packageInfo.stage || "Unknown";
}

function workPackageStateGlyph(packageInfo) {
  const state = stageClass(packageInfo);
  if (state === "completed") return "✓";
  if (["failed", "human-required"].includes(state)) return "!";
  if (state === "active") return "●";
  if (state === "paused") return "Ⅱ";
  return "○";
}

function workPackageDirectiveSummary(packageInfo) {
  const directives = [];
  if (packageInfo.pause_before_start) directives.push("pause before start");
  if (packageInfo.decomposition_required) directives.push("decomposition required");
  if ((packageInfo.directive_pending_sync || []).length) directives.push("pending directive update");
  if (packageInfo.repository_sync_requested) directives.push("repository sync queued");
  return {
    count: directives.length,
    title: directives.join(" · "),
  };
}

function workPackageQuickActions(packageInfo, { controlsLocked = false, list = false } = {}) {
  if (!packageInfo || packageInfo.stage === "completed") return "";
  const future = packageInfo.stage === "prepare" && packageInfo.status === "pending";
  const paused = Boolean(packageInfo.operator_paused || packageInfo.pause_before_start);
  const pauseDisabled = future
    ? Boolean(packageInfo.pause_before_start_reached_at)
    : Boolean(controlsLocked);
  const syncEligible = !packageInfo.parent_id && packageInfo.kind !== "repository_sync";
  const actionClass = list ? "btn small work-package-list-action" : "work-package-action";
  const buttons = [
    `<button type="button" class="${actionClass} pause" data-work-package-action="${paused ? "resume" : "pause"}" data-id="${escapeHtml(packageInfo.id)}" ${pauseDisabled ? "disabled" : ""} title="${future ? (paused ? "Remove the scheduled entry pause" : "Pause when this Work Package becomes ready") : paused ? "Resume this Work Package" : "Pause this Work Package"}">${paused ? "Resume" : "Pause"}</button>`,
    syncEligible
      ? `<button type="button" class="${actionClass}" data-work-package-action="repository-sync" data-id="${escapeHtml(packageInfo.id)}" title="${future ? "Synchronize repositories before this Work Package" : "Pause and synchronize repositories at a safe boundary"}">${packageInfo.repository_sync_requested ? "Edit sync" : "Sync"}</button>`
      : "",
    `<button type="button" class="${actionClass} details" data-work-package-action="execution" data-id="${escapeHtml(packageInfo.id)}" title="Open Work Package execution and routing">Execution</button>`,
    assignedAgent(packageInfo)
      ? `<button type="button" class="${actionClass}" data-work-package-action="agent" data-id="${escapeHtml(packageInfo.id)}" title="Open the assigned agent">Agent</button>`
      : "",
  ].filter(Boolean);
  return buttons.join("");
}

function listItemMarkup(item, { working, selected, activeParallel, controlsLocked = false }) {
  const state = stageClass(item);
  const stage = item.operator_paused ? "Paused" : stageLabel(item);
  const agent = assignedAgent(item) || "Automatic";
  const dependencies = (item.dependencies || []).join(", ");
  const directive = workPackageDirectiveSummary(item);
  const tags = [
    working ? '<span class="work-package-list-tag current">Working now</span>' : "",
    item.operator_paused || item.pause_before_start_reached_at
      ? '<span class="work-package-list-tag attention">Action required</span>'
      : "",
    activeParallel ? '<span class="work-package-list-tag parallel">Parallel wave</span>' : "",
    item.parent_id
      ? `<span class="work-package-list-tag shard">Shard of ${escapeHtml(item.parent_id)}</span>`
      : "",
    directive.count
      ? `<span class="work-package-list-tag directive" title="${escapeHtml(directive.title)}">${directive.count} directive${directive.count === 1 ? "" : "s"}</span>`
      : "",
  ]
    .filter(Boolean)
    .join("");
  return `
    <article class="work-package-list-item ${state}${working ? " working-now" : ""}${selected ? " selected" : ""}" data-id="${escapeHtml(item.id)}">
      <button type="button" class="work-package-list-main" data-work-package-action="details" data-id="${escapeHtml(item.id)}" aria-label="Open details for ${escapeHtml(item.id)}">
        <span class="work-package-state-glyph" aria-hidden="true">${workPackageStateGlyph(item)}</span>
        <span class="work-package-list-copy">
          <span class="work-package-list-title"><strong>${escapeHtml(item.id)}</strong><b>${escapeHtml(item.title)}</b></span>
          <span class="work-package-list-meta">${escapeHtml(stage)} · ${escapeHtml(truncateText(agent, 42))}${dependencies ? ` · needs ${escapeHtml(truncateText(dependencies, 72))}` : ""}</span>
          ${tags ? `<span class="work-package-list-tags">${tags}</span>` : ""}
        </span>
      </button>
      <div class="work-package-list-actions">${workPackageQuickActions(item, { controlsLocked, list: true })}</div>
    </article>`;
}

/**
 * Render the optional compact List representation of the workflow.
 *
 * Graph is the primary Run workspace. List keeps the same workPackage selection
 * and inspector actions for operators who want a compact topological scan. It
 * deliberately exposes only inspection and agent-opening actions; scheduling
 * directives remain in the workPackage detail surface.
 */
export class WorkflowList {
  constructor({ container, onAction = null }) {
    if (!container) throw new Error("WorkflowList requires a container");
    this.container = container;
    this.onAction = onAction || (() => {});
    this.handleActionEvent = (event) => this.#handleAction(event);
    this.container.addEventListener("click", this.handleActionEvent);
  }

  render({
    packages,
    filter = "all",
    selectedId = "",
    activeParallelIds = [],
    workingPackageIds = [],
    controlsLocked = false,
  }) {
    const allPackages = packages || [];
    const visiblePackages = allPackages.filter((item) =>
      packageVisible(item, filter),
    );
    if (!visiblePackages.length) {
      this.container.innerHTML =
        '<div class="workflow-empty-card">No Work Packages match this filter.</div>';
      return;
    }
    const working = new Set(workingPackageIds || []);
    const activeParallel = new Set(activeParallelIds || []);
    const levels = topologicalLevels(allPackages);
    const columns = orderTopologicalColumns(visiblePackages, levels);
    this.container.innerHTML = [...columns.entries()]
      .sort((left, right) => left[0] - right[0])
      .map(
        ([level, items]) => `
          <section class="work-package-list-step" data-step="${level + 1}">
            <header><span>Step ${level + 1}</span><small>${items.length === 1 ? "1 Work Package" : `${items.length} Work Packages`}</small></header>
            <div>
              ${items
                .map((item) =>
                  listItemMarkup(item, {
                    working: working.has(item.id),
                    selected: selectedId === item.id,
                    activeParallel: activeParallel.has(item.id),
                    controlsLocked,
                  }),
                )
                .join("")}
            </div>
          </section>`,
      )
      .join("");
  }

  focus(packageId, { smooth = true } = {}) {
    const item = [...this.container.querySelectorAll(".work-package-list-item")].find(
      (candidate) => candidate.dataset.id === packageId,
    );
    item?.scrollIntoView({
      block: "nearest",
      behavior: smooth ? "smooth" : "auto",
    });
  }

  destroy() {
    this.container.removeEventListener("click", this.handleActionEvent);
  }

  #handleAction(event) {
    const button = event.target.closest("[data-work-package-action]");
    if (!button || !this.container.contains(button) || button.disabled) return;
    const id = button.dataset.id;
    const action = button.dataset.workPackageAction;
    if (id && action) this.onAction(action, id);
  }
}

function cardMarkup(
  item,
  {
    selected,
    parallelActive,
    working,
    controlsLocked = false,
    relationshipRelated,
    relationshipMuted,
    activeAncestor,
  },
) {
  const stage = stageLabel(item);
  const agent = assignedAgent(item) || "Automatic";
  const dependencies = (item.dependencies || []).join(", ") || "None";
  const criteria = item.acceptance_criteria || [];
  const verifiedCriteria = criteria.filter((criterion) => criterion?.verified).length;
  const directive = workPackageDirectiveSummary(item);
  const classes = ["work-package-card", stageClass(item)];
  if (item.parent_id) classes.push("shard");
  if (item.parallel_safe) classes.push("parallel-capable");
  if (parallelActive) classes.push("parallel-active");
  if (working) classes.push("working-now");
  if (selected) classes.push("selected");
  if (relationshipRelated) classes.push("relationship-related");
  if (relationshipMuted) classes.push("relationship-muted");
  if (activeAncestor) classes.push("active-ancestor");
  return `
    <article class="${classes.join(" ")}" data-id="${escapeHtml(item.id)}">
      ${working ? '<div class="work-package-working-banner"><span>● ACTIVE</span><small>scheduler assignment</small></div>' : ""}
      <button class="work-package-card-main" data-work-package-action="select" data-id="${escapeHtml(item.id)}" aria-pressed="${selected ? "true" : "false"}" aria-label="Open ${escapeHtml(item.id)} details and highlight its dependency path" title="Open Work Package details and highlight dependencies">
        <div class="work-package-card-top">
          <span class="work-package-id">${escapeHtml(item.id)}</span>
          <span class="work-package-stage">${escapeHtml(item.operator_paused ? "Paused" : stage)}</span>
        </div>
        <h3>${escapeHtml(item.title)}</h3>
        <div class="work-package-meta"><span>${escapeHtml(truncateText(agent, 30))}</span><span>${criteria.length ? `${verifiedCriteria}/${criteria.length} accepted` : "No criteria"}</span></div>
        <div class="work-package-dependencies"><strong>Needs</strong> ${escapeHtml(truncateText(dependencies, 58))}</div>
        <div class="work-package-tags">
          ${item.operator_paused || item.pause_before_start_reached_at ? '<span class="work-package-tag attention">Action required</span>' : ""}
          ${item.parent_id ? `<span class="work-package-tag shard">Shard of ${escapeHtml(item.parent_id)}</span>` : ""}
          ${item.parallel_safe ? '<span class="work-package-tag parallel">Parallel</span>' : ""}
          ${directive.count ? `<span class="work-package-tag directive" title="${escapeHtml(directive.title)}">${directive.count} directive${directive.count === 1 ? "" : "s"}</span>` : ""}
        </div>
      </button>
      <div class="work-package-actions" aria-label="Quick actions for ${escapeHtml(item.id)}">
        ${workPackageQuickActions(item, { controlsLocked })}
      </div>
    </article>`;
}

function markerForAppearance(appearance) {
  if (appearance === "active-terminal" || appearance === "active-path") {
    return "workflowArrowActive";
  }
  if (appearance === "selected-path") return "workflowArrowSelected";
  return "workflowArrowDefault";
}

/** Render simplified edges and graph-aware shared trunks returned by the router. */
function edgeMarkup(routes) {
  return routes
    .map(
      ({
        sourceId,
        targetId,
        members,
        points,
        appearance,
        markerEnd,
        kind,
      }) => {
        const classes = ["workflow-edge", ...(kind || "edge").split(" ")];
        if (appearance !== "default") classes.push(appearance);
        const marker = markerEnd
          ? ` marker-end="url(#${markerForAppearance(appearance)})"`
          : "";
        return `<path class="${classes.join(" ")}" data-from="${escapeHtml(
          sourceId,
        )}" data-to="${escapeHtml(targetId)}" data-members="${escapeHtml(
          members,
        )}" d="${orthogonalEdgePath(points)}"${marker}></path>`;
      },
    )
    .join("");
}

export class WorkflowGraph {
  constructor({
    board,
    canvas = null,
    svg = null,
    wrap = null,
    edgeSummary = null,
    onAction = null,
  }) {
    this.board = board;
    this.canvas = canvas || board.parentElement;
    this.svg = svg;
    this.wrap = wrap || this.canvas?.parentElement;
    this.edgeSummary = edgeSummary;
    this.onAction = onAction || (() => {});
    this.packages = [];
    this.visiblePackages = [];
    this.displayDependencies = [];
    this.edgeMode = "essential";
    this.selectedId = "";
    this.workingIds = new Set();
    this.activeDependencyIds = new Set();
    this.selectedRelationshipIds = new Set();
    this.drawFrame = 0;
    this.resizeObserver = null;
    this.handleResize = () => this.#scheduleConnections();
    this.handleActionEvent = (event) => this.#handleAction(event);
    this.board.addEventListener("click", this.handleActionEvent);
    if (typeof ResizeObserver !== "undefined") {
      this.resizeObserver = new ResizeObserver(() =>
        this.#scheduleConnections(),
      );
      this.resizeObserver.observe(this.board);
    }
    window.addEventListener("resize", this.handleResize, { passive: true });
  }

  render({
    packages,
    filter = "all",
    selectedId = "",
    activeParallelIds = [],
    workingPackageIds = [],
    controlsLocked = false,
    edgeMode = "essential",
  }) {
    this.packages = packages || [];
    this.edgeMode = edgeMode === "all" ? "all" : "essential";
    this.selectedId = selectedId || "";
    this.workingIds = new Set(workingPackageIds || []);
    this.activeDependencyIds = dependencyClosure(this.packages, [
      ...this.workingIds,
    ]);
    this.selectedRelationshipIds = this.selectedId
      ? relationshipClosure(this.packages, [this.selectedId])
      : new Set();
    const focusIds = this.selectedId
      ? this.selectedRelationshipIds
      : this.activeDependencyIds;
    this.visiblePackages = this.packages.filter((item) =>
      packageVisible(item, filter),
    );
    const dependencyPlan = dependencyEdgesForDisplay(this.packages, {
      visibleIds: new Set(this.visiblePackages.map((item) => item.id)),
      mode: this.edgeMode,
    });
    this.displayDependencies = dependencyPlan.edges;
    this.#renderEdgeSummary(dependencyPlan);
    if (!this.visiblePackages.length) {
      this.board.classList.remove("has-relationship-focus");
      this.board.innerHTML =
        '<div class="workflow-empty-card">No Work Packages match this filter.</div>';
      if (this.svg) this.svg.innerHTML = "";
      return;
    }
    const activeParallel = new Set(activeParallelIds || []);
    const levels = topologicalLevels(this.packages);
    const columns = orderTopologicalColumns(this.visiblePackages, levels);
    const hasFocus = focusIds.size > 0;
    this.board.classList.toggle("has-relationship-focus", hasFocus);
    const markup = [...columns.entries()]
      .sort((left, right) => left[0] - right[0])
      .map(
        ([column, items]) => `
        <section class="work-package-column" data-column="${column}">
          <header><span>Step ${column + 1}</span><strong>${items.length}</strong></header>
          <div class="work-package-column-cards">
            ${items
              .map((item) =>
                cardMarkup(item, {
                  selected: this.selectedId === item.id,
                  parallelActive: activeParallel.has(item.id),
                  working: this.workingIds.has(item.id),
                  controlsLocked,
                  relationshipRelated: focusIds.has(item.id),
                  relationshipMuted: hasFocus && !focusIds.has(item.id),
                  activeAncestor:
                    this.activeDependencyIds.has(item.id) &&
                    !this.workingIds.has(item.id),
                }),
              )
              .join("")}
          </div>
        </section>`,
      )
      .join("");
    this.board.innerHTML = markup;
    this.#scheduleConnections();
  }

  /** Release observers and listeners if the workbench is ever remounted. */
  destroy() {
    cancelAnimationFrame(this.drawFrame);
    this.resizeObserver?.disconnect();
    this.board.removeEventListener("click", this.handleActionEvent);
    window.removeEventListener("resize", this.handleResize);
  }

  #renderEdgeSummary({ mode, edges, declaredCount, hiddenCount }) {
    if (!this.edgeSummary) return;
    const plural = (count, word) =>
      `${count} ${word}${count === 1 ? "" : "s"}`;
    if (mode === "all") {
      this.edgeSummary.textContent =
        `${plural(declaredCount, "declared link")} shown`;
      this.edgeSummary.title =
        "All direct dependencies from the durable package contracts are visible.";
      return;
    }
    const hidden = hiddenCount
      ? ` · ${plural(hiddenCount, "redundant link")} hidden`
      : "";
    this.edgeSummary.textContent =
      `${plural(edges.length, "essential link")}${hidden}`;
    this.edgeSummary.title =
      "Compact view hides direct dependencies already implied by another dependency path. No execution semantics are changed.";
  }

  focus(packageId, { smooth = true } = {}) {
    const card = [...this.board.querySelectorAll(".work-package-card")].find(
      (item) => item.dataset.id === packageId,
    );
    if (!card || !this.wrap) return;
    const viewportRect = this.wrap.getBoundingClientRect();
    const cardRect = card.getBoundingClientRect();
    const left =
      this.wrap.scrollLeft +
      cardRect.left -
      viewportRect.left -
      (viewportRect.width - cardRect.width) / 2;
    const top =
      this.wrap.scrollTop +
      cardRect.top -
      viewportRect.top -
      (viewportRect.height - cardRect.height) / 2;
    this.wrap.scrollTo({
      left: Math.max(0, left),
      top: Math.max(0, top),
      behavior: smooth ? "smooth" : "auto",
    });
  }

  #scheduleConnections() {
    if (!this.svg) return;
    cancelAnimationFrame(this.drawFrame);
    this.drawFrame = requestAnimationFrame(() => this.#drawConnections());
  }

  #handleAction(event) {
    const button = event.target.closest("[data-work-package-action]");
    if (!button || !this.board.contains(button)) return;
    event.stopPropagation();
    const id = button.dataset.id;
    const action = button.dataset.workPackageAction;
    if (!id || !action || button.disabled) return;
    this.onAction(action, id);
  }

  #drawConnections() {
    if (!this.svg || !this.visiblePackages.length) return;
    const boardRect = this.board.getBoundingClientRect();
    // getBoundingClientRect includes the viewport transform. Normalize back to
    // logical canvas units so connectors remain aligned at every zoom level.
    const scaleX = boardRect.width / Math.max(this.board.offsetWidth, 1) || 1;
    const scaleY = boardRect.height / Math.max(this.board.offsetHeight, 1) || 1;
    const width = Math.max(
      this.board.scrollWidth,
      this.wrap?.clientWidth || 0,
      1,
    );
    const height = Math.max(
      this.board.scrollHeight,
      this.wrap?.clientHeight || 0,
      1,
    );
    if (this.canvas) {
      this.canvas.style.width = `${width}px`;
      this.canvas.style.height = `${height}px`;
    }
    this.svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    this.svg.setAttribute("width", String(width));
    this.svg.setAttribute("height", String(height));

    // Read every card rectangle once. Routing is pure after this measurement
    // pass, avoiding repeated layout reads on dense workflows.
    const cards = new Map(
      [...this.board.querySelectorAll(".work-package-card")].map((card) => {
        const rect = card.getBoundingClientRect();
        return [
          card.dataset.id,
          {
            card,
            column: Number(card.closest(".work-package-column")?.dataset.column || 0),
            left: (rect.left - boardRect.left) / scaleX,
            right: (rect.right - boardRect.left) / scaleX,
            top: (rect.top - boardRect.top) / scaleY,
            height: rect.height / scaleY,
          },
        ];
      }),
    );
    const hasSelectedFocus = Boolean(this.selectedId);
    const hasActiveFocus = this.workingIds.size > 0;
    const cardClearance = 5;
    const declaredEdges = [];

    for (const relation of this.displayDependencies) {
      const source = cards.get(relation.sourceId);
      const target = cards.get(relation.targetId);
      if (!source || !target) continue;
      declaredEdges.push({
        key: `${relation.sourceId}>${relation.targetId}`,
        sourceId: relation.sourceId,
        targetId: relation.targetId,
        sourceColumn: source.column,
        targetColumn: target.column,
        startX: source.right + cardClearance,
        endX: target.left - cardClearance,
        sourceCenterY: source.top + source.height / 2,
        targetCenterY: target.top + target.height / 2,
        sourceHeight: source.height,
        targetHeight: target.height,
        appearance: edgeAppearance({
          sourceId: relation.sourceId,
          targetId: relation.targetId,
          activeDependencyIds: this.activeDependencyIds,
          workingIds: this.workingIds,
          selectedRelationshipIds: this.selectedRelationshipIds,
          hasSelectedFocus,
          hasActiveFocus,
        }),
      });
    }

    const routedEdges = bundleOrthogonalEdges(declaredEdges);
    const markup = `
      <defs>
        <marker id="workflowArrowDefault" viewBox="0 0 7 7" markerWidth="7" markerHeight="7" refX="6.4" refY="3.5" orient="auto" markerUnits="userSpaceOnUse"><path d="M0,0 L0,7 L6.4,3.5 z"></path></marker>
        <marker id="workflowArrowSelected" viewBox="0 0 7 7" markerWidth="7" markerHeight="7" refX="6.4" refY="3.5" orient="auto" markerUnits="userSpaceOnUse"><path d="M0,0 L0,7 L6.4,3.5 z"></path></marker>
        <marker id="workflowArrowActive" viewBox="0 0 7 7" markerWidth="7" markerHeight="7" refX="6.4" refY="3.5" orient="auto" markerUnits="userSpaceOnUse"><path d="M0,0 L0,7 L6.4,3.5 z"></path></marker>
      </defs>`;
    this.svg.innerHTML = `${markup}${edgeMarkup(routedEdges)}`;
  }
}
