"""Host execution capabilities used by workflow stages.

Pool resources describe credentials and callable services.  This registry is
deliberately separate: it answers whether the Bastet control plane can perform
an operation on behalf of a sandboxed Agent.  A binary on PATH is not enough;
the probe must exercise the capability through the same host process that runs
deterministic gates.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class CapabilityStatus:
    capability: str
    available: bool
    provider: str
    detail: str = ""


CATALOG = {
    "browser.playwright": {
        "label": "Playwright Chromium",
        "provider": "bastet-host",
        "description": "由 Bastet 主機執行的可信任瀏覽器測試關卡",
    },
}

# These are infrastructure failures, not evidence that product acceptance
# failed.  Keep the markers browser-specific: a generic EPERM from application
# code can still be a real product defect.
_BROWSER_FAILURE_MARKERS = (
    "crashpad setsockopt",
    "browser has been closed",
    "browser executable doesn't exist",
    "executable doesn't exist at",
    "failed to launch chromium",
    "failed to launch chrome",
    "playwright install",
    "operation not permitted",  # only considered with browser terms below
    "sigtrap",
)


def classify_failure(text: str) -> str:
    """Return a stable infrastructure kind, or an empty string for business failure."""
    lowered = (text or "").lower()
    if "capability_unavailable" in lowered and "browser.playwright" in lowered:
        return "capability_unavailable:browser.playwright"
    browser_context = any(word in lowered for word in
                          ("chrome", "chromium", "playwright", "crashpad", "browser"))
    if browser_context and any(marker in lowered for marker in _BROWSER_FAILURE_MARKERS):
        return "capability_unavailable:browser.playwright"
    return ""


def _probe_browser_playwright(timeout_s: int = 20) -> CapabilityStatus:
    # Run out of process so a native browser crash cannot take down Bastet.
    script = (
        "from playwright.sync_api import sync_playwright\n"
        "with sync_playwright() as p:\n"
        " b=p.chromium.launch(headless=True)\n"
        " page=b.new_page()\n"
        " page.set_content('<title>bastet-capability-probe</title>')\n"
        " assert page.title()=='bastet-capability-probe'\n"
        " b.close()\n"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout_s)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CapabilityStatus("browser.playwright", False, "bastet-host",
                                f"{type(exc).__name__}: {exc}")
    detail = (proc.stdout + proc.stderr).strip()[-1200:]
    return CapabilityStatus(
        "browser.playwright", proc.returncode == 0, "bastet-host",
        detail if proc.returncode else "Chromium launch and page render succeeded")


def probe(capability: str) -> CapabilityStatus:
    if capability == "browser.playwright":
        return _probe_browser_playwright()
    return CapabilityStatus(capability, False, "none",
                            "no execution-capability provider is registered")


def probe_required(required: list[str]) -> list[CapabilityStatus]:
    """Probe every declared capability once, preserving workflow order."""
    return [probe(item) for item in dict.fromkeys(required)]


def catalog() -> list[dict]:
    return [{"id": key, **value} for key, value in CATALOG.items()]
