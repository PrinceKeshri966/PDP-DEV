"""
app/agents/scraper_agent.py

ScraperAgent  (Mode 1 – Node 1)
────────────────────────────────
Cloud-safe scrape chain (no Playwright required on Render):

  1. Jina Reader (markdown, best for most URLs)
  2. Direct HTTP fetch + HTML → text (works when Jina times out / blocks)
  3. Playwright (optional — local dev only if browsers installed)

Set SKIP_PLAYWRIGHT=true on Render (default in render.yaml).
"""
from __future__ import annotations

import os
import re
import time
from html import unescape

import httpx

from app.agents.state import AgentState
from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)
_settings = get_settings()

_JINA_BASE = "https://r.jina.ai/"
_JINA_THIN_THRESHOLD = 500
_MIN_USABLE_CHARS = 200
_MAX_CONTENT_CHARS = 80_000
_HTTP_TIMEOUT = 45.0
_JINA_TIMEOUT = 60.0

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _playwright_enabled() -> bool:
    """Playwright is off on Render unless explicitly enabled."""
    return os.getenv("SKIP_PLAYWRIGHT", "true").lower() not in (
        "1",
        "true",
        "yes",
    )


def _html_to_text(html: str) -> str:
    """Strip tags/scripts and normalize whitespace."""
    html = re.sub(r"<script[^>]*>[\s\S]*?</script>", " ", html, flags=re.I)
    html = re.sub(r"<style[^>]*>[\s\S]*?</style>", " ", html, flags=re.I)
    html = re.sub(r"<noscript[^>]*>[\s\S]*?</noscript>", " ", html, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", html)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:_MAX_CONTENT_CHARS]


async def _fetch_with_jina(url: str) -> str:
    jina_url = f"{_JINA_BASE}{url}"
    headers: dict[str, str] = {
        "Accept": "text/markdown",
        "X-Return-Format": "markdown",
        "User-Agent": _BROWSER_UA,
    }
    if _settings.jina_api_key:
        headers["Authorization"] = f"Bearer {_settings.jina_api_key}"

    async with httpx.AsyncClient(timeout=_JINA_TIMEOUT, follow_redirects=True) as client:
        resp = await client.get(jina_url, headers=headers)
        resp.raise_for_status()
        return resp.text.strip()


async def _fetch_with_httpx(url: str) -> str:
    """Direct fetch — works on Render without Playwright browsers."""
    async with httpx.AsyncClient(
        timeout=_HTTP_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": _BROWSER_UA, "Accept": "text/html,application/xhtml+xml"},
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        body = resp.text
        if len(body) > 500 and ("<html" in body.lower() or "<body" in body.lower()):
            return _html_to_text(body)
        return body.strip()[:_MAX_CONTENT_CHARS]


async def _fetch_with_playwright(url: str) -> str:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=_BROWSER_UA)
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=25000)
            try:
                await page.wait_for_selector(
                    "main, article, [data-product], .product-detail, .product",
                    timeout=5000,
                )
            except Exception:
                pass
            content: str = await page.evaluate("""() => {
                const el = (
                    document.querySelector('main') ||
                    document.querySelector('article') ||
                    document.querySelector('[data-product]') ||
                    document.querySelector('.product') ||
                    document.body
                );
                return el.innerText;
            }""")
            return content[:_MAX_CONTENT_CHARS]
        finally:
            await context.close()
            await browser.close()


async def _try_fetch(label: str, fetcher, url: str) -> tuple[str | None, str | None]:
    try:
        text = await fetcher(url)
        if text and len(text.strip()) >= _MIN_USABLE_CHARS:
            return text.strip(), None
        return None, f"{label}: content too short ({len(text or '')} chars)"
    except Exception as exc:
        return None, f"{label}: {exc}"


# ── LangGraph node ────────────────────────────────────────────────────────────
async def scraper_agent(state: AgentState) -> AgentState:
    """
    Scrape product/page URL. Succeeds if any method returns enough text.
    """
    url = (state.get("url") or "").strip()
    if not url:
        state["errors"] = state.get("errors", []) + ["scraper_agent: no URL provided"]
        state["status"] = "failed"
        return state

    logger.info("scraper_agent.start", url=url, skip_playwright=not _playwright_enabled())
    t0 = time.monotonic()
    attempt_errors: list[str] = []
    content = ""
    method = "none"

    # 1 — Jina Reader
    text, err = await _try_fetch("jina", _fetch_with_jina, url)
    if err:
        attempt_errors.append(err)
    if text:
        content, method = text, "jina"

    # 2 — Direct HTTP (upgrade if Jina thin/missing)
    if len(content) < _JINA_THIN_THRESHOLD:
        text, err = await _try_fetch("httpx", _fetch_with_httpx, url)
        if err:
            attempt_errors.append(err)
        if text and len(text) > len(content):
            content, method = text, "httpx"

    # 3 — Playwright (local dev only)
    if _playwright_enabled() and len(content) < _JINA_THIN_THRESHOLD:
        text, err = await _try_fetch("playwright", _fetch_with_playwright, url)
        if err:
            attempt_errors.append(err)
        if text and len(text) > len(content):
            content, method = text, "playwright"

    duration_ms = int((time.monotonic() - t0) * 1000)

    if len(content) < _MIN_USABLE_CHARS:
        summary = (
            "scraper_agent: could not fetch enough page content. "
            f"Tried jina, httpx"
            + (", playwright" if _playwright_enabled() else "")
            + f". Details: {' | '.join(attempt_errors[:3])}"
        )
        logger.error("scraper_agent.all_failed", url=url, errors=attempt_errors)
        state["errors"] = state.get("errors", []) + [summary]
        state["status"] = "failed"
        return state

    logger.info("scraper_agent.done", method=method, chars=len(content), duration_ms=duration_ms)

    state["markdown_content"] = content
    state["scraper_method"] = method
    state["agent_reports"] = state.get("agent_reports", []) + [
        {
            "agent": "scraper_agent",
            "model": f"scraper/{method}",
            "input": {"url": url},
            "output_preview": content[:500],
            "output_chars": len(content),
            "scraper_method": method,
            "duration_ms": duration_ms,
            "fallback_errors": attempt_errors[:5] if attempt_errors else [],
        }
    ]
    return state
