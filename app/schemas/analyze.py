"""
app/schemas/analyze.py
Request / Response schemas for Mode 1 and Mode 2 endpoints.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, field_validator


# ── Mode 1 Request ────────────────────────────────────────────────────────────
class AnalyzePDPRequest(BaseModel):
    url: str
    competitor_urls: list[str] = []  # optional, max 3

    @field_validator("url")
    @classmethod
    def must_be_https(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")
        return v.strip()

    @field_validator("competitor_urls")
    @classmethod
    def max_three_competitors(cls, v: list[str]) -> list[str]:
        return [u.strip() for u in v[:3] if u.strip().startswith(("http://", "https://"))]


# ── Mode 1 Response ───────────────────────────────────────────────────────────
class AnalyzePDPResponse(BaseModel):
    report_id: UUID
    status: str

    # Scores
    overall_health_score: float | None = None
    seo_score: float | None = None

    # Phase 2 — analysis reports
    seo_report: dict[str, Any] = {}
    aeo_report: dict[str, Any] = {}
    ux_report: dict[str, Any] = {}
    competitor_report: dict[str, Any] = {}
    psychology_report: dict[str, Any] = {}

    # Phase 3 — unified diagnosis
    final_diagnosis: dict[str, Any] = {}

    # Phase 4 — generated fixes
    autofix_report: dict[str, Any] = {}
    generated_content: dict[str, Any] = {}

    # Structured product data + source
    json_structured_data: dict[str, Any] = {}
    source_url: str | None = None

    # Meta
    agent_reports: list[dict[str, Any]] = []
    errors: list[str] = []


# ── Mode 2 Request ────────────────────────────────────────────────────────────
class AnalyzeBusinessRequest(BaseModel):
    business_input: str

    @field_validator("business_input")
    @classmethod
    def not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("business_input cannot be empty")
        return v.strip()


# ── Mode 2 Response ───────────────────────────────────────────────────────────
class AnalyzeBusinessResponse(BaseModel):
    blueprint_id: UUID
    status: str
    title: str | None = None
    business_input: str | None = None
    business_understanding: dict[str, Any] = {}
    pdp_research: dict[str, Any] = {}
    final_blueprint: dict[str, Any] = {}
    agent_reports: list[dict[str, Any]] = []
    errors: list[str] = []
