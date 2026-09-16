import { truncateText } from "./ui-utils.js";

function workPackageEvidenceSignature(value) {
  if (!value) return "";
  if (Array.isArray(value)) {
    return value
      .slice(-5)
      .map((item) => workPackageEvidenceSignature(item))
      .join("|");
  }
  if (typeof value !== "object") return truncateText(String(value), 160);
  // Evidence payloads are intentionally compact today, but they may contain
  // provider text. Build a bounded signature from shallow metadata instead of
  // serializing the whole package on every dashboard poll.
  return Object.entries(value)
    .slice(0, 16)
    .map(([key, item]) => {
      if (item === null || item === undefined) return `${key}:`;
      if (Array.isArray(item)) return `${key}:[${item.length}]`;
      if (typeof item === "object") return `${key}:{${Object.keys(item).length}}`;
      return `${key}:${truncateText(String(item), 120)}`;
    })
    .join("|");
}

function workPackageRenderState(packageInfo) {
  return {
    id: packageInfo.id,
    title: packageInfo.title,
    kind: packageInfo.kind || "",
    stage: packageInfo.stage,
    status: packageInfo.status,
    risk: packageInfo.risk,
    priority: packageInfo.priority,
    complexity: packageInfo.complexity,
    computed_complexity: packageInfo.computed_complexity,
    dependencies: packageInfo.dependencies || [],
    requirements: packageInfo.requirements || [],
    acceptance_criteria: packageInfo.acceptance_criteria || [],
    affected_repositories: packageInfo.affected_repositories || [],
    read_scope: packageInfo.read_scope || [],
    write_scope: packageInfo.write_scope || [],
    conflict_keys: packageInfo.conflict_keys || [],
    parent_id: packageInfo.parent_id || "",
    shard_key: packageInfo.shard_key || "",
    shard_ids: packageInfo.shard_ids || [],
    parallel_safe: Boolean(packageInfo.parallel_safe),
    operator_paused: Boolean(packageInfo.operator_paused),
    operator_pause_reason: packageInfo.operator_pause_reason || "",
    pause_before_start: Boolean(packageInfo.pause_before_start),
    pause_before_start_reason: packageInfo.pause_before_start_reason || "",
    pause_before_start_reached_at: packageInfo.pause_before_start_reached_at || "",
    decomposition_status: packageInfo.decomposition_status || "",
    decomposition_origin_stage: packageInfo.decomposition_origin_stage || "",
    decomposition_reason: packageInfo.decomposition_reason || "",
    decomposition_required: Boolean(packageInfo.decomposition_required),
    decomposition_required_reason: packageInfo.decomposition_required_reason || "",
    decomposition_required_consumed_at:
      packageInfo.decomposition_required_consumed_at || "",
    review_recovery_cycles: packageInfo.review_recovery_cycles || 0,
    review_recovery_origin_stage: packageInfo.review_recovery_origin_stage || "",
    implementation_summary: packageInfo.implementation_summary || "",
    review_findings: packageInfo.review_findings || [],
    agent_preferences: packageInfo.agent_preferences || {},
    agent_preference_binding_roles:
      packageInfo.agent_preference_binding_roles || [],
    skill_preferences: packageInfo.skill_preferences || {},
    repository_sync_requested: Boolean(packageInfo.repository_sync_requested),
    directive_pending_sync: packageInfo.directive_pending_sync || [],
    agent_id: packageInfo.agent_id || "",
    reviewer_id: packageInfo.reviewer_id || "",
    final_reviewer_id: packageInfo.final_reviewer_id || "",
    last_fixer_id: packageInfo.last_fixer_id || "",
    decomposition_agent_id: packageInfo.decomposition_agent_id || "",
    evidence: {
      implementation: workPackageEvidenceSignature(packageInfo.last_implementation),
      verification: workPackageEvidenceSignature(packageInfo.last_verification),
      review: workPackageEvidenceSignature(packageInfo.last_review),
      attempts: workPackageEvidenceSignature(packageInfo.last_agent_attempts),
    },
  };
}

function workPackageRelationRenderState(packageInfo) {
  return {
    id: packageInfo.id,
    title: packageInfo.title,
    stage: packageInfo.stage,
    status: packageInfo.status,
    dependencies: packageInfo.dependencies || [],
    parent_id: packageInfo.parent_id || "",
    shard_ids: packageInfo.shard_ids || [],
    operator_paused: Boolean(packageInfo.operator_paused),
    parallel_safe: Boolean(packageInfo.parallel_safe),
  };
}

/**
 * Build the bounded presentation key used to decide whether an open inspector
 * needs a rich rerender. The projection intentionally excludes unrelated
 * dashboard state so idle polling does not churn the inspector DOM.
 */
export function workPackageDetailSnapshotKey(snapshot, packageId) {
  const packages = snapshot?.packages || [];
  const packageInfo = packages.find((item) => item.id === packageId) || null;
  if (!packageInfo) return "";
  const agents = (snapshot?.agents || []).map((agent) => ({
    id: agent.id,
    enabled: Boolean(agent.enabled),
    capabilities: agent.capabilities || [],
    runtime_id: agent.runtime_id || "",
    model_route_id: agent.model_route_id || "",
    target_id: agent.target_id || "",
    model: agent.model || "",
    support: agent.support || null,
    health: agent.health?.status || "unknown",
    available: Boolean(agent.health?.available),
  }));
  const lanes = (snapshot?.execution_lanes || []).map((lane) => ({
    id: lane.id,
    display_name: lane.display_name || "",
    runtime_id: lane.runtime_id || "",
    runtime_kind: lane.runtime_kind || "",
    model_route_id: lane.model_route_id || "",
    model_display_name: lane.model_display_name || "",
    target_id: lane.target_id || "",
    target_display_name: lane.target_display_name || "",
    roles: lane.roles || [],
    profile_ids: lane.profile_ids || [],
    health: lane.health || "unknown",
    availability: lane.availability || "unknown",
    diagnostics_summary: lane.diagnostics_summary || "",
    active_assignments: lane.active_assignments || [],
  }));
  const assignments = (snapshot?.assignments || [])
    .filter((assignment) => assignment.package_id === packageId)
    .map((assignment) => ({
      package_id: assignment.package_id,
      stage: assignment.stage || "",
      status: assignment.status || "",
      agent_id: assignment.agent_id || "",
      model_role: assignment.model_role || "",
      started_at: assignment.started_at || "",
    }));
  const skills = (snapshot?.skills || []).map((skill) => ({
    id: skill.id,
    description: skill.description || "",
    roles: skill.roles || [],
  }));
  return JSON.stringify({
    packageInfo: workPackageRenderState(packageInfo),
    relations: packages.map(workPackageRelationRenderState),
    executionRoles: snapshot?.execution_roles || [],
    executionLanes: lanes,
    assignments,
    agents,
    skills,
    runActive: Boolean(
      snapshot?.run?.owned_running || snapshot?.run?.external_running,
    ),
  });
}

/**
 * Stable Work Package side-inspector lifecycle.
 *
 * The controller deliberately owns only presentation state: visibility, active
 * tab, content scroll, and predictable focus transitions. WorkPackage selection
 * and workflow navigation remain owned by WorkbenchNavigation.
 */
const INSPECTOR_TABS = new Set(["overview", "execution", "evidence"]);

function normalizedTab(value) {
  return INSPECTOR_TABS.has(value) ? value : "overview";
}

export class WorkPackageInspector {
  constructor({
    root,
    title,
    meta,
    content,
    tablist,
    closeButton,
    focusReturnTarget,
    onClose = null,
  }) {
    if (!root || !title || !content || !tablist || !closeButton)
      throw new Error("WorkPackageInspector requires its complete shell");
    this.root = root;
    this.title = title;
    this.meta = meta || null;
    this.content = content;
    this.tablist = tablist;
    this.closeButton = closeButton;
    this.focusReturnTarget = focusReturnTarget || (() => null);
    this.onClose = onClose || (() => {});
    this.packageId = "";
    this.activeTab = "overview";
    this.scrollByTab = new Map();

    this.tablist.addEventListener("click", (event) => {
      const button = event.target.closest("[data-work-package-inspector-tab]");
      if (!button || !this.tablist.contains(button)) return;
      this.setTab(button.dataset.workPackageInspectorTab, { focus: true });
    });
    this.tablist.addEventListener("keydown", (event) => this.#handleTabKeydown(event));
    this.closeButton.addEventListener("click", () => this.close());
    this.root.addEventListener("keydown", (event) => {
      if (event.key !== "Escape" || this.root.hidden) return;
      event.preventDefault();
      this.close();
    });
  }

  get isOpen() {
    return !this.root.hidden;
  }

  isOpenFor(packageId) {
    return this.isOpen && this.packageId === String(packageId || "");
  }

  open({ packageId, title = "WorkPackage details", meta = "", tab = "overview" }) {
    const nextPackageId = String(packageId || "");
    const packageChanged = nextPackageId !== this.packageId;
    if (packageChanged) this.scrollByTab.clear();
    this.packageId = nextPackageId;
    this.title.textContent = title;
    if (this.meta) this.meta.textContent = meta;
    this.root.hidden = false;
    this.root.setAttribute("aria-hidden", "false");
    this.setTab(tab, { focus: false, restoreScroll: !packageChanged });
    if (packageChanged) this.content.scrollTop = 0;
    requestAnimationFrame(() => this.title.focus({ preventScroll: true }));
  }

  close({ restoreFocus = true } = {}) {
    if (!this.isOpen) return;
    this.#rememberScroll();
    const pagePosition = { x: window.scrollX, y: window.scrollY };
    this.root.hidden = true;
    this.root.setAttribute("aria-hidden", "true");
    this.onClose({ packageId: this.packageId, tab: this.activeTab });
    if (!restoreFocus) return;
    requestAnimationFrame(() => {
      const target = this.focusReturnTarget();
      target?.focus?.({ preventScroll: true });
      window.scrollTo({
        left: pagePosition.x,
        top: pagePosition.y,
        behavior: "auto",
      });
    });
  }

  setHeading(title, meta = "") {
    this.title.textContent = title;
    if (this.meta) this.meta.textContent = meta;
  }

  setTab(value, { focus = false, restoreScroll = true } = {}) {
    const tab = normalizedTab(value);
    if (tab !== this.activeTab) this.#rememberScroll();
    this.activeTab = tab;
    this.root.dataset.activeTab = tab;
    this.#syncTabState();
    if (restoreScroll) this.#restoreScroll();
    if (focus) {
      const button = this.tablist.querySelector(
        `[data-work-package-inspector-tab="${tab}"]`,
      );
      button?.focus?.({ preventScroll: true });
    }
  }

  captureRenderState() {
    this.#rememberScroll();
    return {
      packageId: this.packageId,
      tab: this.activeTab,
      scrollTop: this.content.scrollTop,
    };
  }

  restoreRenderState(snapshot) {
    if (!snapshot || snapshot.packageId !== this.packageId) {
      this.#syncTabState();
      return;
    }
    this.activeTab = normalizedTab(snapshot.tab);
    this.scrollByTab.set(this.activeTab, Math.max(0, Number(snapshot.scrollTop) || 0));
    this.root.dataset.activeTab = this.activeTab;
    this.#syncTabState();
    requestAnimationFrame(() => this.#restoreScroll());
  }

  syncRenderedPanels() {
    this.#syncTabState();
  }

  #rememberScroll() {
    if (!this.isOpen) return;
    this.scrollByTab.set(this.activeTab, Math.max(0, this.content.scrollTop || 0));
  }

  #restoreScroll() {
    const top = this.scrollByTab.get(this.activeTab) || 0;
    this.content.scrollTop = top;
  }

  #syncTabState() {
    this.tablist.querySelectorAll("[data-work-package-inspector-tab]").forEach((button) => {
      const selected = button.dataset.workPackageInspectorTab === this.activeTab;
      button.setAttribute("aria-selected", String(selected));
      button.setAttribute("tabindex", selected ? "0" : "-1");
    });
    this.content.querySelectorAll("[data-inspector-panel]").forEach((panel) => {
      const selected = panel.dataset.inspectorPanel === this.activeTab;
      panel.hidden = !selected;
      panel.setAttribute("aria-hidden", String(!selected));
    });
  }

  #handleTabKeydown(event) {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    const buttons = [...this.tablist.querySelectorAll("[data-work-package-inspector-tab]")];
    if (!buttons.length) return;
    const current = Math.max(0, buttons.indexOf(document.activeElement));
    let next = current;
    if (event.key === "ArrowLeft") next = (current - 1 + buttons.length) % buttons.length;
    if (event.key === "ArrowRight") next = (current + 1) % buttons.length;
    if (event.key === "Home") next = 0;
    if (event.key === "End") next = buttons.length - 1;
    event.preventDefault();
    this.setTab(buttons[next].dataset.workPackageInspectorTab, { focus: true });
  }
}
