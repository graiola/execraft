"""Optional Playwright environment probe.

This adapter is a local environment check only. Authentication and remote
execution remain explicit user-driven operations; CI uses the fake adapter.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from execraft.browser.adapter import (
    BrowserAdapter,
    ExecuteResult,
    PrepareResult,
    ProbeResult,
    RunStatus,
)


class PlaywrightProbeAdapter(BrowserAdapter):
    async def login(self, profile_dir: Path | None = None) -> dict[str, Any]:
        return {
            "authenticated": False,
            "profile": str(profile_dir) if profile_dir else None,
            "message": "Authentication is interactive and is not performed by the probe adapter.",
        }

    async def probe(self) -> ProbeResult:
        try:
            installed = importlib.util.find_spec("playwright.async_api") is not None
        except ModuleNotFoundError:
            installed = False
        if not installed:
            return ProbeResult(
                available=False,
                message="Playwright is not installed; install the optional browser dependencies.",
            )
        try:
            from playwright.async_api import async_playwright

            async with async_playwright() as playwright:
                executable = Path(playwright.chromium.executable_path)
                if not executable.is_file():
                    return ProbeResult(
                        available=False,
                        message="Playwright is installed but the Chromium browser is missing.",
                    )
                return ProbeResult(
                    available=True,
                    browser_version="playwright/chromium",
                    message="Playwright Chromium is installed; no login or remote request was made.",
                )
        except Exception as exc:  # Environment probe must report, not crash.
            return ProbeResult(available=False, message=f"Playwright probe failed: {exc}")

    async def prepare(
        self, archive_path: Path, bundle_manifest: dict[str, Any]
    ) -> PrepareResult:
        raise RuntimeError("Playwright remote execution is not configured")

    async def execute(self, run_id: str) -> ExecuteResult:
        raise RuntimeError("Playwright remote execution is not configured")

    async def status(self, run_id: str) -> RunStatus:
        raise RuntimeError("Playwright remote execution is not configured")
