export class LogController {
  constructor({ api }) {
    this.api = api;
    this.$ = (id) => document.getElementById(id);
    this.offset = 0;
    this.source = "orchestrator";
    this.follow = true;
    this.snapshot = null;
    this.polling = false;
    this.active = false;
    this.timer = 0;
    this.$("clearLogBtn").addEventListener("click", () => {
      this.$("logOutput").textContent = "";
      this.offset = 0;
    });
    this.$("logSource").addEventListener("change", (event) => {
      this.source = event.target.value;
      this.offset = 0;
      this.$("logOutput").textContent = "";
      this.#renderPath();
      this.poll();
    });
    this.$("followLogBtn").addEventListener("click", (event) => {
      this.follow = !this.follow;
      event.target.textContent = this.follow ? "Following" : "Paused";
      event.target.classList.toggle("primary", this.follow);
      if (this.follow) this.poll();
      else clearTimeout(this.timer);
    });
  }

  render(snapshot) {
    this.snapshot = snapshot;
    this.#renderPath();
    this.#renderStateMachine();
  }

  setActive(active) {
    this.active = Boolean(active);
    clearTimeout(this.timer);
    if (this.active && this.follow) this.poll();
  }

  async poll() {
    if (!this.active || !this.follow || this.polling) return;
    this.polling = true;
    let nextDelay = 1400;
    try {
      const data = await this.api(
        `/api/log?source=${encodeURIComponent(this.source)}&offset=${this.offset}`,
      );
      if (data.content) {
        const output = this.$("logOutput");
        output.textContent += data.content;
        this.offset = data.next_offset;
        output.scrollTop = output.scrollHeight;
      }
      if (data.path) this.$("logPath").textContent = data.path;
      if (data.truncated) nextDelay = 50;
    } catch (_) {
      // Log polling is opportunistic and retries on the next interval.
    } finally {
      this.polling = false;
      this.#schedule(nextDelay);
    }
  }

  #schedule(delay = 1400) {
    clearTimeout(this.timer);
    if (!this.active || !this.follow) return;
    this.timer = window.setTimeout(() => this.poll(), delay);
  }

  #renderPath() {
    if (!this.snapshot) return;
    this.$("logPath").textContent =
      this.source === "driver"
        ? this.snapshot.paths?.driver_log || ""
        : this.snapshot.paths?.log || "";
  }

  #renderStateMachine() {
    if (!this.snapshot) return;
    const orchestration = this.snapshot.orchestration || {};
    const projectState = String(orchestration.state || "not_initialized");
    const packages = this.snapshot.packages || [];
    const execution = this.snapshot.execution_context || {};
    const activePackage =
      packages.find((item) => item.id === execution.package_id) ||
      packages.find(
        (item) => item.status === "running" && item.stage !== "completed",
      ) ||
      packages.find((item) => item.stage !== "completed") ||
      null;
    const stage = String(execution.stage || activePackage?.stage || "prepare");
    const assignment = (this.snapshot.assignments || []).find(
      (item) => item.package_id === activePackage?.id,
    );
    const activeAgent = execution.agent_id || assignment?.agent_id || "";
    const projectPhases = [
      ["validating_plan", "Validate plan", "Normalize and validate the durable plan"],
      ["running", "Run packages", "Schedule ready work and execution-agent attempts"],
      ["recovery", "Wait or recover", "Execution-agent waits, supervision and operator boundaries"],
      ["completed", "Complete", "All packages committed and finalized"],
    ];
    const recoveryStates = new Set([
      "waiting_for_agent",
      "supervising",
      "waiting_for_human_decision",
      "human_required",
      "operator_paused",
      "paused_low_disk",
    ]);
    const projectIndex =
      projectState === "completed"
        ? 3
        : recoveryStates.has(projectState)
          ? 2
          : projectState === "validating_plan" || projectState === "not_initialized"
            ? 0
            : 1;
    const packageStages = [
      ["prepare", "Prepare"],
      ["decompose", "Decompose"],
      ["implement", "Implement"],
      ["fast_verify", "Fast verify"],
      ["review", "Review"],
      ["fix_review", "Fix review"],
      ["regression_verify", "Regression verify"],
      ["final_review", "Final review"],
      ["ready_to_commit", "Commit"],
      ["completed", "Completed"],
    ];
    const stageIndex = Math.max(
      0,
      packageStages.findIndex(([value]) => value === stage),
    );
    const rail = (items, activeIndex, detail) => {
      const container = document.createElement("div");
      container.className = "machine-rail";
      items.forEach((item, index) => {
        const row = document.createElement("div");
        row.className = `machine-state${index < activeIndex ? " complete" : ""}${index === activeIndex ? " active" : ""}`;
        const node = document.createElement("span");
        node.className = "machine-node";
        const copy = document.createElement("span");
        copy.className = "machine-copy";
        const strong = document.createElement("strong");
        strong.textContent = item[1];
        const small = document.createElement("small");
        small.textContent = index === activeIndex ? detail : item[2] || item[0];
        copy.append(strong, small);
        row.append(node, copy);
        container.append(row);
      });
      return container;
    };
    const section = (title, subtitle, content) => {
      const node = document.createElement("section");
      node.className = "machine-section";
      const heading = document.createElement("h3");
      heading.textContent = title;
      const description = document.createElement("p");
      description.textContent = subtitle;
      node.append(heading, description, content);
      return node;
    };
    const machine = document.getElementById("orchestratorStateMachine");
    machine.replaceChildren(
      section(
        "Project controller",
        "Top-level durable orchestration state",
        rail(projectPhases, projectIndex, projectState.replaceAll("_", " ")),
      ),
      section(
        activePackage ? activePackage.id : "Package pipeline",
        activePackage?.title || "No incomplete package",
        rail(packageStages, stageIndex, stage.replaceAll("_", " ")),
      ),
    );
    const context = document.createElement("div");
    context.className = "machine-current-context";
    context.textContent = activePackage
      ? `${activePackage.id} · ${stage.replaceAll("_", " ")} · ${activeAgent || "no agent assigned"}`
      : "No active package.";
    machine.append(context);
    const badge = document.getElementById("orchestratorMachineState");
    badge.textContent = projectState.replaceAll("_", " ");
    badge.className = `pill ${projectState === "completed" ? "ok" : recoveryStates.has(projectState) ? "warn" : ""}`;
  }
}
