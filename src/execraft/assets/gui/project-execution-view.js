import { escapeHtml as esc } from "./ui-utils.js";

function $(id) { return document.getElementById(id); }

function statusClass(value) {
  return ["passed", "achieved", "complete", "ready", "on_track", "active", "running", "converged"].includes(value)
    ? "available"
    : ["failed", "blocked", "late", "cancelled"].includes(value)
      ? "missing"
      : ["awaiting_decision", "at_risk", "waived"].includes(value)
        ? "warning"
        : "disabled";
}

function statusPill(value) {
  const text = String(value || "unknown").replaceAll("_", " ");
  return `<span class="status ${statusClass(value)}">${esc(text)}</span>`;
}

function list(values = []) {
  return values.length ? values.map((value) => `<code>${esc(value)}</code>`).join(" ") : '<span class="muted">none</span>';
}

function criterionText(criterion = {}) {
  const suffix = criterion.task_id || criterion.gate_id || criterion.artifact_id || criterion.outcome || "";
  return `${criterion.type || "criterion"}${suffix ? ` · ${suffix}` : ""}`;
}

function scheduleText(item = {}) {
  const schedule = item.schedule || {};
  const start = schedule.start || item.start || "";
  const target = schedule.target || item.target || "";
  if (start && target) return `${start} → ${target}`;
  return target || start || "unscheduled";
}

function titleCase(value) {
  return String(value || "").replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function shortDigest(value, length = 16) {
  const text = String(value || "");
  if (!text) return "—";
  return text.length > length + 4 ? `${text.slice(0, length)}…` : text;
}

export class ProjectExecutionView {
  constructor({ api, download, toast, onOpenTask, onCanonicalChange = () => {} }) {
    this.api = api;
    this.download = download;
    this.toast = toast;
    this.onOpenTask = onOpenTask;
    this.onCanonicalChange = onCanonicalChange;
    this.projectId = "";
    this.availableTasks = [];
    this.snapshot = null;
    this.selected = null;
    this.busy = false;
    this.stale = false;
    this.loadError = "";
    this.coordinationHistory = [];
    this.coordinationHistoryLoaded = false;
    this.coordinationHistoryError = "";
    this.#bind();
  }

  setProject(projectId, availableTasks = []) {
    const changed = this.projectId !== projectId;
    this.projectId = projectId || "";
    this.availableTasks = availableTasks || [];
    if (changed) {
      this.snapshot = null;
      this.selected = null;
      this.stale = false;
      this.loadError = "";
      this.coordinationHistory = [];
      this.coordinationHistoryLoaded = false;
      this.coordinationHistoryError = "";
      $("projectExecutionCoordinationInspector")?.classList.add("hidden");
      this.#renderEmpty();
    }
  }

  invalidate() {
    this.stale = true;
  }

  async focusAsset(kind, assetId) {
    if (!this.projectId || !assetId) return;
    await this.load();
    const collection = { phase: "phases", gate: "gates", milestone: "milestones" }[kind];
    const exists = collection && this.snapshot?.[collection]?.some((item) => item.id === assetId);
    if (!exists) {
      this.toast(`Project ${kind || "asset"} ${assetId} is not available in the current Project Execution definition`, true);
      return;
    }
    this.selected = `${kind}:${assetId}`;
    this.#renderInspector();
    queueMicrotask(() => {
      const card = document.querySelector(`[data-project-asset="${CSS.escape(`${kind}:${assetId}`)}"]`);
      card?.scrollIntoView({ block: "nearest", behavior: "smooth" });
      $("projectExecutionInspector")?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    });
  }

  async load({ force = false } = {}) {
    if (!this.projectId || (this.snapshot && !force && !this.stale)) return;
    this.#setLoading(true);
    try {
      this.snapshot = await this.api(`/api/project-execution/status?project_id=${encodeURIComponent(this.projectId)}`);
      this.stale = false;
      this.loadError = "";
      this.render();
      // Reconciliation can change Phase/Gate/Milestone runtime projections
      // without changing their definitions. Roadmap v2 projects that runtime
      // state too, so make the planning view refresh before it is shown again.
      this.onCanonicalChange();
    } catch (error) {
      this.loadError = error.message;
      this.stale = true;
      this.toast(error.message, true);
      if (this.snapshot?.configured) {
        this.render();
        this.#showStateBanner(`Reconcile failed. Showing the last known Project Execution state. ${error.message}`, "error", true);
      } else {
        const target = $("projectExecutionWorkspaceEmpty");
        target.innerHTML = `<strong>Project Execution unavailable.</strong><span>${esc(error.message)}</span>`;
        target.classList.remove("hidden");
        $("projectExecutionWorkspace").classList.add("hidden");
      }
    } finally {
      this.#setLoading(false);
    }
  }

  render() {
    const snapshot = this.snapshot;
    if (!snapshot) return this.#renderEmpty();
    $("projectExecutionWorkspaceEmpty").classList.toggle("hidden", snapshot.configured);
    $("projectExecutionWorkspace").classList.toggle("hidden", !snapshot.configured);
    $("projectExecutionInitialize").classList.toggle("hidden", snapshot.configured);
    if (!snapshot.configured) {
      $("projectExecutionAttentionCount").textContent = "0";
      $("projectExecutionWorkspaceEmpty").innerHTML = '<strong>Project Execution is not initialized.</strong><span>Create the canonical execution graph before coordinating Tasks, Gates and Milestones.</span>';
      this.#clearStateBanner();
      return;
    }

    if (snapshot.coordination?.pending) {
      this.#showCoordinationBanner(snapshot.coordination);
    } else if (snapshot.held) {
      this.#showStateBanner(`Project Execution is held. ${snapshot.hold_reason || "Operator hold"}`, "held");
    } else if (!this.loadError) {
      this.#clearStateBanner();
    }

    $("projectExecutionMode").value = snapshot.mode || "assisted";
    this.#renderAutomaticPolicy();
    $("projectExecutionAttentionCount").textContent = String((snapshot.attention || []).length);
    $("projectExecutionHoldBtn").textContent = snapshot.held ? "Resume execution" : "Hold execution";
    $("projectExecutionHoldBtn").classList.toggle("primary", snapshot.held);
    $("projectExecutionRevision").textContent = `rev ${snapshot.definition_revision}`;
    this.#renderSummary();
    this.#renderCurrent();
    this.#renderAttention();
    this.#renderQueues();
    this.#renderPhases();
    this.#renderGates();
    this.#renderMilestones();
    this.#renderTasks();
    this.#renderInspector();
    if (!$("projectExecutionCoordinationInspector")?.classList.contains("hidden")) {
      this.#renderCoordinationInspector();
    }
  }

  #renderSummary() {
    const s = this.snapshot;
    const cards = [
      ["Mode", s.mode, s.held ? `held · ${s.hold_reason || "operator hold"}` : "running policy"],
      ["Phases", s.phases.length, `${s.phases.filter((item) => item.state === "complete").length} complete`],
      ["Gates", s.gates.length, `${s.gates.filter((item) => ["passed", "waived"].includes(item.state)).length} satisfied`],
      ["Milestones", s.milestones.length, `${s.milestones.filter((item) => item.state === "achieved").length} achieved`],
      ["Tasks", s.tasks.length, `${s.ready_tasks.length} ready · ${s.blocked_tasks.length} blocked`],
    ];
    $("projectExecutionSummary").innerHTML = cards.map(([label, value, detail]) => `<div class="project-execution-stat"><span>${esc(label)}</span><strong>${esc(value)}</strong><small>${esc(detail)}</small></div>`).join("");
  }

  #renderCurrent() {
    const active = this.snapshot.phases.filter((phase) => phase.state === "active");
    const ready = this.snapshot.phases.filter((phase) => phase.state === "ready");
    const visible = active.length ? active : ready.length ? ready : this.snapshot.phases.filter((phase) => phase.state !== "complete" && phase.state !== "cancelled").slice(0, 3);
    const target = $("projectExecutionCurrent");
    if (!visible.length) {
      const allComplete = this.snapshot.phases.length && this.snapshot.phases.every((phase) => ["complete", "cancelled"].includes(phase.state));
      target.innerHTML = `<div class="project-execution-clear"><span>✓</span><div><strong>${allComplete ? "No active Phase remains" : "No Phase is ready yet"}</strong><small>${allComplete ? "Project Phase completion has been derived from Tasks, Gates and Milestones." : "Entry boundaries or project prerequisites have not made a Phase current yet."}</small></div></div>`;
      return;
    }
    target.innerHTML = `<div class="project-execution-current-grid">${visible.map((phase) => {
      const requiredTasks = this.snapshot.tasks.filter((task) => task.phase === phase.id && task.required);
      const completed = requiredTasks.filter((task) => task.outcome === "completed").length;
      return `<button type="button" class="project-execution-current-card" data-project-asset="phase:${esc(phase.id)}"><span class="asset-glyph phase">▰</span><div><strong>${esc(phase.title)}</strong><small>${completed}/${requiredTasks.length} required Tasks complete · ${esc(scheduleText(phase))}</small><div class="project-execution-card-status">${statusPill(phase.state)}${statusPill(phase.health)}</div></div></button>`;
    }).join("")}</div>`;
  }

  #renderAutomaticPolicy() {
    const automatic = this.snapshot.mode === "automatic";
    $("projectExecutionAutomaticPolicy").classList.toggle("hidden", !automatic);
    $("projectExecutionAutomaticCycleBtn").classList.toggle("hidden", !automatic);
    const policy = this.snapshot.policy || {};
    $("projectExecutionMaxParallel").value = policy.maximum_parallel_tasks || 1;
    $("projectExecutionMaxPerPhase").value = policy.maximum_parallel_tasks_per_phase || 1;
    $("projectExecutionMaxActivePhases").value = policy.maximum_active_phases || 1;
    $("projectExecutionFailureBehavior").value = policy.task_failure_behavior || "stop_new";
    const runtime = this.snapshot.automatic || {};
    const started = runtime.started_tasks || [];
    const failures = runtime.start_failures || [];
    $("projectExecutionAutomaticStatus").textContent = runtime.last_cycle_at
      ? `Last cycle ${runtime.last_cycle_at} · ${started.length} started · ${failures.length} start failure(s)`
      : "No Automatic cycle has run yet. Status reads never start Tasks.";
  }

  #renderAttention() {
    const rows = this.snapshot.attention || [];
    $("projectExecutionAttention").innerHTML = rows.length
      ? rows.map((row) => {
        const action = row.kind === "gate_awaiting_decision" ? "Review Gate" : row.kind === "project_hold" ? "Review hold" : "Inspect";
        return `<button type="button" class="project-execution-attention-row" data-execution-attention="${esc(row.kind)}:${esc(row.id || "")}"><span>!</span><div><strong>${esc(row.message)}</strong><small>${esc(titleCase(row.kind))}</small></div><span class="project-execution-attention-action">${esc(action)} →</span></button>`;
      }).join("")
      : '<div class="project-execution-clear"><span>✓</span><div><strong>No project-level attention required</strong><small>Current evidence, boundaries and execution state are consistent.</small></div></div>';
  }

  #renderQueues() {
    const taskById = new Map(this.snapshot.tasks.map((task) => [task.task_id, task]));
    const readyIds = this.snapshot.ready_tasks || [];
    $("projectExecutionReadyCount").textContent = String(readyIds.length);
    $("projectExecutionReadyTasks").innerHTML = readyIds.length
      ? readyIds.map((taskId) => {
        const task = taskById.get(taskId) || { task_id: taskId, title: taskId, phase: "—" };
        const canStart = this.snapshot.mode === "assisted" && !this.snapshot.held;
        return `<article class="project-execution-queue-row"><div><strong>${esc(task.title || task.task_id)}</strong><small>${esc(task.task_id)} · Phase ${esc(task.phase || "—")} · all Project prerequisites satisfied</small></div><div class="project-execution-task-actions">${canStart ? `<button type="button" class="btn tiny primary" data-project-task-start="${esc(task.task_id)}">Start Task</button>` : ""}<button type="button" class="btn tiny" data-project-task-open="${esc(task.task_id)}">Open</button></div></article>`;
      }).join("")
      : '<div class="project-execution-list-empty">No Task is currently eligible to start.</div>';

    const blocked = this.snapshot.blocked_tasks || [];
    $("projectExecutionBlockedCount").textContent = String(blocked.length);
    $("projectExecutionBlockedTasks").innerHTML = blocked.length
      ? blocked.map((row) => {
        const task = taskById.get(row.task_id) || { task_id: row.task_id, title: row.task_id, phase: "—" };
        const reasons = row.reasons || [];
        return `<article class="project-execution-queue-row"><div><strong>${esc(task.title || task.task_id)}</strong><small>${esc(task.task_id)} · Phase ${esc(task.phase || "—")}</small></div><div class="project-execution-task-actions"><button type="button" class="btn tiny" data-project-task-open="${esc(task.task_id)}">Open</button><button type="button" class="btn tiny" data-project-task-edit="${esc(task.task_id)}">Edit prerequisites</button></div><div class="project-execution-queue-reasons">${reasons.map((reason) => `<span>${esc(reason.message || titleCase(reason.kind))}</span>`).join("") || '<span>Not currently eligible</span>'}</div></article>`;
      }).join("")
      : '<div class="project-execution-list-empty">No not-started Task is blocked by Project Execution.</div>';
  }

  #renderPhases() {
    $("projectExecutionPhases").innerHTML = this.snapshot.phases.length
      ? this.snapshot.phases.map((phase) => `<button type="button" class="project-execution-card" data-project-asset="phase:${esc(phase.id)}"><div class="project-execution-card-head"><span class="asset-glyph phase">▰</span><div><strong>${esc(phase.title)}</strong><small>${esc(scheduleText(phase))}</small></div></div><div class="project-execution-card-status">${statusPill(phase.state)}${statusPill(phase.health)}</div><small>${esc((phase.health_reasons || []).join(" · ") || `${phase.tasks.length} task(s)`)}</small></button>`).join("")
      : '<div class="project-execution-list-empty">No Phases defined.</div>';
  }

  #renderGates() {
    $("projectExecutionGates").innerHTML = this.snapshot.gates.length
      ? this.snapshot.gates.map((gate) => `<button type="button" class="project-execution-card" data-project-asset="gate:${esc(gate.id)}"><div class="project-execution-card-head"><span class="asset-glyph gate">⬢</span><div><strong>${esc(gate.title)}</strong><small>${esc(scheduleText(gate))}</small></div></div><div class="project-execution-card-status">${statusPill(gate.state)}</div><small>${esc((gate.evaluation?.reasons || []).join(" · ") || `${gate.criteria?.all?.length || 0} criterion/criteria`)}</small></button>`).join("")
      : '<div class="project-execution-list-empty">No Gates defined.</div>';
  }

  #renderMilestones() {
    $("projectExecutionMilestones").innerHTML = this.snapshot.milestones.length
      ? this.snapshot.milestones.map((milestone) => `<button type="button" class="project-execution-card" data-project-asset="milestone:${esc(milestone.id)}"><div class="project-execution-card-head"><span class="asset-glyph milestone">◆</span><div><strong>${esc(milestone.title)}</strong><small>${esc(scheduleText(milestone))}</small></div></div><div class="project-execution-card-status">${statusPill(milestone.state)}${statusPill(milestone.health)}</div><small>${esc((milestone.missing_requirements || []).join(" · ") || milestone.delivery?.policy || "none")}</small></button>`).join("")
      : '<div class="project-execution-list-empty">No Milestones defined.</div>';
  }

  #renderTasks() {
    const assigned = new Set(this.snapshot.tasks.map((task) => task.task_id));
    const unassigned = this.availableTasks.filter((task) => !assigned.has(task.id) && task.status !== "archived");
    const rows = this.snapshot.tasks.map((task) => {
      const reasons = task.eligibility?.reasons || [];
      const ready = Boolean(task.eligibility?.eligible);
      return `<article class="project-execution-task"><div><strong>${esc(task.title || task.task_id)}</strong><small>${esc(task.task_id)} · Phase ${esc(task.phase)} · ${task.required ? "required" : "optional"}</small></div><div class="project-execution-task-status">${statusPill(task.outcome)}${ready ? '<span class="status available">eligible</span>' : ""}</div><div class="project-execution-task-reasons">${reasons.length ? reasons.map((reason) => `<span>${esc(reason.message || reason.kind)}</span>`).join("") : '<span>All project prerequisites satisfied.</span>'}</div><div class="project-execution-task-actions">${ready && this.snapshot.mode === "assisted" && !this.snapshot.held ? `<button type="button" class="btn tiny primary" data-project-task-start="${esc(task.task_id)}">Start Task</button>` : ""}<button type="button" class="btn tiny" data-project-task-open="${esc(task.task_id)}">Open</button><button type="button" class="btn tiny" data-project-task-edit="${esc(task.task_id)}">Edit metadata</button><button type="button" class="btn tiny danger" data-project-task-remove="${esc(task.task_id)}">Remove</button></div></article>`;
    });
    if (unassigned.length) {
      rows.push(`<div class="project-execution-unassigned"><strong>Canonical Tasks not yet in Project Execution</strong>${unassigned.map((task) => `<button type="button" class="btn tiny" data-project-task-assign="${esc(task.id)}">＋ ${esc(task.title || task.id)}</button>`).join("")}</div>`);
    }
    $("projectExecutionTasks").innerHTML = rows.join("") || '<div class="project-execution-list-empty">No Tasks assigned to Project Execution.</div>';
  }

  #renderInspector() {
    const target = $("projectExecutionInspector");
    if (!this.selected) {
      target.className = "project-execution-inspector empty";
      target.innerHTML = "Select a Phase, Gate or Milestone to inspect its canonical definition and runtime evidence.";
      return;
    }
    const [kind, id] = this.selected.split(":", 2);
    const collection = { phase: "phases", gate: "gates", milestone: "milestones" }[kind];
    const item = this.snapshot[collection]?.find((row) => row.id === id);
    if (!item) {
      this.selected = null;
      return this.#renderInspector();
    }
    target.className = "project-execution-inspector";
    target.innerHTML = kind === "phase"
      ? this.#phaseInspector(item)
      : kind === "gate"
        ? this.#gateInspector(item)
        : this.#milestoneInspector(item);
  }

  #phaseInspector(phase) {
    const requiredTasks = phase.tasks
      .map((taskId) => this.snapshot.tasks.find((task) => task.task_id === taskId))
      .filter((task) => task?.required);
    const completionRows = [
      ...requiredTasks.map((task) => this.#checklistRow(
        task.outcome === "completed",
        task.title || task.task_id,
        `Task · ${task.outcome}`,
      )),
      ...phase.exit_gates.map((gateId) => {
        const gate = this.snapshot.gates.find((row) => row.id === gateId);
        return this.#checklistRow(["passed", "waived"].includes(gate?.state), gate?.title || gateId, `Exit Gate · ${gate?.state || "missing"}`);
      }),
      ...phase.milestones.map((milestoneId) => {
        const milestone = this.snapshot.milestones.find((row) => row.id === milestoneId);
        return this.#checklistRow(milestone?.state === "achieved", milestone?.title || milestoneId, `Milestone · ${milestone?.state || "missing"}`);
      }),
    ];
    const pending = completionRows.filter((row) => !row.satisfied).length;
    const entryRows = phase.entry_gates.map((gateId) => {
      const gate = this.snapshot.gates.find((row) => row.id === gateId);
      return this.#checklistRow(["passed", "waived"].includes(gate?.state), gate?.title || gateId, `Entry Gate · ${gate?.state || "missing"}`);
    });
    return `<header><span class="asset-glyph phase">▰</span><div><span class="eyebrow">Phase</span><h3>${esc(phase.title)}</h3><small>${esc(phase.id)}</small></div><div class="project-execution-inspector-actions"><button type="button" class="btn tiny" data-project-asset-edit="phase:${esc(phase.id)}">Edit</button><button type="button" class="btn tiny danger" data-project-asset-delete="phase:${esc(phase.id)}">Delete</button></div></header><p>${esc(phase.description || "No description.")}</p><dl class="project-execution-facts"><div><dt>Lifecycle</dt><dd>${statusPill(phase.state)} ${statusPill(phase.health)}</dd></div><div><dt>Schedule</dt><dd>${esc(scheduleText(phase))}</dd></div><div><dt>Completion</dt><dd>${pending ? `${pending} requirement(s) remain` : "all required completion conditions satisfied"}</dd></div></dl>${entryRows.length ? `<section><h4>Entry boundary</h4><div class="project-execution-checklist">${entryRows.map((row) => row.html).join("")}</div></section>` : ""}<section><h4>Completion requirements</h4><div class="project-execution-checklist">${completionRows.length ? completionRows.map((row) => row.html).join("") : '<div class="project-execution-list-empty">No required Tasks, exit Gates or Milestones are defined.</div>'}</div></section><div class="project-execution-reasons">${(phase.health_reasons || []).map((reason) => `<span>${esc(reason)}</span>`).join("")}</div>`;
  }

  #checklistRow(satisfied, label, detail) {
    return {
      satisfied: Boolean(satisfied),
      html: `<div class="project-execution-checklist-row ${satisfied ? "good" : "wait"}"><span>${satisfied ? "✓" : "○"}</span><strong>${esc(label)}</strong><small>${esc(detail)}</small></div>`,
    };
  }

  #gateInspector(gate) {
    const decisions = gate.decision_history || [];
    const canDecide = gate.state === "awaiting_decision";
    const waiver = gate.waiver || {};
    return `<header><span class="asset-glyph gate">⬢</span><div><span class="eyebrow">Gate</span><h3>${esc(gate.title)}</h3><small>${esc(gate.id)}</small></div><div class="project-execution-inspector-actions"><button type="button" class="btn tiny" data-project-asset-edit="gate:${esc(gate.id)}">Edit</button><button type="button" class="btn tiny danger" data-project-asset-delete="gate:${esc(gate.id)}">Delete</button></div></header><p>${esc(gate.description || "No description.")}</p><dl class="project-execution-facts"><div><dt>State</dt><dd>${statusPill(gate.state)}</dd></div><div><dt>Schedule</dt><dd>${esc(scheduleText(gate))}</dd></div><div><dt>Fingerprint</dt><dd><code title="${esc(gate.input_fingerprint || "")}">${esc(shortDigest(gate.input_fingerprint, 28))}</code></dd></div><div><dt>Evaluation</dt><dd>revision ${esc(gate.evaluation?.revision || 0)} · ${esc(gate.evaluation?.evaluated_at || "not evaluated")}</dd></div></dl><section><h4>Current typed evidence</h4><div class="project-execution-evidence">${this.#gateEvidenceMarkup(gate)}</div></section>${gate.evaluation?.reasons?.length ? `<section><h4>Failure / waiting reasons</h4><div class="project-execution-reasons">${gate.evaluation.reasons.map((reason) => `<span>${esc(reason)}</span>`).join("")}</div></section>` : ""}<section><h4>Decision history</h4><div class="project-execution-evidence">${decisions.length ? decisions.map((decision) => `<div><strong>${esc(titleCase(decision.decision))} · ${esc(decision.actor)}</strong><small>${esc(decision.timestamp || "")} ${decision.reason ? `· ${esc(decision.reason)}` : ""}<br>fingerprint ${esc(shortDigest(decision.input_fingerprint, 22))}</small></div>`).join("") : '<span class="muted">No decisions recorded.</span>'}${waiver.actor ? `<div><strong>Waived · ${esc(waiver.actor)}</strong><small>${esc(waiver.timestamp || "")} · ${esc(waiver.reason || "")}<br>fingerprint ${esc(shortDigest(waiver.input_fingerprint, 22))}</small></div>` : ""}</div></section><div class="project-execution-inspector-actions">${canDecide ? `<button type="button" class="btn tiny primary" data-project-gate-decision="approved:${esc(gate.id)}">Review &amp; approve</button><button type="button" class="btn tiny danger" data-project-gate-decision="rejected:${esc(gate.id)}">Review &amp; reject</button>` : ""}${!["passed", "waived", "cancelled"].includes(gate.state) ? `<button type="button" class="btn tiny warn" data-project-gate-waive="${esc(gate.id)}">Review waiver</button>` : ""}</div>`;
  }

  #gateEvidenceMarkup(gate) {
    const criteria = gate.criteria?.all || [];
    const evidence = gate.evaluation?.evidence || [];
    if (!criteria.length) return '<div class="project-execution-list-empty">No Gate criteria are defined.</div>';
    return criteria.map((criterion, index) => this.#criterionEvidenceMarkup(criterion, evidence[index] || {})).join("");
  }

  #criterionEvidenceMarkup(criterion, evidence) {
    let state = "wait";
    let icon = "○";
    let label = titleCase(criterion.type || "criterion");
    let detail = "No evaluated evidence is available yet.";
    if (criterion.type === "task_completion") {
      const outcome = evidence.outcome || "unknown";
      const good = outcome === "completed";
      state = good ? "good" : "bad";
      icon = good ? "✓" : "!";
      label = `Task completion · ${criterion.task_id || evidence.task_id || "unknown"}`;
      detail = `Observed outcome: ${outcome}`;
    } else if (criterion.type === "task_verification") {
      const verification = evidence.verification || {};
      const expected = criterion.outcome || "passed";
      const good = verification.outcome === expected;
      state = good ? "good" : "bad";
      icon = good ? "✓" : "!";
      label = `Task verification · ${criterion.task_id || evidence.task_id || "unknown"}`;
      detail = `${verification.outcome || "unknown"} · expected ${expected} · ${verification.passed || 0} passed / ${verification.failed || 0} failed / ${verification.total || 0} total`;
    } else if (criterion.type === "task_artifact") {
      const artifacts = evidence.artifacts || [];
      const artifactId = criterion.artifact_id || evidence.artifact_id || "artifact";
      const good = artifacts.includes(artifactId);
      state = good ? "good" : "bad";
      icon = good ? "✓" : "!";
      label = `Task artifact · ${criterion.task_id || evidence.task_id || "unknown"}`;
      detail = good ? `Produced ${artifactId}` : `${artifactId} is not present in current Task artifacts`;
    } else if (criterion.type === "project_gate") {
      const gateState = evidence.state || "waiting";
      const good = ["passed", "waived"].includes(gateState);
      state = good ? "good" : gateState === "failed" ? "bad" : "wait";
      icon = good ? "✓" : gateState === "failed" ? "!" : "○";
      label = `Project Gate · ${criterion.gate_id || evidence.gate_id || "unknown"}`;
      detail = `Current state: ${gateState} · evaluation rev ${evidence.evaluation_revision || 0}`;
    } else if (criterion.type === "human_approval") {
      const decision = evidence.decision || "pending";
      const good = decision === "approved";
      state = good ? "good" : decision === "rejected" ? "bad" : "wait";
      icon = good ? "✓" : decision === "rejected" ? "!" : "?";
      label = "Human approval";
      detail = decision === "pending" ? "Awaiting an evidence-bound operator decision" : `${titleCase(decision)} by ${evidence.actor || "operator"}`;
    }
    return `<div class="project-execution-evidence-row"><span class="project-execution-evidence-icon ${state}">${icon}</span><div><strong>${esc(label)}</strong><small>${esc(detail)}</small></div></div>`;
  }

  #milestoneInspector(milestone) {
    const baseline = milestone.achievement || {};
    const requirements = [
      ...(milestone.requires?.tasks || []).map((taskId) => {
        const task = this.snapshot.tasks.find((row) => row.task_id === taskId);
        return this.#checklistRow(task?.outcome === "completed", task?.title || taskId, `Task · ${task?.outcome || "missing"}`);
      }),
      ...(milestone.requires?.gates || []).map((gateId) => {
        const gate = this.snapshot.gates.find((row) => row.id === gateId);
        return this.#checklistRow(["passed", "waived"].includes(gate?.state), gate?.title || gateId, `Gate · ${gate?.state || "missing"}`);
      }),
      ...(milestone.requires?.milestones || []).map((milestoneId) => {
        const dependency = this.snapshot.milestones.find((row) => row.id === milestoneId);
        return this.#checklistRow(dependency?.state === "achieved", dependency?.title || milestoneId, `Milestone · ${dependency?.state || "missing"}`);
      }),
    ];
    return `<header><span class="asset-glyph milestone">◆</span><div><span class="eyebrow">Milestone</span><h3>${esc(milestone.title)}</h3><small>${esc(milestone.id)}</small></div><div class="project-execution-inspector-actions"><button type="button" class="btn tiny" data-project-asset-edit="milestone:${esc(milestone.id)}">Edit</button><button type="button" class="btn tiny danger" data-project-asset-delete="milestone:${esc(milestone.id)}">Delete</button></div></header><p>${esc(milestone.description || "No description.")}</p><dl class="project-execution-facts"><div><dt>Lifecycle</dt><dd>${statusPill(milestone.state)} ${statusPill(milestone.health)}</dd></div><div><dt>Target</dt><dd>${esc(scheduleText(milestone))}</dd></div><div><dt>Delivery</dt><dd>${esc(milestone.delivery?.policy || "none")}</dd></div>${baseline.achieved_at ? `<div><dt>Achieved</dt><dd>${esc(baseline.achieved_at)}</dd></div>` : ""}</dl><section><h4>Achievement requirements</h4><div class="project-execution-checklist">${requirements.length ? requirements.map((row) => row.html).join("") : '<div class="project-execution-list-empty">No explicit requirements are defined.</div>'}</div></section><section><h4>Immutable achievement baseline</h4>${milestone.state === "achieved" ? this.#baselineMarkup(baseline) : `<div class="project-execution-reasons">${(milestone.missing_requirements || []).map((reason) => `<span>${esc(reason)}</span>`).join("") || '<span>Requirements are satisfied; baseline capture will occur during reconciliation.</span>'}</div>`}</section>`;
  }

  #baselineMarkup(baseline) {
    const repositories = Object.entries(baseline.repositories || {});
    const tasks = Object.entries(baseline.tasks || {});
    const gates = Object.entries(baseline.gates || {});
    const artifacts = baseline.artifacts || [];
    return `<div class="project-execution-baseline-grid"><div class="project-execution-baseline-section"><strong>Repositories</strong>${repositories.length ? repositories.map(([id, commit]) => `<div class="project-execution-baseline-row"><span>${esc(id)}</span><code title="${esc(commit)}">${esc(shortDigest(commit, 18))}</code></div>`).join("") : '<span class="muted">No repository revisions captured.</span>'}</div><div class="project-execution-baseline-section"><strong>Tasks</strong>${tasks.length ? tasks.map(([id, row]) => `<div class="project-execution-baseline-row"><span>${esc(id)} · ${esc(row.outcome || "unknown")}</span><code>verify ${esc(row.verification?.outcome || "unknown")}</code></div>`).join("") : '<span class="muted">No Task evidence captured.</span>'}</div><div class="project-execution-baseline-section"><strong>Gates</strong>${gates.length ? gates.map(([id, row]) => `<div class="project-execution-baseline-row"><span>${esc(id)} · ${esc(row.state || "unknown")}</span><code>eval rev ${esc(row.evaluation_revision || 0)}</code></div>`).join("") : '<span class="muted">No Gate evidence captured.</span>'}</div><div class="project-execution-baseline-section"><strong>Artifacts</strong>${artifacts.length ? artifacts.map((artifact) => `<div class="project-execution-baseline-row"><span>${esc(artifact)}</span></div>`).join("") : '<span class="muted">No artifact identifiers captured.</span>'}</div><details><summary>Advanced · raw immutable baseline</summary><pre class="project-execution-baseline">${esc(JSON.stringify(baseline, null, 2))}</pre></details></div>`;
  }

  #bind() {
    $("projectExecutionRefreshBtn").addEventListener("click", () => this.load({ force: true }));
    $("projectExecutionExportBtn").addEventListener("click", () => this.#exportProjectReport());
    $("projectExecutionCoordinationBtn").addEventListener("click", () => void this.#toggleCoordinationInspector(true));
    $("projectExecutionCoordinationClose").addEventListener("click", () => this.#toggleCoordinationInspector(false));
    $("projectExecutionInitialize").addEventListener("click", () => this.#initialize());
    $("projectExecutionMode").addEventListener("change", () => this.#setMode());
    $("projectExecutionHoldBtn").addEventListener("click", () => this.#toggleHold());
    $("projectExecutionAutomaticCycleBtn").addEventListener("click", () => this.#runAutomaticCycle());
    $("projectExecutionSavePolicy").addEventListener("click", () => this.#saveAutomaticPolicy());
    for (const [buttonId, kind] of [["projectExecutionAddPhase", "phase"], ["projectExecutionAddGate", "gate"], ["projectExecutionAddMilestone", "milestone"]]) {
      $(buttonId).addEventListener("click", () => this.#openAssetEditor(kind));
    }
    $("projectExecutionWorkspace").addEventListener("click", (event) => this.#handleAction(event));
    $("projectExecutionAssetCancel").addEventListener("click", () => $("projectExecutionAssetDialog").close());
    $("projectExecutionAssetDialog").addEventListener("submit", (event) => {
      event.preventDefault();
      void this.#saveAsset();
    });
    $("projectExecutionGateAddCriterion").addEventListener("click", () => this.#addGateCriterion());
    $("projectExecutionGateCriteria").addEventListener("click", (event) => {
      const remove = event.target.closest("[data-gate-criterion-remove]");
      if (remove) remove.closest("[data-gate-criterion-row]")?.remove();
    });
    $("projectExecutionGateCriteria").addEventListener("change", (event) => {
      const select = event.target.closest("[data-gate-criterion-type]");
      if (select) this.#syncGateCriterionRow(select.closest("[data-gate-criterion-row]"));
    });
    $("projectExecutionTaskCancel").addEventListener("click", () => $("projectExecutionTaskDialog").close());
    $("projectExecutionTaskDialog").addEventListener("submit", (event) => {
      event.preventDefault();
      void this.#saveTaskMetadata();
    });
    for (const dialogId of ["projectExecutionAssetDialog", "projectExecutionTaskDialog"]) {
      $(dialogId).addEventListener("click", (event) => this.#handleReferencePickerAction(event));
      $(dialogId).addEventListener("change", (event) => this.#handleReferencePickerAction(event));
    }
    $("projectExecutionGateDecisionCancel").addEventListener("click", () => $("projectExecutionGateDecisionDialog").close());
    $("projectExecutionGateDecisionDialog").addEventListener("submit", (event) => {
      event.preventDefault();
      void this.#submitGateDecision();
    });
    $("projectExecutionHoldCancel").addEventListener("click", () => $("projectExecutionHoldDialog").close());
    $("projectExecutionHoldDialog").addEventListener("submit", (event) => {
      event.preventDefault();
      void this.#submitHold();
    });
  }

  async #exportProjectReport() {
    if (!this.projectId || !this.download) return;
    try {
      const filename = await this.download(`/api/export/project?project_id=${encodeURIComponent(this.projectId)}&format=pdf`);
      this.toast(`Exported ${filename}`);
    } catch (error) {
      this.toast(error.message, true);
    }
  }

  async #handleAction(event) {
    const reconcile = event.target.closest("[data-project-execution-reconcile]");
    if (reconcile) {
      await this.load({ force: true });
      return;
    }
    const coordinationInspect = event.target.closest("[data-project-coordination-inspect]");
    if (coordinationInspect) {
      await this.#toggleCoordinationInspector(true);
      return;
    }
    const coordinationAction = event.target.closest("[data-project-coordination-action]");
    if (coordinationAction) {
      await this.#resolveCoordination(coordinationAction.dataset.projectCoordinationAction);
      return;
    }
    const attention = event.target.closest("[data-execution-attention]");
    if (attention) {
      const [kind, id] = attention.dataset.executionAttention.split(":", 2);
      if (kind === "project_hold") {
        $("projectExecutionHoldBtn").scrollIntoView({ block: "nearest", behavior: "smooth" });
        $("projectExecutionHoldBtn").focus();
        return;
      }
      const assetKind = kind.startsWith("gate_") ? "gate" : kind.startsWith("phase_") ? "phase" : kind.startsWith("milestone_") ? "milestone" : "";
      if (assetKind && id) {
        this.selected = `${assetKind}:${id}`;
        this.#renderInspector();
      }
      return;
    }
    const asset = event.target.closest("[data-project-asset]");
    if (asset) {
      this.selected = asset.dataset.projectAsset;
      this.#renderInspector();
      return;
    }
    const edit = event.target.closest("[data-project-asset-edit]");
    if (edit) {
      const [kind, id] = edit.dataset.projectAssetEdit.split(":", 2);
      this.#openAssetEditor(kind, id);
      return;
    }
    const removeAsset = event.target.closest("[data-project-asset-delete]");
    if (removeAsset) {
      const [kind, id] = removeAsset.dataset.projectAssetDelete.split(":", 2);
      return this.#deleteAsset(kind, id);
    }
    const start = event.target.closest("[data-project-task-start]");
    if (start) return this.#startTask(start.dataset.projectTaskStart);
    const open = event.target.closest("[data-project-task-open]");
    if (open) return this.onOpenTask(this.projectId, open.dataset.projectTaskOpen);
    const assign = event.target.closest("[data-project-task-assign]");
    if (assign) return this.#openTaskEditor(assign.dataset.projectTaskAssign);
    const taskEdit = event.target.closest("[data-project-task-edit]");
    if (taskEdit) return this.#openTaskEditor(taskEdit.dataset.projectTaskEdit);
    const taskRemove = event.target.closest("[data-project-task-remove]");
    if (taskRemove) return this.#removeTask(taskRemove.dataset.projectTaskRemove);
    const decision = event.target.closest("[data-project-gate-decision]");
    if (decision) {
      const [value, gateId] = decision.dataset.projectGateDecision.split(":", 2);
      return this.#openGateDecisionDialog(gateId, value);
    }
    const waive = event.target.closest("[data-project-gate-waive]");
    if (waive) return this.#openGateDecisionDialog(waive.dataset.projectGateWaive, "waived");
  }

  async #initialize() {
    await this.#mutate("/api/project-execution/initialize", { project_id: this.projectId, mode: "assisted", acknowledged: true });
  }

  async #setMode() {
    const previous = this.snapshot.mode;
    try {
      await this.#mutate("/api/project-execution/mode", { project_id: this.projectId, mode: $("projectExecutionMode").value, expected_revision: this.snapshot.definition_revision, acknowledged: true });
    } catch (_error) {
      $("projectExecutionMode").value = previous;
    }
  }

  async #saveAutomaticPolicy() {
    const policy = {
      maximum_parallel_tasks: Number($("projectExecutionMaxParallel").value),
      maximum_parallel_tasks_per_phase: Number($("projectExecutionMaxPerPhase").value),
      maximum_active_phases: Number($("projectExecutionMaxActivePhases").value),
      task_failure_behavior: $("projectExecutionFailureBehavior").value,
    };
    await this.#mutate("/api/project-execution/policy", {
      project_id: this.projectId,
      policy,
      expected_revision: this.snapshot.definition_revision,
      acknowledged: true,
    });
  }

  async #runAutomaticCycle() {
    if (!window.confirm("Run one bounded Automatic Project Execution cycle now?")) return;
    const result = await this.#mutate("/api/project-execution/automatic/cycle", {
      project_id: this.projectId,
      acknowledged: true,
    });
    const started = result.cycle?.started_tasks || [];
    const failures = result.cycle?.start_failures || [];
    this.toast(failures.length
      ? `Automatic cycle stopped after ${failures.length} start failure(s)`
      : `Automatic cycle started ${started.length} Task(s)`);
  }

  async #toggleHold() {
    if (this.snapshot.held) {
      await this.#mutate("/api/project-execution/resume", { project_id: this.projectId, acknowledged: true });
      return;
    }
    $("projectExecutionHoldReason").value = "operator hold";
    $("projectExecutionHoldDialog").showModal();
    queueMicrotask(() => $("projectExecutionHoldReason")?.focus());
  }

  async #submitHold() {
    const reason = $("projectExecutionHoldReason").value.trim();
    if (!reason) {
      this.toast("A Project Execution hold requires a reason", true);
      return;
    }
    try {
      await this.#mutate("/api/project-execution/pause", { project_id: this.projectId, reason, acknowledged: true });
      $("projectExecutionHoldDialog").close();
    } catch (_error) { /* toast handled by #mutate */ }
  }

  async #startTask(taskId) {
    if (!window.confirm(`Start canonical Task “${taskId}” now?`)) return;
    try {
      const result = await this.api("/api/project-execution/task/start", { method: "POST", body: JSON.stringify({ project_id: this.projectId, task_id: taskId, acknowledged: true }) });
      this.snapshot = result.project_execution;
      this.stale = false;
      this.render();
      this.onCanonicalChange();
      this.toast(result.result?.message || `Task ${taskId} started`);
      await this.onOpenTask(this.projectId, taskId);
    } catch (error) {
      this.toast(error.message, true);
    }
  }

  #openGateDecisionDialog(gateId, decision) {
    const gate = this.snapshot.gates.find((row) => row.id === gateId);
    if (!gate) {
      this.toast(`Project Gate ${gateId} is not available`, true);
      return;
    }
    const waiver = decision === "waived";
    const rejected = decision === "rejected";
    $("projectExecutionGateDecisionId").value = gateId;
    $("projectExecutionGateDecisionValue").value = decision;
    $("projectExecutionGateDecisionEyebrow").textContent = waiver ? "Explicit evidence-bound waiver" : "Evidence-bound human decision";
    $("projectExecutionGateDecisionTitle").textContent = `${waiver ? "Waive" : rejected ? "Reject" : "Approve"} · ${gate.title}`;
    $("projectExecutionGateDecisionState").textContent = titleCase(gate.state);
    $("projectExecutionGateDecisionFingerprint").textContent = gate.input_fingerprint || "not evaluated";
    $("projectExecutionGateDecisionEvidence").innerHTML = this.#gateEvidenceMarkup(gate);
    $("projectExecutionGateDecisionActor").value = localStorage.getItem("execraft-project-gate-actor") || "operator";
    $("projectExecutionGateDecisionReason").value = "";
    $("projectExecutionGateDecisionReason").required = waiver;
    $("projectExecutionGateDecisionReason").placeholder = waiver ? "Waiver reason is required" : "Explain the decision (optional)";
    const warning = $("projectExecutionGateDecisionWarning");
    warning.classList.toggle("hidden", !waiver);
    warning.textContent = waiver
      ? "WAIVED remains historically distinct from PASSED. This waiver only satisfies policy for the exact evidence fingerprint shown above."
      : "";
    const submit = $("projectExecutionGateDecisionSubmit");
    submit.textContent = waiver ? "Record waiver" : rejected ? "Record rejection" : "Record approval";
    submit.className = `btn ${waiver ? "warn" : rejected ? "danger" : "primary"}`;
    $("projectExecutionGateDecisionDialog").showModal();
    queueMicrotask(() => $("projectExecutionGateDecisionActor")?.focus());
  }

  async #submitGateDecision() {
    const gateId = $("projectExecutionGateDecisionId").value;
    const decision = $("projectExecutionGateDecisionValue").value;
    const actor = $("projectExecutionGateDecisionActor").value.trim();
    const reason = $("projectExecutionGateDecisionReason").value.trim();
    if (!actor) {
      this.toast("Gate decision actor is required", true);
      return;
    }
    if (decision === "waived" && !reason) {
      this.toast("A Gate waiver requires a reason", true);
      return;
    }
    localStorage.setItem("execraft-project-gate-actor", actor);
    try {
      const path = decision === "waived"
        ? "/api/project-execution/gate/waive"
        : "/api/project-execution/gate/decide";
      const payload = decision === "waived"
        ? { project_id: this.projectId, gate_id: gateId, actor, reason, acknowledged: true }
        : { project_id: this.projectId, gate_id: gateId, actor, decision, reason, acknowledged: true };
      const result = await this.#post(path, payload);
      this.snapshot = result.project_execution;
      this.stale = false;
      $("projectExecutionGateDecisionDialog").close();
      this.selected = `gate:${gateId}`;
      this.render();
      this.onCanonicalChange();
      this.toast(decision === "waived" ? `Gate ${gateId} waived for current evidence` : `Gate ${gateId} ${decision}`);
    } catch (error) {
      this.toast(error.message, true);
    }
  }

  async #deleteAsset(kind, id) {
    if (!window.confirm(`Delete canonical Project ${kind} “${id}”? Referenced or historically significant assets are protected.`)) return;
    try {
      await this.#mutate(`/api/project-execution/${kind}/delete`, {
        project_id: this.projectId,
        [`${kind}_id`]: id,
        expected_revision: this.snapshot.definition_revision,
      });
      this.selected = null;
      this.render();
    } catch (_error) { /* toast handled by #mutate */ }
  }

  async #removeTask(taskId) {
    if (!window.confirm(`Remove Task “${taskId}” from Project Execution? The canonical Task and its runtime history are not deleted.`)) return;
    try {
      await this.#mutate("/api/project-execution/task/remove", {
        project_id: this.projectId,
        task_id: taskId,
        expected_revision: this.snapshot.definition_revision,
      });
    } catch (_error) { /* toast handled by #mutate */ }
  }

  #openAssetEditor(kind, id = "") {
    const collection = { phase: "phases", gate: "gates", milestone: "milestones" }[kind];
    const item = this.snapshot?.[collection]?.find((row) => row.id === id) || null;
    $("projectExecutionAssetKind").value = kind;
    $("projectExecutionAssetOriginalId").value = id;
    $("projectExecutionAssetDialogTitle").textContent = `${item ? "Edit" : "New"} ${kind[0].toUpperCase()}${kind.slice(1)}`;
    $("projectExecutionAssetId").value = item?.id || "";
    $("projectExecutionAssetId").disabled = Boolean(item);
    $("projectExecutionAssetTitle").value = item?.title || "";
    $("projectExecutionAssetDescription").value = item?.description || "";
    $("projectExecutionAssetStart").value = item?.schedule?.start || item?.start || "";
    $("projectExecutionAssetTarget").value = item?.schedule?.target || item?.target || "";
    $("projectExecutionPhaseFields").classList.toggle("hidden", kind !== "phase");
    $("projectExecutionGateFields").classList.toggle("hidden", kind !== "gate");
    $("projectExecutionMilestoneFields").classList.toggle("hidden", kind !== "milestone");
    if (kind === "gate") this.#renderGateCriteria(item?.criteria?.all || [{ type: "human_approval" }]);
    this.#renderReferencePicker("projectExecutionPhaseEntryGates", "gate", item?.entry_gates || []);
    this.#renderReferencePicker("projectExecutionPhaseExitGates", "gate", item?.exit_gates || []);
    this.#renderReferencePicker("projectExecutionPhaseMilestones", "milestone", item?.milestones || []);
    this.#renderReferencePicker("projectExecutionMilestoneTasks", "task", item?.requires?.tasks || []);
    this.#renderReferencePicker("projectExecutionMilestoneGates", "gate", item?.requires?.gates || []);
    this.#renderReferencePicker("projectExecutionMilestoneMilestones", "milestone", item?.requires?.milestones || [], { exclude: id ? [id] : [] });
    $("projectExecutionMilestoneDelivery").value = item?.delivery?.policy || "none";
    $("projectExecutionAssetDialog").showModal();
    queueMicrotask(() => $("projectExecutionAssetId")?.focus());
  }

  async #saveAsset() {
    const kind = $("projectExecutionAssetKind").value;
    const id = $("projectExecutionAssetId").value.trim();
    const common = {
      id,
      title: $("projectExecutionAssetTitle").value.trim(),
      description: $("projectExecutionAssetDescription").value.trim(),
    };
    const start = $("projectExecutionAssetStart").value;
    const target = $("projectExecutionAssetTarget").value;
    let asset;
    if (kind === "phase") {
      asset = { ...common, schedule: { start, target }, entry_gates: this.#readReferencePicker("projectExecutionPhaseEntryGates"), exit_gates: this.#readReferencePicker("projectExecutionPhaseExitGates"), tasks: this.snapshot.phases.find((row) => row.id === id)?.tasks || [], milestones: this.#readReferencePicker("projectExecutionPhaseMilestones") };
    } else if (kind === "gate") {
      const criteria = this.#gateCriteriaDraft();
      if (!criteria.length) {
        this.toast("A Project Gate requires at least one typed criterion", true);
        return;
      }
      asset = { ...common, schedule: { start, target }, criteria: { all: criteria } };
    } else {
      asset = { ...common, schedule: { start }, target, requires: { tasks: this.#readReferencePicker("projectExecutionMilestoneTasks"), gates: this.#readReferencePicker("projectExecutionMilestoneGates"), milestones: this.#readReferencePicker("projectExecutionMilestoneMilestones") }, delivery: { policy: $("projectExecutionMilestoneDelivery").value } };
    }
    try {
      await this.#mutate(`/api/project-execution/${kind}/upsert`, { project_id: this.projectId, expected_revision: this.snapshot.definition_revision, [kind]: asset });
      $("projectExecutionAssetDialog").close();
      this.selected = `${kind}:${id}`;
      this.render();
    } catch (_error) { /* toast handled by #mutate */ }
  }

  #referenceOptions(kind, exclude = []) {
    const excluded = new Set(exclude);
    if (kind === "task") {
      return this.snapshot.tasks
        .filter((task) => !excluded.has(task.task_id))
        .map((task) => ({ id: task.task_id, title: task.title || task.task_id }));
    }
    const collection = kind === "gate" ? this.snapshot.gates : this.snapshot.milestones;
    return collection
      .filter((item) => !excluded.has(item.id))
      .map((item) => ({ id: item.id, title: item.title || item.id }));
  }

  #referenceLabel(kind) {
    return kind === "task" ? "Task" : kind === "gate" ? "Project Gate" : "Project Milestone";
  }

  #renderReferencePicker(id, kind, values = [], { exclude = [] } = {}) {
    const target = $(id);
    if (!target) return;
    const selected = [...new Set(values.filter(Boolean))];
    const options = this.#referenceOptions(kind, exclude);
    const labels = new Map(options.map((option) => [option.id, option.title]));
    target.dataset.referenceKind = kind;
    target.dataset.referenceValues = JSON.stringify(selected);
    target.dataset.referenceExclude = JSON.stringify(exclude);
    const chips = selected.length
      ? selected.map((value) => `<span class="project-execution-reference-chip"><span title="${esc(value)}">${esc(labels.get(value) || value)}</span><button type="button" data-reference-remove="${esc(value)}" aria-label="Remove ${esc(value)}">×</button></span>`).join("")
      : '<span class="project-execution-reference-empty">None selected</span>';
    const remaining = options.filter((option) => !selected.includes(option.id));
    target.innerHTML = `<div class="project-execution-reference-chips">${chips}</div><select data-reference-add aria-label="Add ${esc(this.#referenceLabel(kind))}"><option value="">＋ Add ${esc(this.#referenceLabel(kind))}</option>${remaining.map((option) => `<option value="${esc(option.id)}">${esc(option.title)} · ${esc(option.id)}</option>`).join("")}</select>`;
    target.querySelector("[data-reference-add]").disabled = !remaining.length;
  }

  #readReferencePicker(id) {
    const target = $(id);
    if (!target) return [];
    try {
      return JSON.parse(target.dataset.referenceValues || "[]");
    } catch (_error) {
      return [];
    }
  }

  #handleReferencePickerAction(event) {
    const remove = event.target.closest?.("[data-reference-remove]");
    const add = event.target.closest?.("[data-reference-add]");
    const picker = (remove || add)?.closest(".project-execution-reference-picker");
    if (!picker) return;
    const values = this.#readReferencePicker(picker.id);
    const kind = picker.dataset.referenceKind;
    let exclude = [];
    try { exclude = JSON.parse(picker.dataset.referenceExclude || "[]"); } catch (_error) { exclude = []; }
    if (remove) {
      this.#renderReferencePicker(picker.id, kind, values.filter((value) => value !== remove.dataset.referenceRemove), { exclude });
      this.#focusReferencePicker(picker);
      return;
    }
    if (add && add.value) {
      const addedValue = add.value;
      this.#renderReferencePicker(picker.id, kind, [...values, addedValue], { exclude });
      this.#focusReferencePicker(picker, addedValue);
    }
  }

  #focusReferencePicker(picker, preferredValue = "") {
    queueMicrotask(() => {
      const add = picker.querySelector("[data-reference-add]:not(:disabled)");
      const escaped = preferredValue && globalThis.CSS?.escape ? CSS.escape(preferredValue) : "";
      const preferredRemove = escaped ? picker.querySelector(`[data-reference-remove="${escaped}"]`) : null;
      (add || preferredRemove || picker.querySelector("[data-reference-remove]"))?.focus();
    });
  }

  #renderGateCriteria(criteria) {
    const target = $("projectExecutionGateCriteria");
    target.innerHTML = "";
    for (const criterion of criteria) this.#addGateCriterion(criterion);
  }

  #addGateCriterion(criterion = { type: "task_completion" }) {
    const row = document.createElement("div");
    row.className = "project-execution-criterion-row";
    row.dataset.gateCriterionRow = "";
    const taskOptions = this.#referenceOptions("task");
    const currentGateId = $("projectExecutionAssetOriginalId").value || $("projectExecutionAssetId").value.trim();
    const gateOptions = this.#referenceOptions("gate", currentGateId ? [currentGateId] : []);
    const taskSelect = this.#selectOptions(taskOptions, criterion.task_id || "", "Select Task");
    const gateSelect = this.#selectOptions(gateOptions, criterion.gate_id || "", "Select upstream Gate");
    row.innerHTML = `<label>Type<select data-gate-criterion-type>
      <option value="task_completion">Task completion</option>
      <option value="task_verification">Task verification</option>
      <option value="task_artifact">Task artifact</option>
      <option value="project_gate">Project Gate</option>
      <option value="human_approval">Human approval</option>
    </select></label>
    <label data-criterion-field="task">Task<select data-gate-criterion-task>${taskSelect}</select></label>
    <label data-criterion-field="gate">Upstream Gate<select data-gate-criterion-gate>${gateSelect}</select></label>
    <label data-criterion-field="outcome">Outcome<input data-gate-criterion-outcome type="text" value="${esc(criterion.outcome || "passed")}" placeholder="passed"></label>
    <label data-criterion-field="artifact">Artifact ID<input data-gate-criterion-artifact type="text" value="${esc(criterion.artifact_id || "")}" placeholder="artifact identifier"></label>
    <button type="button" class="btn tiny danger" data-gate-criterion-remove aria-label="Remove Gate criterion">Remove</button>`;
    $("projectExecutionGateCriteria").append(row);
    row.querySelector("[data-gate-criterion-type]").value = criterion.type || "task_completion";
    this.#syncGateCriterionRow(row);
  }

  #selectOptions(options, selected, placeholder) {
    const rows = [...options];
    if (selected && !rows.some((option) => option.id === selected)) rows.unshift({ id: selected, title: selected });
    return `<option value="">${esc(placeholder)}</option>${rows.map((option) => `<option value="${esc(option.id)}"${option.id === selected ? " selected" : ""}>${esc(option.title)} · ${esc(option.id)}</option>`).join("")}`;
  }

  #syncGateCriterionRow(row) {
    if (!row) return;
    const type = row.querySelector("[data-gate-criterion-type]")?.value || "";
    const task = ["task_completion", "task_verification", "task_artifact"].includes(type);
    row.querySelector('[data-criterion-field="task"]').classList.toggle("hidden", !task);
    row.querySelector('[data-criterion-field="gate"]').classList.toggle("hidden", type !== "project_gate");
    row.querySelector('[data-criterion-field="outcome"]').classList.toggle("hidden", type !== "task_verification");
    row.querySelector('[data-criterion-field="artifact"]').classList.toggle("hidden", type !== "task_artifact");
  }

  #gateCriteriaDraft() {
    return [...$("projectExecutionGateCriteria").querySelectorAll("[data-gate-criterion-row]")].map((row) => {
      const type = row.querySelector("[data-gate-criterion-type]").value;
      const criterion = { type };
      if (["task_completion", "task_verification", "task_artifact"].includes(type)) {
        criterion.task_id = row.querySelector("[data-gate-criterion-task]").value.trim();
      }
      if (type === "project_gate") criterion.gate_id = row.querySelector("[data-gate-criterion-gate]").value.trim();
      if (type === "task_verification") criterion.outcome = row.querySelector("[data-gate-criterion-outcome]").value.trim() || "passed";
      if (type === "task_artifact") criterion.artifact_id = row.querySelector("[data-gate-criterion-artifact]").value.trim();
      return criterion;
    });
  }

  #openTaskEditor(taskId) {
    const task = this.snapshot.tasks.find((row) => row.task_id === taskId);
    const available = this.availableTasks.find((row) => row.id === taskId);
    $("projectExecutionTaskId").value = taskId;
    $("projectExecutionTaskDialogTitle").textContent = task ? `Edit ${task.title || taskId}` : `Assign ${available?.title || taskId}`;
    $("projectExecutionTaskPhase").innerHTML = this.snapshot.phases.map((phase) => `<option value="${esc(phase.id)}">${esc(phase.title)}</option>`).join("");
    $("projectExecutionTaskPhase").value = task?.phase || this.snapshot.phases[0]?.id || "";
    $("projectExecutionTaskRequired").checked = task?.required ?? true;
    const definitionTask = task || {};
    this.#renderReferencePicker("projectExecutionTaskPrerequisites", "task", definitionTask.requires?.tasks || [], { exclude: [taskId] });
    this.#renderReferencePicker("projectExecutionTaskGates", "gate", definitionTask.requires?.gates || []);
    if (!this.snapshot.phases.length) {
      this.toast("Create a Phase before assigning Tasks", true);
      return;
    }
    $("projectExecutionTaskDialog").showModal();
    queueMicrotask(() => $("projectExecutionTaskPhase")?.focus());
  }

  async #saveTaskMetadata() {
    const taskId = $("projectExecutionTaskId").value;
    const metadata = {
      phase: $("projectExecutionTaskPhase").value,
      required: $("projectExecutionTaskRequired").checked,
      requires: { tasks: this.#readReferencePicker("projectExecutionTaskPrerequisites"), gates: this.#readReferencePicker("projectExecutionTaskGates") },
    };
    try {
      await this.#mutate("/api/project-execution/task/assign", { project_id: this.projectId, task_id: taskId, metadata, expected_revision: this.snapshot.definition_revision });
      $("projectExecutionTaskDialog").close();
    } catch (_error) { /* toast handled by #mutate */ }
  }

  async #mutate(path, payload) {
    try {
      const result = await this.#post(path, payload);
      this.snapshot = result.project_execution || result;
      this.stale = false;
      this.loadError = "";
      this.render();
      this.onCanonicalChange();
      return result;
    } catch (error) {
      this.toast(error.message, true);
      if (this.#isRevisionConflict(error)) {
        this.stale = true;
        this.#showStateBanner(`Project Execution changed concurrently. Reconcile before retrying this action. ${error.message}`, "conflict", true);
      }
      throw error;
    }
  }

  #isRevisionConflict(error) {
    return /revision|conflict|refresh|changed concurrently|stale/i.test(String(error?.message || error || ""));
  }



  async #toggleCoordinationInspector(open) {
    const panel = $("projectExecutionCoordinationInspector");
    if (!panel) return;
    panel.classList.toggle("hidden", !open);
    $("projectExecutionCoordinationBtn")?.setAttribute("aria-expanded", String(open));
    if (!open) return;
    this.#renderCoordinationInspector();
    await this.#loadCoordinationHistory();
    this.#renderCoordinationInspector();
    queueMicrotask(() => panel.scrollIntoView({ block: "nearest", behavior: "smooth" }));
  }

  async #loadCoordinationHistory({ force = false } = {}) {
    if (!this.projectId || (this.coordinationHistoryLoaded && !force)) return;
    this.coordinationHistoryError = "";
    try {
      const result = await this.api(`/api/project-execution/coordination/history?project_id=${encodeURIComponent(this.projectId)}&limit=20`);
      this.coordinationHistory = Array.isArray(result?.entries) ? result.entries : [];
      this.coordinationHistoryLoaded = true;
    } catch (error) {
      this.coordinationHistoryError = String(error?.message || error);
    }
  }

  #renderCoordinationInspector() {
    const forensicTarget = $("projectExecutionCoordinationForensics");
    const historyTarget = $("projectExecutionCoordinationHistory");
    if (!forensicTarget || !historyTarget) return;
    const coordination = this.snapshot?.coordination || {};
    const forensic = coordination.forensics || {};
    if (!coordination.pending) {
      forensicTarget.innerHTML = '<div class="project-execution-clear"><span>✓</span><div><strong>No pending canonical coordination</strong><small>There is no split-write intent requiring forensic comparison.</small></div></div>';
    } else if (!forensic.available) {
      forensicTarget.innerHTML = `<div class="project-execution-forensic-domain"><header><h4>Three-way comparison unavailable</h4>${statusPill(coordination.phase || "pending")}</header><p class="muted">${esc(forensic.legacy_reason || "This pending intent predates semantic forensic capture.")} Revisions and digests remain available in the coordination banner.</p></div>`;
    } else {
      forensicTarget.innerHTML = `${forensic.truncated ? '<div class="project-execution-state-banner warning"><div><strong>Forensic subject list truncated</strong><span>Only the bounded semantic subjects recorded with this intent are shown.</span></div></div>' : ""}${this.#forensicDomain("Roadmap planning metadata", forensic.roadmap, coordination.roadmap)}${this.#forensicDomain("Project Execution canonical metadata", forensic.project_execution, coordination.project_execution)}`;
    }

    if (this.coordinationHistoryError) {
      historyTarget.innerHTML = `<div class="project-execution-list-empty">History unavailable: ${esc(this.coordinationHistoryError)}</div>`;
      return;
    }
    if (!this.coordinationHistoryLoaded) {
      historyTarget.innerHTML = '<div class="project-execution-list-empty">Loading recent coordination history…</div>';
      return;
    }
    const rows = [...this.coordinationHistory].reverse();
    historyTarget.innerHTML = rows.length ? rows.map((row) => `<div class="project-execution-coordination-history-row"><div><strong>${esc(titleCase(row.operation || "coordination"))} · ${esc(row.phase || "unknown")}</strong><span>${esc(row.timestamp || row.updated_at || "unknown time")}${row.reason ? ` · ${esc(row.reason)}` : ""}</span></div><code>Roadmap r${esc(row.roadmap_revision || 0)} · Execution r${esc(row.project_execution_revision || 0)}</code></div>`).join("") : '<div class="project-execution-list-empty">No completed or aborted coordination operations recorded yet.</div>';
  }

  #forensicDomain(title, domain = {}, status = {}) {
    const versions = [
      ["Before", domain.before || {}],
      ["Recorded desired", domain.desired || {}],
      ["Current", domain.current || {}],
    ];
    const subjects = domain.subjects || [];
    return `<section class="project-execution-forensic-domain"><header><h4>${esc(title)}</h4>${statusPill(status?.state || "unknown")}</header><div class="project-execution-forensic-meta">${versions.map(([label, row]) => `<div><span>${esc(label)}</span><strong>revision ${esc(row.revision || 0)}</strong><code title="${esc(row.digest || "")}">${esc(shortDigest(row.digest, 20))}</code></div>`).join("")}</div>${subjects.length ? subjects.map((subject) => this.#forensicSubject(subject)).join("") : '<div class="project-execution-list-empty">No semantic subject metadata was recorded for this domain.</div>'}</section>`;
  }

  #forensicSubject(subject = {}) {
    const identity = subject.identity || {};
    const identityText = identity.item_id || `${identity.kind || "asset"}:${identity.asset_id || "unknown"}`;
    return `<div class="project-execution-forensic-subject"><div class="identity"><small>Subject</small><strong>${esc(identityText)}</strong></div>${[["Before", subject.before], ["Recorded desired", subject.desired], ["Current", subject.current]].map(([label, value]) => `<div><small>${esc(label)}</small>${this.#forensicValue(value)}</div>`).join("")}</div>`;
  }

  #forensicValue(value) {
    if (!value) return '<span class="muted">not present</span>';
    const schedule = value.schedule || {};
    const fields = [];
    if (value.lane !== undefined) fields.push(`lane ${value.lane}`);
    if (value.order !== undefined) fields.push(`order ${value.order}`);
    if (value.title) fields.push(value.title);
    if (schedule.start || schedule.target) fields.push(`${schedule.start || "…"} → ${schedule.target || "…"}`);
    if (value.project_asset_id) fields.push(`asset ${value.project_asset_id}`);
    if (value.criterion_count !== undefined) fields.push(`${value.criterion_count} criteria`);
    if (value.task_count !== undefined) fields.push(`${value.task_count} Tasks`);
    if (value.requirement_count !== undefined) fields.push(`${value.requirement_count} requirements`);
    return fields.length ? fields.map((field) => `<span>${esc(field)}</span>`).join("<br>") : '<span class="muted">present</span>';
  }

  #showCoordinationBanner(coordination) {
    const target = $("projectExecutionStateBanner");
    if (!target) return;
    const road = coordination.roadmap || {};
    const execution = coordination.project_execution || {};
    const actions = coordination.safe_actions || [];
    const actionLabels = {
      retry_roll_forward: "Retry recorded roll-forward",
      accept_applied: "Accept recorded result",
      abort: "Abort untouched intent",
      finalize_terminal: "Finalize terminal cleanup",
    };
    target.className = `project-execution-state-banner ${coordination.divergent ? "conflict" : "warning"}`;
    target.innerHTML = `<div><strong>Canonical Roadmap coordination ${coordination.divergent ? "requires operator review" : "is pending"}</strong><span>${esc(coordination.message || "A durable cross-domain mutation is pending.")}<br>Operation <code>${esc(coordination.operation || "unknown")}</code> · <code>${esc(coordination.operation_id || "")}</code><br>Roadmap: ${esc(road.state || "unknown")} (expected ${esc(road.expected_revision || 0)}, current ${esc(road.current_revision || 0)}) · Project Execution: ${esc(execution.state || "unknown")} (expected ${esc(execution.expected_revision || 0)}, current ${esc(execution.current_revision || 0)})</span></div><div class="project-execution-coordination-actions"><button type="button" class="btn tiny" data-project-coordination-inspect>Inspect three-way state</button>${actions.map((action) => `<button type="button" class="btn tiny ${action === "abort" ? "danger" : ""}" data-project-coordination-action="${esc(action)}">${esc(actionLabels[action] || action)}</button>`).join("")}${!actions.length ? '<span class="muted">No force/overwrite action is safe. Resolve the divergent document as a normal revisioned edit, then refresh.</span>' : ""}</div>`;
  }

  async #resolveCoordination(action) {
    const labels = {
      retry_roll_forward: "replay the exact recorded mutation",
      accept_applied: "finalize the already-applied recorded result",
      abort: "abort this untouched coordination intent",
      finalize_terminal: "finalize this already-terminal intent without changing either domain",
    };
    if (!window.confirm(`Confirm: ${labels[action] || action}? No unrecorded content will be overwritten.`)) return;
    try {
      const result = await this.#post("/api/project-execution/coordination/resolve", {
        project_id: this.projectId,
        action,
        acknowledged: true,
      });
      this.snapshot = result.project_execution || result;
      this.stale = false;
      this.loadError = "";
      this.render();
      this.onCanonicalChange();
      this.toast("Coordination state resolved safely");
    } catch (error) {
      this.toast(error.message, true);
      await this.load({ force: true });
    }
  }

  #showStateBanner(message, kind = "info", showAction = false) {
    const target = $("projectExecutionStateBanner");
    if (!target) return;
    target.className = `project-execution-state-banner ${kind}`;
    target.innerHTML = `<div><strong>${kind === "held" ? "Project held" : kind === "conflict" ? "Concurrent change detected" : kind === "error" ? "Reconcile unavailable" : "Project Execution status"}</strong><span>${esc(message)}</span></div>${showAction ? '<button type="button" class="btn tiny" data-project-execution-reconcile>Reconcile now</button>' : ""}`;
  }

  #clearStateBanner() {
    const target = $("projectExecutionStateBanner");
    if (!target) return;
    target.className = "project-execution-state-banner hidden";
    target.innerHTML = "";
  }

  async #post(path, payload) {
    return this.api(path, { method: "POST", body: JSON.stringify(payload) });
  }

  #setLoading(loading) {
    this.busy = loading;
    $("projectExecutionLoading").classList.toggle("hidden", !loading);
    $("projectExecutionRefreshBtn").disabled = loading;
    $("projectExecutionWorkspace")?.setAttribute("aria-busy", String(loading));
  }

  #renderEmpty() {
    $("projectExecutionWorkspace")?.classList.add("hidden");
    $("projectExecutionWorkspaceEmpty")?.classList.remove("hidden");
  }
}
