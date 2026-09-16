import {
  elementById as $,
  escapeHtml as esc,
  syncSelectOptions,
} from "./ui-utils.js";

function humanize(value, fallback = "—") {
  const text = String(value || "").trim();
  return text ? text.replaceAll("_", " ") : fallback;
}

function mappingLines(raw, label = "mapping") {
  const result = {};
  for (const source of String(raw || "").split(/\r?\n/)) {
    const line = source.trim();
    if (!line) continue;
    const separator = line.indexOf("=");
    if (separator <= 0 || separator === line.length - 1)
      throw new Error(`Invalid ${label} ${JSON.stringify(line)}; use KEY=VALUE`);
    const oldId = line.slice(0, separator).trim();
    const newId = line.slice(separator + 1).trim();
    if (!oldId || !newId) throw new Error(`Invalid ${label} ${JSON.stringify(line)}`);
    if (Object.prototype.hasOwnProperty.call(result, oldId))
      throw new Error(`Duplicate ${label} key ${oldId}`);
    result[oldId] = newId;
  }
  return result;
}

function badge(label, kind = "") {
  return `<span class="pill ${esc(kind)}">${esc(label)}</span>`;
}

export class TaskLifecycleView {
  constructor({ api, toast, refreshDashboard, download = null }) {
    this.api = api;
    this.toast = toast;
    this.refreshDashboard = refreshDashboard;
    this.download = download;
    this.lifecycle = null;
    this.acceptedDocuments = {};
    this.candidate = null;
    this.diffName = "";
    this.loading = false;
    this.contextKey = "";
    this.loadEpoch = 0;
    this.providerSelectionInitialized = false;
    this.bind();
  }

  setContext(projectId, taskId) {
    const key = projectId && taskId ? `${projectId}/${taskId}` : "";
    if (key === this.contextKey) return false;
    this.contextKey = key;
    this.loadEpoch += 1;
    this.loading = false;
    this.lifecycle = null;
    this.acceptedDocuments = {};
    this.candidate = null;
    this.diffName = "";
    this.providerSelectionInitialized = false;
    this.renderDocuments();
    this.renderCandidate(null);
    this.renderCandidateList();
    this.message(key ? `Loading task definition for ${key}…` : "No task selected.");
    return true;
  }

  setProviders(agents = []) {
    const select = $("generatePlanProviderInput");
    if (!select) return;
    const usable = agents
      .filter((agent) => {
        if (!agent.enabled) return false;
        if (!(agent.capabilities || []).some((item) => ["plan", "decompose"].includes(item))) return false;
        if (!["available", "probe_due"].includes(agent.health?.status || "available")) return false;
        const endpoint = agent.endpoint || {};
        return !endpoint.url || endpoint.reachable === true;
      })
      .sort((left, right) => {
        if (left.id === "codex") return -1;
        if (right.id === "codex") return 1;
        return Number(right.priority || 0) - Number(left.priority || 0) || left.id.localeCompare(right.id);
      });
    const options = usable.map((agent) => ({
      value: agent.id,
      label: `${agent.id}${agent.model ? " · " + agent.model : ""}`,
    }));
    const values = new Set(options.map((item) => item.value));
    const preferred = values.has("codex") ? "codex" : options[0]?.value || "";
    const selected = this.providerSelectionInitialized && values.has(select.value)
      ? select.value
      : preferred;
    syncSelectOptions(select, options.length ? options : [{ value: "", label: "No planning agent available" }], {
      value: selected,
      fallbackValue: preferred,
      disabled: !options.length,
    });
    this.providerSelectionInitialized = true;
  }

  bind() {
    $("taskLifecycleRefreshBtn")?.addEventListener("click", () => this.load({ force: true }));
    $("taskExportPdfBtn")?.addEventListener("click", () => this.exportTask("pdf"));
    $("taskExportSvgBtn")?.addEventListener("click", () => this.exportTask("svg"));
    $("generatePlanBtn")?.addEventListener("click", () => this.generatePlan());
    $("stageReplanBtn")?.addEventListener("click", () => this.stageCandidate());
    $("adoptCurrentDefinitionBtn")?.addEventListener("click", () => this.adoptCurrentFiles());
    $("loadReplanCandidateBtn")?.addEventListener("click", () => this.loadSelectedCandidate());
    $("replanCandidateSelect")?.addEventListener("change", () => this.updateCandidateControls());
    $("applyReplanBtn")?.addEventListener("click", () => this.applyCandidate());
    $("recoverReplanBtn")?.addEventListener("click", () => this.recoverReplan());
    $("finalSyncBtn")?.addEventListener("click", () => this.finalSync());
    $("completeTaskBtn")?.addEventListener("click", () => this.completeTask());
    $("refreshRepositorySyncBtn")?.addEventListener("click", () => this.refreshRepositorySync());
    $("stageRepositorySyncBtn")?.addEventListener("click", () => this.stageRepositorySync());
    document.querySelectorAll(".definition-doc-tab").forEach((button) => {
      button.addEventListener("click", () => this.selectDocument(button.dataset.definitionDocument));
    });
    $("candidateDiffTabs")?.addEventListener("click", (event) => {
      const button = event.target.closest("[data-candidate-diff]");
      if (!button) return;
      this.diffName = button.dataset.candidateDiff || "";
      this.renderCandidateDiff();
    });
  }

  async exportTask(format) {
    if (!this.contextKey || !this.download) return;
    const [projectId, taskId] = this.contextKey.split("/", 2);
    try {
      const filename = await this.download(`/api/export/task?project_id=${encodeURIComponent(projectId)}&task_id=${encodeURIComponent(taskId)}&format=${encodeURIComponent(format)}&theme=dark`);
      this.toast(`Exported ${filename}`);
    } catch (error) {
      this.toast(error.message, true);
    }
  }

  renderSummary(summary = {}) {
    const definition = summary.definition || {};
    const generation = summary.plan_generation || {};
    const completion = summary.completion || {};
    const workspace = summary.workspace || {};
    if ($("taskLifecycleStatus")) {
      $("taskLifecycleStatus").textContent = humanize(summary.task_status);
      $("taskLifecycleStatusDetail").textContent = summary.replan_transaction?.present
        ? `Replan transaction · ${humanize(summary.replan_transaction.status, "pending")}`
        : "No replan transaction";
      $("definitionRevision").textContent = definition.revision ? `r${String(definition.revision).padStart(4, "0")}` : "—";
      $("definitionIntegrity").textContent = definition.integrity_ok && definition.executable
        ? "Accepted executable definition"
        : definition.integrity_ok
          ? "Plan generation required"
        : definition.reason || `${(definition.changed_documents || []).length} drifted document(s)`;
      $("completionStatus").textContent = humanize(completion.status, "not started");
      $("completionDetail").textContent = completion.phase
        ? `Phase ${humanize(completion.phase)}`
        : "No completion transaction";
      $("lifecycleWorkspaceStatus").textContent = humanize(workspace.status, "unavailable");
      $("lifecycleWorkspaceDetail").textContent = workspace.available
        ? `${humanize(workspace.runtime_status, "runtime unknown")} · ${workspace.repositories?.length || 0} repositories`
        : workspace.reason || "No registered workspace";
    }
    this.renderPlanGeneration(generation, definition);
  }

  renderPlanGeneration(generation = {}, definition = {}) {
    const status = generation.status || "idle";
    const target = $("planGenerationStatus");
    const button = $("generatePlanBtn");
    if (!target || !button) return;
    button.textContent = status === "running" ? "Generating…" : "Generate plan";
    button.disabled = status === "running" || $("generatePlanProviderInput")?.disabled;
    target.className = `plan-generation-status ${status === "candidate_ready" ? "ready" : status}`;
    if (status === "running") {
      target.textContent = `Generation in progress with ${generation.provider_id || "automatic agent selection"}. No accepted files change until a candidate is ready and applied.`;
      this.message(target.textContent);
    } else if (status === "candidate_ready") {
      target.textContent = `Candidate ${generation.candidate_id || ""} is ready. Review the definition diff, then click Apply candidate.`;
      this.message(target.textContent);
    } else if (status === "failed") {
      target.textContent = `Generation failed: ${generation.error || "unknown planning-agent error"}`;
      this.message(target.textContent, true);
    } else if (!definition.executable) {
      const missing = (definition.execution_missing_documents || []).join(", ") || "PLAN.md / PLAN.graph.yaml";
      target.textContent = `Accepted revision is not executable yet. Missing: ${missing}. Generate a candidate to continue.`;
    } else {
      target.textContent = status === "applied" ? "Generated plan is accepted and executable." : "Accepted plan and graph are executable.";
    }
  }

  async load({ force = false } = {}) {
    if (this.loading) return;
    if (force && this.editorDirty() && !confirm("Discard unsaved task-definition editor changes and reload the accepted revision?")) return;
    const epoch = this.loadEpoch;
    this.loading = true;
    this.message("Loading task lifecycle…");
    try {
      const lifecycle = await this.api("/api/task/lifecycle");
      if (epoch !== this.loadEpoch) return;
      this.lifecycle = lifecycle;
      this.acceptedDocuments = { ...(lifecycle.documents || {}) };
      this.candidate = null;
      this.diffName = "";
      this.renderLifecycle();
    } catch (error) {
      if (epoch !== this.loadEpoch) return;
      this.message(error.message, true);
      this.toast(error.message, true);
    } finally {
      if (epoch === this.loadEpoch) this.loading = false;
    }
  }

  renderLifecycle() {
    const data = this.lifecycle || {};
    this.renderSummary(data);
    this.renderDocuments();
    this.renderCandidateList();
    this.renderCompletion();
    this.renderRepositorySync(data.repository_sync_detail || {});
    this.renderWorkspaceOwnership();
    this.renderHistory();
    const tx = data.replan_transaction || {};
    $("recoverReplanBtn").disabled = !tx.present || !tx.recoverable;
    const definition = data.definition || {};
    const issues = [];
    if (data.manifest_error) issues.push(`Task manifest: ${data.manifest_error}`);
    if (data.definition_error) issues.push(data.definition_error);
    if (!definition.integrity_ok) {
      if ((definition.changed_documents || []).length)
        issues.push(`Definition drift: ${(definition.changed_documents || []).join(", ")}`);
      if ((definition.missing_documents || []).length)
        issues.push(`Missing documents: ${(definition.missing_documents || []).join(", ")}`);
      if ((definition.unexpected_documents || []).length)
        issues.push(`Unexpected documents: ${(definition.unexpected_documents || []).join(", ")}`);
      if (definition.reason) issues.push(definition.reason);
    }
    if (tx.present && tx.recoverable)
      issues.push(`Interrupted replan ${tx.candidate_id || "transaction"} requires recovery before execution.`);
    const generation = data.plan_generation || {};
    if (issues.length) this.message(issues.join(" · "), true);
    else if (generation.status === "failed") this.message(`Generation failed: ${generation.error || "unknown planning-agent error"}`, true);
    else if (generation.status === "running") this.message(`Plan generation is running with ${generation.provider_id || "automatic agent selection"}.`);
    else if (generation.status === "candidate_ready") this.message(`Generated candidate ${generation.candidate_id || ""} is ready for review and application.`);
    else if (!definition.executable) this.message(`Accepted revision ${definition.revision || 1} matches its provenance but is not executable. Generate PLAN.md and PLAN.graph.yaml to continue.`, true);
    else this.message(`Accepted executable definition revision ${definition.revision || 1} is coherent. Edits are staged as a candidate before publication.`);
  }

  renderDocuments() {
    const documents = this.lifecycle?.documents || {};
    $("taskBriefEditor").value = documents["BRIEF.md"] || "";
    $("taskPlanEditor").value = documents["PLAN.md"] || "";
    $("taskPlanGraphEditor").value = documents["PLAN.graph.yaml"] || "";
  }

  selectDocument(name) {
    const ids = { brief: "taskBriefEditor", plan: "taskPlanEditor", graph: "taskPlanGraphEditor" };
    for (const [key, id] of Object.entries(ids)) $(id).classList.toggle("hidden", key !== name);
    document.querySelectorAll(".definition-doc-tab").forEach((button) => button.classList.toggle("active", button.dataset.definitionDocument === name));
  }

  editorDirty() {
    if (!this.lifecycle) return false;
    return (
      $("taskBriefEditor").value !== (this.acceptedDocuments["BRIEF.md"] || "") ||
      $("taskPlanEditor").value !== (this.acceptedDocuments["PLAN.md"] || "") ||
      $("taskPlanGraphEditor").value !== (this.acceptedDocuments["PLAN.graph.yaml"] || "")
    );
  }

  candidatePayload({ fromCurrentFiles = false } = {}) {
    const changed = (name, value) => value !== (this.acceptedDocuments[name] || "") ? value : "";
    return {
      requested_change: $("replanRequestInput").value.trim(),
      brief_markdown: fromCurrentFiles ? "" : changed("BRIEF.md", $("taskBriefEditor").value),
      plan_markdown: fromCurrentFiles ? "" : changed("PLAN.md", $("taskPlanEditor").value),
      plan_graph_yaml: fromCurrentFiles ? "" : changed("PLAN.graph.yaml", $("taskPlanGraphEditor").value),
      package_mapping: mappingLines($("replanSupersedeInput").value),
      provider_id: $("replanProviderInput").value.trim(),
      from_current_files: fromCurrentFiles,
      allow_structural_consistency: $("replanStructuralOnlyInput").checked,
    };
  }

  async stageCandidate() {
    try {
      const payload = this.candidatePayload();
      if (!payload.requested_change && !payload.brief_markdown && !payload.plan_markdown && !payload.plan_graph_yaml)
        throw new Error("Edit BRIEF.md/PLAN.md/PLAN.graph.yaml or provide a change request first.");
      await this.createCandidate(payload);
    } catch (error) {
      this.toast(error.message, true);
      this.message(error.message, true);
    }
  }

  async generatePlan() {
    if (this.editorDirty()) {
      this.toast("Apply or discard advanced editor changes before generating from the accepted brief.", true);
      return;
    }
    this.setBusy(true);
    this.message("Generating PLAN.md and PLAN.graph.yaml from the accepted brief…");
    try {
      const candidate = await this.api("/api/task/replan/generate", {
        method: "POST",
        body: JSON.stringify({ provider_id: $("generatePlanProviderInput").value.trim() }),
      });
      this.candidate = candidate;
      this.toast(`Generated ${candidate.candidate_id}; review it before applying`);
      await this.reloadAfterMutation({ preserveCandidate: candidate });
      this.renderCandidate(candidate);
    } catch (error) {
      this.toast(error.message, true);
      this.message(error.message, true);
    } finally {
      this.setBusy(false);
    }
  }

  async adoptCurrentFiles() {
    if (!confirm("Stage the BRIEF.md/PLAN.md/PLAN.graph.yaml currently on disk as an explicit replanning candidate? This is the recovery path for manual out-of-band edits.")) return;
    try {
      await this.createCandidate(this.candidatePayload({ fromCurrentFiles: true }));
    } catch (error) {
      this.toast(error.message, true);
      this.message(error.message, true);
    }
  }

  async createCandidate(payload) {
    this.setBusy(true);
    this.message("Running replanning consistency and deterministic impact analysis…");
    try {
      const candidate = await this.api("/api/task/replan/candidate", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      this.candidate = candidate;
      this.toast(`Staged ${candidate.candidate_id}`);
      await this.reloadAfterMutation({ preserveCandidate: candidate });
      this.renderCandidate(candidate);
    } finally {
      this.setBusy(false);
    }
  }

  renderCandidateList() {
    const select = $("replanCandidateSelect");
    const rows = this.lifecycle?.pending_candidates || [];
    const options = rows.length
      ? [
          { value: "", label: "Select pending candidate" },
          ...rows.map((item) => ({
            value: item.candidate_id,
            label: `${item.candidate_id}${item.invalid ? " · invalid" : item.impact?.applicable ? " · applicable" : " · blocked"}`,
          })),
        ]
      : [{ value: "", label: "No pending candidates" }];
    syncSelectOptions(select, options, {
      value:
        this.candidate?.candidate_id &&
        rows.some((row) => row.candidate_id === this.candidate.candidate_id)
          ? this.candidate.candidate_id
          : select.value,
      fallbackValue: "",
    });
    this.updateCandidateControls();
  }

  updateCandidateControls() {
    const selected = $("replanCandidateSelect").value;
    $("loadReplanCandidateBtn").disabled = !selected;
  }

  async loadSelectedCandidate() {
    const id = $("replanCandidateSelect").value;
    if (!id) return;
    try {
      const candidate = await this.api(`/api/task/replan/candidate?candidate_id=${encodeURIComponent(id)}`);
      this.candidate = candidate;
      this.renderCandidate(candidate);
    } catch (error) {
      this.toast(error.message, true);
      this.message(error.message, true);
    }
  }

  renderCandidate(candidate) {
    const impact = candidate?.impact || {};
    $("candidateMeta").textContent = candidate
      ? `${candidate.candidate_id} · revision ${candidate.revision} · ${humanize(candidate.consistency_mode)}${candidate.provider_id ? " · " + candidate.provider_id : ""}`
      : "No candidate selected.";
    if (!candidate) {
      $("replanImpact").className = "replan-impact empty";
      $("replanImpact").textContent = "Stage or select a candidate to inspect package reconciliation.";
      $("applyReplanBtn").disabled = true;
      this.renderCandidateDiffTabs([]);
      return;
    }
    const packages = impact.packages || [];
    const summary = [
      badge(impact.applicable ? "applicable" : "blocked", impact.applicable ? "ok" : "bad"),
      badge(`${impact.added_packages?.length || 0} added`),
      badge(`${impact.removed_packages?.length || 0} removed`),
      badge(`${impact.completed_packages?.length || 0} completed preserved`),
      badge(`${impact.active_packages?.length || 0} active`),
    ].join("");
    const findings = [
      ...(impact.blockers || []).map((text) => `<div class="finding error">${esc(text)}</div>`),
      ...(impact.warnings || []).map((text) => `<div class="finding warning">${esc(text)}</div>`),
    ].join("");
    const packageRows = packages.map((item) => {
      const changed = item.changed_fields?.length || item.classification !== "unchanged";
      const cls = item.classification.includes("blocked") ? "blocked" : changed ? "changed" : "";
      return `<article class="impact-package ${cls}"><strong>${esc(item.package_id)} · ${esc(humanize(item.classification))}${item.replacement_id ? ` → ${esc(item.replacement_id)}` : ""}</strong><small>${esc(item.summary || "")}</small>${item.changed_fields?.length ? `<small>Changed: ${item.changed_fields.map(esc).join(", ")}</small>` : ""}</article>`;
    }).join("");
    $("replanImpact").className = "replan-impact";
    $("replanImpact").innerHTML = `<div class="impact-summary">${summary}</div>${findings ? `<div class="lifecycle-warning-list">${findings}</div>` : ""}${packageRows || '<div class="empty">No package-level changes.</div>'}`;
    $("replanApplyMessage").className = `message ${impact.applicable ? "ok" : "bad"}`;
    $("replanApplyMessage").textContent = impact.applicable
      ? "Candidate passed deterministic impact analysis. Application still requires quiescent execution, clean superseded workspaces, and transaction locks."
      : "Candidate cannot be applied until every blocker is resolved and a new candidate is staged.";
    $("applyReplanBtn").disabled = !impact.applicable;
    this.renderCandidateDiffTabs(candidate.diffs || []);
  }

  renderCandidateDiffTabs(diffs) {
    const changed = diffs.filter((item) => item.text);
    if (!this.diffName || !diffs.some((item) => item.name === this.diffName))
      this.diffName = (changed[0] || diffs[0] || {}).name || "";
    $("candidateDiffTabs").innerHTML = diffs.map((item) => `<button class="btn small definition-doc-tab ${item.name === this.diffName ? "active" : ""}" data-candidate-diff="${esc(item.name)}">${esc(item.name)}${item.text ? " *" : ""}</button>`).join("");
    this.renderCandidateDiff();
  }

  renderCandidateDiff() {
    const diff = (this.candidate?.diffs || []).find((item) => item.name === this.diffName);
    $("candidateDiffTabs").querySelectorAll("[data-candidate-diff]").forEach((button) => button.classList.toggle("active", button.dataset.candidateDiff === this.diffName));
    $("candidateDiff").textContent = diff?.text || (diff ? `No changes in ${diff.name}.` : "No candidate diff loaded.");
  }

  async applyCandidate() {
    const id = this.candidate?.candidate_id;
    if (!id || !this.candidate?.impact?.applicable) return;
    if (!confirm(`Apply ${id} as the accepted task definition? Completed package history remains immutable and changed active packages must satisfy the supersession safety checks.`)) return;
    this.setBusy(true);
    try {
      const result = await this.api("/api/task/replan/apply", {
        method: "POST",
        body: JSON.stringify({ candidate_id: id }),
      });
      this.toast(`Applied task definition revision ${result.result?.revision || ""}`);
      this.lifecycle = result.lifecycle;
      this.acceptedDocuments = { ...(result.lifecycle?.documents || {}) };
      this.candidate = null;
      $("replanRequestInput").value = "";
      $("replanSupersedeInput").value = "";
      this.renderLifecycle();
      await this.refreshDashboard({ reportError: false });
    } catch (error) {
      this.toast(error.message, true);
      this.message(error.message, true);
    } finally {
      this.setBusy(false);
    }
  }

  async recoverReplan() {
    if (!confirm("Recover the interrupted replanning transaction? Execraft will deterministically roll back or finish the durable commit according to its recorded phase.")) return;
    this.setBusy(true);
    try {
      const result = await this.api("/api/task/replan/recover", { method: "POST", body: "{}" });
      this.lifecycle = result.lifecycle;
      this.acceptedDocuments = { ...(result.lifecycle?.documents || {}) };
      this.candidate = null;
      this.renderLifecycle();
      this.toast(result.recovered ? "Replan transaction recovered" : "No interrupted replan transaction found");
      await this.refreshDashboard({ reportError: false });
    } catch (error) {
      this.toast(error.message, true);
      this.message(error.message, true);
    } finally {
      this.setBusy(false);
    }
  }

  renderRepositorySync(detail = {}) {
    const target = $("repositorySyncStatus");
    if (!target) return;
    const rows = detail.packages || [];
    if (!detail.available) {
      target.className = "repository-sync-list empty";
      target.textContent = detail.error || "Repository synchronization is unavailable.";
      return;
    }
    if (!rows.length) {
      target.className = "repository-sync-list empty";
      target.textContent = "No repository-sync Work Packages in the accepted graph.";
      return;
    }
    target.className = "repository-sync-list";
    target.innerHTML = rows.map((pkg) => {
      const repositories = (pkg.repositories || []).map((repo) => {
        const severity = repo.error ? "bad" : repo.severity === "required" ? "bad" : repo.severity === "warning" ? "warn" : "ok";
        const detailText = repo.error
          ? repo.error
          : `${repo.remote}/${repo.source_branch} · ahead ${repo.ahead} · behind ${repo.behind}`;
        return `<small>${esc(repo.repository_id)} · ${badge(repo.error ? "error" : repo.severity || "current", severity)} ${esc(detailText)}</small>`;
      }).join("");
      const tx = pkg.transaction || {};
      const rollbackAllowed = tx.present && !tx.forward_only && !["complete", "rolled_back"].includes(tx.phase);
      const resolutionAllowed = tx.present && !tx.forward_only && ["resolving", "merging"].includes(tx.phase)
        && (tx.repositories || []).some((repo) => (repo.conflict_paths || []).length);
      const rollback = rollbackAllowed
        ? `<button class="secondary compact repository-sync-rollback" type="button" data-package-id="${esc(pkg.id)}">Rollback uncommitted sync</button>`
        : "";
      const acceptResolution = resolutionAllowed
        ? `<button class="primary compact repository-sync-accept-resolution" type="button" data-package-id="${esc(pkg.id)}">Accept edited resolution</button>`
        : "";
      return `<article class="ownership-repository"><header><strong>${esc(pkg.id)} · ${esc(pkg.title)}</strong>${badge(humanize(pkg.stage))}</header>${repositories}${tx.present ? `<small>Transaction: ${esc(tx.transaction_id || "—")} · ${esc(humanize(tx.phase))}${tx.forward_only ? " · forward-only (resume only)" : ""}</small>` : '<small>No synchronization transaction started.</small>'}<div class="repository-sync-actions">${acceptResolution}${rollback}</div></article>`;
    }).join("");
    target.querySelectorAll(".repository-sync-rollback").forEach((button) => {
      button.addEventListener("click", () => this.rollbackRepositorySync(button.dataset.packageId || ""));
    });
    target.querySelectorAll(".repository-sync-accept-resolution").forEach((button) => {
      button.addEventListener("click", () => this.acceptRepositorySyncResolution(button.dataset.packageId || ""));
    });
  }

  async acceptRepositorySyncResolution(packageId) {
    const id = String(packageId || "").trim();
    if (!id) return;
    if (!confirm(`Accept the current edited conflict resolution for ${id}? Execraft will validate ownership, reject conflict markers, and stage the files itself.`)) return;
    this.setBusy(true);
    try {
      const result = await this.api("/api/task/repository-sync/accept-resolution", {
        method: "POST",
        body: JSON.stringify({ package_id: id }),
      });
      this.lifecycle = result.lifecycle || this.lifecycle;
      this.render(this.lifecycle);
      this.toast(`Accepted resolution for ${id}`);
      await this.refreshDashboard({ reportError: false });
    } catch (error) {
      this.toast(error.message, true);
      this.message(error.message, true);
    } finally {
      this.setBusy(false);
    }
  }

  async rollbackRepositorySync(packageId) {
    const id = String(packageId || "").trim();
    if (!id) return;
    if (!confirm(`Roll back the uncommitted synchronization transaction for ${id}? This is refused after the first merge commit.`)) return;
    this.setBusy(true);
    try {
      const result = await this.api("/api/task/repository-sync/rollback", {
        method: "POST",
        body: JSON.stringify({ package_id: id }),
      });
      this.lifecycle = result.lifecycle || this.lifecycle;
      this.render(this.lifecycle);
      this.toast(`Rolled back ${id}`);
      await this.refreshDashboard({ reportError: false });
    } catch (error) {
      this.toast(error.message, true);
      this.message(error.message, true);
    } finally {
      this.setBusy(false);
    }
  }

  async refreshRepositorySync() {
    this.setBusy(true);
    try {
      const detail = await this.api("/api/task/repository-sync?refresh=1");
      if (this.lifecycle) this.lifecycle.repository_sync_detail = detail;
      this.renderRepositorySync(detail);
      this.toast("Upstream divergence refreshed");
    } catch (error) {
      this.toast(error.message, true);
      this.message(error.message, true);
    } finally {
      this.setBusy(false);
    }
  }

  async stageRepositorySync() {
    const before = $("repositorySyncBeforeInput")?.value.trim() || "";
    if (!before) {
      this.toast("Choose the Work Package that synchronization must precede.", true);
      return;
    }
    const repositories = String($("repositorySyncRepositoriesInput")?.value || "")
      .split(/[\s,]+/).map((item) => item.trim()).filter(Boolean);
    let sourceBranches = {};
    try {
      sourceBranches = mappingLines($("repositorySyncSourcesInput")?.value || "", "source-branch override");
    } catch (error) {
      this.toast(error.message, true);
      return;
    }
    this.setBusy(true);
    try {
      const result = await this.api("/api/task/repository-sync/before", {
        method: "POST",
        body: JSON.stringify({
          before_package_id: before,
          repositories,
          source_branches: sourceBranches,
          remote: $("repositorySyncRemoteInput")?.value.trim() || "origin",
          conflict_policy: $("repositorySyncConflictPolicy")?.value || "ai_resolve",
          sync_package_id: $("repositorySyncIdInput")?.value.trim() || "",
          apply: false,
        }),
      });
      const candidate = result.candidate;
      this.toast(`Staged ${result.repository_sync?.package_id || "repository sync"}`);
      await this.reloadAfterMutation({ preserveCandidate: candidate });
      if (candidate) this.renderCandidate(candidate);
    } catch (error) {
      this.toast(error.message, true);
      this.message(error.message, true);
    } finally {
      this.setBusy(false);
    }
  }

  renderCompletion() {
    const preview = this.lifecycle?.completion_preview || {};
    const completion = this.lifecycle?.completion || {};
    const finalSync = this.lifecycle?.final_sync || {};
    const target = $("completionPreview");
    const finalSyncButton = $("finalSyncBtn");
    finalSyncButton.disabled = !finalSync.eligible || completion.status === "completed";
    finalSyncButton.title = finalSync.eligible
      ? "Fetch upstream divergence and choose whether to add a verified final synchronization Work Package."
      : finalSync.reason || "Final sync becomes available after every package completes.";
    if (completion.status === "completed") {
      target.className = "completion-preview";
      target.innerHTML = `<div class="finding ok">Completion transaction finished. Archive and retained task history remain available.</div>${(completion.actions || []).map((action) => `<div class="completion-action">${esc(action)}</div>`).join("")}`;
      $("completeTaskBtn").disabled = true;
      $("completeTaskBtn").textContent = "Completion finished";
      finalSyncButton.textContent = "Workspace retired";
      return;
    }
    finalSyncButton.textContent = "Review final sync";
    $("completeTaskBtn").textContent = completion.status === "incomplete" ? "Resume archive & cleanup" : "Archive & clean workspace";
    $("completeTaskBtn").disabled = !preview.eligible;
    target.className = `completion-preview${preview.eligible ? "" : " empty"}`;
    const syncGuidance = finalSync.eligible
      ? `<div class="finding warning">Optional: review final upstream divergence before archiving. Synchronization is added only after confirmation and must pass verification and independent review.</div>`
      : "";
    target.innerHTML = preview.eligible
      ? `${syncGuidance}<div class="finding ok">Completion preflight passed. Archive and cleanup remain a separate explicit action.</div>${(preview.actions || []).map((action) => `<div class="completion-action">${esc(action)}</div>`).join("")}`
      : `<div class="finding ${completion.status === "incomplete" ? "error" : "warning"}">${esc(preview.reason || "Task is not yet eligible for completion.")}</div>${completion.status === "incomplete" && completion.report_path ? `<small>Recovery report: ${esc(completion.report_path)}</small>` : ""}`;
  }

  async finalSync() {
    const repositories = String($("repositorySyncRepositoriesInput")?.value || "")
      .split(/[\s,]+/).map((item) => item.trim()).filter(Boolean);
    let sourceBranches = {};
    try {
      sourceBranches = mappingLines($("repositorySyncSourcesInput")?.value || "", "source-branch override");
    } catch (error) {
      this.toast(error.message, true);
      return;
    }
    const payload = {
      repositories,
      source_branches: sourceBranches,
      remote: $("repositorySyncRemoteInput")?.value.trim() || "origin",
      conflict_policy: $("repositorySyncConflictPolicy")?.value || "ai_resolve",
      sync_package_id: $("repositorySyncIdInput")?.value.trim() || "",
    };
    this.setBusy(true);
    try {
      const preview = await this.api("/api/task/final-sync/preview", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      if (!preview.sync_required) {
        this.toast("Final upstream check complete · task branches are current");
        this.message("No final synchronization Work Package is needed. Archive and cleanup can proceed.");
        return;
      }
      const rows = (preview.repositories || [])
        .filter((row) => Number(row.behind || 0) > 0)
        .map((row) => `${row.repository_id}: ${row.behind} commit${Number(row.behind) === 1 ? "" : "s"} behind`)
        .join("\n");
      if (!confirm(`Upstream changes are available:\n\n${rows}\n\nAdd a trailing synchronization Work Package, verify it, and resume orchestration now? Archive and cleanup will remain disabled until it completes.`)) return;
      const result = await this.api("/api/task/final-sync/apply", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      if (!result.applied) {
        this.toast("Upstream became current before the final sync was applied");
        return;
      }
      const run = await this.api("/api/run/start", { method: "POST", body: "{}" });
      this.toast(`Final sync ${result.package_id} started${run.pid ? ` · PID ${run.pid}` : ""}`);
      await this.refreshDashboard({ reportError: false });
      await this.load();
    } catch (error) {
      this.toast(error.message, true);
      this.message(error.message, true);
      await this.load();
    } finally {
      this.setBusy(false);
    }
  }

  async completeTask() {
    const preview = this.lifecycle?.completion_preview || {};
    if (!preview.eligible) return;
    if (!confirm("Archive and clean this task now? If you want a final upstream synchronization, cancel and choose Review final sync first. A verified immutable archive will be created before task-owned runtime resources, registered Git worktrees, and the generated workspace shell are retired. Task branches, the live dossier, archive, history, and workspace tombstone are retained.")) return;
    this.setBusy(true);
    try {
      const result = await this.api("/api/task/complete", { method: "POST", body: JSON.stringify({ dry_run: false }) });
      this.lifecycle = result.lifecycle;
      this.renderLifecycle();
      this.toast("Task completion and cleanup finished");
      await this.refreshDashboard({ reportError: false });
    } catch (error) {
      this.toast(error.message, true);
      this.message(error.message, true);
      await this.load();
    } finally {
      this.setBusy(false);
    }
  }

  renderWorkspaceOwnership() {
    const workspace = this.lifecycle?.workspace || {};
    const target = $("workspaceOwnership");
    if (!workspace.available) {
      target.innerHTML = `<div class="finding warning">${esc(workspace.reason || "Workspace registry is unavailable.")}</div>`;
    } else {
      target.innerHTML = `<div class="impact-summary">${badge(humanize(workspace.status), workspace.status === "removed" ? "ok" : "")}${badge(humanize(workspace.runtime_status))}${workspace.shell_exists ? badge("shell present", "warn") : badge("shell retired", "ok")}</div>${(workspace.repositories || []).map((repo) => `<article class="ownership-repository"><header><strong>${esc(repo.id)} · ${esc(humanize(repo.mutability))}</strong>${repo.dirty ? badge("dirty", "bad") : repo.worktree_exists ? badge("clean", "ok") : badge("retired", "ok")}</header><small>Worktree: ${esc(repo.worktree_path)}</small><small>Source: ${esc(repo.source_path)}</small><small>Branch: ${esc(repo.branch || repo.expected_branch || "—")}${repo.git_operation ? ` · Git operation: ${esc(repo.git_operation)}` : ""}</small>${repo.inspection_error ? `<small class="bad">${esc(repo.inspection_error)}</small>` : ""}</article>`).join("")}`;
    }
    $("retainedResources").innerHTML = (this.lifecycle?.retained_resources || []).map((item) => `<article class="retained-resource"><header><strong>${esc(item.label)}</strong>${badge(item.retained ? "retained" : "missing", item.retained ? "ok" : "bad")}</header><small>${esc(item.value || "—")}</small></article>`).join("") || '<div class="empty">No retained-resource metadata.</div>';
  }

  renderHistory() {
    const rows = this.lifecycle?.revision_history || [];
    $("definitionRevisionHistory").innerHTML = rows.map((row) => `<article class="revision-history-row"><header><strong>Revision ${row.revision}</strong>${badge(row.applied ? "accepted" : "unapplied", row.applied ? "ok" : "warn")}</header>${row.candidate_id ? `<small>${esc(row.candidate_id)} · ${esc(humanize(row.generated_by))} · ${esc(humanize(row.consistency_mode))}</small>` : '<small>Baseline revision</small>'}${row.requested_change ? `<small>${esc(row.requested_change)}</small>` : ""}<small>${esc(row.path)}</small></article>`).join("") || '<div class="empty">No revision history is available yet.</div>';
  }

  async reloadAfterMutation({ preserveCandidate = null } = {}) {
    const lifecycle = await this.api("/api/task/lifecycle");
    this.lifecycle = lifecycle;
    this.acceptedDocuments = { ...(lifecycle.documents || {}) };
    this.candidate = preserveCandidate;
    this.renderLifecycle();
    if (preserveCandidate) {
      $("replanCandidateSelect").value = preserveCandidate.candidate_id;
      this.renderCandidate(preserveCandidate);
    }
  }

  setBusy(busy) {
    const ids = [
      "generatePlanBtn",
      "stageReplanBtn",
      "adoptCurrentDefinitionBtn",
      "loadReplanCandidateBtn",
      "applyReplanBtn",
      "recoverReplanBtn",
      "finalSyncBtn",
      "completeTaskBtn",
      "refreshRepositorySyncBtn",
      "stageRepositorySyncBtn",
      "taskLifecycleRefreshBtn",
    ];
    if (busy) {
      ids.forEach((id) => {
        const node = $(id);
        if (node) node.disabled = true;
      });
      return;
    }
    if (!this.lifecycle) return;
    this.renderCandidateList();
    const tx = this.lifecycle.replan_transaction || {};
    $("recoverReplanBtn").disabled = !tx.present || !tx.recoverable;
    this.renderCompletion();
    $("applyReplanBtn").disabled = !this.candidate?.impact?.applicable;
    $("generatePlanBtn").disabled = Boolean($("generatePlanProviderInput")?.disabled);
    $("stageReplanBtn").disabled = false;
    $("adoptCurrentDefinitionBtn").disabled = false;
    $("taskLifecycleRefreshBtn").disabled = false;
    if ($("refreshRepositorySyncBtn")) $("refreshRepositorySyncBtn").disabled = false;
    if ($("stageRepositorySyncBtn")) $("stageRepositorySyncBtn").disabled = false;
  }

  message(text, error = false) {
    const node = $("taskLifecycleMessage");
    if (!node) return;
    node.className = `workspace-note${error ? " bad" : ""}`;
    node.textContent = text;
  }
}
