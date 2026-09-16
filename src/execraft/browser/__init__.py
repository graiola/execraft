"""Browser agent — transport-neutral adapter for browser-based AI interactions."""

from execraft.browser.adapter import (
    ApplyResult,
    BrowserAdapter,
    ExecuteResult,
    PrepareResult,
    ProbeResult,
    RunStatus,
)
from execraft.browser.agent import BrowserAgent
from execraft.browser.fake_adapter import FakeBrowserAdapter
from execraft.browser.playwright_adapter import PlaywrightProbeAdapter

__all__ = [
    "ApplyResult",
    "BrowserAdapter",
    "BrowserAgent",
    "ExecuteResult",
    "FakeBrowserAdapter",
    "PrepareResult",
    "ProbeResult",
    "RunStatus",
]
