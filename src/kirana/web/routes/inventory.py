"""Inventory & Catalog routes with live HTMX search, filtering, and restock modals."""

from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from ...config import PROJECT_ROOT
from ...domain.money import to_paise
from ...domain.units import to_base
from ..auth import TenantContext, get_current_tenant_context

router = APIRouter(prefix="/inventory", tags=["Inventory"])
templates = Jinja2Templates(directory=str(PROJECT_ROOT / "src" / "kirana" / "web" / "templates"))


@router.get("", response_class=HTMLResponse)
async def inventory_page(
    request: Request,
    ctx: TenantContext = Depends(get_current_tenant_context),
) -> Any:
    """Render full Inventory Management view."""
    repo = ctx.repo
    products = repo.list_inventory(active_only=True)
    health = repo.stock_health()

    return templates.TemplateResponse(
        request=request,
        name="inventory.html",
        context={
            "ctx": ctx,
            "active_page": "inventory",
            "products": products,
            "total_skus": health.total_skus,
            "low_stock_count": len(health.low_stock),
            "out_of_stock_count": len(health.out_of_stock),
            "stock_retail_value_display": f"{health.inventory_retail_value_paise / 100:.2f}",
        },
    )


@router.get("/search", response_class=HTMLResponse)
async def search_inventory(
    request: Request,
    q: str = Query("", alias="q"),
    filter_type: str = Query("all", alias="filter"),
    ctx: TenantContext = Depends(get_current_tenant_context),
) -> Any:
    """Live search and filter returning HTML table fragment."""
    repo = ctx.repo
    clean_q = q.strip()

    if clean_q:
        products = repo.search_products(clean_q, limit=50)
    elif filter_type == "low":
        products = repo.list_low_stock()
    elif filter_type == "out":
        all_prods = repo.list_inventory(active_only=True)
        products = [p for p in all_prods if p.stock_base == 0]
    else:
        products = repo.list_inventory(active_only=True)

    return templates.TemplateResponse(
        request=request,
        name="partials/inventory_table.html",
        context={
            "products": products,
        },
    )


@router.get("/modal/inward", response_class=HTMLResponse)
async def stock_inward_modal(
    request: Request,
    product_id: int = Query(...),
    ctx: TenantContext = Depends(get_current_tenant_context),
) -> Any:
    """Render stock inward modal dialog for product."""
    product = ctx.repo.get_product(product_id)
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")

    return templates.TemplateResponse(
        request=request,
        name="partials/stock_inward_modal.html",
        context={
            "product": product,
        },
    )


@router.post("/receive", response_class=HTMLResponse)
async def receive_stock_action(
    request: Request,
    product_id: int = Form(...),
    qty: float = Form(...),
    cost_rupees: float | None = Form(None),
    sell_rupees: float | None = Form(None),
    expiry: str | None = Form(None),
    note: str | None = Form(None),
    ctx: TenantContext = Depends(get_current_tenant_context),
) -> Any:
    """Process stock inward and return updated table partial."""
    repo = ctx.repo
    product = repo.get_product(product_id)
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")

    qty_base = to_base(qty, None, product.unit)
    cost_paise = to_paise(cost_rupees) if cost_rupees is not None else None
    sell_paise = to_paise(sell_rupees) if sell_rupees is not None else None

    clean_expiry = expiry.strip() if expiry and expiry.strip() else None
    clean_note = note.strip() if note and note.strip() else None

    repo.receive_stock(
        product_id=product_id,
        qty_base=qty_base,
        cost_paise=cost_paise,
        sell_paise=sell_paise,
        expiry=clean_expiry,
        note=clean_note,
    )

    products = repo.list_inventory(active_only=True)
    return templates.TemplateResponse(
        request=request,
        name="partials/inventory_table.html",
        context={
            "products": products,
        },
    )


@router.get("/export/csv")
async def export_inventory_csv(
    ctx: TenantContext = Depends(get_current_tenant_context),
) -> StreamingResponse:
    """Generate and stream CSV catalog export."""
    products = ctx.repo.list_inventory(active_only=True)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Product ID",
        "Name",
        "Brand",
        "Unit",
        "GST Rate (%)",
        "HSN Code",
        "Cost Price (INR)",
        "Sell Price (INR)",
        "MRP (INR)",
        "Current Stock",
        "Reorder Level",
        "Is Loose",
    ])

    for p in products:
        writer.writerow([
            p.id,
            p.name,
            p.brand or "",
            p.unit,
            p.gst_rate,
            p.hsn_code or "",
            f"{p.cost_paise / 100:.2f}",
            f"{p.sell_paise / 100:.2f}",
            f"{p.mrp_paise / 100:.2f}",
            p.stock_display,
            p.reorder_display,
            "Yes" if p.is_loose else "No",
        ])

    output.seek(0)
    filename = f"inventory_{ctx.store_code}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
