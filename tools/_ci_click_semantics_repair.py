from pathlib import Path

path = Path("tests/test_gui_browser.py")
text = path.read_text(encoding="utf-8")

old = '''        opener = page.locator("#executionHealthDrawerOpen")\n        opener.click()'''
new = '''        opener = page.locator("#executionHealthDrawerOpen")\n        # Dispatch the application click without Playwright first scrolling an\n        # off-screen trigger into view; this test measures Execraft scroll\n        # preservation, not locator.click() geometry assistance.\n        opener.evaluate("node => node.click()")'''
if old not in text:
    raise SystemExit("execution-health click block not found")
text = text.replace(old, new, 1)

needle = '''        page.locator('[data-work-package-action="select"][data-id="F"]').click()'''
replacement = '''        page.locator('[data-work-package-action="select"][data-id="F"]').evaluate(\n            "node => node.click()"\n        )'''
if text.count(needle) < 2:
    raise SystemExit("expected two Work Package selection click blocks")
text = text.replace(needle, replacement, 2)

path.write_text(text, encoding="utf-8")
