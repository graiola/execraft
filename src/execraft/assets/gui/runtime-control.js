import {
  elementById as $,
  escapeHtml as esc,
  syncSelectOptions,
} from "./ui-utils.js";

// Operator-facing vocabulary. The payload keeps the schema-v4 names; only the
// labels differ, so nothing here changes routing or topology semantics.
const LOCATION_AUTOMATIC = "Automatic";
function installRuntimeControlStyle() {
  if (document.getElementById("runtimeControlStyle")) return;
  const style = document.createElement("style");
  style.id = "runtimeControlStyle";
  style.textContent = `
    .execution-panel h4{margin:12px 0 6px}
    .execution-row{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin:8px 0}
    .execution-row-3{grid-template-columns:repeat(3,minmax(0,1fr))}
    .execution-options{display:flex;flex-wrap:wrap;gap:14px;align-items:center;margin:8px 0}
    .execution-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
    .execution-note{font-size:12px;color:var(--muted);margin:6px 0}
    .execution-status{font-size:12px;margin:6px 0}
    .execution-status.error{color:var(--danger)}
    .execution-output{max-height:220px;overflow:auto;white-space:pre-wrap;font-size:12px}
    .execution-health summary,.execution-advanced summary{cursor:pointer;display:flex;gap:10px;align-items:center}
    .execution-health-body,.execution-advanced-body{padding:8px 0}
    .execution-inventory{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}
    .execution-inventory-list{display:grid;gap:8px}
    .execution-inventory-row{border:1px solid var(--line);border-radius:9px;padding:9px;display:grid;gap:3px}
    .execution-inventory-row small{color:var(--muted)}
    .execution-setup-block{border:1px solid var(--line);border-radius:10px;padding:12px;margin-top:10px}
    .execution-setup-block.hidden{display:none}
    .execution-setup-block input,.execution-setup-block select{width:100%}
    .execution-readiness-summary{display:flex;justify-content:space-between;gap:12px;align-items:center;margin-bottom:8px}
    @media(max-width:900px){
      .execution-row,.execution-row-3,.execution-inventory{grid-template-columns:1fr}
    }
  `;
  document.head.append(style);
}

function optionRows(items, label) {
  return (items || []).map((item) => ({ value: item.id, label: label(item) }));
}

function jsonText(value) {
  return JSON.stringify(value, null, 2);
}

/** Human label for a model route: the model is what an operator recognises. */
function modelLabel(route) {
  if (!route) return "";
  return route.model ? `${route.model}` : route.id;
}

const DIAGNOSTIC_LAYER_KEYS = ["runtime", "model_route", "execution_target"];

/**
 * Summarize explicit diagnostics without guessing about omitted layers.
 *
 * The backend deliberately names the physical layer `execution_target`; keeping
 * the canonical API key here prevents target-only failures from being dropped by
 * the presentation layer and incorrectly reported as Ready.
 */
export function diagnosticsHealthy(result) {
  const reported = DIAGNOSTIC_LAYER_KEYS.map((name) => result?.[name]).filter(Boolean);
  if (!reported.length) return true;
  return reported.every((layer) => layer.healthy === true || layer.status === "healthy");
}

/**
 * Build the operator-facing model-route detail from the redacted topology API.
 *
 * `credential_configured` is intentionally only a boolean. Credential references
 * are control-plane configuration and must never be required by the browser just
 * to render whether authentication is configured.
 */
export function modelRouteInventoryDetail(route) {
  if (!route) return "";
  const extras = [
    route.provider_alias,
    route.api_family,
    route.credential_configured ? "credential configured" : "",
  ]
    .filter(Boolean)
    .join(" · ");
  return `${route.provider}/${route.model}${extras ? ` · ${extras}` : ""}`;
}

export class RuntimeControlView {
  constructor({ api, toast }) {
    this.api = api;
    this.toast = toast;
    this.snapshot = null;
    this.topology = null;
    this.projectId = "";
    this.loading = false;
    this.diagnostics = null;
    this.setup = null;
    this.setupPreviewSha = "";
    this.executionSetupPreviewSha = "";
    this.migrationPreviewSha = "";
    this.installPreviewed = false;
    installRuntimeControlStyle();
    this.#bind();
  }

  renderSnapshot(snapshot) {
    this.snapshot = snapshot;
    this.projectId = snapshot?.project?.id || snapshot?.focused_project_id || this.projectId || "";
    if (this.topology && this.topology.project_id === this.projectId) this.#render();
  }

  async load({ projectId = "", force = false, includeSetup = false } = {}) {
    const requested = projectId || this.projectId;
    if (!requested || this.loading) return;
    if (!force && this.topology?.project_id === requested) {
      this.#render();
      if (includeSetup) await this.#loadSetup();
      return;
    }
    this.loading = true;
    this.#status("Loading execution configuration…");
    try {
      this.topology = await this.api(`/api/runtime/topology?project_id=${encodeURIComponent(requested)}`);
      this.projectId = requested;
      this.#render();
      if (includeSetup) await this.#loadSetup();
    } catch (error) {
      this.#status(error.message, true);
      this.#health("warn", "Configuration error", error.message, { expand: true });
      const projectTarget = $("projectRuntimeTopologyView");
      if (projectTarget) projectTarget.textContent = error.message;
    } finally {
      this.loading = false;
    }
  }

  #bind() {
    $("runtimeTopologyRefresh")?.addEventListener("click", () => this.load({ force: true }));
    $("runtimeDiagnosticsRun")?.addEventListener("click", () => this.#diagnose());
    $("runtimeSetupRefresh")?.addEventListener("click", () => this.#loadSetup({ forceTopology: true }));
    $("runtimeMigrationPreview")?.addEventListener("click", () => this.#previewMigration());
    $("runtimeMigrationApply")?.addEventListener("click", () => this.#applyMigration());
    $("executionSetupPreview")?.addEventListener("click", () => this.#previewExecutionSetup());
    $("executionSetupApply")?.addEventListener("click", () => this.#applyExecutionSetup());
    $("openclawSetupPreview")?.addEventListener("click", () => this.#previewOpenClawSetup());
    $("openclawSetupApply")?.addEventListener("click", () => this.#applyOpenClawSetup());
    $("openclawInstallPreview")?.addEventListener("click", () => this.#previewOpenClawInstall());
    $("openclawInstallApply")?.addEventListener("click", () => this.#applyOpenClawInstall());
    $("openclawSetupDiagnose")?.addEventListener("click", () => this.#diagnoseOpenClawSetup());
    $("openclawMode")?.addEventListener("change", () => this.#syncOpenClawMode());
    $("openclawModelRoute")?.addEventListener("change", () => this.#syncOpenClawRouteMode());
    $("openclawAuthKind")?.addEventListener("change", () => this.#syncOpenClawAuth());
    $("openclawRuntimeId")?.addEventListener("change", () => this.#syncOpenClawAuth());
  }

  #status(message, error = false) {
    const node = $("runtimeTopologyStatus");
    if (!node) return;
    node.textContent = message;
    node.classList.toggle("error", error);
  }

  /** Compact health. Passive by design: no probe runs unless the operator asks. */
  #health(level, label, detail, { expand = false } = {}) {
    const pill = $("executionHealthPill");
    if (pill) {
      pill.textContent = label;
      pill.dataset.level = level;
    }
    const summary = $("executionHealthSummary");
    if (summary) summary.textContent = detail;
    const box = $("executionHealth");
    if (box && expand) box.open = true;
  }

  // ----------------------------------------------------------------- render

  #render() {
    const topology = this.topology;
    if (!topology) return;
    this.#renderChoices();
    this.#renderRunState();
    this.#renderInventory();
    this.#renderProjectInventory();
  }

  #lists() {
    const topology = this.topology || {};
    return {
      runtimes: topology.runtimes || [],
      routes: topology.model_routes || [],
      targets: topology.execution_targets || [],
      profiles: topology.profiles || [],
      lanes: topology.execution_lanes || [],
    };
  }

  #renderChoices() {
    const { runtimes, routes, targets } = this.#lists();
    const set = (id, items, fallbackValue = "") => {
      const node = $(id);
      if (node) syncSelectOptions(node, items, { value: node.value, fallbackValue });
    };
    const runtimeRows = optionRows(runtimes, (r) => `${r.id} · ${r.kind}`);
    const routeRows = optionRows(routes, (r) => `${r.id} · ${modelLabel(r)}`);
    const supportedTargets = targets.filter((target) => target.support?.supported !== false);
    const targetRows = optionRows(supportedTargets, (t) => `${t.id} · ${t.kind}`);

    set("runtimeDiagnosticsRuntime", [{ value: "", label: "No runtime probe" }, ...runtimeRows]);
    set("runtimeDiagnosticsModel", [{ value: "", label: "No model probe" }, ...routeRows]);
    set("runtimeDiagnosticsTarget", [{ value: "", label: "No location probe" }, ...targetRows]);
  }

  #renderRunState() {
    const { runtimes, routes, targets, profiles, lanes } = this.#lists();
    const run = this.topology?.run || {};
    const active = Boolean(run.owned_running || run.external_running);
    const warnings = this.topology?.warnings || [];

    this.#status(
      active
        ? `${lanes.length} execution lanes · orchestration active · routing changes live in the Work Package inspector.`
        : `${lanes.length} execution lanes · ${profiles.length} raw profiles · ${runtimes.length} runtimes · ${routes.length} models · ${targets.length} locations`,
    );

    if (!this.diagnostics) {
      if (warnings.length) {
        this.#health("warn", "Check configuration", warnings.join(" · "), { expand: true });
      } else {
        this.#health(
          "ok",
          "Ready",
          `${lanes.length} execution lanes · ${runtimes.length} runtimes · ${routes.length} models · ${targets.length} locations configured. Diagnostics not run.`,
        );
      }
    }
  }

  #renderInventory() {
    const { runtimes, routes, targets, profiles, lanes } = this.#lists();
    this.#renderRows("runtimeTopologyLanes", lanes, (lane) => [
      lane.display_name || lane.id,
      `${lane.runtime_kind || lane.runtime_id} · ${lane.model_display_name || lane.model_route_id || "direct model"} · ${lane.target_display_name || lane.target_id || "local/direct"} · ${(lane.roles || []).join(", ") || "no mapped roles"} · ${lane.availability || lane.health || "unknown"}`,
    ]);
    this.#renderRows("runtimeTopologyRuntimes", runtimes, (runtime) => {
      const openclaw = runtime.openclaw || null;
      const detail = openclaw
        ? `${openclaw.mode} · ${openclaw.gateway || "managed gateway"} · auth ${openclaw.authentication_configured ? "configured" : "not configured"}`
        : `${runtime.adapter || "native"} · ${runtime.binary || "default binary"}`;
      return [runtime.id, `${runtime.kind} · ${detail}`];
    });
    this.#renderRows("runtimeTopologyModels", routes, (route) => [
      route.id,
      modelRouteInventoryDetail(route),
    ]);
    this.#renderRows("runtimeTopologyTargets", targets, (target) => {
      const extras = [
        target.workspace_transport,
        target.max_concurrency ? `capacity ${target.max_concurrency}` : "",
        target.concurrency_group,
      ]
        .filter(Boolean)
        .join(" · ");
      const support = target.support?.supported === false ? ` · ${target.support.status}` : "";
      return [target.id, `${target.kind}${target.endpoint ? ` · ${target.endpoint}` : ""}${extras ? ` · ${extras}` : ""}${support}`];
    });
    this.#renderRows("runtimeTopologyProfiles", profiles, (profile) => [
      profile.id,
      `${profile.runtime_id} → ${profile.model_route_id || "direct"} → ${profile.target_id || "local/direct"} · ${(profile.capabilities || []).join(", ")}${profile.support?.supported === false ? ` · ${profile.support.status}` : ""}`,
    ]);
  }

  #renderProjectInventory() {
    const { runtimes, targets, lanes } = this.#lists();
    const node = $("projectRuntimeTopologyView");
    if (!node) return;
    node.innerHTML =
      `<div class="execution-inventory"><div><strong>Runtimes</strong><p>${runtimes.map((r) => esc(`${r.id} (${r.kind})`)).join(" · ") || "None"}</p></div>`
      + `<div><strong>Locations</strong><p>${targets.map((t) => esc(`${t.id} (${t.kind})`)).join(" · ") || "Local/direct"}</p></div></div>`
      + `<small>${lanes.length} execution lane${lanes.length === 1 ? "" : "s"} available. Open a Work Package's Execution tab to change routing. This inventory is passive and does not contact gateways or model endpoints.</small>`;
  }

  #renderRows(id, items, describe) {
    const node = $(id);
    if (!node) return;
    node.innerHTML = items.length
      ? items
          .map((item) => {
            const [title, detail] = describe(item);
            return `<div class="execution-inventory-row"><strong>${esc(title)}</strong><small>${esc(detail)}</small></div>`;
          })
          .join("")
      : '<div class="execution-inventory-row"><small>None configured.</small></div>';
  }

  async #loadSetup({ forceTopology = false } = {}) {
    if (!this.projectId) return;
    const status = $("runtimeSetupStatus");
    if (status) status.textContent = "Loading OpenClaw setup status…";
    try {
      if (forceTopology) {
        this.topology = await this.api(`/api/runtime/topology?project_id=${encodeURIComponent(this.projectId)}`);
        this.#render();
      }
      this.setup = await this.api(`/api/runtime/setup?project_id=${encodeURIComponent(this.projectId)}`);
      this.setupPreviewSha = "";
      this.executionSetupPreviewSha = "";
      this.migrationPreviewSha = "";
      this.installPreviewed = false;
      this.#renderSetup();
    } catch (error) {
      if (status) status.textContent = error.message;
      this.toast(error.message, true);
    }
  }

  #renderSetup() {
    const setup = this.setup || {};
    const host = setup.host || {};
    const profiles = setup.openclaw_profiles || [];
    const configuredRuntimes = setup.openclaw_runtimes || [];
    const status = $("runtimeSetupStatus");
    if (status) {
      const installation = host.installed
        ? `${host.installed_version || "unknown"}${host.version_matches_pin ? " · validated" : " · version mismatch"}`
        : "not installed on PATH";
      const runtimeSummary = configuredRuntimes.length
        ? configuredRuntimes.map((runtime) => `${runtime.id}: ${runtime.mode} · ${runtime.gateway || "gateway"} · auth ${runtime.authentication_configured ? "configured" : runtime.auth_kind === "none" ? "disabled" : "not configured"}`).join(" · ")
        : "no OpenClaw runtime configured";
      const profileSummary = profiles.length
        ? profiles.map((profile) => `${profile.id} → ${profile.model_route_id || "no model"}${profile.target_id ? ` → ${profile.target_id}` : ""}`).join(" · ")
        : "no OpenClaw profile configured";
      const securitySummary = configuredRuntimes.length
        ? configuredRuntimes.map((runtime) => {
            const security = runtime.security || {};
            if (runtime.mode === "external") {
              return `${runtime.id}: external Gateway policy · hard sandbox not claimed`;
            }
            const configured = security.configured_secure ? "hard sandbox configured" : "security policy incomplete";
            const evidence = security.live_enforcement_evidence === "not-verified-by-doctor"
              ? "live enforcement not yet verified"
              : security.live_enforcement_evidence || "live enforcement status unknown";
            return `${runtime.id}: ${configured} · ${evidence}`;
          }).join(" · ")
        : profiles.length ? "security posture unavailable until a runtime is configured" : "security configured with the runtime profile";
      status.classList.remove("empty");
      status.innerHTML = `<strong>OpenClaw host: ${esc(installation)}</strong><small>${esc(runtimeSummary)}</small><small>${esc(profileSummary)}</small><small>${esc(securitySummary)} · protocol ${esc(host.protocol_version || "unknown")} · live health runs only on explicit diagnostics</small>`;
    }

    const migration = setup.migration || {};
    $("runtimeMigrationPanel")?.classList.toggle("hidden", !migration.required);
    if ($("runtimeMigrationSummary")) {
      $("runtimeMigrationSummary").textContent = migration.required
        ? `Legacy schema v${migration.source_schema_version}; preview an explicit migration to schema v4 before configuring OpenClaw.`
        : `Execution configuration is schema v${setup.schema_version || 4}.`;
    }
    const setupBlockedByMigration = Boolean(migration.required);
    $("executionRouteSetupPanel")?.classList.toggle("hidden", setupBlockedByMigration);
    $("openclawSetupPanel")?.classList.toggle("hidden", setupBlockedByMigration);

    const { routes, targets, profiles: allProfiles } = this.#lists();
    syncSelectOptions(
      $("executionSetupProfile"),
      optionRows(allProfiles, (profile) => `${profile.id} · ${profile.runtime_kind || "runtime"}`),
      { value: $("executionSetupProfile")?.value || allProfiles[0]?.id || "", fallbackValue: allProfiles[0]?.id || "" },
    );
    const routeOptions = optionRows(routes, (route) => `${route.id} · ${modelLabel(route)}`);
    syncSelectOptions(
      $("openclawModelRoute"),
      [...routeOptions, { value: "__new__", label: "Add a new local/self-hosted model…" }],
      { value: $("openclawModelRoute")?.value || routeOptions[0]?.value || "__new__", fallbackValue: routeOptions[0]?.value || "__new__" },
    );
    const supportedTargets = targets.filter((target) => target.support?.supported !== false);
    syncSelectOptions(
      $("openclawTarget"),
      [{ value: "", label: "Use model route default" }, ...optionRows(supportedTargets, (target) => `${target.id} · ${target.kind}`)],
      { value: $("openclawTarget")?.value || "", fallbackValue: "" },
    );

    const configuredRuntime = (this.topology?.runtimes || []).find((runtime) => runtime.kind === "openclaw");
    const configuredSetupRuntime = configuredRuntime
      ? configuredRuntimes.find((runtime) => runtime.id === configuredRuntime.id)
      : null;
    if (configuredRuntime?.openclaw) {
      if ($("openclawRuntimeId")) $("openclawRuntimeId").value = configuredRuntime.id;
      if ($("openclawMode")) $("openclawMode").value = configuredRuntime.openclaw.mode || "managed";
      if ($("openclawGateway")) $("openclawGateway").value = configuredRuntime.openclaw.gateway || "ws://127.0.0.1:18789";
      if ($("openclawAuthKind") && configuredSetupRuntime?.auth_kind) {
        $("openclawAuthKind").value = configuredSetupRuntime.auth_kind;
      }
      const authField = $("openclawAuthRef");
      if (authField && configuredSetupRuntime?.authentication_configured) {
        // Credential references stay server-side. Blank means "preserve the
        // existing reference" when editing a configured runtime.
        authField.value = "";
        authField.placeholder = "Configured reference will be preserved";
      }
    }
    const configuredProfile = profiles[0];
    if (configuredProfile) {
      if ($("openclawProfileId")) $("openclawProfileId").value = configuredProfile.id;
      if ($("openclawModelRoute") && configuredProfile.model_route_id) $("openclawModelRoute").value = configuredProfile.model_route_id;
      if ($("openclawTarget") && configuredProfile.target_id) $("openclawTarget").value = configuredProfile.target_id;
    }
    if ($("openclawInstallVersion") && !$("openclawInstallVersion").value) {
      $("openclawInstallVersion").placeholder = host.pinned_version || "validated pinned version";
    }
    this.#syncOpenClawMode();
    this.#syncOpenClawRouteMode();
    this.#syncOpenClawAuth();
  }

  #syncOpenClawMode() {
    const external = $("openclawMode")?.value === "external";
    const installPanel = $("openclawInstallPanel");
    if (installPanel) installPanel.classList.toggle("hidden", external);
  }

  #syncOpenClawRouteMode() {
    const adding = $("openclawModelRoute")?.value === "__new__";
    const quick = $("openclawQuickModel");
    if (quick) quick.open = adding;
    if (!adding) this.#inferOpenClawTarget();
  }

  #inferOpenClawTarget() {
    const targetSelect = $("openclawTarget");
    if (!targetSelect || targetSelect.value) return;
    const { routes, targets } = this.#lists();
    const route = routes.find((item) => item.id === $("openclawModelRoute")?.value);
    if (!route || route.default_target) return; // the blank choice already means route default
    const candidates = targets.filter((target) => {
      if (target.kind !== "local") return false;
      if (!route.endpoint) return true;
      return (target.endpoint || "").replace(/\/$/, "") === route.endpoint.replace(/\/$/, "");
    });
    if (candidates.length === 1) targetSelect.value = candidates[0].id;
  }

  #syncOpenClawAuth() {
    const field = $("openclawAuthRef");
    if (!field) return;
    const kind = $("openclawAuthKind")?.value || "token";
    const none = kind === "none";
    field.disabled = none;
    if (none) {
      field.value = "";
      field.placeholder = "Authentication disabled";
      return;
    }
    const runtimeId = ($("openclawRuntimeId")?.value || "").trim();
    const existing = (this.setup?.openclaw_runtimes || []).find((runtime) => runtime.id === runtimeId);
    if (existing?.authentication_configured && existing.auth_kind === kind) {
      field.placeholder = "Configured reference will be preserved";
      return;
    }
    field.placeholder = "env:OPENCLAW_GATEWAY_TOKEN";
    if (!field.value) field.value = "env:OPENCLAW_GATEWAY_TOKEN";
  }

  #executionSetupPayload() {
    return {
      project_id: this.projectId,
      profile_id: $("executionSetupProfile")?.value || "",
      route_id: ($("executionSetupRouteId")?.value || "").trim(),
      provider: ($("executionSetupProvider")?.value || "").trim(),
      model: ($("executionSetupModel")?.value || "").trim(),
      target_id: ($("executionSetupTargetId")?.value || "").trim(),
      target_kind: $("executionSetupTargetKind")?.value || "local",
      endpoint: ($("executionSetupEndpoint")?.value || "").trim(),
      credential_ref: ($("executionSetupCredentialRef")?.value || "").trim(),
      api_family: ($("executionSetupApiFamily")?.value || "").trim(),
    };
  }

  #openClawCapabilities() {
    const preset = $("openclawCapabilityPreset")?.value || "coding";
    if (preset === "review") return ["review", "fix_review"];
    if (preset === "implementation") return ["implement"];
    return ["implement", "review", "fix_review"];
  }

  #openClawPayload() {
    const selected = $("openclawModelRoute")?.value || "";
    const adding = selected === "__new__";
    const routeId = adding ? ($("openclawNewRouteId")?.value || "").trim() : selected;
    const targetId = adding
      ? ($("openclawNewTargetId")?.value || "").trim()
      : ($("openclawTarget")?.value || "").trim();
    return {
      project_id: this.projectId,
      runtime_id: ($("openclawRuntimeId")?.value || "openclaw-local").trim(),
      profile_id: ($("openclawProfileId")?.value || "openclaw-local").trim(),
      mode: $("openclawMode")?.value || "managed",
      gateway: ($("openclawGateway")?.value || "").trim(),
      executable: "openclaw",
      auth_kind: $("openclawAuthKind")?.value || "token",
      auth_ref: ($("openclawAuthRef")?.value || "").trim(),
      model_route_id: routeId,
      target_id: targetId,
      capabilities: this.#openClawCapabilities(),
      priority: 50,
      max_complexity: 70,
      route_id: adding ? routeId : "",
      provider: adding ? ($("openclawNewProvider")?.value || "").trim() : "",
      model: adding ? ($("openclawNewModel")?.value || "").trim() : "",
      endpoint: adding ? ($("openclawNewEndpoint")?.value || "").trim() : "",
      target_kind: adding ? ($("openclawNewTargetKind")?.value || "local") : "local",
    };
  }

  async #previewExecutionSetup() {
    const output = $("executionSetupDiff");
    try {
      const result = await this.api("/api/runtime/execution/setup/preview", {
        method: "POST",
        body: JSON.stringify(this.#executionSetupPayload()),
      });
      this.executionSetupPreviewSha = result.source_sha256 || "";
      if (output) {
        output.textContent = result.diff || "No model-route changes are required.";
        output.classList.remove("hidden");
      }
      if ($("executionSetupApply")) $("executionSetupApply").disabled = !result.changed;
    } catch (error) {
      this.executionSetupPreviewSha = "";
      if (output) { output.textContent = error.message; output.classList.remove("hidden"); }
      this.toast(error.message, true);
    }
  }

  async #applyExecutionSetup() {
    if (!this.executionSetupPreviewSha) return this.toast("Preview the model-route configuration first", true);
    if (!confirm("Apply the reviewed model-route/location configuration? A timestamped backup will be created.")) return;
    try {
      await this.api("/api/runtime/execution/setup/apply", {
        method: "POST",
        body: JSON.stringify({ ...this.#executionSetupPayload(), expected_sha256: this.executionSetupPreviewSha, acknowledged: true }),
      });
      this.toast("Execution model route updated");
      await this.load({ projectId: this.projectId, force: true, includeSetup: true });
    } catch (error) {
      this.toast(error.message, true);
    }
  }

  async #previewMigration() {
    const output = $("runtimeMigrationDiff");
    try {
      const result = await this.api("/api/runtime/migration/preview", {
        method: "POST",
        body: JSON.stringify({ project_id: this.projectId }),
      });
      this.migrationPreviewSha = result.source_sha256 || "";
      if (output) {
        output.textContent = result.diff || "Configuration is already schema v4; no migration changes required.";
        output.classList.remove("hidden");
      }
      if ($("runtimeMigrationApply")) $("runtimeMigrationApply").disabled = !result.changed;
    } catch (error) {
      if (output) { output.textContent = error.message; output.classList.remove("hidden"); }
      this.toast(error.message, true);
    }
  }

  async #applyMigration() {
    if (!this.migrationPreviewSha) return this.toast("Preview the migration first", true);
    if (!confirm("Apply the reviewed schema-v4 migration? A timestamped backup of agents.yaml will be created.")) return;
    try {
      const result = await this.api("/api/runtime/migration/apply", {
        method: "POST",
        body: JSON.stringify({ project_id: this.projectId, expected_sha256: this.migrationPreviewSha, acknowledged: true }),
      });
      const backup = result.backup ? ` Backup: ${result.backup}` : "";
      const output = $("runtimeMigrationDiff");
      if (output) { output.textContent = `Migration applied.${backup}`; output.classList.remove("hidden"); }
      this.toast(`Execution configuration migrated to schema v4.${backup}`);
      await this.load({ projectId: this.projectId, force: true, includeSetup: true });
    } catch (error) {
      this.toast(error.message, true);
    }
  }

  async #previewOpenClawSetup() {
    const output = $("openclawSetupDiff");
    try {
      const result = await this.api("/api/runtime/openclaw/setup/preview", {
        method: "POST",
        body: JSON.stringify(this.#openClawPayload()),
      });
      this.setupPreviewSha = result.source_sha256 || "";
      if (output) {
        output.textContent = result.diff || "No configuration changes are required.";
        output.classList.remove("hidden");
      }
      if ($("openclawSetupApply")) $("openclawSetupApply").disabled = !result.changed;
    } catch (error) {
      this.setupPreviewSha = "";
      if (output) { output.textContent = error.message; output.classList.remove("hidden"); }
      this.toast(error.message, true);
    }
  }

  async #applyOpenClawSetup() {
    if (!this.setupPreviewSha) return this.toast("Preview the OpenClaw configuration first", true);
    if (!confirm("Apply the reviewed OpenClaw runtime/profile configuration? A timestamped backup will be created.")) return;
    try {
      await this.api("/api/runtime/openclaw/setup/apply", {
        method: "POST",
        body: JSON.stringify({ ...this.#openClawPayload(), expected_sha256: this.setupPreviewSha, acknowledged: true }),
      });
      this.toast("OpenClaw execution configuration updated");
      await this.load({ projectId: this.projectId, force: true, includeSetup: true });
    } catch (error) {
      this.toast(error.message, true);
    }
  }

  async #diagnoseOpenClawSetup() {
    const output = $("openclawSetupDiagnostics");
    const payload = this.#openClawPayload();
    try {
      if (output) { output.textContent = "Running explicit runtime/model/location diagnostics…"; output.classList.remove("hidden"); }
      const result = await this.#runDiagnostics({
        runtime_id: payload.runtime_id,
        model_route_id: payload.model_route_id,
        target_id: payload.target_id,
      });
      if (output) output.textContent = jsonText(result);
    } catch (error) {
      if (output) { output.textContent = error.message; output.classList.remove("hidden"); }
      this.toast(error.message, true);
    }
  }

  async #previewOpenClawInstall() {
    const output = $("openclawInstallOutput");
    try {
      const result = await this.api("/api/runtime/openclaw/install/preview", {
        method: "POST",
        body: JSON.stringify({ prefix: $("openclawInstallPrefix")?.value || "~/.local", version: $("openclawInstallVersion")?.value || "" }),
      });
      this.installPreviewed = true;
      if (output) {
        output.textContent = `Command (no shell):\n${(result.command || []).join(" ")}\n\nCurrent host:\n${jsonText(result.host || {})}`;
        output.classList.remove("hidden");
      }
      if ($("openclawInstallApply")) $("openclawInstallApply").disabled = false;
    } catch (error) {
      this.installPreviewed = false;
      if (output) { output.textContent = error.message; output.classList.remove("hidden"); }
      this.toast(error.message, true);
    }
  }

  async #applyOpenClawInstall() {
    if (!this.installPreviewed) return this.toast("Preview the install command first", true);
    if (!confirm("Install the validated OpenClaw npm package into this user-owned prefix?")) return;
    const output = $("openclawInstallOutput");
    try {
      const result = await this.api("/api/runtime/openclaw/install/apply", {
        method: "POST",
        body: JSON.stringify({ prefix: $("openclawInstallPrefix")?.value || "~/.local", version: $("openclawInstallVersion")?.value || "", acknowledged: true }),
      });
      if (output) output.textContent = jsonText(result);
      this.toast("OpenClaw installation completed");
      await this.#loadSetup();
    } catch (error) {
      if (output) output.textContent = error.message;
      this.toast(error.message, true);
    }
  }

  async #diagnose() {
    const output = $("runtimeDiagnosticsOutput");
    output.textContent = "Running explicit diagnostics…";
    try {
      const result = await this.#runDiagnostics({
        runtime_id: $("runtimeDiagnosticsRuntime")?.value || "",
        model_route_id: $("runtimeDiagnosticsModel")?.value || "",
        target_id: $("runtimeDiagnosticsTarget")?.value || "",
      });
      output.textContent = jsonText(result);
      this.diagnostics = result;
      const healthy = diagnosticsHealthy(result);
      this.#health(
        healthy ? "ok" : "warn",
        healthy ? "Ready" : "Attention",
        healthy ? "Diagnostics passed." : "One or more layers reported a problem.",
        { expand: !healthy },
      );
    } catch (error) {
      output.textContent = error.message;
      this.diagnostics = null;
      this.#health("warn", "Attention", error.message, { expand: true });
      this.toast(error.message, true);
    }
  }

  async #runDiagnostics(payload) {
    return this.api("/api/runtime/diagnostics", {
      method: "POST",
      body: JSON.stringify({ project_id: this.projectId, ...payload }),
    });
  }
}
