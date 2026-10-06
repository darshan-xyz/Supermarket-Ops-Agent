"""Health checks, root discovery, and keep-alive endpoints for Render.com and UptimeRobot."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ...config import MODEL
from ...db.master import MasterRegistry, get_master_registry
from ..auth import COOKIE_NAME, decode_session_token

router = APIRouter(tags=["Health & Discovery"])

_START_TIME = time.time()


def _get_uptime_seconds() -> int:
    return int(time.time() - _START_TIME)


def _get_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@router.get("/health", response_class=JSONResponse)
@router.get("/ping", response_class=JSONResponse)
async def health_check() -> dict[str, Any]:
    """Render.com / UptimeRobot health probe endpoint."""
    return {
        "status": "ok",
        "healthy": True,
        "uptime_seconds": _get_uptime_seconds(),
        "timestamp": _get_timestamp(),
    }


@router.get("/", response_class=HTMLResponse)
async def root_discovery(
    request: Request,
    registry: MasterRegistry = Depends(get_master_registry),
) -> Any:
    """Root endpoint: redirects browser to /dashboard (if authenticated) or /login.

    If request accepts JSON or is an automated probe, returns service metadata.
    """
    accept = request.headers.get("accept", "")
    user_agent = request.headers.get("user-agent", "").lower()

    # If automated health probe or JSON client
    if "application/json" in accept or "uptimerobot" in user_agent or "curl" in user_agent:
        try:
            stores_count = len(registry.list_all_tenants(active_only=True))
        except Exception:
            stores_count = 0

        return JSONResponse(
            {
                "service": "kirana-ops-agent",
                "status": "healthy",
                "uptime_seconds": _get_uptime_seconds(),
                "model": MODEL,
                "stores_count": stores_count,
                "timestamp": _get_timestamp(),
            }
        )

    # Browser request: check cookie
    cookie = request.cookies.get(COOKIE_NAME)
    token_data = decode_session_token(cookie) if cookie else None
    if token_data and registry.get_tenant_by_code(token_data.get("store_code", "")):
        return RedirectResponse(url="/dashboard", status_code=303)

    return RedirectResponse(url="/login", status_code=303)
