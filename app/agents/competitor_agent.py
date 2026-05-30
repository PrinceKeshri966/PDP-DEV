"""

CompetitorAgent — hybrid benchmark intelligence + optional live side-by-side compare.



  1. Claude knowledge benchmark (always — gaps, scores, market positioning)

  2. Live competitor discovery + scrape when URLs are found (Jina → HTTP → Playwright)

  3. Merges live_compare matrix into the Claude report when scrapes succeed

"""

from __future__ import annotations



import time

from typing import Any

from urllib.parse import urlparse



from app.agents.claude_client import claude

from app.agents.competitor_discovery import discover_competitor_urls, discover_replacement_urls, resolve_homepage_mode

from app.agents.json_utils import safe_json_parse_report

from app.agents.model_router import get_model

from app.agents.page_features import (

    build_comparison_matrix,

    features_from_markdown,

    features_from_structured,

    gaps_from_matrix,

)

from app.agents.page_fetch import fetch_page_live

from app.agents.state import AgentState, state_dict

from app.core.logging import get_logger



logger = get_logger(__name__)



_MODEL = get_model("seo")



_SYSTEM_PROMPT = """

You are an expert e-commerce competitor intelligence analyst following

Semrush and Ahrefs competitive analysis frameworks.

Analyze the product and its competitive landscape.



Return ONLY a valid JSON object — no prose, no markdown fences.



Required JSON schema:

{

  "competitors_analyzed": [string],

  "data_source": "live_scrape|claude_knowledge",

  "market_positioning": {

    "price_tier": "budget|mid-range|premium|luxury",

    "price_positioning_index": float,

    "target_segment": string,

    "differentiation": string,

    "market_maturity": "emerging|growing|mature|declining"

  },

  "benchmark_scores": {

    "avg_seo_score": float (0-10),

    "avg_ai_visibility_score": float (0-10),

    "avg_conversion_score": float (0-10),

    "avg_content_depth_score": float (0-10)

  },

  "feature_comparison": {

    "product_images_avg": int,

    "description_word_count_avg": int,

    "has_video_pct": float,

    "has_size_guide_pct": float,

    "has_reviews_pct": float,

    "avg_review_count": int

  },

  "share_of_voice": {

    "estimated_keyword_overlap_pct": float,

    "top_shared_keywords": [string],

    "your_unique_keywords": [string]

  },

  "traffic_estimate": {

    "your_tier": "low|medium|high",

    "competitor_avg_tier": "low|medium|high",

    "gap_assessment": string

  },

  "backlink_gap": {

    "your_authority_estimate": "low|medium|high",

    "competitor_avg_authority": "low|medium|high",

    "recommendation": string

  },

  "your_gaps_vs_competitors": [string],

  "winning_patterns": [string],

  "opportunities": [string],

  "category_best_practices": [string],

  "first_mover_opportunities": [string]

}

""".strip()





def _site_label(url: str) -> str:

    try:

        return urlparse(url).netloc.replace("www.", "")

    except Exception:

        return url[:40]





def _site_entry(

    *,

    role: str,

    name: str,

    url: str,

    page_type: str,

    scrape_ok: bool,

    features: dict[str, Any],

    screenshot_base64: str | None = None,

) -> dict[str, Any]:

    entry: dict[str, Any] = {

        "role": role,

        "name": name,

        "url": url,

        "page_type": page_type,

        "scrape_ok": scrape_ok,

        "features": features,

    }

    if screenshot_base64:

        entry["screenshot_base64"] = screenshot_base64

    return entry





def _avg_feature(sites: list[dict[str, Any]], key: str) -> float | None:

    vals = [s["features"].get(key) for s in sites[1:] if s.get("scrape_ok") and s.get("features")]

    nums = [float(v) for v in vals if isinstance(v, (int, float)) and v is not None]

    return round(sum(nums) / len(nums), 1) if nums else None





async def _scrape_one(

    url: str,

    *,

    role: str,

    page_type: str,

    fallback_features: dict[str, Any] | None = None,

) -> tuple[dict[str, Any], str]:

    """Live-scrape a URL; return site object + optional context snippet for Claude."""

    name = "Your site" if role == "you" else _site_label(url)

    fetched = await fetch_page_live(url)

    markdown = fetched.get("markdown") or ""

    screenshot = fetched.get("screenshot_base64")

    if fetched.get("scrape_ok") and markdown:

        features = features_from_markdown(markdown, url)

        site = _site_entry(

            role=role,

            name=name,

            url=url,

            page_type=page_type,

            scrape_ok=True,

            features=features,

            screenshot_base64=screenshot,

        )

        ctx = f"\n\n{'Your page' if role == 'you' else 'Competitor URL'}: {url}\n{markdown[:3000]}"

        return site, ctx

    features = fallback_features if role == "you" and fallback_features else {}

    site = _site_entry(

        role=role,

        name=name,

        url=url,

        page_type=page_type,

        scrape_ok=False,

        features=features,

        screenshot_base64=screenshot,

    )

    return site, ""





async def _build_live_compare(
    *,
    user_url: str,
    structured: dict,
    competitor_urls: list[str],
    compare_as: str,
) -> tuple[dict[str, Any] | None, str, str]:
    """Discover + scrape user + up to 3 competitors; replace failed scrapes with similar sites."""
    COMPETITOR_TARGET = 3
    CANDIDATE_POOL = 8

    homepage_mode = resolve_homepage_mode(user_url, compare_as)
    compare_page_type = "homepage" if homepage_mode else "product"

    candidates = await discover_competitor_urls(
        user_url,
        structured.get("product_name") or "",
        structured.get("categories") or [],
        existing=competitor_urls,
        limit=CANDIDATE_POOL,
        homepage_mode=homepage_mode,
    )
    logger.info("competitor_agent.discovered", count=len(candidates), urls=candidates)
    if not candidates:
        return None, "", "claude_knowledge"

    you_site, you_ctx = await _scrape_one(
        user_url,
        role="you",
        page_type=compare_page_type,
        fallback_features=features_from_structured(structured),
    )

    competitor_sites: list[dict[str, Any]] = []
    scraped_context = you_ctx
    tried_domains: set[str] = {_site_label(user_url)}
    candidate_queue = list(candidates)

    async def _try_fill_from_queue() -> None:
        nonlocal scraped_context
        while len(competitor_sites) < COMPETITOR_TARGET and candidate_queue:
            url = candidate_queue.pop(0)
            dom = _site_label(url)
            if dom in tried_domains:
                continue
            tried_domains.add(dom)
            comp_site, comp_ctx = await _scrape_one(url, role="competitor", page_type=compare_page_type)
            if comp_site.get("scrape_ok"):
                competitor_sites.append(comp_site)
                scraped_context += comp_ctx

    await _try_fill_from_queue()

    while len(competitor_sites) < COMPETITOR_TARGET:
        seen = set(tried_domains)
        seen.add(_site_label(user_url))
        extras = await discover_replacement_urls(
            user_url,
            structured.get("product_name") or "",
            structured.get("categories") or [],
            seen_domains=seen,
            homepage_mode=homepage_mode,
            limit=COMPETITOR_TARGET - len(competitor_sites) + 2,
        )
        if not extras:
            break
        candidate_queue.extend(extras)
        before = len(competitor_sites)
        await _try_fill_from_queue()
        if len(competitor_sites) == before:
            break

    if not competitor_sites:
        return None, "", "claude_knowledge"

    sites: list[dict[str, Any]] = [you_site, *competitor_sites[:COMPETITOR_TARGET]]
    comp_scraped = len(competitor_sites)

    ok_sites = [s for s in sites if s.get("scrape_ok")]
    rows = build_comparison_matrix(ok_sites, page_type=compare_page_type)
    gaps = gaps_from_matrix(ok_sites, rows) if ok_sites else []
    wins = [f"You lead on {r['label']}" for r in rows if r.get("you_win")]

    live_compare = {
        "compare_as": compare_as,
        "compare_page_type": compare_page_type,
        "sites": sites,
        "rows": rows,
        "your_gaps_vs_competitors": gaps,
        "winning_patterns": wins,
    }

    if you_site.get("scrape_ok") and comp_scraped >= 1:
        data_source = "live_scrape"
    elif comp_scraped >= 1:
        data_source = "partial"
    else:
        data_source = "claude_knowledge"

    return live_compare, scraped_context if comp_scraped else "", data_source


async def competitor_agent(state: AgentState) -> AgentState:

    structured = state_dict(state, "json_structured_data")

    if not structured:

        return {"errors": ["competitor_agent: no json_structured_data"]}



    user_url = (state.get("url") or structured.get("product_url") or "").strip()

    competitor_urls = state.get("competitor_urls") or []

    compare_as = (state.get("compare_as") or "auto").lower().strip()

    t0 = time.monotonic()



    live_compare, scraped_context, live_data_source = await _build_live_compare(

        user_url=user_url,

        structured=structured,

        competitor_urls=competitor_urls,

        compare_as=compare_as,

    )



    if scraped_context:

        user_message = f"""

Product being analyzed:

{structured}



Live competitor data:

{scraped_context}



Benchmark this product against the scraped competitors.

Identify gaps, winning patterns, and opportunities.

""".strip()

    else:

        user_message = f"""

Product being analyzed:

{structured}



No live competitor data available. Use your knowledge of the industry

to benchmark this product against typical competitors in its category.

Identify gaps, winning patterns, and opportunities based on industry standards.

""".strip()



    response = await claude.messages.create(

        model=_MODEL,

        max_tokens=2048,

        system=_SYSTEM_PROMPT,

        messages=[{"role": "user", "content": user_message}],

    )



    raw = response.content[0].text.strip()

    competitor_report, parse_err = safe_json_parse_report(raw, "competitor_agent")

    if parse_err:

        return {"errors": [parse_err]}



    competitor_report["data_source"] = live_data_source if live_compare else "claude_knowledge"



    if live_compare:

        competitor_report["live_compare"] = live_compare

        if live_compare.get("rows"):

            competitor_report.setdefault("your_gaps_vs_competitors", live_compare.get("your_gaps_vs_competitors", []))

            competitor_report.setdefault("winning_patterns", live_compare.get("winning_patterns", []))

        sites = live_compare.get("sites") or []

        scraped = sum(1 for s in sites if s.get("role") == "competitor" and s.get("scrape_ok"))

        if scraped:

            competitor_report["feature_comparison"] = {

                "product_images_avg": _avg_feature(sites, "images_count"),

                "description_word_count_avg": _avg_feature(sites, "page_word_count"),

                "has_video_pct": round(

                    100

                    * sum(

                        1

                        for s in sites[1:]

                        if s.get("scrape_ok") and s["features"].get("has_video")

                    )

                    / max(scraped, 1),

                    0,

                ),

                "has_size_guide_pct": round(

                    100

                    * sum(

                        1

                        for s in sites[1:]

                        if s.get("scrape_ok") and s["features"].get("has_size_guide")

                    )

                    / max(scraped, 1),

                    0,

                ),

                "has_reviews_pct": round(

                    100

                    * sum(

                        1

                        for s in sites[1:]

                        if s.get("scrape_ok") and s["features"].get("has_reviews")

                    )

                    / max(scraped, 1),

                    0,

                ),

                "avg_review_count": _avg_feature(sites, "review_count"),

            }

        names = [_site_label(s["url"]) for s in sites if s.get("role") == "competitor"]

        if names:

            competitor_report["competitors_analyzed"] = names



    duration_ms = int((time.monotonic() - t0) * 1000)

    logger.info(

        "competitor_agent.done",

        source=competitor_report.get("data_source"),

        live_sites=len((live_compare or {}).get("sites") or []),

        duration_ms=duration_ms,

    )



    return {

        "competitor_report": competitor_report,

        "agent_reports": [

            {

                "agent": "competitor_agent",

                "model": _MODEL,

                "output": competitor_report,

                "duration_ms": duration_ms,

                "input_tokens": response.usage.input_tokens,

                "output_tokens": response.usage.output_tokens,

            }

        ],

    }


