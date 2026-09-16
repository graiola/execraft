import { syncSelectOptions } from "./ui-utils.js";

/**
 * Persistent project/task context navigation.
 *
 * The control center has three intentionally separate scopes:
 *   1. the global project catalog,
 *   2. one project's roadmap-first management workspace,
 *   3. one task's execution dashboard.
 *
 * This controller owns only transitions between those scopes. Project and task
 * content remain in OnboardingView and UiShell respectively.
 */
export class ContextNavigation {
  constructor({ api, toast, onSnapshot }) {
    this.api = api;
    this.toast = toast;
    this.onSnapshot = onSnapshot;
    this.snapshot = null;
    this.projects = [];
    this.catalogPromise = null;
    this.catalogLoadedAt = 0;
    this.busy = false;
    this.#bind();
  }

  render(snapshot) {
    this.snapshot = snapshot;
    if (snapshot.mode === "home") {
      this.#setProjects(snapshot.projects || []);
    } else if (!this.projects.length || Date.now() - this.catalogLoadedAt > 30000) {
      void this.refreshCatalog();
    }
    this.#renderSelectors();
  }

  async refreshCatalog({ force = false } = {}) {
    if (this.catalogPromise) return this.catalogPromise;
    if (!force && this.projects.length && Date.now() - this.catalogLoadedAt < 30000) {
      return this.projects;
    }
    this.catalogPromise = (async () => {
      try {
        const payload = await this.api("/api/projects");
        this.#setProjects(payload.projects || []);
        this.#renderSelectors();
        return this.projects;
      } catch (error) {
        this.toast(`Project catalog unavailable: ${error.message}`, true);
        return this.projects;
      } finally {
        this.catalogPromise = null;
      }
    })();
    return this.catalogPromise;
  }

  async goToCatalog() {
    if (this.#isCatalog()) return;
    await this.#transition("/api/session/catalog", { acknowledged: true });
  }

  async goToProject(projectId = this.#currentProjectId()) {
    const id = String(projectId || "").trim();
    if (!id) return this.goToCatalog();
    if (this.snapshot?.mode === "task") {
      await this.#transition("/api/session/project", {
        project_id: id,
        acknowledged: true,
      });
      return;
    }
    if (this.snapshot?.focused_project_id === id) return;
    await this.#transition("/api/project/focus", { project_id: id });
  }

  async openTask(projectId, taskId) {
    const project = String(projectId || "").trim();
    const task = String(taskId || "").trim();
    if (!project || !task) return;
    const active = this.#activeTask();
    if (active?.project_id === project && active?.task_id === task) return;
    await this.#transition("/api/session/open", {
      project_id: project,
      task_id: task,
      acknowledged: true,
    });
  }

  #bind() {
    document.getElementById("projectsBreadcrumbBtn").addEventListener("click", () => {
      void this.goToCatalog();
    });
    document.getElementById("projectPicker").addEventListener("change", (event) => {
      const projectId = event.target.value;
      void (projectId ? this.goToProject(projectId) : this.goToCatalog());
    });
    document.getElementById("taskPicker").addEventListener("change", (event) => {
      const projectId =
        document.getElementById("projectPicker").value || this.#currentProjectId();
      const taskId = event.target.value;
      void (taskId ? this.openTask(projectId, taskId) : this.goToProject(projectId));
    });
  }

  async #transition(path, body) {
    if (this.busy) return;
    this.busy = true;
    this.#renderSelectors();
    try {
      const snapshot = await this.api(path, {
        method: "POST",
        body: JSON.stringify(body),
      });
      this.onSnapshot(snapshot);
    } catch (error) {
      this.toast(error.message, true);
      this.#renderSelectors();
    } finally {
      this.busy = false;
      this.#renderSelectors();
    }
  }

  #setProjects(projects) {
    this.projects = [...projects].sort((left, right) => left.id.localeCompare(right.id));
    this.catalogLoadedAt = Date.now();
  }

  #activeTask() {
    if (this.snapshot?.mode === "task") {
      return {
        project_id: this.snapshot.project?.id || "",
        task_id: this.snapshot.project?.task_id || "",
      };
    }
    return this.snapshot?.application?.active_task || null;
  }

  #currentProjectId() {
    const active = this.#activeTask();
    return (
      active?.project_id ||
      this.snapshot?.focused_project_id ||
      this.snapshot?.application?.focused_project_id ||
      ""
    );
  }

  #isCatalog() {
    return this.snapshot?.mode === "home" && !this.#currentProjectId();
  }

  #project(projectId = this.#currentProjectId()) {
    return this.projects.find((item) => item.id === projectId) || null;
  }

  #renderSelectors() {
    const projectPicker = document.getElementById("projectPicker");
    const taskPicker = document.getElementById("taskPicker");
    const projectSeparator = document.getElementById("projectContextSeparator");
    const taskSeparator = document.getElementById("taskContextSeparator");
    const currentProject = this.#currentProjectId();
    const active = this.#activeTask();
    const selectedProject = this.#project(currentProject);

    syncSelectOptions(
      projectPicker,
      [
        { value: "", label: "Select project…" },
        ...this.projects.map((project) => ({
          value: project.id,
          label: project.id,
        })),
      ],
      {
        value: currentProject,
        disabled: this.busy || !this.projects.length,
      },
    );

    const tasks = selectedProject?.tasks || [];
    syncSelectOptions(
      taskPicker,
      [
        { value: "", label: "Project roadmap" },
        ...tasks.map((task) => ({
          value: task.id,
          label: task.title || task.id,
        })),
      ],
      {
        value: active?.task_id || "",
        disabled: this.busy || !currentProject,
      },
    );

    const showProjects = Boolean(this.projects.length);
    const showTasks = Boolean(currentProject);
    projectPicker.classList.toggle("hidden", !showProjects);
    projectSeparator.classList.toggle("hidden", !showProjects);
    taskPicker.classList.toggle("hidden", !showTasks);
    taskSeparator.classList.toggle("hidden", !showTasks);
    document.getElementById("projectsBreadcrumbBtn").disabled = this.#isCatalog();
    document.getElementById("contextNavigator").dataset.mode =
      this.snapshot?.mode || "home";
  }
}
