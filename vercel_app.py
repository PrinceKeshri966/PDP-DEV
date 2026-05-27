"""
Lightweight FastAPI entry for Vercel serverless.
API routers register lazily so /health and /api/v1/config work even if
optional deps fail during cold start.
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

from app.core.config import get_settings

settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_routers_loaded = False


def _load_routers() -> None:
    global _routers_loaded
    if _routers_loaded:
        return

    for loader in (
        _load_auth,
        _load_reports,
        _load_chat,
        _load_analyze,
    ):
        try:
            loader()
        except Exception:
            pass
    _routers_loaded = True


def _load_auth() -> None:
    from app.api.routes import auth

    app.include_router(auth.router, prefix="/api/v1")


def _load_reports() -> None:
    from app.api.routes import reports

    app.include_router(reports.router, prefix="/api/v1")


def _load_chat() -> None:
    from app.api.routes import chat

    app.include_router(chat.router, prefix="/api/v1")


def _load_analyze() -> None:
    from app.api.routes import analyze

    app.include_router(analyze.router, prefix="/api/v1")


try:
    _load_routers()
except Exception:
    pass


def _auth_provider() -> str:
    if settings.google_enabled:
        return "google"
    if settings.clerk_enabled:
        return "clerk"
    return "none"


@app.get("/health", tags=["Health"])
async def health() -> dict[str, str]:
    return {"status": "ok", "app": settings.app_name}


@app.get("/api/v1/config", tags=["Config"])
async def frontend_config() -> dict[str, str | bool]:
    provider = _auth_provider()
    return {
        "clerk_publishable_key": settings.clerk_publishable_key,
        "auth_provider": provider,
        "google_login_url": "/api/v1/auth/google/login",
        "auth_required": provider != "none"
        or not (settings.dev_auth_bypass and settings.app_env == "development"),
    }


@app.get("/", include_in_schema=False)
async def root(request: Request) -> RedirectResponse:
    dest = "/index.html"
    if request.url.query:
        dest = f"{dest}?{request.url.query}"
    return RedirectResponse(dest, status_code=307)


@app.get("/api/v1/glossary/checks", tags=["Config"])
async def check_glossary() -> JSONResponse:
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parent / "frontend" / "check_glossary.json"
    return JSONResponse(content=json.loads(path.read_text(encoding="utf-8")))


@app.get("/api/v1/_debug/routes", tags=["Debug"], include_in_schema=False)
async def debug_routes() -> dict[str, list[str]]:
    return {
        "paths": sorted(
            {getattr(r, "path", "") for r in app.routes if getattr(r, "path", None)}
        )
    }
