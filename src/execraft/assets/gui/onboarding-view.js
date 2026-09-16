import {
  escapeHtml as esc,
  formatLocalTime as localTime,
  syncSelectOptions,
} from "./ui-utils.js";
import { RoadmapView } from "./roadmap-view.js";
import { ProjectExecutionView } from "./project-execution-view.js";

function element(id) {
  return document.getElementById(id);
}

function commaList(value) {
  return [...new Set(String(value || "").split(",").map((item) => item.trim()).filter(Boolean))];
}

async function uploadedText(inputId) {
  const file = element(inputId)?.files?.[0];
  if (!file) return { content: "", source: "" };
  const maxBytes = 2 * 1024 * 1024;
  if (file.size > maxBytes) throw new Error(`${file.name} exceeds the 2 MiB import limit`);
  let content;
  try {
    content = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer());
  } catch (_error) {
    throw new Error(`${file.name} must be UTF-8 text`);
  }
  return { content: content.replace(/\r\n?/g, "\n"), source: `browser-upload:${file.name}` };
}

function findingsHtml(findings = []) {
  if (!findings.length) return '<div class="finding info">No findings.</div>';
  return findings
    .map(
      (finding) =>
        `<div class="finding ${esc(finding.severity || "info")}"><strong>${esc(finding.code || finding.severity || "finding")}</strong><div>${esc(finding.message || "")}</div>${finding.remediation ? `<small>${esc(finding.remediation)}</small>` : ""}</div>`,
    )
    .join("");
}

function stepsHtml(steps = {}) {
  return Object.entries(steps)
    .map(
      ([id, value]) =>
        `<span class="session-step ${esc(value?.status || "")}">${esc(id)} · ${esc(value?.status || "pending")}</span>`,
    )
    .join("");
}

export class OnboardingView {
  constructor({ api, download, toast, onSnapshot, onProjectViewChange = () => {} }) {
    this.api = api;
    this.download = download;
    this.toast = toast;
    this.onSnapshot = onSnapshot;
    this.onProjectViewChange = onProjectViewChange;
    this.snapshot = null;
    this.currentProject = null;
    this.discoveryPreview = null;
    this.taskReview = null;
    this.verification = null;
    this.verificationProjectId = "";
    // A project is primarily a planning context.  Tasks remain available as a
    // secondary inventory view, but entering a project should answer "what is
    // happening next?" first, so Roadmap is the canonical landing surface.
    this.projectView = "roadmap";
    this.pendingRoadmapTaskLink = null;
    this.roadmapView = new RoadmapView({
      api: this.api,
      download: this.download,
      toast: this.toast,
      onOpenTask: (projectId, taskId) => this.openTask(projectId, taskId),
      onCreateTaskFromPlanned: (item, context) => this.createTaskFromRoadmap(item, context),
      onOpenProjectAsset: (kind, assetId) => this.#openProjectExecutionAsset(kind, assetId),
      onCanonicalChange: () => this.projectExecutionView?.invalidate(),
    });
    this.projectExecutionView = new ProjectExecutionView({
      api: this.api,
      download: this.download,
      toast: this.toast,
      onOpenTask: (projectId, taskId) => this.openTask(projectId, taskId),
      onCanonicalChange: () => this.roadmapView?.invalidateCanonicalProjection(),
    });
    this.busy = false;
    this.#bind();
  }

  render(snapshot) {
    this.snapshot = snapshot;
    const home = snapshot.mode === "home";
    element("homeContent").classList.toggle("hidden", !home);
    element("mainContent").classList.toggle("hidden", home);
    element("commandPaletteBtn").classList.toggle("hidden", home);
    if (!home) return;

    const projects = snapshot.projects || [];
    const focusedProjectId = snapshot.focused_project_id || "";
    const project = projects.find((item) => item.id === focusedProjectId) || null;
    element("projectCatalogView").classList.toggle("hidden", Boolean(project));
    element("projectWorkspaceView").classList.toggle("hidden", !project);
    this.#renderTemplates(snapshot.templates || {});
    this.#renderProjects(projects);
    this.#renderSessions(snapshot.onboarding_sessions || []);
    if (!projects.length) element("addProjectPanel").open = true;

    if (project) {
      this.openProject(project.id, { persist: false });
      return;
    }
    this.currentProject = null;
    this.roadmapView.setProject("");
    this.projectExecutionView.setProject("");
  }

  #bind() {
    element("refreshHomeBtn").addEventListener("click", () => this.refreshHome());
    document.querySelectorAll("[data-onboarding-mode]").forEach((button) => {
      button.addEventListener("click", () => this.#selectOnboardingMode(button.dataset.onboardingMode));
    });
    element("inspectSourceBtn").addEventListener("click", () => this.inspectSource());
    element("createProjectBtn").addEventListener("click", () => this.createProject());
    element("registerDescriptorBtn").addEventListener("click", () => this.registerDescriptor());
    element("previewGreenfieldBtn").addEventListener("click", () => this.previewGreenfield());
    element("createGreenfieldBtn").addEventListener("click", () => this.createGreenfield());
    element("closeProjectWorkbench").addEventListener("click", () => {
      this.returnToProjectCatalog();
    });
    element("newTaskShortcut").addEventListener("click", () => {
      this.pendingRoadmapTaskLink = null;
      this.#selectProjectView("tasks");
      element("taskDescriptionInput").focus();
    });
    document.querySelectorAll(".project-tab").forEach((tab) => {
      tab.addEventListener("click", () => this.#selectProjectView(tab.dataset.projectView));
    });
    element("previewTaskBtn").addEventListener("click", () => this.previewTask());
    element("createTaskBtn").addEventListener("click", () => this.createTask());
    element("closeTaskReview").addEventListener("click", () => element("taskReviewDialog").close());
    element("openReviewedTask").addEventListener("click", () => this.openReviewedTask());
    element("resumeReviewedTask").addEventListener("click", () => this.resumeReviewedTask());
  }

  #selectOnboardingMode(mode) {
    document.querySelectorAll("[data-onboarding-mode]").forEach((button) => {
      const selected = button.dataset.onboardingMode === mode;
      button.classList.toggle("active", selected);
      button.setAttribute("aria-selected", String(selected));
    });
    document.querySelectorAll("[data-onboarding-panel]").forEach((panel) => {
      panel.classList.toggle("hidden", panel.dataset.onboardingPanel !== mode);
    });
    const focusTarget = {
      source: "sourceRootInput",
      descriptor: "descriptorInput",
      greenfield: "greenfieldName",
    }[mode];
    if (focusTarget) element(focusTarget).focus();
  }

  async refreshHome() {
    try {
      const snapshot = await this.api("/api/home");
      this.onSnapshot(snapshot);
    } catch (error) {
      this.toast(error.message, true);
    }
  }

  async returnToProjectCatalog() {
    try {
      const snapshot = await this.api("/api/project/focus", {
        method: "POST",
        body: JSON.stringify({ project_id: "" }),
      });
      this.currentProject = null;
      this.onSnapshot(snapshot);
    } catch (error) {
      this.toast(error.message, true);
    }
  }

  #renderTemplates(templates) {
    const populate = (select, items, fallback = "") => {
      const current = select.value;
      syncSelectOptions(
        select,
        items.map((item) => ({
          value: item.reference || item.id,
          label: `${item.reference || item.id} · ${item.description}`,
        })),
        { value: current, fallbackValue: fallback },
      );
    };
    const newProjectProfiles = templates.new_project_profiles || (templates.profiles || []).filter((item) => item.new_project_selectable !== false);
    populate(element("greenfieldTemplate"), templates.greenfield || [], "python-service@1");
    populate(element("existingProjectProfile"), newProjectProfiles, "standard@3");
    populate(element("greenfieldProfile"), newProjectProfiles, "standard@3");
  }

  #renderProjects(projects) {
    element("projectCatalogSummary").textContent = `${projects.length} registered project${projects.length === 1 ? "" : "s"}`;
    const grid = element("projectGrid");
    if (!projects.length) {
      grid.innerHTML = '<div class="inspection-result empty">No registered projects. Open “Add or create a project” below to get started.</div>';
      return;
    }
    grid.innerHTML = projects
      .map((project) => {
        const ready = Boolean(project.readiness?.ready);
        const tasks = project.tasks || [];
        const taskRows = tasks.length
          ? tasks.slice(0, 4).map((task) => `<button class="project-card-task project-task-link" data-project-id="${esc(project.id)}" data-task-id="${esc(task.id)}"><span><strong>${esc(task.title || task.id)}</strong><small>${esc(task.id)}</small></span><span class="task-status">${esc(task.status || "draft")}</span></button>`).join("")
          : '<div class="project-card-empty">No tasks yet</div>';
        return `<article class="project-card ${project.active ? "active" : ""}" data-project-id="${esc(project.id)}">
          <header><div><h3>${esc(project.id)}</h3><p>${esc(project.description || "No description")}</p></div><span class="readiness-badge ${ready ? "ready" : "blocked"}">${ready ? "ready" : "blocked"}</span></header>
          <dl><div><dt>Profile</dt><dd>${esc(project.profile || "unversioned compatibility")}</dd></div><div><dt>Repositories</dt><dd>${project.repositories?.length || 0}</dd></div><div><dt>Tasks</dt><dd>${project.task_count || 0}</dd></div><div><dt>Roadmaps</dt><dd>${project.roadmap_count || 0}</dd></div><div><dt>Source</dt><dd title="${esc(project.source_root || "Unbound")}">${esc(project.source_root || "Unbound")}</dd></div></dl>
          <section class="project-card-task-list" aria-label="Recent tasks for ${esc(project.id)}"><div class="project-card-section-title">Recent tasks</div>${taskRows}</section>
          <div class="project-card-actions"><button class="btn small primary project-details" data-project-id="${esc(project.id)}">Open roadmap</button><button class="btn small project-new-task" data-project-id="${esc(project.id)}">New task</button></div>
        </article>`;
      })
      .join("");
    grid.querySelectorAll(".project-details").forEach((button) => {
      button.addEventListener("click", () => this.openProject(button.dataset.projectId));
    });
    grid.querySelectorAll(".project-new-task").forEach((button) => {
      button.addEventListener("click", async () => {
        await this.openProject(button.dataset.projectId);
        this.#selectProjectView("tasks");
        element("taskDescriptionInput").focus();
      });
    });
    grid.querySelectorAll(".project-task-link").forEach((button) => {
      button.addEventListener("click", () => this.openTask(button.dataset.projectId, button.dataset.taskId));
    });
  }

  #renderSessions(sessions) {
    const panel = element("sessionsPanel");
    const list = element("onboardingSessions");
    panel.classList.toggle("hidden", !sessions.length);
    if (!sessions.length) {
      list.replaceChildren();
      return;
    }
    list.innerHTML = sessions
      .map(
        (session) => `<div class="session-row"><div><strong>${esc(session.project_id)}/${esc(session.task_id)}</strong><small>${esc(localTime(session.modified_at))}</small></div><div class="session-steps">${stepsHtml(session.steps)}</div><div>${session.resumable ? `<button class="btn small resume-session" data-project-id="${esc(session.project_id)}" data-task-id="${esc(session.task_id)}">Review / resume</button>` : '<span class="readiness-badge ready">complete</span>'}</div></div>`,
      )
      .join("");
    list.querySelectorAll(".resume-session").forEach((button) => {
      button.addEventListener("click", () => this.reviewTask(button.dataset.projectId, button.dataset.taskId));
    });
  }

  async inspectSource() {
    const sourceRoot = element("sourceRootInput").value.trim();
    if (!sourceRoot) return this.toast("Enter a source folder", true);
    this.#setBusy(true);
    try {
      const result = await this.api("/api/onboarding/inspect", {
        method: "POST",
        body: JSON.stringify({
          source_root: sourceRoot,
          template_id: element("existingProjectProfile").value || "standard",
          features: commaList(element("existingProjectFeatures").value),
          devcontainer: element("existingProjectDevcontainer").checked,
          accept_decisions: element("acceptDiscoveryDecisions").checked,
        }),
      });
      this.discoveryPreview = result;
      const report = result.report || {};
      const creation = result.creation?.plan || {};
      element("sourceInspection").classList.remove("empty");
      const registered = result.registered_project || null;
      element("sourceInspection").innerHTML = `<div class="inspection-summary"><strong>${esc(report.project_id || "Project")}</strong><span>${report.repositories?.length || 0} repositories · ${(report.languages || []).join(", ") || "no language detected"}</span>${registered ? `<span>Already registered as ${esc(registered.id)}</span>` : `<span>${creation.files?.length || 0} generated files · ${creation.can_apply ? "applicable" : "blocked"}</span>`}${findingsHtml(report.findings || [])}</div>`;
      element("createProjectBtn").disabled = Boolean(registered) || !creation.can_apply;
    } catch (error) {
      this.discoveryPreview = null;
      element("createProjectBtn").disabled = true;
      element("sourceInspection").textContent = error.message;
      this.toast(error.message, true);
    } finally {
      this.#setBusy(false);
    }
  }

  async createProject() {
    if (!this.discoveryPreview) return;
    this.#setBusy(true);
    try {
      const result = await this.api("/api/onboarding/project/create", {
        method: "POST",
        body: JSON.stringify({
          source_root: element("sourceRootInput").value.trim(),
          template_id: element("existingProjectProfile").value || "standard",
          features: commaList(element("existingProjectFeatures").value),
          devcontainer: element("existingProjectDevcontainer").checked,
          accept_decisions: element("acceptDiscoveryDecisions").checked,
          acknowledged: true,
        }),
      });
      this.toast("Project created and registered");
      this.discoveryPreview = null;
      element("createProjectBtn").disabled = true;
      this.onSnapshot(result.home);
    } catch (error) {
      this.toast(error.message, true);
    } finally {
      this.#setBusy(false);
    }
  }

  async registerDescriptor() {
    const descriptor = element("descriptorInput").value.trim();
    if (!descriptor) return this.toast("Enter a project.yaml path", true);
    this.#setBusy(true);
    try {
      const result = await this.api("/api/onboarding/project/register", {
        method: "POST",
        body: JSON.stringify({
          descriptor,
          source_root: element("descriptorSourceInput").value.trim(),
          acknowledged: true,
        }),
      });
      this.toast(`Registered ${result.project}`);
      this.onSnapshot(result.home);
    } catch (error) {
      this.toast(error.message, true);
    } finally {
      this.#setBusy(false);
    }
  }

  #greenfieldPayload() {
    return {
      name: element("greenfieldName").value.trim(),
      parent: element("greenfieldParent").value.trim() || ".",
      template: element("greenfieldTemplate").value || "python-service",
      project_template: element("greenfieldProfile").value || "standard",
      features: commaList(element("greenfieldFeatures").value),
      devcontainer: element("greenfieldDevcontainer").checked,
      description: element("greenfieldDescription").value.trim(),
      planner: element("greenfieldPlanner").value,
    };
  }

  #renderGreenfieldPreview(result, payload) {
    const target = element("greenfieldPreview");
    target.classList.remove("empty", "hidden");
    target.innerHTML = `<div class="inspection-summary"><strong>${esc(payload.name)}</strong><span>${result.source_creation?.files?.length || 0} source files</span><span>${result.project_creation?.plan?.files?.length || 0} control-plane files</span>${payload.description ? `<span>First task: ${esc(payload.description)}</span>` : ""}</div>`;
  }

  async previewGreenfield({ apply = false } = {}) {
    const payload = this.#greenfieldPayload();
    if (!payload.name) return this.toast("Enter a project name", true);
    this.#setBusy(true);
    try {
      const preview = await this.api("/api/onboarding/greenfield/preview", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      this.#renderGreenfieldPreview(preview, payload);
      if (!apply) return;
      if (preview.source_creation?.can_apply === false || preview.project_creation?.plan?.can_apply === false) {
        this.toast("Project preview is blocked; review the details above", true);
        return;
      }
      const result = await this.api("/api/onboarding/greenfield/apply", {
        method: "POST",
        body: JSON.stringify({ ...payload, acknowledged: true }),
      });
      this.toast("Greenfield project created");
      this.onSnapshot(result.home);
    } catch (error) {
      const target = element("greenfieldPreview");
      target.classList.remove("hidden");
      target.textContent = error.message;
      this.toast(error.message, true);
      if (apply) await this.refreshHome();
    } finally {
      this.#setBusy(false);
    }
  }

  async createGreenfield() {
    return this.previewGreenfield({ apply: true });
  }

  async openProject(projectId, { persist = true } = {}) {
    if (persist && this.snapshot?.focused_project_id !== projectId) {
      try {
        const snapshot = await this.api("/api/project/focus", {
          method: "POST",
          body: JSON.stringify({ project_id: projectId }),
        });
        this.onSnapshot(snapshot);
        return;
      } catch (error) {
        this.toast(error.message, true);
        return;
      }
    }
    const project = (this.snapshot?.projects || []).find((item) => item.id === projectId);
    if (!project) return;
    const changedProject = this.currentProject?.id !== project.id;
    this.currentProject = project;
    this.roadmapView.setProject(project.id);
    this.projectExecutionView.setProject(project.id, project.tasks || []);
    if (changedProject) this.pendingRoadmapTaskLink = null;
    element("projectWorkbenchTitle").textContent = project.id;
    element("projectWorkbenchSubtitle").textContent = project.description || project.source_root || "Project management and task execution";
    element("projectRoadmapsCount").textContent = String(project.roadmap_count || 0);
    element("projectWorkspaceFacts").innerHTML = `<div><dt>Profile</dt><dd>${esc(project.profile || "unversioned compatibility")}</dd></div><div><dt>Repositories</dt><dd>${project.repositories?.length || 0}</dd></div><div><dt>Tasks</dt><dd>${project.task_count || 0}</dd></div><div><dt>Roadmaps</dt><dd>${project.roadmap_count || 0}</dd></div><div><dt>Source</dt><dd title="${esc(project.source_root || "Unbound")}">${esc(project.source_root || "Unbound")}</dd></div>`;
    this.#renderRepositoryChoices(project.repositories || [], {
      preserveSelection: !changedProject,
    });
    this.#renderTaskList(project.tasks || []);
    if (changedProject) this.projectView = "roadmap";
    this.#selectProjectView(this.projectView);
    await Promise.all([
      this.#loadReadiness(projectId),
      this.#loadExecution(projectId),
      this.#loadVerification(projectId),
    ]);
  }

  #selectProjectView(view) {
    const selected = ["roadmap", "execution", "tasks", "settings", "archive"].includes(view)
      ? view
      : "roadmap";
    this.projectView = selected;
    document.querySelectorAll(".project-tab").forEach((tab) => {
      const active = tab.dataset.projectView === selected;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", String(active));
    });
    for (const name of ["roadmap", "execution", "tasks", "settings", "archive"]) {
      element(`project${name[0].toUpperCase()}${name.slice(1)}View`).classList.toggle(
        "hidden",
        name !== selected,
      );
    }
    this.onProjectViewChange(selected, this.currentProject?.id || "");
    if (selected === "roadmap") void this.roadmapView.load();
    if (selected === "execution") void this.projectExecutionView.load();
  }

  /**
   * Select a project-level view from application navigation.
   *
   * This small public seam keeps task-to-project navigation out of the
   * RoadmapView and ContextNavigation implementations: they own navigation
   * intent, while OnboardingView continues to own project-workspace content.
   */
  showProjectView(view = "roadmap") {
    if (!this.currentProject) return;
    this.#selectProjectView(view);
  }

  #openProjectExecutionAsset(kind, assetId) {
    if (!this.currentProject || !assetId) return;
    this.#selectProjectView("execution");
    void this.projectExecutionView.focusAsset(kind, assetId);
  }

  async #loadReadiness(projectId) {
    const target = element("projectReadinessView");
    target.innerHTML = "Loading readiness…";
    try {
      const report = await this.api(`/api/onboarding/readiness?project_id=${encodeURIComponent(projectId)}`);
      target.innerHTML = `<div class="readiness-list">${(report.checks || []).map((check) => `<div class="readiness-row"><strong>${esc(check.id)}</strong><div>${esc(check.summary)}${(check.details || []).map((item) => `<small>${esc(item)}</small>`).join("")}</div><span class="status ${esc(check.status)}">${esc(check.status)}</span></div>`).join("")}</div>`;
    } catch (error) {
      target.textContent = error.message;
    }
  }

  async #loadExecution(projectId) {
    const target = element("projectExecutionReadinessView");
    target.innerHTML = "Loading execution readiness…";
    try {
      const report = await this.api(`/api/onboarding/execution?project_id=${encodeURIComponent(projectId)}`);
      const runtimes = (report.runtimes || []).map((runtime) => `<div class="provider-row"><strong>${esc(runtime.id)}</strong><div><code>${esc(runtime.kind === "openclaw" ? `${runtime.mode || "managed"} · ${runtime.gateway || "gateway"}` : runtime.binary_path || runtime.binary || runtime.adapter || "runtime")}</code><small>${esc(runtime.kind === "openclaw" ? `OpenClaw · auth ${runtime.authentication_configured ? "configured" : "not configured"}` : `Native · ${runtime.adapter || "adapter"}`)}</small></div><span class="status ${runtime.ready ? "available" : "missing"}">${runtime.ready ? "configured" : "missing prerequisite"}</span></div>`).join("");
      const profiles = (report.profiles || []).map((profile) => `<div class="provider-row"><strong>${esc(profile.id)}</strong><div><code>${esc(`${profile.runtime_id} → ${profile.model_route_id || "runtime default"} → ${profile.target_id || "local/direct"}`)}</code><small>${esc((profile.capabilities || []).join(", "))}</small></div><span class="status ${profile.ready ? "available" : profile.enabled ? "missing" : "disabled"}">${profile.ready ? "ready" : profile.enabled ? "blocked" : "disabled"}</span></div>`).join("");
      target.innerHTML = `<div class="execution-readiness-summary"><strong>Execution architecture</strong><small>schema v${esc(report.source_schema_version || "?")} · ${esc(report.ready_profiles || 0)}/${esc(report.enabled_profiles || 0)} enabled profiles have local runtime prerequisites</small></div><div class="provider-list">${runtimes || '<div class="inspection-result empty">No runtimes configured.</div>'}</div><details><summary>Effective profiles</summary><div class="provider-list">${profiles || '<div class="inspection-result empty">No execution profiles configured.</div>'}</div></details>${findingsHtml(report.findings || [])}${(report.warnings || []).map((warning) => `<div class="inspection-result warning">${esc(warning)}</div>`).join("")}`;
    } catch (error) {
      target.textContent = error.message;
    }
  }

  #verificationDraft() {
    const checkboxes = [
      ...document.querySelectorAll(
        "#projectVerificationView [data-verification-index]",
      ),
    ];
    const required = element("verificationRequired");
    if (!required) return null;
    return {
      enabledIndexes: checkboxes
        .filter((item) => item.checked)
        .map((item) => Number(item.dataset.verificationIndex))
        .sort((left, right) => left - right),
      requireCommands: required.checked,
    };
  }

  #verificationDraftDirty(projectId) {
    if (!this.verification || this.verificationProjectId !== projectId)
      return false;
    const draft = this.#verificationDraft();
    if (!draft) return false;
    const accepted = (this.verification.commands || [])
      .filter((command) => command.enabled)
      .map((command) => Number(command.index))
      .sort((left, right) => left - right);
    return (
      JSON.stringify(draft.enabledIndexes) !== JSON.stringify(accepted) ||
      draft.requireCommands !== Boolean(this.verification.require_commands)
    );
  }

  async #loadVerification(projectId) {
    const target = element("projectVerificationView");
    const hasExistingControls = Boolean(
      this.verificationProjectId === projectId && this.#verificationDraft(),
    );
    if (!hasExistingControls) target.innerHTML = "Loading verification…";
    try {
      const registry = await this.api(`/api/onboarding/verification?project_id=${encodeURIComponent(projectId)}`);
      // Re-read the DOM after the request: the operator may have changed a
      // checkbox while this background refresh was in flight.
      const localDraft = this.#verificationDraftDirty(projectId)
        ? this.#verificationDraft()
        : null;
      this.verification = registry;
      this.verificationProjectId = projectId;
      const enabled = new Set(
        localDraft?.enabledIndexes ||
          (registry.commands || [])
            .filter((command) => command.enabled)
            .map((command) => Number(command.index)),
      );
      const requireCommands = localDraft
        ? localDraft.requireCommands
        : Boolean(registry.require_commands);
      target.innerHTML = `<div class="verification-list">${(registry.commands || []).map((command) => `<label class="verification-row"><input type="checkbox" data-verification-index="${command.index}" ${enabled.has(Number(command.index)) ? "checked" : ""}><strong>${esc(command.id || command.profile || `command-${command.index + 1}`)}</strong><code>${esc(command.command)}</code><span>${esc(command.profile || "focused")}</span></label>`).join("") || '<div class="inspection-result empty">No verification commands are configured.</div>'}</div><div class="verification-actions"><label class="check-row"><input id="verificationRequired" type="checkbox" ${requireCommands ? "checked" : ""}> Require at least one enabled command</label><button id="saveVerificationBtn" type="button" class="btn primary">Save approvals</button></div>`;
      element("saveVerificationBtn").addEventListener("click", () => this.saveVerification());
    } catch (error) {
      if (hasExistingControls) {
        let notice = target.querySelector(".verification-refresh-error");
        if (!notice) {
          notice = document.createElement("div");
          notice.className = "inspection-result error verification-refresh-error";
          target.appendChild(notice);
        }
        notice.textContent = `Verification refresh unavailable: ${error.message}`;
        return;
      }
      this.verification = null;
      this.verificationProjectId = "";
      target.textContent = error.message;
    }
  }

  async saveVerification() {
    if (!this.currentProject || !this.verification) return;
    const draft = this.#verificationDraft();
    const enabled = draft?.enabledIndexes || [];
    this.#setBusy(true);
    try {
      const result = await this.api("/api/onboarding/verification/update", {
        method: "POST",
        body: JSON.stringify({
          project_id: this.currentProject.id,
          expected_sha256: this.verification.sha256,
          enabled_indexes: enabled,
          require_commands: Boolean(draft?.requireCommands),
          acknowledged: true,
        }),
      });
      this.verification = result.verification;
      this.verificationProjectId = this.currentProject.id;
      this.toast("Verification approvals saved");
      await this.#loadReadiness(this.currentProject.id);
      await this.#loadVerification(this.currentProject.id);
    } catch (error) {
      this.toast(error.message, true);
    } finally {
      this.#setBusy(false);
    }
  }

  #renderRepositoryChoices(repositories, { preserveSelection = true } = {}) {
    const target = element("taskRepositoryChoices");
    const previous = preserveSelection
      ? new Map(
          [...target.querySelectorAll("[data-task-repository]")].map((input) => [
            input.dataset.taskRepository,
            input.checked,
          ]),
        )
      : new Map();
    target.innerHTML = repositories
      .map((repository) => {
        const checked = repository.required
          ? true
          : previous.has(repository.id)
            ? previous.get(repository.id)
            : true;
        return `<label class="repository-choice ${repository.required ? "required" : ""}"><input type="checkbox" data-task-repository="${esc(repository.id)}" ${checked ? "checked" : ""} ${repository.required ? "disabled" : ""}>${esc(repository.id)}${repository.required ? " · required" : ""}</label>`;
      })
      .join("");
  }

  #renderTaskList(tasks) {
    const target = element("projectTaskList");
    const navigator = element("projectTaskNavigator");
    element("projectTasksCount").textContent = String(tasks.length);
    if (!tasks.length) {
      target.innerHTML = '<div class="inspection-result empty">No tasks yet. Use the task form above to create the first one.</div>';
      navigator.innerHTML = '<div class="project-task-navigator-empty">No tasks yet</div>';
      return;
    }
    target.innerHTML = tasks
      .map((task) => `<article class="task-card"><div><h4>${esc(task.title || task.id)}</h4><p>${esc(task.id)} · ${esc(task.status)} · ${(task.repositories || []).map(esc).join(", ")}</p></div><div class="task-card-actions"><button class="btn small review-task" data-task-id="${esc(task.id)}">Review</button><button class="btn small primary open-task" data-task-id="${esc(task.id)}">Open dashboard</button></div></article>`)
      .join("");
    navigator.innerHTML = tasks.slice(0, 8)
      .map((task) => `<button class="project-task-nav-item" data-task-id="${esc(task.id)}"><span>${esc(task.title || task.id)}</span><small>${esc(task.status || "draft")}</small></button>`)
      .join("");
    target.querySelectorAll(".review-task").forEach((button) => button.addEventListener("click", () => this.reviewTask(this.currentProject.id, button.dataset.taskId)));
    target.querySelectorAll(".open-task").forEach((button) => button.addEventListener("click", () => this.openTask(this.currentProject.id, button.dataset.taskId)));
    navigator.querySelectorAll(".project-task-nav-item").forEach((button) => button.addEventListener("click", () => this.openTask(this.currentProject.id, button.dataset.taskId)));
  }

  createTaskFromRoadmap(item, context) {
    if (!item || !context || !this.currentProject) return;
    this.pendingRoadmapTaskLink = context;
    this.#selectProjectView("tasks");
    element("taskDescriptionInput").value = item.description?.trim() || item.title || "";
    element("taskIdInput").value = "";
    element("taskPreview").classList.add("hidden");
    element("taskAdvancedOptions").open = false;
    element("taskDescriptionInput").focus();
    this.toast(`Creating a task from roadmap item “${item.title || item.id}”. The roadmap link will be updated after canonical task creation.`);
  }

  async #taskPayload() {
    const brief = await uploadedText("taskBriefFileInput");
    const plan = await uploadedText("taskPlanFileInput");
    const graph = await uploadedText("taskPlanGraphFileInput");
    return {
      project_id: this.currentProject?.id || "",
      source_root: this.currentProject?.source_root || "",
      description: element("taskDescriptionInput").value.trim(),
      task_id: element("taskIdInput").value.trim(),
      planner: element("taskPlannerInput").value,
      no_workspace: element("taskNoWorkspaceInput").checked,
      repositories: [...document.querySelectorAll("[data-task-repository]:checked")].map((item) => item.dataset.taskRepository),
      brief_markdown: brief.content,
      brief_source: brief.source,
      plan_markdown: plan.content,
      plan_source: plan.source,
      plan_graph_yaml: graph.content,
      plan_graph_source: graph.source,
    };
  }

  #validateTaskPayload(payload) {
    if (payload.description || payload.brief_markdown || payload.plan_markdown || payload.plan_graph_yaml) return true;
    this.toast("Describe the task or import BRIEF.md/PLAN.md", true);
    return false;
  }

  #renderTaskPreview(result, payload) {
    const imported = [
      payload.brief_markdown && "BRIEF.md",
      payload.plan_markdown && "PLAN.md",
      payload.plan_graph_yaml && "PLAN.graph.yaml",
    ].filter(Boolean);
    const target = element("taskPreview");
    target.classList.remove("empty", "hidden");
    target.innerHTML = `<div class="inspection-summary"><strong>${esc(result.project)}/${esc(result.task_id)}</strong><span>Repositories: ${esc((result.repository_scope?.repositories || []).join(", "))}</span><span>Planner: ${esc(result.provider?.selected?.name || "local deterministic")}</span><span>Workspace: ${esc(result.workspace_root || "disabled")}</span>${imported.length ? `<span>Imported: ${esc(imported.join(", "))}</span>` : ""}${(result.steps || []).map((step) => `<span>${esc(step.status)} · ${esc(step.id)} · ${esc(step.summary)}</span>`).join("")}</div>`;
  }

  async previewTask({ apply = false } = {}) {
    if (!this.currentProject) return;
    let payload;
    try {
      payload = await this.#taskPayload();
    } catch (error) {
      return this.toast(error.message, true);
    }
    if (!this.#validateTaskPayload(payload)) return;
    this.#setBusy(true);
    try {
      const preview = await this.api("/api/onboarding/start/preview", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      this.#renderTaskPreview(preview, payload);
      if (!preview.can_apply) {
        this.toast(apply ? "Task cannot be created yet; review the preview details" : "Task preview is blocked; review the details above", true);
        return;
      }
      if (!apply) return;
      const result = await this.api("/api/onboarding/start/apply", {
        method: "POST",
        body: JSON.stringify({ ...payload, acknowledged: true }),
      });
      this.toast("Task, workspace, and draft plan created");
      await this.refreshHome();
      if (this.pendingRoadmapTaskLink) {
        const context = this.pendingRoadmapTaskLink;
        this.pendingRoadmapTaskLink = null;
        const linked = await this.roadmapView.linkCreatedTask(
          context,
          result.outcome.task_id,
        );
        if (linked) this.toast("Roadmap item converted to the new Execraft task");
      }
      await this.reviewTask(result.outcome.project, result.outcome.task_id);
    } catch (error) {
      const target = element("taskPreview");
      target.classList.remove("hidden");
      target.textContent = error.message;
      this.toast(error.message, true);
      if (apply) await this.refreshHome();
    } finally {
      this.#setBusy(false);
    }
  }

  async createTask() {
    return this.previewTask({ apply: true });
  }

  async reviewTask(projectId, taskId) {
    try {
      const review = await this.api(`/api/onboarding/task?project_id=${encodeURIComponent(projectId)}&task_id=${encodeURIComponent(taskId)}`);
      this.taskReview = review;
      element("taskReviewTitle").textContent = review.manifest?.title || taskId;
      element("taskReviewSubtitle").textContent = `${projectId}/${taskId} · ${review.manifest?.status || "draft"}`;
      element("taskReviewBrief").textContent = review.brief || "No brief available.";
      const packages = review.plan_graph?.work_packages || [];
      element("taskReviewPlan").innerHTML = review.plan_error
        ? `<div class="finding error">${esc(review.plan_error)}</div>`
        : packages.length
          ? packages.map((item) => `<div class="plan-package"><strong>${esc(item.id)} · ${esc(item.title)}</strong><small>${esc(item.stage || "prepare")} · ${(item.affected_repositories || []).map(esc).join(", ") || "repository scope inherited"}</small></div>`).join("")
          : '<div class="inspection-result empty">No executable plan is published.</div>';
      element("taskReviewJournal").innerHTML = review.journal && Object.keys(review.journal).length
        ? `<div class="session-steps">${stepsHtml(review.journal.steps || {})}</div>`
        : '<div class="inspection-result empty">No start journal is available.</div>';
      element("resumeReviewedTask").classList.toggle("hidden", !review.resumable);
      element("taskReviewDialog").showModal();
    } catch (error) {
      this.toast(error.message, true);
    }
  }

  async openTask(projectId, taskId) {
    try {
      const snapshot = await this.api("/api/session/open", {
        method: "POST",
        body: JSON.stringify({ project_id: projectId, task_id: taskId, acknowledged: true }),
      });
      if (element("taskReviewDialog").open) element("taskReviewDialog").close();
      this.onSnapshot(snapshot);
    } catch (error) {
      this.toast(error.message, true);
    }
  }

  openReviewedTask() {
    if (!this.taskReview) return;
    this.openTask(this.taskReview.project, this.taskReview.task_id);
  }

  async resumeReviewedTask() {
    if (!this.taskReview) return;
    const description = this.taskReview.journal?.description || "";
    const sourceRoot = this.taskReview.journal?.source_root || "";
    if (!description || !sourceRoot) {
      this.toast("This legacy journal lacks the original task description; start it again from the project task form.", true);
      return;
    }
    try {
      const result = await this.api("/api/onboarding/start/apply", {
        method: "POST",
        body: JSON.stringify({
          ...(this.taskReview.journal?.request || {}),
          project_id: this.taskReview.project,
          task_id: this.taskReview.task_id,
          source_root: sourceRoot,
          description,
          acknowledged: true,
        }),
      });
      this.toast("Start workflow resumed");
      await this.reviewTask(result.outcome.project, result.outcome.task_id);
      await this.refreshHome();
    } catch (error) {
      this.toast(error.message, true);
    }
  }

  #setBusy(value) {
    this.busy = value;
    document.querySelectorAll("#homeContent button").forEach((button) => {
      if (value) {
        button.dataset.wasDisabled = String(button.disabled);
        button.disabled = true;
      } else if (button.dataset.wasDisabled !== undefined) {
        button.disabled = button.dataset.wasDisabled === "true";
        delete button.dataset.wasDisabled;
      }
    });
  }
}
