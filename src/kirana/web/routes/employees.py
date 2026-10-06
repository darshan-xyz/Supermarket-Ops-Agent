"""Staff & Employee Management routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ...config import PROJECT_ROOT
from ..auth import TenantContext, get_current_tenant_context

router = APIRouter(prefix="/employees", tags=["Employees"])
templates = Jinja2Templates(directory=str(PROJECT_ROOT / "src" / "kirana" / "web" / "templates"))


@router.get("", response_class=HTMLResponse)
async def employees_page(
    request: Request,
    ctx: TenantContext = Depends(get_current_tenant_context),
) -> Any:
    """Render staff roster management page."""
    employees = ctx.repo.list_employees(active_only=False)

    return templates.TemplateResponse(
        request=request,
        name="employees.html",
        context={
            "ctx": ctx,
            "active_page": "employees",
            "employees": employees,
        },
    )


@router.post("/add", response_class=HTMLResponse)
async def add_employee_action(
    request: Request,
    name: str = Form(...),
    role: str = Form("cashier"),
    phone: str | None = Form(None),
    ctx: TenantContext = Depends(get_current_tenant_context),
) -> Any:
    """Add employee and return refreshed table fragment."""
    repo = ctx.repo
    clean_name = name.strip()
    clean_phone = phone.strip() if phone and phone.strip() else None

    repo.add_employee(
        name=clean_name,
        role=role.strip().lower(),
        phone=clean_phone,
    )

    employees = repo.list_employees(active_only=False)
    return templates.TemplateResponse(
        request=request,
        name="partials/employee_table.html",
        context={
            "employees": employees,
        },
    )


@router.post("/deactivate", response_class=HTMLResponse)
async def deactivate_employee_action(
    request: Request,
    emp_code: str = Form(...),
    ctx: TenantContext = Depends(get_current_tenant_context),
) -> Any:
    """Deactivate employee and return refreshed table fragment."""
    repo = ctx.repo
    repo.remove_employee(emp_code.strip())

    employees = repo.list_employees(active_only=False)
    return templates.TemplateResponse(
        request=request,
        name="partials/employee_table.html",
        context={
            "employees": employees,
        },
    )
