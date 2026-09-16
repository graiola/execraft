from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is required for dashboard JavaScript tests")
def test_sync_select_options_defers_active_picker_and_preserves_user_choice(tmp_path: Path) -> None:
    source = Path("src/execraft/assets/gui/ui-utils.js").read_text(encoding="utf-8")
    module = tmp_path / "ui-utils.mjs"
    module.write_text(source, encoding="utf-8")
    script = tmp_path / "selection-regression.mjs"
    script.write_text(
        textwrap.dedent(
            """
            import { syncSelectOptions } from "./ui-utils.mjs";

            globalThis.document = {
              activeElement: null,
              createElement(tag) {
                if (tag !== "option") throw new Error(`unexpected tag ${tag}`);
                return { value: "", textContent: "", disabled: false };
              },
            };

            class FakeSelect {
              constructor() {
                this.options = [];
                this.value = "";
                this.disabled = false;
                this.isConnected = true;
                this.listeners = new Map();
                this.replaceCount = 0;
              }
              replaceChildren(...children) {
                this.options = children;
                this.replaceCount += 1;
                if (!children.some((item) => item.value === this.value))
                  this.value = children[0]?.value || "";
              }
              addEventListener(type, callback) {
                this.listeners.set(type, callback);
              }
              blur() {
                document.activeElement = null;
                const callback = this.listeners.get("blur");
                this.listeners.delete("blur");
                if (callback) callback();
              }
            }

            const select = new FakeSelect();
            syncSelectOptions(
              select,
              [
                { value: "a", label: "Agent A" },
                { value: "b", label: "Agent B" },
              ],
              { value: "a" },
            );
            if (select.value !== "a" || select.replaceCount !== 1)
              throw new Error("initial select reconciliation failed");

            document.activeElement = select;
            const applied = syncSelectOptions(
              select,
              [
                { value: "a", label: "Agent A" },
                { value: "b", label: "Agent B" },
                { value: "c", label: "Agent C" },
              ],
              { value: "a" },
            );
            if (applied !== false || select.replaceCount !== 1)
              throw new Error("active picker was rebuilt during polling");

            // Emulate the operator choosing B after the poll has already queued
            // an authoritative A refresh. Blur must preserve the user choice.
            select.value = "b";
            select.blur();
            if (select.replaceCount !== 2)
              throw new Error("deferred option update was not flushed on blur");
            if (select.value !== "b")
              throw new Error(`operator choice was overwritten: ${select.value}`);

            // Once the control is idle, the next authoritative render is free
            // to reconcile the selected value normally.
            syncSelectOptions(select, select.options.map((item) => ({
              value: item.value,
              label: item.textContent,
              disabled: item.disabled,
            })), { value: "c" });
            if (select.value !== "c")
              throw new Error("idle select did not reconcile authoritative value");
            """
        ),
        encoding="utf-8",
    )
    subprocess.run([NODE, script], check=True, cwd=tmp_path)
