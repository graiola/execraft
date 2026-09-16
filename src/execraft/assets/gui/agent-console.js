import { escapeHtml, syncSelectOptions } from "./ui-utils.js";

const RUNNING_POLL_MS = 250;
const ACTIVITY_POLL_MS = 250;
const IDLE_POLL_MS = 1200;
const HIDDEN_POLL_MS = 1800;
const LIVE_LONG_POLL_MS = 15000;
const SESSION_REFRESH_MS = 5000;
const RAW_INPUT_BATCH_MS = 12;
const RESIZE_DEBOUNCE_MS = 120;
const MAX_ACTIVITY_EVENTS = 2000;
const MAX_INTERACTION_EVENTS = 1200;
function terminalSequence(event) {
  if (event.metaKey || event.isComposing) return "";
  if (event.ctrlKey && !event.altKey) {
    if (event.key === " ") return "\x00";
    if (event.key === "?") return "\x7f";
    if (event.key.length === 1) {
      const code = event.key.toUpperCase().charCodeAt(0);
      if (code >= 64 && code <= 95) return String.fromCharCode(code - 64);
    }
  }
  const special = {
    Enter: "\r",
    Backspace: "\x7f",
    Tab: event.shiftKey ? "\x1b[Z" : "\t",
    Escape: "\x1b",
    ArrowUp: "\x1b[A",
    ArrowDown: "\x1b[B",
    ArrowRight: "\x1b[C",
    ArrowLeft: "\x1b[D",
    Home: "\x1b[H",
    End: "\x1b[F",
    Insert: "\x1b[2~",
    Delete: "\x1b[3~",
    PageUp: "\x1b[5~",
    PageDown: "\x1b[6~",
    F1: "\x1bOP",
    F2: "\x1bOQ",
    F3: "\x1bOR",
    F4: "\x1bOS",
    F5: "\x1b[15~",
    F6: "\x1b[17~",
    F7: "\x1b[18~",
    F8: "\x1b[19~",
    F9: "\x1b[20~",
    F10: "\x1b[21~",
    F11: "\x1b[23~",
    F12: "\x1b[24~",
  };
  let value = special[event.key] ?? (event.key.length === 1 ? event.key : "");
  if (value && event.altKey) value = `\x1b${value}`;
  return value;
}

function terminalSequenceForKey(key) {
  return terminalSequence({
    key,
    ctrlKey: false,
    altKey: false,
    metaKey: false,
    shiftKey: false,
    isComposing: false,
  });
}

function composerPayload(value) {
  const normalized = String(value || "").replace(/\r?\n/g, "\r");
  return normalized ? `${normalized}\r` : "";
}

function terminalText(screen, running) {
  if (!screen?.content) return "";
  if (!running || !screen.cursor_visible) return screen.content;
  const lines = screen.content.split("\n");
  const row = Math.max(0, Number(screen.cursor_row) || 0);
  const column = Math.max(0, Number(screen.cursor_column) || 0);
  while (lines.length <= row) lines.push("");
  const line = lines[row].padEnd(column, " ");
  lines[row] = `${line.slice(0, column)}▌${line.slice(column)}`;
  return lines.join("\n");
}

function eventNode(event) {
  const labels = {
    stderr: "ERR",
    system: "SYS",
    terminal: "TTY",
    input: "IN",
    stdout: "OUT",
  };
  const kind = Object.hasOwn(labels, event.stream) ? event.stream : "stdout";
  const node = document.createElement("span");
  node.className = `console-event ${kind}`;
  node.title = String(event.at || "");
  const label = document.createElement("span");
  label.className = "stream-label";
  label.textContent = labels[kind];
  node.append(label, document.createTextNode(String(event.text || "")));
  return node;
}

function semanticMode(metadata) {
  return metadata?.interaction?.mode === "conversation";
}

function telemetryActivity(telemetry) {
  if (telemetry?.output_active) return "output";
  if (telemetry?.input_active) return "input";
  if (telemetry?.cpu_active) return "compute";
  if (
    telemetry?.disk_io_active ||
    Number(telemetry?.read_bytes_delta || 0) > 0 ||
    Number(telemetry?.write_bytes_delta || 0) > 0
  )
    return "disk";
  if (telemetry?.stdio_active || telemetry?.io_active) return "stdio";
  return "idle";
}

function compactText(value, limit = 120) {
  const text = String(value || "")
    .replace(/\s+/g, " ")
    .trim();
  if (text.length <= limit) return text;
  return `${text.slice(0, Math.max(0, limit - 1))}…`;
}

function controlMode(metadata) {
  return String(metadata?.interaction?.control_mode || "none");
}

function controlDescriptor(metadata) {
  const descriptors = {
    live_steering: {
      badge: "live steering",
      placeholder:
        "Steer the running agent… Enter sends, Shift+Enter adds a line",
      send: "Steer agent",
      interrupt: "Interrupt",
      interruptTitle: "Interrupt the current provider turn",
      hint: "Messages and interrupts affect the active provider turn and are audit-logged.",
    },
    queued_guidance: {
      badge: "queued guidance",
      placeholder:
        "Queue guidance for the agent… Enter sends, Shift+Enter adds a line",
      send: "Queue guidance",
      interrupt: "Queue reassess",
      interruptTitle:
        "Queue a request to stop the current approach and reassess",
      hint: "Guidance is queued for the provider; it may not interrupt the current operation immediately.",
    },
    observation: {
      badge: "observation stream",
      placeholder: "This provider is observation-only for the current session.",
      send: "Send",
      interrupt: "Interrupt",
      interruptTitle: "Agent-native interruption is unavailable",
      hint: "This transport exposes live activity but no provider-native steering.",
    },
    none: {
      badge: "activity stream",
      placeholder: "Message the terminal… Enter sends, Shift+Enter adds a line",
      send: "Send",
      interrupt: "Interrupt",
      interruptTitle: "Interrupt the terminal process",
      hint: "Controls are bounded and audit-logged.",
    },
  };
  return descriptors[controlMode(metadata)] || descriptors.none;
}

function interactionTime(event) {
  return String(event?.at || "")
    .replace("T", " ")
    .replace("+00:00", "Z");
}

function textElement(tag, className, text) {
  const node = document.createElement(tag);
  node.className = className;
  node.textContent = String(text || "");
  return node;
}

export class AgentConsoleWorkbench {
  constructor({ api, toast, localTime }) {
    this.api = api;
    this.toast = toast;
    this.localTime = localTime;
    this.$ = (id) => document.getElementById(id);
    this.state = this.#initialState();
    this.maximized = false;
    this.#bind();
    this.#applyWorkbenchState();
  }

  #initialState() {
    return {
      open: false,
      agentId: "",
      sessionId: "",
      offset: 0,
      events: [],
      interactionOffset: 0,
      interactions: [],
      version: -1,
      loading: false,
      requestController: null,
      timer: null,
      artifactPath: "",
      metadata: {},
      manual: {},
      screen: {},
      screenToken: "",
      sessionsUpdatedAt: 0,
      attached: false,
      acknowledged: false,
      directKeyboard: false,
      terminalFocused: false,
      rawBuffer: "",
      rawTimer: null,
      resizeTimer: null,
      actionQueue: [],
      actionDraining: false,
      lastRows: 0,
      lastColumns: 0,
      attachAfterStart: false,
      activeView: "conversation",
      preferredPackageId: "",
      preferredStage: "",
      loadError: "",
    };
  }

  open(agentId, { packageId = "", stage = "" } = {}) {
    this.#stopTimers();
    this.state = {
      ...this.#initialState(),
      open: true,
      agentId,
      preferredPackageId: String(packageId || ""),
      preferredStage: String(stage || ""),
    };
    this.$("directAgentKeyboard").checked = false;
    this.$("agentConsoleModal").classList.remove("hidden");
    this.$("agentConsoleModal").setAttribute("aria-hidden", "false");
    this.maximized = false;
    this.#applyWorkbenchState();
    this.$("agentConsoleTitle").textContent = agentId;
    this.$("agentConsoleSubtitle").textContent = "Live agent session";
    this.$("agentTerminalScreen").textContent = "Loading agent sessions…";
    this.#resetActivity("Loading captured activity…");
    this.#resetConversation("Loading live agent activity…");
    this.#resetChanges();
    this.$("agentArtifactOutput").textContent =
      "Select Result to load the durable artifact.";
    this.#setView("conversation");
    this.load();
  }

  close() {
    this.state.requestController?.abort();
    this.#flushDirectInput().catch(() => {
      /* toast already shown */
    });
    Object.assign(this.state, {
      open: false,
      attached: false,
      acknowledged: false,
      directKeyboard: false,
      terminalFocused: false,
      rawBuffer: "",
    });
    this.#stopTimers();
    this.$("agentTerminalKeySink").blur();
    this.$("agentConsoleModal").classList.add("hidden");
    this.$("agentConsoleModal").setAttribute("aria-hidden", "true");
    document.body.classList.remove("workbench-open", "workbench-maximized");
  }

  isOpen() {
    return this.state.open;
  }

  async load() {
    const state = this.state;
    if (!state.open || state.loading) {
      this.#schedule();
      return;
    }
    state.loading = true;
    const controller = new AbortController();
    state.requestController = controller;
    const now = Date.now();
    const includeSessions =
      !state.sessionId || now - state.sessionsUpdatedAt >= SESSION_REFRESH_MS;
    const live =
      state.metadata?.status === "running" && semanticMode(state.metadata);
    const includeInteractions = ["conversation", "changes"].includes(
      state.activeView,
    );
    try {
      const query = new URLSearchParams({
        agent_id: state.agentId,
        session_id: state.sessionId,
        offset: String(state.offset),
        include_sessions: includeSessions ? "1" : "0",
        include_events: state.activeView === "activity" ? "1" : "0",
        interaction_offset: String(state.interactionOffset),
        include_interactions: includeInteractions ? "1" : "0",
        since_version: String(state.version),
        wait_ms: live && !document.hidden ? String(LIVE_LONG_POLL_MS) : "0",
        terminal_screen_token: state.screenToken,
        preferred_package_id: state.preferredPackageId,
        preferred_stage: state.preferredStage,
      });
      const data = await this.api(`/api/agent/console?${query}`, {
        signal: controller.signal,
      });
      if (!state.open || state !== this.state) return;
      state.loadError = "";
      this.#render(data);
    } catch (error) {
      if (error.name !== "AbortError") {
        state.loadError = String(error.message || "Console polling failed");
        this.$("agentTerminalScreen").textContent = error.message;
        this.#renderProgress();
      }
    } finally {
      if (state.requestController === controller)
        state.requestController = null;
      state.loading = false;
      if (state.open && state === this.state) this.#schedule();
    }
  }

  #render(data) {
    const state = this.state;
    const previousStatus = state.metadata?.status || "";
    const metadata = data.metadata || {};
    const telemetry = metadata.telemetry || {};
    const progress = metadata.interaction?.progress || {};
    const selectedSessionId = data.selected_session_id || "";
    const sessionChanged = selectedSessionId !== state.sessionId;

    if (sessionChanged) {
      state.offset = 0;
      state.events = [];
      state.interactionOffset = 0;
      state.interactions = [];
      state.version = -1;
      state.screen = {};
      state.screenToken = "";
      this.#resetActivity();
      this.#resetConversation();
      this.#resetChanges();
    }
    state.sessionId = selectedSessionId;
    state.metadata = metadata;
    state.manual = data.manual_console || {};
    state.artifactPath = metadata.artifact?.path || "";
    state.offset = data.next_offset || 0;
    state.interactionOffset = data.interaction_next_offset || 0;
    state.version = Number.isFinite(Number(data.version))
      ? Number(data.version)
      : state.version;

    if (data.sessions_included) {
      state.sessionsUpdatedAt = Date.now();
      this.#renderSessions(data.sessions || [], state.sessionId);
    }

    const screenChanged = Boolean(
      data.reset ||
        sessionChanged ||
        (data.terminal_screen && Object.keys(data.terminal_screen).length),
    );
    if (data.terminal_screen_token !== undefined) {
      state.screenToken = data.terminal_screen_token || "";
    }
    if (data.terminal_screen && Object.keys(data.terminal_screen).length) {
      state.screen = data.terminal_screen;
    }

    const newEvents = data.events || [];
    this.#appendActivity(newEvents, {
      reset: Boolean(data.reset || sessionChanged),
    });
    this.#appendInteractions(data.interactions || [], {
      reset: Boolean(data.interaction_reset || sessionChanged),
    });

    this.$("agentConsoleSubtitle").textContent = metadata.package_id
      ? `${metadata.origin === "manual" ? "Standalone workspace session" : metadata.package_id} · ${metadata.stage} · ${metadata.model || metadata.adapter || state.agentId}`
      : "No captured execution";

    const badges = [];
    if (metadata.status) {
      const className = ["running", "completed"].includes(metadata.status)
        ? "ok"
        : "warn";
      badges.push(
        `<span class="pill ${className}">${escapeHtml(metadata.status)}</span>`,
      );
    }
    if (telemetry.pid)
      badges.push(`<span class="pill">PID ${escapeHtml(telemetry.pid)}</span>`);
    if (telemetry.process_state) {
      const processCount =
        telemetry.process_count === undefined
          ? ""
          : ` · ${telemetry.process_count} proc`;
      badges.push(
        `<span class="pill">${escapeHtml(telemetry.process_state)}${escapeHtml(processCount)}</span>`,
      );
    }
    if (metadata.status === "running") {
      const activity =
        compactText(progress.current_activity, 52) ||
        telemetryActivity(telemetry);
      const activityClass =
        progress.state === "possibly_stalled" || progress.state === "attention"
          ? "warn"
          : "";
      badges.push(
        `<span class="pill ${activityClass}">${escapeHtml(activity)}</span>`,
      );
    }
    if (telemetry.last_output_age_seconds !== undefined) {
      badges.push(
        `<span class="pill">output ${Math.round(telemetry.last_output_age_seconds)}s ago</span>`,
      );
    }
    if (metadata.truncated)
      badges.push('<span class="pill warn">capture truncated</span>');
    if (semanticMode(metadata)) {
      const descriptor = controlDescriptor(metadata);
      const streamClass = controlMode(metadata) === "observation" ? "" : "ok";
      badges.push(
        `<span class="pill ${streamClass}">${escapeHtml(descriptor.badge)}</span>`,
      );
      if (metadata.interaction?.transport) {
        badges.push(
          `<span class="pill">${escapeHtml(metadata.interaction.transport)}</span>`,
        );
      }
    }
    this.$("agentConsoleStatus").innerHTML = badges.join("");

    const available = this.#terminalAvailable();
    const steering = this.#steeringAvailable();
    const manualRunning = available && metadata.origin === "manual";
    if ((manualRunning || state.attachAfterStart) && !state.attached) {
      state.attached = true;
      state.acknowledged = true;
      state.directKeyboard = true;
      state.attachAfterStart = false;
      this.$("directAgentKeyboard").checked = true;
      queueMicrotask(() => {
        this.#resize({ immediate: true });
        this.#focusTerminal();
      });
    }
    if (!available) {
      state.attached = steering;
      state.acknowledged = steering;
      state.directKeyboard = false;
      state.terminalFocused = false;
      this.$("directAgentKeyboard").checked = false;
      this.$("agentTerminalKeySink").blur();
    }

    this.#syncViewAvailability();
    if (semanticMode(metadata) && sessionChanged) this.#setView("conversation");
    if (metadata.origin === "manual" && sessionChanged)
      this.#setView("terminal");

    if (screenChanged || previousStatus !== metadata.status)
      this.#renderTerminal();
    else this.#renderTerminalInteractionState();
    this.#renderProgress();
    this.#renderControls();
    this.#renderMeta();
  }

  #syncViewAvailability() {
    const metadata = this.state.metadata || {};
    const hasSession = Boolean(metadata.session_id);
    const availability = {
      conversation: semanticMode(metadata),
      changes: semanticMode(metadata),
      terminal: Boolean(metadata.terminal?.enabled),
      activity: true,
      result: hasSession,
    };
    document.querySelectorAll("[data-console-view]").forEach((button) => {
      const available = Boolean(availability[button.dataset.consoleView]);
      button.classList.toggle("hidden", !available);
      button.disabled = !available;
    });
    if (!availability.conversation) this.$("agentAdvancedControls").open = true;
    if (availability[this.state.activeView]) return;
    const fallback = availability.conversation
      ? "conversation"
      : availability.terminal
        ? "terminal"
        : "activity";
    this.#setView(fallback);
  }

  #renderSessions(sessions, selected) {
    const options = sessions.length
      ? sessions.map((item) => {
          const origin =
            item.origin === "manual" ? "standalone" : item.package_id;
          return {
            value: item.session_id,
            label: `${origin} · ${item.stage} · ${item.status} · ${this.localTime(item.started_at)}`,
          };
        })
      : [{ value: "", label: "No sessions yet" }];
    syncSelectOptions(this.$("agentConsoleSession"), options, {
      value: selected,
    });
  }

  #renderTerminal() {
    const running = this.state.metadata?.status === "running";
    const text = terminalText(this.state.screen, running);
    this.$("agentTerminalScreen").textContent =
      text ||
      (this.state.metadata?.session_id
        ? "This session has no terminal surface. Open Activity for stdout/stderr."
        : "Start a new terminal to interact with this agent in the task workspace.");
    this.#renderTerminalInteractionState();
  }

  #renderTerminalInteractionState() {
    const ready = this.state.attached && this.state.directKeyboard;
    const terminal = this.$("agentTerminalScreen");
    const surface = this.$("agentTerminalSurface");
    terminal.classList.toggle("keyboard-attached", ready);
    surface.classList.toggle("keyboard-ready", ready);
    surface.classList.toggle(
      "keyboard-focused",
      ready && this.state.terminalFocused,
    );
    terminal.setAttribute(
      "aria-label",
      ready
        ? "Interactive agent terminal; click and type"
        : "Agent terminal output",
    );
    this.$("agentTerminalFocusHint").textContent = this.state.terminalFocused
      ? "Keyboard input active"
      : "Click the terminal to type";
    this.$("agentTerminalInputState").textContent = !this.state.attached
      ? "Input disabled."
      : !this.state.directKeyboard
        ? "Composer input enabled; terminal keys disabled."
        : this.state.terminalFocused
          ? "Terminal keyboard active. Type, paste, or use the navigation keys."
          : "Click the terminal or press Focus terminal to activate keyboard input.";
  }

  #focusTerminal() {
    if (
      !this.state.attached ||
      !this.state.directKeyboard ||
      this.state.activeView !== "terminal"
    )
      return;
    const sink = this.$("agentTerminalKeySink");
    sink.value = "";
    sink.focus({ preventScroll: true });
    this.state.terminalFocused = document.activeElement === sink;
    this.#renderTerminalInteractionState();
  }

  #resetActivity(message = "No captured stdout, stderr or operator events.") {
    const output = this.$("agentActivityOutput");
    output.replaceChildren();
    if (message) {
      const empty = document.createElement("span");
      empty.className = "console-empty";
      empty.textContent = message;
      output.append(empty);
    }
  }

  #appendActivity(events, { reset = false } = {}) {
    const output = this.$("agentActivityOutput");
    const follow =
      reset || output.scrollHeight - output.scrollTop - output.clientHeight < 48;
    if (reset) {
      this.state.events = [];
      output.replaceChildren();
    }
    if (!events.length) {
      if (!this.state.events.length && !output.childNodes.length)
        this.#resetActivity();
      return;
    }
    if (output.querySelector(".console-empty")) output.replaceChildren();
    const fragment = document.createDocumentFragment();
    for (const event of events) fragment.append(eventNode(event));
    output.append(fragment);
    this.state.events.push(...events);

    const overflow = this.state.events.length - MAX_ACTIVITY_EVENTS;
    if (overflow > 0) {
      this.state.events.splice(0, overflow);
      for (let index = 0; index < overflow && output.firstChild; index += 1) {
        output.firstChild.remove();
      }
    }
    if (this.state.activeView === "activity" && follow)
      output.scrollTop = output.scrollHeight;
  }

  #resetConversation(
    message = "No semantic agent events have been captured for this session.",
  ) {
    const output = this.$("agentConversationOutput");
    output.replaceChildren();
    if (message) output.append(textElement("div", "console-empty", message));
  }

  #renderProgress() {
    const metadata = this.state.metadata || {};
    const progress = metadata.interaction?.progress || {};
    const sessionAvailable = Boolean(metadata.session_id);
    const state = String(
      progress.state ||
        (metadata.status === "running"
          ? "starting"
          : metadata.status || "waiting"),
    );
    const labels = {
      starting: "Starting",
      working: "Working",
      possibly_stalled: "Possibly stalled",
      attention: "Needs attention",
      completed: "Completed",
      failed: "Failed",
      waiting: "Waiting",
    };
    const stateNode = this.$("agentProgressState");
    stateNode.textContent = labels[state] || state.replaceAll("_", " ");
    stateNode.className = `agent-progress-state ${state}`;
    this.$("agentProgressActivity").textContent = sessionAvailable
      ? progress.current_activity ||
        (metadata.status === "running"
          ? "Waiting for semantic activity…"
          : metadata.detail || "Session finished")
      : "Select a live agent session";
    const targetNode = this.$("agentProgressTarget");
    targetNode.textContent = sessionAvailable
      ? String(progress.current_target || "")
      : "";
    targetNode.classList.toggle("hidden", !targetNode.textContent);

    const counters = [
      [progress.meaningful_events, "events"],
      [progress.files_touched, "files"],
      [progress.commands, "commands"],
      [progress.completed_tools, "tools done"],
    ];
    const stats = counters
      .filter(([value]) => Number(value || 0) > 0)
      .map(([value, label]) => `${Number(value)} ${label}`);
    this.$("agentProgressStats").textContent =
      stats.join(" · ") ||
      (sessionAvailable ? "Awaiting progress signals" : "");

    const warningNode = this.$("agentProgressWarning");
    const warning = String(this.state.loadError || progress.warning || "");
    warningNode.textContent = warning;
    warningNode.classList.toggle("hidden", !warning);
  }

  #resetChanges() {
    this.$("agentPlanOutput").replaceChildren(
      textElement("div", "console-empty", "No plan updates yet."),
    );
    this.$("agentDiffOutput").textContent = "No live diff available.";
  }

  #appendInteractions(events, { reset = false } = {}) {
    if (reset) this.state.interactions = [];
    if (events.length) this.state.interactions.push(...events);
    const overflow = this.state.interactions.length - MAX_INTERACTION_EVENTS;
    if (overflow > 0) this.state.interactions.splice(0, overflow);
    if (!(reset || events.length)) return;
    if (this.state.activeView === "conversation") this.#renderConversation();
    if (this.state.activeView === "changes") this.#renderChanges();
  }

  #renderConversation() {
    const output = this.$("agentConversationOutput");
    const follow =
      output.scrollHeight - output.scrollTop - output.clientHeight < 64 ||
      Boolean(output.querySelector(".console-empty"));
    const events = this.state.interactions;
    output.replaceChildren();
    if (!events.length) {
      output.append(
        textElement(
          "div",
          "console-empty",
          semanticMode(this.state.metadata)
            ? "Waiting for the agent to emit its first live event…"
            : "This legacy/standalone session exposes Terminal and Activity output.",
        ),
      );
      return;
    }

    const grouped = [];
    const byTool = new Map();
    for (const event of events) {
      const kind = String(event.kind || "status");
      const itemId = String(event.item_id || "");
      const parentItemId = String(event.parent_item_id || "");
      if (["assistant_delta", "assistant"].includes(kind)) {
        const previous = grouped.at(-1);
        if (
          previous?.role === "assistant" &&
          (!itemId || previous.itemId === itemId) &&
          previous.parentItemId === parentItemId
        ) {
          // Completed provider items are authoritative snapshots. Replacing the
          // accumulated deltas avoids rendering the same answer twice.
          previous.text =
            kind === "assistant"
              ? String(event.text || previous.text)
              : previous.text + String(event.text || "");
          previous.at = event.at || previous.at;
          continue;
        }
        grouped.push({
          role: "assistant",
          text: String(event.text || ""),
          itemId,
          parentItemId,
          at: event.at,
        });
        continue;
      }
      if (["reasoning_delta", "reasoning"].includes(kind)) {
        const previous = grouped.at(-1);
        if (
          previous?.role === "reasoning" &&
          (!itemId || previous.itemId === itemId) &&
          previous.parentItemId === parentItemId
        ) {
          previous.text =
            kind === "reasoning"
              ? String(event.text || previous.text)
              : previous.text + String(event.text || "");
          continue;
        }
        grouped.push({
          role: "reasoning",
          text: String(event.text || ""),
          itemId,
          parentItemId,
          at: event.at,
        });
        continue;
      }
      if (kind === "operator") {
        grouped.push({
          role: "operator",
          text: String(event.text || ""),
          at: event.at,
        });
        continue;
      }
      if (
        [
          "tool",
          "tool_input_delta",
          "tool_output",
          "tool_progress",
          "file_change",
          "approval",
          "question",
        ].includes(kind)
      ) {
        const key = itemId || `${kind}-${event.sequence || grouped.length}`;
        let item = byTool.get(key);
        if (!item) {
          item = {
            role: "tool",
            kind,
            title: String(event.title || kind.replaceAll("_", " ")),
            text: "",
            summary: String(event.summary || ""),
            category: String(event.category || ""),
            operation: String(event.operation || ""),
            target: String(event.target || ""),
            command: String(event.command || ""),
            parentItemId: String(event.parent_item_id || ""),
            status: String(event.status || "running"),
            data: event.data || {},
            at: event.at,
          };
          byTool.set(key, item);
          grouped.push(item);
        }
        const incomingText = String(event.text || "");
        if (kind !== "tool_input_delta") {
          item.text =
            kind === "tool" && incomingText
              ? incomingText
              : item.text + incomingText;
        }
        item.title = String(event.title || item.title);
        item.summary = String(event.summary || item.summary);
        item.category = String(event.category || item.category);
        item.operation = String(event.operation || item.operation);
        item.target = String(event.target || item.target);
        item.command = String(event.command || item.command);
        item.parentItemId = String(event.parent_item_id || item.parentItemId);
        item.status = String(event.status || item.status);
        item.data = event.data || item.data;
        item.at = event.at || item.at;
        continue;
      }
      if (["status", "session", "error", "operator_ack"].includes(kind)) {
        grouped.push({
          role: "status",
          text: String(event.text || event.title || kind),
          status: String(event.status || ""),
          at: event.at,
        });
      }
    }

    const fragment = document.createDocumentFragment();
    for (const item of grouped) {
      if (item.role === "status") {
        const node = textElement(
          "div",
          `conversation-status ${item.status === "failed" ? "failed" : ""}`,
          item.text,
        );
        node.title = interactionTime(item);
        fragment.append(node);
        continue;
      }
      if (item.role === "reasoning") {
        const details = document.createElement("details");
        details.className = "conversation-entry reasoning";
        if (item.parentItemId) details.classList.add("child-agent");
        const summary = textElement(
          "summary",
          "conversation-header",
          "Reasoning summary",
        );
        const body = textElement("div", "conversation-thinking", item.text);
        details.append(summary, body);
        fragment.append(details);
        continue;
      }

      const entry = document.createElement("article");
      entry.className = `conversation-entry ${item.role}`;
      if (item.role === "tool" && item.parentItemId)
        entry.classList.add("child-tool");
      if (item.role === "assistant" && item.parentItemId)
        entry.classList.add("child-agent");
      const avatar = textElement(
        "div",
        "conversation-avatar",
        item.role === "operator"
          ? "YOU"
          : item.role === "assistant"
            ? "AI"
            : "TOOL",
      );
      if (item.role === "assistant" && item.parentItemId)
        avatar.textContent = "SUB";
      const body = document.createElement("div");
      body.className = "conversation-body";
      const header = document.createElement("div");
      header.className = "conversation-header";
      header.append(
        document.createTextNode(
          item.role === "operator"
            ? "Operator"
            : item.role === "assistant"
              ? this.state.agentId
              : item.title,
        ),
        textElement("time", "", interactionTime(item)),
      );
      body.append(header);
      if (item.role === "tool") {
        const card = document.createElement("section");
        card.className = "conversation-card";
        const cardHeader = document.createElement("header");
        cardHeader.append(
          document.createTextNode(item.title),
          textElement("span", "", item.status),
        );
        card.append(cardHeader);
        const summary =
          item.summary ||
          item.target ||
          (item.status === "running" ? "Running…" : "Completed");
        card.append(textElement("div", "conversation-tool-summary", summary));
        const target = item.command || item.target;
        if (target && target !== summary) {
          card.append(textElement("code", "conversation-tool-target", target));
        }
        if (item.text) {
          const details = document.createElement("details");
          details.className = "conversation-tool-details";
          details.append(
            textElement(
              "summary",
              "",
              item.kind === "tool_output" ? "Tool output" : "Details",
            ),
            textElement("pre", "", item.text),
          );
          card.append(details);
        }
        body.append(card);
      } else {
        body.append(textElement("div", "conversation-text", item.text));
      }
      entry.append(avatar, body);
      fragment.append(entry);
    }
    output.append(fragment);
    if (this.state.activeView === "conversation" && follow)
      output.scrollTop = output.scrollHeight;
  }

  #renderChanges() {
    const events = this.state.interactions;
    const planEvent = [...events]
      .reverse()
      .find((event) => event.kind === "plan");
    const diffEvent = [...events]
      .reverse()
      .find((event) => event.kind === "diff");
    const plan = this.$("agentPlanOutput");
    plan.replaceChildren();
    const steps = Array.isArray(planEvent?.data?.steps)
      ? planEvent.data.steps
      : [];
    if (!steps.length) {
      plan.append(
        textElement(
          "div",
          "console-empty",
          planEvent?.text || "No plan updates yet.",
        ),
      );
    } else {
      for (const step of steps) {
        const status = String(step.status || step.state || "pending")
          .toLowerCase()
          .replaceAll(" ", "_");
        plan.append(
          textElement(
            "div",
            `agent-plan-step ${status}`,
            step.step || step.text || step.description || JSON.stringify(step),
          ),
        );
      }
      if (planEvent.text)
        plan.prepend(
          textElement("div", "conversation-thinking", planEvent.text),
        );
    }
    this.$("agentDiffOutput").textContent =
      diffEvent?.text || "No live diff available.";
  }

  #renderControls() {
    const state = this.state;
    const available = this.#terminalAvailable();
    const steering = this.#steeringAvailable();
    const descriptor = controlDescriptor(state.metadata);
    const mode = controlMode(state.metadata);
    const manual = state.manual || {};
    this.$("newAgentTerminal").disabled = !manual.can_start;
    this.$("newAgentTerminal").title =
      manual.reason || "Start the configured agent CLI in the task workspace";
    this.$("killAgentTerminal").disabled = !(
      manual.running &&
      state.metadata?.origin === "manual" &&
      state.metadata?.status === "running"
    );
    this.$("enableAgentInput").disabled =
      !available || state.attached || steering;
    this.$("enableAgentInput").classList.toggle(
      "hidden",
      !available || state.attached || steering,
    );
    this.$("agentInputDock").classList.toggle(
      "hidden",
      !(state.attached || steering),
    );
    this.$("agentReadOnlyNotice").classList.toggle(
      "hidden",
      !available || state.attached,
    );
    this.$("agentTerminalTools").classList.toggle("hidden", steering);
    this.$("interruptAgentTurn").classList.toggle("hidden", !steering);
    this.$("interruptAgentTurn").disabled = !steering;
    this.$("interruptAgentTurn").textContent = descriptor.interrupt;
    this.$("interruptAgentTurn").title = descriptor.interruptTitle;
    this.$("directAgentKeyboard").disabled = !state.attached || steering;
    this.$("agentTerminalInput").disabled = !(state.attached || steering);
    this.$("agentTerminalInput").placeholder = steering
      ? descriptor.placeholder
      : "Message the terminal… Enter sends, Shift+Enter adds a line";
    this.$("sendAgentTerminalInput").textContent = steering
      ? descriptor.send
      : "Send";
    this.$("sendAgentTerminalInput").disabled = !(state.attached || steering);
    this.$("focusAgentTerminal").disabled = !(
      state.attached && state.directKeyboard
    );
    document.querySelectorAll(".terminal-key-button").forEach((button) => {
      button.disabled = !(state.attached && state.directKeyboard);
    });
    this.$("interruptAgentTerminal").disabled = !state.attached;
    this.$("eofAgentTerminal").disabled = !state.attached;
    this.$("detachAgentTerminal").disabled = !state.attached;
    this.$("copyAgentConsole").disabled = !state.metadata?.session_id;
    this.$("agentConsoleControlHint").textContent = semanticMode(state.metadata)
      ? descriptor.hint
      : "Controls are bounded and audit-logged.";
    this.$("agentInputDock").dataset.controlMode = mode;
    this.#renderTerminalInteractionState();
  }

  #renderMeta() {
    const metadata = this.state.metadata || {};
    const parts = [];
    if (metadata.working_directory) parts.push(metadata.working_directory);
    if (metadata.started_at)
      parts.push(`started ${this.localTime(metadata.started_at)}`);
    if (metadata.duration_seconds !== undefined)
      parts.push(`${Math.round(metadata.duration_seconds)}s`);
    if (metadata.detail) parts.push(metadata.detail);
    this.$("agentConsoleMeta").textContent =
      parts.join(" · ") || "No agent terminal selected.";
  }

  #terminalAvailable() {
    const metadata = this.state.metadata;
    return Boolean(
      metadata?.status === "running" && metadata?.terminal?.enabled,
    );
  }

  #steeringAvailable() {
    const metadata = this.state.metadata;
    return Boolean(
      metadata?.status === "running" &&
        metadata?.interaction?.mode === "conversation" &&
        metadata?.interaction?.steering_supported,
    );
  }

  #selectSession(sessionId) {
    this.state.requestController?.abort();
    this.state.loading = false;
    Object.assign(this.state, {
      sessionId,
      offset: 0,
      events: [],
      interactionOffset: 0,
      interactions: [],
      version: -1,
      artifactPath: "",
      metadata: {},
      screen: {},
      screenToken: "",
      attached: false,
      acknowledged: false,
      directKeyboard: false,
      terminalFocused: false,
      rawBuffer: "",
      attachAfterStart: false,
      sessionsUpdatedAt: Date.now(),
    });
    this.$("directAgentKeyboard").checked = false;
    this.$("agentTerminalKeySink").blur();
    this.#resetActivity();
    this.#resetConversation();
    this.#resetChanges();
    this.$("agentArtifactOutput").textContent =
      "Select Result to load the durable artifact.";
    this.load();
  }

  async #start() {
    if (!this.state.manual?.can_start) return;
    const confirmed = window.confirm(
      `Start ${this.state.agentId} in the task workspace?\n\nThe configured provider can inspect and modify files within its normal permissions. Orchestration remains paused until this terminal exits.`,
    );
    if (!confirmed) return;
    try {
      const result = await this.api("/api/agent/console/start", {
        method: "POST",
        body: JSON.stringify({
          agent_id: this.state.agentId,
          acknowledged: true,
        }),
      });
      Object.assign(this.state, {
        sessionId: result.session_id || "",
        offset: 0,
        events: [],
        screen: {},
        screenToken: "",
        sessionsUpdatedAt: 0,
        attachAfterStart: true,
      });
      this.#resetActivity();
      await this.load();
      this.toast(`Terminal started for ${this.state.agentId}`);
    } catch (error) {
      this.toast(error.message, true);
    }
  }

  async #stop() {
    if (!this.state.manual?.running) return;
    if (!window.confirm(`Kill the standalone ${this.state.agentId} terminal?`))
      return;
    try {
      await this.api("/api/agent/console/stop", {
        method: "POST",
        body: JSON.stringify({
          agent_id: this.state.agentId,
          session_id: this.state.sessionId,
        }),
      });
      this.state.attached = false;
      this.state.acknowledged = false;
      this.state.sessionsUpdatedAt = 0;
      await this.load();
      this.toast(`Terminal stopped for ${this.state.agentId}`);
    } catch (error) {
      this.toast(error.message, true);
    }
  }

  #enableInput() {
    if (!this.#terminalAvailable()) return;
    if (this.state.metadata?.origin !== "manual") {
      const confirmed = window.confirm(
        "Enable operator input for this orchestrated agent?\n\nInput can change files and the current result. Every action is bounded and audit-logged.",
      );
      if (!confirmed) return;
    }
    this.state.attached = true;
    this.state.acknowledged = true;
    this.state.directKeyboard = true;
    this.$("directAgentKeyboard").checked = true;
    this.#renderControls();
    this.#resize({ immediate: true });
    this.#focusTerminal();
  }

  #sendAction(action, extra = {}, { allowSteering = false } = {}) {
    const state = this.state;
    const authorized = allowSteering
      ? this.#steeringAvailable()
      : state.attached && state.acknowledged;
    if (!authorized || !state.sessionId) {
      return Promise.resolve(undefined);
    }
    return new Promise((resolve, reject) => {
      state.actionQueue.push({
        sessionId: state.sessionId,
        action,
        extra,
        resolve,
        reject,
      });
      this.#drainActionQueue(state);
    });
  }

  async #drainActionQueue(state) {
    if (state.actionDraining) return;
    state.actionDraining = true;
    try {
      while (state.actionQueue.length) {
        const item = state.actionQueue.shift();
        try {
          const result = await this.api("/api/agent/console/input", {
            method: "POST",
            body: JSON.stringify({
              session_id: item.sessionId,
              action: item.action,
              acknowledged: true,
              ...item.extra,
            }),
          });
          if (item.action === "steer") this.#schedule({ immediate: true });
          item.resolve(result);
        } catch (error) {
          this.toast(error.message, true);
          item.reject(error);
        }
      }
    } finally {
      state.actionDraining = false;
      if (state.actionQueue.length) this.#drainActionQueue(state);
    }
  }

  async #sendLine() {
    const input = this.$("agentTerminalInput");
    const steering = this.#steeringAvailable();
    const data = steering
      ? String(input.value || "").trim()
      : composerPayload(input.value);
    if (!data) return;
    input.value = "";
    if (!steering) await this.#flushDirectInput();
    await this.#sendAction(
      steering ? "steer" : "input",
      { data },
      { allowSteering: steering },
    );
  }

  #flushDirectInput() {
    const state = this.state;
    if (state.rawTimer) window.clearTimeout(state.rawTimer);
    state.rawTimer = null;
    const data = state.rawBuffer;
    state.rawBuffer = "";
    return data
      ? this.#sendAction("input", { data })
      : Promise.resolve(undefined);
  }

  #queueDirectInput(value) {
    if (!this.state.attached || !this.state.directKeyboard || !value) return;
    this.state.rawBuffer += value;
    if (this.state.rawTimer) return;
    this.state.rawTimer = window.setTimeout(() => {
      this.#flushDirectInput().catch(() => {
        /* toast already shown */
      });
    }, RAW_INPUT_BATCH_MS);
  }

  #sendTerminalKey(key) {
    const sequence = terminalSequenceForKey(key);
    if (!sequence || !this.state.attached || !this.state.directKeyboard) return;
    this.#queueDirectInput(sequence);
    this.#focusTerminal();
  }

  #resize({ immediate = false } = {}) {
    if (!this.state.attached) return;
    if (this.state.resizeTimer) window.clearTimeout(this.state.resizeTimer);
    const send = () => {
      this.state.resizeTimer = null;
      const terminal = this.$("agentTerminalScreen");
      const columns = Math.max(
        40,
        Math.min(500, Math.floor(terminal.clientWidth / 7.25)),
      );
      const rows = Math.max(
        10,
        Math.min(300, Math.floor(terminal.clientHeight / 16.3)),
      );
      if (rows === this.state.lastRows && columns === this.state.lastColumns)
        return;
      this.state.lastRows = rows;
      this.state.lastColumns = columns;
      this.#sendAction("resize", { rows, columns }).catch(() => {});
    };
    if (immediate) send();
    else this.state.resizeTimer = window.setTimeout(send, RESIZE_DEBOUNCE_MS);
  }

  async #copy() {
    const content =
      this.state.activeView === "activity"
        ? this.state.events.map((event) => event.text || "").join("")
        : this.state.activeView === "result"
          ? this.$("agentArtifactOutput").textContent
          : this.state.activeView === "conversation"
            ? this.state.interactions
                .filter((event) =>
                  [
                    "operator",
                    "assistant",
                    "assistant_delta",
                    "reasoning",
                    "reasoning_delta",
                    "tool",
                    "tool_output",
                    "tool_progress",
                    "file_change",
                    "status",
                    "error",
                  ].includes(event.kind),
                )
                .map((event) => {
                  if (
                    [
                      "tool",
                      "tool_output",
                      "tool_progress",
                      "file_change",
                    ].includes(event.kind)
                  ) {
                    const subject =
                      event.command ||
                      event.target ||
                      event.summary ||
                      event.text ||
                      "";
                    return `${event.title || event.kind} [${event.status || ""}]: ${subject}`;
                  }
                  return `${event.kind}: ${event.text || event.summary || ""}`;
                })
                .join("\n")
            : this.state.activeView === "changes"
              ? `${this.$("agentPlanOutput").textContent}\n\n${this.$("agentDiffOutput").textContent}`
              : this.state.screen?.content || "";
    try {
      await navigator.clipboard.writeText(content);
      this.toast("Console content copied");
    } catch (_) {
      this.toast("Clipboard access failed", true);
    }
  }

  async #loadArtifact() {
    if (!this.state.artifactPath) {
      this.$("agentArtifactOutput").textContent =
        "This session has no durable result artifact.";
      return;
    }
    try {
      const data = await this.api(
        `/api/agent/artifact?path=${encodeURIComponent(this.state.artifactPath)}`,
      );
      this.$("agentArtifactOutput").textContent =
        data.content +
        (data.truncated ? "\n\n[artifact preview truncated]" : "");
    } catch (error) {
      this.$("agentArtifactOutput").textContent = error.message;
    }
  }

  #setView(view) {
    if (view !== this.state.activeView) this.state.requestController?.abort();
    this.state.activeView = view;
    document.querySelectorAll("[data-console-view]").forEach((button) => {
      const active = button.dataset.consoleView === view;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
      button.tabIndex = active ? 0 : -1;
    });
    document
      .querySelectorAll(".console-view")
      .forEach((panel) => panel.classList.add("hidden"));
    this.$(
      `agentConsole${view[0].toUpperCase()}${view.slice(1)}View`,
    ).classList.remove("hidden");
    if (view === "result") this.#loadArtifact();
    if (view === "activity")
      this.$("agentActivityOutput").scrollTop = this.$(
        "agentActivityOutput",
      ).scrollHeight;
    if (view === "conversation") {
      this.#renderConversation();
      this.$("agentConversationOutput").scrollTop = this.$(
        "agentConversationOutput",
      ).scrollHeight;
    }
    if (view === "changes") this.#renderChanges();
    if (view === "terminal") this.#focusTerminal();
    this.#schedule({ immediate: true });
  }

  #applyWorkbenchState() {
    const panel = this.$("agentConsoleModal");
    panel.classList.toggle("maximized", this.maximized);
    document.body.classList.toggle("workbench-open", this.state.open);
    document.body.classList.toggle(
      "workbench-maximized",
      this.state.open && this.maximized,
    );
    this.$("maximizeAgentConsole").textContent = this.maximized ? "❐" : "□";
    this.$("maximizeAgentConsole").title = this.maximized
      ? "Restore agent workbench"
      : "Maximize agent workbench";
    if (this.state.open) queueMicrotask(() => this.#resize({ immediate: true }));
  }

  #toggleMaximized() {
    this.maximized = !this.maximized;
    this.#applyWorkbenchState();
  }

  #pollDelay() {
    if (document.hidden) return HIDDEN_POLL_MS;
    if (this.state.metadata?.status === "running") {
      if (semanticMode(this.state.metadata)) return 40;
      return this.state.activeView === "terminal"
        ? RUNNING_POLL_MS
        : ACTIVITY_POLL_MS;
    }
    return IDLE_POLL_MS;
  }

  #schedule({ immediate = false } = {}) {
    if (this.state.timer) window.clearTimeout(this.state.timer);
    this.state.timer = null;
    if (!this.state.open) return;
    this.state.timer = window.setTimeout(
      () => this.load(),
      immediate ? 0 : this.#pollDelay(),
    );
  }

  #stopTimers() {
    for (const key of ["timer", "rawTimer", "resizeTimer"]) {
      if (this.state[key]) window.clearTimeout(this.state[key]);
      this.state[key] = null;
    }
  }

  #bind() {
    const terminal = this.$("agentTerminalScreen");
    const surface = this.$("agentTerminalSurface");
    const sink = this.$("agentTerminalKeySink");

    this.$("closeAgentConsole").addEventListener("click", () => this.close());
    this.$("maximizeAgentConsole").addEventListener("click", () =>
      this.#toggleMaximized(),
    );
    this.$("agentConsoleSession").addEventListener("change", (event) =>
      this.#selectSession(event.target.value),
    );
    this.$("newAgentTerminal").addEventListener("click", () => this.#start());
    this.$("killAgentTerminal").addEventListener("click", () => this.#stop());
    this.$("enableAgentInput").addEventListener("click", () =>
      this.#enableInput(),
    );
    this.$("sendAgentTerminalInput").addEventListener("click", () => {
      this.#sendLine().catch(() => {
        /* toast already shown */
      });
    });
    this.$("interruptAgentTerminal").addEventListener("click", async () => {
      try {
        await this.#flushDirectInput();
        await this.#sendAction("signal", { signal: "interrupt" });
      } catch (_) {
        /* toast already shown */
      }
    });
    this.$("interruptAgentTurn").addEventListener("click", async () => {
      try {
        await this.#sendAction(
          "signal",
          { signal: "interrupt" },
          { allowSteering: true },
        );
        this.#schedule({ immediate: true });
      } catch (_) {
        /* toast already shown */
      }
    });
    this.$("eofAgentTerminal").addEventListener("click", async () => {
      try {
        await this.#flushDirectInput();
        await this.#sendAction("eof");
      } catch (_) {
        /* toast already shown */
      }
    });
    this.$("detachAgentTerminal").addEventListener("click", () => {
      this.state.attached = false;
      this.state.acknowledged = false;
      this.state.directKeyboard = false;
      this.state.terminalFocused = false;
      this.$("directAgentKeyboard").checked = false;
      sink.blur();
      this.#renderControls();
    });
    this.$("agentTerminalInput").addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        this.#sendLine().catch(() => {
          /* toast already shown */
        });
      }
    });
    this.$("directAgentKeyboard").addEventListener("change", (event) => {
      this.state.directKeyboard = Boolean(
        event.target.checked && this.state.attached,
      );
      if (this.state.directKeyboard) this.#focusTerminal();
      else {
        this.state.terminalFocused = false;
        sink.blur();
        this.#renderTerminalInteractionState();
      }
      this.#renderControls();
    });
    this.$("focusAgentTerminal").addEventListener("click", () =>
      this.#focusTerminal(),
    );
    document.querySelectorAll(".terminal-key-button").forEach((button) => {
      button.addEventListener("click", () =>
        this.#sendTerminalKey(button.dataset.terminalKey || ""),
      );
    });

    const handleTerminalKey = (event) => {
      if (!this.state.attached || !this.state.directKeyboard) return;
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "v")
        return;
      const sequence = terminalSequence(event);
      if (!sequence) return;
      event.preventDefault();
      event.stopPropagation();
      this.#queueDirectInput(sequence);
    };
    terminal.addEventListener("keydown", handleTerminalKey);
    sink.addEventListener("keydown", handleTerminalKey);
    sink.addEventListener("input", () => {
      if (!this.state.attached || !this.state.directKeyboard || !sink.value)
        return;
      const value = sink.value;
      sink.value = "";
      this.#queueDirectInput(value);
    });
    const handlePaste = (event) => {
      if (!this.state.attached || !this.state.directKeyboard) return;
      const text = event.clipboardData?.getData("text") || "";
      if (!text) return;
      event.preventDefault();
      this.#queueDirectInput(text);
    };
    terminal.addEventListener("paste", handlePaste);
    sink.addEventListener("paste", handlePaste);
    surface.addEventListener("pointerdown", () => {
      queueMicrotask(() => this.#focusTerminal());
    });
    terminal.addEventListener("focus", () => this.#focusTerminal());
    sink.addEventListener("focus", () => {
      this.state.terminalFocused = true;
      this.#renderTerminalInteractionState();
    });
    sink.addEventListener("blur", () => {
      this.state.terminalFocused = false;
      this.#renderTerminalInteractionState();
    });

    this.$("copyAgentConsole").addEventListener("click", () => this.#copy());
    this.$("clearAgentActivity").addEventListener("click", () => {
      this.state.events = [];
      this.#resetActivity();
    });
    document.querySelectorAll("[data-console-view]").forEach((button) => {
      button.addEventListener("click", () => {
        this.#setView(button.dataset.consoleView);
        if (button.closest("#agentAdvancedControls"))
          this.$("agentAdvancedControls").open = false;
      });
    });
    const primaryTabs = [
      ...document.querySelectorAll(".console-primary-tabs > [data-console-view]"),
    ];
    primaryTabs.forEach((button) => {
      button.addEventListener("keydown", (event) => {
        const availableTabs = primaryTabs.filter(
          (tab) => !tab.disabled && !tab.classList.contains("hidden"),
        );
        const index = availableTabs.indexOf(button);
        if (index < 0 || !availableTabs.length) return;
        let next = -1;
        if (event.key === "ArrowRight") next = (index + 1) % availableTabs.length;
        if (event.key === "ArrowLeft")
          next = (index - 1 + availableTabs.length) % availableTabs.length;
        if (event.key === "Home") next = 0;
        if (event.key === "End") next = availableTabs.length - 1;
        if (next < 0) return;
        event.preventDefault();
        this.#setView(availableTabs[next].dataset.consoleView);
        availableTabs[next].focus();
      });
    });
    window.addEventListener("resize", () => this.#resize());
    document.addEventListener("visibilitychange", () => {
      if (this.state.open && !document.hidden)
        this.#schedule({ immediate: true });
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && this.state.open && !event.defaultPrevented)
        this.close();
    });
  }
}
