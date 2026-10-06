"""Billing, Invoices, and GST Tax compliance routes."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from ...config import PROJECT_ROOT
from ...db.connection import IST
from ..auth import TenantContext, get_current_tenant_context

router = APIRouter(prefix="/billing", tags=["Billing"])
templates = Jinja2Templates(directory=str(PROJECT_ROOT / "src" / "kirana" / "web" / "templates"))


def _query_bills(repo, query_str: str = "", payment_mode: str = "", limit: int = 50) -> list[dict[str, Any]]:
    """Retrieve bill history with optional customer/invoice search and payment mode filter."""
    sql = """
        SELECT b.*, c.name as customer_name, c.phone as customer_phone
        FROM bills b
        LEFT JOIN customers c ON c.id = b.customer_id
        WHERE 1=1
    """
    params: list[Any] = []

    if query_str:
        sql += " AND (b.invoice_no LIKE ? OR c.name LIKE ? OR b.customer_phone LIKE ?)"
        wildcard = f"%{query_str}%"
        params.extend([wildcard, wildcard, wildcard])

    if payment_mode:
        sql += " AND b.payment_mode = ?"
        params.append(payment_mode.lower())

    sql += " ORDER BY b.id DESC LIMIT ?"
    params.append(limit)

    return repo.db.query_all(sql, params)


@router.get("", response_class=HTMLResponse)
async def billing_page(
    request: Request,
    ctx: TenantContext = Depends(get_current_tenant_context),
) -> Any:
    """Render Billing History & GST Overview page."""
    repo = ctx.repo
    bills = _query_bills(repo, limit=50)

    # Calculate 30-day GST summary
    today = datetime.now(IST).date()
    start_30d = (today - timedelta(days=30)).isoformat()
    end_today = today.isoformat()
    gst_report = repo.gst_collected(start_30d, end_today)

    return templates.TemplateResponse(
        request=request,
        name="billing.html",
        context={
            "ctx": ctx,
            "active_page": "billing",
            "bills": bills,
            "gst_report": gst_report,
        },
    )


@router.get("/search", response_class=HTMLResponse)
async def search_bills(
    request: Request,
    q: str = Query("", alias="q"),
    mode: str = Query("", alias="mode"),
    ctx: TenantContext = Depends(get_current_tenant_context),
) -> Any:
    """Live search returning bill feed HTML table fragment."""
    bills = _query_bills(ctx.repo, query_str=q.strip(), payment_mode=mode.strip(), limit=50)
    return templates.TemplateResponse(
        request=request,
        name="partials/bill_feed.html",
        context={
            "bills": bills,
        },
    )


@router.get("/modal/receipt", response_class=HTMLResponse)
async def receipt_modal(
    request: Request,
    bill_id: int = Query(...),
    ctx: TenantContext = Depends(get_current_tenant_context),
) -> Any:
    """Render 58mm/80mm thermal receipt preview modal."""
    repo = ctx.repo
    bill_detail = repo.get_bill(bill_id)
    if not bill_detail:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bill not found")

    profile = repo.get_shop_profile()
    return templates.TemplateResponse(
        request=request,
        name="partials/receipt_modal.html",
        context={
            "bill": bill_detail.bill,
            "lines": bill_detail.lines,
            "profile": profile,
        },
    )


@router.get("/export/csv")
async def export_bills_csv(
    ctx: TenantContext = Depends(get_current_tenant_context),
) -> StreamingResponse:
    """Export finalized invoices to CSV."""
    bills = _query_bills(ctx.repo, limit=1000)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Bill ID",
        "Invoice Number",
        "Date",
        "Customer Name",
        "Customer Phone",
        "Cashier / Biller",
        "Payment Mode",
        "Taxable Subtotal (INR)",
        "CGST (INR)",
        "SGST (INR)",
        "Round-off (INR)",
        "Grand Total (INR)",
        "Status",
    ])

    for b in bills:
        writer.writerow([
            b["id"],
            b["invoice_no"] or "",
            b["created_at"],
            b["customer_name"] or "Walk-in",
            b["customer_phone"] or "",
            b["billed_by"] or "Owner",
            b["payment_mode"] or "",
            f"{b['subtotal_paise'] / 100:.2f}",
            f"{b['cgst_paise'] / 100:.2f}",
            f"{b['sgst_paise'] / 100:.2f}",
            f"{b['round_off_paise'] / 100:.2f}",
            f"{b['total_paise'] / 100:.2f}",
            b["status"],
        ])

    output.seek(0)
    filename = f"bills_{ctx.store_code}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/export/gst-csv")
async def export_gst_csv(
    start: str = Query(...),
    end: str = Query(...),
    ctx: TenantContext = Depends(get_current_tenant_context),
) -> StreamingResponse:
    """Export GST slab tax report to CSV."""
    report = ctx.repo.gst_collected(start, end)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["GST Slab Rate (%)", "Taxable Turnover (INR)", "CGST (INR)", "SGST (INR)", "Total GST (INR)"])

    for s in report.by_slab:
        writer.writerow([
            f"{s['gst_rate']}%",
            f"{s['taxable_paise'] / 100:.2f}",
            f"{s['cgst_paise'] / 100:.2f}",
            f"{s['sgst_paise'] / 100:.2f}",
            f"{(s['cgst_paise'] + s['sgst_paise']) / 100:.2f}",
        ])

    writer.writerow([
        "TOTAL",
        "—",
        f"{report.cgst_paise / 100:.2f}",
        f"{report.sgst_paise / 100:.2f}",
        f"{report.total_tax_paise / 100:.2f}",
    ])

    output.seek(0)
    filename = f"gst_report_{ctx.store_code}_{start}_{end}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
