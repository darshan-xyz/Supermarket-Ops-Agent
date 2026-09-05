"""Inventory management tools: add products, receive stock, search, and check levels.

These are thin adapters over the repository. All business invariants (GST validation,
stock checks, price guards) live in the repository — tools just parse args, convert
units/money, delegate, and return compact summaries.
"""

from __future__ import annotations


from .base import tool

from ..db.repository import ProductNotFound
from ..domain import money
from ..domain.units import to_base
from ._helpers import arg, guard, ok
from .context import get_repo


@tool(
    "add_product",
    "Add a new product to the kirana store catalogue. Use when the owner introduces a new item. "
    "Captures name, unit, GST slab, pricing, optional brand/HSN/aliases, and opening stock.",
    {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Product name (e.g. 'Tata Salt')."},
            "unit": {
                "type": "string",
                "enum": ["kg", "g", "litre", "ml", "packet", "piece", "dozen"],
                "description": "Selling unit of the product.",
            },
            "gst_rate": {
                "type": "integer",
                "enum": [0, 5, 12, 18, 28],
                "description": "GST slab percentage.",
            },
            "mrp_rupees": {"type": "number", "description": "Maximum retail price in rupees."},
            "sell_rupees": {"type": "number", "description": "Actual selling price in rupees."},
            "cost_rupees": {"type": "number", "description": "Purchase/cost price in rupees (optional)."},
            "brand": {"type": "string", "description": "Brand name (optional)."},
            "hsn_code": {"type": "string", "description": "HSN code for GST (optional)."},
            "is_loose": {
                "type": "boolean",
                "description": "True if sold loose/by weight (e.g. loose sugar). Default false.",
            },
            "aliases": {
                "type": "string",
                "description": "Comma-separated aliases/hinglish names (e.g. 'namak,tata namak').",
            },
            "opening_stock_qty": {
                "type": "number",
                "description": "Opening stock quantity in the product's own unit.",
            },
            "reorder_qty": {
                "type": "number",
                "description": "Reorder level quantity in the product's own unit.",
            },
            "expiry": {
                "type": "string",
                "description": "Expiry date of the opening stock as YYYY-MM-DD (optional, for perishables).",
            },
        },
        "required": ["name", "unit", "gst_rate", "mrp_rupees", "sell_rupees"],
    },
)
@guard
async def add_product(args: dict) -> dict:
    repo = get_repo()
    name: str = args["name"]
    unit: str = args["unit"]
    gst_rate: int = args["gst_rate"]
    mrp_paise = money.to_paise(args["mrp_rupees"])
    sell_paise = money.to_paise(args["sell_rupees"])

    cost_raw = arg(args, "cost_rupees")
    cost_paise = money.to_paise(cost_raw) if cost_raw is not None else 0

    opening_raw = arg(args, "opening_stock_qty", 0)
    opening_base = to_base(opening_raw, unit, unit) if opening_raw else 0

    reorder_raw = arg(args, "reorder_qty", 0)
    reorder_base = to_base(reorder_raw, unit, unit) if reorder_raw else 0

    product = repo.add_product(
        name=name,
        unit=unit,
        gst_rate=gst_rate,
        mrp_paise=mrp_paise,
        sell_paise=sell_paise,
        cost_paise=cost_paise,
        brand=arg(args, "brand"),
        hsn_code=arg(args, "hsn_code"),
        is_loose=bool(arg(args, "is_loose", False)),
        aliases=arg(args, "aliases"),
        opening_stock_base=opening_base,
        reorder_base=reorder_base,
        expiry=arg(args, "expiry"),
    )
    return ok(
        f"Added '{product.name}'. Stock: {product.stock_display}. "
        f"Sell: {money.format_inr(product.sell_paise)}, MRP: {money.format_inr(product.mrp_paise)}.",
        product_id=product.id,
        name=product.name,
        unit=product.unit,
        stock=product.stock_display,
        sell=money.format_inr(product.sell_paise),
        mrp=money.format_inr(product.mrp_paise),
        gst_rate=product.gst_rate,
    )


@tool(
    "receive_stock",
    "Record stock arriving into inventory. Use when the owner says items 'came in', 'arrived', "
    "'received', or is restocking. Optionally update cost/MRP/sell price on receipt.",
    {
        "type": "object",
        "properties": {
            "product_id": {"type": "integer", "description": "ID of the product being restocked."},
            "quantity": {"type": "number", "description": "Quantity received."},
            "unit": {
                "type": "string",
                "enum": ["kg", "g", "litre", "ml", "packet", "piece", "dozen"],
                "description": "Unit for the received quantity. Defaults to product's own unit.",
            },
            "cost_rupees": {"type": "number", "description": "New cost/purchase price per unit in rupees (optional)."},
            "mrp_rupees": {"type": "number", "description": "New MRP per unit in rupees (optional)."},
            "sell_rupees": {"type": "number", "description": "New selling price per unit in rupees (optional)."},
            "note": {"type": "string", "description": "Optional note (e.g. supplier name, batch)."},
            "expiry": {"type": "string", "description": "Expiry date of this batch as YYYY-MM-DD (optional, for perishables)."},
        },
        "required": ["product_id", "quantity"],
    },
)
@guard
async def receive_stock(args: dict) -> dict:
    repo = get_repo()
    product_id: int = args["product_id"]
    quantity = args["quantity"]
    input_unit: str | None = arg(args, "unit")

    # Fetch product first to resolve its unit for quantity conversion.
    product = repo.get_product(product_id)
    if product is None:
        raise ProductNotFound(f"No product with id {product_id}.")

    qty_base = to_base(quantity, input_unit, product.unit)

    cost_raw = arg(args, "cost_rupees")
    mrp_raw = arg(args, "mrp_rupees")
    sell_raw = arg(args, "sell_rupees")

    updated = repo.receive_stock(
        product_id,
        qty_base,
        cost_paise=money.to_paise(cost_raw) if cost_raw is not None else None,
        mrp_paise=money.to_paise(mrp_raw) if mrp_raw is not None else None,
        sell_paise=money.to_paise(sell_raw) if sell_raw is not None else None,
        note=arg(args, "note"),
        expiry=arg(args, "expiry"),
    )
    return ok(
        f"Received stock for '{updated.name}'. New stock: {updated.stock_display}.",
        product_id=updated.id,
        name=updated.name,
        stock=updated.stock_display,
        sell=money.format_inr(updated.sell_paise),
        mrp=money.format_inr(updated.mrp_paise),
    )


@tool(
    "search_products",
    "Search the product catalogue by name, brand, alias, or SKU. "
    "Use to resolve ambiguity before billing or stock operations.",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search term (name, brand, alias, SKU)."},
            "limit": {"type": "integer", "description": "Max results to return (default 10)."},
        },
        "required": ["query"],
    },
)
@guard
async def search_products(args: dict) -> dict:
    repo = get_repo()
    query: str = args["query"]
    limit: int = arg(args, "limit", 10)

    results = repo.search_products(query, limit)
    if not results:
        return ok(
            f"No matching product found for '{query}'. "
            "Try a different name, brand, or alias.",
            candidates=[],
        )
    candidates = [p.summary() for p in results]
    return ok(
        f"Found {len(candidates)} product(s) matching '{query}'.",
        candidates=candidates,
    )


@tool(
    "get_stock",
    "Get current stock level for a specific product. Use when asked 'how much X do we have' "
    "or before a big sale to confirm availability.",
    {
        "type": "object",
        "properties": {
            "product_id": {
                "type": "integer",
                "description": "ID of the product to check stock for.",
            }
        },
        "required": ["product_id"],
    },
)
@guard
async def get_stock(args: dict) -> dict:
    repo = get_repo()
    product_id: int = args["product_id"]

    info = repo.get_stock(product_id)
    status = "out of stock" if info.is_out else ("low stock" if info.is_low else "in stock")
    return ok(
        f"'{info.product.name}': {info.stock_display} ({status}).",
        product_id=info.product.id,
        name=info.product.name,
        stock=info.stock_display,
        is_low=info.is_low,
        is_out=info.is_out,
        reorder_level=info.product.reorder_display,
    )


@tool(
    "list_low_stock",
    "List all products that are at or below their reorder level. "
    "Use when the owner asks what needs to be ordered or restocked.",
    {
        "type": "object",
        "properties": {},
        "required": [],
    },
)
@guard
async def list_low_stock(args: dict) -> dict:
    repo = get_repo()
    products = repo.list_low_stock()
    if not products:
        return ok("All products are well-stocked. Nothing needs reordering right now.", items=[])
    items = [
        {
            "id": p.id,
            "name": p.name,
            "stock": p.stock_display,
            "reorder_level": p.reorder_display,
        }
        for p in products
    ]
    names = ", ".join(p.name for p in products[:5])
    suffix = f" and {len(products) - 5} more" if len(products) > 5 else ""
    return ok(
        f"{len(products)} product(s) need restocking: {names}{suffix}.",
        items=items,
    )


@tool(
    "reorder_suggestions",
    "Get prioritised reorder suggestions based on sales velocity and current stock. "
    "Use when the owner asks 'what should I order?' or 'what will run out soon?'.",
    {
        "type": "object",
        "properties": {
            "cover_days": {
                "type": "integer",
                "description": "Number of days of stock cover to target (default 7).",
            },
        },
        "required": [],
    },
)
@guard
async def reorder_suggestions(args: dict) -> dict:
    repo = get_repo()
    cover_days: int = arg(args, "cover_days", 7)

    suggestions = repo.reorder_suggestions(cover_days)
    if not suggestions:
        return ok(
            f"No reorder needed — all products have at least {cover_days} days of stock cover.",
            suggestions=[],
        )
    items = [
        {
            "id": s.product.id,
            "name": s.product.name,
            "current_stock": s.product.stock_display,
            "suggest_qty": s.suggested_qty_display,
            "reason": s.reason,
        }
        for s in suggestions
    ]
    names = ", ".join(s.product.name for s in suggestions[:5])
    suffix = f" and {len(suggestions) - 5} more" if len(suggestions) > 5 else ""
    return ok(
        f"Reorder {len(suggestions)} product(s): {names}{suffix}.",
        cover_days=cover_days,
        suggestions=items,
    )


@tool(
    "list_expiring",
    "List stock batches expiring soon (or already expired), earliest first. Use when the "
    "owner asks what's about to expire, or during a daily/weekly review. FEFO means the "
    "oldest batch sells first automatically.",
    {
        "type": "object",
        "properties": {
            "within_days": {
                "type": "integer",
                "description": "Look-ahead window in days (default 7). Includes already-expired stock.",
            },
        },
        "required": [],
    },
)
@guard
async def list_expiring(args: dict) -> dict:
    repo = get_repo()
    within_days: int = arg(args, "within_days", 7)
    batches = repo.expiring_soon(within_days)
    if not batches:
        return ok(f"Nothing expiring within {within_days} days.", items=[])
    items = [
        {
            "name": b.name,
            "qty": b.qty_display,
            "expiry": b.expiry,
            "status": "EXPIRED" if b.days_left < 0 else f"{b.days_left} day(s) left",
        }
        for b in batches
    ]
    return ok(f"{len(batches)} batch(es) expiring within {within_days} days.", items=items)


INVENTORY_TOOLS = [
    add_product,
    receive_stock,
    search_products,
    get_stock,
    list_low_stock,
    reorder_suggestions,
    list_expiring,
]
