import { escapeHtml, formatBytes, formatLocalTime } from "./ui-utils.js";

function entryCard(entry, { active = false } = {}) {
  const current = entry.status === "current";
  const detail =
    entry.kind === "project"
      ? `${entry.task_count || 0} active task${entry.task_count === 1 ? "" : "s"}`
      : entry.status;
  return `
    <article class="archive-card ${active ? "active-entry" : "archived-entry"}" data-kind="${escapeHtml(entry.kind)}" data-project="${escapeHtml(entry.project_id)}" data-id="${escapeHtml(entry.id)}">
      <button type="button" class="archive-card-main" data-archive-action="${active ? "noop" : "inspect"}">
        <span class="archive-kind">${escapeHtml(entry.kind)}</span>
        <strong>${escapeHtml(entry.id)}</strong>
        <span>${escapeHtml(entry.title || entry.id)}</span>
        <small>${escapeHtml(detail)}${entry.archived_at ? ` · ${escapeHtml(formatLocalTime(entry.archived_at))}` : ""}</small>
      </button>
      <div class="archive-card-actions">
        ${
          active
            ? `<button type="button" class="btn small danger" data-archive-action="archive" ${current ? "disabled" : ""}>Archive</button><button type="button" class="btn small danger" data-archive-action="delete" ${current ? "disabled" : ""}>Delete permanently…</button>`
            : '<button type="button" class="btn small" data-archive-action="inspect">Inspect</button><button type="button" class="btn small primary" data-archive-action="reactivate">Reactivate</button><button type="button" class="btn small danger" data-archive-action="delete">Delete permanently…</button>'
        }
      </div>
    </article>`;
}

export class ArchiveView {
  constructor({ api, toast, onCatalogChanged }) {
    this.api = api;
    this.toast = toast;
    this.onCatalogChanged = onCatalogChanged || (() => {});
    this.$ = (id) => document.getElementById(id);
    this.catalog = null;
    this.selected = null;
    this.busy = false;
    this.$("archiveRefreshBtn").addEventListener("click", () => this.load());
    this.$("reactivateArchiveBtn").addEventListener("click", () =>
      this.reactivateSelected(),
    );
    for (const id of [
      "archivedTasks",
      "archivedProjects",
      "activeTasks",
      "activeProjects",
    ]) {
      this.$(id).addEventListener("click", (event) =>
        this.#handleListAction(event),
      );
    }
  }

  async load() {
    try {
      this.catalog = await this.api("/api/archive");
      this.renderCatalog();
      if (this.selected) await this.inspect(this.selected);
    } catch (error) {
      this.toast(error.message, true);
      this.$("archiveSummary").textContent =
        `Archive unavailable: ${error.message}`;
    }
  }

  renderCatalog() {
    const catalog = this.catalog || {};
    const archivedTasks = catalog.archived_tasks || [];
    const archivedProjects = catalog.archived_projects || [];
    this.$("archiveSummary").textContent =
      `${archivedTasks.length} archived tasks · ${archivedProjects.length} archived projects · ${catalog.archive_root || ""}`;
    this.renderList("archivedTasks", archivedTasks, { active: false });
    this.renderList("archivedProjects", archivedProjects, { active: false });
    this.renderList("activeTasks", catalog.active_tasks || [], {
      active: true,
    });
    this.renderList("activeProjects", catalog.active_projects || [], {
      active: true,
    });
  }

  renderList(id, entries, { active }) {
    const node = this.$(id);
    node.innerHTML = entries.length
      ? entries.map((entry) => entryCard(entry, { active })).join("")
      : `<div class="empty">No ${active ? "active" : "archived"} entries.</div>`;
  }

  async inspect(entry) {
    this.selected = entry;
    this.$("archiveInspectorTitle").textContent = `${entry.kind}: ${entry.id}`;
    this.$("archiveInspectorMeta").textContent =
      "Loading metadata and integrity report…";
    this.$("archiveInspector").className = "archive-inspector-body";
    this.$("archiveInspector").textContent = "Loading…";
    this.$("reactivateArchiveBtn").disabled = true;
    try {
      const query = new URLSearchParams({
        kind: entry.kind,
        project_id: entry.project_id,
        id: entry.id,
      });
      const result = await this.api(`/api/archive/item?${query}`);
      this.selected = { ...entry, result };
      const verification = result.verification || {};
      const state = result.state || {};
      this.$("archiveInspectorMeta").textContent =
        `${verification.ok ? "Integrity verified" : "Integrity failure"} · ${verification.file_count || 0} files · ${formatBytes(verification.total_bytes || 0)}`;
      const problems = [
        ...(verification.missing || []).map((item) => `Missing: ${item}`),
        ...(verification.changed || []).map((item) => `Changed: ${item}`),
        ...(verification.unexpected || []).map((item) => `Unexpected: ${item}`),
      ];
      this.$("archiveInspector").innerHTML = `
        <div class="archive-facts">
          <div><span>Archived</span><strong>${escapeHtml(formatLocalTime(result.entry?.archived_at))}</strong></div>
          <div><span>Reason</span><strong>${escapeHtml(result.entry?.reason || "Not specified")}</strong></div>
          <div><span>Runtime state</span><strong>${escapeHtml(state.state || "No retained state")}${state.total_packages != null ? ` · ${state.completed_packages}/${state.total_packages}` : ""}</strong></div>
          <div><span>Path</span><strong>${escapeHtml(result.path || "")}</strong></div>
        </div>
        ${problems.length ? `<div class="archive-integrity bad"><strong>Integrity verification failed</strong>${problems.map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</div>` : '<div class="archive-integrity ok">All archived files match the recorded SHA-256 inventory.</div>'}
        <div class="archive-previews">${
          (result.previews || [])
            .map(
              (preview) => `
          <details ${preview.name === "TASK.yaml" || preview.name === "project.yaml" ? "open" : ""}>
            <summary>${escapeHtml(preview.name)}${preview.truncated ? " · preview truncated" : ""}</summary>
            <pre>${escapeHtml(preview.content)}</pre>
          </details>`,
            )
            .join("") || '<div class="empty">No previewable files.</div>'
        }</div>`;
      this.$("reactivateArchiveBtn").disabled = !verification.ok;
    } catch (error) {
      this.$("archiveInspectorMeta").textContent = "Inspection failed";
      this.$("archiveInspector").className = "archive-inspector-body empty";
      this.$("archiveInspector").textContent = error.message;
      this.toast(error.message, true);
    }
  }

  async archive(entry) {
    if (this.busy) return;
    const reason =
      window.prompt(
        `Why archive ${entry.kind} ${entry.id}?`,
        "Inactive / historical work",
      ) ?? null;
    if (reason === null) return;
    if (
      !window.confirm(
        `Move ${entry.kind} ${entry.id} out of the active catalog?\n\nThe dossier remains inspectable and can be reactivated.`,
      )
    )
      return;
    this.busy = true;
    try {
      await this.api("/api/archive/archive", {
        method: "POST",
        body: JSON.stringify({ ...entry, reason }),
      });
      this.toast(`Archived ${entry.kind} ${entry.id}`);
      this.selected = entry;
      await this.load();
      await this.onCatalogChanged();
    } catch (error) {
      this.toast(error.message, true);
    } finally {
      this.busy = false;
    }
  }

  async deletePermanently(entry) {
    if (this.busy) return;
    const expected = entry.kind === "task" ? entry.id : entry.project_id;
    const confirmation = window.prompt(
      `Permanently delete ${entry.kind} ${entry.id}?\n\nThis removes Execraft lifecycle state and cannot be undone. Source repositories are preserved.\n\nType ${expected} to confirm:`,
      "",
    );
    if (confirmation === null) return;
    if (confirmation !== expected) {
      this.toast("Permanent deletion cancelled: confirmation did not match the exact ID.", true);
      return;
    }
    const deleteBranches = window.confirm(
      "Also delete local task branches that Execraft can prove it created?\n\nChoose Cancel to preserve all Git branches.",
    );
    this.busy = true;
    try {
      await this.api("/api/archive/delete", {
        method: "POST",
        body: JSON.stringify({
          ...entry,
          confirmation,
          delete_branches: deleteBranches,
        }),
      });
      this.toast(`Permanently deleted ${entry.kind} ${entry.id}`);
      this.selected = null;
      this.$("archiveInspectorTitle").textContent = "Archive inspector";
      this.$("archiveInspectorMeta").textContent = "Select an archived item.";
      this.$("archiveInspector").className = "archive-inspector-body empty";
      this.$("archiveInspector").textContent =
        "Select an archived task or project to inspect its metadata, integrity report, and key files.";
      this.$("reactivateArchiveBtn").disabled = true;
      await this.load();
      await this.onCatalogChanged();
    } catch (error) {
      this.toast(error.message, true);
    } finally {
      this.busy = false;
    }
  }

  async reactivateSelected() {
    if (!this.selected) return;
    await this.reactivate(this.selected);
  }

  async reactivate(entry) {
    if (this.busy) return;
    if (
      !window.confirm(
        `Reactivate ${entry.kind} ${entry.id}?\n\nIt will return to the active project/task catalog.`,
      )
    )
      return;
    this.busy = true;
    try {
      await this.api("/api/archive/reactivate", {
        method: "POST",
        body: JSON.stringify(entry),
      });
      this.toast(`Reactivated ${entry.kind} ${entry.id}`);
      this.selected = null;
      this.$("archiveInspectorTitle").textContent = "Archive inspector";
      this.$("archiveInspectorMeta").textContent = "Select an archived item.";
      this.$("archiveInspector").className = "archive-inspector-body empty";
      this.$("archiveInspector").textContent =
        "Select an archived task or project to inspect its metadata, integrity report, and key files.";
      this.$("reactivateArchiveBtn").disabled = true;
      await this.load();
      await this.onCatalogChanged();
    } catch (error) {
      this.toast(error.message, true);
    } finally {
      this.busy = false;
    }
  }

  #handleListAction(event) {
    const button = event.target.closest("[data-archive-action]");
    const card = event.target.closest(".archive-card");
    if (
      !button ||
      !card ||
      button.disabled ||
      button.dataset.archiveAction === "noop"
    )
      return;
    const entry = {
      kind: card.dataset.kind,
      project_id: card.dataset.project,
      id: card.dataset.id,
    };
    const action = button.dataset.archiveAction;
    if (action === "inspect") this.inspect(entry);
    if (action === "archive") this.archive(entry);
    if (action === "reactivate") this.reactivate(entry);
    if (action === "delete") this.deletePermanently(entry);
  }
}
