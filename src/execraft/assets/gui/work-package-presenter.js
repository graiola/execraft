import { escapeHtml as esc } from "./ui-utils.js";

/** Pure HTML projections shared by workPackage Overview/Evidence surfaces. */
export function hasStructuredContent(value) {
  if (value === null || value === undefined || value === "") return false;
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === "object") return Object.keys(value).length > 0;
  return true;
}

export function structuredEvidence(title, value) {
  if (!hasStructuredContent(value)) return "";
  let rendered = "";
  if (typeof value === "string") rendered = value;
  else {
    try {
      rendered = JSON.stringify(value, null, 2);
    } catch (_error) {
      rendered = String(value);
    }
  }
  return `<details class="work-package-output"><summary>${esc(title)}</summary><pre>${esc(rendered)}</pre></details>`;
}

export function packageRelationButton(packageInfo, { prefix = "" } = {}) {
  if (!packageInfo) return '<span class="relationship-empty">None</span>';
  const stage = packageInfo.operator_paused ? "paused" : packageInfo.stage;
  return `<button class="relationship-chip" data-open-package="${esc(packageInfo.id)}" title="Open ${esc(packageInfo.id)}"><strong>${esc(prefix + packageInfo.id)}</strong><span>${esc(stage)}</span></button>`;
}

export function packageRelationList(items, empty = "None") {
  return items.length
    ? items.map((item) => packageRelationButton(item)).join("")
    : `<span class="relationship-empty">${esc(empty)}</span>`;
}

export function contractList(items, empty) {
  return items.length
    ? `<ul class="work-package-contract-list">${items.map((item) => `<li>${esc(item)}</li>`).join("")}</ul>`
    : `<div class="relationship-empty">${esc(empty)}</div>`;
}

export function acceptanceCriteriaMarkup(criteria) {
  if (!criteria.length)
    return '<div class="relationship-empty">No acceptance criteria declared.</div>';
  return `<div class="acceptance-list">${criteria.map((criterion) => `<article class="acceptance-item ${criterion.verified ? "verified" : ""}"><span class="acceptance-state">${criterion.verified ? "✓" : "○"}</span><div><strong>${esc(criterion.id || "criterion")}</strong><p>${esc(criterion.description || "")}</p>${criterion.evidence ? `<small>${esc(criterion.evidence)}</small>` : ""}</div></article>`).join("")}</div>`;
}
