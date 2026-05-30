"""
app/agents/state.py

AgentState is the single shared payload that flows through every LangGraph node.

Parallel fan-out fix:
  Fields written by multiple parallel agents (errors, agent_reports) use
  Annotated[list, operator.add] so LangGraph merges them instead of conflicting.
  All other fields are written by exactly ONE agent, so no reducer needed.
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, Optional
from typing_extensions import TypedDict


def state_dict(state: AgentState, key: str) -> dict[str, Any]:
    """Return a state dict field, treating missing or null values as {}."""
    val = state.get(key)
    return val if isinstance(val, dict) else {}


class AgentState(TypedDict, total=False):
    # ── Identity (written once at pipeline start, never by agents) ────────────
    tenant_id: str
    user_id: str

    # ── Mode 1 inputs ─────────────────────────────────────────────────────────
    url: Optional[str]
    competitor_urls: Optional[list[str]]
    compare_as: Optional[str]  # auto | homepage | product

    # ── Mode 2 inputs ─────────────────────────────────────────────────────────
    business_input: Optional[str]
    # Uploaded file text to help Mode 2 (chat/file uploads)
    uploaded_context: Optional[str]
    # Chat history for Mode 2 interactive chat (sequence of messages)
    chat_history: Optional[list[dict[str, Any]]]

    # ── Phase 1: Scraper output ───────────────────────────────────────────────
    markdown_content: Optional[str]
    scraper_method: Optional[str]
    dom_technical_seo: Optional[dict[str, Any]]

    # ── Phase 1: Extractor output ─────────────────────────────────────────────
    json_structured_data: Optional[dict[str, Any]]

    # ── Phase 2: Parallel analysis outputs (each written by ONE agent) ────────
    seo_report: Optional[dict[str, Any]]
    aeo_report: Optional[dict[str, Any]]
    ux_report: Optional[dict[str, Any]]
    competitor_report: Optional[dict[str, Any]]
    psychology_report: Optional[dict[str, Any]]

    # ── Phase 3: Prioritization output ───────────────────────────────────────
    final_diagnosis: Optional[dict[str, Any]]

    # ── Phase 4: Parallel generation outputs (each written by ONE agent) ──────
    autofix_report: Optional[dict[str, Any]]
    generated_content: Optional[dict[str, Any]]

    # ── Mode 2 intermediates ──────────────────────────────────────────────────
    business_understanding: Optional[dict[str, Any]]
    pdp_research: Optional[dict[str, Any]]
    final_blueprint: Optional[dict[str, Any]]

    # ── Audit trail — REDUCER: parallel agents append, LangGraph merges ───────
    agent_reports: Annotated[list[dict[str, Any]], operator.add]

    # ── Pipeline control ──────────────────────────────────────────────────────
    status: str
    # errors — REDUCER: parallel agents append, LangGraph merges
    errors: Annotated[list[str], operator.add]
