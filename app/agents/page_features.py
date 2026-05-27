"""Extract comparable PDP features from structured data or scraped text."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return ""


def features_from_structured(data: dict[str, Any]) -> dict[str, Any]:
    images = data.get("image_urls") or []
    return {
        "product_name": data.get("product_name") or "Your product",
        "images_count": int(data.get("images_count") or len(images) or 0),
        "has_video": bool(data.get("has_video")),
        "has_reviews": bool(data.get("has_reviews")),
        "review_count": data.get("review_count"),
        "avg_rating": data.get("avg_rating"),
        "page_word_count": int(data.get("page_word_count") or 0),
        "has_size_guide": bool(data.get("has_size_guide")),
        "has_return_policy": bool(data.get("return_policy")),
        "price": data.get("price"),
        "description_len": len((data.get("description") or "")),
    }


def features_from_markdown(markdown: str, url: str = "") -> dict[str, Any]:
    text = markdown.lower()
    title_match = re.search(r"^#\s+(.+)$", markdown, re.M)
    images = len(re.findall(r"!\[[^\]]*\]\([^)]+\)|https?://[^\s)\]]+\.(?:jpg|jpeg|png|webp|gif)", markdown, re.I))
    reviews = bool(re.search(r"\breviews?\b|\bratings?\b|\d+\s*reviews?|\d\.\d\s*/\s*5|★|⭐", text))
    rc = re.search(r"(\d[\d,]*)\s*reviews?", text)
    rating = re.search(r"(\d\.\d)\s*/\s*5|(\d\.\d)\s*out of\s*5", text)
    return {
        "product_name": (title_match.group(1).strip() if title_match else _domain(url) or "Competitor"),
        "images_count": images,
        "has_video": bool(re.search(r"\bvideo\b|youtube|vimeo|\.mp4", text)),
        "has_reviews": reviews,
        "review_count": int(rc.group(1).replace(",", "")) if rc else None,
        "avg_rating": float(rating.group(1) or rating.group(2)) if rating else None,
        "page_word_count": len(markdown.split()),
        "has_size_guide": bool(re.search(r"size guide|size chart|find your size", text)),
        "has_return_policy": bool(re.search(r"return policy|easy returns|days return", text)),
        "price": _first_price(text),
        "description_len": len(markdown),
    }


def _first_price(text: str) -> str | None:
    m = re.search(r"(?:₹|rs\.?|inr)\s*[\d,]+(?:\.\d{2})?|\$\s*[\d,]+(?:\.\d{2})?", text, re.I)
    return m.group(0).strip() if m else None


_COMPARE_ROWS = (
    ("Product images", "images_count", "higher"),
    ("Page content (words)", "page_word_count", "higher"),
    ("Customer reviews", "has_reviews", "bool"),
    ("Review count", "review_count", "higher"),
    ("Product video", "has_video", "bool"),
    ("Size guide", "has_size_guide", "bool"),
    ("Return policy", "has_return_policy", "bool"),
)


def build_comparison_matrix(sites: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for label, key, mode in _COMPARE_ROWS:
        values = [s.get("features", {}).get(key) for s in sites]
        if all(v in (None, 0, False, "") for v in values):
            continue
        best_idx = _best_index(values, mode)
        rows.append(
            {
                "label": label,
                "key": key,
                "values": values,
                "best_index": best_idx,
                "you_win": best_idx == 0,
            }
        )
    return rows


def _best_index(values: list[Any], mode: str) -> int:
    if mode == "bool":
        truthy = [i for i, v in enumerate(values) if v]
        return truthy[0] if len(truthy) == 1 else (0 if values[0] else (truthy[0] if truthy else 0))
    numeric = []
    for i, v in enumerate(values):
        try:
            numeric.append((float(v) if v is not None else -1, i))
        except (TypeError, ValueError):
            numeric.append((-1, i))
    return max(numeric, key=lambda x: x[0])[1]


def gaps_from_matrix(sites: list[dict[str, Any]], rows: list[dict[str, Any]]) -> list[str]:
    gaps: list[str] = []
    you = sites[0].get("features", {}) if sites else {}
    for row in rows:
        if row.get("you_win"):
            continue
        key = row["key"]
        label = row["label"]
        best_i = row.get("best_index", 0)
        if best_i == 0 or best_i >= len(sites):
            continue
        comp = sites[best_i]
        comp_name = _domain(comp.get("url", "")) or comp.get("name", "Competitor")
        yv, cv = you.get(key), comp.get("features", {}).get(key)
        if row["key"] in ("has_reviews", "has_video", "has_size_guide", "has_return_policy"):
            if not yv and cv:
                gaps.append(f"{label}: missing on your page — {comp_name} has it")
        elif isinstance(yv, (int, float)) and isinstance(cv, (int, float)) and cv > yv:
            gaps.append(f"{label}: you have {yv}, {comp_name} has {cv}")
    return gaps[:8]
