"""Authentication routes (login, logout, session verification)."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ...config import PROJECT_ROOT
from ...db.master import MasterRegistry, get_master_registry
from ..auth import (
    COOKIE_NAME,
    TenantContext,
    authenticate_store_credentials,
    create_session_token,
    get_optional_tenant_context,
)

log = logging.getLogger("kirana.web.routes.auth")
templates = Jinja2Templates(directory=str(PROJECT_ROOT / "src" / "kirana" / "web" / "templates"))

router = APIRouter(tags=["Authentication"])


@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    ctx: TenantContext | None = Depends(get_optional_tenant_context),
) -> Any:
    """Render login page or redirect to dashboard if already authenticated."""
    if ctx:
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)

    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "error": None,
            "store_code": "",
        },
    )


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    store_code: str = Form(...),
    pin: str = Form(...),
    registry: MasterRegistry = Depends(get_master_registry),
) -> Any:
    """Process Store Code + PIN authentication and issue session cookie."""
    client_ip = request.client.host if request.client else "127.0.0.1"
    success, tenant, error_msg = authenticate_store_credentials(
        store_code=store_code,
        pin=pin,
        registry=registry,
        client_ip=client_ip,
    )

    if not success or not tenant:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "error": error_msg,
                "store_code": store_code,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # Issue signed session cookie
    token = create_session_token(tenant.store_code, role="owner")
    response = RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=False,  # Allow local HTTP testing; production reverse proxies handle TLS
        max_age=86400 * 7,
    )
    return response


@router.get("/logout")
async def logout(request: Request) -> RedirectResponse:
    """Clear session cookie and redirect to login."""
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(COOKIE_NAME)
    return response
