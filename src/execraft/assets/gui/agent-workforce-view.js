import { escapeHtml as esc, formatLocalTime as localTime } from "./ui-utils.js";

const HEALTHY_STATUSES = new Set(["available", "probe_due"]);
const ATTENTION_STATUSES = new Set(["cooldown", "blocked", "failed", "unhealthy", "degraded", "unavailable"]);

function text(value) {
  return String(value ?? "").trim();
}

function humanize(value, fallback = "—") {
  const valueText = text(value);
  return valueText ? valueText.replaceAll("_", " ") : fallback;
}

function laneForAgent(lanes, agentId) {
  return lanes.find((lane) => (lane.profile_ids || []).includes(agentId)) || null;
}

function laneLabel(lane) {
  return text(lane?.display_name) || text(lane?.id) || "Automatic lane";
}

function routeLabel(agent, lane) {
  if (lane) {
    return [
      lane.runtime_kind === "openclaw" ? "OpenClaw" : humanize(lane.runtime_kind, "Native"),
      text(lane.model_display_name) || text(agent.model) || text(agent.model_route_id),
      text(lane.target_display_name) || text(lane.target_id) || text(agent.target_id) || "local/direct",
    ]
      .filter(Boolean)
      .join(" · ");
  }
  return [
    agent.runtime_kind === "openclaw" ? "OpenClaw" : humanize(agent.runtime_kind || agent.adapter, "Native"),
    text(agent.model) || text(agent.model_route_id),
    text(agent.target_id) || "local/direct",
  ]
    .filter(Boolean)
    .join(" · ");
}

function currentAssignment(agent, assignments) {
  const embedded = Array.isArray(agent.assignments) ? agent.assignments : [];
  return (
    embedded[0] ||
    assignments.find((assignment) => text(assignment.agent_id) === text(agent.id)) ||
    null
  );
}

function workerAttention(agent) {
  const status = text(agent.health?.status) || "unknown";
  const actionStatus = text(agent.action?.status);
  return (
    ATTENTION_STATUSES.has(status) ||
    (status === "unknown" && agent.enabled !== false) ||
    actionStatus === "failed" ||
    Boolean(text(agent.action?.error))
  );
}

function promotable(agent) {
  const activePromotions = (agent.promotions || []).filter((item) => item.active !== false);
  return Boolean(
    agent.native_maintenance &&
      agent.enabled !== false &&
      (activePromotions.length ||
        (agent.capabilities || []).some(
          (capability) => Number(agent.max_complexity?.[capability] ?? 100) < 100,
        )),
  );
}

function workerView(agent, { assignments, lanes, packages, runActive }) {
  const assignment = currentAssignment(agent, assignments);
  const lane = laneForAgent(lanes, agent.id);
  const packageInfo = assignment
    ? packages.find((item) => text(item.id) === text(assignment.package_id)) || null
    : null;
  const healthStatus = text(agent.health?.status) || "unknown";
  const actionBusy = text(agent.action?.status) === "running";
  const assigned = Boolean(assignment);
  const healthActionsDisabled =
    !agent.native_maintenance || runActive || assigned || actionBusy || agent.enabled === false;
  const resetUseful =
    agent.native_maintenance &&
    (!HEALTHY_STATUSES.has(healthStatus) || Number(agent.health?.failures || 0) > 0);
  const attention = workerAttention(agent);
  const category = agent.enabled === false
    ? "disabled"
    : attention
      ? "attention"
      : assigned
        ? "working"
        : "available";
  return {
    id: text(agent.id),
    name: text(agent.name) || text(agent.id) || "Agent",
    category,
    enabled: agent.enabled !== false,
    assignment,
    package_title: text(assignment?.package_title) || text(packageInfo?.title),
    stage: text(assignment?.stage) || text(packageInfo?.stage),
    health_status: healthStatus,
    health_reason: text(agent.health?.reason) || text(agent.action?.error),
    health_failures: Number(agent.health?.failures || 0),
    lane_id: text(lane?.id),
    lane_label: laneLabel(lane),
    route_label: routeLabel(agent, lane),
    capabilities: Array.isArray(agent.capabilities) ? agent.capabilities.map(text).filter(Boolean) : [],
    native_maintenance: Boolean(agent.native_maintenance),
    can_doctor: !healthActionsDisabled,
    can_reset: !healthActionsDisabled && resetUseful,
    can_promote: promotable(agent),
    promotion_active: (agent.promotions || []).some((item) => item.active !== false),
    action_status: text(agent.action?.status),
    action_kind: text(agent.action?.action),
    action_finished_at: text(agent.action?.finished_at),
    runtime_kind: text(agent.runtime_kind),
    model: text(agent.model),
    target_id: text(agent.target_id),
    profile_id: text(agent.id),
  };
}

/** Build a task-centric workforce projection over AgentProfile + assignments. */
export function summarizeAgentWorkforce(snapshot = {}) {
  const agents = Array.isArray(snapshot.agents) ? snapshot.agents : [];
  const assignments = Array.isArray(snapshot.assignments) ? snapshot.assignments : [];
  const lanes = Array.isArray(snapshot.execution_lanes) ? snapshot.execution_lanes : [];
  const packages = Array.isArray(snapshot.packages) ? snapshot.packages : [];
  const runActive = Boolean(snapshot.run?.owned_running || snapshot.run?.external_running);
  const workers = agents.map((agent) => workerView(agent, { assignments, lanes, packages, runActive }));
  const groups = {
    working: workers.filter((worker) => worker.category === "working"),
    attention: workers.filter((worker) => worker.category === "attention"),
    available: workers.filter((worker) => worker.category === "available"),
    disabled: workers.filter((worker) => worker.category === "disabled"),
  };
  return {
    workers,
    groups,
    counts: {
      total: workers.length,
      working: groups.working.length,
      attention: groups.attention.length,
      available: groups.available.length,
      disabled: groups.disabled.length,
    },
  };
}

function healthClass(worker) {
  if (worker.category === "attention") return "warn";
  if (worker.category === "disabled") return "";
  return HEALTHY_STATUSES.has(worker.health_status) ? "ok" : "warn";
}

function assignmentMarkup(worker) {
  if (!worker.assignment) {
    return `<div class="agent-worker-assignment idle"><strong>Available</strong><span>No active Work Package assignment.</span></div>`;
  }
  const packageId = text(worker.assignment.package_id) || "Current work";
  const title = worker.package_title ? ` · ${esc(worker.package_title)}` : "";
  return `<div class="agent-worker-assignment"><strong>${esc(packageId)}${title}</strong><span>${esc(humanize(worker.stage, "running"))}${worker.assignment.parallel ? " · parallel" : ""}</span></div>`;
}

function workerActionsMarkup(worker) {
  const nativeActions = worker.native_maintenance
    ? `<button type="button" class="btn small" data-worker-action="doctor" data-agent="${esc(worker.id)}" ${worker.can_doctor ? "" : "disabled"}>Doctor</button>
       <button type="button" class="btn small${worker.promotion_active ? " primary" : ""}" data-worker-action="promote" data-agent="${esc(worker.id)}" ${worker.can_promote ? "" : "disabled"}>${worker.promotion_active ? "Promotion…" : "Promote"}</button>
       <button type="button" class="btn small danger" data-worker-action="reset" data-agent="${esc(worker.id)}" ${worker.can_reset ? "" : "disabled"}>Reset health</button>`
    : "";
  return `<div class="agent-worker-actions">
    <button type="button" class="btn small primary" data-worker-action="open" data-agent="${esc(worker.id)}">Open</button>
    ${nativeActions}
    <button type="button" class="btn small" data-worker-action="diagnostics" data-agent="${esc(worker.id)}">Diagnostics</button>
  </div>`;
}

function workerMarkup(worker) {
  const health = worker.enabled ? humanize(worker.health_status, "unknown") : "disabled";
  const attention = worker.health_reason
    ? `<div class="agent-worker-attention">${esc(worker.health_reason)}</div>`
    : worker.health_failures
      ? `<div class="agent-worker-attention">${esc(worker.health_failures)} consecutive failures</div>`
      : "";
  const capabilities = worker.capabilities.length
    ? worker.capabilities.map((item) => `<span>${esc(humanize(item))}</span>`).join("")
    : '<span>general</span>';
  const action = worker.action_status
    ? `<small class="agent-worker-action-state">${esc(humanize(worker.action_kind, "maintenance"))}: ${esc(humanize(worker.action_status))}${worker.action_finished_at ? ` · ${esc(localTime(worker.action_finished_at))}` : ""}</small>`
    : "";
  return `<article class="agent-worker-card ${esc(worker.category)}" data-agent-worker="${esc(worker.id)}">
    <header class="agent-worker-card-head">
      <div><span class="agent-worker-presence" aria-hidden="true"></span><strong>${esc(worker.name)}</strong><small>${esc(worker.profile_id)}</small></div>
      <span class="pill ${healthClass(worker)}">${esc(health)}</span>
    </header>
    ${assignmentMarkup(worker)}
    <div class="agent-worker-route"><strong>${esc(worker.lane_label)}</strong><span>${esc(worker.route_label)}</span></div>
    ${attention}${action}
    <div class="agent-worker-capabilities">${capabilities}</div>
    ${workerActionsMarkup(worker)}
    <details class="agent-worker-technical"><summary>Technical details</summary><dl><dt>Profile</dt><dd>${esc(worker.profile_id)}</dd><dt>Lane</dt><dd>${esc(worker.lane_id || "automatic")}</dd><dt>Runtime</dt><dd>${esc(worker.runtime_kind || "—")}</dd><dt>Model</dt><dd>${esc(worker.model || "—")}</dd><dt>Target</dt><dd>${esc(worker.target_id || "local/direct")}</dd></dl></details>
  </article>`;
}

function groupMarkup(id, title, subtitle, workers, emptyText) {
  const body = workers.length
    ? workers.map(workerMarkup).join("")
    : `<div class="agent-workforce-empty">${esc(emptyText)}</div>`;
  return `<section class="agent-workforce-group ${id}" aria-labelledby="agentWorkforce${id}Title">
    <header><div><h3 id="agentWorkforce${id}Title">${esc(title)}</h3><p>${esc(subtitle)}</p></div><span class="pill">${workers.length}</span></header>
    <div class="agent-worker-grid">${body}</div>
  </section>`;
}

/** Dedicated task workforce surface. It never mutates scheduler identity. */
export class AgentWorkforceView {
  constructor({ root, summary, groups, onOpenAgent, onDoctor, onReset, onPromote, onDiagnostics }) {
    this.root = root;
    this.summary = summary;
    this.groups = groups;
    this.onOpenAgent = onOpenAgent;
    this.onDoctor = onDoctor;
    this.onReset = onReset;
    this.onPromote = onPromote;
    this.onDiagnostics = onDiagnostics;
    this.snapshot = {};
    this.renderKey = "";
    this.groups.addEventListener("click", (event) => this.#handleAction(event));
  }

  render(snapshot) {
    this.snapshot = snapshot || {};
    const workforce = summarizeAgentWorkforce(this.snapshot);
    const key = JSON.stringify(workforce);
    if (key === this.renderKey) return workforce;
    this.renderKey = key;
    const { counts, groups } = workforce;
    this.summary.innerHTML = `
      <div><strong>${counts.total}</strong><span>Total</span></div>
      <div><strong>${counts.working}</strong><span>Working</span></div>
      <div class="${counts.attention ? "attention" : ""}"><strong>${counts.attention}</strong><span>Needs attention</span></div>
      <div><strong>${counts.available}</strong><span>Available</span></div>`;
    this.groups.innerHTML = [
      groupMarkup("working", "Working", "Agents currently assigned to Work Package work.", groups.working, "No agents are working right now."),
      groupMarkup("attention", "Needs attention", "Workers with health or maintenance signals that deserve operator review.", groups.attention, "No agents currently require attention."),
      groupMarkup("available", "Available", "Enabled workers ready for scheduler assignment.", groups.available, "No idle agents are currently available."),
      groups.disabled.length
        ? groupMarkup("disabled", "Disabled", "Configured profiles intentionally excluded from scheduling.", groups.disabled, "")
        : "",
    ].join("");
    return workforce;
  }

  #worker(agentId) {
    return summarizeAgentWorkforce(this.snapshot).workers.find((item) => item.id === agentId) || null;
  }

  #handleAction(event) {
    const button = event.target.closest("button[data-worker-action][data-agent]");
    if (!button || !this.groups.contains(button) || button.disabled) return;
    const worker = this.#worker(button.dataset.agent);
    if (!worker) return;
    const action = button.dataset.workerAction;
    if (action === "open") {
      this.onOpenAgent(worker.id, {
        packageId: text(worker.assignment?.package_id),
        stage: worker.stage,
      });
    } else if (action === "doctor") {
      void this.onDoctor(worker.id);
    } else if (action === "reset") {
      void this.onReset(worker.id);
    } else if (action === "promote") {
      this.onPromote(worker.id);
    } else if (action === "diagnostics") {
      this.onDiagnostics(worker.id);
    }
  }
}
