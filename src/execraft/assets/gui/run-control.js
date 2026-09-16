export class RunControlController {
  constructor({ api, toast, refresh }) {
    this.api = api;
    this.toast = toast;
    this.refresh = refresh;
    this.scopeApproval = null;
    this.scopePreview = null;
    this.operatorAcceptance = null;
    this.operatorAcceptancePreview = null;
    this.$ = (id) => document.getElementById(id);
    this.$("runBtn").addEventListener("click", () => this.start());
    this.$("stopBtn").addEventListener("click", () => this.stop());
    this.$("approveScopeBtn").addEventListener("click", () =>
      this.openScopeApproval(),
    );
    this.$("acceptRiskBtn").addEventListener("click", () =>
      this.openOperatorAcceptance(),
    );
    this.$("confirmScopeApproval").addEventListener("click", () =>
      this.approveScope(),
    );
    this.$("closeScopeApproval").addEventListener("click", () =>
      this.closeScopeApproval(),
    );
    this.$("cancelScopeApproval").addEventListener("click", () =>
      this.closeScopeApproval(),
    );
    this.$("scopeApprovalDialog").addEventListener("click", (event) => {
      if (event.target === this.$("scopeApprovalDialog"))
        this.closeScopeApproval();
    });
    this.$("confirmOperatorAcceptance").addEventListener("click", () =>
      this.acceptOperatorRisk(),
    );
    this.$("closeOperatorAcceptance").addEventListener("click", () =>
      this.closeOperatorAcceptance(),
    );
    this.$("cancelOperatorAcceptance").addEventListener("click", () =>
      this.closeOperatorAcceptance(),
    );
    this.$("operatorAcceptanceAcknowledge").addEventListener("change", () =>
      this.updateOperatorAcceptanceConfirmation(),
    );
    this.$("operatorAcceptanceReason").addEventListener("input", () =>
      this.updateOperatorAcceptanceConfirmation(),
    );
    this.$("operatorAcceptanceDialog").addEventListener("click", (event) => {
      if (event.target === this.$("operatorAcceptanceDialog"))
        this.closeOperatorAcceptance();
    });
  }

  render(run, control) {
    const running = Boolean(run.owned_running || run.external_running);
    const supersededExit = Boolean(run.last_exit_superseded);
    const expectedControlHold = Boolean(run.last_exit_expected_control_hold);
    const scopeApproval = control.scope_approval?.available
      ? control.scope_approval
      : null;
    const operatorAcceptance = control.operator_acceptance?.available
      ? control.operator_acceptance
      : null;
    this.scopeApproval = scopeApproval;
    this.operatorAcceptance = operatorAcceptance;
    const label = run.owned_running
      ? `Running · PID ${run.pid}`
      : run.external_running
        ? "Running externally"
        : supersededExit
          ? "Completed · prior driver exit superseded"
          : expectedControlHold
            ? "Action required"
            : `Idle${run.last_exit_code !== null ? ` · exit ${run.last_exit_code}` : ""}`;
    const state = this.$("driverState");
    const dot = document.createElement("span");
    dot.className = `status-dot ${running ? "running" : ""}`;
    state.replaceChildren(dot, document.createTextNode(label));

    const runButton = this.$("runBtn");
    const approvalButton = this.$("approveScopeBtn");
    const acceptRiskButton = this.$("acceptRiskBtn");
    runButton.classList.toggle("hidden", Boolean(scopeApproval));
    approvalButton.classList.toggle("hidden", !scopeApproval);
    approvalButton.disabled = running || !scopeApproval;
    approvalButton.textContent = scopeApproval
      ? `Approve protected scope · ${scopeApproval.package_id}`
      : "Approve protected scope";
    approvalButton.title = scopeApproval?.reason || "";
    acceptRiskButton.classList.toggle("hidden", !operatorAcceptance);
    acceptRiskButton.disabled = running || !operatorAcceptance;
    acceptRiskButton.textContent = operatorAcceptance
      ? `Accept & continue · ${operatorAcceptance.package_id}`
      : "Accept & continue";
    acceptRiskButton.title = operatorAcceptance?.disclaimer || "";

    runButton.disabled = running || control.can_start === false;
    this.$("stopBtn").disabled = !run.owned_running;
    runButton.textContent = control.label || "Run / Resume";
    runButton.title = control.reason || "";
    const message = this.$("runMessage");
    let text = "";
    let className = "run-message";
    if (!running && supersededExit) {
      text = `A previous GUI-owned driver exited with code ${run.last_exit_code}, but a newer run completed the durable pipeline successfully.`;
      className += " show";
    } else if (!running && expectedControlHold) {
      text = control.reason || run.last_exit_summary || "Operator action is required before orchestration can continue.";
      className += " show warn";
    } else if (!running && run.last_exit_code !== null && run.last_exit_code !== 0) {
      text = `The GUI-owned driver exited with code ${run.last_exit_code}.`;
      if (run.last_exit_summary) text += `\n\n${run.last_exit_summary}`;
      className += " show bad";
    } else if (!running && control.can_start === false && control.reason) {
      text = control.reason;
      className += " show warn";
    }
    message.textContent = text;
    message.className = className;
  }

  closeOperatorAcceptance() {
    const dialog = this.$("operatorAcceptanceDialog");
    if (dialog.open) dialog.close();
    this.operatorAcceptancePreview = null;
    this.$("operatorAcceptanceReason").value = "";
    this.$("operatorAcceptanceAcknowledge").checked = false;
    this.$("confirmOperatorAcceptance").disabled = true;
  }

  updateOperatorAcceptanceConfirmation() {
    const reason = this.$("operatorAcceptanceReason").value.trim();
    const acknowledged = this.$("operatorAcceptanceAcknowledge").checked;
    this.$("confirmOperatorAcceptance").disabled = !(
      this.operatorAcceptancePreview?.available &&
      acknowledged &&
      reason.length > 0
    );
  }

  renderOperatorAcceptancePreview(preview) {
    this.operatorAcceptancePreview = preview;
    this.$("operatorAcceptanceTitle").textContent =
      `Accept deferred risk · ${preview.package_id}`;
    this.$("operatorAcceptanceSubtitle").textContent =
      `${preview.stage || "late human-decision hold"} · this decision remains explicit in the audit trail`;
    this.$("operatorAcceptanceRequirement").textContent =
      preview.blocked_requirement || preview.impact || "Late acceptance check requires operator disposition.";

    const evidence = (preview.evidence || []).map((item, index) => {
      const row = document.createElement("div");
      row.className = "operator-acceptance-item";
      const strong = document.createElement("strong");
      strong.textContent = `Evidence ${index + 1}`;
      const small = document.createElement("small");
      small.textContent = item;
      row.replaceChildren(strong, small);
      return row;
    });
    if (!evidence.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "No additional evidence text was attached to this check.";
      evidence.push(empty);
    }
    this.$("operatorAcceptanceEvidence").replaceChildren(...evidence);

    const criteria = (preview.unverified_criteria || []).map((criterion) => {
      const row = document.createElement("div");
      row.className = "operator-acceptance-item";
      const strong = document.createElement("strong");
      strong.textContent = criterion.id || "Unnamed criterion";
      const small = document.createElement("small");
      small.textContent = criterion.description || "No description recorded.";
      row.replaceChildren(strong, small);
      return row;
    });
    if (!criteria.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent =
        "No acceptance criterion is currently marked unverified; this decision covers the blocking late-review disposition itself.";
      criteria.push(empty);
    }
    this.$("operatorAcceptanceCriteria").replaceChildren(...criteria);
    this.updateOperatorAcceptanceConfirmation();
  }

  async loadOperatorAcceptancePreview() {
    const packageId = this.operatorAcceptance?.package_id || "";
    if (!packageId) throw new Error("No operator-acceptable Work Package is active.");
    const preview = await this.api(
      `/api/operator-acceptance?package_id=${encodeURIComponent(packageId)}`,
    );
    this.renderOperatorAcceptancePreview(preview);
    return preview;
  }

  async openOperatorAcceptance() {
    const button = this.$("acceptRiskBtn");
    button.disabled = true;
    this.operatorAcceptancePreview = null;
    this.$("operatorAcceptanceReason").value = "";
    this.$("operatorAcceptanceAcknowledge").checked = false;
    this.$("confirmOperatorAcceptance").disabled = true;
    this.$("operatorAcceptanceTitle").textContent = "Accept deferred risk";
    this.$("operatorAcceptanceSubtitle").textContent =
      "Loading the authoritative human-decision hold…";
    this.$("operatorAcceptanceRequirement").textContent = "";
    const loading = document.createElement("div");
    loading.className = "empty";
    loading.textContent = "Loading current reviewer evidence…";
    this.$("operatorAcceptanceEvidence").replaceChildren(loading);
    this.$("operatorAcceptanceCriteria").replaceChildren(loading.cloneNode(true));
    const dialog = this.$("operatorAcceptanceDialog");
    if (!dialog.open) dialog.showModal();
    try {
      await this.loadOperatorAcceptancePreview();
    } catch (error) {
      this.closeOperatorAcceptance();
      this.toast(error.message, true);
      await this.refresh();
    } finally {
      button.disabled = false;
    }
  }

  async acceptOperatorRisk() {
    const preview = this.operatorAcceptancePreview;
    const reason = this.$("operatorAcceptanceReason").value.trim();
    const acknowledged = this.$("operatorAcceptanceAcknowledge").checked;
    if (!preview?.available || !preview.package_id) {
      this.toast("Refresh the operator-acceptance preview before continuing.", true);
      return;
    }
    if (!acknowledged || !reason) {
      this.toast("Acknowledge the unverified status and record a reason first.", true);
      return;
    }
    const button = this.$("confirmOperatorAcceptance");
    button.disabled = true;
    try {
      const result = await this.api("/api/operator-acceptance/accept", {
        method: "POST",
        body: JSON.stringify({
          package_id: preview.package_id,
          expected_sequence: preview.sequence,
          reason,
          acknowledged: true,
        }),
      });
      this.closeOperatorAcceptance();
      const running = Boolean(result.run?.owned_running);
      this.toast(
        running
          ? `Risk accepted for ${preview.package_id} · orchestration resumed`
          : `Risk accepted for ${preview.package_id}`,
      );
      await this.refresh();
    } catch (error) {
      this.toast(error.message, true);
      try {
        await this.loadOperatorAcceptancePreview();
      } catch (refreshError) {
        this.closeOperatorAcceptance();
        this.toast(refreshError.message, true);
        await this.refresh();
      }
    } finally {
      if (this.$("operatorAcceptanceDialog").open)
        this.updateOperatorAcceptanceConfirmation();
    }
  }

  closeScopeApproval() {
    const dialog = this.$("scopeApprovalDialog");
    if (dialog.open) dialog.close();
    this.scopePreview = null;
    this.$("confirmScopeApproval").disabled = true;
  }

  renderScopePreview(preview) {
    this.scopePreview = preview;
    this.$("scopeApprovalTitle").textContent =
      `Approve exact scope · ${preview.package_id}`;
    this.$("scopeApprovalSubtitle").textContent =
      `${preview.candidate_paths.length} current candidate path${preview.candidate_paths.length === 1 ? "" : "s"} · next stage follows ${preview.stage || "current package stage"}`;
    this.$("scopeApprovalReason").textContent = preview.reason || "";
    const protectedPaths = new Set(preview.protected_paths || []);
    const relationshipByPath = new Map(
      (preview.candidates || []).map((item) => [item.path, item.relationship]),
    );
    const nodes = (preview.candidate_paths || []).map((path) => {
      const row = document.createElement("div");
      row.className = `scope-approval-path ${protectedPaths.has(path) ? "protected" : ""}`;
      const code = document.createElement("code");
      code.textContent = path;
      const meta = document.createElement("small");
      const relationship = String(
        relationshipByPath.get(path) || "scope candidate",
      ).replaceAll("_", " ");
      meta.textContent = protectedPaths.has(path)
        ? `protected · ${relationship}`
        : relationship;
      row.replaceChildren(code, meta);
      return row;
    });
    this.$("scopeApprovalPaths").replaceChildren(...nodes);
    this.$("confirmScopeApproval").disabled = nodes.length === 0;
  }

  async loadScopeApprovalPreview() {
    const packageId = this.scopeApproval?.package_id || "";
    if (!packageId) throw new Error("No protected-scope package is active.");
    const preview = await this.api(
      `/api/scope/approval?package_id=${encodeURIComponent(packageId)}`,
    );
    this.renderScopePreview(preview);
    return preview;
  }

  async openScopeApproval() {
    const button = this.$("approveScopeBtn");
    button.disabled = true;
    this.scopePreview = null;
    this.$("confirmScopeApproval").disabled = true;
    this.$("scopeApprovalTitle").textContent = "Approve exact scope";
    this.$("scopeApprovalSubtitle").textContent =
      "Loading the authoritative candidate set…";
    this.$("scopeApprovalReason").textContent = "";
    const loading = document.createElement("div");
    loading.className = "empty";
    loading.textContent = "Loading current workspace candidates…";
    this.$("scopeApprovalPaths").replaceChildren(loading);
    const dialog = this.$("scopeApprovalDialog");
    if (!dialog.open) dialog.showModal();
    try {
      await this.loadScopeApprovalPreview();
    } catch (error) {
      this.closeScopeApproval();
      this.toast(error.message, true);
      await this.refresh();
    } finally {
      button.disabled = false;
    }
  }

  async approveScope() {
    const preview = this.scopePreview;
    if (!preview?.package_id || !preview.candidate_paths?.length) {
      this.toast("Refresh the protected-scope preview before approving.", true);
      return;
    }
    const button = this.$("confirmScopeApproval");
    button.disabled = true;
    try {
      const result = await this.api("/api/scope/accept", {
        method: "POST",
        body: JSON.stringify({
          package_id: preview.package_id,
          expected_candidates: preview.candidate_paths,
        }),
      });
      this.closeScopeApproval();
      const running = Boolean(result.run?.owned_running);
      this.toast(
        running
          ? `Protected scope approved for ${preview.package_id} · verification resumed`
          : `Protected scope approved for ${preview.package_id}`,
      );
      await this.refresh();
    } catch (error) {
      this.toast(error.message, true);
      try {
        await this.loadScopeApprovalPreview();
      } catch (refreshError) {
        this.closeScopeApproval();
        this.toast(refreshError.message, true);
        await this.refresh();
      }
    } finally {
      if (this.$("scopeApprovalDialog").open) button.disabled = false;
    }
  }

  async start() {
    try {
      const result = await this.api("/api/run/start", {
        method: "POST",
        body: "{}",
      });
      this.toast(
        result.initialized
          ? "Plan initialized · ready to run"
          : result.owned_running
          ? `Orchestrator started · PID ${result.pid}`
          : "Orchestrator command completed",
      );
      await this.refresh();
    } catch (error) {
      this.toast(error.message, true);
      await this.refresh();
    }
  }

  async stop() {
    if (
      !window.confirm(
        "Stop the dashboard-owned orchestrator process? The current agent subprocess group will be terminated.",
      )
    )
      return;
    try {
      await this.api("/api/run/stop", { method: "POST", body: "{}" });
      this.toast("Stop signal sent");
      await this.refresh();
    } catch (error) {
      this.toast(error.message, true);
    }
  }
}
