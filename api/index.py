"""
Vercel serverless entry — only /api/* and /health hit this file.
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from mangum import Mangum
    from app.main import app as fastapi_app

    handler = Mangum(fastapi_app, lifespan="auto")
    app = handler
except Exception:
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse, PlainTextResponse

    _err = traceback.format_exc()
    fastapi_app = FastAPI()

    @fastapi_app.get("/health")
    async def health_fail():
        return JSONResponse({"status": "error", "boot_error": _err}, status_code=500)

    @fastapi_app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def boot_error(full_path: str = ""):
        return PlainTextResponse(_err, status_code=500)

    handler = fastapi_app
    app = fastapi_app
