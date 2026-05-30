"""
app/agents/seo_agent.py

SEOAgent  (Mode 1 – Node 2)   Model: Claude Haiku (fast)
──────────────────────────────────────────────────────────
Receives the raw Markdown from ScraperAgent, extracts structured
SEO signals, and stores a JSON report in state["seo_report"].

Uses the Anthropic SDK directly (AsyncAnthropic.messages.create).
"""
from __future__ import annotations

import json
import time

from app.agents.claude_client import claude
from app.agents.json_utils import safe_json_parse_report
from app.agents.model_router import get_model
from app.agents.state import AgentState, state_dict
from app.core.logging import get_logger

logger = get_logger(__name__)

_MODEL = get_model("seo")


def _seo_value_missing(value: object) -> bool:
    if value is None:
        return True
    text = str(value).strip()
    return not text or "not provided" in text.lower()


def _apply_dom_seo_recovery(seo_report: dict[str, Any], dom: dict[str, Any]) -> None:
    """Map scraped head metadata into seo_report when the LLM could not read tags from markdown."""
    if not dom:
        return
    if dom.get("meta_description"):
        block = seo_report.get("meta_description") or {}
        if _seo_value_missing(block.get("value")):
            val = str(dom["meta_description"]).strip()
            seo_report["meta_description"] = {**block, "value": val, "length": len(val)}
    if dom.get("title_tag"):
        block = seo_report.get("title_tag") or {}
        if _seo_value_missing(block.get("value")):
            val = str(dom["title_tag"]).strip()
            seo_report["title_tag"] = {**block, "value": val, "length": len(val)}


_SYSTEM_PROMPT = """
You are an expert e-commerce SEO analyst following Google Search Essentials and
Semrush/Ahrefs industry standards.
Analyse the provided product page Markdown and return ONLY a valid JSON object.
No prose, no markdown fences, no explanation – raw JSON only.

Required JSON schema:
{
  "title_tag": {
    "value": string,
    "length": int,
    "score": float (0-10),
    "issues": [string]
  },
  "meta_description": {
    "value": string,
    "length": int,
    "has_cta": boolean,
    "score": float (0-10),
    "issues": [string]
  },
  "h1": {
    "value": string,
    "count": int,
    "score": float (0-10),
    "issues": [string]
  },
  "headings_structure": {
    "h2_count": int,
    "h3_count": int,
    "logical_hierarchy": boolean,
    "keyword_in_headings": boolean,
    "score": float (0-10)
  },
  "keyword_analysis": {
    "primary_keyword": string,
    "secondary_keywords": [string],
    "density_pct": float,
    "in_title": boolean,
    "in_h1": boolean,
    "in_meta_description": boolean,
    "in_first_100_words": boolean,
    "score": float (0-10)
  },
  "content_quality": {
    "word_count": int,
    "readability": "poor|average|good|excellent",
    "thin_content": boolean,
    "duplicate_content_risk": boolean,
    "score": float (0-10)
  },
  "image_seo": {
    "total_images": int,
    "missing_alt": int,
    "descriptive_alt": int,
    "score": float (0-10)
  },
  "structured_data": {
    "detected": boolean,
    "types": [string],
    "has_product_schema": boolean,
    "has_review_schema": boolean,
    "has_breadcrumb_schema": boolean,
    "score": float (0-10)
  },
  "links": {
    "internal_count": int,
    "external_count": int,
    "broken_links_risk": boolean,
    "score": float (0-10)
  },
  "technical_seo": {
    "canonical_present": boolean,
    "open_graph_present": boolean,
    "twitter_card_present": boolean,
    "hreflang_present": boolean,
    "mobile_friendly": boolean,
    "core_web_vitals_risk": "low|medium|high",
    "page_speed_signals": {
      "large_images_detected": boolean,
      "render_blocking_scripts": boolean,
      "lazy_loading_used": boolean,
      "estimated_lcp_risk": "low|medium|high",
      "estimated_cls_risk": "low|medium|high"
    },
    "pagination_signals": boolean,
    "score": float (0-10)
  },
  "url_structure": {
    "is_seo_friendly": boolean,
    "has_keyword": boolean,
    "issues": [string]
  },
  "overall_seo_score": float (0-10),
  "top_issues": [string],
  "quick_wins": [string]
}
""".strip()


async def seo_agent(state: AgentState) -> AgentState:
    """Analyse Markdown for SEO and append structured report to state."""
    markdown = state.get("markdown_content", "")
    if not markdown:
        return {"errors": ["seo_agent: no markdown_content"]}

    structured = state_dict(state, "json_structured_data")

    logger.info("seo_agent.start", model=_MODEL, chars=len(markdown))
    t0 = time.monotonic()

    user_message = (
        f"Product structured data:\n{json.dumps(structured, indent=2)}\n\n"
        f"Raw page Markdown (for tag/heading analysis):\n{markdown[:6000]}"
    )

    response = await claude.messages.create(
        model=_MODEL,
        max_tokens=4096,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    raw = response.content[0].text.strip()
    duration_ms = int((time.monotonic() - t0) * 1000)

    seo_report, parse_err = safe_json_parse_report(raw, "seo_agent")
    if parse_err:
        return {"errors": [parse_err]}

    dom = state.get("dom_technical_seo") or structured.get("_dom_technical_seo") or {}
    if isinstance(dom, dict):
        _apply_dom_seo_recovery(seo_report, dom)

    logger.info(
        "seo_agent.done",
        score=seo_report.get("overall_seo_score"),
        duration_ms=duration_ms,
    )

    return {
        "seo_report": seo_report,
        "agent_reports": [
            {
                "agent": "seo_agent",
                "model": _MODEL,
                "input": {"markdown_chars": len(markdown), "markdown_preview": markdown[:400]},
                "output": seo_report,
                "duration_ms": duration_ms,
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            }
        ],
    }
