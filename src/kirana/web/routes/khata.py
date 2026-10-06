"""Khata (Customer Credit Ledger) routes with 1-click WhatsApp reminders and ledger statements."""

from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from ...config import PROJECT_ROOT
from ...domain.money import to_paise
from ..auth import TenantContext, get_current_tenant_context

router = APIRouter(prefix="/khata", tags=["Khata"])
templates = Jinja2Templates(directory=str(PROJECT_ROOT / "src" / "kirana" / "web" / "templates"))


@router.get("", response_class=HTMLResponse)
async def khata_page(
    request: Request,
    ctx: TenantContext = Depends(get_current_tenant_context),
) -> Any:
    """Render Khata & Customer Credit Management page."""
    debtors = ctx.repo.list_khata_debtors()
    total_khata_paise = sum(d.balance_paise for d in debtors)

    return templates.TemplateResponse(
        request=request,
        name="khata.html",
        context={
            "ctx": ctx,
            "active_page": "khata",
            "debtors": debtors,
            "total_khata_display": f"{total_khata_paise / 100:.2f}",
            "store_name": ctx.store_name,
        },
    )


@router.get("/search", response_class=HTMLResponse)
async def search_debtors(
    request: Request,
    q: str = Query("", alias="q"),
    ctx: TenantContext = Depends(get_current_tenant_context),
) -> Any:
    """Live search for debtors by name or phone."""
    debtors = ctx.repo.list_khata_debtors()
    clean_q = q.strip().lower()

    if clean_q:
        debtors = [
            d for d in debtors
            if clean_q in d.customer.name.lower() or (d.customer.phone and clean_q in d.customer.phone)
        ]

    return templates.TemplateResponse(
        request=request,
        name="partials/debtor_list.html",
        context={
            "debtors": debtors,
            "store_name": ctx.store_name,
        },
    )


@router.get("/modal/statement", response_class=HTMLResponse)
async def customer_statement_modal(
    request: Request,
    customer_name: str = Query(...),
    ctx: TenantContext = Depends(get_current_tenant_context),
) -> Any:
    """Render ledger statement modal for customer."""
    repo = ctx.repo
    entries = repo.khata_statement(customer_name, limit=50)
    state = repo.khata_balance(customer_name)

    return templates.TemplateResponse(
        request=request,
        name="partials/customer_ledger.html",
        context={
            "customer_name": customer_name,
            "balance_paise": state.balance_paise,
            "entries": entries,
        },
    )


@router.get("/modal/payment", response_class=HTMLResponse)
async def payment_modal(
    request: Request,
    customer_name: str = Query(...),
    ctx: TenantContext = Depends(get_current_tenant_context),
) -> Any:
    """Render repayment modal for customer."""
    return templates.TemplateResponse(
        request=request,
        name="partials/payment_modal.html",
        context={
            "customer_name": customer_name,
        },
    )


@router.post("/payment", response_class=HTMLResponse)
async def record_payment_action(
    request: Request,
    customer_name: str = Form(...),
    amount_rupees: float = Form(...),
    note: str | None = Form(None),
    ctx: TenantContext = Depends(get_current_tenant_context),
) -> Any:
    """Process customer payment and return refreshed debtor table."""
    repo = ctx.repo
    amount_paise = to_paise(amount_rupees)
    clean_note = note.strip() if note and note.strip() else None

    repo.khata_record_payment(
        name=customer_name.strip(),
        amount_paise=amount_paise,
        note=clean_note,
        allow_overpay=True,
    )

    debtors = repo.list_khata_debtors()
    return templates.TemplateResponse(
        request=request,
        name="partials/debtor_list.html",
        context={
            "debtors": debtors,
            "store_name": ctx.store_name,
        },
    )


@router.get("/export/csv")
async def export_khata_csv(
    ctx: TenantContext = Depends(get_current_tenant_context),
) -> StreamingResponse:
    """Export customer credit balances to CSV."""
    debtors = ctx.repo.list_khata_debtors()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Customer Name", "Phone", "Outstanding Credit (INR)", "Last Activity Date"])

    for d in debtors:
        last_date = d.last_entry.created_at if d.last_entry else "N/A"
        writer.writerow([
            d.customer.name,
            d.customer.phone or "",
            f"{d.balance_paise / 100:.2f}",
            last_date,
        ])

    output.seek(0)
    filename = f"khata_{ctx.store_code}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
