"""
app/agents/extractor_agent.py

ExtractorAgent  (Mode 1 – Node 2)   Model: Claude Haiku (fast)
───────────────────────────────────────────────────────────────
Sits between ScraperAgent and SEOAgent.
Takes raw markdown/text (which may contain nav, footer, ads) and
uses Haiku to extract a clean structured JSON of the product data.

This prevents SEO and AutoFix agents from hallucinating on noisy input.

LangGraph signature:  async (state: AgentState) -> AgentState
"""
from __future__ import annotations

import time

from app.agents.claude_client import claude
from app.agents.json_utils import safe_json_parse_report
from app.agents.model_router import get_model
from app.agents.state import AgentState
from app.core.logging import get_logger

logger = get_logger(__name__)

_MODEL = get_model("scraper_parser")  # Haiku — fast, cheap, perfect for extraction

_SYSTEM_PROMPT = """
You are an expert e-commerce data extractor trained on Google's Product structured data spec (schema.org).
Given raw scraped text from a product page, extract only the core product information.
Ignore navigation menus, footers, cookie banners, ads, and unrelated content.

Return ONLY a valid JSON object — no prose, no markdown fences.

Required JSON schema (aligned with schema.org Product + Google Merchant Center fields):
{
  "product_name": string,
  "brand": string,
  "seller_name": string | null,
  "product_url": string | null,
  "language": string | null,
  "sku": string | null,
  "gtin": string | null,
  "mpn": string | null,
  "price": string,
  "original_price": string | null,
  "discount_pct": float | null,
  "currency": string,
  "price_valid_until": string | null,
  "availability": "InStock|OutOfStock|PreOrder|LimitedAvailability",
  "condition": "NewCondition|UsedCondition|RefurbishedCondition",
  "description": string,
  "features": [string],
  "categories": [string],
  "breadcrumb": [string],
  "image_urls": [string],
  "video_urls": [string],
  "images_count": int,
  "has_video": boolean,
  "has_size_guide": boolean,
  "has_reviews": boolean,
  "review_count": int | null,
  "avg_rating": float | null,
  "rating_max": float,
  "shipping_info": string | null,
  "return_policy": string | null,
  "warranty": string | null,
  "color_variants": [string],
  "size_variants": [string],
  "related_products_count": int,
  "page_word_count": int,
  "above_fold_cta": string | null,
  "trust_badges": [string]
}
""".strip()


async def extractor_agent(state: AgentState) -> AgentState:
    """Extract clean structured product data from raw scraped content."""
    markdown = state.get("markdown_content", "")
    if not markdown:
        return {"errors": ["extractor_agent: no markdown_content"]}

    logger.info("extractor_agent.start", model=_MODEL, chars=len(markdown))
    t0 = time.monotonic()

    source_url = state.get("url", "")
    response = await claude.messages.create(
        model=_MODEL,
        max_tokens=2000,
        system=_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Source URL: {source_url}\n\n"
                    f"Extract product data from this scraped page content:\n\n"
                    f"{markdown[:10000]}"
                ),
            }
        ],
    )

    raw = response.content[0].text.strip()
    duration_ms = int((time.monotonic() - t0) * 1000)

    structured_data, parse_err = safe_json_parse_report(raw, "extractor_agent")
    if parse_err:
        return {"errors": [parse_err]}

    dom = state.get("dom_technical_seo")
    if isinstance(dom, dict) and dom:
        structured_data["_dom_technical_seo"] = dom

    logger.info(
        "extractor_agent.done",
        product=structured_data.get("product_name"),
        duration_ms=duration_ms,
    )

    return {
        "json_structured_data": structured_data,
        "agent_reports": [
            {
                "agent": "extractor_agent",
                "model": _MODEL,
                "input": {
                    "markdown_chars": len(markdown),
                    "scraper_method": state.get("scraper_method", "unknown"),
                },
                "output": structured_data,
                "duration_ms": duration_ms,
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            }
        ],
    }
