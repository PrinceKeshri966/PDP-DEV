"""Discover real competitor URLs via Jina search — homepage vs product page aware."""
from __future__ import annotations

import re
from urllib.parse import quote, urlparse

import httpx

from app.agents.claude_client import claude
from app.agents.json_utils import safe_json_parse
from app.agents.model_router import get_model
from app.core.config import get_settings

_LINK_RE = re.compile(r"https?://[^\s\)\]\"'<>]+", re.I)
_SOURCE_RE = re.compile(r"URL Source:\s*(https?://\S+)", re.I)
_SKIP_DOMAINS = {
    "google.com",
    "google.co.in",
    "facebook.com",
    "instagram.com",
    "youtube.com",
    "twitter.com",
    "x.com",
    "pinterest.com",
    "linkedin.com",
    "amazon.com",
    "amazon.in",
    "flipkart.com",
    "wikipedia.org",
}
_PRODUCT_PATH_MARKERS = ("/product", "/products/", "/p/", "/dp/", "/item/", "/buy", "/shop/")


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return ""


def resolve_homepage_mode(user_url: str, compare_as: str | None = None) -> bool:
    """Resolve whether to compare homepages from user preference or URL auto-detect."""
    mode = (compare_as or "auto").lower().strip()
    if mode == "homepage":
        return True
    if mode == "product":
        return False
    return is_homepage_url(user_url)


def is_homepage_url(url: str) -> bool:
    """True when the user URL is a site homepage, not a product/detail page."""
    try:
        raw = url if url.startswith("http") else f"https://{url}"
        u = urlparse(raw)
        path = (u.path or "/").strip().rstrip("/") or "/"
        if path == "/":
            return True
        low = path.lower()
        if any(marker in low for marker in _PRODUCT_PATH_MARKERS):
            return False
        segments = [s for s in path.split("/") if s]
        return len(segments) <= 1
    except Exception:
        return False


def to_site_root(url: str) -> str:
    u = urlparse(url if url.startswith("http") else f"https://{url}")
    scheme = u.scheme or "https"
    netloc = u.netloc
    return f"{scheme}://{netloc}/"


def _is_productish(url: str) -> bool:
    low = url.lower()
    if any(ext in low for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg")):
        return False
    if "nykaa.com/media/" in low or "/media/catalog/" in low:
        return False
    path = urlparse(url).path.lower()
    if path in ("", "/"):
        return False
    if any(x in path for x in _PRODUCT_PATH_MARKERS):
        return True
    return path.count("/") >= 2 and len(path) > 10


def _url_score(url: str, *, homepage_mode: bool) -> int:
    path = urlparse(url).path.lower()
    if homepage_mode:
        if path in ("", "/"):
            return 20
        return -10
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


def _search_queries(
    *,
    homepage_mode: bool,
    product_name: str,
    categories: list[str],
    user_domain: str,
) -> list[str]:
    cat = (categories[0] if categories else "") or "ecommerce"
    name = (product_name or cat).split("|")[0].strip()[:60]
    brand = user_domain.split(".")[0].replace("-", " ").strip() if user_domain else ""
    if homepage_mode:
        queries = [
            f"{cat} brand india official website",
            f"{name} competitors india",
            f"top {cat} companies india online",
        ]
        if brand:
            queries.append(f"brands like {brand} {cat} india")
        return queries
    return [f"buy {name} {cat} online india", f"{name} {cat} india shop"]


async def _discover_via_claude(
    user_url: str,
    product_name: str,
    categories: list[str],
    *,
    homepage_mode: bool,
    limit: int,
    seen_domains: set[str],
    user_domain: str,
) -> list[str]:
    """Fallback when Jina search is unavailable — Claude suggests real competitor URLs."""
    cat = (categories[0] if categories else "") or "ecommerce"
    kind = "homepage root URL" if homepage_mode else "product page URL"
    try:
        response = await claude.messages.create(
            model=get_model("scraper_parser"),
            max_tokens=400,
            system='Return ONLY JSON: {"urls":["https://example.com/", ...]} — no prose.',
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"List up to {limit} direct competitor {kind}s for this business.\n"
                        f"Site: {user_url}\nProduct/brand: {product_name}\nCategory: {cat}\n"
                        f"Exclude domain: {user_domain}\n"
                        f"Use real Indian market competitors with working https URLs."
                    ),
                }
            ],
        )
        parsed = safe_json_parse(response.content[0].text)
        found: list[str] = []
        for raw in parsed.get("urls") or []:
            if not isinstance(raw, str) or not raw.startswith("http"):
                continue
            norm = to_site_root(raw) if homepage_mode else raw.split("#")[0]
            dom = _domain(norm)
            if not dom or dom in seen_domains or dom == user_domain:
                continue
            found.append(norm)
            seen_domains.add(dom)
            if len(found) >= limit:
                break
        return found
    except Exception:
        return []


async def discover_competitor_urls(
    user_url: str,
    product_name: str,
    categories: list[str],
    *,
    existing: list[str] | None = None,
    limit: int = 3,
    homepage_mode: bool | None = None,
) -> list[str]:
    settings = get_settings()
    user_domain = _domain(user_url)
    if homepage_mode is None:
        homepage_mode = is_homepage_url(user_url)

    found: list[str] = []
    seen_domains: set[str] = {user_domain}

    for u in existing or []:
        norm = to_site_root(u) if homepage_mode else u.split("#")[0]
        d = _domain(norm)
        if d and d not in seen_domains:
            found.append(norm)
            seen_domains.add(d)
        if len(found) >= limit:
            return found[:limit]

    headers: dict[str, str] = {"Accept": "text/plain", "User-Agent": "Organic360/1.0"}
    if settings.jina_api_key.strip():
        headers["Authorization"] = f"Bearer {settings.jina_api_key.strip()}"

    queries = _search_queries(
        homepage_mode=homepage_mode,
        product_name=product_name,
        categories=categories,
        user_domain=user_domain,
    )

    try:
        async with httpx.AsyncClient(timeout=55.0, follow_redirects=True) as client:
            for query in queries:
                if len(found) >= limit:
                    break
                resp = await client.get(f"https://s.jina.ai/{quote(query)}", headers=headers)
                resp.raise_for_status()
                candidates = sorted(
                    _extract_urls(resp.text),
                    key=lambda u: _url_score(u, homepage_mode=homepage_mode),
                    reverse=True,
                )
                for url in candidates:
                    dom = _domain(url)
                    if not dom or dom in seen_domains or dom == user_domain:
                        continue
                    if any(dom == s or dom.endswith("." + s) for s in _SKIP_DOMAINS):
                        continue
                    if homepage_mode:
                        norm = to_site_root(url)
                    else:
                        if not _is_productish(url):
                            continue
                        norm = url.split("#")[0]
                    found.append(norm)
                    seen_domains.add(dom)
                    if len(found) >= limit:
                        break
    except Exception:
        pass

    if len(found) < limit:
        claude_urls = await _discover_via_claude(
            user_url,
            product_name,
            categories,
            homepage_mode=homepage_mode,
            limit=limit - len(found),
            seen_domains=seen_domains,
            user_domain=user_domain,
        )
        found.extend(claude_urls)

    return found[:limit]


async def discover_replacement_urls(
    user_url: str,
    product_name: str,
    categories: list[str],
    *,
    seen_domains: set[str],
    homepage_mode: bool,
    limit: int = 3,
) -> list[str]:
    """Extra competitor URLs when live scrape fails — excludes already-tried domains."""
    return await _discover_via_claude(
        user_url,
        product_name,
        categories,
        homepage_mode=homepage_mode,
        limit=limit,
        seen_domains=seen_domains,
        user_domain=_domain(user_url),
    )
