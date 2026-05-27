"""Discover real competitor PDP URLs via Jina search."""
from __future__ import annotations

import re
from urllib.parse import quote, urlparse

import httpx

from app.core.config import get_settings

_LINK_RE = re.compile(r"https?://[^\s\)\]\"'<>]+", re.I)
_SOURCE_RE = re.compile(r"URL Source:\s*(https?://\S+)", re.I)
_SKIP_DOMAINS = {
    "google.com", "google.co.in", "facebook.com", "instagram.com",
    "youtube.com", "twitter.com", "x.com", "pinterest.com", "linkedin.com",
    "amazon.com", "amazon.in", "flipkart.com", "wikipedia.org",
}


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return ""


def _is_productish(url: str) -> bool:
    low = url.lower()
    if any(ext in low for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg")):
        return False
    if "nykaa.com/media/" in low or "/media/catalog/" in low:
        return False
    path = urlparse(url).path.lower()
    if path in ("", "/"):
        return False
    if any(x in path for x in ("/product", "/products/", "/p/", "/dp/", "/item/", "/shop/", "/buy")):
        return True
    return path.count("/") >= 2 and len(path) > 10


def _url_score(url: str) -> int:
    path = urlparse(url).path.lower()
    score = 0
    if "/products/" in path or "/product/" in path:
        score += 20
    if "/dp/" in path or "/p/" in path:
        score += 15
    if "/collections/" in path or "/search" in path or "/cart" in path:
        score -= 10
    return score


def _extract_urls(text: str) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for pattern in (_SOURCE_RE, _LINK_RE):
        for match in pattern.finditer(text):
            url = match.group(1 if pattern is _SOURCE_RE else 0).rstrip(".,;)")
            if url not in seen:
                seen.add(url)
                ordered.append(url)
    return ordered


async def discover_competitor_urls(
    user_url: str,
    product_name: str,
    categories: list[str],
    *,
    existing: list[str] | None = None,
    limit: int = 3,
) -> list[str]:
    settings = get_settings()
    user_domain = _domain(user_url)
    found: list[str] = []
    seen_domains: set[str] = {user_domain}

    for u in existing or []:
        d = _domain(u)
        if d and d not in seen_domains:
            found.append(u)
            seen_domains.add(d)
        if len(found) >= limit:
            return found[:limit]

    cat = (categories[-1] if categories else "") or "product"
    name = (product_name or cat).split("|")[0].strip()[:60]
    query = f"buy {name} {cat} online india"
    headers: dict[str, str] = {"Accept": "text/plain", "User-Agent": "OptiPDP/1.0"}
    if settings.jina_api_key.strip():
        headers["Authorization"] = f"Bearer {settings.jina_api_key.strip()}"

    try:
        async with httpx.AsyncClient(timeout=55.0, follow_redirects=True) as client:
            resp = await client.get(f"https://s.jina.ai/{quote(query)}", headers=headers)
            resp.raise_for_status()
            candidates = sorted(
                _extract_urls(resp.text),
                key=_url_score,
                reverse=True,
            )
            for url in candidates:
                dom = _domain(url)
                if not dom or dom in seen_domains or dom == user_domain:
                    continue
                if any(dom == s or dom.endswith("." + s) for s in _SKIP_DOMAINS):
                    continue
                if not _is_productish(url):
                    continue
                found.append(url.split("#")[0])
                seen_domains.add(dom)
                if len(found) >= limit:
                    break
    except Exception:
        pass

    return found[:limit]
