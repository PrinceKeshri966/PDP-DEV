"""Lightweight page fetch for competitor URLs (Jina → HTTP → Playwright)."""
from __future__ import annotations

import base64
import re
from html import unescape
from typing import Any

import httpx

from app.core.config import get_settings
from app.core.playwright_env import playwright_enabled

_settings = get_settings()
_JINA_BASE = "https://r.jina.ai/"
_MIN_CHARS = 200
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _html_to_text(html: str) -> str:
    html = re.sub(r"<script[^>]*>[\s\S]*?</script>", " ", html, flags=re.I)
    html = re.sub(r"<style[^>]*>[\s\S]*?</style>", " ", html, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", unescape(text)).strip()[:80_000]


async def _fetch_with_playwright(url: str) -> tuple[str | None, str | None]:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=_UA, viewport={"width": 1366, "height": 900})
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=25000)
            content: str = await page.evaluate("""() => {
                const el = document.querySelector('main') || document.body;
                return el.innerText;
            }""")
            text = content.strip()
            screenshot_b64: str | None = None
            try:
                png = await page.screenshot(full_page=False, type="png")
                screenshot_b64 = base64.b64encode(png).decode("ascii")
            except Exception:
                pass
            if len(text) >= _MIN_CHARS:
                return text, screenshot_b64
            return None, screenshot_b64
        finally:
            await context.close()
            await browser.close()


async def fetch_page_markdown(url: str) -> str | None:
    result = await fetch_page_live(url)
    return result.get("markdown") if result.get("scrape_ok") else None


async def fetch_page_live(url: str) -> dict[str, Any]:
    """Fetch page text + optional viewport screenshot for live competitor compare."""
    headers: dict[str, str] = {
        "Accept": "text/markdown",
        "X-Return-Format": "markdown",
        "User-Agent": _UA,
    }
    if _settings.jina_api_key:
        headers["Authorization"] = f"Bearer {_settings.jina_api_key.strip()}"
    try:
        async with httpx.AsyncClient(timeout=45.0, follow_redirects=True) as client:
            resp = await client.get(f"{_JINA_BASE}{url}", headers=headers)
            resp.raise_for_status()
            text = resp.text.strip()
            if len(text) >= _MIN_CHARS:
                return {"markdown": text, "screenshot_base64": None, "scrape_ok": True}
    except Exception:
        pass
    try:
        async with httpx.AsyncClient(timeout=35.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": _UA, "Accept": "text/html"})
            resp.raise_for_status()
            text = _html_to_text(resp.text)
            if len(text) >= _MIN_CHARS:
                return {"markdown": text, "screenshot_base64": None, "scrape_ok": True}
    except Exception:
        pass
    if playwright_enabled():
        try:
            text, screenshot_b64 = await _fetch_with_playwright(url)
            if text:
                return {"markdown": text, "screenshot_base64": screenshot_b64, "scrape_ok": True}
            if screenshot_b64:
                return {"markdown": None, "screenshot_base64": screenshot_b64, "scrape_ok": False}
        except Exception:
            pass
    return {"markdown": None, "screenshot_base64": None, "scrape_ok": False}
