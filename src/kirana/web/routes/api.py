"""JSON REST API endpoints (v1) for POS/mobile app integration and headless dashboard widgets."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from ..auth import TenantContext, get_current_tenant_context

router = APIRouter(prefix="/api/v1", tags=["REST API v1"])


@router.get("/summary", response_class=JSONResponse)
async def api_store_summary(
    ctx: TenantContext = Depends(get_current_tenant_context),
) -> dict[str, Any]:
    """Return live store operational snapshot (JSON)."""
    repo = ctx.repo
    daily = repo.daily_summary()
    health = repo.stock_health()
    debtors = repo.list_khata_debtors()

    return {
        "store_code": ctx.store_code,
        "store_name": ctx.store_name,
        "today_gross_paise": daily.gross_sales_paise,
        "today_gross_rupees": round(daily.gross_sales_paise / 100, 2),
        "today_bill_count": daily.bill_count,
        "cgst_paise": daily.cgst_paise,
        "sgst_paise": daily.sgst_paise,
        "payment_breakdown": daily.by_payment,
        "inventory": {
            "total_skus": health.total_skus,
            "low_stock_count": len(health.low_stock),
            "out_of_stock_count": len(health.out_of_stock),
            "retail_value_rupees": round(health.inventory_retail_value_paise / 100, 2),
            "cost_value_rupees": round(health.inventory_cost_value_paise / 100, 2),
        },
        "khata": {
            "total_outstanding_rupees": round(sum(d.balance_paise for d in debtors) / 100, 2),
            "debtor_count": len(debtors),
        },
    }


@router.get("/inventory", response_class=JSONResponse)
async def api_inventory_list(
    q: str = Query("", alias="q"),
    ctx: TenantContext = Depends(get_current_tenant_context),
) -> list[dict[str, Any]]:
    """Return catalog product list (JSON)."""
    repo = ctx.repo
    if q.strip():
        products = repo.search_products(q.strip(), limit=50)
    else:
        products = repo.list_inventory(active_only=True)

    return [
        {
            "id": p.id,
            "name": p.name,
            "brand": p.brand,
            "unit": p.unit,
            "is_loose": p.is_loose,
            "gst_rate": p.gst_rate,
            "hsn_code": p.hsn_code,
            "cost_rupees": round(p.cost_paise / 100, 2),
            "sell_rupees": round(p.sell_paise / 100, 2),
            "mrp_rupees": round(p.mrp_paise / 100, 2),
            "stock_display": p.stock_display,
            "reorder_display": p.reorder_display,
            "is_low_stock": p.stock_base <= p.reorder_base,
            "is_out_of_stock": p.stock_base == 0,
        }
        for p in products
    ]


@router.get("/khata", response_class=JSONResponse)
async def api_khata_list(
    ctx: TenantContext = Depends(get_current_tenant_context),
) -> list[dict[str, Any]]:
    """Return outstanding debtor accounts (JSON)."""
    debtors = ctx.repo.list_khata_debtors()
    return [
        {
            "customer_id": d.customer.id,
            "customer_name": d.customer.name,
            "customer_phone": d.customer.phone,
            "balance_rupees": round(d.balance_paise / 100, 2),
            "balance_paise": d.balance_paise,
            "last_activity": d.last_entry.created_at if d.last_entry else None,
        }
        for d in debtors
    ]
