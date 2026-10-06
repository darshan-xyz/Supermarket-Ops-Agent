"""Store Pulse Dashboard routes with real-time KPI aggregations."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ...config import PROJECT_ROOT
from ..auth import TenantContext, get_current_tenant_context

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])
templates = Jinja2Templates(directory=str(PROJECT_ROOT / "src" / "kirana" / "web" / "templates"))


def _compute_dashboard_metrics(ctx: TenantContext) -> dict[str, Any]:
    repo = ctx.repo

    # 1. Today's operational summary
    daily = repo.daily_summary()
    today_gross_rupees = daily.gross_sales_paise / 100
    today_cgst_rupees = daily.cgst_paise / 100
    today_sgst_rupees = daily.sgst_paise / 100

    today_aov = 0.0
    if daily.bill_count > 0:
        today_aov = (daily.gross_sales_paise / daily.bill_count) / 100

    # 2. Payment split
    cash_paise = daily.by_payment.get("cash", 0)
    upi_paise = daily.by_payment.get("upi", 0)
    khata_paise = daily.by_payment.get("khata", 0)
    total_paid_paise = max(1, cash_paise + upi_paise + khata_paise)

    payment_split = {
        "cash_display": f"{cash_paise / 100:.2f}",
        "cash_pct": int((cash_paise / total_paid_paise) * 100),
        "upi_display": f"{upi_paise / 100:.2f}",
        "upi_pct": int((upi_paise / total_paid_paise) * 100),
        "khata_display": f"{khata_paise / 100:.2f}",
        "khata_pct": int((khata_paise / total_paid_paise) * 100),
    }

    # 3. Khata receivables
    debtors = repo.list_khata_debtors()
    total_khata_paise = sum(d.balance_paise for d in debtors)

    # 4. Inventory health snapshot
    health = repo.stock_health()

    # 5. Top selling items today
    top_items_formatted = [
        {
            "name": item.name,
            "qty_display": item.qty_display,
            "revenue_display": f"{item.revenue_paise / 100:.2f}",
        }
        for item in daily.top_items
    ]

    return {
        "today_gross_display": f"{today_gross_rupees:.2f}",
        "today_cgst_display": f"{today_cgst_rupees:.2f}",
        "today_sgst_display": f"{today_sgst_rupees:.2f}",
        "today_bill_count": daily.bill_count,
        "today_aov_display": f"{today_aov:.2f}",
        "total_khata_display": f"{total_khata_paise / 100:.2f}",
        "debtor_count": len(debtors),
        "stock_retail_value_display": f"{health.inventory_retail_value_paise / 100:.2f}",
        "stock_cost_value_display": f"{health.inventory_cost_value_paise / 100:.2f}",
        "low_stock_count": len(health.low_stock),
        "out_of_stock_count": len(health.out_of_stock),
        "payment_split": payment_split,
        "top_items": top_items_formatted,
        "low_stock_items": health.low_stock,
    }


@router.get("", response_class=HTMLResponse)
async def dashboard_view(
    request: Request,
    ctx: TenantContext = Depends(get_current_tenant_context),
) -> Any:
    """Render full Store Pulse Dashboard page."""
    metrics = _compute_dashboard_metrics(ctx)
    expiring = ctx.repo.expiring_soon(within_days=7)

    context = {
        "ctx": ctx,
        "active_page": "dashboard",
        "expiring_batches": expiring,
        **metrics,
    }
    return templates.TemplateResponse(request=request, name="dashboard.html", context=context)


@router.get("/partials/cards", response_class=HTMLResponse)
async def dashboard_cards_partial(
    request: Request,
    ctx: TenantContext = Depends(get_current_tenant_context),
) -> Any:
    """Return HTMX partial for auto-refreshing KPI cards."""
    metrics = _compute_dashboard_metrics(ctx)
    context = {
        "ctx": ctx,
        **metrics,
    }
    return templates.TemplateResponse(request=request, name="partials/dashboard_cards.html", context=context)
