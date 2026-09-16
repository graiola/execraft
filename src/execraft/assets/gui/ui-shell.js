function formatElapsed(value) {
  const started = Date.parse(value || "");
  if (!Number.isFinite(started)) return "—";
  const seconds = Math.max(0, Math.floor((Date.now() - started) / 1000));
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

function formatElapsedSeconds(value) {
  const seconds = Math.max(0, Math.floor(Number(value) || 0));
  if (!seconds) return "—";
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

function humanize(value, fallback = "—") {
  const text = String(value || "").trim();
  return text ? text.replaceAll("_", " ") : fallback;
}

function packageAgentForStage(packageRow, stage) {
  if (!packageRow) return "";
  if (stage === "decompose") return packageRow.decomposition_agent_id || "";
  if (stage === "review" || stage === "final_review") {
    return packageRow.final_reviewer_id || packageRow.reviewer_id || "";
  }
  if (stage === "fix_review") return packageRow.last_fixer_id || "";
  return packageRow.agent_id || "";
}

function executionContextsFor(snapshot) {
  const contexts = Array.isArray(snapshot.execution_contexts)
    ? snapshot.execution_contexts.filter((context) => context && typeof context === "object")
    : [];
  if (contexts.length) return contexts;
  const legacy = snapshot.execution_context;
  return legacy && typeof legacy === "object" && Object.keys(legacy).length
    ? [legacy]
    : [];
}

function executionContextKey(context) {
  return [
    context.invocation_id || "",
    context.source || "",
    context.package_id || "",
    context.stage || "",
    context.agent_id || "",
  ].join("|");
}

function selectExecutionContext(contexts, preferredPackageId = "") {
  if (!contexts.length) return {};
  if (contexts[0]?.source === "supervisor") return contexts[0];
  const preferred = String(preferredPackageId || "").trim();
  return (
    (preferred && contexts.find((context) => context.package_id === preferred)) ||
    contexts[0]
  );
}

function liveAgentContexts(contexts) {
  return contexts.filter(
    (context) =>
      context.source === "invocation" &&
      context.status === "running" &&
      Boolean(context.agent_id),
  );
}

export class UiShell {
  constructor({ api, toast, setView, refresh, openAgent }) {
    this.api = api;
    this.toast = toast;
    this.setView = setView;
    this.refresh = refresh;
    this.openAgent = openAgent;
    this.$ = (id) => document.getElementById(id);
    this.snapshot = null;
    this.commands = [
      {
        id: "run",
        label: "Open Run",
        detail: "Operate the current workflow",
        run: () => setView("run"),
      },
      {
        id: "agents",
        label: "Open Agents",
        detail: "Manage the task workforce and agent actions",
        run: () => setView("agents"),
      },
      {
        id: "plan",
        label: "Open Plan",
        detail: "Review task definition, replanning and completion",
        run: () => setView("plan"),
      },
      {
        id: "changes",
        label: "Open Changes",
        detail: "Review changed files, diffs and commits",
        run: () => setView("changes"),
      },
      {
        id: "logs",
        label: "Open logs & diagnostics",
        detail: "Inspect runtime logs and orchestrator state",
        run: () => setView("logs"),
      },
      {
        id: "config",
        label: "Open advanced configuration",
        detail: "Validate and save raw project/task YAML",
        run: () => setView("config"),
      },
      {
        id: "agent",
        label: "Open active agent",
        detail: "Observe or steer the current session",
        run: () => this.#openCurrentAgent(),
      },
      {
        id: "refresh",
        label: "Refresh dashboard",
        detail: "Fetch the latest durable snapshot",
        run: () => refresh(),
      },
    ];
    this.#bindTabs();
    this.#bindActions();
    this.#bindPalette();
  }

  render(snapshot, { preferredPackageId = "" } = {}) {
    this.snapshot = snapshot;
    const orchestration = snapshot.orchestration || {};
    const assignments = snapshot.assignments || [];
    const active = assignments[0] || {};
    const contexts = executionContextsFor(snapshot);
    const execution = selectExecutionContext(contexts, preferredPackageId);
    const liveContexts = liveAgentContexts(contexts);
    const action = snapshot.run_control?.human_action || {};
    const packages = snapshot.packages || [];
    const packagesById = new Map(packages.map((item) => [item.id, item]));
    const firstIncomplete = packages.find(
      (item) => item.stage !== "completed" && !item.operator_paused,
    );
    const supervisor = snapshot.supervisor || {};
    const incident = supervisor.incident || {};
    const incidentActive = Boolean(
      incident.incident_id &&
        !["idle", "resolved", "completed", "stopped"].includes(
          incident.status || "",
        ),
    );
    const state = orchestration.state || "not initialized";
    const blocked =
      action.problem || action.reason || orchestration.error_message || "";
    const incidentWaiting =
      incident.status === "waiting_for_human" ||
      orchestration.state === "waiting_for_human_decision";
    this.$("actionCenter").classList.toggle(
      "attention",
      Boolean(blocked) || incidentWaiting,
    );
    const packageId =
      (incidentActive && incident.package_id) ||
      execution.package_id ||
      active.package_id ||
      action.package_id ||
      firstIncomplete?.id ||
      "";
    const packageRow = packagesById.get(packageId);
    const stage =
      (incidentActive && (incident.stage || incident.status)) ||
      execution.stage ||
      active.stage ||
      action.stage ||
      packageRow?.stage ||
      orchestration.state ||
      "idle";
    const agentId = incidentActive
      ? supervisor.agent_id ||
        supervisor.configured_agent ||
        incident.supervisor_agent_id ||
        ""
      : execution.agent_id ||
        active.agent_id ||
        packageAgentForStage(packageRow, stage);
    const parallelLive = liveContexts.length > 1;
    const liveStages = [
      ...new Set(liveContexts.map((context) => humanize(context.stage, "idle"))),
    ];
    const operationalContext = incidentActive
      ? incident.summary ||
        `${humanize(incident.classification, "incident")} recovery is ${humanize(incident.status, "active")}.`
      : parallelLive
        ? liveContexts
            .map(
              (context) =>
                `${context.package_id || "package"} · ${humanize(context.stage, "running")}`,
            )
            .join("  •  ")
        : active.package_title ||
          packageRow?.title ||
          "No blocker is currently reported.";
    const title = blocked
      ? `${humanize(state)} — action required`
      : parallelLive
        ? `${liveContexts.length} agents are running in parallel`
        : packageId
          ? `${packageId} is ${humanize(stage || state)}`
          : humanize(state);
    this.$("actionCenterTitle").textContent = title;
    this.$("actionCenterTitle").title = title;
    this.$("actionCenterContext").textContent =
      blocked || action.recommended_decision || operationalContext;
    this.$("actionCenterContext").title = this.$(
      "actionCenterContext",
    ).textContent;
    this.$("actionPackage").textContent = packageId || "—";
    this.$("actionPackage").title = parallelLive
      ? `Primary: ${packageId || "none"}; active: ${liveContexts
          .map((context) => context.package_id)
          .filter(Boolean)
          .join(", ")}`
      : packageId || "";
    this.$("actionAgent").textContent = parallelLive
      ? `${liveContexts.length} running · ${liveStages.join(", ")}`
      : `${agentId || "Unassigned"} / ${humanize(stage, "idle")}`;
    this.$("actionAgent").title = parallelLive
      ? liveContexts
          .map(
            (context) =>
              `${context.package_id}: ${context.agent_id} / ${humanize(context.stage, "idle")}`,
          )
          .join("; ")
      : `Agent: ${agentId || "Unassigned"}; stage: ${humanize(stage, "idle")}`;
    this.$("actionElapsed").textContent =
      incidentActive && incident.elapsed_seconds
        ? formatElapsedSeconds(incident.elapsed_seconds)
        : formatElapsed(
            execution.started_at ||
              active.started_at ||
              snapshot.run?.started_at ||
              orchestration.started_at,
          );
    const detailParts = [
      packageId ? `Work Package ${packageId}` : "No active Work Package",
      parallelLive
        ? `${liveContexts.length} active agents`
        : agentId
          ? `${agentId} · ${humanize(stage, "idle")}`
          : humanize(stage, "idle"),
      this.$("actionElapsed").textContent !== "—"
        ? this.$("actionElapsed").textContent
        : "",
    ].filter(Boolean);
    this.$("actionDetailsSummary").textContent = detailParts.join(" · ");
    this.$("openCurrentAgent").disabled = !agentId;
    this.$("openCurrentAgent").dataset.agent = agentId;
    this.$("openCurrentAgent").dataset.package = packageId;
    this.$("openCurrentAgent").dataset.stage = incidentActive
      ? "supervise"
      : stage;
    this.$("openCurrentAgent").textContent = parallelLive
      ? "Open primary agent"
      : "Open agent";
    this.#renderExecutionContexts(contexts, execution);
  }

  #renderExecutionContexts(contexts, primary) {
    const container = this.$("executionContexts");
    const visible = contexts.filter(
      (context) => context.agent_id || context.source === "supervisor",
    );
    container.classList.toggle("hidden", visible.length < 2);
    if (visible.length < 2) {
      container.replaceChildren();
      return;
    }
    const primaryKey = executionContextKey(primary);
    container.replaceChildren(
      ...visible.map((context) => {
        const row = document.createElement(context.agent_id ? "button" : "div");
        if (row instanceof HTMLButtonElement) row.type = "button";
        row.className = "execution-context-row";
        row.classList.toggle(
          "primary",
          executionContextKey(context) === primaryKey,
        );
        row.classList.toggle("supervisor", context.source === "supervisor");
        row.dataset.agent = context.agent_id || "";
        row.dataset.package = context.package_id || "";
        row.dataset.stage = context.stage || "";

        const packageNode = document.createElement("strong");
        packageNode.textContent = context.package_id || "—";
        packageNode.title = context.package_title || context.package_id || "";
        const agentNode = document.createElement("span");
        agentNode.className = "execution-context-agent";
        agentNode.textContent = context.agent_id || "Unassigned";
        const stageNode = document.createElement("span");
        stageNode.className = "execution-context-stage";
        stageNode.textContent = humanize(context.stage, "idle");
        const elapsedNode = document.createElement("time");
        elapsedNode.textContent = formatElapsed(context.started_at);
        elapsedNode.dateTime = context.started_at || "";
        row.append(packageNode, agentNode, stageNode, elapsedNode);
        row.title = [
          context.package_title || context.package_id,
          context.model,
          context.source === "supervisor" ? "Supervisor recovery" : "",
        ]
          .filter(Boolean)
          .join(" · ");
        if (row instanceof HTMLButtonElement) {
          row.setAttribute(
            "aria-label",
            `Open ${context.agent_id} for ${context.package_id} at ${humanize(context.stage, "idle")}`,
          );
          row.addEventListener("click", () =>
            this.openAgent(context.agent_id, {
              packageId: context.package_id || "",
              stage: context.stage || "",
            }),
          );
        }
        return row;
      }),
    );
  }

  #bindTabs() {
    const tabs = [...document.querySelectorAll(".tabs [role=tab]")];
    const activate = (tab, { focus = false } = {}) => {
      tabs.forEach((item) => {
        const selected = item === tab;
        item.classList.toggle("active", selected);
        item.setAttribute("aria-selected", String(selected));
        item.tabIndex = selected ? 0 : -1;
      });
      if (focus) tab.focus();
      this.$("taskMoreMenu").open = false;
      this.setView(tab.dataset.view);
    };
    tabs.forEach((tab) => {
      tab.addEventListener("click", () => activate(tab));
      tab.addEventListener("keydown", (event) => {
        const index = tabs.indexOf(tab);
        let next = -1;
        if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
        if (event.key === "ArrowLeft")
          next = (index - 1 + tabs.length) % tabs.length;
        if (event.key === "Home") next = 0;
        if (event.key === "End") next = tabs.length - 1;
        if (next < 0) return;
        event.preventDefault();
        activate(tabs[next], { focus: true });
      });
    });
  }

  #bindActions() {
    this.$("openCurrentAgent").addEventListener("click", () =>
      this.#openCurrentAgent(),
    );
    this.$("openLogsAction").addEventListener("click", () =>
      this.setView("logs"),
    );
    this.$("openChangesAction").addEventListener("click", () =>
      this.setView("changes"),
    );
    document.querySelectorAll("[data-utility-view]").forEach((button) => {
      button.addEventListener("click", () => {
        this.$("taskMoreMenu").open = false;
        this.setView(button.dataset.utilityView);
      });
    });
    this.$("copyTaskLaunchCommand").addEventListener("click", async () => {
      const command = this.$("taskLaunchCommand").textContent;
      try {
        await navigator.clipboard.writeText(command);
        this.toast("Task launch command copied");
      } catch (_) {
        this.toast("Copy the task launch command manually", true);
      }
    });
  }

  #openCurrentAgent() {
    const button = this.$("openCurrentAgent");
    if (!button.dataset.agent) return;
    this.openAgent(button.dataset.agent, {
      packageId: button.dataset.package || "",
      stage: button.dataset.stage || "",
    });
  }

  #bindPalette() {
    const dialog = this.$("commandPalette");
    const input = this.$("commandSearch");
    const list = this.$("commandList");
    let activeIndex = 0;
    const render = () => {
      const query = input.value.trim().toLowerCase();
      const commands = this.commands.filter((command) =>
        `${command.label} ${command.detail}`.toLowerCase().includes(query),
      );
      activeIndex = Math.min(activeIndex, Math.max(0, commands.length - 1));
      list.replaceChildren(
        ...commands.map((command, index) => {
          const button = document.createElement("button");
          button.type = "button";
          button.className = `command-item${index === activeIndex ? " active" : ""}`;
          button.dataset.command = command.id;
          button.id = `command-${command.id}`;
          button.setAttribute("role", "option");
          button.setAttribute("aria-selected", String(index === activeIndex));
          button.innerHTML = `<strong>${command.label}</strong><small>${command.detail}</small>`;
          button.addEventListener("click", () => {
            dialog.close();
            command.run();
          });
          return button;
        }),
      );
      input.setAttribute(
        "aria-activedescendant",
        commands[activeIndex] ? `command-${commands[activeIndex].id}` : "",
      );
      return commands;
    };
    const open = () => {
      if (!dialog.open) dialog.showModal();
      input.value = "";
      activeIndex = 0;
      render();
      queueMicrotask(() => input.focus());
    };
    this.$("commandPaletteBtn").addEventListener("click", open);
    input.addEventListener("input", render);
    input.addEventListener("keydown", (event) => {
      const commands = render();
      if (event.key === "ArrowDown")
        activeIndex = Math.min(activeIndex + 1, commands.length - 1);
      else if (event.key === "ArrowUp")
        activeIndex = Math.max(activeIndex - 1, 0);
      else if (event.key === "Enter" && commands[activeIndex]) {
        event.preventDefault();
        dialog.close();
        commands[activeIndex].run();
        return;
      } else return;
      event.preventDefault();
      render();
    });
    document.addEventListener("keydown", (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        dialog.open ? dialog.close() : open();
      }
    });
  }
}
