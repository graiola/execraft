import { escapeHtml as esc } from "./ui-utils.js";
import {
  clientToTimelineDay,
  connectorCurve,
  fittedTimelineDomain,
  formatRoadmapDay,
  laneDropIntent,
  parseRoadmapDay,
  resizeSchedule,
  rowDropIntent,
  scheduleDurationDays,
  scheduleSummary,
  scheduleWindow,
  scheduleWithDuration,
  shiftSchedule,
  visualItemId,
} from "./roadmap-interactions.js";

const DAY_MS = 24 * 60 * 60 * 1000;
const LABEL_WIDTH = 196;
const ROW_HEIGHT = 62;
const HEADER_HEIGHT = 44;
const ZOOM_LEVELS = {
  month: { pixelsPerDay: 13, minimumDays: 96, paddingDays: 14 },
  quarter: { pixelsPerDay: 5.5, minimumDays: 280, paddingDays: 32 },
  year: { pixelsPerDay: 2.25, minimumDays: 740, paddingDays: 90 },
};

function $(id) { return document.getElementById(id); }
function todayDay() { return Math.floor(Date.now() / DAY_MS); }

const parseDay = parseRoadmapDay;
const formatDay = formatRoadmapDay;

function itemTitle(item) {
  return item?.kind === "task" ? item.task?.title || item.task_id : item?.title || item?.id || "";
}

function scheduleBounds(item) {
  return scheduleWindow(item?.schedule || {});
}

function kindLabel(kind) {
  return {
    task: "Task",
    planned_task: "Planned task",
    milestone: "Milestone",
    phase: "Phase",
    gate: "Gate",
  }[kind] || kind;
}

function kindGlyph(kind) {
  return { task: "▣", planned_task: "▭", milestone: "◆", phase: "▰", gate: "⬢" }[kind] || "•";
}

function taskStateLabel(task) {
  if (!task) return "unknown";
  if (task.availability === "archived") return "archived";
  if (task.availability === "missing") return "missing";
  return task.runtime_state || task.status || "planned";
}

function projectAssetStateLabel(item) {
  const state = String(item?.project_asset?.state || "").replaceAll("_", " ");
  const health = String(item?.project_asset?.health || "").replaceAll("_", " ");
  if (state && health && health !== "on track") return `${state} · ${health}`;
  return state || health || kindLabel(item?.kind);
}

function itemMappingForSave(item, overrides = {}) {
  const schedule = Object.prototype.hasOwnProperty.call(overrides, "schedule")
    ? { ...(overrides.schedule || {}) }
    : { ...(item.schedule || {}) };
  Object.keys(schedule).forEach((key) => { if (!schedule[key]) delete schedule[key]; });
  const mapping = {
    id: item.id,
    kind: item.kind,
    lane: overrides.lane ?? item.lane ?? "General",
    order: overrides.order ?? item.order ?? 0,
    schedule,
  };
  if (item.kind === "task") mapping.task_id = item.task_id;
  else {
    if (item.project_asset_id) mapping.project_asset_id = item.project_asset_id;
    mapping.title = overrides.title ?? item.title ?? "";
    mapping.description = overrides.description ?? item.description ?? "";
  }
  if (!Object.keys(schedule).length) delete mapping.schedule;
  return mapping;
}

function relationKey(relation) { return `${relation.from}|${relation.to}|${relation.kind}`; }

export class RoadmapView {
  constructor({ api, download, toast, onOpenTask, onCreateTaskFromPlanned, onOpenProjectAsset = () => {}, onCanonicalChange = () => {} }) {
    this.api = api;
    this.download = download;
    this.toast = toast;
    this.onOpenTask = onOpenTask;
    this.onCreateTaskFromPlanned = onCreateTaskFromPlanned;
    this.onOpenProjectAsset = onOpenProjectAsset;
    this.onCanonicalChange = onCanonicalChange;
    this.projectId = "";
    this.catalog = [];
    this.roadmap = null;
    this.selectedItemId = "";
    this.selectedRelationKey = "";
    this.zoom = "quarter";
    this.fitMode = true;
    this.groupMode = "lane";
    this.taskTrayCollapsed = false;
    this.drag = null;
    this.rowDrag = null;
    this.connection = null;
    this.lastDrag = null;
    this.placementKind = "";
    this.timelineRows = [];
    this.timelineDomain = null;
    this.restoreViewOnNextRender = false;
    this.loading = false;
    this.loadedProjectId = "";
    this.#bind();
  }

  setProject(projectId) {
    const next = String(projectId || "");
    if (next === this.projectId) return;
    this.projectId = next;
    this.catalog = [];
    this.roadmap = null;
    this.selectedItemId = "";
    this.selectedRelationKey = "";
    this.loadedProjectId = "";
    this.fitMode = true;
    this.groupMode = "lane";
    this.taskTrayCollapsed = false;
    this.#cancelPlacement();
    this.#renderEmpty("Open Roadmap to load project planning.");
  }

  invalidateCanonicalProjection() {
    this.loadedProjectId = "";
  }

  async load({ force = false } = {}) {
    if (!this.projectId || this.loading) return;
    if (!force && this.loadedProjectId === this.projectId && this.roadmap) {
      this.render();
      return;
    }
    this.loading = true;
    $("roadmapLoading").classList.remove("hidden");
    $("roadmapTimelineScroll")?.setAttribute("aria-busy", "true");
    try {
      const catalog = await this.api(`/api/roadmaps?project_id=${encodeURIComponent(this.projectId)}`);
      this.#clearInlineState();
      this.catalog = catalog.roadmaps || [];
      $("projectRoadmapsCount").textContent = String(this.catalog.length);
      this.#renderRoadmapSelector();
      if (!this.catalog.length) {
        this.roadmap = null;
        this.loadedProjectId = this.projectId;
        this.#renderEmpty("No roadmap yet. Create one to plan project work visually.");
        return;
      }
      await this.open(this.#preferredRoadmapId(), { remember: false });
      this.loadedProjectId = this.projectId;
    } catch (error) {
      this.#renderEmpty(error.message, true);
      this.toast(error.message, true);
    } finally {
      this.loading = false;
      $("roadmapLoading").classList.add("hidden");
      $("roadmapTimelineScroll")?.setAttribute("aria-busy", "false");
    }
  }

  async open(roadmapId, { remember = true } = {}) {
    if (!this.projectId || !roadmapId) return;
    try {
      const roadmap = await this.api(`/api/roadmap?project_id=${encodeURIComponent(this.projectId)}&roadmap_id=${encodeURIComponent(roadmapId)}`);
      const changedRoadmap = this.roadmap?.id !== roadmap.id;
      this.roadmap = roadmap;
      this.#clearInlineState();
      this.selectedItemId = this.#item(this.selectedItemId) ? this.selectedItemId : "";
      this.selectedRelationKey = "";
      if (remember) localStorage.setItem(`execraft-roadmap:${this.projectId}`, roadmap.id);
      this.#refreshCatalogSummary(roadmap);
      this.#renderRoadmapSelector();
      if (changedRoadmap) this.restoreViewOnNextRender = true;
      this.render();
      if (roadmap.coordination?.pending) this.#showCoordinationState(roadmap.coordination);
    } catch (error) {
      this.toast(error.message, true);
      this.#showInlineState(error.message, this.#isRevisionConflict(error));
      if (String(error.message).includes("refresh")) await this.load({ force: true });
    }
  }

  render() {
    if (!this.roadmap) return this.#renderEmpty("No roadmap selected.");
    $("roadmapEmptyState").classList.add("hidden");
    $("roadmapWorkspace").classList.remove("hidden");
    $("roadmapTitle").textContent = this.roadmap.title;
    $("roadmapSubtitle").textContent = this.roadmap.description || "Direct-manipulation project plan · execution remains task-local";
    $("roadmapStats").innerHTML = this.#statsHtml();
    document.querySelectorAll("[data-roadmap-zoom]").forEach((button) => {
      const active = !this.fitMode && button.dataset.roadmapZoom === this.zoom;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    $("roadmapFitBtn").classList.toggle("active", this.fitMode);
    $("roadmapFitBtn").setAttribute("aria-pressed", String(this.fitMode));
    $("roadmapSelectedBtn").disabled = !this.#selectedScheduledItem();
    $("roadmapGroupBy").value = this.groupMode;
    $("roadmapTaskTray").classList.toggle("collapsed", this.taskTrayCollapsed);
    $("roadmapTaskTrayToggle").textContent = this.taskTrayCollapsed ? "Show" : "Hide";
    $("roadmapTaskTrayToggle").setAttribute("aria-expanded", String(!this.taskTrayCollapsed));
    this.#renderTimeline();
    this.#renderUnscheduled();
    this.#renderInspector();
    if (this.restoreViewOnNextRender) this.#restoreViewState();
  }

  async linkCreatedTask(context, taskId) {
    if (!context?.roadmapId || !context?.itemId) return false;
    try {
      const result = await this.api("/api/roadmap/item/link-task", {
        method: "POST",
        body: JSON.stringify({
          project_id: context.projectId,
          roadmap_id: context.roadmapId,
          expected_revision: context.revision,
          item_id: context.itemId,
          task_id: taskId,
          lane: context.lane || "General",
          order: context.order || 0,
        }),
      });
      if (context.projectId === this.projectId && context.roadmapId === result.id) {
        this.roadmap = result;
        this.#refreshCatalogSummary(result);
      }
      return true;
    } catch (error) {
      this.toast(`Task created, but the roadmap changed before it could be linked: ${error.message}`, true);
      return false;
    }
  }

  #bind() {
    $("roadmapSelect").addEventListener("change", (event) => this.open(event.target.value));
    $("roadmapRefreshBtn").addEventListener("click", () => this.load({ force: true }));
    $("roadmapExportBtn").addEventListener("click", () => this.#exportRoadmap());
    $("newRoadmapBtn").addEventListener("click", () => this.#openRoadmapDialog());
    $("roadmapEditBtn").addEventListener("click", () => this.#openRoadmapDialog({ edit: true }));
    $("roadmapDeleteBtn").addEventListener("click", () => this.#deleteRoadmap());
    $("roadmapPaletteBtn").addEventListener("click", () => this.#togglePalette());
    $("roadmapFitBtn").addEventListener("click", () => this.#fitPlan());
    $("roadmapTodayBtn").addEventListener("click", () => this.#scrollToday());
    $("roadmapSelectedBtn").addEventListener("click", () => this.#scrollSelected());
    $("roadmapGroupBy").addEventListener("change", (event) => {
      this.groupMode = event.target.value === "phase" ? "phase" : "lane";
      this.#renderTimeline();
      this.#rememberViewState();
    });
    $("roadmapTaskTrayToggle").addEventListener("click", () => {
      this.taskTrayCollapsed = !this.taskTrayCollapsed;
      $("roadmapTaskTray").classList.toggle("collapsed", this.taskTrayCollapsed);
      $("roadmapTaskTrayToggle").textContent = this.taskTrayCollapsed ? "Show" : "Hide";
      $("roadmapTaskTrayToggle").setAttribute("aria-expanded", String(!this.taskTrayCollapsed));
      this.#rememberViewState();
    });
    document.querySelectorAll("[data-roadmap-zoom]").forEach((button) => {
      button.addEventListener("click", () => {
        this.fitMode = false;
        this.zoom = button.dataset.roadmapZoom;
        this.#renderTimeline();
        this.#syncTimelineToolbar();
      });
    });
    $("roadmapBlockPalette").querySelectorAll("[data-roadmap-create-kind]").forEach((button) => {
      button.addEventListener("click", () => this.#beginPlacement(button.dataset.roadmapCreateKind));
    });
    $("roadmapPaletteTasks").addEventListener("click", () => {
      this.#closePalette();
      this.taskTrayCollapsed = false;
      $("roadmapTaskTray").classList.remove("collapsed");
      $("roadmapTaskTrayToggle").textContent = "Hide";
      $("roadmapTaskTrayToggle").setAttribute("aria-expanded", "true");
      $("roadmapTaskTray").classList.add("attention");
      $("roadmapTaskTray").scrollIntoView({ behavior: "smooth", block: "nearest" });
      setTimeout(() => $("roadmapTaskTray").classList.remove("attention"), 900);
    });
    $("roadmapCreateDialog").addEventListener("submit", (event) => {
      event.preventDefault();
      this.#saveRoadmapDialog();
    });
    $("roadmapCreateCancel").addEventListener("click", () => $("roadmapCreateDialog").close());
    $("roadmapTimelineScroll").addEventListener("pointerdown", (event) => this.#beginPointerInteraction(event));
    $("roadmapTimelineScroll").addEventListener("click", (event) => this.#handleCanvasClick(event));
    $("roadmapTimelineScroll").addEventListener("dblclick", (event) => this.#handleCanvasDoubleClick(event));
    $("roadmapTimelineScroll").addEventListener("keydown", (event) => this.#handleCanvasKeydown(event));
    window.addEventListener("pointermove", (event) => this.#movePointerInteraction(event));
    window.addEventListener("pointerup", (event) => this.#endPointerInteraction(event));
    window.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        this.#cancelPlacement();
        this.#cancelConnection();
      }
    });
    $("roadmapTimelineScroll").addEventListener("dragover", (event) => {
      if (event.dataTransfer?.types.includes("application/x-execraft-task")) {
        event.preventDefault();
        event.dataTransfer.dropEffect = "move";
      }
    });
    $("roadmapTimelineScroll").addEventListener("drop", (event) => this.#dropTask(event));
  }

  async #exportRoadmap() {
    if (!this.roadmap || !this.download) return;
    const format = $("roadmapExportFormat").value === "svg" ? "svg" : "pdf";
    try {
      const filename = await this.download(`/api/export/roadmap?project_id=${encodeURIComponent(this.projectId)}&roadmap_id=${encodeURIComponent(this.roadmap.id)}&format=${format}&theme=dark`);
      this.toast(`Exported ${filename}`);
    } catch (error) {
      this.toast(error.message, true);
    }
  }

  #preferredRoadmapId() {
    const remembered = localStorage.getItem(`execraft-roadmap:${this.projectId}`) || "";
    return this.catalog.some((roadmap) => roadmap.id === remembered) ? remembered : this.catalog[0]?.id || "";
  }

  #renderRoadmapSelector() {
    const select = $("roadmapSelect");
    select.innerHTML = this.catalog.length
      ? this.catalog.map((roadmap) => `<option value="${esc(roadmap.id)}">${esc(roadmap.title)}</option>`).join("")
      : '<option value="">No roadmaps</option>';
    select.value = this.roadmap?.id || this.#preferredRoadmapId();
    select.disabled = !this.catalog.length;
    $("roadmapEditBtn").disabled = !this.roadmap;
    $("roadmapDeleteBtn").disabled = !this.roadmap;
    $("roadmapPaletteBtn").disabled = !this.roadmap;
    $("roadmapExportBtn").disabled = !this.roadmap;
    $("roadmapFitBtn").disabled = !this.roadmap;
    $("roadmapTodayBtn").disabled = !this.roadmap;
    this.#syncTimelineToolbar();
  }

  #refreshCatalogSummary(roadmap) {
    const index = this.catalog.findIndex((item) => item.id === roadmap.id);
    const summary = {
      id: roadmap.id,
      title: roadmap.title,
      description: roadmap.description,
      revision: roadmap.revision,
      updated_at: roadmap.updated_at,
      item_count: roadmap.items?.length || 0,
      task_count: roadmap.statistics?.linked_tasks || 0,
    };
    if (index >= 0) this.catalog[index] = summary;
    else this.catalog.unshift(summary);
    $("projectRoadmapsCount").textContent = String(this.catalog.length);
  }

  #statsHtml() {
    const stats = this.roadmap.statistics || {};
    return [
      ["Items", stats.items || 0],
      ["Tasks", stats.linked_tasks || 0],
      ["Gates", stats.gates || 0],
      ["Unscheduled", stats.unscheduled_tasks || 0],
    ].map(([label, value]) => `<span><strong>${value}</strong>${label}</span>`).join("");
  }

  #renderEmpty(message, error = false) {
    $("roadmapWorkspace").classList.add("hidden");
    const target = $("roadmapEmptyState");
    target.classList.remove("hidden");
    target.classList.toggle("error", error);
    target.setAttribute("role", error ? "alert" : "status");
    target.innerHTML = `<strong>${error ? "Roadmap unavailable" : "Project roadmap"}</strong><p>${esc(message)}</p>${!error && this.projectId ? '<button id="roadmapEmptyCreate" type="button" class="btn primary">Create roadmap</button>' : ""}`;
    $("roadmapEmptyCreate")?.addEventListener("click", () => this.#openRoadmapDialog());
    $("roadmapTitle").textContent = "Roadmap";
    $("roadmapSubtitle").textContent = "Plan project-level work without changing task execution semantics.";
  }

  #domain() {
    const zoom = ZOOM_LEVELS[this.zoom];
    const today = todayDay();
    const scheduledDays = [];
    for (const item of this.roadmap?.items || []) {
      const bounds = scheduleBounds(item);
      if (bounds) scheduledDays.push(bounds.start, bounds.target);
    }

    if (this.fitMode && scheduledDays.length) {
      return fittedTimelineDomain({
        minimumDay: Math.min(...scheduledDays),
        maximumDay: Math.max(...scheduledDays),
        viewportWidth: $("roadmapTimelineScroll")?.clientWidth || 960,
        labelWidth: LABEL_WIDTH,
      });
    }

    const days = [today, ...scheduledDays];
    let start = Math.min(...days) - zoom.paddingDays;
    let end = Math.max(...days) + zoom.paddingDays;
    if (end - start < zoom.minimumDays) {
      const missing = zoom.minimumDays - (end - start);
      start -= Math.floor(missing / 2);
      end += Math.ceil(missing / 2);
    }
    return { start, end, width: Math.ceil((end - start + 1) * zoom.pixelsPerDay), ...zoom };
  }

  #orderedRows() {
    const items = [...(this.roadmap?.items || [])].sort((a, b) => (a.order || 0) - (b.order || 0) || itemTitle(a).localeCompare(itemTitle(b)));
    if (this.groupMode === "phase") {
      const groups = new Map();
      for (const item of items) {
        const phase = item.project_phase || { id: "__unassigned__", title: "Unassigned", order: 1_000_000, kind: "unassigned" };
        const key = phase.id || "__unassigned__";
        if (!groups.has(key)) groups.set(key, { phase, items: [] });
        groups.get(key).items.push(item);
      }
      const orderedGroups = [...groups.values()].sort((a, b) => Number(a.phase.order || 0) - Number(b.phase.order || 0) || String(a.phase.title || "").localeCompare(String(b.phase.title || "")));
      const rows = [];
      for (const group of orderedGroups) {
        rows.push({ type: "group", groupKind: "phase", phase: group.phase });
        for (const item of group.items) rows.push({ type: "item", item });
      }
      return rows;
    }

    const laneNames = [];
    const grouped = new Map();
    for (const item of items) {
      const lane = item.lane || "General";
      if (!grouped.has(lane)) {
        laneNames.push(lane);
        grouped.set(lane, []);
      }
      grouped.get(lane).push(item);
    }
    const rows = [];
    const showLaneHeaders = laneNames.length > 1 || laneNames[0] !== "General";
    for (const lane of laneNames.length ? laneNames : ["General"]) {
      if (showLaneHeaders) rows.push({ type: "lane", lane });
      for (const item of grouped.get(lane) || []) rows.push({ type: "item", item });
    }
    return rows;
  }

  #renderTimeline() {
    const target = $("roadmapTimelineCanvas");
    if (!this.roadmap) return;
    const domain = this.#domain();
    const rows = this.#orderedRows();
    this.timelineDomain = domain;
    this.timelineRows = rows;
    const totalWidth = LABEL_WIDTH + domain.width;
    const bodyHeight = Math.max(260, Math.max(1, rows.length) * ROW_HEIGHT);
    target.style.width = `${totalWidth}px`;
    target.style.minHeight = `${HEADER_HEIGHT + bodyHeight}px`;
    target.dataset.domainStart = String(domain.start);
    target.dataset.pixelsPerDay = String(domain.pixelsPerDay);
    target.innerHTML = `${this.#timelineHeader(domain, totalWidth)}<div class="roadmap-timeline-body" style="height:${bodyHeight}px">${this.#timelineRows(rows, domain, totalWidth)}</div>${this.#dependencySvg(rows, domain, totalWidth, bodyHeight)}${this.#relationControl(rows, domain)}<span id="roadmapDragGuide" class="roadmap-drag-guide hidden" aria-hidden="true"></span>`;
    this.#syncTimelineToolbar();
  }

  #timelineHeader(domain, totalWidth) {
    const ticks = [];
    let date = new Date(domain.start * DAY_MS);
    date = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), 1));
    while (Math.floor(date.getTime() / DAY_MS) <= domain.end) {
      const day = Math.floor(date.getTime() / DAY_MS);
      const left = LABEL_WIDTH + (day - domain.start) * domain.pixelsPerDay;
      const label = date.toLocaleDateString(undefined, this.zoom === "year" ? { year: "numeric", month: "short" } : { month: "short", year: "numeric" });
      ticks.push(`<span class="roadmap-axis-tick" style="left:${left}px"><b>${esc(label)}</b></span>`);
      date = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + (this.zoom === "year" ? 3 : 1), 1));
    }
    const todayLeft = LABEL_WIDTH + (todayDay() - domain.start) * domain.pixelsPerDay;
    const axisLabel = this.groupMode === "phase" ? "Phase view" : "Rows";
    return `<div class="roadmap-axis" style="width:${totalWidth}px"><div class="roadmap-axis-label"><span class="roadmap-axis-symbol">${this.groupMode === "phase" ? "▰" : "↕"}</span><span>${axisLabel}</span></div>${ticks.join("")}<span class="roadmap-today-line" style="left:${todayLeft}px" title="Today"></span></div>`;
  }

  #timelineRows(rows, domain, totalWidth) {
    if (!rows.length) {
      return `<div class="roadmap-canvas-empty" style="width:${totalWidth}px"><span>＋</span><strong>Add your first block</strong><small>Use + Block, then click anywhere on the timeline.</small></div>`;
    }
    return rows.map((row, rowIndex) => {
      const top = rowIndex * ROW_HEIGHT;
      if (row.type === "lane") {
        return `<div class="roadmap-row roadmap-lane-row" data-roadmap-lane-row="${esc(row.lane)}" data-roadmap-row-index="${rowIndex}" style="top:${top}px;width:${totalWidth}px"><div class="roadmap-row-label roadmap-lane-label"><button type="button" class="roadmap-row-grip lane" data-roadmap-lane-drag="${esc(row.lane)}" aria-label="Move lane ${esc(row.lane)}" title="Drag to reorder lane">⠿</button><span class="roadmap-lane-icon">≡</span><strong>${esc(row.lane)}</strong></div><div class="roadmap-row-track" style="left:${LABEL_WIDTH}px;width:${domain.width}px"></div></div>`;
      }
      if (row.type === "group") {
        const phase = row.phase || {};
        const subtitle = phase.kind === "phase" ? "Canonical Project Phase" : phase.kind === "cross_phase" ? "Shared across Project Phases" : "Not assigned to a Project Phase";
        return `<div class="roadmap-row roadmap-phase-group-row" data-roadmap-row-index="${rowIndex}" style="top:${top}px;width:${totalWidth}px"><div class="roadmap-row-label"><span class="roadmap-lane-icon">▰</span><span><strong>${esc(phase.title || "Unassigned")}</strong><small>${esc(subtitle)}</small></span></div><div class="roadmap-row-track" style="left:${LABEL_WIDTH}px;width:${domain.width}px"></div></div>`;
      }
      const item = row.item;
      const selected = item.id === this.selectedItemId;
      const bounds = scheduleBounds(item);
      const label = itemTitle(item);
      const task = item.task;
      const canonical = Boolean(item.project_asset_id);
      const status = item.kind === "task" ? taskStateLabel(task) : projectAssetStateLabel(item);
      const primaryTitle = canonical
        ? `Select ${label}. Schedule changes update canonical Project Execution.`
        : `Select ${label}`;
      let block = "";
      if (bounds) {
        const left = (bounds.start - domain.start) * domain.pixelsPerDay;
        const width = Math.max(22, (bounds.target - bounds.start + 1) * domain.pixelsPerDay);
        if (["milestone", "gate"].includes(item.kind)) {
          const pointLeft = (bounds.target - domain.start) * domain.pixelsPerDay;
          block = `<div class="roadmap-item-point kind-${esc(item.kind)} ${canonical ? "canonical" : ""} ${selected ? "selected" : ""}" data-roadmap-item="${esc(item.id)}" role="button" tabindex="0" aria-pressed="${selected}" style="left:${pointLeft}px" title="${esc(primaryTitle)}" aria-label="${esc(primaryTitle)}"><span class="roadmap-connector input" data-roadmap-connect-in="${esc(item.id)}" title="Planning dependency input"></span><span class="roadmap-point-shape"></span><span class="roadmap-point-label">${esc(label)}</span><span class="roadmap-connector output" data-roadmap-connect-out="${esc(item.id)}" title="Drag planning dependency"></span></div>`;
        } else {
          const progress = item.kind === "task" ? Math.max(0, Math.min(100, Number(task?.progress_percent || 0))) : 0;
          const duration = bounds.durationDays || 1;
          const durationBadge = ["task", "planned_task"].includes(item.kind)
            ? `<span class="roadmap-duration-badge" title="Planned duration">${duration}d</span>`
            : "";
          block = `<div class="roadmap-item-bar kind-${esc(item.kind)} ${canonical ? "canonical" : ""} ${selected ? "selected" : ""}" data-roadmap-item="${esc(item.id)}" role="button" tabindex="0" aria-pressed="${selected}" style="left:${left}px;width:${width}px" title="${esc(primaryTitle)}" aria-label="${esc(primaryTitle)}"><span class="roadmap-progress-fill" style="width:${progress}%"></span><span class="roadmap-resize start" data-roadmap-resize="start" tabindex="0" role="separator" aria-orientation="vertical" aria-label="Resize ${esc(kindLabel(item.kind))} start" title="Drag or use ←/→ to resize start"></span><span class="roadmap-connector input" data-roadmap-connect-in="${esc(item.id)}" title="Planning dependency input"></span><span class="roadmap-bar-kind">${esc(kindGlyph(item.kind))}</span><span class="roadmap-bar-label">${esc(label)}</span>${durationBadge}<span class="roadmap-connector output" data-roadmap-connect-out="${esc(item.id)}" title="Drag planning dependency"></span><span class="roadmap-resize target" data-roadmap-resize="target" tabindex="0" role="separator" aria-orientation="vertical" aria-label="Resize ${esc(kindLabel(item.kind))} target" title="Drag or use ←/→ to resize target"></span></div>`;
        }
      } else {
        block = `<span class="roadmap-unscheduled-placeholder" title="No schedule is defined for this block">Unscheduled</span>`;
      }
      const reorderControl = this.groupMode === "lane"
        ? `<button type="button" class="roadmap-row-grip item" data-roadmap-row-drag="${esc(item.id)}" aria-label="Move row ${esc(label)}" title="Drag to reorder row">⠿</button>`
        : '<span class="roadmap-row-grip-disabled" title="Phase grouping is view-only. Switch Group to Lane to reorder rows.">·</span>';
      return `<div class="roadmap-row roadmap-item-row ${canonical ? "canonical" : ""} ${selected ? "selected" : ""}" data-roadmap-row="${esc(item.id)}" data-roadmap-row-index="${rowIndex}" style="top:${top}px;width:${totalWidth}px"><div class="roadmap-row-label roadmap-item-label">${reorderControl}<button type="button" class="roadmap-label-button" data-roadmap-primary="${esc(item.id)}" aria-pressed="${selected}" title="${esc(primaryTitle)}"><span class="roadmap-kind-symbol kind-${esc(item.kind)}">${esc(kindGlyph(item.kind))}</span><span><strong>${esc(label)}</strong><small>${esc(status)}</small></span></button><button type="button" class="roadmap-item-menu-button" data-roadmap-edit="${esc(item.id)}" aria-label="Select ${esc(label)}" title="Select and show actions">⋮</button></div><div class="roadmap-row-track" data-roadmap-drop-track="true" style="left:${LABEL_WIDTH}px;width:${domain.width}px">${block}</div></div>`;
    }).join("");
  }

  #dependencySvg(rows, domain, totalWidth, bodyHeight) {
    const rowIndex = new Map();
    rows.forEach((row, index) => { if (row.type === "item") rowIndex.set(row.item.id, index); });
    const byId = new Map((this.roadmap?.items || []).map((item) => [item.id, item]));
    const paths = [];
    for (const relation of this.roadmap?.relations || []) {
      const source = byId.get(relation.from);
      const target = byId.get(relation.to);
      const sourceBounds = source && scheduleBounds(source);
      const targetBounds = target && scheduleBounds(target);
      if (!sourceBounds || !targetBounds || !rowIndex.has(source.id) || !rowIndex.has(target.id)) continue;
      const sourcePoint = ["milestone", "gate"].includes(source.kind);
      const targetPoint = ["milestone", "gate"].includes(target.kind);
      const x1 = LABEL_WIDTH + (sourceBounds.target - domain.start + (sourcePoint ? 0 : 1)) * domain.pixelsPerDay + (sourcePoint ? 14 : 0);
      const x2 = LABEL_WIDTH + (targetBounds.start - domain.start) * domain.pixelsPerDay - (targetPoint ? 5 : 0);
      const y1 = rowIndex.get(source.id) * ROW_HEIGHT + ROW_HEIGHT / 2;
      const y2 = rowIndex.get(target.id) * ROW_HEIGHT + ROW_HEIGHT / 2;
      const d = connectorCurve(x1, y1, x2, y2);
      const key = relationKey(relation);
      const selected = key === this.selectedRelationKey;
      paths.push(`<path class="roadmap-relation-shadow ${selected ? "selected" : ""}" d="${d}" data-roadmap-relation="${esc(key)}"></path><path class="roadmap-relation ${esc(relation.kind)} ${selected ? "selected" : ""}" d="${d}" marker-end="url(#roadmapArrow)" data-roadmap-relation="${esc(key)}"></path>`);
    }
    return `<svg class="roadmap-relations" width="${totalWidth}" height="${bodyHeight}" style="top:${HEADER_HEIGHT}px"><defs><marker id="roadmapArrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 Z"></path></marker></defs>${paths.join("")}<path id="roadmapConnectorPreview" class="roadmap-connector-preview hidden"></path></svg>`;
  }

  #relationControl(rows, domain) {
    if (!this.selectedRelationKey) return "";
    const [sourceId, targetId] = this.selectedRelationKey.split("|");
    const source = this.#item(sourceId);
    const target = this.#item(targetId);
    const sourceBounds = scheduleBounds(source);
    const targetBounds = scheduleBounds(target);
    const sourceIndex = rows.findIndex((row) => row.type === "item" && row.item.id === sourceId);
    const targetIndex = rows.findIndex((row) => row.type === "item" && row.item.id === targetId);
    if (!sourceBounds || !targetBounds || sourceIndex < 0 || targetIndex < 0) return "";
    const sourcePoint = ["milestone", "gate"].includes(source.kind);
    const targetPoint = ["milestone", "gate"].includes(target.kind);
    const x1 = LABEL_WIDTH + (sourceBounds.target - domain.start + (sourcePoint ? 0 : 1)) * domain.pixelsPerDay + (sourcePoint ? 14 : 0);
    const x2 = LABEL_WIDTH + (targetBounds.start - domain.start) * domain.pixelsPerDay - (targetPoint ? 5 : 0);
    const left = Math.max(LABEL_WIDTH + 10, (x1 + x2) / 2 - 15);
    const top = HEADER_HEIGHT + ((sourceIndex + targetIndex) / 2) * ROW_HEIGHT + 6;
    return `<button type="button" class="roadmap-relation-delete" style="left:${left}px;top:${top}px" data-delete-selected-relation title="Delete dependency" aria-label="Delete dependency">×</button>`;
  }

  #renderUnscheduled() {
    if (!this.roadmap) return;
    const tasks = this.roadmap.unscheduled_tasks || [];
    $("roadmapUnscheduledCount").textContent = String(tasks.length);
    const target = $("roadmapUnscheduledTasks");
    target.innerHTML = tasks.length
      ? tasks.map((task) => `<article class="roadmap-unscheduled-task" draggable="true" data-roadmap-task="${esc(task.id)}" title="Drag onto roadmap"><span class="roadmap-task-grip">⠿</span><div><strong>${esc(task.title || task.id)}</strong><small>${esc(taskStateLabel(task))}</small></div></article>`).join("")
      : '<div class="roadmap-side-empty roadmap-tray-empty"><span>✓</span><small>Every active task is on this roadmap.</small></div>';
    target.querySelectorAll("[data-roadmap-task]").forEach((card) => {
      card.addEventListener("dragstart", (event) => {
        event.dataTransfer.setData("application/x-execraft-task", card.dataset.roadmapTask);
        event.dataTransfer.effectAllowed = "move";
        card.classList.add("dragging");
      });
      card.addEventListener("dragend", () => card.classList.remove("dragging"));
    });
  }

  #renderInspector() {
    const target = $("roadmapInspector");
    if (this.selectedRelationKey) {
      const [sourceId, targetId, kind] = this.selectedRelationKey.split("|");
      const source = this.#item(sourceId);
      const destination = this.#item(targetId);
      target.className = "roadmap-inspector roadmap-selection-hud";
      target.innerHTML = `<div class="roadmap-selection-symbol relation">→</div><div class="roadmap-selection-copy"><span class="eyebrow">${esc(kind || "blocks")}</span><strong>${esc(itemTitle(source))} → ${esc(itemTitle(destination))}</strong><small>Planning dependency · does not control Task eligibility.</small></div><div class="roadmap-selection-actions"><button type="button" class="icon-btn danger" data-delete-selected-relation title="Delete dependency" aria-label="Delete dependency">×</button></div>`;
      target.querySelector("[data-delete-selected-relation]")?.addEventListener("click", () => this.#deleteRelation(this.selectedRelationKey));
      return;
    }
    const item = this.#item(this.selectedItemId);
    if (!item) {
      target.className = "roadmap-inspector roadmap-selection-hud empty";
      target.innerHTML = '<div class="roadmap-hud-guide"><span>↔</span><small>Drag to schedule</small><span>↕</span><small>Move rows</small><span>●→●</span><small>Connect dependencies</small></div>';
      return;
    }
    const task = item.task || null;
    const bounds = scheduleBounds(item);
    const taskLike = ["task", "planned_task"].includes(item.kind);
    const canonical = Boolean(item.project_asset_id);
    const scheduleEditor = taskLike
      ? `<div class="roadmap-schedule-editor" data-roadmap-schedule-editor>
          <label><span>Start</span><input type="date" data-roadmap-schedule-start value="${esc(item.schedule?.start || "")}"></label>
          <label><span>Target</span><input type="date" data-roadmap-schedule-target value="${esc(item.schedule?.target || "")}"></label>
          <label><span>Duration</span><span class="roadmap-duration-input"><input type="number" min="1" max="3650" step="1" inputmode="numeric" data-roadmap-schedule-duration value="${bounds ? bounds.durationDays : ""}" aria-label="Task duration in days"><small>days</small></span></label>
          <div class="roadmap-schedule-actions"><button type="button" class="btn small primary" data-roadmap-schedule-apply>Apply schedule</button><button type="button" class="btn small" data-roadmap-schedule-clear>Clear</button></div>
          <small class="roadmap-schedule-note">Planning only · Task execution eligibility is unchanged.</small>
        </div>`
      : "";
    const canonicalOwnership = canonical
      ? `<div class="roadmap-canonical-ownership"><div><span class="roadmap-canonical-badge">◇ Project ${esc(kindLabel(item.kind))} · Canonical</span><small>Title, description, and schedule are owned by Project Execution. Dragging or renaming this block updates the canonical asset and every Roadmap projects the result.</small></div><button type="button" class="btn small" data-hud-open-execution>Open in Execution</button></div>`
      : "";
    const projectedStateChip = canonical
      ? `<span title="Projected Project Execution state">● ${esc(projectAssetStateLabel(item))}</span>`
      : "";
    target.className = "roadmap-inspector roadmap-selection-hud";
    target.innerHTML = `<div class="roadmap-selection-summary"><div class="roadmap-selection-symbol kind-${esc(item.kind)}">${esc(kindGlyph(item.kind))}</div><div class="roadmap-selection-copy"><span class="eyebrow">${esc(kindLabel(item.kind))}</span><strong>${esc(itemTitle(item))}</strong><div class="roadmap-selection-chips"><span title="Roadmap lane">↕ ${esc(item.lane || "General")}</span>${item.project_phase?.title ? `<span title="Projected Project Phase">▰ ${esc(item.project_phase.title)}</span>` : ""}${bounds ? `<span title="Schedule">◷ ${esc(formatDay(bounds.start))}${bounds.start !== bounds.target ? ` → ${esc(formatDay(bounds.target))}` : ""}</span><span title="Duration">↔ ${bounds.durationDays}d</span>` : ""}${item.kind === "task" ? `<span title="Task state">● ${esc(taskStateLabel(task))}</span>` : ""}${projectedStateChip}</div></div><div class="roadmap-selection-actions">${item.kind === "task" && task?.availability === "active" ? '<button type="button" class="icon-btn" data-hud-open title="Open Task" aria-label="Open Task">↗</button>' : ""}${item.kind === "planned_task" ? '<button type="button" class="icon-btn" data-hud-promote title="Create Execraft Task" aria-label="Create Execraft Task">⚡</button>' : ""}${item.kind !== "task" ? '<button type="button" class="icon-btn" data-hud-rename title="Rename" aria-label="Rename">✎</button>' : ""}<button type="button" class="icon-btn danger" data-hud-delete title="Remove from Roadmap" aria-label="Remove from Roadmap">×</button></div></div>${canonicalOwnership}${scheduleEditor}`;
    target.querySelector("[data-hud-open]")?.addEventListener("click", () => this.#openItem(item));
    target.querySelector("[data-hud-open-execution]")?.addEventListener("click", () => this.#openProjectAsset(item));
    target.querySelector("[data-hud-promote]")?.addEventListener("click", () => this.onCreateTaskFromPlanned(item, {
      projectId: this.projectId,
      roadmapId: this.roadmap.id,
      revision: this.roadmap.revision,
      itemId: item.id,
      lane: item.lane,
      order: item.order,
    }));
    target.querySelector("[data-hud-rename]")?.addEventListener("click", () => this.#beginInlineRename(item.id));
    target.querySelector("[data-hud-delete]")?.addEventListener("click", () => this.#deleteItem(item));
    if (taskLike) this.#bindScheduleEditor(target, item);
  }

  #bindScheduleEditor(container, item) {
    const startInput = container.querySelector("[data-roadmap-schedule-start]");
    const targetInput = container.querySelector("[data-roadmap-schedule-target]");
    const durationInput = container.querySelector("[data-roadmap-schedule-duration]");
    const applyButton = container.querySelector("[data-roadmap-schedule-apply]");
    const clearButton = container.querySelector("[data-roadmap-schedule-clear]");
    if (!startInput || !targetInput || !durationInput || !applyButton || !clearButton) return;

    const refreshDuration = () => {
      const duration = scheduleDurationDays({ start: startInput.value, target: targetInput.value });
      durationInput.value = duration ? String(duration) : "";
      durationInput.setCustomValidity("");
    };
    startInput.addEventListener("change", () => {
      const duration = Number(durationInput.value || 0);
      if (startInput.value && Number.isInteger(duration) && duration > 0) {
        targetInput.value = scheduleWithDuration({ start: startInput.value }, duration).target;
      } else {
        refreshDuration();
      }
    });
    targetInput.addEventListener("change", refreshDuration);
    durationInput.addEventListener("change", () => {
      durationInput.setCustomValidity("");
      if (!startInput.value) {
        durationInput.setCustomValidity("Set a start date before changing duration");
        durationInput.reportValidity();
        return;
      }
      try {
        const schedule = scheduleWithDuration({ start: startInput.value, target: targetInput.value }, Number(durationInput.value));
        targetInput.value = schedule.target;
      } catch (error) {
        durationInput.setCustomValidity(error.message);
        durationInput.reportValidity();
      }
    });
    applyButton.addEventListener("click", () => {
      let start = startInput.value;
      let targetDate = targetInput.value;
      if (start && !targetDate) targetDate = start;
      if (targetDate && !start) start = targetDate;
      const startDay = parseDay(start);
      const targetDay = parseDay(targetDate);
      if (startDay === null || targetDay === null || startDay > targetDay) {
        this.toast("Task schedule requires valid dates with target on or after start", true);
        return;
      }
      void this.#persistSchedule(item, { start, target: targetDate }, "Task schedule updated");
    });
    clearButton.addEventListener("click", () => void this.#persistSchedule(item, {}, "Task schedule cleared"));
  }

  #handleCanvasClick(event) {
    if (event.target.closest("[data-roadmap-row-drag], [data-roadmap-lane-drag]")) return;
    const resizeHandle = event.target.closest("[data-roadmap-resize]");
    if (resizeHandle) {
      event.preventDefault();
      event.stopPropagation();
      const block = resizeHandle.closest("[data-roadmap-item]");
      if (block) this.#selectItem(block.dataset.roadmapItem);
      return;
    }
    if (this.placementKind) {
      if (event.target.closest("[data-roadmap-item], [data-roadmap-relation], .roadmap-row-label")) return;
      void this.#placeVisualItem(event);
      return;
    }
    const relation = event.target.closest("[data-roadmap-relation]");
    if (relation) {
      event.stopPropagation();
      this.selectedRelationKey = relation.dataset.roadmapRelation;
      this.selectedItemId = "";
      this.#renderTimeline();
      this.#renderInspector();
      return;
    }
    const deleteRelation = event.target.closest("[data-delete-selected-relation]");
    if (deleteRelation) {
      event.stopPropagation();
      void this.#deleteRelation(this.selectedRelationKey);
      return;
    }
    const edit = event.target.closest("[data-roadmap-edit]");
    if (edit) {
      event.stopPropagation();
      this.#selectItem(edit.dataset.roadmapEdit);
      return;
    }
    const primary = event.target.closest("[data-roadmap-primary]");
    if (primary) {
      this.#selectItem(primary.dataset.roadmapPrimary);
      return;
    }
    const block = event.target.closest("[data-roadmap-item]");
    if (block) {
      const itemId = block.dataset.roadmapItem;
      if (this.#suppressPostDragClick(itemId)) return;
      this.#selectItem(itemId);
    }
  }

  #handleCanvasDoubleClick(event) {
    const block = event.target.closest("[data-roadmap-item], [data-roadmap-row]");
    const itemId = block?.dataset.roadmapItem || block?.dataset.roadmapRow;
    const item = this.#item(itemId);
    if (!item) return;
    event.preventDefault();
    event.stopPropagation();
    if (item.kind === "planned_task") this.#beginInlineRename(item.id);
    else this.#openItem(item);
  }

  #handleCanvasKeydown(event) {
    const block = event.target.closest("[data-roadmap-item]");
    if (!block) return;
    const item = this.#item(block.dataset.roadmapItem);
    if (!item) return;
    const resizeHandle = event.target.closest("[data-roadmap-resize]");
    if (resizeHandle && ["ArrowLeft", "ArrowRight"].includes(event.key)) {
      event.preventDefault();
      event.stopPropagation();
      const direction = event.key === "ArrowRight" ? 1 : -1;
      const delta = direction * (event.shiftKey ? 7 : 1);
      const schedule = resizeSchedule(item.schedule || {}, resizeHandle.dataset.roadmapResize, delta);
      void this.#persistSchedule(item, schedule, `Schedule resized · ${scheduleSummary(schedule)}`);
      return;
    }
    if (event.key === " ") {
      event.preventDefault();
      this.#selectItem(item.id);
    } else if (event.key === "Enter") {
      event.preventDefault();
      this.#openItem(item);
    } else if (event.key === "F2" && item.kind !== "task") {
      event.preventDefault();
      this.#beginInlineRename(item.id);
    } else if ((event.key === "Delete" || event.key === "Backspace") && event.metaKey) {
      event.preventDefault();
      void this.#deleteItem(item);
    }
  }

  #selectItem(itemId) {
    const active = document.activeElement;
    let focusSelector = "";
    const escaped = CSS.escape(itemId);
    if (active?.matches?.(`[data-roadmap-primary="${escaped}"]`)) focusSelector = `[data-roadmap-primary="${escaped}"]`;
    else if (active?.matches?.(`[data-roadmap-edit="${escaped}"]`)) focusSelector = `[data-roadmap-edit="${escaped}"]`;
    else if (active?.closest?.(`[data-roadmap-item="${escaped}"]`)) focusSelector = `[data-roadmap-item="${escaped}"]`;
    this.selectedItemId = itemId;
    this.selectedRelationKey = "";
    this.#renderTimeline();
    this.#renderInspector();
    if (focusSelector) queueMicrotask(() => document.querySelector(focusSelector)?.focus({ preventScroll: true }));
  }

  #openItem(item) {
    if (!item) return;
    if (item.kind === "task" && item.task?.availability === "active") {
      this.#rememberViewState();
      void this.onOpenTask(this.projectId, item.task_id);
      return;
    }
    if (item.project_asset_id) {
      this.#openProjectAsset(item);
      return;
    }
    this.#selectItem(item.id);
  }

  #openProjectAsset(item) {
    if (!item?.project_asset_id) return;
    this.#rememberViewState();
    void this.onOpenProjectAsset(item.kind, item.project_asset_id);
  }

  #item(itemId) { return (this.roadmap?.items || []).find((item) => item.id === itemId) || null; }

  #togglePalette() {
    const palette = $("roadmapBlockPalette");
    const opening = palette.classList.contains("hidden");
    palette.classList.toggle("hidden", !opening);
    $("roadmapPaletteBtn").setAttribute("aria-expanded", String(opening));
  }

  #closePalette() {
    $("roadmapBlockPalette").classList.add("hidden");
    $("roadmapPaletteBtn").setAttribute("aria-expanded", "false");
  }

  #beginPlacement(kind) {
    this.placementKind = kind;
    this.#closePalette();
    const hint = $("roadmapPlacementHint");
    hint.textContent = `${kindGlyph(kind)} ${kindLabel(kind)} · click the timeline to place · Esc to cancel`;
    hint.classList.remove("hidden");
    $("roadmapTimelineScroll").classList.add("placing-block");
  }

  #cancelPlacement() {
    this.placementKind = "";
    $("roadmapPlacementHint")?.classList.add("hidden");
    $("roadmapTimelineScroll")?.classList.remove("placing-block");
    this.#clearDropRows();
    this.#hideDragGuide();
  }

  #confirmCanonicalInitialization(kind) {
    if (this.roadmap?.project_execution_configured) return Promise.resolve(true);
    const dialog = $("roadmapCanonicalInitializeDialog");
    const confirmButton = $("roadmapCanonicalInitializeConfirm");
    const cancelButton = $("roadmapCanonicalInitializeCancel");
    const message = $("roadmapCanonicalInitializeMessage");
    if (!dialog || !confirmButton || !cancelButton || !message) return Promise.resolve(false);
    message.textContent = `Creating this ${kindLabel(kind)} from the Roadmap initializes PROJECT_EXECUTION.yaml. The ${kindLabel(kind)} becomes a canonical Project Execution asset; the Roadmap stores only its reference and view layout.`;
    return new Promise((resolve) => {
      let finished = false;
      const finish = (accepted) => {
        if (finished) return;
        finished = true;
        confirmButton.removeEventListener("click", accept);
        cancelButton.removeEventListener("click", reject);
        dialog.removeEventListener("cancel", cancel);
        dialog.removeEventListener("close", closed);
        if (dialog.open) dialog.close();
        resolve(accepted);
      };
      const accept = () => finish(true);
      const reject = () => finish(false);
      const cancel = (event) => { event.preventDefault(); finish(false); };
      const closed = () => finish(false);
      confirmButton.addEventListener("click", accept);
      cancelButton.addEventListener("click", reject);
      dialog.addEventListener("cancel", cancel);
      dialog.addEventListener("close", closed);
      dialog.showModal();
    });
  }

  async #placeVisualItem(event) {
    if (!this.roadmap || !this.placementKind) return;
    const kind = this.placementKind;
    const day = this.#dayAtClient(event.clientX);
    const intent = this.#rowIntent(event.clientY);
    const durations = { planned_task: 13, phase: 29 };
    const title = { planned_task: "New task", phase: "New phase", milestone: "Milestone", gate: "Gate" }[kind] || "New item";
    const schedule = ["milestone", "gate"].includes(kind)
      ? { target: formatDay(day) }
      : { start: formatDay(day), target: formatDay(day + (durations[kind] || 0)) };
    const item = {
      id: visualItemId(kind),
      kind,
      title,
      lane: intent.lane || "General",
      order: 0,
      schedule,
    };
    this.#cancelPlacement();
    if (["phase", "gate", "milestone"].includes(kind)) {
      const accepted = await this.#confirmCanonicalInitialization(kind);
      if (!accepted) return;
    }
    try {
      let result = await this.api("/api/roadmap/item/upsert", {
        method: "POST",
        body: JSON.stringify({ project_id: this.projectId, roadmap_id: this.roadmap.id, expected_revision: this.roadmap.revision, expected_project_execution_revision: this.roadmap.project_execution_revision || 0, item }),
      });
      if (intent.targetItemId) {
        result = await this.api("/api/roadmap/item/move", {
          method: "POST",
          body: JSON.stringify({ project_id: this.projectId, roadmap_id: result.id, expected_revision: result.revision, expected_project_execution_revision: result.project_execution_revision || 0, item_id: item.id, lane: intent.lane, start: schedule.start || "", target: schedule.target || "", target_item_id: intent.targetItemId, placement: intent.placement }),
        });
      }
      this.roadmap = result;
      this.selectedItemId = item.id;
      this.selectedRelationKey = "";
      this.#refreshCatalogSummary(result);
      this.render();
      if (["phase", "gate", "milestone"].includes(kind)) this.onCanonicalChange();
      queueMicrotask(() => this.#beginInlineRename(item.id));
    } catch (error) {
      this.toast(error.message, true);
    }
  }

  #beginInlineRename(itemId) {
    const item = this.#item(itemId);
    if (!item || item.kind === "task") return;
    const row = $("roadmapTimelineCanvas").querySelector(`[data-roadmap-row="${CSS.escape(itemId)}"]`);
    const strong = row?.querySelector(".roadmap-label-button strong");
    if (!strong || strong.querySelector("input")) return;
    const input = document.createElement("input");
    input.className = "roadmap-inline-rename";
    input.value = item.title;
    input.maxLength = 500;
    strong.replaceWith(input);
    input.focus();
    input.select();
    let committed = false;
    const commit = async () => {
      if (committed) return;
      committed = true;
      const title = input.value.trim();
      if (!title || title === item.title) {
        this.#renderTimeline();
        return;
      }
      if (item.project_asset_id) {
        await this.#updateCanonicalAssetMetadata(item, { title }, "Canonical asset renamed");
      } else {
        await this.#upsert(itemMappingForSave(item, { title }), "Renamed");
      }
    };
    input.addEventListener("click", (event) => event.stopPropagation());
    input.addEventListener("pointerdown", (event) => event.stopPropagation());
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); void commit(); }
      if (event.key === "Escape") { committed = true; this.#renderTimeline(); }
    });
    input.addEventListener("blur", () => void commit(), { once: true });
  }

  #beginPointerInteraction(event) {
    if (!this.roadmap || event.button !== 0 || this.placementKind) return;
    const laneGrip = event.target.closest("[data-roadmap-lane-drag]");
    if (laneGrip) {
      this.#beginLaneRowDrag(event, laneGrip.dataset.roadmapLaneDrag);
      return;
    }
    const rowGrip = event.target.closest("[data-roadmap-row-drag]");
    if (rowGrip) {
      this.#beginItemRowDrag(event, rowGrip.dataset.roadmapRowDrag);
      return;
    }
    const connector = event.target.closest("[data-roadmap-connect-out]");
    if (connector) {
      this.#beginConnection(event, connector.dataset.roadmapConnectOut);
      return;
    }
    if (event.target.closest("[data-roadmap-connect-in]")) return;
    const element = event.target.closest("[data-roadmap-item]");
    if (!element) return;
    const item = this.#item(element.dataset.roadmapItem);
    const bounds = scheduleBounds(item);
    if (!item || !bounds) return;
    const resize = event.target.closest("[data-roadmap-resize]")?.dataset.roadmapResize || "move";
    const sourceRowIndex = this.timelineRows.findIndex((row) => row.type === "item" && row.item.id === item.id);
    this.drag = {
      pointerId: event.pointerId,
      item,
      mode: resize,
      startX: event.clientX,
      startY: event.clientY,
      startDate: item.schedule?.start || "",
      targetDate: item.schedule?.target || "",
      sourceRowIndex,
      dropIntent: { lane: item.lane || "General", targetItemId: "", placement: "end", rowIndex: sourceRowIndex },
      moved: false,
      element,
      originalWidth: element.getBoundingClientRect().width,
    };
    element.setPointerCapture?.(event.pointerId);
    element.classList.add("dragging");
    document.body.classList.add("roadmap-drag-active");
    if (item.project_asset_id) {
      document.body.classList.add("roadmap-canonical-drag-active");
      this.#showPlacementHint(`◇ Canonical Project ${kindLabel(item.kind)} · dragging changes Project Execution schedule`);
    }
    event.preventDefault();
  }

  #beginItemRowDrag(event, itemId) {
    const item = this.#item(itemId);
    const rowElement = event.target.closest(".roadmap-item-row");
    const sourceRowIndex = this.timelineRows.findIndex((row) => row.type === "item" && row.item.id === itemId);
    if (!item || !rowElement || sourceRowIndex < 0) return;
    this.rowDrag = {
      type: "item",
      pointerId: event.pointerId,
      item,
      sourceRowIndex,
      startY: event.clientY,
      intent: { lane: item.lane || "General", targetItemId: "", placement: "end", rowIndex: sourceRowIndex },
      moved: false,
      elements: [rowElement],
    };
    event.target.setPointerCapture?.(event.pointerId);
    rowElement.classList.add("roadmap-row-dragging");
    document.body.classList.add("roadmap-row-drag-active");
    this.#showPlacementHint(`↕ ${item.lane || "General"} · drag row`);
    event.preventDefault();
    event.stopPropagation();
  }

  #beginLaneRowDrag(event, lane) {
    const sourceRowIndex = this.timelineRows.findIndex((row) => row.type === "lane" && row.lane === lane);
    if (sourceRowIndex < 0) return;
    const sourceIndices = this.timelineRows
      .map((row, index) => ({ row, index }))
      .filter(({ row }) => row.type === "lane" ? row.lane === lane : row.item?.lane === lane)
      .map(({ index }) => index);
    const elements = sourceIndices
      .map((index) => $("roadmapTimelineCanvas").querySelector(`[data-roadmap-row-index="${index}"]`))
      .filter(Boolean);
    if (!elements.length) return;
    this.rowDrag = {
      type: "lane",
      pointerId: event.pointerId,
      lane,
      sourceRowIndex,
      sourceIndices,
      startY: event.clientY,
      intent: { targetLane: "", placement: "before", rowIndex: sourceRowIndex },
      moved: false,
      elements,
    };
    event.target.setPointerCapture?.(event.pointerId);
    elements.forEach((element) => element.classList.add("roadmap-lane-group-dragging"));
    document.body.classList.add("roadmap-row-drag-active");
    this.#showPlacementHint(`⠿ ${lane} · drag lane`);
    event.preventDefault();
    event.stopPropagation();
  }

  #moveRowDrag(event) {
    const drag = this.rowDrag;
    if (!drag) return;
    const deltaY = event.clientY - drag.startY;
    if (Math.abs(deltaY) > 3) drag.moved = true;
    if (drag.type === "item") {
      drag.intent = this.#rowIntent(event.clientY, drag.item.id);
      drag.elements[0].style.transform = `translateY(${deltaY}px)`;
      this.#markDropRow(drag.intent.rowIndex);
      this.#showPlacementHint(`↕ ${drag.intent.lane} · move row`);
      return;
    }
    drag.intent = this.#laneIntent(event.clientY, drag.lane);
    drag.elements.forEach((element) => { element.style.transform = `translateY(${deltaY}px)`; });
    this.#markLaneDrop(drag.intent);
    this.#showPlacementHint(drag.intent.targetLane ? `⠿ ${drag.lane} ${drag.intent.placement === "before" ? "↑" : "↓"} ${drag.intent.targetLane}` : `⠿ ${drag.lane}`);
  }

  async #endRowDrag() {
    const drag = this.rowDrag;
    if (!drag) return;
    this.rowDrag = null;
    drag.elements.forEach((element) => {
      element.style.transform = "";
      element.classList.remove("roadmap-row-dragging", "roadmap-lane-group-dragging");
    });
    document.body.classList.remove("roadmap-row-drag-active");
    this.#clearDropRows();
    this.#clearLaneDrops();
    $("roadmapPlacementHint").classList.add("hidden");
    if (!drag.moved) return;
    if (drag.type === "lane") {
      if (!drag.intent.targetLane) return;
      await this.#moveLane(drag.lane, drag.intent.targetLane, drag.intent.placement);
      return;
    }
    if (drag.intent.rowIndex === drag.sourceRowIndex) return;
    await this.#moveItem(drag.item, {
      start: drag.item.schedule?.start || "",
      target: drag.item.schedule?.target || "",
      ...drag.intent,
    });
  }

  #movePointerInteraction(event) {
    if (this.placementKind && !this.connection && !this.drag && !this.rowDrag) {
      this.#updatePlacementPreview(event);
      return;
    }
    if (this.connection && event.pointerId === this.connection.pointerId) {
      this.#moveConnection(event);
      return;
    }
    if (this.rowDrag && event.pointerId === this.rowDrag.pointerId) {
      this.#moveRowDrag(event);
      return;
    }
    if (!this.drag || event.pointerId !== this.drag.pointerId) return;
    const ppd = Number($("roadmapTimelineCanvas").dataset.pixelsPerDay || 1);
    const requestedDelta = Math.round((event.clientX - this.drag.startX) / ppd);
    const previewSchedule = this.#previewScheduleValues(this.drag, requestedDelta);
    const originalWindow = scheduleWindow({ start: this.drag.startDate, target: this.drag.targetDate });
    const previewWindow = scheduleWindow(previewSchedule);
    let delta = requestedDelta;
    if (this.drag.mode === "start" && originalWindow && previewWindow) delta = previewWindow.start - originalWindow.start;
    if (this.drag.mode === "target" && originalWindow && previewWindow) delta = previewWindow.target - originalWindow.target;
    const x = delta * ppd;
    let y = 0;
    if (this.drag.mode === "move") {
      if (this.groupMode === "lane") {
        const intent = this.#rowIntent(event.clientY, this.drag.item.id);
        this.drag.dropIntent = intent;
        y = (intent.rowIndex - this.drag.sourceRowIndex) * ROW_HEIGHT;
        this.#markDropRow(intent.rowIndex);
      } else {
        this.#clearDropRows();
      }
      this.drag.element.style.transform = `translate(${x}px, ${y}px)`;
    } else if (this.drag.mode === "start") {
      this.drag.element.style.transform = `translateX(${x}px)`;
      this.drag.element.style.width = `${Math.max(22, this.drag.originalWidth - x)}px`;
    } else {
      this.drag.element.style.transform = "";
      this.drag.element.style.width = `${Math.max(22, this.drag.originalWidth + x)}px`;
    }
    if (Math.abs(event.clientX - this.drag.startX) > 3 || Math.abs(event.clientY - this.drag.startY) > 3) this.drag.moved = true;
    this.drag.delta = delta;
    const ownership = this.drag.item.project_asset_id ? `◇ canonical Project ${kindLabel(this.drag.item.kind)} · ` : "";
    const grouping = this.groupMode === "phase" ? "Phase grouping is view-only · " : "";
    this.#showPlacementHint(`${ownership}${grouping}${scheduleSummary(previewSchedule)}`);
    const previewDay = parseDay(previewSchedule.start || previewSchedule.target);
    if (previewDay !== null) this.#showDragGuide(previewDay);
  }

  async #endPointerInteraction(event) {
    if (this.connection && event.pointerId === this.connection.pointerId) {
      await this.#endConnection(event);
      return;
    }
    if (this.rowDrag && event.pointerId === this.rowDrag.pointerId) {
      await this.#endRowDrag(event);
      return;
    }
    if (!this.drag || event.pointerId !== this.drag.pointerId) return;
    const drag = this.drag;
    this.drag = null;
    drag.element.classList.remove("dragging");
    drag.element.style.transform = "";
    drag.element.style.width = "";
    document.body.classList.remove("roadmap-drag-active");
    document.body.classList.remove("roadmap-canonical-drag-active");
    this.#clearDropRows();
    this.#hideDragGuide();
    $("roadmapPlacementHint").classList.add("hidden");
    if (!drag.moved) return;
    this.lastDrag = { itemId: drag.item.id, finishedAt: Date.now() };
    const schedule = this.#previewScheduleValues(drag, drag.delta || 0);
    if (this.groupMode === "phase" || drag.mode !== "move" || drag.dropIntent.rowIndex === drag.sourceRowIndex) {
      await this.#persistSchedule(drag.item, schedule, drag.item.project_asset_id ? "Canonical schedule updated" : "Schedule updated");
      return;
    }
    await this.#moveItem(drag.item, { start: schedule.start || "", target: schedule.target || "", ...drag.dropIntent });
  }

  #previewScheduleValues(drag, delta) {
    const schedule = { start: drag.startDate, target: drag.targetDate };
    if (drag.mode === "move") return shiftSchedule(schedule, delta);
    return resizeSchedule(schedule, drag.mode, delta);
  }

  async #persistSchedule(item, schedule, successMessage = "Schedule updated") {
    const normalized = {
      ...(schedule?.start ? { start: schedule.start } : {}),
      ...(schedule?.target ? { target: schedule.target } : {}),
    };
    if (item.project_asset_id) {
      await this.#updateCanonicalAssetMetadata(item, { schedule: normalized }, successMessage);
      return;
    }
    await this.#upsert(itemMappingForSave(item, { schedule: normalized }), successMessage);
  }

  async #moveItem(item, { start, target, lane, targetItemId, placement }) {
    try {
      const result = await this.api("/api/roadmap/item/move", {
        method: "POST",
        body: JSON.stringify({
          project_id: this.projectId,
          roadmap_id: this.roadmap.id,
          expected_revision: this.roadmap.revision,
          expected_project_execution_revision: this.roadmap.project_execution_revision || 0,
          item_id: item.id,
          lane: lane || item.lane || "General",
          start: start || "",
          target: target || "",
          target_item_id: targetItemId || "",
          placement: placement || "end",
        }),
      });
      this.roadmap = result;
      this.selectedItemId = item.id;
      this.#refreshCatalogSummary(result);
      this.render();
      if (item.project_asset_id) this.onCanonicalChange();
    } catch (error) {
      this.toast(error.message, true);
      this.#showInlineState(error.message, this.#isRevisionConflict(error));
      if (String(error.message).includes("refresh before")) await this.open(this.roadmap.id, { remember: false });
    }
  }

  async #moveLane(lane, targetLane, placement) {
    try {
      const result = await this.api("/api/roadmap/lane/move", {
        method: "POST",
        body: JSON.stringify({
          project_id: this.projectId,
          roadmap_id: this.roadmap.id,
          expected_revision: this.roadmap.revision,
          lane,
          target_lane: targetLane,
          placement,
        }),
      });
      this.roadmap = result;
      this.#refreshCatalogSummary(result);
      this.render();
    } catch (error) {
      this.toast(error.message, true);
      this.#showInlineState(error.message, this.#isRevisionConflict(error));
      if (String(error.message).includes("refresh before")) await this.open(this.roadmap.id, { remember: false });
    }
  }

  #beginConnection(event, sourceId) {
    const source = this.#item(sourceId);
    if (!source || !scheduleBounds(source)) return;
    const canvasRect = $("roadmapTimelineCanvas").getBoundingClientRect();
    const sourceElement = event.target.closest("[data-roadmap-item]");
    const rect = event.target.getBoundingClientRect();
    this.connection = {
      pointerId: event.pointerId,
      sourceId,
      startX: rect.left + rect.width / 2 - canvasRect.left,
      startY: rect.top + rect.height / 2 - canvasRect.top - HEADER_HEIGHT,
    };
    sourceElement.classList.add("connecting");
    $("roadmapTimelineCanvas").classList.add("connecting");
    this.#showPlacementHint("Drag to another block’s input dot");
    event.preventDefault();
    event.stopPropagation();
  }

  #moveConnection(event) {
    const preview = $("roadmapConnectorPreview");
    if (!preview || !this.connection) return;
    const canvasRect = $("roadmapTimelineCanvas").getBoundingClientRect();
    const x2 = event.clientX - canvasRect.left;
    const y2 = event.clientY - canvasRect.top - HEADER_HEIGHT;
    preview.setAttribute("d", connectorCurve(this.connection.startX, this.connection.startY, x2, y2));
    preview.classList.remove("hidden");
    document.querySelectorAll(".roadmap-connector.input.connection-target").forEach((node) => node.classList.remove("connection-target"));
    const target = document.elementFromPoint(event.clientX, event.clientY)?.closest("[data-roadmap-connect-in]");
    if (target && target.dataset.roadmapConnectIn !== this.connection.sourceId) target.classList.add("connection-target");
  }

  async #endConnection(event) {
    const connection = this.connection;
    if (!connection) return;
    const target = document.elementFromPoint(event.clientX, event.clientY)?.closest("[data-roadmap-connect-in]");
    const targetId = target?.dataset.roadmapConnectIn || "";
    this.#cancelConnection();
    if (!targetId || targetId === connection.sourceId) return;
    await this.#addRelation(connection.sourceId, targetId);
  }

  #cancelConnection() {
    if (!this.connection) return;
    document.querySelector(`[data-roadmap-item="${CSS.escape(this.connection.sourceId)}"]`)?.classList.remove("connecting");
    this.connection = null;
    $("roadmapTimelineCanvas")?.classList.remove("connecting");
    $("roadmapConnectorPreview")?.classList.add("hidden");
    document.querySelectorAll(".roadmap-connector.input.connection-target").forEach((node) => node.classList.remove("connection-target"));
    $("roadmapPlacementHint")?.classList.add("hidden");
  }

  async #addRelation(source, target) {
    try {
      const result = await this.api("/api/roadmap/relation/upsert", {
        method: "POST",
        body: JSON.stringify({ project_id: this.projectId, roadmap_id: this.roadmap.id, expected_revision: this.roadmap.revision, from: source, to: target, kind: "blocks" }),
      });
      this.roadmap = result;
      this.selectedRelationKey = `${source}|${target}|blocks`;
      this.selectedItemId = "";
      this.render();
    } catch (error) {
      this.toast(error.message, true);
    }
  }

  async #deleteRelation(encoded) {
    if (!encoded) return;
    const [source, target, kind] = String(encoded).split("|");
    try {
      const result = await this.api("/api/roadmap/relation/delete", {
        method: "POST",
        body: JSON.stringify({ project_id: this.projectId, roadmap_id: this.roadmap.id, expected_revision: this.roadmap.revision, from: source, to: target, kind }),
      });
      this.roadmap = result;
      this.selectedRelationKey = "";
      this.render();
    } catch (error) {
      this.toast(error.message, true);
    }
  }

  #dayAtClient(clientX) {
    const canvas = $("roadmapTimelineCanvas");
    return clientToTimelineDay({
      clientX,
      canvasRect: canvas.getBoundingClientRect(),
      labelWidth: LABEL_WIDTH,
      domainStart: Number(canvas.dataset.domainStart || todayDay()),
      pixelsPerDay: Number(canvas.dataset.pixelsPerDay || 1),
    });
  }

  #rowIntent(clientY, movingItemId = "") {
    const body = $("roadmapTimelineCanvas").querySelector(".roadmap-timeline-body");
    if (!body) return { lane: "General", targetItemId: "", placement: "end", rowIndex: 0 };
    return rowDropIntent({ rows: this.timelineRows, clientY, bodyTop: body.getBoundingClientRect().top, rowHeight: ROW_HEIGHT, movingItemId });
  }

  #laneIntent(clientY, movingLane) {
    const body = $("roadmapTimelineCanvas").querySelector(".roadmap-timeline-body");
    if (!body) return { targetLane: "", placement: "before", rowIndex: 0 };
    return laneDropIntent({ rows: this.timelineRows, clientY, bodyTop: body.getBoundingClientRect().top, rowHeight: ROW_HEIGHT, movingLane });
  }

  #markLaneDrop(intent) {
    this.#clearLaneDrops();
    if (!intent?.targetLane) return;
    const target = $("roadmapTimelineCanvas").querySelector(`[data-roadmap-row-index="${intent.rowIndex}"]`);
    target?.classList.add("roadmap-lane-drop-target", `drop-${intent.placement}`);
  }

  #clearLaneDrops() {
    $("roadmapTimelineCanvas")?.querySelectorAll(".roadmap-lane-drop-target").forEach((row) => row.classList.remove("roadmap-lane-drop-target", "drop-before", "drop-after"));
  }

  #markDropRow(rowIndex) {
    this.#clearDropRows();
    $("roadmapTimelineCanvas").querySelector(`[data-roadmap-row-index="${rowIndex}"]`)?.classList.add("roadmap-drop-target");
  }

  #clearDropRows() {
    $("roadmapTimelineCanvas")?.querySelectorAll(".roadmap-drop-target").forEach((row) => row.classList.remove("roadmap-drop-target"));
  }

  #updatePlacementPreview(event) {
    const canvas = $("roadmapTimelineCanvas");
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    if (event.clientX < rect.left + LABEL_WIDTH || event.clientX > rect.right || event.clientY < rect.top + HEADER_HEIGHT || event.clientY > rect.bottom) {
      this.#clearDropRows();
      this.#hideDragGuide();
      return;
    }
    const day = this.#dayAtClient(event.clientX);
    const intent = this.#rowIntent(event.clientY);
    this.#markDropRow(intent.rowIndex);
    this.#showDragGuide(day);
    this.#showPlacementHint(`${kindGlyph(this.placementKind)} ${kindLabel(this.placementKind)} · ${formatDay(day)} · ${intent.lane}`);
  }

  #showDragGuide(day) {
    const guide = $("roadmapDragGuide");
    if (!guide || !this.timelineDomain) return;
    guide.style.left = `${LABEL_WIDTH + (day - this.timelineDomain.start) * this.timelineDomain.pixelsPerDay}px`;
    guide.classList.remove("hidden");
  }

  #hideDragGuide() {
    $("roadmapDragGuide")?.classList.add("hidden");
  }

  #showPlacementHint(text) {
    const hint = $("roadmapPlacementHint");
    hint.textContent = text;
    hint.classList.remove("hidden");
  }

  #suppressPostDragClick(itemId) {
    return Boolean(this.lastDrag && this.lastDrag.itemId === itemId && Date.now() - this.lastDrag.finishedAt < 400);
  }

  async #updateCanonicalAssetMetadata(item, overrides = {}, successMessage = "Project asset updated") {
    if (!this.roadmap || !item.project_asset_id) return;
    const metadata = {
      title: overrides.title ?? item.title ?? item.project_asset_id,
      description: overrides.description ?? item.description ?? "",
      schedule: overrides.schedule ?? item.schedule ?? {},
    };
    try {
      await this.api(`/api/project-execution/${item.kind}/metadata`, {
        method: "POST",
        body: JSON.stringify({
          project_id: this.projectId,
          [`${item.kind}_id`]: item.project_asset_id,
          expected_revision: this.roadmap.project_execution_revision || 0,
          metadata,
        }),
      });
      await this.open(this.roadmap.id, { remember: false });
      this.selectedItemId = item.id;
      this.#renderInspector();
      this.onCanonicalChange();
      if (successMessage) this.toast(successMessage);
    } catch (error) {
      this.toast(error.message, true);
      this.#showInlineState(error.message, this.#isRevisionConflict(error));
      if (String(error.message).includes("refresh before")) await this.open(this.roadmap.id, { remember: false });
    }
  }

  async #upsert(item, successMessage = "Roadmap updated") {
    if (!this.roadmap) return;
    try {
      const result = await this.api("/api/roadmap/item/upsert", {
        method: "POST",
        body: JSON.stringify({ project_id: this.projectId, roadmap_id: this.roadmap.id, expected_revision: this.roadmap.revision, expected_project_execution_revision: this.roadmap.project_execution_revision || 0, item }),
      });
      this.roadmap = result;
      this.#refreshCatalogSummary(result);
      this.render();
      if (successMessage) this.toast(successMessage);
    } catch (error) {
      this.toast(error.message, true);
      this.#showInlineState(error.message, this.#isRevisionConflict(error));
      if (String(error.message).includes("refresh before")) await this.open(this.roadmap.id, { remember: false });
    }
  }

  async #deleteItem(item) {
    if (!confirm(`Remove “${itemTitle(item)}” from this roadmap?\n\nThe linked Execraft task, if any, will not be deleted.`)) return;
    try {
      const result = await this.api("/api/roadmap/item/delete", {
        method: "POST",
        body: JSON.stringify({ project_id: this.projectId, roadmap_id: this.roadmap.id, expected_revision: this.roadmap.revision, item_id: item.id }),
      });
      this.roadmap = result;
      this.selectedItemId = "";
      this.selectedRelationKey = "";
      this.#refreshCatalogSummary(result);
      this.render();
      this.toast("Roadmap block removed; task data was untouched");
    } catch (error) {
      this.toast(error.message, true);
    }
  }

  async #linkTask(taskId, target, intent) {
    try {
      let result = await this.api("/api/roadmap/item/link-task", {
        method: "POST",
        body: JSON.stringify({ project_id: this.projectId, roadmap_id: this.roadmap.id, expected_revision: this.roadmap.revision, task_id: taskId, lane: intent?.lane || "General", target, order: 0 }),
      });
      const linked = result.items.find((item) => item.kind === "task" && item.task_id === taskId);
      if (linked && intent?.targetItemId) {
        result = await this.api("/api/roadmap/item/move", {
          method: "POST",
          body: JSON.stringify({ project_id: this.projectId, roadmap_id: result.id, expected_revision: result.revision, item_id: linked.id, lane: intent.lane, start: "", target, target_item_id: intent.targetItemId, placement: intent.placement }),
        });
      }
      this.roadmap = result;
      this.selectedItemId = linked?.id || "";
      this.selectedRelationKey = "";
      this.#refreshCatalogSummary(result);
      this.render();
    } catch (error) {
      this.toast(error.message, true);
    }
  }

  #dropTask(event) {
    const taskId = event.dataTransfer?.getData("application/x-execraft-task");
    if (!taskId || !this.roadmap) return;
    event.preventDefault();
    const target = formatDay(this.#dayAtClient(event.clientX));
    const intent = this.#rowIntent(event.clientY);
    void this.#linkTask(taskId, target, intent);
  }

  #openRoadmapDialog({ edit = false } = {}) {
    const dialog = $("roadmapCreateDialog");
    dialog.dataset.mode = edit ? "edit" : "create";
    $("roadmapCreateTitle").textContent = edit ? "Edit roadmap" : "New roadmap";
    $("roadmapNameInput").value = edit ? this.roadmap?.title || "" : "";
    $("roadmapDescriptionInput").value = edit ? this.roadmap?.description || "" : "";
    $("roadmapIdInput").value = edit ? this.roadmap?.id || "" : "";
    $("roadmapIdInput").disabled = edit;
    dialog.showModal();
    $("roadmapNameInput").focus();
  }

  async #saveRoadmapDialog() {
    const edit = $("roadmapCreateDialog").dataset.mode === "edit";
    const payload = {
      project_id: this.projectId,
      roadmap_id: $("roadmapIdInput").value.trim(),
      title: $("roadmapNameInput").value.trim(),
      description: $("roadmapDescriptionInput").value.trim(),
    };
    if (!payload.title) return this.toast("Roadmap title is required", true);
    const path = edit ? "/api/roadmap/metadata/update" : "/api/roadmap/create";
    if (edit) payload.expected_revision = this.roadmap.revision;
    try {
      const result = await this.api(path, { method: "POST", body: JSON.stringify(payload) });
      $("roadmapCreateDialog").close();
      this.roadmap = result;
      this.#refreshCatalogSummary(result);
      localStorage.setItem(`execraft-roadmap:${this.projectId}`, result.id);
      this.#renderRoadmapSelector();
      this.render();
      this.toast(edit ? "Roadmap details updated" : "Roadmap created");
    } catch (error) {
      this.toast(error.message, true);
    }
  }

  async #deleteRoadmap() {
    if (!this.roadmap) return;
    if (!confirm(`Delete roadmap “${this.roadmap.title}”?\n\nOnly the roadmap planning document is deleted. Linked Execraft tasks remain untouched.`)) return;
    try {
      const removedId = this.roadmap.id;
      await this.api("/api/roadmap/delete", {
        method: "POST",
        body: JSON.stringify({ project_id: this.projectId, roadmap_id: removedId, expected_revision: this.roadmap.revision, acknowledged: true }),
      });
      localStorage.removeItem(`execraft-roadmap:${this.projectId}`);
      this.roadmap = null;
      this.catalog = this.catalog.filter((item) => item.id !== removedId);
      this.selectedItemId = "";
      this.selectedRelationKey = "";
      $("projectRoadmapsCount").textContent = String(this.catalog.length);
      this.#renderRoadmapSelector();
      if (this.catalog.length) await this.open(this.catalog[0].id);
      else this.#renderEmpty("No roadmap yet. Create one to plan project work visually.");
      this.toast("Roadmap deleted; linked tasks were preserved");
    } catch (error) {
      this.toast(error.message, true);
    }
  }


  #showCoordinationState(coordination) {
    const road = coordination.roadmap || {};
    const execution = coordination.project_execution || {};
    const message = `${coordination.message || "Canonical Roadmap coordination is pending"} Roadmap ${road.state || "unknown"} (rev ${road.current_revision || 0}); Project Execution ${execution.state || "unknown"} (rev ${execution.current_revision || 0}). Open Project Execution for typed recovery details.`;
    this.#showInlineState(message, true);
  }

  #isRevisionConflict(error) {
    return /revision|conflict|refresh before|changed concurrently|stale/i.test(String(error?.message || error || ""));
  }

  #showInlineState(message, conflict = false) {
    const target = $("roadmapInlineState");
    if (!target) return;
    target.className = `roadmap-inline-state ${conflict ? "conflict" : "error"}`;
    target.setAttribute("role", conflict ? "status" : "alert");
    target.innerHTML = `<div><strong>${conflict ? "Roadmap changed concurrently" : "Roadmap action failed"}</strong><span>${esc(message)}</span></div><button type="button" class="btn tiny" data-roadmap-inline-refresh>Refresh</button>`;
    target.querySelector("[data-roadmap-inline-refresh]")?.addEventListener("click", () => this.load({ force: true }));
  }

  #clearInlineState() {
    const target = $("roadmapInlineState");
    if (!target) return;
    target.className = "roadmap-inline-state hidden";
    target.setAttribute("role", "status");
    target.innerHTML = "";
  }

  #rememberViewState() {
    if (!this.projectId || !this.roadmap) return;
    const scroll = $("roadmapTimelineScroll");
    try {
      sessionStorage.setItem(`execraft-roadmap-view:${this.projectId}:${this.roadmap.id}`, JSON.stringify({ zoom: this.zoom, fitMode: this.fitMode, groupMode: this.groupMode, taskTrayCollapsed: this.taskTrayCollapsed, scrollLeft: scroll.scrollLeft, scrollTop: scroll.scrollTop }));
      this.restoreViewOnNextRender = true;
    } catch (_error) { /* optional browser storage */ }
  }

  #restoreViewState() {
    if (!this.projectId || !this.roadmap) return;
    this.restoreViewOnNextRender = false;
    try {
      const raw = sessionStorage.getItem(`execraft-roadmap-view:${this.projectId}:${this.roadmap.id}`);
      if (!raw) {
        this.#fitPlan({ behavior: "auto" });
        return;
      }
      const state = JSON.parse(raw);
      // View state written before GUI-R1 had no fitMode and can preserve the
      // old, badly framed horizontal viewport. Migrate it by discarding that
      // geometry once and adopting the new plan-focused default.
      if (!Object.prototype.hasOwnProperty.call(state, "fitMode")) {
        this.#fitPlan({ behavior: "auto" });
        return;
      }
      const previousFitMode = this.fitMode;
      const previousGroupMode = this.groupMode;
      this.fitMode = Boolean(state.fitMode);
      this.groupMode = state.groupMode === "phase" ? "phase" : "lane";
      this.taskTrayCollapsed = Boolean(state.taskTrayCollapsed);
      $("roadmapGroupBy").value = this.groupMode;
      $("roadmapTaskTray").classList.toggle("collapsed", this.taskTrayCollapsed);
      $("roadmapTaskTrayToggle").textContent = this.taskTrayCollapsed ? "Show" : "Hide";
      $("roadmapTaskTrayToggle").setAttribute("aria-expanded", String(!this.taskTrayCollapsed));
      if (["month", "quarter", "year"].includes(state.zoom) && state.zoom !== this.zoom) {
        this.zoom = state.zoom;
        this.#renderTimeline();
      } else if (previousFitMode !== this.fitMode || previousGroupMode !== this.groupMode || this.fitMode) {
        this.#renderTimeline();
      }
      const scroll = $("roadmapTimelineScroll");
      queueMicrotask(() => {
        scroll.scrollLeft = Math.max(0, Number(state.scrollLeft || 0));
        scroll.scrollTop = Math.max(0, Number(state.scrollTop || 0));
      });
    } catch (_error) { /* corrupt/session-disabled state is non-fatal */ }
  }

  #scrollToday() {
    this.fitMode = false;
    this.#renderTimeline();
    const target = $("roadmapTimelineScroll");
    const domainStart = Number($("roadmapTimelineCanvas").dataset.domainStart);
    const ppd = Number($("roadmapTimelineCanvas").dataset.pixelsPerDay);
    const todayX = LABEL_WIDTH + (todayDay() - domainStart) * ppd;
    queueMicrotask(() => target.scrollTo({ left: Math.max(0, todayX - target.clientWidth / 2), behavior: "smooth" }));
  }

  #fitPlan({ behavior = "smooth" } = {}) {
    this.fitMode = true;
    this.#renderTimeline();
    const target = $("roadmapTimelineScroll");
    queueMicrotask(() => target.scrollTo({ left: 0, behavior }));
  }

  #selectedScheduledItem() {
    const item = this.#item(this.selectedItemId);
    return item && scheduleBounds(item) ? item : null;
  }

  #scrollSelected() {
    const item = this.#selectedScheduledItem();
    if (!item) return;
    const bounds = scheduleBounds(item);
    const target = $("roadmapTimelineScroll");
    const domainStart = Number($("roadmapTimelineCanvas").dataset.domainStart);
    const ppd = Number($("roadmapTimelineCanvas").dataset.pixelsPerDay);
    const midpoint = LABEL_WIDTH + (((bounds.start + bounds.target) / 2) - domainStart) * ppd;
    target.scrollTo({ left: Math.max(0, midpoint - target.clientWidth / 2), behavior: "smooth" });
  }

  #syncTimelineToolbar() {
    document.querySelectorAll("[data-roadmap-zoom]").forEach((button) => {
      const active = !this.fitMode && button.dataset.roadmapZoom === this.zoom;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    const fit = $("roadmapFitBtn");
    if (fit) {
      fit.classList.toggle("active", this.fitMode);
      fit.setAttribute("aria-pressed", String(this.fitMode));
    }
    const selected = $("roadmapSelectedBtn");
    if (selected) selected.disabled = !this.#selectedScheduledItem();
  }
}
