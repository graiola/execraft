import { escapeHtml as esc } from "./ui-utils.js";

const ROUTING_MODES = Object.freeze([
  { value: "automatic", label: "Automatic" },
  { value: "prefer", label: "Prefer" },
  { value: "force", label: "Force" },
]);

const AGENT_STAGE_ROLES = new Set([
  "decompose",
  "implement",
  "review",
  "fix_review",
  "final_review",
]);

function uniqueStrings(value) {
  if (!Array.isArray(value)) return [];
  return [...new Set(value.map((item) => String(item || "").trim()).filter(Boolean))];
}

function runtimeLabel(kind) {
  if (kind === "openclaw") return "OpenClaw";
  if (kind === "native") return "Native";
  return kind || "Runtime";
}

function laneDetail(lane) {
  if (!lane) return "Automatic scheduler selection";
  return [
    runtimeLabel(lane.runtime_kind),
    lane.model_display_name || lane.model_route_id || "default model",
    lane.target_display_name || lane.target_id || "local/direct",
  ]
    .filter(Boolean)
    .join(" · ");
}

function availabilityLabel(value) {
  return {
    ready: "Ready",
    degraded: "Degraded",
    unavailable: "Unavailable",
    unsupported: "Unsupported",
    disabled: "Disabled",
    unknown: "Health unknown",
  }[value] || String(value || "Unknown");
}

function availabilityClass(value) {
  if (value === "ready") return "ok";
  if (value === "degraded" || value === "unknown") return "warn";
  return "bad";
}

function roleForStage(stage) {
  return AGENT_STAGE_ROLES.has(stage) ? stage : "";
}

function nextRoleIds(packageInfo) {
  const stage = String(packageInfo?.stage || "");
  if (packageInfo?.execution_mode === "review_shard" && stage === "review") return [];
  if (stage === "prepare") return ["implement"];
  if (stage === "decompose") return ["implement"];
  if (["implement", "fast_verify", "targeted_verify"].includes(stage)) return ["review"];
  if (stage === "review") {
    return (packageInfo.review_findings || []).length
      ? ["fix_review"]
      : ["fix_review", "final_review"];
  }
  if (["fix_review", "regression_verify"].includes(stage)) return ["final_review"];
  return [];
}

function roleMap(snapshot) {
  return new Map(
    (snapshot?.execution_roles || [])
      .filter((role) => role?.id)
      .map((role) => [String(role.id), role]),
  );
}

function laneMap(snapshot) {
  return new Map(
    (snapshot?.execution_lanes || [])
      .filter((lane) => lane?.id)
      .map((lane) => [String(lane.id), lane]),
  );
}

function profileLaneMap(snapshot) {
  const result = new Map();
  for (const lane of snapshot?.execution_lanes || []) {
    for (const profileId of lane.profile_ids || []) result.set(String(profileId), lane);
  }
  return result;
}

function routingForRole(snapshot, packageInfo, roleId) {
  const preferences = uniqueStrings(packageInfo?.agent_preferences?.[roleId]);
  const binding = new Set(uniqueStrings(packageInfo?.agent_preference_binding_roles));
  const byProfile = profileLaneMap(snapshot);
  const preferredLanes = [];
  for (const profileId of preferences) {
    const lane = byProfile.get(profileId);
    if (lane && !preferredLanes.some((item) => item.id === lane.id)) preferredLanes.push(lane);
  }
  return {
    mode: preferences.length ? (binding.has(roleId) ? "force" : "prefer") : "automatic",
    lane: preferredLanes[0] || null,
    preferredLanes,
    profileIds: preferences,
  };
}

function laneForAgent(snapshot, agentId) {
  if (!agentId) return null;
  return profileLaneMap(snapshot).get(String(agentId)) || null;
}

function roleLabel(snapshot, roleId) {
  const role = roleMap(snapshot).get(roleId);
  return String(role?.label || roleId || "Execution");
}

function effectiveRouteMarkup(snapshot, packageInfo, roleId, { context = "" } = {}) {
  const route = routingForRole(snapshot, packageInfo, roleId);
  const modeLabel = ROUTING_MODES.find((item) => item.value === route.mode)?.label || route.mode;
  const lane = route.lane;
  const fallback = route.preferredLanes.length > 1
    ? ` · ${route.preferredLanes.length - 1} additional preferred lane${route.preferredLanes.length === 2 ? "" : "s"}`
    : "";
  const routeText = lane ? lane.display_name : "Scheduler chooses a compatible lane";
  return `<article class="work-package-route-card${context ? ` ${esc(context)}` : ""}">
    <div class="work-package-route-card-head"><span>${esc(roleLabel(snapshot, roleId))}</span><span class="pill">${esc(modeLabel)}</span></div>
    <strong>${esc(routeText)}</strong>
    <small>${esc(lane ? laneDetail(lane) : "Health, capability, complexity, independence and concurrency policy remain authoritative")}${esc(fallback)}</small>
  </article>`;
}

function currentAndNextMarkup(snapshot, packageInfo) {
  const assignment = (snapshot?.assignments || []).find(
    (item) => String(item.package_id || "") === String(packageInfo.id || ""),
  );
  const stageRole = roleForStage(String(assignment?.stage || packageInfo.stage || ""));
  const currentRole = stageRole || "";
  const activeLane = laneForAgent(snapshot, assignment?.agent_id || "");
  let current = "";
  if (currentRole) {
    const configured = routingForRole(snapshot, packageInfo, currentRole);
    const lane = activeLane || configured.lane;
    const mode = activeLane ? "Active" : ROUTING_MODES.find((item) => item.value === configured.mode)?.label || configured.mode;
    current = `<article class="work-package-route-card current"><div class="work-package-route-card-head"><span>Current · ${esc(roleLabel(snapshot, currentRole))}</span><span class="pill ${activeLane ? "ok" : ""}">${esc(mode)}</span></div><strong>${esc(lane?.display_name || "Scheduler controlled")}</strong><small>${esc(lane ? laneDetail(lane) : `Stage ${packageInfo.stage || "unknown"}${assignment?.status ? ` · ${assignment.status}` : ""}`)}</small></article>`;
  } else {
    current = `<article class="work-package-route-card current deterministic"><div class="work-package-route-card-head"><span>Current</span><span class="pill">Local check</span></div><strong>${esc(String(packageInfo.stage || "deterministic stage"))}</strong><small>No agent routing decision is made during this deterministic stage.</small></article>`;
  }

  const nextRoles = nextRoleIds(packageInfo);
  const next = nextRoles.length
    ? nextRoles.map((roleId) => effectiveRouteMarkup(snapshot, packageInfo, roleId, { context: "next" })).join("")
    : `<article class="work-package-route-card next"><div class="work-package-route-card-head"><span>Next</span><span class="pill">—</span></div><strong>No later agent role is currently planned</strong><small>The remaining transition is deterministic, completion-only, or depends on a future replan.</small></article>`;
  return `<div class="work-package-route-summary" aria-label="Current and next execution routing">${current}${next}</div>`;
}

function laneOptionLabel(lane) {
  return `${lane.display_name} — ${availabilityLabel(lane.availability)}`;
}

function laneCard(lane, snapshot) {
  if (!lane) return `<div class="lane-selection-card automatic"><strong>Automatic routing</strong><small>The scheduler chooses among compatible profiles at invocation time.</small></div>`;
  const roles = (lane.roles || []).map((roleId) => roleLabel(snapshot, roleId)).join(" · ");
  return `<div class="lane-selection-card"><div><strong>${esc(lane.display_name)}</strong><span class="pill ${availabilityClass(lane.availability)}">${esc(availabilityLabel(lane.availability))}</span></div><small>${esc(laneDetail(lane))}</small><small>Roles: ${esc(roles || "none")}${lane.diagnostics_summary ? ` · ${esc(lane.diagnostics_summary)}` : ""}</small></div>`;
}

function policySignature(packageInfo) {
  return JSON.stringify({
    agents: packageInfo?.agent_preferences || {},
    skills: packageInfo?.skill_preferences || {},
    bindings: packageInfo?.agent_preference_binding_roles || [],
  });
}

export class WorkPackageExecutionView {
  constructor({ api, toast, refreshSnapshot, rerenderWorkPackage }) {
    this.api = api;
    this.toast = toast;
    this.refreshSnapshot = refreshSnapshot;
    this.rerenderWorkPackage = rerenderWorkPackage;
    this.contextKey = "";
    this.routingDrafts = new Map();
    this.advancedDrafts = new Map();
    this.previewByPackage = new Map();
    this.busyPackages = new Set();
  }

  setContext(snapshot) {
    const key = `${snapshot?.project?.id || ""}:${snapshot?.project?.task_id || ""}`;
    if (this.contextKey && key !== this.contextKey) {
      this.routingDrafts.clear();
      this.advancedDrafts.clear();
      this.previewByPackage.clear();
      this.busyPackages.clear();
    }
    this.contextKey = key;
  }

  render(snapshot, packageInfo, { runActive = false } = {}) {
    this.setContext(snapshot);
    const draft = this.#routingDraft(snapshot, packageInfo);
    const lanes = this.#eligibleLanes(snapshot, draft.roleId);
    const selectedLane = laneMap(snapshot).get(draft.laneId) || null;
    const automatic = draft.mode === "automatic";
    const preview = this.previewByPackage.get(packageInfo.id) || null;
    const busy = this.busyPackages.has(packageInfo.id);
    const shardOption = !packageInfo.parent_id
      ? `<label class="execution-shard-option"><input type="checkbox" data-lane-apply-shards ${draft.applyToShards ? "checked" : ""}> Apply routing to direct child shards</label>`
      : `<div class="execution-shard-note">This shard owns its routing independently. Edit the parent to propagate a routing choice across direct shards.</div>`;

    return `<div class="work-package-execution-workbench">
      <section class="work-package-detail-section execution-context-section">
        <div class="work-package-section-head"><div><h4>Execution route</h4><div class="preference-help">Current work and the next agent boundary use the existing scheduler policy; lanes are presentation groups, not scheduler identities.</div></div></div>
        ${currentAndNextMarkup(snapshot, packageInfo)}
      </section>
      <section class="work-package-detail-section lane-routing-editor">
        <div class="work-package-section-head"><div><h4>Edit routing</h4><div class="preference-help">Choose a role, policy, and execution lane. Automatic clears the role override; Prefer keeps compatible fallbacks; Force makes the matching profile pool binding.</div></div></div>
        <div class="lane-routing-grid">
          <label>Role<select data-lane-role>${(snapshot?.execution_roles || []).map((item) => `<option value="${esc(item.id)}" ${String(item.id) === draft.roleId ? "selected" : ""}>${esc(item.label || item.id)}</option>`).join("")}</select></label>
          <label>Routing<select data-lane-mode>${ROUTING_MODES.map((item) => `<option value="${item.value}" ${item.value === draft.mode ? "selected" : ""}>${item.label}</option>`).join("")}</select></label>
          <label data-lane-field class="${automatic ? "is-disabled" : ""}">Execution lane<select data-lane-select ${automatic ? "disabled" : ""}>${lanes.length ? lanes.map((lane) => `<option value="${esc(lane.id)}" ${lane.id === draft.laneId ? "selected" : ""} ${["unsupported", "disabled"].includes(lane.availability) ? "disabled" : ""}>${esc(laneOptionLabel(lane))}</option>`).join("") : '<option value="">No compatible lanes</option>'}</select></label>
        </div>
        <div data-lane-card>${laneCard(automatic ? null : selectedLane, snapshot)}</div>
        <div class="lane-routing-options">${shardOption}</div>
        <p class="execution-note">${runActive ? "A live invocation is never hot-migrated. Preview shows whether the dashboard-owned driver can be cancelled and restarted at the invocation boundary." : "The change is persisted through the package's existing role→profile preference seam and applies to the next invocation."}</p>
        <div class="execution-actions"><button type="button" class="btn small" data-lane-preview ${busy ? "disabled" : ""}>Preview</button><button type="button" class="btn small primary" data-lane-apply ${busy || runActive ? "disabled" : ""}>${busy ? "Applying…" : "Apply next invocation"}</button><button type="button" class="btn small danger ${preview?.can_cancel_and_switch ? "" : "hidden"}" data-lane-cancel-switch ${busy ? "disabled" : ""}>Cancel &amp; switch</button></div>
        <pre data-lane-plan class="execution-output muted ${preview ? "" : "hidden"}">${esc(preview ? this.#previewText(preview, snapshot) : "No routing change previewed.")}</pre>
      </section>
      ${this.#advancedMarkup(snapshot, packageInfo, { runActive })}
    </div>`;
  }

  bind(container, snapshot, packageInfo) {
    if (!container) return;
    const roleSelect = container.querySelector("[data-lane-role]");
    const modeSelect = container.querySelector("[data-lane-mode]");
    const laneSelect = container.querySelector("[data-lane-select]");
    const shardInput = container.querySelector("[data-lane-apply-shards]");
    roleSelect?.addEventListener("change", () => {
      const draft = this.#routingDraft(snapshot, packageInfo);
      draft.roleId = roleSelect.value;
      const effective = routingForRole(snapshot, packageInfo, draft.roleId);
      draft.mode = effective.mode;
      draft.laneId = effective.lane?.id || this.#eligibleLanes(snapshot, draft.roleId)[0]?.id || "";
      this.previewByPackage.delete(packageInfo.id);
      this.rerenderWorkPackage(packageInfo.id);
    });
    modeSelect?.addEventListener("change", () => {
      const draft = this.#routingDraft(snapshot, packageInfo);
      draft.mode = modeSelect.value;
      if (draft.mode === "automatic") draft.laneId = "";
      else if (!draft.laneId) draft.laneId = this.#eligibleLanes(snapshot, draft.roleId)[0]?.id || "";
      this.previewByPackage.delete(packageInfo.id);
      this.rerenderWorkPackage(packageInfo.id);
    });
    laneSelect?.addEventListener("change", () => {
      const draft = this.#routingDraft(snapshot, packageInfo);
      draft.laneId = laneSelect.value;
      this.previewByPackage.delete(packageInfo.id);
      const card = container.querySelector("[data-lane-card]");
      if (card) card.innerHTML = laneCard(laneMap(snapshot).get(draft.laneId), snapshot);
    });
    shardInput?.addEventListener("change", () => {
      this.#routingDraft(snapshot, packageInfo).applyToShards = shardInput.checked;
      this.previewByPackage.delete(packageInfo.id);
    });
    container.querySelector("[data-lane-preview]")?.addEventListener("click", () => this.#preview(snapshot, packageInfo, container));
    container.querySelector("[data-lane-apply]")?.addEventListener("click", () => this.#apply(snapshot, packageInfo, false));
    container.querySelector("[data-lane-cancel-switch]")?.addEventListener("click", () => this.#apply(snapshot, packageInfo, true));
    this.#bindAdvanced(container, snapshot, packageInfo);
  }

  #routingDraft(snapshot, packageInfo) {
    let draft = this.routingDrafts.get(packageInfo.id);
    if (draft) return draft;
    const assignment = (snapshot?.assignments || []).find((item) => String(item.package_id || "") === String(packageInfo.id));
    const currentRole = roleForStage(String(assignment?.stage || packageInfo.stage || ""));
    const next = nextRoleIds(packageInfo);
    const roleId = currentRole || next[0] || "implement";
    const effective = routingForRole(snapshot, packageInfo, roleId);
    draft = {
      roleId,
      mode: effective.mode,
      laneId: effective.lane?.id || this.#eligibleLanes(snapshot, roleId)[0]?.id || "",
      applyToShards: false,
    };
    this.routingDrafts.set(packageInfo.id, draft);
    return draft;
  }

  #eligibleLanes(snapshot, roleId) {
    return (snapshot?.execution_lanes || []).filter(
      (lane) => (lane.roles || []).includes(roleId),
    );
  }

  #selectionPayload(snapshot, packageInfo) {
    const draft = this.#routingDraft(snapshot, packageInfo);
    const lane = laneMap(snapshot).get(draft.laneId) || null;
    if (draft.mode !== "automatic" && !lane) throw new Error(`Choose an execution lane for ${roleLabel(snapshot, draft.roleId)}.`);
    return {
      package_id: packageInfo.id,
      role: draft.roleId,
      mode: draft.mode,
      runtime_id: lane?.runtime_id || "",
      model_route_id: lane?.model_route_id || "",
      target_id: lane?.target_id || "",
      apply_to_shards: Boolean(draft.applyToShards),
    };
  }

  #previewText(preview, snapshot) {
    const plan = preview?.plan || {};
    const laneByProfile = profileLaneMap(snapshot);
    const matching = uniqueStrings(plan.matching_profiles)
      .map((profileId) => laneByProfile.get(profileId)?.display_name || profileId);
    const uniqueMatching = [...new Set(matching)];
    const lines = [
      `Policy: ${String(plan.mode || preview.mode || "automatic")}`,
      `Effective: ${String(plan.effective_when || "next invocation")}`,
      uniqueMatching.length ? `Matching lane: ${uniqueMatching.join(" · ")}` : "Matching lane: automatic scheduler selection",
      preview.apply_to_shards ? "Propagation: direct child shards" : "Propagation: selected Work Package only",
    ];
    if (preview.external_driver_blocks_switch) lines.push("External driver active: routing cannot change until it is idle.");
    else if (preview.can_cancel_and_switch) lines.push("Dashboard driver active: Cancel & switch is available; no hot migration occurs.");
    return lines.join("\n");
  }

  async #preview(snapshot, packageInfo, container) {
    try {
      const result = await this.api("/api/runtime/selection/preview", {
        method: "POST",
        body: JSON.stringify(this.#selectionPayload(snapshot, packageInfo)),
      });
      this.previewByPackage.set(packageInfo.id, result);
      const plan = container.querySelector("[data-lane-plan]");
      if (plan) {
        plan.textContent = this.#previewText(result, snapshot);
        plan.classList.remove("hidden");
      }
      const apply = container.querySelector("[data-lane-apply]");
      if (apply) apply.disabled = !result.can_apply_now;
      const cancel = container.querySelector("[data-lane-cancel-switch]");
      if (cancel) cancel.classList.toggle("hidden", !result.can_cancel_and_switch);
    } catch (error) {
      this.toast(error.message, true);
      const plan = container.querySelector("[data-lane-plan]");
      if (plan) {
        plan.textContent = error.message;
        plan.classList.remove("hidden");
      }
    }
  }

  async #apply(snapshot, packageInfo, cancelAndSwitch) {
    if (cancelAndSwitch && !confirm("Stop the dashboard-owned orchestrator, persist this lane routing at the invocation boundary, and resume? The current invocation is cancelled rather than hot-migrated.")) return;
    this.busyPackages.add(packageInfo.id);
    this.rerenderWorkPackage(packageInfo.id);
    try {
      const result = await this.api("/api/runtime/selection/apply", {
        method: "POST",
        body: JSON.stringify({
          ...this.#selectionPayload(snapshot, packageInfo),
          cancel_and_switch: cancelAndSwitch,
        }),
      });
      this.toast(result.restarted ? "Execution routing changed; orchestration resumed" : "Execution routing updated");
      this.routingDrafts.delete(packageInfo.id);
      this.advancedDrafts.delete(packageInfo.id);
      this.previewByPackage.delete(packageInfo.id);
      await this.refreshSnapshot();
    } catch (error) {
      this.toast(error.message, true);
    } finally {
      this.busyPackages.delete(packageInfo.id);
      this.rerenderWorkPackage(packageInfo.id);
    }
  }

  #advancedDraft(snapshot, packageInfo) {
    const signature = policySignature(packageInfo);
    let draft = this.advancedDrafts.get(packageInfo.id);
    if (draft && (draft.dirty || draft.signature === signature)) return draft;
    const roles = snapshot?.execution_roles || [];
    draft = {
      signature,
      dirty: false,
      agents: {},
      skills: {},
      pendingAgents: {},
      pendingSkills: {},
      applyToShards: false,
    };
    for (const role of roles) {
      draft.agents[role.id] = uniqueStrings(packageInfo?.agent_preferences?.[role.id]);
      draft.skills[role.id] = uniqueStrings(packageInfo?.skill_preferences?.[role.id]);
      draft.pendingAgents[role.id] = "";
      draft.pendingSkills[role.id] = "";
    }
    this.advancedDrafts.set(packageInfo.id, draft);
    return draft;
  }

  #advancedMarkup(snapshot, packageInfo, { runActive = false } = {}) {
    const draft = this.#advancedDraft(snapshot, packageInfo);
    const busy = this.busyPackages.has(packageInfo.id);
    const saveBlocked = busy || runActive;
    const laneRows = (snapshot?.execution_lanes || [])
      .map((lane) => `<details class="lane-advanced-row"><summary><span>${esc(lane.display_name)}</span><span class="pill ${availabilityClass(lane.availability)}">${esc(availabilityLabel(lane.availability))}</span></summary><dl class="kv"><dt>Runtime</dt><dd>${esc(lane.runtime_id)}</dd><dt>Model route</dt><dd>${esc(lane.model_route_id || "direct")}</dd><dt>Target</dt><dd>${esc(lane.target_id || "local/direct")}</dd><dt>Profiles</dt><dd>${esc((lane.profile_ids || []).join(", ") || "none")}</dd><dt>Health</dt><dd>${esc(lane.health || "unknown")}</dd></dl></details>`)
      .join("");
    const roleRows = (snapshot?.execution_roles || []).map((role) => this.#advancedRoleMarkup(snapshot, role, draft)).join("");
    return `<details class="execution-advanced work-package-execution-advanced">
      <summary>Advanced profile and skill policy</summary>
      <div class="execution-advanced-body">
        <p class="execution-note">Normal routing should use lanes above. Advanced controls expose canonical profile IDs and role skills for troubleshooting or precise compatibility work; scheduler validation remains authoritative.</p>
        <h5>Lane composition</h5><div class="lane-advanced-list">${laneRows || '<div class="relationship-empty">No execution lanes configured.</div>'}</div>
        <h5>Raw role policy</h5><div class="advanced-policy-list">${roleRows}</div>
        ${runActive ? '<p class="execution-note">Stop orchestration before saving raw profile/skill policy. Lane preview remains available above for safe invocation-boundary switching.</p>' : ""}
        <div class="preference-actions"><button type="button" class="btn small primary" data-advanced-save ${saveBlocked ? "disabled" : ""}>${busy ? "Saving…" : "Save advanced policy"}</button><button type="button" class="btn small" data-advanced-reset ${busy ? "disabled" : ""}>Reset draft</button>${!packageInfo.parent_id ? `<label><input type="checkbox" data-advanced-shards ${draft.applyToShards ? "checked" : ""}> Apply to direct shards</label>` : ""}</div>
      </div>
    </details>`;
  }

  #advancedRoleMarkup(snapshot, role, draft) {
    const agents = draft.agents[role.id] || [];
    const skills = draft.skills[role.id] || [];
    const eligibleAgents = (snapshot?.agents || []).filter((agent) => agent.enabled && (agent.capabilities || []).includes(role.capability) && !agents.includes(agent.id));
    const eligibleSkills = (snapshot?.skills || []).filter((skill) => (!(skill.roles || []).length || (skill.roles || []).includes("*") || (skill.roles || []).includes(role.id)) && !skills.includes(skill.id));
    const agentOptions = eligibleAgents.map((agent) => `<option value="${esc(agent.id)}">${esc(agent.id)} · ${esc(agent.health?.status || "unknown")}${agent.model ? ` · ${esc(agent.model)}` : ""}</option>`).join("");
    const skillOptions = eligibleSkills.map((skill) => `<option value="${esc(skill.id)}">${esc(skill.id)} · ${esc(skill.description || "")}</option>`).join("");
    const agentChips = agents.length ? agents.map((id, index) => this.#policyChip("agent", role.id, id, index, true)).join("") : '<span class="preference-help">Automatic scheduler order</span>';
    const skillChips = skills.length ? skills.map((id, index) => this.#policyChip("skill", role.id, id, index, false)).join("") : `<span class="preference-help">Defaults: ${esc((role.default_skills || []).join(", ") || "none")}</span>`;
    return `<section class="policy-role"><h5>${esc(role.label || role.id)} <span>${esc(role.capability || role.id)}${role.read_only ? " · read-only" : ""}</span></h5><div class="preference-row"><label>Profiles</label><select data-advanced-select="agent" data-role="${esc(role.id)}"><option value="">Choose an eligible profile…</option>${agentOptions}</select><button class="btn small" type="button" data-advanced-action="add" data-kind="agent" data-role="${esc(role.id)}">Add</button><div class="preference-list">${agentChips}</div></div><div class="preference-row"><label>Skills</label><select data-advanced-select="skill" data-role="${esc(role.id)}"><option value="">Choose a compatible skill…</option>${skillOptions}</select><button class="btn small" type="button" data-advanced-action="add" data-kind="skill" data-role="${esc(role.id)}">Add</button><div class="preference-list">${skillChips}</div></div></section>`;
  }

  #policyChip(kind, roleId, id, index, ranked) {
    return `<span class="preference-chip ${kind === "skill" ? "skill" : ""}">${ranked ? `${index + 1}. ` : ""}${esc(id)}<button type="button" data-advanced-action="up" data-kind="${kind}" data-role="${esc(roleId)}" data-index="${index}" title="Move up">↑</button><button type="button" data-advanced-action="down" data-kind="${kind}" data-role="${esc(roleId)}" data-index="${index}" title="Move down">↓</button><button type="button" data-advanced-action="remove" data-kind="${kind}" data-role="${esc(roleId)}" data-index="${index}" title="Remove">×</button></span>`;
  }

  #bindAdvanced(container, snapshot, packageInfo) {
    const draft = this.#advancedDraft(snapshot, packageInfo);
    container.querySelectorAll("[data-advanced-select]").forEach((select) => {
      select.addEventListener("change", () => {
        const pending = select.dataset.advancedSelect === "skill" ? draft.pendingSkills : draft.pendingAgents;
        pending[select.dataset.role] = select.value;
        draft.dirty = true;
      });
    });
    container.querySelectorAll("[data-advanced-action]").forEach((button) => {
      button.addEventListener("click", () => {
        const roleId = button.dataset.role;
        const kind = button.dataset.kind;
        const action = button.dataset.advancedAction;
        const collection = kind === "skill" ? draft.skills : draft.agents;
        const pending = kind === "skill" ? draft.pendingSkills : draft.pendingAgents;
        const items = collection[roleId] || [];
        if (action === "add") {
          const value = pending[roleId] || "";
          if (value && !items.includes(value)) items.push(value);
          pending[roleId] = "";
        } else {
          const index = Number(button.dataset.index);
          if (action === "remove") items.splice(index, 1);
          if (action === "up" && index > 0) [items[index - 1], items[index]] = [items[index], items[index - 1]];
          if (action === "down" && index < items.length - 1) [items[index + 1], items[index]] = [items[index], items[index + 1]];
        }
        collection[roleId] = items;
        draft.dirty = true;
        this.rerenderWorkPackage(packageInfo.id);
      });
    });
    container.querySelector("[data-advanced-shards]")?.addEventListener("change", (event) => {
      draft.applyToShards = event.target.checked;
      draft.dirty = true;
    });
    container.querySelector("[data-advanced-reset]")?.addEventListener("click", () => {
      this.advancedDrafts.delete(packageInfo.id);
      this.rerenderWorkPackage(packageInfo.id);
    });
    container.querySelector("[data-advanced-save]")?.addEventListener("click", () => this.#saveAdvanced(snapshot, packageInfo));
  }

  async #saveAdvanced(snapshot, packageInfo) {
    const draft = this.#advancedDraft(snapshot, packageInfo);
    const agent_preferences = {};
    const skill_preferences = {};
    for (const role of snapshot?.execution_roles || []) {
      const agents = uniqueStrings(draft.agents[role.id]);
      const skills = uniqueStrings(draft.skills[role.id]);
      if (agents.length) agent_preferences[role.id] = agents;
      if (skills.length) skill_preferences[role.id] = skills;
    }
    const bindingRoles = uniqueStrings(packageInfo.agent_preference_binding_roles).filter(
      (roleId) => (agent_preferences[roleId] || []).length,
    );
    this.busyPackages.add(packageInfo.id);
    this.rerenderWorkPackage(packageInfo.id);
    try {
      const result = await this.api("/api/package/policy", {
        method: "POST",
        body: JSON.stringify({
          package_id: packageInfo.id,
          agent_preferences,
          skill_preferences,
          agent_preference_binding_roles: bindingRoles,
          apply_to_shards: Boolean(draft.applyToShards),
        }),
      });
      this.toast(result.stdout?.trim?.() || "Advanced execution policy saved");
      this.routingDrafts.delete(packageInfo.id);
      this.advancedDrafts.delete(packageInfo.id);
      this.previewByPackage.delete(packageInfo.id);
      await this.refreshSnapshot();
    } catch (error) {
      this.toast(error.message, true);
    } finally {
      this.busyPackages.delete(packageInfo.id);
      this.rerenderWorkPackage(packageInfo.id);
    }
  }
}

export const workPackageExecutionInternals = Object.freeze({
  nextRoleIds,
  routingForRole,
  laneDetail,
  availabilityLabel,
});
