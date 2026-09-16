/** Shared, dependency-free helpers for the dashboard modules. */

const HTML_ESCAPE = Object.freeze({
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;",
});

/** Resolve a required dashboard element and fail with an actionable error. */
export function elementById(id) {
  const element = document.getElementById(id);
  if (!element) throw new Error(`Dashboard element #${id} is missing`);
  return element;
}

/** Escape untrusted text before inserting it into an HTML template. */
export function escapeHtml(value) {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (character) => HTML_ESCAPE[character],
  );
}

export function truncateText(value, length = 52) {
  const text = String(value ?? "");
  return text.length > length
    ? `${text.slice(0, Math.max(0, length - 1))}…`
    : text;
}

export function formatLocalTime(value, fallback = "—") {
  if (!value) return fallback;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

export function formatBytes(value, { zeroLabel = "0 B" } = {}) {
  const bytes = Math.max(0, Number(value) || 0);
  if (!bytes) return zeroLabel;
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let amount = bytes;
  let unit = 0;
  while (amount >= 1024 && unit < units.length - 1) {
    amount /= 1024;
    unit += 1;
  }
  const rendered =
    amount >= 10 || unit === 0 ? Math.round(amount) : amount.toFixed(1);
  return `${rendered} ${units[unit]}`;
}

/** Format large non-negative counters compactly without hiding smaller values. */
export function compactCount(value) {
  const count = Math.max(0, Number(value) || 0);
  return new Intl.NumberFormat(undefined, {
    notation: count >= 10000 ? "compact" : "standard",
    maximumFractionDigits: 1,
  }).format(count);
}

export function readJsonStorage(key) {
  try {
    const value = window.localStorage.getItem(key);
    const parsed = value ? JSON.parse(value) : {};
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? parsed
      : {};
  } catch (_) {
    return {};
  }
}

export function updateJsonStorage(key, patch) {
  try {
    window.localStorage.setItem(
      key,
      JSON.stringify({ ...readJsonStorage(key), ...patch }),
    );
  } catch (_) {
    // Persistence is optional in private or policy-restricted browser profiles.
  }
}

const pendingSelectSyncs = new WeakMap();
const selectBlurHooks = new WeakSet();

function normalizeSelectOption(option) {
  return {
    value: String(option?.value ?? ""),
    label: String(option?.label ?? option?.text ?? option?.value ?? ""),
    disabled: Boolean(option?.disabled),
  };
}

function selectOptionsMatch(select, options) {
  if (select.options.length !== options.length) return false;
  return options.every((option, index) => {
    const current = select.options[index];
    return (
      current.value === option.value &&
      current.textContent === option.label &&
      current.disabled === option.disabled
    );
  });
}

function requestedSelectValue(request) {
  const available = new Set(request.options.map((option) => option.value));
  if (available.has(request.value)) return request.value;
  if (available.has(request.fallbackValue)) return request.fallbackValue;
  return request.options[0]?.value ?? "";
}

function applySelectSync(
  select,
  request,
  { preserveCurrentValue = false } = {},
) {
  if (!select.isConnected) return false;
  const currentValue = select.value;
  if (!selectOptionsMatch(select, request.options)) {
    select.replaceChildren(
      ...request.options.map((item) => {
        const option = document.createElement("option");
        option.value = item.value;
        option.textContent = item.label;
        option.disabled = item.disabled;
        return option;
      }),
    );
  }
  const currentStillAvailable = request.options.some(
    (option) => option.value === currentValue,
  );
  const value =
    preserveCurrentValue && currentStillAvailable
      ? currentValue
      : requestedSelectValue(request);
  if (select.value !== value) select.value = value;
  if (request.disabled !== undefined)
    select.disabled = Boolean(request.disabled);
  return true;
}

function deferSelectSync(select, request) {
  pendingSelectSyncs.set(select, request);
  if (selectBlurHooks.has(select)) return;
  selectBlurHooks.add(select);
  select.addEventListener(
    "blur",
    () => {
      selectBlurHooks.delete(select);
      const pending = pendingSelectSyncs.get(select);
      pendingSelectSyncs.delete(select);
      if (pending)
        applySelectSync(select, pending, { preserveCurrentValue: true });
    },
    { once: true },
  );
}

/**
 * Reconcile a select without tearing down an actively used native picker.
 *
 * Dashboard snapshots are intentionally polled. Replacing <option> nodes on
 * every poll closes an open native dropdown even when the data did not change.
 * This helper keeps stable options intact and defers a real option/value change
 * until blur when the operator is currently using the control.
 */
export function syncSelectOptions(
  select,
  options,
  { value = "", fallbackValue = "", disabled } = {},
) {
  const request = {
    options: options.map(normalizeSelectOption),
    value: String(value ?? ""),
    fallbackValue: String(fallbackValue ?? ""),
    disabled,
  };
  if (document.activeElement === select) {
    const requestedValue = requestedSelectValue(request);
    const disabledMatches =
      request.disabled === undefined ||
      select.disabled === Boolean(request.disabled);
    if (
      selectOptionsMatch(select, request.options) &&
      select.value === requestedValue &&
      disabledMatches
    ) {
      pendingSelectSyncs.delete(select);
      return true;
    }
    deferSelectSync(select, request);
    return false;
  }
  pendingSelectSyncs.delete(select);
  return applySelectSync(select, request);
}

/** Return true while a mutable form control inside a region owns focus. */
export function hasActiveFormInteraction(container) {
  const active = document.activeElement;
  return Boolean(
    active &&
      container?.contains(active) &&
      active.matches("select, input, textarea, [contenteditable='true']"),
  );
}

export async function downloadAuthenticatedArtifact(path, token) {
  const headers = new Headers();
  headers.set("X-Execraft-Token", token);
  const response = await fetch(path, { cache: "no-store", headers });
  if (!response.ok) {
    const text = await response.text();
    let message = text || response.statusText;
    try { message = JSON.parse(text).error || message; } catch (_) {}
    throw new Error(message);
  }
  const blob = await response.blob();
  const disposition = response.headers.get("Content-Disposition") || "";
  const match = disposition.match(/filename="?([^";]+)"?/i);
  const filename = match?.[1] || "execraft-export";
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  return filename;
}
