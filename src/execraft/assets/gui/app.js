import { AgentConsoleWorkbench } from "./agent-console.js";
import { AgentWorkforceView } from "./agent-workforce-view.js";
import { ArchiveView } from "./archive-view.js";
import { ContextNavigation } from "./context-navigation.js";
import { ExecutionHealthView } from "./execution-health-view.js";
import { ExecutionTraceView } from "./execution-trace-view.js";
import { LogController } from "./log-controller.js";
import { WorkPackageInspector, workPackageDetailSnapshotKey } from "./work-package-inspector.js";
import { acceptanceCriteriaMarkup, contractList, hasStructuredContent, packageRelationButton, packageRelationList, structuredEvidence } from "./work-package-presenter.js";
import { WorkPackageExecutionView } from "./work-package-execution.js";
import { OnboardingView } from "./onboarding-view.js";
import { ProfileMaintenanceView } from "./profile-maintenance.js";
import { RunControlController } from "./run-control.js";
import { RuntimeControlView } from "./runtime-control.js";
import { TaskLifecycleView } from "./task-lifecycle.js";
import { UiShell } from "./ui-shell.js";
import { WorkflowGraph, WorkflowList, assignedAgent } from "./workflow.js";
import { WorkbenchNavigation } from "./workbench-navigation.js";
import { WorkflowViewport } from "./workflow-viewport.js";
import { WorkspaceChangesView } from "./workspace-changes-view.js";
import {
  elementById as $,
  escapeHtml as esc,
  compactCount,
  downloadAuthenticatedArtifact,
  formatLocalTime as localTime,
  hasActiveFormInteraction,
  readJsonStorage,
  syncSelectOptions,
  updateJsonStorage,
  truncateText as truncate,
} from "./ui-utils.js";

const TOKEN = document.querySelector('meta[name="execraft-token"]')?.content || "";
const DASHBOARD_REFRESH_MS = 3000;
const HIDDEN_REFRESH_MS = 15000;
const WORKSPACE_REFRESH_MS = 6000;
const HOME_REFRESH_MS = 10000;
const GUI_PREFERENCES_KEY = "execraft.gui.preferences.v1";

function workbenchPreferences() {
  return readJsonStorage(GUI_PREFERENCES_KEY);
}

function initialWorkflowViewMode() {
  const value = workbenchPreferences().workflowViewMode;
  return value === "list" ? "list" : "graph";
}

const workbenchNavigation = new WorkbenchNavigation({
  view: initialWorkflowViewMode(),
  followActive: Boolean(workbenchPreferences().followActive),
});

const state = {
  snapshot: null,
  filter: "all",
  workflowEdgeMode: "essential",
  configKey: null,
  configSha: "",
  workflowRenderKey: "",
  workflowContextKey: "",
  workflowGraphInitialized: false,
  workflowNavigationFrame: 0,
  configMenuRenderKey: "",
  workPackageDetailRenderKey: "",
  workPackageDetailRefreshPending: false,
  workPackageDetailErrorKey: "",
  supervisorQuestionRenderKey: "",
  supervisorQuestionIdentityKey: "",
  supervisorQuestionRefreshPending: false,
  workPackageSync: { packageId: "", options: null, busy: false },
};

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("X-Execraft-Token", TOKEN);
  if (options.body != null && !headers.has("Content-Type"))
    headers.set("Content-Type", "application/json");
  const init = { cache: "no-store", ...options, headers };
  const response = await fetch(path, init);
  const payload = await response.text();
  let data = {};
  try {
    data = payload ? JSON.parse(payload) : {};
  } catch (_) {
    data = { error: payload || response.statusText };
  }
  if (!response.ok)
    throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}
function toast(message, error = false) {
  const node = $("toast");
  node.textContent = message;
  node.className = "toast show" + (error ? " error" : "");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (node.className = "toast"), 4200);
}
function selectedPackageId() {
  return workbenchNavigation.snapshot().selectedId;
}

function selectPackage(id) {
  workbenchNavigation.select(id);
}

function workflowViewMode() {
  return workbenchNavigation.snapshot().viewMode;
}

function setWorkflowViewMode(mode) {
  workbenchNavigation.setViewMode(mode);
  updateJsonStorage(GUI_PREFERENCES_KEY, { workflowViewMode: mode });
}

function setFollowActive(enabled) {
  workbenchNavigation.setFollowActive(enabled);
  updateJsonStorage(GUI_PREFERENCES_KEY, { followActive: Boolean(enabled) });
}

function workflowContextKey(snapshot) {
  if (snapshot?.mode !== "task") return "";
  return [snapshot.project?.id || "", snapshot.project?.task_id || ""].join("/");
}

function syncWorkflowContext(snapshot) {
  const contextKey = workflowContextKey(snapshot);
  if (contextKey === state.workflowContextKey) return;
  state.workflowContextKey = contextKey;
  state.workflowRenderKey = "";
  state.workflowGraphInitialized = false;
  workPackageInspector.close({ restoreFocus: false });
  executionHealthView.close({ restoreFocus: false });
  state.workPackageDetailRefreshPending = false;
  state.workPackageDetailRenderKey = "";
  state.workPackageDetailErrorKey = "";
  executionTraceView.reset();
  workspaceChangesView.reset();
  workbenchNavigation.clearSelection();
  workbenchNavigation.setActive([]);
  workbenchNavigation.setViewport({});
  workflowViewport.restore({});
}

const workflowViewport = new WorkflowViewport({
  viewport: $("workflowWrap"),
  space: $("workflowViewport"),
  canvas: $("workflowCanvas"),
  controls: $("workflowViewportControls"),
  output: $("workflowZoom"),
  onChange: (viewport) => workbenchNavigation.setViewport(viewport),
});
const workflowGraph = new WorkflowGraph({
  board: $("workflow"),
  canvas: $("workflowCanvas"),
  svg: $("workflowEdges"),
  wrap: $("workflowWrap"),
  edgeSummary: $("workflowEdgeSummary"),
  onAction: handleWorkPackageAction,
});
const workflowList = new WorkflowList({
  container: $("workflowList"),
  onAction: handleWorkPackageAction,
});
const workPackageInspector = new WorkPackageInspector({
  root: $("workPackageInspector"),
  title: $("workPackageInspectorTitle"),
  meta: $("workPackageInspectorMeta"),
  content: $("packageDetail"),
  tablist: $("workPackageInspectorTabs"),
  closeButton: $("closeWorkPackageInspector"),
  focusReturnTarget: () =>
    workflowViewMode() === "graph"
      ? $("workflowWrap")
      : $("workflowViewMode").querySelector('[data-workflow-view="list"]'),
  onClose: () => {
    state.workPackageDetailRefreshPending = false;
    state.workPackageDetailRenderKey = "";
    state.workPackageDetailErrorKey = "";
  },
});
const agentConsole = new AgentConsoleWorkbench({ api, toast, localTime });
const executionTraceView = new ExecutionTraceView({
  api,
  agentConsole,
  selectedPackageId,
});
const workspaceChangesView = new WorkspaceChangesView({
  api,
  toast,
  refreshDashboard: () => refresh(),
  snapshot: () => state.snapshot,
});
const runControl = new RunControlController({ api, toast, refresh });
const logs = new LogController({ api });
const uiShell = new UiShell({
  api,
  toast,
  setView,
  refresh,
  openAgent: (agentId, options) => agentConsole.open(agentId, options),
});
const contextNavigation = new ContextNavigation({
  api,
  toast,
  onSnapshot: applySnapshot,
});
const archiveView = new ArchiveView({
  api,
  toast,
  onCatalogChanged: async () => {
    await contextNavigation.refreshCatalog({ force: true });
    await refresh({ reportError: false });
  },
});
const onboardingView = new OnboardingView({
  api,
  download: (path) => downloadAuthenticatedArtifact(path, TOKEN),
  toast,
  onSnapshot: applySnapshot,
  onProjectViewChange: (view, projectId) => {
    if (view === "archive") void archiveView.load();
    if (view === "settings") void runtimeControlView.load({ projectId, includeSetup: true });
  },
});
const taskLifecycleView = new TaskLifecycleView({
  api,
  toast,
  download: (path) => downloadAuthenticatedArtifact(path, TOKEN),
  refreshDashboard: refresh,
});
const runtimeControlView = new RuntimeControlView({ api, toast });
const executionHealthView = new ExecutionHealthView({
  root: $("diagnosticsStrip"),
  drawer: $("executionHealthDrawer"),
  openButton: $("executionHealthDrawerOpen"),
  closeButton: $("executionHealthDrawerClose"),
  overview: $("executionHealthOverview"),
  status: $("infrastructureHealth"),
  summary: $("diagnosticsSummary"),
  onVisibilityChange: (open) => {
    profileMaintenance.invalidate();
    if (!open || !state.snapshot) return;
    profileMaintenance.render(state.snapshot);
    void runtimeControlView.load({
      projectId: state.snapshot.project?.id || "",
      force: false,
    });
  },
});
const profileMaintenance = new ProfileMaintenanceView({
  api,
  toast,
  refresh,
  snapshot: () => state.snapshot,
  healthView: executionHealthView,
  agentConsole,
});
const agentWorkforceView = new AgentWorkforceView({
  root: $("agentsView"),
  summary: $("agentWorkforceSummary"),
  groups: $("agentWorkforceGroups"),
  onOpenAgent: (agentId, options) => profileMaintenance.openAgent(agentId, options),
  onDoctor: (agentId) => profileMaintenance.runAgentAction(agentId, "doctor"),
  onReset: (agentId) => profileMaintenance.runAgentAction(agentId, "reset-health"),
  onPromote: (agentId) => profileMaintenance.openPromotion(agentId),
  onDiagnostics: () => {
    setView("run");
    executionHealthView.open();
  },
});
const workPackageExecutionView = new WorkPackageExecutionView({
  api,
  toast,
  refreshSnapshot: () => refresh(),
  rerenderWorkPackage: (packageId) =>
    showPackageSafely(packageId, { preserveExisting: true }),
});

function applySnapshot(snapshot) {
  syncWorkflowContext(snapshot);
  state.snapshot = snapshot;
  contextNavigation.render(snapshot);
  onboardingView.render(snapshot);
  runtimeControlView.renderSnapshot(snapshot);
  if (snapshot.mode === "task") {
    const projectId = snapshot.project?.id || "project";
    $("taskRoadmapBackBtn").textContent = `← ${projectId} roadmap`;
    $("taskRoadmapBackBtn").title = `Return to the ${projectId} project roadmap`;
  }
  const lifecycleContextChanged = taskLifecycleView.setContext(
    snapshot.mode === "task" ? snapshot.project?.id || "" : "",
    snapshot.mode === "task" ? snapshot.project?.task_id || "" : "",
  );
  if (snapshot.mode === "home") {
    uiShell.snapshot = snapshot;
    return;
  }
  render(snapshot);
  if (lifecycleContextChanged && !$("planView").hidden)
    void taskLifecycleView.load({ force: true });
}

$("taskRoadmapBackBtn").addEventListener("click", async () => {
  const projectId = state.snapshot?.mode === "task" ? state.snapshot.project?.id || "" : "";
  if (!projectId) return;
  await contextNavigation.goToProject(projectId);
  // Returning from a task is an explicit project-planning action.  Force the
  // Roadmap surface even if the operator had previously visited Tasks or
  // Settings in the same project workspace.
  onboardingView.showProjectView("roadmap");
});

let refreshPromise = null;
async function refresh({ reportError = true } = {}) {
  if (refreshPromise) return refreshPromise;
  refreshPromise = (async () => {
    try {
      const snapshot = await api("/api/snapshot");
      applySnapshot(snapshot);
      return snapshot;
    } catch (error) {
      if (reportError) toast(error.message, true);
      return null;
    }
  })();
  try {
    return await refreshPromise;
  } finally {
    refreshPromise = null;
  }
}
function renderActionCenter() {
  if (!state.snapshot) return;
  uiShell.render(state.snapshot, {
    preferredPackageId: selectedPackageId() || "",
  });
}

function render(s) {
  const o = s.orchestration || {};
  const packages = s.packages || [];
  const completed = packages.filter((p) => p.stage === "completed").length;
  const ready = packages.filter(
    (p) =>
      p.status === "pending" && p.stage === "prepare" && !p.operator_paused,
  ).length;
  const active = s.assignments || [];
  const agents = s.agents || [];
  taskLifecycleView.setProviders(agents);
  const enabled = agents.filter((a) => a.enabled);
  const available = enabled.filter((a) =>
    ["available", "probe_due"].includes(a.health?.status),
  );
  const satellites = (s.nodes || []).filter((n) => n.id !== "local");
  const reachable = satellites.filter((n) => n.reachable === true).length;
  $("mState").textContent = o.state || "not initialized";
  $("mStateDetail").textContent = o.last_transition_at
    ? `Updated ${localTime(o.last_transition_at)}`
    : "No durable state";
  $("mProgress").textContent = `${completed}/${packages.length || 0}`;
  $("mProgressDetail").textContent = packages.length
    ? `${Math.round((completed / packages.length) * 100)}% complete`
    : "Initialize the plan";
  $("mWorkers").textContent = active.length;
  const wave = o.scheduler?.parallel_wave;
  $("mWave").textContent = wave
    ? `Wave ${wave.wave_id || ""} · ${(wave.package_ids || []).length} shards`
    : "No parallel wave";
  $("mReady").textContent = ready;
  const paused = packages.filter(
    (p) => p.operator_paused && p.stage !== "completed",
  ).length;
  $("mBlocked").textContent =
    `${packages.filter((p) => p.status === "blocked").length} blocked${paused ? " · " + paused + " paused" : ""}`;
  $("mAgents").textContent = `${available.length}/${enabled.length}`;
  $("mAgentsDetail").textContent = "available / enabled";
  const tokenTotals = s.token_usage?.totals || {};
  const totalTokens = Number(tokenTotals.total_tokens || 0);
  const failedTokens = Number(s.token_usage?.by_status?.failed?.total_tokens || 0);
  const cache = s.token_usage?.cache || {};
  $("mTokens").textContent = `${compactCount(totalTokens)} processed`;
  $("mTokens").title = `${totalTokens.toLocaleString()} total processed tokens`;
  const tokenDetail = [];
  if (cache.status === "reported") {
    tokenDetail.push(`${Math.round(Number(cache.hit_rate || 0) * 100)}% cache hits`);
  }
  if (failedTokens) {
    tokenDetail.push(`${failedTokens.toLocaleString()} on failed attempts`);
  }
  if (!tokenDetail.length) {
    tokenDetail.push(`${Number(tokenTotals.invocations || 0)} measured invocations`);
  }
  $("mTokensDetail").textContent = tokenDetail.join(" · ");
  const tokenParts = [
    [tokenTotals.input_tokens, "reported input"],
    [tokenTotals.estimated_input_tokens, "estimated input"],
    [tokenTotals.output_tokens, "output"],
    [tokenTotals.cache_read_tokens, "cache reads"],
    [tokenTotals.reasoning_tokens, "reasoning"],
  ].filter(([value]) => Number(value || 0) > 0);
  $("mTokensBreakdown").replaceChildren(
    ...tokenParts.map(([value, label]) => {
      const item = document.createElement("span");
      item.textContent = `${compactCount(value)} ${label}`;
      item.title = `${Number(value).toLocaleString()} ${label} tokens`;
      return item;
    }),
  );
  $("mNodes").textContent = `${reachable}/${satellites.length}`;
  $("mNodesDetail").textContent = satellites.length
    ? "reachable satellites"
    : "local agents only";
  executionHealthView.render(s);
  agentWorkforceView.render(s);
  taskLifecycleView.renderSummary(s.task_lifecycle || {});
  renderActionCenter();
  runControl.render(s.run || {}, s.run_control || {});
  renderSupervisor(s.supervisor || {}, s.run || {}, o);
  renderWorkflow();
  profileMaintenance.render(s);
  renderConfigMenu(s.configs || []);
  logs.render(s);
  renderOpenWorkPackageFromSnapshot();
}
function renderSupervisor(supervisor, run, orchestration) {
  const panel = $("supervisorPanel");
  const incident = supervisor.incident || {};
  const policy = supervisor.policy || {};
  const autoDecision = policy.auto_decision || {};
  const status =
    incident.status ||
    (supervisor.enabled
      ? supervisor.available
        ? "idle"
        : "unavailable"
      : "disabled");
  const active =
    ["open", "diagnosing", "delegating", "verifying"].includes(status) ||
    orchestration.state === "supervising";
  const waiting =
    status === "waiting_for_human" ||
    orchestration.state === "waiting_for_human_decision";
  const deterministic = supervisor.deterministic_recovery || {};
  const deterministicReady = Boolean(deterministic.available);
  const recoveryReady = Boolean(
    deterministicReady ||
      supervisor.auto_resume_lost_delegation ||
      supervisor.auto_resume_transport_failure,
  );
  const visible = active || waiting || recoveryReady;

  panel.classList.toggle("hidden", !visible);
  panel.classList.toggle("active", active || recoveryReady);
  panel.classList.toggle("waiting", waiting);
  panel.dataset.status = status;
  if (!visible) $("supervisorDetails").open = false;

  $("supervisorStatus").textContent = status.replaceAll("_", " ");
  $("supervisorStatus").className =
    `pill ${waiting ? "warn" : active || recoveryReady ? "ok" : ""}`;
  $("supervisorSubtitle").textContent =
    supervisor.error ||
    `${supervisor.agent_id || supervisor.configured_agent || "automatic"} · ${supervisor.enabled ? "enabled" : "disabled"} · ${policy.skill || policy.skill_id || "ai-supervise"}${autoDecision.enabled ? ` · auto ≥${autoDecision.minimum_weight || 0}% / +${autoDecision.minimum_margin || 0}` : ""}`;
  $("supervisorIncident").textContent = incident.incident_id || "None";
  $("supervisorPackage").textContent = incident.package_id || "—";
  $("supervisorAttempts").textContent =
    `${incident.attempts || 0} / ${policy.max_attempts_per_incident || 0}`;
  $("supervisorDelegations").textContent =
    `${incident.delegated_tasks || 0} / ${policy.max_agent_delegations || 0}`;

  const summary = deterministicReady
    ? `Deterministic review recovery is ready: ${deterministic.finding_count || 0} exact finding(s), rescue cycle ${deterministic.cycle || 0}/${deterministic.max_cycles || 0}. Broad supervision will be bypassed and the configured Supervisor agent is ${deterministic.avoid_supervisor_agent ? "excluded" : "available only as policy allows"} for the fixer campaign.`
    : recoveryReady
      ? `A delegated recovery is queued and will resume automatically. ${incident.summary || ""}`.trim()
      : incident.summary ||
        (active
          ? `${String(incident.classification || "incident").replaceAll("_", " ")} recovery is ${status.replaceAll("_", " ")}.`
          : "");
  $("supervisorSummary").textContent = summary;
  $("supervisorCollapsedSummary").textContent = waiting
    ? `${incident.package_id || "Task"} needs an operator decision`
    : recoveryReady && !active
      ? `${incident.package_id || "Task"} recovery is queued`
      : `${incident.package_id || "Task"} · ${status.replaceAll("_", " ")}`;

  const agentId = supervisor.agent_id || supervisor.configured_agent || "";
  $("openSupervisorConsole").disabled = !(
    agentId &&
    active &&
    incident.incident_id
  );
  $("openSupervisorConsole").dataset.agent = agentId;
  $("openSupervisorConsole").dataset.package = incident.package_id || "";
  const running = Boolean(run.owned_running || run.external_running);
  $("pauseSupervisor").disabled = !run.owned_running || !active;

  const decision = $("supervisorDecision");
  const question = incident.human_question || {};
  const options = Array.isArray(question.options) ? question.options : [];
  const show = waiting && !recoveryReady && question.question && options.length;
  decision.classList.toggle("hidden", !show);
  if (!show) {
    state.supervisorQuestionRenderKey = "";
    state.supervisorQuestionIdentityKey = "";
    state.supervisorQuestionRefreshPending = false;
    return;
  }

  $("supervisorQuestion").textContent = question.question;
  $("supervisorQuestionContext").textContent = question.context || "";
  const identityKey = JSON.stringify({
    incidentId: incident.incident_id || "",
    question: question.question,
    optionIds: options.map((option) => option.id),
  });
  const renderKey = JSON.stringify({ identityKey, options, question });
  if (
    renderKey !== state.supervisorQuestionRenderKey &&
    identityKey === state.supervisorQuestionIdentityKey &&
    hasActiveFormInteraction($("supervisorDecision"))
  ) {
    state.supervisorQuestionRefreshPending = true;
  } else if (renderKey !== state.supervisorQuestionRenderKey) {
    const current = document.querySelector(
      'input[name="supervisorOption"]:checked',
    )?.value;
    const preserveCurrent =
      identityKey === state.supervisorQuestionIdentityKey &&
      options.some((option) => option.id === current);
    const selectedId = preserveCurrent
      ? current
      : question.recommended_option || options[0]?.id || "";
    $("supervisorOptions").innerHTML = options
      .map((option) => {
        const hasWeight =
          option.weight !== null &&
          option.weight !== undefined &&
          option.weight !== "";
        const weight =
          hasWeight && Number.isFinite(Number(option.weight))
            ? `${Number(option.weight)}%`
            : "";
        const risk =
          option.risk && option.risk !== "unknown"
            ? String(option.risk).replaceAll("_", " ")
            : "";
        const meta = [weight, risk].filter(Boolean).join(" · ");
        return `<label class="supervisor-option"><input type="radio" name="supervisorOption" value="${esc(option.id)}" ${selectedId === option.id ? "checked" : ""}><span><strong>${esc(option.label)}${meta ? `<em class="supervisor-option-meta">${esc(meta)}</em>` : ""}</strong>${option.consequence ? `<small>${esc(option.consequence)}</small>` : ""}</span></label>`;
      })
      .join("");
    state.supervisorQuestionIdentityKey = identityKey;
    state.supervisorQuestionRenderKey = renderKey;
    state.supervisorQuestionRefreshPending = false;
  }
  $("submitSupervisorAnswer").disabled = running;
}

function flushPendingSupervisorQuestionRefresh() {
  if (!state.supervisorQuestionRefreshPending) return;
  if (hasActiveFormInteraction($("supervisorDecision"))) return;
  const snapshot = state.snapshot || {};
  state.supervisorQuestionRefreshPending = false;
  renderSupervisor(
    snapshot.supervisor || {},
    snapshot.run || {},
    snapshot.orchestration || {},
  );
}

async function submitSupervisorDecision(event) {
  event.preventDefault();
  const selected = document.querySelector(
    'input[name="supervisorOption"]:checked',
  );
  if (!selected) {
    toast("Select one Supervisor option.", true);
    return;
  }
  const button = $("submitSupervisorAnswer");
  button.disabled = true;
  try {
    const result = await api("/api/supervisor/answer", {
      method: "POST",
      body: JSON.stringify({
        option_id: selected.value,
        message: $("supervisorGuidance").value,
      }),
    });
    $("supervisorGuidance").value = "";
    toast(result.stdout.trim() || "Supervisor decision recorded");
    await refresh();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

function workflowPackageRenderState(packageInfo) {
  return {
    id: packageInfo.id,
    title: packageInfo.title,
    stage: packageInfo.stage,
    status: packageInfo.status,
    dependencies: packageInfo.dependencies,
    parent_id: packageInfo.parent_id,
    parallel_safe: packageInfo.parallel_safe,
    priority: packageInfo.priority,
    computed_complexity: packageInfo.computed_complexity,
    complexity: packageInfo.complexity,
    decomposition_status: packageInfo.decomposition_status,
    operator_paused: packageInfo.operator_paused,
    operator_pause_reason: packageInfo.operator_pause_reason,
    pause_before_start: packageInfo.pause_before_start,
    pause_before_start_reached_at: packageInfo.pause_before_start_reached_at,
    decomposition_required: packageInfo.decomposition_required,
    directive_pending_sync: packageInfo.directive_pending_sync,
    repository_sync_requested: packageInfo.repository_sync_requested,
    upstream_sync_summary: packageInfo.upstream_sync_summary,
    kind: packageInfo.kind,
    decomposition_agent_id: packageInfo.decomposition_agent_id,
    final_reviewer_id: packageInfo.final_reviewer_id,
    reviewer_id: packageInfo.reviewer_id,
    last_fixer_id: packageInfo.last_fixer_id,
    agent_id: packageInfo.agent_id,
  };
}

function scheduleWorkflowNavigation() {
  if (
    state.workflowNavigationFrame ||
    !workbenchNavigation.snapshot().navigationRequest
  )
    return;
  state.workflowNavigationFrame = requestAnimationFrame(() => {
    state.workflowNavigationFrame = 0;
    const request = workbenchNavigation.consumeNavigationRequest();
    if (!request || request.kind !== "locate") return;
    const view = workflowViewMode() === "graph" ? workflowGraph : workflowList;
    view.focus(request.targetId, { smooth: request.reason !== "first-open" });
  });
}

function requestWorkflowLocate(reason = "operator") {
  const navigation = workbenchNavigation.snapshot();
  const targetId = navigation.selectedId || navigation.activeIds[0] || "";
  if (!targetId) return;
  workbenchNavigation.requestLocate(targetId, { reason });
  scheduleWorkflowNavigation();
}

function scheduleInitialGraphView(packages) {
  if (state.workflowGraphInitialized || !packages.length) return;
  state.workflowGraphInitialized = true;
  if (workbenchNavigation.snapshot().navigationRequest) {
    scheduleWorkflowNavigation();
    return;
  }
  requestAnimationFrame(() => workflowViewport.fit());
}

function renderWorkflow() {
  const orchestration = state.snapshot?.orchestration || {};
  const wave = orchestration.scheduler?.parallel_wave || {};
  const workingIds = [
    ...new Set(
      (state.snapshot?.assignments || [])
        .map((item) => item.package_id)
        .filter(Boolean),
    ),
  ];
  workbenchNavigation.setActive(workingIds);
  const navigation = workbenchNavigation.snapshot();
  const currentLabel = $("workflowCurrentLabel");
  currentLabel.textContent = workingIds.length
    ? `Working now: ${workingIds.join(", ")}`
    : "No active Work Package";
  currentLabel.classList.toggle("active", workingIds.length > 0);

  const graphMode = navigation.viewMode === "graph";
  $("workflowList").classList.toggle("hidden", graphMode);
  $("workflowGraphView").classList.toggle("hidden", !graphMode);
  $("workflowGraphControls").classList.toggle("hidden", !graphMode);
  $("workflowViewMode")
    .querySelectorAll("[data-workflow-view]")
    .forEach((button) =>
      button.setAttribute(
        "aria-pressed",
        String(button.dataset.workflowView === navigation.viewMode),
      ),
    );
  $("workflowFollowActive").setAttribute(
    "aria-pressed",
    String(navigation.followActive),
  );
  $("workflowLocateBtn").disabled = !(navigation.selectedId || workingIds[0]);

  const packages = state.snapshot?.packages || [];
  const controlsLocked = Boolean(
    state.snapshot?.run?.owned_running || state.snapshot?.run?.external_running,
  );
  const renderKey = JSON.stringify({
    packages: packages.map(workflowPackageRenderState),
    filter: state.filter,
    workflowViewMode: navigation.viewMode,
    workflowEdgeMode: state.workflowEdgeMode,
    selectedPackage: navigation.selectedId,
    activeParallelIds: wave.package_ids || [],
    workingIds,
    controlsLocked,
  });
  if (renderKey !== state.workflowRenderKey) {
    const common = {
      packages,
      filter: state.filter,
      selectedId: navigation.selectedId,
      activeParallelIds: wave.package_ids || [],
      workingPackageIds: workingIds,
      controlsLocked,
    };
    if (graphMode) {
      workflowGraph.render({
        ...common,
        edgeMode: state.workflowEdgeMode,
        controlsLocked,
      });
    } else {
      workflowList.render(common);
    }
    state.workflowRenderKey = renderKey;
  }

  if (graphMode) scheduleInitialGraphView(packages);
  scheduleWorkflowNavigation();
}

function workPackageDetailErrorMessage(error) {
  const message = String(error?.message || error || "Unknown rendering error");
  return truncate(message, 500);
}

function renderWorkPackageDetailError(packageId, error, { preserveExisting = false } = {}) {
  const detail = $("packageDetail");
  const message = workPackageDetailErrorMessage(error);
  if (!preserveExisting || !detail.childElementCount) {
    detail.className = "detail-body";
    detail.innerHTML = `<div class="message bad"><strong>Work Package details could not be rendered.</strong><br>${esc(message)}</div>`;
  }
  const errorKey = `${packageId}:${message}`;
  if (state.workPackageDetailErrorKey !== errorKey) {
    state.workPackageDetailErrorKey = errorKey;
    console.error(`Failed to render Work Package ${packageId}`, error);
    toast(`Could not render ${packageId}: ${message}`, true);
  }
}

function showPackageSafely(packageId, { preserveExisting = false } = {}) {
  const renderState =
    preserveExisting && workPackageInspector.isOpenFor(packageId)
      ? workPackageInspector.captureRenderState()
      : null;
  try {
    const packageInfo = showPackage(packageId);
    if (!packageInfo) {
      renderWorkPackageDetailError(
        packageId,
        new Error("This Work Package is no longer present in the current snapshot."),
        { preserveExisting },
      );
      return null;
    }
    state.workPackageDetailErrorKey = "";
    if (renderState) workPackageInspector.restoreRenderState(renderState);
    else workPackageInspector.syncRenderedPanels();
    return packageInfo;
  } catch (error) {
    state.workPackageDetailRenderKey = "";
    state.workPackageDetailRefreshPending = false;
    renderWorkPackageDetailError(packageId, error, { preserveExisting });
    return null;
  }
}

function renderOpenWorkPackageFromSnapshot() {
  const packageId = selectedPackageId();
  if (!packageId || !workPackageInspector.isOpenFor(packageId)) return;
  const key = workPackageDetailSnapshotKey(state.snapshot, packageId);
  if (!key) {
    workPackageInspector.setHeading(
      packageId,
      "This Work Package is no longer present in the current task snapshot.",
    );
    renderWorkPackageDetailError(
      packageId,
      new Error("This Work Package is no longer present in the current snapshot."),
    );
    state.workPackageDetailRenderKey = "";
    return;
  }

  // The execution trace is safe to refresh independently because it does not
  // replace any operator-owned form controls in the assignment editor.
  void executionTraceView.load(packageId, { force: true });
  if (key === state.workPackageDetailRenderKey) return;
  if (hasActiveFormInteraction($("packageDetail"))) {
    state.workPackageDetailRefreshPending = true;
    return;
  }
  showPackageSafely(packageId, { preserveExisting: true });
}

function flushPendingWorkPackageDetailRefresh() {
  if (!state.workPackageDetailRefreshPending || !selectedPackageId()) return;
  if (hasActiveFormInteraction($("packageDetail"))) return;
  state.workPackageDetailRefreshPending = false;
  showPackageSafely(selectedPackageId(), { preserveExisting: true });
}

function isFutureWorkPackage(packageInfo) {
  return (
    packageInfo?.stage === "prepare" && packageInfo?.status === "pending"
  );
}

function showPackage(id) {
  const packages = state.snapshot?.packages || [];
  const p = packages.find((x) => x.id === id);
  if (!p) return null;
  const runActive = !!(
    state.snapshot?.run?.owned_running || state.snapshot?.run?.external_running
  );
  const agent = assignedAgent(p) || "";
  const dependencies = (p.dependencies || [])
    .map((dependencyId) => packages.find((item) => item.id === dependencyId))
    .filter(Boolean);
  const dependents = packages.filter((item) =>
    (item.dependencies || []).includes(p.id),
  );
  const parent = p.parent_id
    ? packages.find((item) => item.id === p.parent_id)
    : null;
  const shardIds = new Set([
    ...(p.shard_ids || []),
    ...packages
      .filter((item) => item.parent_id === p.id)
      .map((item) => item.id),
  ]);
  const shards = [...shardIds]
    .map((shardId) => packages.find((item) => item.id === shardId))
    .filter(Boolean);
  const criteria = p.acceptance_criteria || [];
  const verifiedCriteria = criteria.filter((item) => item.verified).length;
  const requirements = p.requirements || [];
  const findings = p.review_findings || [];
  workPackageInspector.setHeading(
    `${p.id} — ${p.title}`,
    `${p.stage || "unknown"} · ${p.status || "unknown"}${agent ? ` · ${agent}` : " · automatic assignment"}`,
  );
  const future = isFutureWorkPackage(p);
  const syncEligible =
    p.stage !== "completed" && !p.parent_id && p.kind !== "repository_sync";
  $("syncWorkPackageBtn").disabled = !syncEligible;
  $("syncWorkPackageBtn").textContent = p.repository_sync_requested
    ? "Edit sync request"
    : future
      ? "Sync before"
      : "Pause & Sync";
  $("syncWorkPackageBtn").classList.toggle(
    "primary",
    !!p.repository_sync_requested,
  );
  $("decomposeBtn").disabled = future
    ? !!p.parent_id || !!p.decomposition_status
    : !!p.parent_id ||
      p.stage === "completed" ||
      !!p.decomposition_status ||
      runActive;
  $("decomposeBtn").textContent = future
    ? p.decomposition_required
      ? "Remove required decomposition"
      : "Require decomposition"
    : "Decompose now";
  $("decomposeBtn").classList.toggle(
    "primary",
    future && !!p.decomposition_required,
  );
  $("pauseWorkPackageBtn").disabled = future
    ? p.stage === "completed" || !!p.pause_before_start_reached_at
    : p.stage === "completed" || runActive;
  $("pauseWorkPackageBtn").textContent = future
    ? p.pause_before_start_reached_at
      ? "Entry pause reached"
      : p.pause_before_start
        ? "Remove entry pause"
        : "Pause before start"
    : p.operator_paused
      ? "Resume"
      : "Pause now";
  $("pauseWorkPackageBtn").classList.toggle(
    "primary",
    future ? !!p.pause_before_start : !!p.operator_paused,
  );
  $("openWorkPackageAgentBtn").disabled = !agent;
  $("openWorkPackageAgentBtn").dataset.agent = agent;
  $("packageDetail").className = "detail-body";
  $("packageDetail").innerHTML = `
    <section id="workPackageOverviewPanel" class="work-package-inspector-pane" role="tabpanel" aria-labelledby="workPackageOverviewTab" data-inspector-panel="overview">
      <div class="work-package-summary-grid compact">
        <div><span>State</span><strong>${esc(p.stage)}</strong><small>${esc(p.status)}</small></div>
        <div><span>Complexity</span><strong>${esc(p.computed_complexity ?? p.complexity ?? "—")}</strong><small>${esc(p.risk)} risk · priority ${esc(p.priority ?? 0)}</small></div>
        <div><span>Acceptance</span><strong>${esc(verifiedCriteria)}/${esc(criteria.length)} verified</strong><small>${esc(p.verification_attempts ?? 0)} verification attempts</small></div>
        <div><span>Structure</span><strong>${esc(dependencies.length)} dependencies</strong><small>${esc(shards.length)} direct shard${shards.length === 1 ? "" : "s"}</small></div>
      </div>
      ${p.operator_paused ? `<div class="work-package-pause-banner"><strong>Paused by operator</strong><span>${esc(p.operator_pause_reason || "No reason recorded")}${p.operator_paused_at ? ` · ${esc(localTime(p.operator_paused_at))}` : ""}</span></div>` : ""}
      ${p.pause_before_start ? `<div class="work-package-directive-banner pause"><strong>Pause scheduled before start</strong><span>${esc(p.pause_before_start_reason || "The orchestrator will stop when this Work Package first becomes ready.")}${p.pause_before_start_requested_at ? ` · requested ${esc(localTime(p.pause_before_start_requested_at))}` : ""}</span></div>` : ""}
      ${p.pause_before_start_reached_at ? `<div class="work-package-directive-banner reached"><strong>Entry pause reached</strong><span>The orchestrator stopped before this Work Package started · ${esc(localTime(p.pause_before_start_reached_at))}</span></div>` : ""}
      ${p.decomposition_required ? `<div class="work-package-directive-banner"><strong>Mandatory decomposition scheduled</strong><span>${esc(p.decomposition_required_reason || "A validated decomposition pass will run before implementation.")}${p.decomposition_required_at ? ` · requested ${esc(localTime(p.decomposition_required_at))}` : ""}</span></div>` : ""}
      ${(p.directive_pending_sync || []).length ? `<div class="preference-help">Directive update is queued and will be consumed by the orchestrator at the next safe package boundary.</div>` : ""}
      <section class="work-package-detail-section relationship-section">
        <div class="work-package-section-head"><div><h4>Workflow relationships</h4><div class="preference-help">Open any connected Work Package or shard without leaving the workbench.</div></div></div>
        <div class="work-package-relationship-grid">
          <div><h5>Depends on</h5><div class="relationship-chips">${packageRelationList(dependencies, "No prerequisites")}</div></div>
          <div><h5>Unlocks</h5><div class="relationship-chips">${packageRelationList(dependents, "No dependents")}</div></div>
          <div><h5>Parent</h5><div class="relationship-chips">${packageRelationButton(parent)}</div></div>
          <div><h5>Direct shards</h5><div class="relationship-chips">${packageRelationList(shards, "No generated shards")}</div></div>
        </div>
        ${shards.length ? `<div class="shard-detail-list">${shards.map((shard) => `<button class="shard-detail-card" data-open-package="${esc(shard.id)}"><span><strong>${esc(shard.id)}</strong><small>${esc(shard.stage)} · ${esc(shard.status)}</small></span><span>${esc(truncate(shard.title, 64))}</span><i>${shard.operator_paused ? "Paused" : shard.parallel_safe ? "Parallel safe" : "Serial"}</i></button>`).join("")}</div>` : ""}
      </section>
      <section class="work-package-detail-section">
        <div class="work-package-section-head"><div><h4>Contract and acceptance</h4><div class="preference-help">Durable requirements used by implementation, verification, and review.</div></div></div>
        <div class="work-package-contract-grid">
          <div><h5>Requirements</h5>${contractList(requirements, "No explicit requirements declared.")}</div>
          <div><h5>Acceptance criteria</h5>${acceptanceCriteriaMarkup(criteria)}</div>
        </div>
      </section>
      <details class="work-package-technical"><summary>Scope, decomposition, and counters</summary><dl class="kv"><dt>Parent</dt><dd>${esc(p.parent_id || "—")}</dd><dt>Shard key</dt><dd>${esc(p.shard_key || "—")}</dd><dt>Dependencies</dt><dd>${esc((p.dependencies || []).join(", ") || "none")}</dd><dt>Repositories</dt><dd>${esc((p.affected_repositories || []).join(", ") || "unspecified")}</dd><dt>Read scope</dt><dd>${esc((p.read_scope || []).join(", ") || "unspecified")}</dd><dt>Write scope</dt><dd>${esc((p.write_scope || []).join(", ") || "unspecified")}</dd><dt>Conflict keys</dt><dd>${esc((p.conflict_keys || []).join(", ") || "none")}</dd><dt>Decomposition origin</dt><dd>${esc(p.decomposition_origin_stage || "—")}</dd><dt>Decomposition reason</dt><dd>${esc(p.decomposition_reason || "—")}</dd><dt>Pause before start</dt><dd>${esc(p.pause_before_start ? p.pause_before_start_reason || "scheduled" : "not scheduled")}</dd><dt>Mandatory decomposition</dt><dd>${esc(p.decomposition_required ? p.decomposition_required_reason || "required" : p.decomposition_required_consumed_at ? `consumed ${localTime(p.decomposition_required_consumed_at)}` : "not required")}</dd><dt>Recovery cycles</dt><dd>${esc(p.review_recovery_cycles ?? 0)}${p.review_recovery_origin_stage ? ` · from ${esc(p.review_recovery_origin_stage)}` : ""}</dd></dl></details>
    </section>
    <section id="workPackageExecutionPanel" class="work-package-inspector-pane" role="tabpanel" aria-labelledby="workPackageExecutionTab" data-inspector-panel="execution" hidden>
      <div class="work-package-summary-grid compact">
        <div><span>Execution</span><strong>${esc(p.execution_mode || "standard")}</strong><small>${p.parallel_safe ? "parallel safe" : "serial"} · ${esc(p.verification_profile || "targeted")}</small></div>
        <div><span>Agent</span><strong>${esc(agent || "Automatic")}</strong><small>${esc(p.operator_paused ? "operator paused" : "scheduler controlled")}</small></div>
        <div><span>Verification</span><strong>${esc(p.verification_attempts ?? 0)} attempts</strong><small>${esc(verifiedCriteria)}/${esc(criteria.length)} criteria verified</small></div>
        <div><span>Review</span><strong>${esc(p.review_cycles ?? 0)} cycles</strong><small>${esc(findings.length)} open finding${findings.length === 1 ? "" : "s"}</small></div>
      </div>
      ${workPackageExecutionView.render(state.snapshot, p, { runActive })}
    </section>
    <section id="workPackageEvidencePanel" class="work-package-inspector-pane" role="tabpanel" aria-labelledby="workPackageEvidenceTab" data-inspector-panel="evidence" hidden>
      <section class="work-package-detail-section execution-trace-section">
        <div class="work-package-section-head"><div><h4>Execution trace</h4><div class="preference-help">Stage progression first; attempts, fallbacks, and diagnostics remain available on demand.</div></div><span class="pill">flow view</span></div>
        <div id="executionTraceBody" class="execution-trace-body" aria-live="polite"><div class="execution-trace-loading"><span></span>Loading stage history…</div></div>
      </section>
      ${p.implementation_summary ? `<section class="work-package-detail-section"><h4>Implementation summary</h4><p class="work-package-long-summary">${esc(p.implementation_summary)}</p></section>` : ""}
      ${findings.length ? `<section class="work-package-detail-section"><h4>Review findings</h4>${contractList(findings, "No review findings.")}</section>` : ""}
      <section class="work-package-detail-section evidence-section">
        <div class="work-package-section-head"><div><h4>Recent stage evidence</h4><div class="preference-help">Compact durable handoff context; full agent output remains in invocation artifacts.</div></div></div>
        <div class="work-package-output-list">
          ${structuredEvidence("Latest implementation", p.last_implementation)}
          ${structuredEvidence("Latest verification", p.last_verification)}
          ${structuredEvidence("Latest review", p.last_review)}
          ${structuredEvidence("Recent agent attempts", (p.last_agent_attempts || []).slice(-5))}
          ${!hasStructuredContent(p.last_implementation) && !hasStructuredContent(p.last_verification) && !hasStructuredContent(p.last_review) && !(p.last_agent_attempts || []).length ? '<div class="relationship-empty">No durable stage evidence has been recorded yet.</div>' : ""}
        </div>
      </section>
    </section>`;
  $("packageDetail")
    .querySelectorAll("[data-open-package]")
    .forEach((button) =>
      button.addEventListener("click", () => {
        const targetId = button.dataset.openPackage;
        if (!targetId) return;
        openWorkPackageInspector(targetId, { tab: "overview" });
      }),
    );
  workPackageExecutionView.bind($("workPackageExecutionPanel"), state.snapshot, p);
  state.workPackageDetailRefreshPending = false;
  state.workPackageDetailRenderKey = workPackageDetailSnapshotKey(state.snapshot, p.id);
  void executionTraceView.load(p.id, { force: true });
  return p;
}
function refreshWorkPackageSelectionVisuals() {
  renderActionCenter();
  renderWorkflow();
}

function openWorkPackageInspector(id, { tab = "overview" } = {}) {
  selectPackage(id);

  // Open the stable side surface before rendering so a rich-render failure is
  // still visible and never turns a workPackage click into an apparent no-op.
  workPackageInspector.open({
    packageId: id,
    title: `${id} — loading…`,
    meta: "Loading durable Work Package state…",
    tab,
  });
  const packageInfo = showPackageSafely(id);
  refreshWorkPackageSelectionVisuals();
  if (!packageInfo) return;
  workPackageInspector.setTab(tab, { focus: false, restoreScroll: false });
  $("packageDetail").scrollTop = 0;
}

async function setWorkPackageDirective(kind, enabled) {
  const packageId = selectedPackageId();
  if (!packageId) return;
  const p = (state.snapshot?.packages || []).find(
    (item) => item.id === packageId,
  );
  if (!p) return;
  const isPause = kind === "pause_before_start";
  let reason = "";
  if (enabled) {
    const value = window.prompt(
      isPause
        ? `Why pause before ${packageId} starts?`
        : `Why must ${packageId} be decomposed before implementation?`,
      isPause
        ? "Review before starting this Work Package"
        : "Mandatory decomposition requested by operator",
    );
    if (value === null) return;
    reason = value.trim();
  }
  const description = isPause
    ? "entry hold"
    : "mandatory decomposition";
  if (
    !confirm(
      `${enabled ? "Schedule" : "Remove"} ${description} for ${packageId}?`,
    )
  )
    return;
  try {
    const result = await api("/api/package/directive", {
      method: "POST",
      body: JSON.stringify({
        package_id: packageId,
        kind,
        enabled,
        reason,
      }),
    });
    toast(
      `${description} ${enabled ? "scheduled" : "removed"}${
        result.queued_while_running ? " · queued for the next safe boundary" : ""
      }`,
    );
    await refresh();
  } catch (e) {
    toast(e.message, true);
  }
}
async function decomposeWorkPackage() {
  if (!selectedPackageId()) return;
  const p = (state.snapshot?.packages || []).find(
    (item) => item.id === selectedPackageId(),
  );
  if (!p) return;
  if (isFutureWorkPackage(p)) {
    await setWorkPackageDirective(
      "require_decomposition",
      !p.decomposition_required,
    );
    return;
  }
  if (
    !confirm(
      `Decompose ${selectedPackageId()} into scheduler-managed shards now?`,
    )
  )
    return;
  try {
    const result = await api("/api/decompose", {
      method: "POST",
      body: JSON.stringify({ package_id: selectedPackageId() }),
    });
    toast(result.stdout.trim() || "Decomposition completed");
    await refresh();
  } catch (e) {
    toast(e.message, true);
  }
}
async function changeWorkPackagePause(paused) {
  const packageId = selectedPackageId();
  if (!packageId) return;
  const p = (state.snapshot?.packages || []).find(
    (item) => item.id === packageId,
  );
  if (!p) return;
  if (isFutureWorkPackage(p)) {
    await setWorkPackageDirective("pause_before_start", paused);
    return;
  }
  let reason = "";
  if (paused) {
    const value = window.prompt(
      `Why pause ${packageId}?`,
      "Paused by operator",
    );
    if (value === null) return;
    reason = value.trim();
  }
  const verb = paused ? "Pause" : "Resume";
  if (
    !confirm(
      `${verb} ${packageId}${!p.parent_id ? " and its direct incomplete shards" : ""}?`,
    )
  )
    return;
  try {
    const result = await api("/api/package/pause", {
      method: "POST",
      body: JSON.stringify({
        package_id: packageId,
        paused,
        reason,
        apply_to_shards: !p.parent_id,
      }),
    });
    toast(
      result.stdout.trim() || `${packageId} ${paused ? "paused" : "resumed"}`,
    );
    await refresh();
  } catch (e) {
    toast(e.message, true);
  }
}

function workPackageSyncSelection() {
  const repositories = [];
  const sourceBranches = {};
  document
    .querySelectorAll("#workPackageSyncRepositories [data-sync-repository]")
    .forEach((row) => {
      const id = row.dataset.syncRepository || "";
      const checkbox = row.querySelector("[data-sync-select]");
      const branch = row.querySelector("[data-sync-branch]");
      if (!id || !checkbox?.checked) return;
      repositories.push(id);
      const selectedBranch = branch?.value || row.dataset.baseBranch || "";
      if (selectedBranch && selectedBranch !== (row.dataset.baseBranch || ""))
        sourceBranches[id] = selectedBranch;
    });
  return { repositories, sourceBranches };
}

function updateWorkPackageSyncRowWarning(row) {
  const branch = row.querySelector("[data-sync-branch]")?.value || "";
  const base = row.dataset.baseBranch || "";
  const warning = row.querySelector("[data-sync-warning]");
  if (!warning) return;
  const overridden = !!branch && !!base && branch !== base;
  warning.classList.toggle("hidden", !overridden);
  warning.textContent = overridden
    ? `Non-base branch: ${branch} (configured base: ${base}). This override is recorded in synchronization evidence.`
    : "";
}

function renderWorkPackageSyncOptions(data) {
  state.workPackageSync.options = data;
  const host = $("workPackageSyncRepositories");
  const pending = data.pending_request || {};
  $("workPackageSyncConflictPolicy").value = pending.conflict_policy || "ai_resolve";
  $("workPackageSyncAutoResume").checked = pending.auto_resume !== false;
  const mode = data.mode === "after" ? "after" : "before";
  $("workPackageSyncTitle").textContent =
    mode === "after" ? `Pause & Sync after ${data.package_id}` : `Sync before ${data.package_id}`;
  $("workPackageSyncSubtitle").textContent =
    mode === "after"
      ? "The current Work Package finishes first; synchronization starts only at the next safe boundary."
      : "Synchronization is inserted before this Work Package and runs before development continues.";
  $("confirmWorkPackageSync").textContent =
    mode === "after" ? "Pause & Sync" : "Sync before";
  const repositories = data.repositories || [];
  if (!repositories.length) {
    host.innerHTML = '<div class="empty">No task-owned repositories are available for synchronization.</div>';
    return;
  }
  host.innerHTML = repositories
    .map((repo) => {
      const base = repo.configured_base_branch || "";
      const selectedBranch = repo.selected_source_branch || base;
      const branches = [...new Set([base, selectedBranch, ...(repo.branches || []).map((item) => item.branch || item.name || item)].filter(Boolean))];
      const divergence = repo.divergence || {};
      const checked = repo.selected ? "checked" : "";
      return `<section class="work-package-sync-repository" data-sync-repository="${esc(repo.id)}" data-base-branch="${esc(base)}">
        <label class="work-package-sync-repository-main"><input type="checkbox" data-sync-select ${checked}><span><strong>${esc(repo.id)}</strong><small>Target: ${esc(repo.task_branch || "task branch")}</small></span></label>
        <label class="work-package-sync-branch-label">From
          <select data-sync-branch aria-label="Upstream branch for ${esc(repo.id)}">${branches.map((branch) => `<option value="${esc(branch)}" ${branch === selectedBranch ? "selected" : ""}>${esc(`origin/${branch}`)}${branch === base ? " · configured base" : ""}</option>`).join("")}</select>
        </label>
        <div class="work-package-sync-divergence"><span>↑ ${esc(divergence.ahead ?? "—")}</span><span>↓ ${esc(divergence.behind ?? "—")}</span></div>
        <div class="work-package-sync-warning hidden" data-sync-warning></div>
      </section>`;
    })
    .join("");
  host.querySelectorAll("[data-sync-branch]").forEach((select) => {
    const row = select.closest("[data-sync-repository]");
    select.addEventListener("change", () => updateWorkPackageSyncRowWarning(row));
    updateWorkPackageSyncRowWarning(row);
  });
}

async function openWorkPackageSyncDialog(packageId, { refreshBranches = false } = {}) {
  if (!packageId || state.workPackageSync.busy) return;
  state.workPackageSync.packageId = packageId;
  $("workPackageSyncRepositories").innerHTML = '<div class="empty">Loading repositories…</div>';
  $("workPackageSyncPreview").className = "work-package-sync-preview empty";
  $("workPackageSyncPreview").textContent = "Preview selected branch divergence before confirming.";
  const dialog = $("workPackageSyncDialog");
  if (!dialog.open) dialog.showModal();
  try {
    const data = await api(`/api/task/repository-sync/options?package_id=${encodeURIComponent(packageId)}&refresh=${refreshBranches ? "1" : "0"}`);
    renderWorkPackageSyncOptions(data);
  } catch (error) {
    $("workPackageSyncRepositories").innerHTML = `<div class="message error">${esc(error.message)}</div>`;
  }
}

async function previewWorkPackageSync() {
  const packageId = state.workPackageSync.packageId;
  const selection = workPackageSyncSelection();
  if (!selection.repositories.length) return toast("Select at least one repository.", true);
  state.workPackageSync.busy = true;
  try {
    const result = await api("/api/task/repository-sync/preview", {
      method: "POST",
      body: JSON.stringify({ package_id: packageId, ...selection, source_branches: selection.sourceBranches, remote: "origin" }),
    });
    const rows = result.repositories || [];
    $("workPackageSyncPreview").className = "work-package-sync-preview";
    $("workPackageSyncPreview").innerHTML = rows.map((row) => `<div><strong>${esc(row.repository_id)}</strong><span>${esc(`origin/${row.source_branch}`)} → ${esc(row.target_branch || "task branch")}</span><b>↑ ${esc(row.ahead ?? 0)} · ↓ ${esc(row.behind ?? 0)}</b>${row.source_selection === "operator_override" ? '<em>operator branch override</em>' : ""}</div>`).join("") || "No divergence data returned.";
  } catch (error) {
    toast(error.message, true);
  } finally {
    state.workPackageSync.busy = false;
  }
}

async function submitWorkPackageSync() {
  const packageId = state.workPackageSync.packageId;
  const selection = workPackageSyncSelection();
  if (!selection.repositories.length) return toast("Select at least one repository.", true);
  state.workPackageSync.busy = true;
  $("confirmWorkPackageSync").disabled = true;
  try {
    const result = await api("/api/task/repository-sync/request", {
      method: "POST",
      body: JSON.stringify({
        package_id: packageId,
        repositories: selection.repositories,
        source_branches: selection.sourceBranches,
        remote: "origin",
        conflict_policy: $("workPackageSyncConflictPolicy").value,
        auto_resume: $("workPackageSyncAutoResume").checked,
      }),
    });
    $("workPackageSyncDialog").close();
    toast(result.queued_while_running ? "Pause & Sync queued for the next safe boundary." : "Synchronization queued. Starting orchestration…");
    await refresh();
    if (!result.queued_while_running) {
      try {
        await api("/api/run/start", { method: "POST", body: JSON.stringify({}) });
      } catch (error) {
        toast(`Synchronization request is queued; run start needs attention: ${error.message}`, true);
      }
    }
  } catch (error) {
    toast(error.message, true);
  } finally {
    state.workPackageSync.busy = false;
    $("confirmWorkPackageSync").disabled = false;
  }
}

function handleWorkPackageAction(action, id) {
  if (["select", "details", "assignment", "execution"].includes(action)) {
    openWorkPackageInspector(id, {
      tab: ["assignment", "execution"].includes(action) ? "execution" : "overview",
    });
    return;
  }

  selectPackage(id);
  refreshWorkPackageSelectionVisuals();
  switch (action) {
    case "decompose":
      decomposeWorkPackage();
      break;
    case "require-decomposition":
      setWorkPackageDirective("require_decomposition", true);
      break;
    case "remove-decomposition-requirement":
      setWorkPackageDirective("require_decomposition", false);
      break;
    case "pause-before-start":
      setWorkPackageDirective("pause_before_start", true);
      break;
    case "remove-pause-before-start":
      setWorkPackageDirective("pause_before_start", false);
      break;
    case "pause":
      changeWorkPackagePause(true);
      break;
    case "repository-sync":
      openWorkPackageSyncDialog(id);
      break;
    case "resume":
      changeWorkPackagePause(false);
      break;
    case "agent": {
      const packageInfo = (state.snapshot?.packages || []).find(
        (item) => item.id === id,
      );
      const agent = assignedAgent(packageInfo || {});
      if (agent)
        agentConsole.open(agent, {
          packageId: id,
          stage: packageInfo?.stage || "",
        });
      break;
    }
    default:
      break;
  }
}
function renderConfigMenu(configs) {
  const renderKey = JSON.stringify({ configs, selected: state.configKey });
  if (renderKey === state.configMenuRenderKey) return;
  $("configMenu").innerHTML = configs
    .map(
      (c) =>
        `<button class="config-item ${state.configKey === c.key ? "active" : ""}" data-key="${esc(c.key)}"><strong>${esc(c.label)}</strong><small>${esc(c.relative_path)}</small></button>`,
    )
    .join("");
  state.configMenuRenderKey = renderKey;
}
async function loadConfig(key) {
  try {
    const data = await api("/api/config?name=" + encodeURIComponent(key));
    state.configKey = key;
    state.configSha = data.sha256;
    $("configTitle").textContent = data.label;
    $("configPath").textContent = data.relative_path;
    $("configMeta").textContent =
      `${data.size} bytes · modified ${localTime(data.modified_at)}${data.requires_idle ? " · requires idle orchestrator" : ""}`;
    $("configEditor").value = data.content;
    $("configEditor").disabled = false;
    $("reloadConfigBtn").disabled = false;
    $("validateConfigBtn").disabled = false;
    $("saveConfigBtn").disabled = false;
    $("configMessage").className = "message";
    $("configMessage").textContent =
      "Loaded from disk. Saves are atomic and create a backup.";
    renderConfigMenu(state.snapshot.configs || []);
  } catch (e) {
    toast(e.message, true);
  }
}
async function validateConfig() {
  if (!state.configKey) return;
  const result = await api("/api/config/validate", {
    method: "POST",
    body: JSON.stringify({
      name: state.configKey,
      content: $("configEditor").value,
    }),
  });
  $("configMessage").className = "message " + (result.valid ? "ok" : "bad");
  $("configMessage").textContent = result.valid
    ? "Validation passed."
    : result.error;
  return result.valid;
}
async function saveConfig() {
  try {
    if (!(await validateConfig())) return;
    const data = await api("/api/config/save", {
      method: "POST",
      body: JSON.stringify({
        name: state.configKey,
        content: $("configEditor").value,
        sha256: state.configSha,
      }),
    });
    state.configSha = data.sha256;
    $("configMessage").className = "message ok";
    $("configMessage").textContent = `Saved. Backup: ${data.backup}`;
    toast("Configuration saved");
    await refresh();
  } catch (e) {
    toast(e.message, true);
    $("configMessage").className = "message bad";
    $("configMessage").textContent = e.message;
  }
}
function setView(name) {
  const primaryViews = new Set(["run", "agents", "plan", "changes"]);
  const utilityViews = new Set(["logs", "config"]);
  const selected = primaryViews.has(name) || utilityViews.has(name) ? name : "run";
  if (selected !== "run") executionHealthView.close({ restoreFocus: false });
  document.querySelectorAll(".tabs [role=tab]").forEach((tab) => {
    const active = tab.dataset.view === selected;
    const keyboardFallback = utilityViews.has(selected) && tab.dataset.view === "run";
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active || keyboardFallback ? 0 : -1;
  });
  document.querySelectorAll("#mainContent > .view").forEach((view) => {
    const active = view.id === `${selected}View`;
    view.classList.toggle("active", active);
    view.hidden = !active;
  });
  const moreMenu = $("taskMoreMenu");
  moreMenu.classList.toggle("active", utilityViews.has(selected));
  moreMenu.dataset.activeView = utilityViews.has(selected) ? selected : "";
  logs.setActive(selected === "logs");
  if (selected === "changes") void workspaceChangesView.load();
  if (selected === "plan") taskLifecycleView.load();
  scheduleWorkspaceRefresh();
}

$("refreshBtn").addEventListener("click", () => refresh());
$("configMenu").addEventListener("click", (event) => {
  const button = event.target.closest(".config-item[data-key]");
  if (button) loadConfig(button.dataset.key);
});
$("openSupervisorConsole").addEventListener("click", () => {
  const button = $("openSupervisorConsole");
  const agent = button.dataset.agent;
  if (agent)
    agentConsole.open(agent, {
      packageId: button.dataset.package || "",
      stage: "supervise",
    });
});
$("pauseSupervisor").addEventListener("click", async () => {
  if (
    !confirm(
      "Pause the current Supervisor/orchestrator process? Durable incident state is preserved.",
    )
  )
    return;
  try {
    await api("/api/run/stop", { method: "POST", body: "{}" });
    toast("Supervision paused");
    await refresh();
  } catch (e) {
    toast(e.message, true);
  }
});
$("supervisorDecision").addEventListener("submit", submitSupervisorDecision);
$("workflowViewMode").addEventListener("click", (event) => {
  const button = event.target.closest("[data-workflow-view]");
  if (!button) return;
  const mode = button.dataset.workflowView === "graph" ? "graph" : "list";
  if (mode === workflowViewMode()) return;
  setWorkflowViewMode(mode);
  state.workflowRenderKey = "";
  renderWorkflow();
});
$("workflowFollowActive").addEventListener("click", () => {
  const next = !workbenchNavigation.snapshot().followActive;
  setFollowActive(next);
  renderWorkflow();
});
$("workflowLocateBtn").addEventListener("click", () =>
  requestWorkflowLocate("operator"),
);
$("workflowFilter").addEventListener("change", (e) => {
  state.filter = e.target.value;
  renderWorkflow();
});
$("workflowEdgeMode").addEventListener("click", (event) => {
  const button = event.target.closest("[data-workflow-edge-mode]");
  if (!button) return;
  const mode =
    button.dataset.workflowEdgeMode === "all" ? "all" : "essential";
  if (mode === state.workflowEdgeMode) return;
  state.workflowEdgeMode = mode;
  $("workflowEdgeMode")
    .querySelectorAll("[data-workflow-edge-mode]")
    .forEach((candidate) =>
      candidate.setAttribute(
        "aria-pressed",
        String(candidate.dataset.workflowEdgeMode === mode),
      ),
    );
  renderWorkflow();
});
$("decomposeBtn").addEventListener("click", decomposeWorkPackage);
$("pauseWorkPackageBtn").addEventListener("click", () => {
  const p = (state.snapshot?.packages || []).find(
    (item) => item.id === selectedPackageId(),
  );
  if (p)
    changeWorkPackagePause(
      isFutureWorkPackage(p) ? !p.pause_before_start : !p.operator_paused,
    );
});
$("openWorkPackageAgentBtn").addEventListener("click", () => {
  const button = $("openWorkPackageAgentBtn");
  if (button.dataset.agent)
    agentConsole.open(button.dataset.agent, {
      packageId: selectedPackageId() || "",
    });
});
$("packageDetail").addEventListener("focusout", () => {
  requestAnimationFrame(flushPendingWorkPackageDetailRefresh);
});
$("supervisorDecision").addEventListener("focusout", () => {
  requestAnimationFrame(flushPendingSupervisorQuestionRefresh);
});
$("syncWorkPackageBtn").addEventListener("click", () => {
  if (selectedPackageId()) openWorkPackageSyncDialog(selectedPackageId());
});
$("closeWorkPackageSyncDialog").addEventListener("click", () => $("workPackageSyncDialog").close());
$("cancelWorkPackageSync").addEventListener("click", () => $("workPackageSyncDialog").close());
$("workPackageSyncDialog").addEventListener("click", (event) => {
  if (event.target === $("workPackageSyncDialog")) $("workPackageSyncDialog").close();
});
$("refreshWorkPackageSyncBranches").addEventListener("click", () => {
  if (state.workPackageSync.packageId) openWorkPackageSyncDialog(state.workPackageSync.packageId, { refreshBranches: true });
});
$("previewWorkPackageSync").addEventListener("click", previewWorkPackageSync);
$("confirmWorkPackageSync").addEventListener("click", submitWorkPackageSync);
$("reloadConfigBtn").addEventListener("click", () =>
  loadConfig(state.configKey),
);
$("validateConfigBtn").addEventListener("click", validateConfig);
$("saveConfigBtn").addEventListener("click", saveConfig);
$("configEditor").addEventListener("keydown", (e) => {
  if (e.key === "Tab") {
    e.preventDefault();
    const t = e.target,
      s = t.selectionStart,
      n = t.selectionEnd;
    t.value = t.value.slice(0, s) + "  " + t.value.slice(n);
    t.selectionStart = t.selectionEnd = s + 2;
  }
  if ((e.ctrlKey || e.metaKey) && e.key === "s") {
    e.preventDefault();
    saveConfig();
  }
});

let dashboardRefreshTimer = 0;
let workspaceRefreshTimer = 0;
function scheduleDashboardRefresh() {
  clearTimeout(dashboardRefreshTimer);
  dashboardRefreshTimer = window.setTimeout(
    async () => {
      await refresh({ reportError: false });
      scheduleDashboardRefresh();
    },
    document.hidden
      ? HIDDEN_REFRESH_MS
      : state.snapshot?.mode === "home"
        ? HOME_REFRESH_MS
        : DASHBOARD_REFRESH_MS,
  );
}
function scheduleWorkspaceRefresh() {
  clearTimeout(workspaceRefreshTimer);
  if (!document.hidden && $("changesView").classList.contains("active")) {
    workspaceRefreshTimer = window.setTimeout(async () => {
      await workspaceChangesView.load();
      scheduleWorkspaceRefresh();
    }, WORKSPACE_REFRESH_MS);
  }
}
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refresh({ reportError: false });
  scheduleDashboardRefresh();
  scheduleWorkspaceRefresh();
});
refresh().finally(() => {
  scheduleDashboardRefresh();
  scheduleWorkspaceRefresh();
});
