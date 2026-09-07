"""Inventory management tools: add products, receive stock, search, and check levels.

These are thin adapters over the repository. All business invariants (GST validation,
stock checks, price guards) live in the repository — tools just parse args, convert
units/money, delegate, and return compact summaries.
"""

from __future__ import annotations


from .base import tool

from ..db.repository import ProductNotFound
from ..domain import money
from ..domain.units import format_qty, from_base, to_base
from ._helpers import arg, err, guard, ok
from .context import current_chat, get_repo


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
    "'received', or is restocking. Optionally update cost/MRP/sell price on receipt. "
    "If receiving quantity is above 1000, ask the owner 'The order seems to be bulk, is this the right quantity?' "
    "and only proceed with confirm_bulk=true once the owner confirms.",
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
            "confirm_bulk": {
                "type": "boolean",
                "description": "Set to true only after the owner confirms receiving a bulk quantity above 1000.",
            },
        },
        "required": ["product_id", "quantity"],
    },
)
@guard
async def receive_stock(args: dict) -> dict:
    repo = get_repo()
    product_id: int = args["product_id"]
    quantity = args["quantity"]
    if quantity <= 0:
        return err("Received quantity must be strictly positive.", code="invalid_quantity")
    input_unit: str | None = arg(args, "unit")

    # Fetch product first to resolve its unit for quantity conversion.
    product = repo.get_product(product_id)
    if product is None:
        raise ProductNotFound(f"No product with id {product_id}.")

    qty_base = to_base(quantity, input_unit, product.unit)
    qty_in_prod_unit = from_base(qty_base, product.unit)

    # Check if quantity is above 1000 (excluding grams/ml unless converted product units > 1000).
    is_bulk = (quantity > 1000 if input_unit not in ("g", "ml") else False) or (qty_in_prod_unit > 1000)
    confirm_bulk = bool(arg(args, "confirm_bulk", False))

    if is_bulk and not confirm_bulk:
        qty_display = format_qty(qty_base, product.unit)
        return err(
            f"The order seems to be bulk, is this the right quantity? "
            f"(Attempting to receive {qty_display} of '{product.name}'). "
            "No changes were made to inventory. Ask the owner for confirmation before making any changes, "
            "and pass confirm_bulk=true only after the owner confirms.",
            code="bulk_confirmation_required",
        )

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
    "update_product",
    "Update an existing product's catalog details such as selling price, MRP, cost price, "
    "reorder level, brand, aliases, or GST slab. Use when supplier prices change, retail prices are updated, "
    "or product catalog details need correction without receiving fresh stock.",
    {
        "type": "object",
        "properties": {
            "product_id": {"type": "integer", "description": "ID of the product to update."},
            "name": {"type": "string", "description": "Updated product name (optional)."},
            "sell_rupees": {"type": "number", "description": "New selling price in rupees (optional)."},
            "mrp_rupees": {"type": "number", "description": "New MRP in rupees (optional)."},
            "cost_rupees": {"type": "number", "description": "New cost/purchase price in rupees (optional)."},
            "reorder_qty": {"type": "number", "description": "New reorder level quantity in the product's unit (optional)."},
            "brand": {"type": "string", "description": "Updated brand name (optional)."},
            "aliases": {"type": "string", "description": "Updated comma-separated aliases/search terms (optional)."},
            "hsn_code": {"type": "string", "description": "Updated HSN code (optional)."},
            "gst_rate": {
                "type": "integer",
                "enum": [0, 5, 12, 18, 28],
                "description": "Updated GST slab percentage (optional).",
            },
        },
        "required": ["product_id"],
    },
)
@guard
async def update_product(args: dict) -> dict:
    repo = get_repo()
    product_id: int = args["product_id"]
    product = repo.get_product(product_id)
    if product is None:
        raise ProductNotFound(f"No product with id {product_id}.")

    sell_raw = arg(args, "sell_rupees")
    mrp_raw = arg(args, "mrp_rupees")
    cost_raw = arg(args, "cost_rupees")
    reorder_raw = arg(args, "reorder_qty")

    reorder_base = to_base(reorder_raw, product.unit, product.unit) if reorder_raw is not None else None

    updated = repo.update_product(
        product_id,
        name=arg(args, "name"),
        sell_paise=money.to_paise(sell_raw) if sell_raw is not None else None,
        mrp_paise=money.to_paise(mrp_raw) if mrp_raw is not None else None,
        cost_paise=money.to_paise(cost_raw) if cost_raw is not None else None,
        reorder_base=reorder_base,
        brand=arg(args, "brand"),
        aliases=arg(args, "aliases"),
        hsn_code=arg(args, "hsn_code"),
        gst_rate=arg(args, "gst_rate"),
    )

    changes = []
    if sell_raw is not None:
        changes.append(f"Selling price: {money.format_inr(updated.sell_paise)}")
    if mrp_raw is not None:
        changes.append(f"MRP: {money.format_inr(updated.mrp_paise)}")
    if cost_raw is not None:
        changes.append(f"Cost: {money.format_inr(updated.cost_paise)}")
    if reorder_raw is not None:
        changes.append(f"Reorder level: {updated.reorder_display}")
    if arg(args, "name") is not None:
        changes.append(f"Name: '{updated.name}'")

    change_desc = f" ({', '.join(changes)})" if changes else ""
    return ok(
        f"Updated product '{updated.name}'{change_desc}.",
        product_id=updated.id,
        name=updated.name,
        stock=updated.stock_display,
        sell=money.format_inr(updated.sell_paise),
        mrp=money.format_inr(updated.mrp_paise),
        cost=money.format_inr(updated.cost_paise),
        reorder_level=updated.reorder_display,
    )


@tool(
    "adjust_stock",
    "Adjust stock levels of a product due to physical audit count, damage, spoilage, shrinkage, "
    "expiry disposal, or internal store consumption. Supports both absolute count ('set') and relative adjustment ('reduce' or 'add'). "
    "NEVER use receive_stock to reduce quantities; ALWAYS use adjust_stock.",
    {
        "type": "object",
        "properties": {
            "product_id": {"type": "integer", "description": "ID of the product to adjust."},
            "quantity": {"type": "number", "description": "Quantity to adjust (positive number)."},
            "unit": {
                "type": "string",
                "enum": ["kg", "g", "litre", "ml", "packet", "piece", "dozen"],
                "description": "Unit for the quantity. Defaults to product's own unit.",
            },
            "mode": {
                "type": "string",
                "enum": ["reduce", "set", "add"],
                "description": "Adjustment mode: 'reduce' to deduct stock (damage/spoilage/waste), 'set' to set absolute physical stock count from audit, 'add' to increase stock (audit surplus). Default is 'reduce'.",
            },
            "reason": {
                "type": "string",
                "enum": ["damage", "spoilage", "audit_correction", "theft", "internal_use", "expired", "other"],
                "description": "Reason for the stock adjustment (e.g. 'damage', 'spoilage', 'audit_correction').",
            },
            "note": {"type": "string", "description": "Optional notes explaining the adjustment."},
        },
        "required": ["product_id", "quantity"],
    },
)
@guard
async def adjust_stock(args: dict) -> dict:
    repo = get_repo()
    product_id: int = args["product_id"]
    quantity = args["quantity"]
    if quantity < 0:
        return err("Quantity must be a non-negative number.", code="invalid_quantity")

    product = repo.get_product(product_id)
    if product is None:
        raise ProductNotFound(f"No product with id {product_id}.")

    input_unit = arg(args, "unit")
    qty_base = to_base(quantity, input_unit, product.unit)
    mode = arg(args, "mode", "reduce")
    reason = arg(args, "reason", "audit_correction")
    note = arg(args, "note")

    if mode == "reduce":
        delta_base = -qty_base
    elif mode == "add":
        delta_base = qty_base
    elif mode == "set":
        delta_base = qty_base - product.stock_base
    else:
        return err(f"Invalid mode '{mode}'. Must be 'reduce', 'set', or 'add'.", code="invalid_mode")

    old_stock_display = product.stock_display
    updated = repo.adjust_stock(product_id, delta_base, reason=reason, note=note)

    delta_display = format_qty(abs(delta_base), product.unit)
    direction = "reduced by" if delta_base < 0 else ("increased by" if delta_base > 0 else "unchanged at")

    return ok(
        f"Adjusted stock for '{updated.name}': {direction} {delta_display} (reason: {reason}). "
        f"Previous: {old_stock_display} → New stock: {updated.stock_display}.",
        product_id=updated.id,
        name=updated.name,
        previous_stock=old_stock_display,
        stock=updated.stock_display,
        delta=format_qty(delta_base, product.unit),
        reason=reason,
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


@tool(
    "list_inventory",
    "List all available products in the store inventory along with their current stock quantities, units, "
    "and prices. ALWAYS use when the owner asks for store inventory, all products, current stock, "
    "stock list, or what is available in the store.",
    {
        "type": "object",
        "properties": {
            "include_out_of_stock": {
                "type": "boolean",
                "description": "Whether to include products with 0 stock. Default true.",
            },
        },
        "required": [],
    },
)
@guard
async def list_inventory(args: dict) -> dict:
    repo = get_repo()
    include_out = arg(args, "include_out_of_stock", True)
    products = repo.list_inventory()
    if not include_out:
        products = [p for p in products if p.stock_base > 0]

    health = repo.stock_health()

    if not products:
        return ok("Store inventory is empty. No products found in catalogue.", items=[], total_skus=0)

    # Build the health status header
    if len(health.out_of_stock) == 0 and len(health.low_stock) == 0:
        status_line = "The store inventory is healthy:"
    else:
        status_line = "Store inventory status:"

    summary_lines = [
        status_line,
        "",
        f"• Total SKUs: {health.total_skus}",
        f"• Out of stock: {len(health.out_of_stock)}",
        f"• Low stock: {len(health.low_stock)}",
        f"• Total Cost Value: {money.format_inr(health.inventory_cost_value_paise)}",
        f"• Total Retail Value: {money.format_inr(health.inventory_retail_value_paise)}",
        "",
        "Here is the list of products currently in your store:",
        "",
    ]

    items = []
    for p in products:
        price_str = f"{money.format_inr(p.sell_paise)}/{p.unit}" if p.is_loose else money.format_inr(p.sell_paise)
        summary_lines.append(f"• {p.name} ({p.stock_display}) - {price_str}")
        items.append({
            "id": p.id,
            "name": p.name,
            "stock": p.stock_display,
            "unit": p.unit,
            "sell_price": money.format_inr(p.sell_paise),
            "mrp": money.format_inr(p.mrp_paise),
            "is_loose": p.is_loose,
            "is_low": 0 < p.stock_base <= p.reorder_base,
            "is_out": p.stock_base <= 0,
        })

    chat_id = current_chat.get()
    if chat_id:
        try:
            cart_view = repo.cart_view(chat_id)
            if cart_view.lines:
                pending = [f"{l.name} ({l.qty_display})" for l in cart_view.lines]
                summary_lines.append("")
                summary_lines.append(
                    f"⚠️ *Note: You have an open draft bill containing {len(cart_view.lines)} item(s) pending payment: "
                    f"{', '.join(pending)} (Total {money.format_inr(cart_view.grand_total_paise)}). "
                    f"These items will be deducted from inventory once finalized with payment.*"
                )
        except Exception:
            pass

    summary_msg = "\n".join(summary_lines)
    return ok(
        summary_msg,
        items=items,
        total_skus=health.total_skus,
        out_of_stock_count=len(health.out_of_stock),
        low_stock_count=len(health.low_stock),
        cost_value=money.format_inr(health.inventory_cost_value_paise),
        retail_value=money.format_inr(health.inventory_retail_value_paise),
    )


# Model alias for convenience
get_inventory = tool(
    "get_inventory",
    "Alias for list_inventory. Retrieve the complete list of store products and their stock quantities.",
    list_inventory.input_schema,
)(list_inventory.handler)


INVENTORY_TOOLS = [
    add_product,
    update_product,
    receive_stock,
    adjust_stock,
    search_products,
    get_stock,
    list_low_stock,
    list_inventory,
    get_inventory,
    reorder_suggestions,
    list_expiring,
]
