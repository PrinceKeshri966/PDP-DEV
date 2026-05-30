"""
app/main.py
FastAPI application factory – registers routers, lifespan, middleware.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from app.api.routes import analyze, auth, chat, reports, screenshot
from app.core.config import get_settings
from app.core.playwright_env import playwright_enabled
from app.core.database import init_db
from app.core.logging import get_logger, setup_logging

_ROOT_DIR = Path(__file__).parent.parent
_FRONTEND_DIR = _ROOT_DIR / "frontend"
_PUBLIC_DIR = _ROOT_DIR / "public"

settings = get_settings()
setup_logging()
logger = get_logger(__name__)


# ── Lifespan (startup / shutdown) ─────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("startup.begin", app=settings.app_name, env=settings.app_env)
    if os.getenv("VERCEL") == "1":
        logger.info("startup.skip_db_init", reason="vercel_serverless")
    else:
        try:
            await init_db()
            logger.info("startup.db_ready")
        except Exception as exc:
            err = str(exc).lower()
            if "password authentication failed" in err or "invalidpassword" in type(exc).__name__.lower():
                logger.error(
                    "startup.db_failed",
                    hint=(
                        "DATABASE_URL is wrong. Copy fresh connection string from Neon, "
                        "change postgresql:// to postgresql+asyncpg://, add ?ssl=require."
                    ),
                )
            else:
                logger.error("startup.db_failed", error=str(exc))
            raise
    yield
    logger.info("shutdown.begin")


# ── App factory ───────────────────────────────────────────────────────────────
def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version="1.0.0",
        description=(
            "Multi-Tenant AI Commerce OS – "
            "PDP analysis and blueprint generation powered by Claude."
        ),
        docs_url="/docs" if settings.debug else None,
        redoc_url="/redoc" if settings.debug else None,
        lifespan=lifespan,
    )

    # ── CORS ──────────────────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Routers ───────────────────────────────────────────────────────────────
    app.include_router(analyze.router, prefix="/api/v1")
    app.include_router(screenshot.router, prefix="/api/v1")
    app.include_router(chat.router, prefix="/api/v1")
    app.include_router(auth.router, prefix="/api/v1")
    app.include_router(reports.router, prefix="/api/v1")

    # ── Health check ──────────────────────────────────────────────────────────
    @app.get("/health", tags=["Health"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "app": settings.app_name}

    # ── Frontend config (exposes public Clerk key to the browser) ─────────────
    @app.get("/api/v1/config", tags=["Config"])
    async def frontend_config() -> dict[str, str | bool]:
        if settings.google_enabled:
            provider = "google"
        elif settings.clerk_enabled:
            provider = "clerk"
        else:
            provider = "none"
        return {
            "clerk_publishable_key": settings.clerk_publishable_key,
            "auth_provider": provider,
            "google_login_url": "/api/v1/auth/google/login",
            "auth_required": provider != "none"
                or not (settings.dev_auth_bypass and settings.app_env == "development"),
            "screenshot_available": playwright_enabled(),
        }

    # ── Serve the React frontend (SPA — deep links must return index.html) ─────

    def _frontend_index_path() -> Path:
        if os.getenv("VERCEL") == "1":
            index = _PUBLIC_DIR / "index.html"
            if index.is_file():
                return index
        return _FRONTEND_DIR / "index.html"

    def _serve_spa() -> FileResponse:
        return FileResponse(_frontend_index_path())

    @app.get("/api/v1/glossary/checks", tags=["Config"])
    async def check_glossary() -> JSONResponse:
        import json
        path = _FRONTEND_DIR / "check_glossary.json"
        return JSONResponse(content=json.loads(path.read_text(encoding="utf-8")))

    @app.get("/", include_in_schema=False)
    @app.get("/index.html", include_in_schema=False)
    @app.get("/history", include_in_schema=False)
    @app.get("/report/{report_id}", include_in_schema=False)
    @app.get("/report/{report_id}/{tab_slug}", include_in_schema=False)
    async def serve_frontend() -> FileResponse:
        return _serve_spa()

    return app


app = create_app()
