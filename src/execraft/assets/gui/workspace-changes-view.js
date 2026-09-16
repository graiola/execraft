import {
  elementById as $,
  escapeHtml as esc,
  syncSelectOptions,
} from "./ui-utils.js";

function changeKey(repositoryId, path) {
  return repositoryId + "\u0000" + path;
}

function statusLabel(change) {
  const pair = (change.index_status || ".") + (change.worktree_status || ".");
  return change.index_status === "?" ? "??" : pair.replace(/\./g, " ");
}

export function changeKind(change) {
  const index = String(change.index_status || ".");
  const worktree = String(change.worktree_status || ".");
  if (index === "?" || index === "A" || worktree === "A") return "added";
  if (index === "D" || worktree === "D") return "deleted";
  return "modified";
}

/**
 * Own the Changes tab's client-side selection and mutation lifecycle.
 *
 * This controller is intentionally a projection/control layer over the existing
 * workspace APIs. It does not own Git semantics or task lifecycle state.
 */
export class WorkspaceChangesView {
  constructor({ api, toast, refreshDashboard, snapshot }) {
    this.api = api;
    this.toast = toast;
    this.refreshDashboard = refreshDashboard;
    this.snapshot = snapshot;
    this.workspace = null;
    this.repository = null;
    this.path = null;
    this.filter = "all";
    this.selected = new Set();
    this.busy = false;
    this.loading = false;
    this.renderKey = "";
    this.#wire();
  }

  reset() {
    this.workspace = null;
    this.repository = null;
    this.path = null;
    this.selected.clear();
    this.busy = false;
    this.loading = false;
    this.renderKey = "";
  }

  async load() {
    if (this.busy || this.loading) return;
    this.loading = true;
    try {
      this.workspace = await this.api("/api/workspace/changes");
      this.render(this.workspace);
    } catch (error) {
      const changed = this.workspace?.reason !== error.message;
      this.workspace = { available: false, reason: error.message, repositories: [] };
      this.render(this.workspace);
      if (changed) this.toast(error.message, true);
    } finally {
      this.loading = false;
    }
  }

  render(workspace) {
    const note = $("workspaceNote");
    const snapshot = this.snapshot() || {};
    const renderKey = JSON.stringify({
      workspace,
      repository: this.repository,
      selected: [...this.selected].sort(),
      check: snapshot.run_control?.human_action || {},
      orchestration: snapshot.orchestration?.state || "",
    });
    if (renderKey === this.renderKey) {
      this.#updateCommitControls();
      return;
    }
    if (!workspace.available) {
      note.className = "workspace-note bad";
      note.textContent = workspace.reason || "Task workspace is unavailable.";
      $("workspaceRepositories").innerHTML = '<div class="empty">No registered workspace.</div>';
      $("workspaceChanges").innerHTML = '<div class="empty">Workspace unavailable.</div>';
      this.renderKey = renderKey;
      this.#updateCommitControls();
      return;
    }
    const repositories = workspace.repositories || [];
    const validKeys = new Set();
    repositories.forEach((repo) =>
      (repo.changes || []).forEach((change) => validKeys.add(changeKey(repo.id, change.path))),
    );
    this.selected = new Set([...this.selected].filter((key) => validKeys.has(key)));
    const dirty = repositories.filter((repo) => repo.dirty);
    if (!this.repository || !repositories.some((repo) => repo.id === this.repository && repo.dirty))
      this.repository = (dirty[0] || repositories[0] || {}).id || null;
    note.className = "workspace-note" + (workspace.driver_active ? " warn" : "");
    const orchestration = snapshot.orchestration || {};
    const action = snapshot.run_control?.human_action || {};
    const check = action.package_id
      ? `\nCurrent check: ${action.package_id} · ${action.stage || orchestration.state || "unknown"}${action.recommended_decision ? " · " + action.recommended_decision : ""}`
      : `\nOrchestration state: ${orchestration.state || "unknown"}.`;
    note.textContent =
      (workspace.driver_active
        ? "The orchestrator is active. Inspection remains available, but deletion, AI generation, and commits are disabled until it stops."
        : workspace.reason || "Inspect exact task-workspace changes, then create operator-authorized commits. No action pushes or changes branches.") + check;
    $("workspaceSummary").textContent = `${workspace.changed_file_count || 0} changed files · ${workspace.dirty_repository_count || 0} dirty repositories`;
    $("workspaceRepositories").innerHTML = repositories.map((repo) => `<button class="repo-filter ${repo.id === this.repository ? "active" : ""}" data-repository="${esc(repo.id)}"><strong>${esc(repo.id)}</strong><span>${repo.dirty ? repo.change_count : 0}</span></button>`).join("") || '<div class="empty">No repositories registered.</div>';
    $("workspaceRepositories").querySelectorAll(".repo-filter").forEach((button) =>
      button.addEventListener("click", () => {
        this.repository = button.dataset.repository;
        this.path = null;
        this.render(this.workspace);
      }),
    );
    const repo = repositories.find((item) => item.id === this.repository);
    this.#renderChanges(repo);
    this.#renderAgentOptions(workspace.review_agents || []);
    this.renderKey = JSON.stringify({
      workspace,
      repository: this.repository,
      selected: [...this.selected].sort(),
      check: snapshot.run_control?.human_action || {},
      orchestration: snapshot.orchestration?.state || "",
    });
    this.#updateCommitControls();
  }

  #renderAgentOptions(agents) {
    const select = $("commitAgent");
    const previous = select.value;
    const available = agents.find((agent) => agent.available);
    const options = agents.length
      ? agents.map((agent) => ({
          value: agent.id,
          label: `${agent.id}${agent.model ? " · " + agent.model : ""}${agent.runtime_kind && agent.runtime_kind !== "native" ? " · " + agent.runtime_kind : ""}${agent.available ? "" : " · " + agent.health}`,
          disabled: !agent.available,
        }))
      : [{ value: "", label: "No review-capable agent" }];
    syncSelectOptions(select, options, {
      value: agents.some((agent) => agent.id === previous && agent.available)
        ? previous
        : available?.id || "",
    });
  }

  #renderChanges(repo) {
    document.querySelectorAll("[data-change-filter]").forEach((button) =>
      button.classList.toggle("active", button.dataset.changeFilter === this.filter),
    );
    if (!repo) {
      $("workspaceRepoTitle").textContent = "Changed files";
      $("workspaceRepoMeta").textContent = "";
      $("workspaceChanges").innerHTML = '<div class="empty">No repository selected.</div>';
      $("selectAllChangesBtn").disabled = true;
      return;
    }
    $("workspaceRepoTitle").textContent = repo.id;
    $("workspaceRepoMeta").textContent = `${repo.branch || "unknown branch"} · ${repo.change_count || 0} changes`;
    const allChanges = repo.changes || [];
    const changes = this.filter === "all" ? allChanges : allChanges.filter((change) => changeKind(change) === this.filter);
    $("selectAllChangesBtn").disabled = !changes.length || !repo.committable;
    $("workspaceChanges").innerHTML = changes.map((change) => {
      const key = changeKey(repo.id, change.path);
      const checked = this.selected.has(key);
      const active = this.path === change.path;
      const kind = changeKind(change);
      const label = kind === "added" ? "Added" : kind === "deleted" ? "Deleted" : "Modified";
      return `<div class="change-row ${active ? "active" : ""}"><input class="change-check" type="checkbox" data-repository="${esc(repo.id)}" data-path="${esc(change.path)}" ${checked ? "checked" : ""} ${!repo.committable || change.conflicted ? "disabled" : ""}><span class="change-kind ${kind} ${change.conflicted ? "conflicted" : ""}">${change.conflicted ? "Conflict" : label}</span><button class="change-path" data-repository="${esc(repo.id)}" data-path="${esc(change.path)}" title="${esc(change.path)}"><strong>${esc(change.path)}</strong>${change.original_path ? `<small>from ${esc(change.original_path)}</small>` : ""}</button><code>${esc(statusLabel(change))}</code></div>`;
    }).join("") || `<div class="empty">${allChanges.length ? "No changes match this filter." : "Repository is clean."}</div>`;
    $("workspaceChanges").querySelectorAll(".change-check").forEach((box) =>
      box.addEventListener("change", () => {
        const key = changeKey(box.dataset.repository, box.dataset.path);
        if (box.checked) this.selected.add(key);
        else this.selected.delete(key);
        this.#updateCommitControls();
      }),
    );
    $("workspaceChanges").querySelectorAll(".change-path").forEach((button) =>
      button.addEventListener("click", () => void this.#loadDiff(button.dataset.repository, button.dataset.path)),
    );
  }

  async #loadDiff(repositoryId, path) {
    this.repository = repositoryId;
    this.path = path;
    $("workspaceDiffTitle").textContent = path;
    $("workspaceDiffMeta").textContent = repositoryId;
    $("workspaceDiff").textContent = "Loading diff…";
    this.#renderChanges((this.workspace?.repositories || []).find((repo) => repo.id === repositoryId));
    try {
      const diff = await this.api(`/api/workspace/diff?repository=${encodeURIComponent(repositoryId)}&path=${encodeURIComponent(path)}`);
      $("workspaceDiff").textContent = diff.content;
      $("workspaceDiffMeta").textContent = `${repositoryId} · ${diff.size_bytes} bytes${diff.truncated ? " · truncated" : ""}${diff.binary ? " · binary" : ""}`;
    } catch (error) {
      $("workspaceDiff").textContent = error.message;
      this.toast(error.message, true);
    }
  }

  #selection() {
    const result = {};
    for (const key of this.selected) {
      const [repositoryId, path] = key.split("\u0000");
      if (!result[repositoryId]) result[repositoryId] = [];
      result[repositoryId].push(path);
    }
    for (const paths of Object.values(result)) paths.sort();
    return result;
  }

  #digests() {
    const result = {};
    for (const repo of this.workspace?.repositories || [])
      if (repo.status_digest) result[repo.id] = repo.status_digest;
    return result;
  }

  #entries() {
    const entries = [];
    for (const key of this.selected) {
      const [repositoryId, path] = key.split("\u0000");
      const repo = (this.workspace?.repositories || []).find((item) => item.id === repositoryId);
      const change = (repo?.changes || []).find((item) => item.path === path);
      if (repo && change) entries.push({ repo, change });
    }
    return entries;
  }

  #updateCommitControls() {
    const workspace = this.workspace || {};
    const count = this.selected.size;
    const subject = $("commitSubject").value.trim();
    const reviewed = $("commitReviewed").checked;
    const agent = $("commitAgent").value;
    const entries = this.#entries();
    const allDeletable = entries.length === count && count > 0 && entries.every((item) => item.change.deletable);
    $("deleteWorkspaceFilesBtn").disabled = this.busy || !workspace.delete_allowed || !allDeletable;
    $("deleteWorkspaceFilesBtn").textContent = allDeletable ? `Delete untracked (${count})` : "Delete untracked";
    $("generateCommitBtn").disabled = this.busy || !workspace.commit_allowed || count === 0 || !agent;
    $("commitChangesBtn").disabled = this.busy || !workspace.commit_allowed || count === 0 || !subject || !reviewed;
    $("commitChangesBtn").textContent = count ? `Commit selected (${count})` : "Commit selected";
  }

  async #deleteFiles() {
    if (this.busy || !this.selected.size) return;
    const count = this.selected.size;
    if (!confirm(`Permanently delete ${count} selected untracked file(s) from the task workspace?\n\nTracked files cannot be deleted by this action. This cannot be undone by Git.`)) return;
    this.busy = true;
    const button = $("deleteWorkspaceFilesBtn");
    button.disabled = true;
    button.textContent = "Deleting…";
    try {
      const result = await this.api("/api/workspace/delete", {
        method: "POST",
        body: JSON.stringify({ selections: this.#selection(), expected_digests: this.#digests() }),
      });
      $("commitMessage").className = "message ok";
      $("commitMessage").textContent = `Deleted ${result.deleted_count || 0} untracked workspace file(s). No commit was created.`;
      this.toast(`Deleted ${result.deleted_count || 0} untracked file(s)`);
      this.selected.clear();
      this.path = null;
      $("workspaceDiffTitle").textContent = "Review diff";
      $("workspaceDiffMeta").textContent = "Select a changed file.";
      $("workspaceDiff").textContent = "Select a changed file to inspect its staged and working-tree diff.";
      await this.load();
      await this.refreshDashboard();
    } catch (error) {
      $("commitMessage").className = "message bad";
      $("commitMessage").textContent = error.message;
      this.toast(error.message, true);
    } finally {
      this.busy = false;
      button.textContent = "Delete untracked";
      this.#updateCommitControls();
    }
  }

  async #generateCommitMessage() {
    if (this.busy) return;
    this.busy = true;
    const button = $("generateCommitBtn");
    button.disabled = true;
    button.textContent = "Generating…";
    $("commitMessage").className = "message";
    $("commitMessage").textContent = "Read-only agent is summarizing the selected diff. No files will be modified.";
    try {
      const result = await this.api("/api/workspace/ai-commit", {
        method: "POST",
        body: JSON.stringify({ selections: this.#selection(), expected_digests: this.#digests(), agent_id: $("commitAgent").value }),
      });
      $("commitSubject").value = result.subject || "";
      $("commitBody").value = result.body || "";
      $("commitMessage").className = "message ok";
      $("commitMessage").textContent = `Generated by agent ${result.provider_id}${result.model ? " · " + result.model : ""}. Review and edit before committing.`;
      this.toast("AI commit message generated");
    } catch (error) {
      $("commitMessage").className = "message bad";
      $("commitMessage").textContent = error.message;
      this.toast(error.message, true);
    } finally {
      this.busy = false;
      button.textContent = "Generate message";
      this.#updateCommitControls();
    }
  }

  async #commit() {
    if (this.busy || !this.selected.size) return;
    const count = this.selected.size;
    const subject = $("commitSubject").value.trim();
    if (!confirm(`Create Git commit(s) for ${count} selected path(s)?\n\n${subject}\n\nNothing will be pushed.`)) return;
    this.busy = true;
    const button = $("commitChangesBtn");
    button.disabled = true;
    button.textContent = "Committing…";
    try {
      const result = await this.api("/api/workspace/commit", {
        method: "POST",
        body: JSON.stringify({ selections: this.#selection(), expected_digests: this.#digests(), subject, body: $("commitBody").value, reviewed: $("commitReviewed").checked }),
      });
      const commits = (result.commits || []).map((item) => `${item.repository_id}@${item.short_commit}`).join(", ");
      $("commitMessage").className = "message ok";
      $("commitMessage").textContent = `Committed: ${commits}. Transaction ${result.transaction_id}. Nothing was pushed.`;
      this.toast(`Committed ${commits}`);
      this.selected.clear();
      this.path = null;
      $("commitReviewed").checked = false;
      $("commitSubject").value = "";
      $("commitBody").value = "";
      await this.load();
      await this.refreshDashboard();
    } catch (error) {
      $("commitMessage").className = "message bad";
      $("commitMessage").textContent = error.message;
      this.toast(error.message, true);
    } finally {
      this.busy = false;
      button.textContent = "Commit selected";
      this.#updateCommitControls();
    }
  }

  #wire() {
    $("workspaceRefreshBtn").addEventListener("click", () => void this.load());
    document.querySelectorAll("[data-change-filter]").forEach((button) =>
      button.addEventListener("click", () => {
        this.filter = button.dataset.changeFilter || "all";
        const repo = (this.workspace?.repositories || []).find((item) => item.id === this.repository);
        this.#renderChanges(repo);
      }),
    );
    $("selectAllChangesBtn").addEventListener("click", () => {
      const repo = (this.workspace?.repositories || []).find((item) => item.id === this.repository);
      if (!repo) return;
      const keys = (repo.changes || [])
        .filter((change) => !change.conflicted && (this.filter === "all" || changeKind(change) === this.filter))
        .map((change) => changeKey(repo.id, change.path));
      const all = keys.length && keys.every((key) => this.selected.has(key));
      keys.forEach((key) => all ? this.selected.delete(key) : this.selected.add(key));
      this.#renderChanges(repo);
      this.#updateCommitControls();
    });
    $("deleteWorkspaceFilesBtn").addEventListener("click", () => void this.#deleteFiles());
    $("generateCommitBtn").addEventListener("click", () => void this.#generateCommitMessage());
    $("commitChangesBtn").addEventListener("click", () => void this.#commit());
    $("commitSubject").addEventListener("input", () => this.#updateCommitControls());
    $("commitReviewed").addEventListener("change", () => this.#updateCommitControls());
    $("commitAgent").addEventListener("change", () => this.#updateCommitControls());
  }
}
