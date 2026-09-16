import {
  elementById as $,
  escapeHtml as esc,
  formatBytes,
  formatLocalTime as localTime,
} from "./ui-utils.js";

function durationLabel(seconds) {
  const n = Number(seconds || 0);
  if (!n) return "disabled";
  if (n % 3600 === 0) return `${n / 3600}h`;
  if (n % 60 === 0) return `${n / 60}m`;
  return `${n}s`;
}

function byteSizeLabel(bytes) {
  return formatBytes(bytes, { zeroLabel: "unbounded" });
}

function healthClass(status) {
  return ["available", "probe_due"].includes(status)
    ? "ok"
    : ["cooldown", "blocked"].includes(status)
      ? "warn"
      : "bad";
}

/**
 * Own the deliberately advanced profile-level maintenance UI.
 *
 * The normal Run surface is lane-oriented. Raw AgentProfile health, Native
 * doctor/reset actions and temporary promotions remain available here for
 * operators who explicitly open Advanced profile maintenance.
 */
export class ProfileMaintenanceView {
  constructor({ api, toast, refresh, snapshot, healthView, agentConsole }) {
    this.api = api;
    this.toast = toast;
    this.refresh = refresh;
    this.snapshot = snapshot;
    this.healthView = healthView;
    this.agentConsole = agentConsole;
    this.renderKey = "";
    this.promotionAgentId = "";
    this.#wire();
  }

  invalidate() {
    this.renderKey = "";
  }

  openAgent(agentId, { packageId = "", stage = "" } = {}) {
    this.healthView.close({ restoreFocus: false });
    this.agentConsole.open(agentId, { packageId, stage });
  }

  runAgentAction(agentId, action) {
    return this.#runAgentAction(agentId, action);
  }

  openPromotion(agentId) {
    this.#openPromotionDialog(agentId);
  }

  render(snapshot = this.snapshot()) {
    const nodes = snapshot?.nodes || [];
    const agents = snapshot?.agents || [];
    const errors = snapshot?.config_errors || [];
    if (!this.healthView.isOpen() || !$("executionProfileMaintenance").open) {
      this.renderKey = "";
      return;
    }
    const runActive = !!(snapshot?.run?.owned_running || snapshot?.run?.external_running);
    const renderKey = JSON.stringify({ nodes, agents, errors, runActive });
    if (renderKey === this.renderKey) return;
    const byId = new Map(agents.map((agent) => [agent.id, agent]));
    let html = errors
      .map(
        (error) =>
          `<div class="node-group"><div class="agent-card"><span class="pill bad">CONFIG ERROR</span><div class="agent-model">${esc(error)}</div></div></div>`,
      )
      .join("");
    for (const node of nodes) html += this.#nodeMarkup(node, byId, runActive);
    $("agentList").innerHTML = html || '<div class="empty">No agents configured.</div>';
    this.renderKey = renderKey;
  }

  #nodeMarkup(node, byId, runActive) {
    const nodeAgents = (node.agents || []).map((id) => byId.get(id)).filter(Boolean);
    const assignedOnNode = nodeAgents.some((agent) => (agent.assignments || []).length > 0);
    const loadedModels = node.loaded_models || [];
    const loadedKnown = node.loaded_models_supported === true;
    const runtimeWarning = node.reachable === true && loadedKnown && assignedOnNode && !loadedModels.length;
    const badge = runtimeWarning ? "warn" : node.reachable === true ? "ok" : node.reachable === false ? "bad" : "warn";
    const status = node.id === "local"
      ? "local"
      : runtimeWarning
        ? "reachable · no loaded model"
        : node.reachable === true
          ? `${node.latency_ms ?? "—"} ms`
          : node.reachable === false
            ? "offline"
            : "unchecked";
    const installed = node.id === "local" ? "" : `Available models: ${(node.models || []).join(", ") || "none reported"}`;
    const runtime = loadedKnown
      ? `Loaded now: ${loadedModels.join(", ") || "none (normal while idle)"}`
      : node.loaded_models_error
        ? `Runtime probe unavailable: ${node.loaded_models_error}`
        : "";
    let html = `<div class="node-group"><div class="node-group-head"><div><strong>${esc(node.name)}</strong><small>${esc(node.url || "Local CLI processes")}</small>${installed ? `<small>${esc(installed)}</small>` : ""}${runtime ? `<small>${esc(runtime)}</small>` : ""}</div><span class="pill ${badge}">${esc(status)}</span></div>`;
    for (const id of node.agents || []) {
      const agent = byId.get(id);
      if (agent) html += this.#agentMarkup(agent, runActive);
    }
    return html + "</div>";
  }

  #agentMarkup(agent, runActive) {
    const health = agent.health || {};
    const action = agent.action || {};
    const busy = action.status === "running";
    const assigned = (agent.assignments || []).length > 0;
    const resetUseful = !["available"].includes(health.status) || (health.failures || 0) > 0;
    const actionDisabled = runActive || busy || assigned || !agent.enabled;
    const reason = health.reason
      ? `${health.reason}${health.unavailable_until ? " · until " + localTime(health.unavailable_until) : ""}`
      : health.failures
        ? `${health.failures} consecutive failures`
        : "";
    const actionText = [action.error, action.stderr, action.stdout].filter(Boolean).join("\n").trim();
    const actionTitle = action.action === "doctor" ? "Doctor test" : action.action === "reset-health" ? "Reset health" : "";
    const result = action.status
      ? `<div class="agent-action-state ${esc(action.status)}"><strong>${esc(actionTitle)}: ${esc(action.status)}</strong>${action.finished_at ? ` · ${esc(localTime(action.finished_at))}` : ""}${actionText ? `<details><summary>Details</summary><pre class="agent-action-output">${esc(actionText)}</pre></details>` : ""}</div>`
      : "";
    const timeouts = agent.timeouts || {};
    const startup = Number(timeouts.first_output_seconds || 0) > 0 ? ` · first output ${durationLabel(timeouts.first_output_seconds)}` : "";
    const outputCap = Number(timeouts.max_output_bytes || 0) > 0 ? ` · output cap ${byteSizeLabel(timeouts.max_output_bytes)}` : "";
    const timeoutText = `total ${durationLabel(timeouts.total_seconds)} · inactivity ${durationLabel(timeouts.inactivity_seconds)}${startup} · output silence ${durationLabel(timeouts.output_silence_seconds)}${outputCap}`;
    const promotions = (agent.promotions || []).filter((item) => item.active !== false);
    const capsHtml = (agent.capabilities || []).map((capability) => {
      const base = agent.max_complexity?.[capability] ?? 100;
      const effective = agent.effective_max_complexity?.[capability] ?? base;
      const promoted = effective > base;
      return `<span class="pill${promoted ? " promoted" : ""}">${esc(capability)} ≤${esc(base)}${promoted ? ` →${esc(effective)}` : ""}</span>`;
    }).join("");
    const promotionSummary = promotions.length
      ? `<div class="agent-promotion-summary"><strong>Temporary promotion active</strong>${promotions.map((item) => `${esc(item.capability)} ${esc(item.base_max_complexity)}→${esc(item.promoted_max_complexity)} · ${item.fallback_only ? "fallback only" : "immediate"} · until ${esc(localTime(item.expires_at))}`).join("<br>")}</div>`
      : "";
    const promotable = (agent.capabilities || []).some((capability) => (agent.max_complexity?.[capability] ?? 100) < 100);
    const routeText = `${agent.runtime_kind === "openclaw" ? "OpenClaw" : "Native"} · ${agent.model || agent.model_route_id || "runtime default"} · ${agent.target_id || "local/direct"}`;
    const nativeControls = agent.native_maintenance
      ? `<div class="agent-actions"><button class="btn small primary open-agent-console" data-agent="${esc(agent.id)}" title="Open the Native agent workbench">Open console</button><button class="btn small agent-action" data-agent="${esc(agent.id)}" data-action="doctor" ${actionDisabled ? "disabled" : ""} title="Run a live, read-only Native adapter smoke test">${busy && action.action === "doctor" ? "Testing…" : "Doctor test"}</button><button class="btn small promotion-action${promotions.length ? " active" : ""}" data-agent="${esc(agent.id)}" ${!agent.enabled || (!promotable && !promotions.length) ? "disabled" : ""} title="Temporarily raise this agent profile's complexity ceiling for the current task">${promotions.length ? "Promotion…" : "Promote"}</button><button class="btn small danger agent-action" data-agent="${esc(agent.id)}" data-action="reset-health" ${actionDisabled || !resetUseful ? "disabled" : ""} title="Clear stored cooldown/failure state; this does not restore model quota or runtime availability">${busy && action.action === "reset-health" ? "Resetting…" : "Reset health"}</button></div>`
      : '<div class="agent-health-detail">OpenClaw lifecycle and model/location checks are available through Runtime diagnostics.</div>';
    return `<div class="agent-card"><div class="agent-top"><strong>${esc(agent.id)}</strong><span class="pill ${healthClass(health.status)}">${esc(agent.enabled ? health.status : "disabled")}</span></div><div class="agent-model">${esc(routeText)}</div><div class="agent-health-detail">${esc(timeoutText)}</div>${reason ? `<div class="agent-health-detail">${esc(reason)}</div>` : ""}<div class="agent-caps">${capsHtml}</div>${promotionSummary}${(agent.assignments || []).map((item) => `<div class="assignment">${esc(item.package_id)} · ${esc(item.stage)}${item.parallel ? " · parallel" : ""}</div>`).join("")}${nativeControls}${agent.native_maintenance && runActive ? '<div class="agent-health-detail">Health actions are disabled while the orchestrator is active.</div>' : agent.native_maintenance && assigned ? '<div class="agent-health-detail">Health actions are disabled while this agent has an active assignment.</div>' : ""}${result}</div>`;
  }

  #wire() {
    const actionHandler = (event) => this.#handleAction(event);
    $("agentList").addEventListener("click", actionHandler);
    $("executionHealthOverview").addEventListener("click", actionHandler);
    $("executionProfileMaintenance").addEventListener("toggle", () => {
      this.invalidate();
      if ($("executionProfileMaintenance").open) this.render();
    });
    $("providerPromotionCapabilities").addEventListener("change", () => this.#syncFinalReviewState());
    $("applyProviderPromotion").addEventListener("click", () => this.#applyPromotion());
    $("revokeProviderPromotion").addEventListener("click", () => this.#revokePromotion());
    for (const id of ["cancelProviderPromotion", "closeProviderPromotion"])
      $(id).addEventListener("click", () => $("providerPromotionDialog").close());
    $("providerPromotionDialog").addEventListener("click", (event) => {
      if (event.target === $("providerPromotionDialog")) $("providerPromotionDialog").close();
    });
    $("providerPromotionDialog").addEventListener("close", () => {
      this.promotionAgentId = "";
    });
  }

  #handleAction(event) {
    const button = event.target.closest("button[data-agent]");
    if (!button || button.disabled) return;
    if (button.classList.contains("agent-action")) void this.runAgentAction(button.dataset.agent, button.dataset.action);
    if (button.classList.contains("promotion-action")) this.openPromotion(button.dataset.agent);
    if (button.classList.contains("open-agent-console")) {
      this.openAgent(button.dataset.agent, {
        packageId: button.dataset.package || "",
        stage: button.dataset.stage || "",
      });
    }
  }

  async #runAgentAction(agentId, action) {
    const label = action === "doctor" ? "Doctor test" : "Reset health";
    if (action === "reset-health" && !confirm(`Reset stored health for ${agentId}? This does not restore quota, credentials, or runtime/model availability.`)) return;
    try {
      await this.api("/api/agent/action", {
        method: "POST",
        body: JSON.stringify({ agent_id: agentId, action, timeout_seconds: 180 }),
      });
      this.toast(`${label} started for ${agentId}`);
      await this.refresh();
    } catch (error) {
      this.toast(error.message, true);
    }
  }

  #agent(agentId) {
    return (this.snapshot()?.agents || []).find((item) => item.id === agentId) || null;
  }

  #waitScopes(agentId) {
    const waits = Object.values(this.snapshot()?.orchestration?.agent_waits || {});
    const result = [];
    for (const wait of waits) {
      if (!(wait.policy_excluded || []).some((item) => item.agent_id === agentId)) continue;
      const packageId = String(wait.package_id || "").trim();
      const capability = String(wait.capability || "").trim();
      const stage = String(wait.stage || "").trim();
      if (!packageId || !capability) continue;
      if (!result.some((item) => item.packageId === packageId && item.capability === capability))
        result.push({ packageId, capability, stage });
    }
    return result;
  }

  #selectedCapabilities() {
    return Array.from(document.querySelectorAll('#providerPromotionCapabilities input[type="checkbox"]:checked')).map((input) => input.value);
  }

  #syncFinalReviewState() {
    const enabled = this.#selectedCapabilities().includes("review");
    const checkbox = $("providerPromotionFinalReview");
    checkbox.disabled = !enabled;
    if (!enabled) checkbox.checked = false;
  }

  #openPromotionDialog(agentId) {
    const agent = this.#agent(agentId);
    if (!agent) {
      this.toast(`Agent ${agentId} is no longer configured.`, true);
      return;
    }
    this.promotionAgentId = agentId;
    const promotions = (agent.promotions || []).filter((item) => item.active !== false);
    const activeCapabilities = new Set(promotions.map((item) => item.capability));
    const waitScopes = this.#waitScopes(agentId);
    const blockedCapabilities = waitScopes.map((item) => item.capability).filter((capability) => (agent.capabilities || []).includes(capability));
    const defaults = new Set(blockedCapabilities.length ? blockedCapabilities : activeCapabilities.size ? [...activeCapabilities] : (agent.capabilities || []).filter((capability) => (agent.max_complexity?.[capability] ?? 100) < 100));
    $("providerPromotionTitle").textContent = `Temporary promotion — ${agentId}`;
    $("providerPromotionSubtitle").textContent = `${agent.model || agent.adapter} · current task only · changes are hot-reloaded by the scheduler`;
    $("providerPromotionCapabilities").innerHTML = (agent.capabilities || []).map((capability) => {
      const base = agent.max_complexity?.[capability] ?? 100;
      const current = promotions.find((item) => item.capability === capability);
      const disabled = base >= 100 && !current;
      return `<label class="promotion-capability"><input type="checkbox" value="${esc(capability)}" ${defaults.has(capability) ? "checked" : ""} ${disabled ? "disabled" : ""}><span>${esc(capability)}</span><small>base ≤${esc(base)}${current ? ` · active ≤${esc(current.promoted_max_complexity)}` : ""}</small></label>`;
    }).join("");
    const highestActive = promotions.reduce((maximum, item) => Math.max(maximum, Number(item.promoted_max_complexity || 0)), 0);
    $("providerPromotionCeiling").value = String(highestActive || 100);
    $("providerPromotionDuration").value = "14400";
    const packageSelect = $("providerPromotionPackage");
    const scopeByPackage = new Map();
    for (const item of waitScopes) {
      if (!scopeByPackage.has(item.packageId)) scopeByPackage.set(item.packageId, []);
      scopeByPackage.get(item.packageId).push(item.stage || item.capability);
    }
    packageSelect.innerHTML = ['<option value="">Entire current task</option>', ...Array.from(scopeByPackage.entries()).map(([packageId, stages]) => `<option value="${esc(packageId)}">${esc(packageId)} · ${esc([...new Set(stages)].join(", "))}</option>`)].join("");
    const activeScopes = [...new Set(promotions.map((item) => String(item.package_id || "")))];
    if (activeScopes.length === 1 && activeScopes[0]) packageSelect.value = activeScopes[0];
    else if (!promotions.length && scopeByPackage.size === 1) packageSelect.value = scopeByPackage.keys().next().value;
    else packageSelect.value = "";
    $("providerPromotionFallbackOnly").checked = promotions.length ? promotions.every((item) => item.fallback_only) : true;
    $("providerPromotionFinalReview").checked = promotions.some((item) => item.allow_final_review);
    $("providerPromotionReason").value = promotions.find((item) => item.reason)?.reason || "Preferred agent temporarily unavailable";
    $("providerPromotionCurrent").classList.toggle("empty", !promotions.length);
    $("providerPromotionCurrent").innerHTML = promotions.length
      ? promotions.map((item) => `<strong>${esc(item.capability)} ${esc(item.base_max_complexity)}→${esc(item.promoted_max_complexity)}</strong> · ${item.package_id ? `scope ${esc(item.package_id)}` : "task-wide"} · ${item.fallback_only ? "fallback only" : "immediate"} · expires ${esc(localTime(item.expires_at))}${item.allow_final_review ? " · final review allowed" : ""}`).join("<br>")
      : "No active promotion for this agent profile.";
    $("revokeProviderPromotion").disabled = !promotions.length;
    this.#syncFinalReviewState();
    $("providerPromotionDialog").showModal();
  }

  async #applyPromotion() {
    const agentId = this.promotionAgentId;
    const capabilities = this.#selectedCapabilities();
    if (!agentId || !capabilities.length) {
      this.toast("Select at least one capability to promote.", true);
      return;
    }
    const ceiling = Number($("providerPromotionCeiling").value || 100);
    const duration = Number($("providerPromotionDuration").value || 14400);
    if (!Number.isInteger(ceiling) || ceiling < 1 || ceiling > 100) {
      this.toast("Temporary ceiling must be an integer between 1 and 100.", true);
      return;
    }
    $("applyProviderPromotion").disabled = true;
    try {
      const result = await this.api("/api/agent/promotion", {
        method: "POST",
        body: JSON.stringify({
          agent_id: agentId,
          enabled: true,
          capabilities,
          promoted_max_complexity: ceiling,
          duration_seconds: duration,
          fallback_only: $("providerPromotionFallbackOnly").checked,
          allow_final_review: $("providerPromotionFinalReview").checked,
          package_id: $("providerPromotionPackage").value,
          reason: $("providerPromotionReason").value.trim(),
        }),
      });
      $("providerPromotionDialog").close();
      this.toast(`Promotion active for ${agentId}: ${(result.promotions || []).map((item) => item.capability).join(", ")}.`);
      await this.refresh();
    } catch (error) {
      this.toast(error.message, true);
    } finally {
      $("applyProviderPromotion").disabled = false;
    }
  }

  async #revokePromotion() {
    const agentId = this.promotionAgentId;
    if (!agentId || !confirm(`Revoke all active temporary promotions for ${agentId}?`)) return;
    $("revokeProviderPromotion").disabled = true;
    try {
      await this.api("/api/agent/promotion", {
        method: "POST",
        body: JSON.stringify({ agent_id: agentId, enabled: false, capabilities: [] }),
      });
      $("providerPromotionDialog").close();
      this.toast(`Temporary promotion revoked for ${agentId}.`);
      await this.refresh();
    } catch (error) {
      this.toast(error.message, true);
    } finally {
      $("revokeProviderPromotion").disabled = false;
    }
  }
}
