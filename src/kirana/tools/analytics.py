"""Analytics tools — daily summary, sales reports, GST, and stock health.

Dates are 'YYYY-MM-DD' strings in IST. All amounts are formatted as ₹ strings
in the response payload; paise are kept internal.
"""

from __future__ import annotations

from .base import tool

from ..db.connection import today_iso
from ..domain import money
from ._helpers import arg, guard, ok
from .context import get_repo


@tool(
    "daily_summary",
    "Show today's (or a specific past date's) sales summary: bill count, gross sales, "
    "tax breakdown, payment split, and top-selling items. For today's sales, do NOT "
    "provide any date parameter (omit date so it automatically defaults to today in IST).",
    {
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "description": "Optional past date in YYYY-MM-DD (IST). Leave blank/omit for today's sales.",
            },
        },
        "required": [],
    },
)
@guard
async def daily_summary(args: dict) -> dict:
    date_raw = arg(args, "date", None)
    if not date_raw or str(date_raw).strip().lower() in ("today", "now", "current"):
        date = today_iso()
    else:
        date = str(date_raw).strip()

    repo = get_repo()

    # Guard against LLM cutoff hallucinating a past year (e.g. 2024 or 2025) when querying today
    if (date.startswith("2024-") or date.startswith("2025-")) and date != today_iso():
        r_past = repo.daily_summary(date)
        if r_past.bill_count == 0:
            r_today = repo.daily_summary(today_iso())
            if r_today.bill_count > 0:
                date = today_iso()
                r = r_today
            else:
                r = r_past
        else:
            r = r_past
    else:
        r = repo.daily_summary(date)

    payment_split = {
        mode: money.format_inr(paise)
        for mode, paise in r.by_payment.items()
    }
    top = [
        {
            "name": item.name,
            "qty": item.qty_display,
            "revenue": money.format_inr(item.revenue_paise),
        }
        for item in r.top_items
    ]

    summary = (
        f"{date}: {r.bill_count} bills, gross {money.format_inr(r.gross_sales_paise)}, "
        f"tax {money.format_inr(r.total_tax_paise)} "
        f"(CGST {money.format_inr(r.cgst_paise)} + SGST {money.format_inr(r.sgst_paise)})."
    )
    return ok(
        summary,
        date=date,
        bill_count=r.bill_count,
        gross_sales=money.format_inr(r.gross_sales_paise),
        total_tax=money.format_inr(r.total_tax_paise),
        cgst=money.format_inr(r.cgst_paise),
        sgst=money.format_inr(r.sgst_paise),
        payment_split=payment_split,
        top_items=top,
    )


@tool(
    "sales_report",
    "Generate a sales report for a date range. Returns bill count, gross sales, "
    "taxable amount, tax collected, payment split, and top items.",
    {
        "type": "object",
        "properties": {
            "start": {
                "type": "string",
                "description": "Start date in YYYY-MM-DD (IST), inclusive.",
            },
            "end": {
                "type": "string",
                "description": "End date in YYYY-MM-DD (IST), inclusive.",
            },
        },
        "required": ["start", "end"],
    },
)
@guard
async def sales_report(args: dict) -> dict:
    start: str = args["start"]
    end: str = args["end"]
    repo = get_repo()
    r = repo.sales_report(start, end)

    payment_split = {
        mode: money.format_inr(paise)
        for mode, paise in r.by_payment.items()
    }
    top = [
        {
            "name": item.name,
            "qty": item.qty_display,
            "revenue": money.format_inr(item.revenue_paise),
        }
        for item in r.top_items
    ]

    summary = (
        f"{start} to {end}: {r.bill_count} bills, "
        f"gross {money.format_inr(r.gross_sales_paise)}, "
        f"taxable {money.format_inr(r.taxable_paise)}, "
        f"tax {money.format_inr(r.total_tax_paise)}."
    )
    return ok(
        summary,
        start=start,
        end=end,
        bill_count=r.bill_count,
        gross_sales=money.format_inr(r.gross_sales_paise),
        taxable=money.format_inr(r.taxable_paise),
        total_tax=money.format_inr(r.total_tax_paise),
        payment_split=payment_split,
        top_items=top,
    )


@tool(
    "top_items",
    "List the best-selling items by revenue for a date range. Useful for "
    "'what sold most this week?' or identifying fast-moving products.",
    {
        "type": "object",
        "properties": {
            "start": {
                "type": "string",
                "description": "Start date in YYYY-MM-DD (IST), inclusive.",
            },
            "end": {
                "type": "string",
                "description": "End date in YYYY-MM-DD (IST), inclusive.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of items to return. Defaults to 10.",
            },
        },
        "required": ["start", "end"],
    },
)
@guard
async def top_items(args: dict) -> dict:
    start: str = args["start"]
    end: str = args["end"]
    limit: int = int(arg(args, "limit", 10))
    repo = get_repo()
    items = repo.top_items(start, end, limit=limit)

    items_out = [
        {
            "product_id": item.product_id,
            "name": item.name,
            "qty": item.qty_display,
            "revenue": money.format_inr(item.revenue_paise),
            "bills": item.count,
        }
        for item in items
    ]

    summary = (
        f"Top {len(items_out)} items from {start} to {end}."
        if items_out
        else f"No sales found from {start} to {end}."
    )
    return ok(summary, items=items_out)


@tool(
    "gst_collected",
    "Show GST collected for a date range, broken down by tax slab. "
    "Use for 'GST summary', 'GSTR filing', or monthly tax calculations.",
    {
        "type": "object",
        "properties": {
            "start": {
                "type": "string",
                "description": "Start date in YYYY-MM-DD (IST), inclusive.",
            },
            "end": {
                "type": "string",
                "description": "End date in YYYY-MM-DD (IST), inclusive.",
            },
        },
        "required": ["start", "end"],
    },
)
@guard
async def gst_collected(args: dict) -> dict:
    start: str = args["start"]
    end: str = args["end"]
    repo = get_repo()
    r = repo.gst_collected(start, end)

    by_slab = [
        {
            "rate": f"{slab['rate']}%",
            "taxable": money.format_inr(slab["taxable_paise"]),
            "cgst": money.format_inr(slab["cgst_paise"]),
            "sgst": money.format_inr(slab["sgst_paise"]),
        }
        for slab in r.by_slab
    ]

    summary = (
        f"GST {start} to {end}: CGST {money.format_inr(r.cgst_paise)}, "
        f"SGST {money.format_inr(r.sgst_paise)}, "
        f"total {money.format_inr(r.total_tax_paise)}."
    )
    return ok(
        summary,
        start=start,
        end=end,
        by_slab=by_slab,
        cgst=money.format_inr(r.cgst_paise),
        sgst=money.format_inr(r.sgst_paise),
        total_tax=money.format_inr(r.total_tax_paise),
    )


@tool(
    "stock_health",
    "Show a snapshot of inventory health: total SKUs, out-of-stock items, "
    "low-stock items, and inventory value at cost and retail price. "
    "Use for 'stock status', 'what's out of stock?', or 'inventory value?'.",
    {
        "type": "object",
        "properties": {},
        "required": [],
    },
)
@guard
async def stock_health(args: dict) -> dict:
    repo = get_repo()
    r = repo.stock_health()

    out_of_stock = [
        {"id": p.id, "name": p.name, "unit": p.unit}
        for p in r.out_of_stock
    ]
    low_stock = [
        {
            "id": p.id,
            "name": p.name,
            "in_stock": p.stock_display,
            "reorder_at": p.reorder_display,
        }
        for p in r.low_stock
    ]

    summary = (
        f"Inventory: {r.total_skus} SKUs, "
        f"{len(r.out_of_stock)} out of stock, "
        f"{len(r.low_stock)} low stock. "
        f"Cost value {money.format_inr(r.inventory_cost_value_paise)}, "
        f"retail value {money.format_inr(r.inventory_retail_value_paise)}."
    )
    return ok(
        summary,
        total_skus=r.total_skus,
        out_of_stock_count=len(r.out_of_stock),
        low_stock_count=len(r.low_stock),
        out_of_stock=out_of_stock,
        low_stock=low_stock,
        inventory_cost_value=money.format_inr(r.inventory_cost_value_paise),
        inventory_retail_value=money.format_inr(r.inventory_retail_value_paise),
    )


ANALYTICS_TOOLS = [daily_summary, sales_report, top_items, gst_collected, stock_health]
