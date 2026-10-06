"""Store Profile & Settings routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ...config import PROJECT_ROOT
from ..auth import TenantContext, get_current_tenant_context

router = APIRouter(prefix="/settings", tags=["Settings"])
templates = Jinja2Templates(directory=str(PROJECT_ROOT / "src" / "kirana" / "web" / "templates"))


@router.get("", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    ctx: TenantContext = Depends(get_current_tenant_context),
) -> Any:
    """Render Store Profile and Preferences page."""
    profile = ctx.repo.get_shop_profile()
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "ctx": ctx,
            "active_page": "settings",
            "profile": profile,
            "success_msg": None,
            "error_msg": None,
        },
    )


@router.post("/profile", response_class=HTMLResponse)
async def update_profile_action(
    request: Request,
    name: str = Form(...),
    address: str = Form(...),
    phone: str | None = Form(None),
    gstin: str | None = Form(None),
    invoice_prefix: str = Form("INV"),
    ctx: TenantContext = Depends(get_current_tenant_context),
) -> Any:
    """Update shop profile details."""
    repo = ctx.repo
    clean_gstin = gstin.strip().upper() if gstin and gstin.strip() else None
    clean_phone = phone.strip() if phone and phone.strip() else None

    profile = repo.set_shop_profile(
        name=name.strip(),
        address=address.strip(),
        phone=clean_phone,
        gstin=clean_gstin,
        invoice_prefix=invoice_prefix.strip().upper(),
    )

    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "ctx": ctx,
            "active_page": "settings",
            "profile": profile,
            "success_msg": "Store profile updated successfully!",
            "error_msg": None,
        },
    )


@router.post("/password", response_class=HTMLResponse)
async def change_pin_action(
    request: Request,
    current_pin: str = Form(...),
    new_pin: str = Form(...),
    ctx: TenantContext = Depends(get_current_tenant_context),
) -> Any:
    """Change owner security PIN with current PIN verification."""
    repo = ctx.repo
    profile = repo.get_shop_profile()

    if not repo.verify_owner_password(current_pin.strip()):
        return templates.TemplateResponse(
            request=request,
            name="settings.html",
            context={
                "ctx": ctx,
                "active_page": "settings",
                "profile": profile,
                "success_msg": None,
                "error_msg": "Current PIN verification failed.",
            },
        )

    if len(new_pin.strip()) < 4:
        return templates.TemplateResponse(
            request=request,
            name="settings.html",
            context={
                "ctx": ctx,
                "active_page": "settings",
                "profile": profile,
                "success_msg": None,
                "error_msg": "New PIN must be at least 4 digits.",
            },
        )

    repo.set_owner_password(new_pin.strip())
    updated_profile = repo.get_shop_profile()

    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "ctx": ctx,
            "active_page": "settings",
            "profile": updated_profile,
            "success_msg": "Owner Security PIN changed successfully!",
            "error_msg": None,
        },
    )
