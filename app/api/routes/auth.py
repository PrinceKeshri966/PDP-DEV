"""
app/api/routes/auth.py
Google OAuth sign-in / sign-up for Organic360.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import ClerkUser, get_current_user
from app.models.tenant import Tenant
from app.models.user import User

router = APIRouter(prefix="/auth", tags=["Auth"])
settings = get_settings()

_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"


def _make_oauth_state() -> str:
    """Signed state (no cookie) — works across localhost / 127.0.0.1."""
    nonce = secrets.token_urlsafe(24)
    sig = hmac.new(
        settings.secret_key.encode(),
        nonce.encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"{nonce}.{sig}"


def _verify_oauth_state(state: str) -> bool:
    try:
        nonce, sig = state.rsplit(".", 1)
        expected = hmac.new(
            settings.secret_key.encode(),
            nonce.encode(),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(sig, expected)
    except (ValueError, TypeError):
        return False


def _issue_app_jwt(user: User) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user.clerk_user_id,
        "email": user.email,
        "name": user.full_name or user.email.split("@")[0],
        "picture": user.avatar_url,
        "iss": "organic360",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=settings.jwt_expire_hours)).timestamp()),
    }
    token = jwt.encode(payload, settings.secret_key, algorithm="HS256")
    return token if isinstance(token, str) else token.decode()


async def _create_user_tenant(db: AsyncSession, email: str, full_name: str | None) -> Tenant:
    """Each user gets a private workspace (tenant)."""
    slug_base = "".join(c for c in (email.split("@")[0] or "user") if c.isalnum())[:16] or "user"
    display = (full_name or email.split("@")[0] or "My").strip()
    tenant = Tenant(
        name=f"{display}'s Workspace",
        slug=f"{slug_base}-{secrets.token_hex(3)}",
    )
    db.add(tenant)
    await db.flush()
    return tenant


@router.get("/google/login")
async def google_login() -> RedirectResponse:
    """Start Google OAuth — works for both sign-in and sign-up."""
    if not settings.google_enabled:
        raise HTTPException(
            status_code=503,
            detail="Google OAuth is not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env",
        )

    state = _make_oauth_state()
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "online",
        "prompt": "select_account",
        "state": state,
    }
    url = f"{_GOOGLE_AUTH_URL}?{urlencode(params)}"
    return RedirectResponse(url=url, status_code=302)


@router.get("/google/callback")
async def google_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Google redirects here after user approves access."""
    if error:
        return RedirectResponse(
            url=f"{settings.app_base_url}/index.html?auth_error={error}",
            status_code=302,
        )
    if not code or not state:
        return RedirectResponse(
            url=f"{settings.app_base_url}/index.html?auth_error=missing_code",
            status_code=302,
        )

    if not _verify_oauth_state(state):
        return RedirectResponse(
            url=f"{settings.app_base_url}/index.html?auth_error=invalid_state",
            status_code=302,
        )

    async with httpx.AsyncClient(timeout=15.0) as client:
        token_resp = await client.post(
            _GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": settings.google_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        if token_resp.status_code != 200:
            return RedirectResponse(
                url=f"{settings.app_base_url}/index.html?auth_error=token_exchange_failed",
                status_code=302,
            )
        access_token = token_resp.json().get("access_token")
        if not access_token:
            return RedirectResponse(
                url=f"{settings.app_base_url}/index.html?auth_error=no_access_token",
                status_code=302,
            )

        user_resp = await client.get(
            _GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if user_resp.status_code != 200:
            return RedirectResponse(
                url=f"{settings.app_base_url}/index.html?auth_error=userinfo_failed",
                status_code=302,
            )
        profile = user_resp.json()

    google_sub = profile.get("id")
    email = profile.get("email")
    if not google_sub or not email:
        return RedirectResponse(
            url=f"{settings.app_base_url}/index.html?auth_error=invalid_profile",
            status_code=302,
        )

    clerk_user_id = f"google_{google_sub}"
    result = await db.execute(select(User).where(User.clerk_user_id == clerk_user_id))
    user = result.scalar_one_or_none()

    if user is None:
        tenant = await _create_user_tenant(db, email, profile.get("name"))
        user = User(
            clerk_user_id=clerk_user_id,
            tenant_id=tenant.id,
            email=email,
            full_name=profile.get("name"),
            avatar_url=profile.get("picture"),
            role="owner",
        )
        db.add(user)
    else:
        user.email = email
        user.full_name = profile.get("name") or user.full_name
        user.avatar_url = profile.get("picture") or user.avatar_url
        user.last_login_at = datetime.now(timezone.utc)

    await db.commit()

    app_token = _issue_app_jwt(user)
    # Frontend reads token from URL and stores in localStorage (shared across tabs).
    return RedirectResponse(
        url=f"{settings.app_base_url}/index.html?auth_success=1&token={app_token}",
        status_code=302,
    )


@router.get("/me")
async def auth_me(
    clerk_user: ClerkUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str | None]:
    """Current signed-in user profile (name from Google account)."""
    result = await db.execute(
        select(User).where(User.clerk_user_id == clerk_user.clerk_id)
    )
    user = result.scalar_one_or_none()
    if user:
        return {
            "name": user.full_name,
            "email": user.email,
            "picture": user.avatar_url,
        }
    return {
        "name": clerk_user.raw.get("name"),
        "email": clerk_user.email,
        "picture": clerk_user.raw.get("picture"),
    }
