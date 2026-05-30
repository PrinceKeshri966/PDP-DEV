"""
app/api/routes/analyze.py

POST /analyze/pdp      – Mode 1: Full 10-agent pipeline
POST /analyze/business – Mode 2: Blueprint generation
"""
# Integration instructions for Chat + File Upload (for Cursor):
#
# "I have the backend logic working (I get a valid JSON response). Now I need to implement the Chat and File Upload features and fix the view rendering. Please update my files as follows:
#
# 1. Backend (analyze.py):
#
# Create a new endpoint POST /api/v1/chat/upload. It must accept UploadFile. Use python-docx, pypdf, and python-pptx to extract text.
#
# Ensure this endpoint returns a JSON: {"text": "extracted content..."}.
#
# 2. Frontend (index.html):
#
# Inside view === 'MODE2_INPUT', replace the static form with the Chat UI I previously shared (or a refined version).
#
# File Upload Integration: Add a file input (hidden) and a paperclip button. When a file is uploaded, use fetch to send it to /api/v1/chat/upload. Take the returned text and append it as a 'system' message into the chatHistory state.
#
# Chat Logic: Ensure handleSendMessage sends the message to the backend.
#
# Display Logic: Ensure that when the AI returns the blueprint JSON (the one I shared), setView('SELECT_LAYOUT') is triggered and the UI displays the blueprint data fetched from the API response."

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone

import httpx
from uuid import UUID

from anthropic import APIConnectionError, APIStatusError, APITimeoutError
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db_tenant, get_db_user
from app.core.database import AsyncSessionLocal, get_db
from app.models.analysis_report import AnalysisReport
from app.models.blueprint import Blueprint
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.analyze import (
    AnalyzeBusinessRequest,
    AnalyzeBusinessResponse,
    AnalyzePDPRequest,
    AnalyzePDPResponse,
)

router = APIRouter(prefix="/analyze", tags=["Analyze"])


def _mode1_runners():
    from app.agents.mode1_graph import run_mode1, stream_mode1

    return run_mode1, stream_mode1


def _mode2_runners():
    from app.agents.mode2_graph import run_mode2, stream_mode2

    return run_mode2, stream_mode2


def _pipeline_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, APIStatusError) and exc.status_code in (429, 529, 503):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI service is temporarily overloaded. Please wait a moment and try again.",
        )
    if isinstance(exc, (APIConnectionError, APITimeoutError, httpx.ReadError, httpx.ConnectError, httpx.TimeoutException)):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI service connection dropped. Please retry — the pipeline will auto-retry on transient errors.",
        )
    msg = str(exc).lower()
    if "connection error" in msg or "readerror" in msg or "connecterror" in msg:
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI service connection dropped. Please retry — the pipeline will auto-retry on transient errors.",
        )
    if "overloaded" in msg or "529" in msg or "rate limit" in msg:
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI service is temporarily overloaded. Please wait a moment and try again.",
        )
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=f"Pipeline failed: {exc}",
    )


def _sse_event(payload: dict) -> str:
    return f"data: {json.dumps(payload, default=str)}\n\n"


def _persist_mode1_report(report: AnalysisReport, final_state: dict) -> None:
    report.status = final_state.get("status", "completed")
    report.raw_markdown = final_state.get("markdown_content")
    report.scraper_method = final_state.get("scraper_method")
    report.json_structured_data = final_state.get("json_structured_data") or {}
    report.seo_report = final_state.get("seo_report") or {}
    report.aeo_report = final_state.get("aeo_report") or {}
    report.ux_report = final_state.get("ux_report") or {}
    report.competitor_report = final_state.get("competitor_report") or {}
    report.psychology_report = final_state.get("psychology_report") or {}
    report.final_diagnosis = final_state.get("final_diagnosis") or {}
    report.autofix_report = final_state.get("autofix_report") or {}
    report.generated_content = final_state.get("generated_content") or {}
    report.seo_score = report.seo_report.get("overall_seo_score")
    report.overall_health_score = report.final_diagnosis.get("overall_health_score")
    report.agent_logs = final_state.get("agent_reports", [])
    report.total_tokens = sum(
        (r.get("input_tokens", 0) or 0) + (r.get("output_tokens", 0) or 0)
        for r in final_state.get("agent_reports", [])
    ) or None
    report.error_message = "; ".join(final_state.get("errors", [])) or None
    if report.status == "completed":
        report.completed_at = datetime.now(timezone.utc)


def _mode1_response(report: AnalysisReport, final_state: dict, url: str) -> AnalyzePDPResponse:
    return AnalyzePDPResponse(
        report_id=report.id,
        status=report.status,
        overall_health_score=report.overall_health_score,
        seo_score=report.seo_score,
        source_url=url,
        json_structured_data=report.json_structured_data,
        seo_report=report.seo_report,
        aeo_report=report.aeo_report,
        ux_report=report.ux_report,
        competitor_report=report.competitor_report,
        psychology_report=report.psychology_report,
        final_diagnosis=report.final_diagnosis,
        autofix_report=report.autofix_report,
        generated_content=report.generated_content,
        agent_reports=final_state.get("agent_reports", []),
        errors=final_state.get("errors", []),
    )


def _persist_mode2_blueprint(blueprint: Blueprint, final_state: dict) -> None:
    blueprint.status = final_state.get("status", "completed")
    blueprint.business_understanding = final_state.get("business_understanding") or {}
    blueprint.pdp_research = final_state.get("pdp_research") or {}
    blueprint.final_blueprint = final_state.get("final_blueprint") or {}
    blueprint.title = (
        blueprint.final_blueprint.get("blueprint_title") if blueprint.final_blueprint else None
    )
    blueprint.error_message = "; ".join(final_state.get("errors", [])) or None
    if blueprint.status == "completed":
        blueprint.completed_at = datetime.now(timezone.utc)


def _mode2_response(blueprint: Blueprint, final_state: dict) -> AnalyzeBusinessResponse:
    return AnalyzeBusinessResponse(
        blueprint_id=blueprint.id,
        status=blueprint.status,
        title=blueprint.title,
        business_input=blueprint.business_input,
        business_understanding=blueprint.business_understanding,
        pdp_research=blueprint.pdp_research,
        final_blueprint=blueprint.final_blueprint,
        agent_reports=final_state.get("agent_reports", []),
        errors=final_state.get("errors", []),
    )


async def _commit_mode1_report(report_id: UUID, final_state: dict, *, failed: bool = False, error: str | None = None) -> AnalysisReport:
    """Persist report using a fresh DB session (SSE streams outlive the request session)."""
    async with AsyncSessionLocal() as session:
        report = await session.get(AnalysisReport, report_id)
        if report is None:
            raise HTTPException(status_code=404, detail="Report not found")
        if failed:
            report.status = "failed"
            report.error_message = error
        else:
            _persist_mode1_report(report, final_state)
        await session.commit()
        await session.refresh(report)
        return report


async def _commit_mode2_blueprint(blueprint_id: UUID, final_state: dict, *, failed: bool = False, error: str | None = None) -> Blueprint:
    async with AsyncSessionLocal() as session:
        blueprint = await session.get(Blueprint, blueprint_id)
        if blueprint is None:
            raise HTTPException(status_code=404, detail="Blueprint not found")
        if failed:
            blueprint.status = "failed"
            blueprint.error_message = error
        else:
            _persist_mode2_blueprint(blueprint, final_state)
        await session.commit()
        await session.refresh(blueprint)
        return blueprint


async def _create_running_report(tenant_id: UUID, user_id: UUID, url: str, competitor_urls: list) -> UUID:
    async with AsyncSessionLocal() as session:
        report = AnalysisReport(
            tenant_id=tenant_id,
            user_id=user_id,
            source_url=url,
            competitor_urls=competitor_urls,
            status="running",
        )
        session.add(report)
        await session.commit()
        await session.refresh(report)
        return report.id


async def _create_running_blueprint(tenant_id: UUID, user_id: UUID, business_input: str) -> UUID:
    async with AsyncSessionLocal() as session:
        blueprint = Blueprint(
            tenant_id=tenant_id,
            user_id=user_id,
            business_input=business_input,
            status="running",
        )
        session.add(blueprint)
        await session.commit()
        await session.refresh(blueprint)
        return blueprint.id


# ── Mode 1: Full Pipeline ─────────────────────────────────────────────────────
@router.post(
    "/pdp",
    response_model=AnalyzePDPResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Mode 1 – Full PDP analysis (SEO + AEO + UX + Competitor + Psychology + Fixes)",
)
async def analyze_pdp(
    body: AnalyzePDPRequest,
    db: AsyncSession = Depends(get_db),
    db_user: User = Depends(get_db_user),
    tenant: Tenant = Depends(get_db_tenant),
) -> AnalyzePDPResponse:
    # ── Create DB record immediately so client has an ID ──────────────────────
    report = AnalysisReport(
        tenant_id=tenant.id,
        user_id=db_user.id,
        source_url=body.url,
        competitor_urls=body.competitor_urls,
        status="running",
    )
    db.add(report)
    await db.flush()

    run_mode1, _ = _mode1_runners()
    try:
        final_state = await run_mode1(
            url=body.url,
            tenant_id=str(tenant.id),
            user_id=str(db_user.id),
            competitor_urls=body.competitor_urls,
            compare_as=body.compare_as,
        )
    except Exception as exc:
        report.status = "failed"
        report.error_message = str(exc)
        await db.commit()
        raise _pipeline_http_error(exc) from exc

    # ── Persist all results ───────────────────────────────────────────────────
    _persist_mode1_report(report, final_state)
    await db.flush()

    return _mode1_response(report, final_state, body.url)


@router.post(
    "/pdp/stream",
    summary="Mode 1 – Full PDP analysis with live agent progress (SSE)",
)
async def analyze_pdp_stream(
    body: AnalyzePDPRequest,
    db_user: User = Depends(get_db_user),
    tenant: Tenant = Depends(get_db_tenant),
) -> StreamingResponse:
    report_id = await _create_running_report(
        tenant.id, db_user.id, body.url, body.competitor_urls
    )
    report_id_str = str(report_id)

    _, stream_mode1 = _mode1_runners()

    async def event_generator() -> AsyncIterator[str]:
        final_state: dict = {}
        try:
            async for event, state in stream_mode1(
                url=body.url,
                tenant_id=str(tenant.id),
                user_id=str(db_user.id),
                competitor_urls=body.competitor_urls,
                compare_as=body.compare_as,
            ):
                final_state = state
                if event["type"] == "progress":
                    yield _sse_event({**event, "report_id": report_id_str})
                elif event["type"] == "done":
                    report = await _commit_mode1_report(report_id, final_state)
                    result = _mode1_response(report, final_state, body.url)
                    yield _sse_event(
                        {
                            "type": "done",
                            "report_id": report_id_str,
                            "result": result.model_dump(mode="json"),
                        }
                    )
        except Exception as exc:
            await _commit_mode1_report(report_id, final_state, failed=True, error=str(exc))
            err = _pipeline_http_error(exc)
            yield _sse_event({"type": "error", "detail": err.detail})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Mode 2: Business Brief → PDP Blueprint ───────────────────────────────────
@router.post(
    "/business",
    response_model=AnalyzeBusinessResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Mode 2 – Generate a PDP Blueprint from a business brief",
)
async def analyze_business(
    body: AnalyzeBusinessRequest,
    db: AsyncSession = Depends(get_db),
    db_user: User = Depends(get_db_user),
    tenant: Tenant = Depends(get_db_tenant),
) -> AnalyzeBusinessResponse:
    blueprint = Blueprint(
        tenant_id=tenant.id,
        user_id=db_user.id,
        business_input=body.business_input,
        status="running",
    )
    db.add(blueprint)
    await db.flush()

    run_mode2, _ = _mode2_runners()
    try:
        final_state = await run_mode2(
            business_input=body.business_input,
            tenant_id=str(tenant.id),
            user_id=str(db_user.id),
        )
    except Exception as exc:
        blueprint.status = "failed"
        blueprint.error_message = str(exc)
        await db.commit()
        raise _pipeline_http_error(exc) from exc

    blueprint.status = final_state.get("status", "completed")
    _persist_mode2_blueprint(blueprint, final_state)
    await db.flush()

    return _mode2_response(blueprint, final_state)


@router.post(
    "/business/stream",
    summary="Mode 2 – Blueprint generation with live agent progress (SSE)",
)
async def analyze_business_stream(
    body: AnalyzeBusinessRequest,
    db_user: User = Depends(get_db_user),
    tenant: Tenant = Depends(get_db_tenant),
) -> StreamingResponse:
    blueprint_id = await _create_running_blueprint(tenant.id, db_user.id, body.business_input)
    blueprint_id_str = str(blueprint_id)

    _, stream_mode2 = _mode2_runners()

    async def event_generator() -> AsyncIterator[str]:
        final_state: dict = {}
        try:
            async for event, state in stream_mode2(
                business_input=body.business_input,
                tenant_id=str(tenant.id),
                user_id=str(db_user.id),
            ):
                final_state = state
                if event["type"] == "progress":
                    yield _sse_event({**event, "blueprint_id": blueprint_id_str})
                elif event["type"] == "done":
                    blueprint = await _commit_mode2_blueprint(blueprint_id, final_state)
                    result = _mode2_response(blueprint, final_state)
                    yield _sse_event(
                        {
                            "type": "done",
                            "blueprint_id": blueprint_id_str,
                            "result": result.model_dump(mode="json"),
                        }
                    )
        except Exception as exc:
            await _commit_mode2_blueprint(blueprint_id, final_state, failed=True, error=str(exc))
            err = _pipeline_http_error(exc)
            yield _sse_event({"type": "error", "detail": err.detail})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
