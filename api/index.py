"""
Vercel serverless entry — wraps FastAPI ASGI app via Mangum.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.main import app  # noqa: E402

try:
    from mangum import Mangum

    handler = Mangum(app, lifespan="on")
except ImportError:  # pragma: no cover
    handler = app
