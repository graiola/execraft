import { escapeHtml as esc } from "./ui-utils.js";

const HEALTHY_PROFILE_STATUSES = new Set(["available", "probe_due"]);
const USABLE_LANE_STATES = new Set(["ready", "degraded", "unknown"]);
const ATTENTION_LANE_STATES = new Set(["degraded", "unavailable"]);

function text(value) {
  return String(value ?? "").trim();
}

function laneLabel(lane) {
  return text(lane?.display_name) || text(lane?.model_display_name) || text(lane?.id) || "Execution lane";
}

function laneRuntimeLabel(lane) {
  const runtime = text(lane?.runtime_kind) || text(lane?.runtime_id) || "runtime";
  const target = text(lane?.target_display_name) || text(lane?.target_id);
  return [runtime === "openclaw" ? "OpenClaw" : runtime, target].filter(Boolean).join(" · ");
}

function laneForAgent(lanes, agentId) {
  return lanes.find((lane) => (lane.profile_ids || []).includes(agentId)) || null;
}

function issue(kind, title, detail, severity = "warn") {
  return { kind, title, detail, severity };
}

export function summarizeExecutionHealth(snapshot = {}) {
  const agents = Array.isArray(snapshot.agents) ? snapshot.agents : [];
  const nodes = Array.isArray(snapshot.nodes) ? snapshot.nodes : [];
  const lanes = Array.isArray(snapshot.execution_lanes) ? snapshot.execution_lanes : [];
  const assignments = Array.isArray(snapshot.assignments) ? snapshot.assignments : [];
  const errors = Array.isArray(snapshot.config_errors) ? snapshot.config_errors : [];
  const enabledAgents = agents.filter((agent) => agent.enabled !== false);
  const assignedAgentIds = new Set(assignments.map((item) => text(item.agent_id)).filter(Boolean));
  const satellites = nodes.filter((node) => node.id !== "local");
  const reachableSatellites = satellites.filter((node) => node.reachable === true);
  const issues = [];

  errors.forEach((message) => {
    issues.push(issue("config", "Configuration error", text(message), "bad"));
  });

  satellites.forEach((node) => {
    const nodeName = text(node.name) || text(node.id) || "Execution node";
    if (node.reachable === false) {
      issues.push(
        issue(
          "node",
          `${nodeName} offline`,
          text(node.url) || "The configured execution node is not reachable.",
          "bad",
        ),
      );
      return;
    }
    const hasAssignedProfile = (node.agents || []).some((agentId) =>
      assignedAgentIds.has(text(agentId)),
    );
    if (
      node.reachable === true &&
      node.loaded_models_supported === true &&
      hasAssignedProfile &&
      !(node.loaded_models || []).length
    ) {
      issues.push(
        issue(
          "node-runtime",
          `${nodeName} has no loaded model`,
          "The node is reachable but reports no loaded model for its active assignment.",
          "warn",
        ),
      );
    } else if (node.reachable === true && text(node.loaded_models_error)) {
      issues.push(
        issue(
          "node-runtime",
          `${nodeName} runtime probe unavailable`,
          text(node.loaded_models_error),
          "warn",
        ),
      );
    }
  });

  lanes.forEach((lane) => {
    const availability = text(lane.availability) || "unknown";
    if (!ATTENTION_LANE_STATES.has(availability)) return;
    issues.push(
      issue(
        "lane",
        `${laneLabel(lane)} ${availability}`,
        text(lane.diagnostics_summary) || laneRuntimeLabel(lane),
        availability === "unavailable" ? "bad" : "warn",
      ),
    );
  });

  // Profiles without a lane still need an operator-visible health signal. This
  // mainly protects compatibility/legacy configurations during schema migration.
  enabledAgents.forEach((agent) => {
    if (laneForAgent(lanes, agent.id)) return;
    const status = text(agent.health?.status) || "unknown";
    if (HEALTHY_PROFILE_STATUSES.has(status) || status === "unknown") return;
    issues.push(
      issue(
        "profile",
        `${text(agent.id) || "Agent profile"} ${status}`,
        text(agent.health?.reason) || "Profile health requires attention.",
        ["failed", "blocked", "unhealthy"].includes(status) ? "bad" : "warn",
      ),
    );
  });

  const active = assignments.map((assignment) => {
    const agentId = text(assignment.agent_id);
    const lane = laneForAgent(lanes, agentId);
    return {
      package_id: text(assignment.package_id),
      package_title: text(assignment.package_title),
      stage: text(assignment.stage),
      agent_id: agentId,
      parallel: Boolean(assignment.parallel),
      lane_id: lane?.id || "",
      lane_label: lane ? laneLabel(lane) : agentId || "Unassigned",
      lane_detail: lane ? laneRuntimeLabel(lane) : "",
    };
  });

  const availableLanes = lanes.filter((lane) => USABLE_LANE_STATES.has(text(lane.availability) || "unknown"));
  const severe = issues.some((item) => item.severity === "bad");
  const summary = issues.length
    ? `${issues.length} ${issues.length === 1 ? "execution issue" : "execution issues"} · ${availableLanes.length}/${lanes.length || 0} lanes usable`
    : `Execution healthy · ${lanes.length} ${lanes.length === 1 ? "lane" : "lanes"} · ${active.length} active${satellites.length ? ` · ${reachableSatellites.length}/${satellites.length} satellites` : ""}`;

  return {
    active,
    issues,
    availableLanes,
    lanes,
    agents,
    nodes,
    enabledAgents,
    satellites,
    reachableSatellites,
    severe,
    summary,
  };
}

export class ExecutionHealthView {
  constructor({ root, drawer, openButton, closeButton, overview, status, summary, onVisibilityChange = () => {} }) {
    this.root = root;
    this.drawer = drawer;
    this.openButton = openButton;
    this.closeButton = closeButton;
    this.overview = overview;
    this.status = status;
    this.summary = summary;
    this.onVisibilityChange = onVisibilityChange;
    this.lastTrigger = null;
    this.latest = summarizeExecutionHealth({});
    this.overviewKey = "";
    this.#bind();
  }

  isOpen() {
    return !this.drawer.hidden;
  }

  render(snapshot) {
    this.latest = summarizeExecutionHealth(snapshot);
    const { issues, severe, summary } = this.latest;
    this.root.classList.toggle("attention", issues.length > 0);
    this.status.textContent = issues.length ? `${issues.length} ${issues.length === 1 ? "issue" : "issues"}` : "Healthy";
    this.status.className = `pill ${issues.length ? (severe ? "bad" : "warn") : "ok"}`;
    this.summary.textContent = summary;
    if (this.isOpen()) this.#renderOverviewIfChanged();
    return this.latest;
  }

  open({ focus = true } = {}) {
    if (this.isOpen()) return;
    this.lastTrigger = document.activeElement instanceof HTMLElement ? document.activeElement : this.openButton;
    this.drawer.hidden = false;
    this.drawer.classList.add("open");
    this.openButton.setAttribute("aria-expanded", "true");
    this.#renderOverviewIfChanged();
    this.onVisibilityChange(true);
    if (focus) queueMicrotask(() => this.closeButton.focus({ preventScroll: true }));
  }

  close({ restoreFocus = true } = {}) {
    if (!this.isOpen()) return;
    this.drawer.classList.remove("open");
    this.drawer.hidden = true;
    this.openButton.setAttribute("aria-expanded", "false");
    this.onVisibilityChange(false);
    if (restoreFocus) {
      const target = this.lastTrigger?.isConnected ? this.lastTrigger : this.openButton;
      queueMicrotask(() => target?.focus({ preventScroll: true }));
    }
  }

  #renderOverviewIfChanged() {
    const { active, issues, availableLanes } = this.latest;
    const key = JSON.stringify({
      active,
      issues,
      lanes: availableLanes.map((lane) => ({
        id: lane.id || "",
        display_name: lane.display_name || "",
        runtime_kind: lane.runtime_kind || "",
        runtime_id: lane.runtime_id || "",
        target_display_name: lane.target_display_name || "",
        target_id: lane.target_id || "",
        roles: lane.roles || [],
        health: lane.health || "",
        availability: lane.availability || "",
      })),
    });
    if (key === this.overviewKey) return;
    this.overviewKey = key;
    this.#renderOverview();
  }

  #renderOverview() {
    const { active, issues, availableLanes } = this.latest;
    const activeHtml = active.length
      ? active
          .map(
            (item) => `<article class="execution-health-item active"><div><strong>${esc(item.package_id || "Active work")}${item.package_title ? ` · ${esc(item.package_title)}` : ""}</strong><small>${esc(item.stage || "running")} · ${esc(item.lane_label)}${item.parallel ? " · parallel" : ""}</small></div>${item.agent_id ? `<button type="button" class="btn small open-agent-console" data-agent="${esc(item.agent_id)}" data-package="${esc(item.package_id)}" data-stage="${esc(item.stage)}">Open agent</button>` : ""}</article>`,
          )
          .join("")
      : '<div class="execution-health-empty">No active assignments.</div>';
    const attentionHtml = issues.length
      ? issues
          .map(
            (item) => `<article class="execution-health-item attention"><span class="pill ${esc(item.severity)}">${item.severity === "bad" ? "ACTION" : "CHECK"}</span><div><strong>${esc(item.title)}</strong><small>${esc(item.detail)}</small></div></article>`,
          )
          .join("")
      : '<div class="execution-health-empty">No execution issues require attention.</div>';
    const lanesHtml = availableLanes.length
      ? availableLanes
          .map((lane) => {
            const roles = (lane.roles || []).map((role) => text(role).replaceAll("_", " ")).filter(Boolean).join(" · ");
            const availability = text(lane.availability) || "unknown";
            const badge = availability === "ready" ? "ok" : availability === "degraded" ? "warn" : "";
            return `<article class="execution-health-item lane"><div><strong>${esc(laneLabel(lane))}</strong><small>${esc(laneRuntimeLabel(lane))}${roles ? ` · ${esc(roles)}` : ""}</small></div><span class="pill ${badge}">${esc(availability)}</span></article>`;
          })
          .join("")
      : '<div class="execution-health-empty">No execution lanes are currently usable.</div>';

    this.overview.innerHTML = `
      <section class="execution-health-group active" aria-labelledby="executionHealthActiveTitle"><h3 id="executionHealthActiveTitle">Active</h3>${activeHtml}</section>
      <section class="execution-health-group attention" aria-labelledby="executionHealthAttentionTitle"><h3 id="executionHealthAttentionTitle">Attention required</h3>${attentionHtml}</section>
      <section class="execution-health-group available" aria-labelledby="executionHealthAvailableTitle"><h3 id="executionHealthAvailableTitle">Available lanes</h3>${lanesHtml}</section>`;
  }

  #bind() {
    this.openButton.addEventListener("click", () => this.open());
    this.closeButton.addEventListener("click", () => this.close());
    this.drawer.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      this.close();
    });
  }
}
