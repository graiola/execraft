from pathlib import Path


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"expected block not found in {path}: {old[:120]!r}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


health = Path("src/execraft/assets/gui/execution-health-view.js")
replace_once(
    health,
    '    this.lastTrigger = null;\n    this.latest = summarizeExecutionHealth({});',
    '    this.lastTrigger = null;\n    this.pagePosition = null;\n    this.latest = summarizeExecutionHealth({});',
)
replace_once(
    health,
    '  open({ focus = true } = {}) {\n    if (this.isOpen()) return;\n    this.lastTrigger = document.activeElement instanceof HTMLElement ? document.activeElement : this.openButton;',
    '  open({ focus = true } = {}) {\n    if (this.isOpen()) return;\n    this.pagePosition = { x: window.scrollX, y: window.scrollY };\n    this.lastTrigger = document.activeElement instanceof HTMLElement ? document.activeElement : this.openButton;',
)
replace_once(
    health,
    '''    this.onVisibilityChange(false);\n    if (restoreFocus) {\n      const target = this.lastTrigger?.isConnected ? this.lastTrigger : this.openButton;\n      queueMicrotask(() => target?.focus({ preventScroll: true }));\n    }''',
    '''    this.onVisibilityChange(false);\n    const pagePosition = this.pagePosition;\n    this.pagePosition = null;\n    const target = this.lastTrigger?.isConnected ? this.lastTrigger : this.openButton;\n    queueMicrotask(() => {\n      if (restoreFocus) target?.focus({ preventScroll: true });\n      if (pagePosition) window.scrollTo({ left: pagePosition.x, top: pagePosition.y, behavior: "auto" });\n    });''',
)

inspector = Path("src/execraft/assets/gui/work-package-inspector.js")
replace_once(
    inspector,
    '    this.scrollByTab = new Map();\n\n    this.tablist.addEventListener',
    '    this.scrollByTab = new Map();\n    this.pagePosition = null;\n    this.workflowPosition = null;\n\n    this.tablist.addEventListener',
)
replace_once(
    inspector,
    '''  open({ packageId, title = "WorkPackage details", meta = "", tab = "overview" }) {\n    const nextPackageId = String(packageId || "");''',
    '''  open({ packageId, title = "WorkPackage details", meta = "", tab = "overview" }) {\n    this.pagePosition = { x: window.scrollX, y: window.scrollY };\n    const workflow = document.querySelector("#workflowWrap");\n    this.workflowPosition = workflow\n      ? { left: workflow.scrollLeft, top: workflow.scrollTop }\n      : null;\n    const nextPackageId = String(packageId || "");''',
)
replace_once(
    inspector,
    '''    if (packageChanged) this.content.scrollTop = 0;\n    requestAnimationFrame(() => this.title.focus({ preventScroll: true }));''',
    '''    if (packageChanged) this.content.scrollTop = 0;\n    requestAnimationFrame(() => {\n      this.title.focus({ preventScroll: true });\n      const workflow = document.querySelector("#workflowWrap");\n      if (workflow && this.workflowPosition) {\n        workflow.scrollTo({ ...this.workflowPosition, behavior: "auto" });\n      }\n      if (this.pagePosition) {\n        window.scrollTo({ ...this.pagePosition, behavior: "auto" });\n      }\n    });''',
)
replace_once(
    inspector,
    '''    this.#rememberScroll();\n    const pagePosition = { x: window.scrollX, y: window.scrollY };\n    this.root.hidden = true;''',
    '''    this.#rememberScroll();\n    const pagePosition = this.pagePosition || { x: window.scrollX, y: window.scrollY };\n    const workflowPosition = this.workflowPosition;\n    this.pagePosition = null;\n    this.workflowPosition = null;\n    this.root.hidden = true;''',
)
replace_once(
    inspector,
    '''      const target = this.focusReturnTarget();\n      target?.focus?.({ preventScroll: true });\n      window.scrollTo({''',
    '''      const target = this.focusReturnTarget();\n      target?.focus?.({ preventScroll: true });\n      const workflow = document.querySelector("#workflowWrap");\n      if (workflow && workflowPosition) {\n        workflow.scrollTo({ ...workflowPosition, behavior: "auto" });\n      }\n      window.scrollTo({''',
)

tests = Path("tests/test_gui_browser.py")
text = tests.read_text(encoding="utf-8")
old = '''        for _ in range(4):\n            page.locator('[data-workflow-viewport-action="zoom-in"]').click()\n        page.evaluate("document.querySelector('#workflowWrap').scrollLeft = 40")\n        before_left = page.evaluate("document.querySelector('#workflowWrap').scrollLeft")\n        before_page = page.evaluate("window.scrollY")\n        point = _workflow_visible_point(page)'''
new = '''        for _ in range(4):\n            page.locator('[data-workflow-viewport-action="zoom-in"]').click()\n        page.evaluate(\n            """() => {\n              const wrap = document.querySelector('#workflowWrap');\n              const top = wrap.getBoundingClientRect().top + window.scrollY;\n              window.scrollTo(0, Math.max(0, top - 200));\n              wrap.scrollLeft = 40;\n            }"""\n        )\n        before_left = page.evaluate("document.querySelector('#workflowWrap').scrollLeft")\n        before_page = page.evaluate("window.scrollY")\n        point = _workflow_visible_point(page)'''
if old not in text:
    raise SystemExit("wheel block not found")
text = text.replace(old, new, 1)

old = '''        track = page.locator("[data-roadmap-row='task-demo'] .roadmap-row-track")\n        box = track.bounding_box()\n        assert box is not None\n        page.mouse.click(box["x"] + min(180, box["width"] / 2), box["y"] + box["height"] / 2)\n        rename = page.locator(".roadmap-inline-rename")'''
new = '''        track = page.locator("[data-roadmap-row='task-demo'] .roadmap-row-track")\n        box = track.bounding_box()\n        assert box is not None\n        placement = page.evaluate(\n            """() => {\n              const row = document.querySelector("[data-roadmap-row='task-demo']");\n              const track = row.querySelector('.roadmap-row-track').getBoundingClientRect();\n              const bars = [...row.querySelectorAll('.roadmap-item-bar')]\n                .map((bar) => bar.getBoundingClientRect())\n                .sort((left, right) => left.x - right.x);\n              let x = null;\n              for (let index = 0; index < bars.length - 1; index += 1) {\n                const gap = bars[index + 1].x - (bars[index].x + bars[index].width);\n                if (gap >= 24) {\n                  x = bars[index].x + bars[index].width + Math.min(gap / 2, 80);\n                  break;\n                }\n              }\n              if (x === null && bars.length && track.right - (bars.at(-1).x + bars.at(-1).width) >= 24) {\n                x = bars.at(-1).x + bars.at(-1).width + 40;\n              }\n              if (x === null) x = track.x + 40;\n              return {x, y: track.y + track.height / 2};\n            }"""\n        )\n        page.mouse.click(placement["x"], placement["y"])\n        rename = page.locator(".roadmap-inline-rename")'''
if old not in text:
    raise SystemExit("roadmap placement block not found")
tests.write_text(text.replace(old, new, 1), encoding="utf-8")
