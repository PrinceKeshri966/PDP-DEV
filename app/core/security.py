"""
app/core/security.py
Verifies Clerk-issued JWTs offline using the Clerk PEM public key.
Returns a typed ClerkUser payload so every route knows who is calling.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import jwt                          # PyJWT
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import get_settings

settings = get_settings()

# ── Bearer extractor ──────────────────────────────────────────────────────────
_bearer = HTTPBearer(auto_error=False)

# ── JWKS cache (populated lazily) ─────────────────────────────────────────────
_jwks_cache: dict[str, Any] | None = None
_CLERK_JWKS_URL = "https://api.clerk.com/v1/jwks"


async def _fetch_jwks() -> dict[str, Any]:
    global _jwks_cache
    if _jwks_cache is not None:
        return _jwks_cache
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            _CLERK_JWKS_URL,
            headers={"Authorization": f"Bearer {settings.clerk_secret_key}"},
            timeout=10,
        )
        resp.raise_for_status()
        _jwks_cache = resp.json()
    return _jwks_cache


def _pem_from_settings() -> str | None:
    """Return the PEM key from env if configured (for offline / air-gapped use)."""
    raw = settings.clerk_jwt_key.strip()
    if raw:
        return raw.replace("\\n", "\n")
    return None


# ── Verified user model ───────────────────────────────────────────────────────
class ClerkUser:
    """Slim wrapper around the decoded JWT claims."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._p = payload

    @property
    def clerk_id(self) -> str:          # e.g. "user_2abc..."
        return self._p["sub"]

    @property
    def email(self) -> str | None:
        emails = self._p.get("email_addresses", [])
        return emails[0].get("email_address") if emails else self._p.get("email")

    @property
    def org_id(self) -> str | None:     # Clerk Organization = Tenant
        return self._p.get("org_id")

    @property
    def raw(self) -> dict[str, Any]:
        return self._p


# ── Core verification logic ───────────────────────────────────────────────────
async def _verify_jwt(token: str) -> dict[str, Any]:
    """
    Try offline PEM verification first (fast, no network).
    Fall back to JWKS endpoint if PEM is not set.
    """
    algorithms = ["RS256"]

    # ── 1. Offline PEM path ───────────────────────────────────────────────
    pem = _pem_from_settings()
    if pem:
        try:
            return jwt.decode(
                token,
                pem,
                algorithms=algorithms,
                options={"verify_aud": False},
            )
        except jwt.ExpiredSignatureError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token has expired.",
            )
        except jwt.PyJWTError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Invalid token: {exc}",
            )

    # ── 2. JWKS path (online) ─────────────────────────────────────────────
    try:
        jwks = await _fetch_jwks()
        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header.get("kid")

        public_key: Any = None
        for key_data in jwks.get("keys", []):
            if key_data.get("kid") == kid:
                public_key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(key_data))
                break

        if public_key is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="No matching public key found in JWKS.",
            )

        return jwt.decode(
            token,
            public_key,
            algorithms=algorithms,
            options={"verify_aud": False},
        )
    except HTTPException:
        raise
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired.",
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
        )


_DEV_USER_PAYLOAD: dict[str, Any] = {
    "sub": "user_dev_local",
    "email": "dev@localhost",
}


# ── FastAPI dependency ────────────────────────────────────────────────────────
async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> ClerkUser:
    """
    FastAPI dependency – inject into any protected route:

        @router.get("/me")
        async def me(user: ClerkUser = Depends(get_current_user)):
            ...
    """
    token = credentials.credentials if credentials else None

    # Local dev bypass only when no auth provider is configured
    if (
        settings.dev_auth_bypass
        and settings.app_env == "development"
        and not settings.google_enabled
        and not settings.clerk_enabled
    ):
        if not token or token == "dev":
            return ClerkUser(_DEV_USER_PAYLOAD)

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated. Sign in with Google or provide a Bearer token.",
        )

    # App-issued JWT (Google OAuth users)
    try:
        payload = jwt.decode(
            token,
            settings.secret_key,
            algorithms=["HS256"],
            options={"verify_aud": False},
        )
        if payload.get("iss") == "organic360" and payload.get("sub"):
            return ClerkUser(payload)
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired. Please sign in again.",
        )
    except jwt.PyJWTError:
        pass

    if settings.clerk_enabled:
        payload = await _verify_jwt(token)
        return ClerkUser(payload)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid session. Please sign in again.",
    )
