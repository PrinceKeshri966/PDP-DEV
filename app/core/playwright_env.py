"""Whether Playwright (browser) features are enabled on this deployment."""
from __future__ import annotations


def playwright_enabled() -> bool:
    """Hardcoded on for local dev — Jina/HTTP fall back if Chromium is unavailable."""
    return True
