import { elementById as $, escapeHtml as esc, formatLocalTime as localTime, truncateText as truncate } from "./ui-utils.js";
import { structuredEvidence } from "./work-package-presenter.js";

function executionTraceDuration(seconds) {
  const value = Math.max(0, Number(seconds || 0));
  if (value < 1) return value > 0 ? "<1s" : "0s";
  const rounded = Math.round(value);
  const hours = Math.floor(rounded / 3600);
  const minutes = Math.floor((rounded % 3600) / 60);
  const secs = rounded % 60;
  if (hours) return `${hours}h ${minutes}m`;
  if (minutes) return `${minutes}m ${secs}s`;
  return `${secs}s`;
}
function executionTraceStage(stage) {
  return String(stage || "unknown")
    .replaceAll("_", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}
function executionTraceOutcome(outcome) {
  return String(outcome || "completed").replaceAll("_", " ");
}
function executionTraceClass(value) {
  return String(value || "unknown")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
}
function executionTraceMetric(label, value, detail = "") {
  return `<div><span>${esc(label)}</span><strong>${esc(value)}</strong>${detail ? `<small>${esc(detail)}</small>` : ""}</div>`;
}
function executionTraceOutcomeIsProblem(node) {
  const value = executionTraceClass(node?.outcome || node?.status);
  return [
    "failed",
    "validation-failed",
    "invalid-output",
    "changes-requested",
    "timeout",
    "error",
  ].some((token) => value.includes(token));
}
function executionTraceFlowGroups(nodes) {
  const groups = [];
  for (const node of nodes || []) {
    const previous = groups[groups.length - 1];
    const sameStage = previous
      && previous.stage === node.stage
      && previous.kind === node.kind;
    if (sameStage) previous.nodes.push(node);
    else groups.push({ stage: node.stage, kind: node.kind, nodes: [node] });
  }
  return groups.map((group) => {
    const members = group.nodes;
    const representative = members.find((node) => node.live) || members[members.length - 1];
    const agents = [...new Set(members.map((node) => node.agent_id).filter(Boolean))];
    return {
      ...representative,
      incoming_reason: members[0].incoming_reason,
      duration_seconds: members.reduce((total, node) => total + Number(node.duration_seconds || 0), 0),
      flow_nodes: members,
      flow_node_ids: members.map((node) => node.id),
      flow_attempt_count: members.length,
      flow_failure_count: members.filter(executionTraceOutcomeIsProblem).length,
      flow_agents: agents,
    };
  });
}
function executionTraceTransitionMarkup(node, index, mode) {
  if (!index) return "";
  const reason = node.incoming_reason || "Next stage";
  const noteworthy = /change|fail|recover|supervisor|decompos|manual|pause|retry/i.test(reason);
  const label = mode === "attempts" || noteworthy
    ? `<em>${esc(truncate(reason, 34))}</em>`
    : "";
  return `<span class="execution-trace-connector${label ? " has-label" : ""}" title="${esc(reason)}" aria-hidden="true"><i></i>${label}</span>`;
}
function executionTraceNodeMarkup(node, index, mode) {
  const connector = executionTraceTransitionMarkup(node, index, mode);
  const targetId = node.id;
  const members = node.flow_node_ids || [targetId];
  const agents = node.flow_agents || [node.agent_id].filter(Boolean);
  const agent = agents.length > 1
    ? `${agents.length} agents`
    : agents[0] || (node.kind === "deterministic" ? "Deterministic check" : "Unassigned");
  const attemptCount = Number(node.flow_attempt_count || 1);
  const failureCount = Number(node.flow_failure_count || 0);
  const attempt = Number(node.attempt || 0) > 1 ? ` · attempt ${node.attempt}` : "";
  const statusClass = executionTraceClass(node.status || node.outcome);
  const outcomeClass = executionTraceClass(node.outcome);
  const rightMeta = mode === "flow" && attemptCount > 1
    ? ""
    : node.model
      ? truncate(node.model, 25)
      : "";
  const flags = mode === "flow" && (attemptCount > 1 || failureCount)
    ? `<span class="execution-trace-node-flags">${attemptCount > 1 ? `<i>${attemptCount} attempts</i>` : ""}${failureCount ? `<i class="warn">${failureCount} issue${failureCount === 1 ? "" : "s"}</i>` : ""}</span>`
    : "";
  return `${connector}<button type="button" class="execution-trace-node ${esc(node.kind || "stage")} status-${esc(statusClass)} outcome-${esc(outcomeClass)}${node.live ? " live" : ""}" data-trace-node="${esc(targetId)}" data-trace-members="${esc(members.join(" "))}" aria-label="Inspect ${esc(node.stage_label || executionTraceStage(node.stage))}">
    <span class="execution-trace-node-head"><b>${esc(node.stage_label || executionTraceStage(node.stage))}</b><i>${esc(executionTraceDuration(node.duration_seconds))}</i></span>
    <span class="execution-trace-agent">${esc(agent)}</span>
    ${flags}
    <span class="execution-trace-node-foot"><small>${esc(executionTraceOutcome(node.outcome))}${esc(attempt)}</small>${rightMeta ? `<small title="${esc(node.model || rightMeta)}">${esc(rightMeta)}</small>` : ""}</span>
  </button>`;
}
function executionTraceLaneMarkup(lane, mode) {
  const rawNodes = lane.nodes || [];
  const nodes = mode === "flow" ? executionTraceFlowGroups(rawNodes) : rawNodes;
  const identity = lane.parent_id
    ? `${lane.shard_key || "Shard"} · ${lane.package_id}`
    : lane.package_id;
  return `<article class="execution-trace-lane">
    <header><div><strong>${esc(identity)}</strong><span>${esc(truncate(lane.title || "", 88))}</span></div><span class="pill ${lane.status === "completed" ? "ok" : lane.status === "blocked" ? "warn" : ""}">${esc(lane.current_stage || lane.status || "pending")}</span></header>
    <div class="execution-trace-scroll"><div class="execution-trace-flow">${nodes.length ? nodes.map((node, index) => executionTraceNodeMarkup(node, index, mode)).join("") : '<div class="execution-trace-empty">No structured stage attempts recorded for this lane.</div>'}</div></div>
  </article>`;
}
function executionTraceAnnotations(trace) {
  return (trace?.lanes || []).flatMap((lane) =>
    (lane.annotations || []).map((annotation) => ({ ...annotation, package_id: lane.package_id })),
  );
}
function executionTraceDiagnosticGroups(trace) {
  const groups = new Map();
  for (const annotation of executionTraceAnnotations(trace)) {
    const key = annotation.event_type || annotation.label || "event";
    const current = groups.get(key) || {
      event_type: key,
      label: annotation.label || executionTraceStage(key),
      count: 0,
      latest: annotation,
      packages: new Set(),
    };
    current.count += 1;
    current.latest = annotation;
    if (annotation.package_id) current.packages.add(annotation.package_id);
    groups.set(key, current);
  }
  return [...groups.values()].sort((left, right) => right.count - left.count);
}
function executionTraceDiagnosticsMarkup(trace) {
  const annotations = executionTraceAnnotations(trace);
  if (!annotations.length) return "";
  const groups = executionTraceDiagnosticGroups(trace);
  const fallbackCount = annotations.filter((item) => item.event_type === "agent_failover").length;
  const retryCount = annotations.filter((item) => item.event_type === "agent_contract_retry").length;
  const waitCount = annotations.filter((item) => item.event_type === "agent_wait_scheduled").length;
  const summary = [
    fallbackCount ? `${fallbackCount} fallback${fallbackCount === 1 ? "" : "s"}` : "",
    retryCount ? retryCount === 1 ? "1 retry" : `${retryCount} retries` : "",
    waitCount ? `${waitCount} wait${waitCount === 1 ? "" : "s"}` : "",
  ].filter(Boolean).join(" · ") || `${annotations.length} events`;
  return `<details class="execution-trace-diagnostics">
    <summary><span>Diagnostics</span><small>${esc(summary)}</small></summary>
    <div class="execution-trace-diagnostic-groups">${groups.map((group) => `<article><span><b>${esc(group.label)}</b><i>${esc(group.count)}</i></span><p>${esc(group.latest.detail || "No additional detail")}</p><small>${esc(group.packages.size)} package${group.packages.size === 1 ? "" : "s"}${group.latest.timestamp ? ` · latest ${esc(localTime(group.latest.timestamp))}` : ""}</small></article>`).join("")}</div>
  </details>`;
}
function findExecutionTraceNode(trace, nodeId) {
  for (const lane of trace?.lanes || []) {
    const node = (lane.nodes || []).find((item) => item.id === nodeId);
    if (node) return node;
  }
  return null;
}
function defaultExecutionTraceNode(trace) {
  const nodes = (trace?.lanes || []).flatMap((lane) => lane.nodes || []);
  const live = nodes.find((node) => node.live);
  if (live) return live;
  for (let index = nodes.length - 1; index >= 0; index -= 1) {
    if (nodes[index].status === "failed") return nodes[index];
  }
  return nodes[nodes.length - 1] || null;
}
function executionTraceNodeDetail(node) {
  if (!node)
    return '<div class="execution-trace-empty">Select a stage to inspect its execution metadata.</div>';
  const metrics = Object.entries(node.metrics || {});
  const skills = (node.skills || [])
    .map((item) => item.skill_id || item.id || item.name || "")
    .filter(Boolean);
  const timestamps = [
    node.started_at ? `Started ${localTime(node.started_at)}` : "",
    node.completed_at && !node.live ? `Ended ${localTime(node.completed_at)}` : "",
  ].filter(Boolean).join(" · ");
  const attempts = node.flow_nodes || [node];
  const visibleAttempts = attempts.length > 5
    ? [attempts[0], null, ...attempts.slice(-3)]
    : attempts;
  const statusClass = node.status === "completed" ? "ok" : node.status === "failed" ? "bad" : node.live ? "warn" : "";
  const openAgent = node.agent_id
    ? `<button type="button" class="btn small" data-trace-open-agent="${esc(node.agent_id)}" data-package="${esc(node.package_id || "")}" data-stage="${esc(node.stage || "")}">Open output</button>`
    : "";
  return `<div class="execution-trace-detail-head"><div><span class="eyebrow">Selected stage</span><h5>${esc(node.stage_label || executionTraceStage(node.stage))}</h5><p>${esc(node.agent_id || (node.kind === "deterministic" ? "Deterministic orchestration check" : "Unassigned"))}${node.model ? ` · ${esc(node.model)}` : ""}</p></div><div class="execution-trace-detail-actions"><span class="pill ${statusClass}">${esc(executionTraceOutcome(node.outcome))}</span>${openAgent}</div></div>
    ${attempts.length > 1 ? `<div class="execution-trace-attempt-list">${visibleAttempts.map((attempt, index) => attempt ? `<div><span>Attempt ${esc(attempt.attempt || index + 1)}</span><strong>${esc(attempt.agent_id || (attempt.kind === "deterministic" ? "Deterministic" : "Unassigned"))}</strong><i>${esc(executionTraceOutcome(attempt.outcome))} · ${esc(executionTraceDuration(attempt.duration_seconds))}</i></div>` : `<div class="execution-trace-attempt-omitted">${esc(attempts.length - 4)} earlier attempts hidden</div>`).join("")}</div>` : ""}
    <details class="execution-trace-detail-more"><summary>Metadata and evidence</summary>
      <dl class="execution-trace-kv">
        <div><dt>Package</dt><dd>${esc(node.package_id || "—")}</dd></div>
        <div><dt>Duration</dt><dd>${esc(executionTraceDuration(node.duration_seconds))}</dd></div>
        <div><dt>Attempts</dt><dd>${esc(node.flow_attempt_count || node.attempt || 1)}</dd></div>
        <div><dt>Capability</dt><dd>${esc(node.capability || node.kind || "—")}</dd></div>
        <div><dt>Transition</dt><dd>${esc(node.transition_to ? `${node.transition_reason || "Completed"} → ${executionTraceStage(node.transition_to)}` : node.live ? "Still running" : "No later transition recorded")}</dd></div>
        <div><dt>Invocation</dt><dd title="${esc(node.invocation_id || "")}">${esc(node.invocation_id ? truncate(node.invocation_id, 28) : "deterministic")}</dd></div>
      </dl>
      ${timestamps ? `<p class="execution-trace-timestamps">${esc(timestamps)}</p>` : ""}
      ${metrics.length ? `<div class="execution-trace-metrics">${metrics.map(([key, value]) => `<span><b>${esc(value)}</b>${esc(key)}</span>`).join("")}</div>` : ""}
      ${skills.length ? `<p class="execution-trace-skills"><strong>Skills:</strong> ${esc(skills.join(", "))}</p>` : ""}
      ${structuredEvidence("Failure details", node.failure)}
      ${structuredEvidence("Validation errors", node.validation_errors)}
      ${structuredEvidence("Artifact reference", node.artifact)}
    </details>`;
}
function traceButtonContains(button, nodeId) {
  return String(button?.dataset?.traceMembers || button?.dataset?.traceNode || "")
    .split(" ")
    .filter(Boolean)
    .includes(nodeId);
}

/**
 * Own execution-trace cache, selection, presentation mode and refresh lifecycle.
 * Keeping this state outside app.js prevents dashboard polling from coupling the
 * global workbench coordinator to evidence rendering details.
 */
export class ExecutionTraceView {
  constructor({ api, agentConsole, selectedPackageId }) {
    this.api = api;
    this.agentConsole = agentConsole;
    this.selectedPackageId = selectedPackageId;
    this.cache = new Map();
    this.inflight = new Map();
    this.selectedNodes = new Map();
    this.modes = new Map();
  }

  reset() {
    this.cache.clear();
    this.inflight.clear();
    this.selectedNodes.clear();
    this.modes.clear();
  }

  render(packageId, trace) {
    if (this.selectedPackageId() !== packageId) return;
    const host = $("executionTraceBody");
    if (!host) return;
    const summary = trace.summary || {};
    const agents = summary.agents_involved || [];
    const allNodes = (trace.lanes || []).flatMap((lane) => lane.nodes || []);
    const mode = this.modes.get(packageId) || "flow";
    const requested = this.selectedNodes.get(packageId);
    const selectedExact = findExecutionTraceNode(trace, requested) || defaultExecutionTraceNode(trace);
    if (selectedExact) this.selectedNodes.set(packageId, selectedExact.id);
    const displayNodes = (trace.lanes || []).flatMap((lane) =>
      mode === "flow" ? executionTraceFlowGroups(lane.nodes || []) : lane.nodes || [],
    );
    const selected = displayNodes.find((node) =>
      (node.flow_node_ids || [node.id]).includes(selectedExact?.id),
    ) || selectedExact;
    const annotations = executionTraceAnnotations(trace);
    const coverage = !trace.history_complete || (trace.warnings || []).length
      ? `<details class="execution-trace-history"><summary>History coverage</summary>${!trace.history_complete ? '<p>Earlier runs predate structured stage-transition history. Agent attempts are exact; some deterministic stages or transition durations may be missing.</p>' : ""}${(trace.warnings || []).map((warning) => `<p class="warn">${esc(warning)}</p>`).join("")}</details>`
      : "";
    host.innerHTML = `
      <div class="execution-trace-toolbar">
        <div class="execution-trace-mode" role="group" aria-label="Execution trace detail level">
          <button type="button" data-trace-mode="flow" aria-pressed="${mode === "flow"}">Flow</button>
          <button type="button" data-trace-mode="attempts" aria-pressed="${mode === "attempts"}">All attempts</button>
        </div>
        <span>${esc(summary.stage_count || allNodes.length)} attempts · ${esc(annotations.length)} diagnostic events</span>
      </div>
      <div class="execution-trace-summary">
        ${executionTraceMetric("Wall clock", executionTraceDuration(summary.wall_clock_seconds), `${summary.package_count || 1} package lane${summary.package_count === 1 ? "" : "s"}`)}
        ${executionTraceMetric("Agent time", executionTraceDuration(summary.agent_work_seconds), `${summary.attempt_count || 0} agent attempt${summary.attempt_count === 1 ? "" : "s"}`)}
        ${executionTraceMetric("Agents", agents.length || 0, agents.length ? truncate(agents.join(", "), 54) : "No agent attempt yet")}
        ${executionTraceMetric("Review loops", summary.review_loops || 0, `${summary.provider_fallbacks || 0} agent fallback${summary.provider_fallbacks === 1 ? "" : "s"} · ${executionTraceDuration(summary.waiting_seconds)} waiting`)}
      </div>
      ${coverage}
      <div class="execution-trace-lanes">${(trace.lanes || []).map((lane) => executionTraceLaneMarkup(lane, mode)).join("") || '<div class="execution-trace-empty">No execution history has been recorded for this Work Package.</div>'}</div>
      ${executionTraceDiagnosticsMarkup(trace)}
      <aside id="executionTraceNodeDetail" class="execution-trace-detail">${executionTraceNodeDetail(selected)}</aside>`;
    host.querySelectorAll("[data-trace-mode]").forEach((button) =>
      button.addEventListener("click", () => {
        const nextMode = button.dataset.traceMode === "attempts" ? "attempts" : "flow";
        if (nextMode === mode) return;
        this.modes.set(packageId, nextMode);
        this.render(packageId, trace);
      }),
    );
    host.querySelectorAll("[data-trace-node]").forEach((button) => {
      button.classList.toggle("selected", traceButtonContains(button, selectedExact?.id));
      button.addEventListener("click", () => {
        const node = displayNodes.find((item) => item.id === button.dataset.traceNode);
        if (!node) return;
        this.selectedNodes.set(packageId, node.id);
        host.querySelectorAll("[data-trace-node]").forEach((item) =>
          item.classList.toggle("selected", item === button),
        );
        const detail = $("executionTraceNodeDetail");
        if (detail) {
          detail.innerHTML = executionTraceNodeDetail(node);
          this.#wireDetail(detail);
        }
      });
    });
    this.#wireDetail($("executionTraceNodeDetail"));
  }

  async load(packageId, { force = false } = {}) {
    const cached = this.cache.get(packageId);
    if (cached) this.render(packageId, cached.trace);
    if (!force && cached && Date.now() - cached.loadedAt < 2500) return cached.trace;
    if (this.inflight.has(packageId)) return this.inflight.get(packageId);
    const request = this.api(`/api/package/execution-trace?package_id=${encodeURIComponent(packageId)}`)
      .then((trace) => {
        this.cache.set(packageId, { trace, loadedAt: Date.now() });
        this.render(packageId, trace);
        return trace;
      })
      .catch((error) => {
        const host = $("executionTraceBody");
        if (this.selectedPackageId() === packageId && host && !cached)
          host.innerHTML = `<div class="execution-trace-notice warn">Could not load execution history: ${esc(error.message)}</div>`;
        return null;
      })
      .finally(() => this.inflight.delete(packageId));
    this.inflight.set(packageId, request);
    return request;
  }

  #wireDetail(detail) {
    if (!detail) return;
    detail.querySelectorAll("[data-trace-open-agent]").forEach((button) =>
      button.addEventListener("click", () =>
        this.agentConsole.open(button.dataset.traceOpenAgent, {
          packageId: button.dataset.package || "",
          stage: button.dataset.stage || "",
        }),
      ),
    );
  }
}
