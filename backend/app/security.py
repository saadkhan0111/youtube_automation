"""Request security: optional dashboard token, CSRF protection, agent token."""
from __future__ import annotations

import hashlib
import hmac

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from .config import settings

SESSION_COOKIE = "ytshorts_session"
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "ytshorts"
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
PUBLIC_PATHS = {"/api/health", "/api/auth/callback", "/api/login"}


def session_value() -> str:
    return hashlib.sha256(("ytshorts:" + settings.dashboard_token).encode()).hexdigest()


def _bearer(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else None


class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not path.startswith("/api/"):
            return await call_next(request)

        if path.startswith("/api/agent/"):
            # Agent API: bearer token only (no cookies, so no CSRF exposure).
            if not settings.agent_api_token:
                return JSONResponse({"detail": "Agent API disabled. Set AGENT_API_TOKEN to enable it."}, status_code=403)
            token = _bearer(request)
            if not token or not hmac.compare_digest(token, settings.agent_api_token):
                return JSONResponse({"detail": "Invalid agent token."}, status_code=401)
            return await call_next(request)

        if path not in PUBLIC_PATHS:
            if settings.dashboard_token:
                token = _bearer(request)
                cookie = request.cookies.get(SESSION_COOKIE)
                ok = (token and hmac.compare_digest(token, settings.dashboard_token)) or (
                    cookie and hmac.compare_digest(cookie, session_value())
                )
                if not ok:
                    return JSONResponse({"detail": "Authentication required."}, status_code=401)
            if request.method in MUTATING:
                # CSRF: require a custom header (forces a CORS preflight) and a trusted Origin.
                if request.headers.get(CSRF_HEADER, "").lower() != CSRF_VALUE:
                    return JSONResponse({"detail": "Missing CSRF header."}, status_code=403)
                origin = request.headers.get("origin")
                if origin and origin not in settings.allowed_origins:
                    return JSONResponse({"detail": "Origin not allowed."}, status_code=403)
        return await call_next(request)


def require_human(request: Request) -> str:
    """Actor name for dashboard routes."""
    return "dashboard"


def ensure_safe_bind() -> None:
    local = settings.host in {"127.0.0.1", "localhost", "::1"}
    if not local and not settings.dashboard_token:
        raise RuntimeError(
            f"Refusing to listen on {settings.host} without DASHBOARD_TOKEN. "
            "Set DASHBOARD_TOKEN (and use HTTPS via a reverse proxy) before exposing the dashboard."
        )


def check_login(token: str) -> None:
    if not settings.dashboard_token:
        return
    if not hmac.compare_digest(token or "", settings.dashboard_token):
        raise HTTPException(401, "Invalid token")
