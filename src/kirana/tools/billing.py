"""Billing tools: multi-turn cart building, edits, GST breakup, and an
idempotent finalize. These are thin adapters over the repository — every
invariant (oversell, below-cost, idempotency, khata integrity) lives there.

The cart is keyed by ``chat_id`` (a draft bill per chat). We never expose
``chat_id`` to the model; it is read from the ContextVar set by the session
runner.
"""

from __future__ import annotations

from .base import tool

from ..db.connection import today_iso
from ..db.repository import ProductNotFound
from ..domain import money
from ..domain.models import CartView
from ..domain.units import from_base, to_base
from ..domain.variants import extract_base_and_variant, get_sibling_variants, is_variant_specified_in_query
from .context import current_chat, current_user_message, get_repo
from ._helpers import arg, err, guard, ok


def _format_cart(view: CartView, note: str | None = None) -> dict:
    """Render a CartView as a bill summary with GST breakup.

    Used by every cart tool so the running total is always visible. Returns an
    ``ok(...)`` result; pass ``note`` to prefix a one-line confirmation.
    """
    if not view.lines:
        summary = "🧺 Cart is empty."
        if note:
            summary = f"{note}\n{summary}"
        return ok(summary, bill_id=view.bill_id, items=0)

    repo = get_repo()
    seq = repo.upcoming_daily_seq()
    biller = view.biller_name or repo.get_preference("active_cashier")
    biller_confirmed = getattr(view, "biller_confirmed", False)
    rows = [f"🛒 **Draft Bill #{seq} (In Progress)**"]
    meta_info = []
    if biller:
        if biller_confirmed:
            meta_info.append(f"Cashier: {biller}")
        else:
            meta_info.append(f"Cashier: {biller} (unconfirmed)")
    if view.customer_name:
        cust_str = view.customer_name
        if view.customer_phone:
            cust_str += f" (+91 {view.customer_phone})"
        meta_info.append(f"Customer: {cust_str}")
    if meta_info:
        rows.append(f"*({' · '.join(meta_info)})*")

    for ln in view.lines:
        rows.append(f"• {ln.name} — {ln.qty_display} — {money.format_inr(ln.line_total_paise)}")
    rows.append("")
    rows.append(f"Subtotal: {money.format_inr(view.subtotal_paise)}")
    rows.append(
        f"GST: CGST {money.format_inr(view.cgst_paise)} + "
        f"SGST {money.format_inr(view.sgst_paise)}"
    )
    if view.round_off_paise:
        rows.append(f"Round-off: {money.format_inr(view.round_off_paise)}")
    total_line = f"**Total: {money.format_inr(view.grand_total_paise)}**"
    extras = []
    if view.payment_mode:
        extras.append(view.payment_mode.upper())
    if view.customer_name:
        cust_display = view.customer_name
        if view.customer_phone:
            cust_display += f" (+91 {view.customer_phone})"
        extras.append(cust_display)
    if extras:
        total_line += "  ·  " + " · ".join(extras)
    rows.append(total_line)
    for w in view.warnings:
        rows.append(f"⚠️ {w}")
    rows.append("")
    rows.append("⚠️ *Draft only — stock is deducted upon finalization with payment.*")

    missing_details = []
    if not biller_confirmed:
        if biller:
            missing_details.append(f"Store Employee / Cashier name (confirm if {biller} or another employee is billing)")
        else:
            missing_details.append("Store Employee / Cashier name (who is billing this order)")
    if not view.customer_name and not view.customer_phone:
        missing_details.append("Customer name & 10-digit mobile number (to track purchase history)")
    if missing_details:
        rows.append(
            "📋 *Required before payment: Ask whether this bill is final or needs edits (add/remove items), "
            f"and ask for { ' and '.join(missing_details) }.*"
        )

    summary = "\n".join(rows)
    if note:
        summary = f"{note}\n\n{summary}"

    has_shortfall = any(l.shortfall for l in view.lines)
    return ok(
        summary,
        bill_id=view.bill_id,
        items=len(view.lines),
        subtotal=money.format_inr(view.subtotal_paise),
        cgst=money.format_inr(view.cgst_paise),
        sgst=money.format_inr(view.sgst_paise),
        round_off=money.format_inr(view.round_off_paise),
        grand_total=money.format_inr(view.grand_total_paise),
        payment_mode=view.payment_mode,
        customer=view.customer_name,
        customer_phone=view.customer_phone,
        cashier=biller,
        has_shortfall=has_shortfall,
        warnings=view.warnings or None,
    )


def _is_discrete_unit(unit: str, is_loose: bool) -> bool:
    """Return True if unit is a packaged discrete item that cannot be sold in fractional amounts."""
    if is_loose:
        return False
    return unit.lower() in ("packet", "piece", "dozen", "bottle", "can", "pouch", "box", "pkt")


def _resolve_product(
    repo,
    product_id: int | None,
    product_name: str | None = None,
    unit: str | None = None,
    quantity: float | None = None,
):
    """Resolve a product by ID or name with automatic reconciliation against hallucinated IDs and unit awareness."""
    if product_id is not None:
        p = repo.get_product(product_id)
        if p is not None:
            if not product_name:
                return p
            p_clean = p.name.lower().strip()
            name_clean = product_name.lower().strip()
            if name_clean in p_clean or p_clean in name_clean:
                return p
            # Mismatch: LLM likely guessed product_id based on a line number in pasted text! Fall through to name search.
    if product_name:
        clean = product_name.strip()
        candidates = repo.search_products(clean, limit=5)
        if not candidates:
            base_name, _ = extract_base_and_variant(clean)
            if base_name and base_name != clean.lower():
                candidates = repo.search_products(base_name, limit=5)

        if candidates:
            for c in candidates:
                if c.name.lower() == clean.lower():
                    return c

            # Unit-aware preference (e.g. 'kg' matching Loose Atta vs 5kg sealed packet)
            if unit:
                norm_unit = unit.strip().lower()
                is_loose_req = norm_unit in ("kg", "kgs", "kilo", "kilos", "g", "gm", "gms", "gram", "grams", "l", "ltr", "litre", "ml")
                is_pack_req = norm_unit in ("packet", "packets", "pkt", "pkts", "pack", "packs", "piece", "pieces", "box", "pouch")
                matching_unit = [
                    c for c in candidates
                    if (c.unit.lower() == norm_unit)
                    or (is_loose_req and c.is_loose)
                    or (is_pack_req and not c.is_loose)
                ]
                if matching_unit:
                    for c in matching_unit:
                        if clean.lower() in c.name.lower() or c.name.lower() in clean.lower():
                            return c
                    return matching_unit[0]

            for c in candidates:
                if clean.lower() in c.name.lower() or c.name.lower() in clean.lower():
                    return c
            return candidates[0]
    if product_id is not None:
        return repo.get_product(product_id)
    return None


@tool(
    "cart_add_item",
    "Add a product to the current bill (or increase its quantity). Use when the "
    "owner is billing a customer and names an item + how many. quantity is the "
    "number; unit is optional (defaults to the product's own unit, e.g. '500' with "
    "unit 'g' for a kg-priced item). If the product has multiple weight/size variants in the catalogue "
    "(e.g. Maggi 70g vs Maggi 10g), you must clarify which variant the owner wants before adding, "
    "or pass confirm_variant=true once confirmed. If quantity is above 1000, ask the owner "
    "'The order seems to be bulk, is this the right quantity?' and pass confirm_bulk=true once confirmed.",
    {
        "type": "object",
        "properties": {
            "product_id": {"type": "integer", "description": "ID of the product to add."},
            "product_name": {"type": "string", "description": "Optional name of the product to add or verify."},
            "quantity": {"type": "number", "description": "Quantity to add (strictly positive)."},
            "unit": {"type": "string", "description": "Optional unit of the quantity."},
            "confirm_variant": {
                "type": "boolean",
                "description": "Set to true only when the owner has explicitly chosen or confirmed which weight/size variant to add among multiple options.",
            },
            "confirm_bulk": {
                "type": "boolean",
                "description": "Set to true only after the owner confirms a bulk order quantity above 1000.",
            },
            "employee_name": {
                "type": "string",
                "description": "Optional name of the employee / cashier billing currently.",
            },
            "customer_name": {
                "type": "string",
                "description": "Optional customer name.",
            },
            "customer_phone": {
                "type": "string",
                "description": "Optional customer 10-digit mobile phone number.",
            },
        },
        "required": ["quantity"],
    },
)
@guard
async def cart_add_item(args):
    qty = args["quantity"]
    if qty <= 0:
        return err("Quantity to add must be strictly positive.", code="invalid_quantity")

    repo = get_repo()
    prod_id = arg(args, "product_id")
    prod_name = arg(args, "product_name")
    input_unit = arg(args, "unit")
    product = _resolve_product(repo, prod_id, prod_name, unit=input_unit, quantity=qty)
    if product is None:
        raise ProductNotFound(f"No product found matching id={prod_id}, name='{prod_name}'.")

    # Discrete unit integrity guardrail
    if _is_discrete_unit(product.unit, product.is_loose) and (qty % 1 != 0):
        return err(
            f"Cannot bill fractional quantity ({qty:g} {product.unit}) for packaged product '{product.name}'. "
            f"Packets are discrete sealed units and must be billed in whole units.",
            code="fractional_discrete_quantity",
        )

    # Bulk quantity confirmation guardrail (> 1000)
    input_unit = arg(args, "unit")
    qty_in_prod_unit = qty if not input_unit or input_unit == product.unit else from_base(to_base(qty, input_unit, product.unit), product.unit)
    unit_str = input_unit or product.unit
    is_bulk = (qty > 1000 if unit_str not in ("g", "ml") else False) or (qty_in_prod_unit > 1000)
    confirm_bulk = bool(arg(args, "confirm_bulk", False))

    if is_bulk and not confirm_bulk:
        return err(
            f"The order seems to be bulk, is this the right quantity? "
            f"(Billing {qty:g} {unit_str} of {product.name}). "
            "Ask the owner 'The order seems to be bulk, is this the right quantity?' "
            "and pass confirm_bulk=true only after the owner confirms.",
            code="bulk_confirmation_required",
        )

    # Multi-variant disambiguation guardrail
    confirm_variant = bool(arg(args, "confirm_variant", False))
    siblings = get_sibling_variants(repo, product.id)
    if len(siblings) > 1 and not confirm_variant:
        user_msg = current_user_message.get() or ""
        base_name, target_variant = extract_base_and_variant(product.name)

        # Check if user message explicitly mentioned this variant (e.g. "70g", "70 g", "70 gm")
        has_variant_in_msg = bool(target_variant and is_variant_specified_in_query(user_msg, target_variant))

        # Check standing store preference (e.g. "default maggi": "Maggi 70g")
        pref_key = f"default_{base_name}".replace(" ", "_")
        pref_val = repo.get_preference(pref_key) or repo.get_preference(f"default {base_name}")
        has_pref = bool(pref_val and pref_val.lower() in product.name.lower())

        if not has_variant_in_msg and not has_pref:
            options = [
                f"{i+1}. {p.name} ({p.stock_display} in stock) - {money.format_inr(p.sell_paise)}"
                for i, p in enumerate(siblings)
            ]
            options_text = "\n".join(options)
            return err(
                f"Product '{base_name.title()}' has multiple weight/pack variants in catalogue:\n"
                f"{options_text}\n"
                f"The owner did not specify which weight or size they want to bill. "
                f"DO NOT add this item to the draft bill yet. "
                f"Ask the owner: 'Which {base_name.title()} would you like to add?' and present the options above. "
                f"Only call cart_add_item with confirm_variant=true after the owner confirms which variant they want.",
                code="variant_confirmation_required",
            )

    chat_id = current_chat.get() or 0
    emp_name = arg(args, "employee_name")
    if emp_name:
        repo.cart_set_biller(chat_id, emp_name)
    cust_name = arg(args, "customer_name")
    cust_phone = arg(args, "customer_phone")
    if cust_name:
        repo.cart_set_customer(chat_id, cust_name, phone=cust_phone)
    elif cust_phone:
        repo.cart_set_customer(chat_id, "Walk-in Customer", phone=cust_phone)

    view = repo.cart_add_item(
        chat_id, product.id, qty, arg(args, "unit")
    )
    return _format_cart(view)


@tool(
    "cart_add_batch",
    "Add multiple products to the current bill in a single operation. "
    "Use when billing multiple items at once, billing from a copy-pasted inventory list, "
    "or billing whole/bulk store inventory. Accepts a list of items with product_id or product_name, quantity, and unit.",
    {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "product_id": {"type": "integer", "description": "Product ID if known."},
                        "product_name": {"type": "string", "description": "Product name from inventory or order."},
                        "quantity": {"type": "number", "description": "Quantity to bill (positive number)."},
                        "unit": {"type": "string", "description": "Unit of quantity (e.g. packet, kg)."},
                        "confirm_variant": {"type": "boolean", "description": "Set true if variant chosen."},
                        "confirm_bulk": {"type": "boolean", "description": "Set true if bulk confirmed."},
                    },
                    "required": ["quantity"],
                },
                "description": "List of products and quantities to add to the cart.",
            },
            "confirm_bulk": {
                "type": "boolean",
                "description": "Set to true if owner confirmed bulk order (>1000 total or single item >1000).",
            },
            "employee_name": {
                "type": "string",
                "description": "Optional name of the employee / cashier billing currently.",
            },
            "customer_name": {
                "type": "string",
                "description": "Optional customer name.",
            },
            "customer_phone": {
                "type": "string",
                "description": "Optional customer 10-digit mobile phone number.",
            },
        },
        "required": ["items"],
    },
)
@guard
async def cart_add_batch(args):
    raw_items = args.get("items", [])
    if not raw_items:
        return err("No items provided in batch.", code="empty_batch")

    batch_confirm_bulk = bool(arg(args, "confirm_bulk", False))
    repo = get_repo()
    chat_id = current_chat.get() or 0
    user_msg = current_user_message.get() or ""

    resolved = []
    total_qty = 0.0

    for idx, item in enumerate(raw_items):
        qty = item.get("quantity", 0)
        if qty <= 0:
            return err(f"Item #{idx+1}: Quantity must be strictly positive.", code="invalid_quantity")

        prod_id = item.get("product_id")
        prod_name = item.get("product_name")
        input_unit = item.get("unit")
        product = _resolve_product(repo, prod_id, prod_name, unit=input_unit, quantity=qty)
        if product is None:
            name_label = prod_name or f"ID {prod_id}"
            return err(f"Item #{idx+1}: Could not find product '{name_label}' in catalogue.", code="product_not_found")

        unit_str = input_unit or product.unit

        # Discrete unit integrity check
        if _is_discrete_unit(product.unit, product.is_loose):
            if qty % 1 != 0:
                floored_qty = int(qty)
                if floored_qty <= 0:
                    return err(
                        f"Cannot bill fractional quantity ({qty:g} {product.unit}) for packaged product '{product.name}'. "
                        f"Packets are discrete sealed units and must be billed in whole units.",
                        code="fractional_discrete_quantity",
                    )
                qty = floored_qty

        # Bulk check on individual item
        qty_in_prod_unit = qty if not input_unit or input_unit == product.unit else from_base(to_base(qty, input_unit, product.unit), product.unit)
        is_item_bulk = (qty > 1000 if unit_str not in ("g", "ml") else False) or (qty_in_prod_unit > 1000)
        item_confirm_bulk = bool(item.get("confirm_bulk", False)) or batch_confirm_bulk

        if is_item_bulk and not item_confirm_bulk:
            return err(
                f"The order seems to be bulk, is this the right quantity? "
                f"(Billing {qty:g} {unit_str} of {product.name}). "
                "Ask the owner 'The order seems to be bulk, is this the right quantity?' "
                "and pass confirm_bulk=true only after the owner confirms.",
                code="bulk_confirmation_required",
            )

        if unit_str not in ("g", "ml"):
            total_qty += qty_in_prod_unit

        # Variant disambiguation check
        confirm_variant = bool(item.get("confirm_variant", False))
        siblings = get_sibling_variants(repo, product.id)
        if len(siblings) > 1 and not confirm_variant:
            base_name, target_variant = extract_base_and_variant(product.name)
            has_variant_in_msg = bool(target_variant and is_variant_specified_in_query(user_msg, target_variant))
            pref_key = f"default_{base_name}".replace(" ", "_")
            pref_val = repo.get_preference(pref_key) or repo.get_preference(f"default {base_name}")
            has_pref = bool(pref_val and pref_val.lower() in product.name.lower())

            if not has_variant_in_msg and not has_pref:
                options = [
                    f"{i+1}. {p.name} ({p.stock_display} in stock) - {money.format_inr(p.sell_paise)}"
                    for i, p in enumerate(siblings)
                ]
                return err(
                    f"Product '{base_name.title()}' has multiple weight/pack variants in catalogue:\n"
                    + "\n".join(options) + "\n"
                    f"The owner did not specify which weight or size they want to bill. "
                    f"Ask the owner: 'Which {base_name.title()} would you like to add?' and present the options above.",
                    code="variant_confirmation_required",
                )

        resolved.append((product, qty, unit_str))

    # Overall batch bulk check
    if total_qty > 1000 and not batch_confirm_bulk:
        return err(
            f"The order seems to be bulk, is this the right quantity? "
            f"(Total batch quantity is {total_qty:g} units across {len(resolved)} items). "
            "Ask the owner 'The order seems to be bulk, is this the right quantity?' "
            "and pass confirm_bulk=true only after the owner confirms.",
            code="bulk_confirmation_required",
        )

    # Apply optional employee / customer details if provided
    emp_name = arg(args, "employee_name")
    if emp_name:
        repo.cart_set_biller(chat_id, emp_name)
    cust_name = arg(args, "customer_name")
    cust_phone = arg(args, "customer_phone")
    if cust_name:
        repo.cart_set_customer(chat_id, cust_name, phone=cust_phone)
    elif cust_phone:
        repo.cart_set_customer(chat_id, "Walk-in Customer", phone=cust_phone)

    # Add items to cart
    view = None
    for prod, q, u in resolved:
        view = repo.cart_add_item(chat_id, prod.id, q, u)

    if view is None:
        view = repo.cart_view(chat_id)

    return _format_cart(view, note=f"Added {len(resolved)} item(s) to draft bill.")


@tool(
    "cart_update_item",
    "Set an item's quantity on the current bill to an absolute value (not a delta). "
    "Use to correct a quantity the owner already added. If quantity is above 1000, ask the owner "
    "'The order seems to be bulk, is this the right quantity?' and pass confirm_bulk=true once confirmed.",
    {
        "type": "object",
        "properties": {
            "product_id": {"type": "integer", "description": "ID of the product to update."},
            "product_name": {"type": "string", "description": "Optional name of the product to update or verify."},
            "quantity": {"type": "number", "description": "Absolute quantity to set."},
            "unit": {"type": "string", "description": "Optional unit."},
            "confirm_bulk": {
                "type": "boolean",
                "description": "Set to true only after the owner confirms a bulk quantity above 1000.",
            },
        },
        "required": ["quantity"],
    },
)
@guard
async def cart_update_item(args):
    qty = args["quantity"]
    if qty < 0:
        return err("Quantity cannot be negative.", code="invalid_quantity")

    repo = get_repo()
    prod_id = arg(args, "product_id")
    prod_name = arg(args, "product_name")
    product = _resolve_product(repo, prod_id, prod_name)
    if product is None:
        raise ProductNotFound(f"No product found matching id={prod_id}, name='{prod_name}'.")

    # Discrete unit integrity guardrail
    if _is_discrete_unit(product.unit, product.is_loose) and (qty % 1 != 0):
        return err(
            f"Cannot bill fractional quantity ({qty:g} {product.unit}) for packaged product '{product.name}'. "
            f"Packets are discrete sealed units and must be billed in whole units.",
            code="fractional_discrete_quantity",
        )

    # Bulk quantity confirmation guardrail (> 1000)
    input_unit = arg(args, "unit")
    qty_in_prod_unit = qty if not input_unit or input_unit == product.unit else from_base(to_base(qty, input_unit, product.unit), product.unit)
    unit_str = input_unit or product.unit
    is_bulk = (qty > 1000 if unit_str not in ("g", "ml") else False) or (qty_in_prod_unit > 1000)
    confirm_bulk = bool(arg(args, "confirm_bulk", False))

    if is_bulk and not confirm_bulk:
        return err(
            f"The order seems to be bulk, is this the right quantity? "
            f"(Billing {qty:g} {unit_str} of {product.name}). "
            "Ask the owner 'The order seems to be bulk, is this the right quantity?' "
            "and pass confirm_bulk=true only after the owner confirms.",
            code="bulk_confirmation_required",
        )

    chat_id = current_chat.get() or 0
    view = repo.cart_update_item(
        chat_id, product.id, qty, arg(args, "unit")
    )
    return _format_cart(view)


@tool(
    "cart_remove_item",
    "Remove a product from the current bill entirely.",
    {
        "type": "object",
        "properties": {"product_id": {"type": "integer"}},
        "required": ["product_id"],
    },
)
@guard
async def cart_remove_item(args):
    repo = get_repo()
    chat_id = current_chat.get() or 0
    view = repo.cart_remove_item(chat_id, args["product_id"])
    return _format_cart(view)


@tool(
    "cart_view",
    "Show the current bill: line items, GST breakup, and grand total.",
    {"type": "object", "properties": {}},
)
@guard
async def cart_view(args):
    repo = get_repo()
    chat_id = current_chat.get() or 0
    view = repo.cart_view(chat_id)
    return _format_cart(view)


@tool(
    "cart_set_payment",
    "Set how the current bill is paid: cash, upi, card, or khata (credit). "
    "reference is optional (e.g. a UPI txn id). Khata also needs a customer name.",
    {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["cash", "upi", "card", "khata"]},
            "reference": {"type": "string"},
        },
        "required": ["mode"],
    },
)
@guard
async def cart_set_payment(args):
    repo = get_repo()
    chat_id = current_chat.get() or 0
    view = repo.cart_set_payment(chat_id, args["mode"], arg(args, "reference"))
    note = f"Payment set to {view.payment_mode}."
    return _format_cart(view, note=note)


@tool(
    "cart_set_biller",
    "Record who is billing currently (cashier / employee name) on the current draft bill and set as the active store cashier for the shift.",
    {
        "type": "object",
        "properties": {
            "employee_name": {
                "type": "string",
                "description": "Name of the employee / cashier billing currently (e.g. 'Ramesh', 'Priya')",
            },
        },
        "required": ["employee_name"],
    },
)
@guard
async def cart_set_biller(args):
    repo = get_repo()
    chat_id = current_chat.get() or 0
    emp_name = args["employee_name"]
    view = repo.cart_set_biller(chat_id, emp_name)
    note = f"Cashier set to {view.biller_name}."
    return _format_cart(view, note=note)


@tool(
    "cart_set_customer",
    "Attach customer details (name and optional 10-digit mobile phone number) to the current bill. "
    "Mobile number is used to track customer purchase history across visits and is required for khata (credit) sales.",
    {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Customer name"},
            "phone": {"type": "string", "description": "Optional 10-digit mobile number to track customer purchase history"},
        },
        "required": ["name"],
    },
)
@guard
async def cart_set_customer(args):
    repo = get_repo()
    chat_id = current_chat.get() or 0
    name = args["name"]
    phone = arg(args, "phone")
    view = repo.cart_set_customer(chat_id, name, phone=phone)
    note = f"Customer set to {view.customer_name}."
    if view.customer_phone:
        note = f"Customer set to {view.customer_name} (+91 {view.customer_phone})."
        try:
            hist = repo.get_customer_history(view.customer_phone)
            if hist.total_visits > 0:
                note += f"\n⭐ Returning customer: {hist.total_visits} past visit(s), total spend {money.format_inr(hist.total_spend_paise)}."
        except Exception:
            pass
    return _format_cart(view, note=note)


@tool(
    "cart_finalize",
    "Finalize the current bill: set payment mode if provided, decrement stock, assign unique invoice number, "
    "and commit the sale. If payment_mode is omitted and none was set on cart, defaults to store default or 'cash'. "
    "Set confirm_below_cost=true only after the owner confirms a loss-making price. Idempotent — a retry returns the same invoice.",
    {
        "type": "object",
        "properties": {
            "payment_mode": {
                "type": "string",
                "enum": ["cash", "upi", "card", "khata"],
                "description": "Payment method: 'cash', 'upi', 'card', or 'khata'. Defaults to store default or 'cash'.",
            },
            "customer_name": {
                "type": "string",
                "description": "Customer name (required for khata sales).",
            },
            "customer_phone": {
                "type": "string",
                "description": "Customer 10-digit mobile phone number to record purchase history.",
            },
            "employee_name": {
                "type": "string",
                "description": "Cashier / employee name billing currently.",
            },
            "payment_ref": {
                "type": "string",
                "description": "Optional payment reference or UPI transaction ID.",
            },
            "confirm_below_cost": {"type": "boolean"},
        },
    },
)
@guard
async def cart_finalize(args):
    repo = get_repo()
    chat_id = current_chat.get() or 0
    view = repo.cart_view(chat_id)
    if not view.lines:
        return ok("Cart is empty — add items before finalizing.", bill_id=view.bill_id)

    emp_name = arg(args, "employee_name")
    if emp_name:
        view = repo.cart_set_biller(chat_id, emp_name)

    cust_name = arg(args, "customer_name")
    cust_phone = arg(args, "customer_phone")
    if cust_name:
        view = repo.cart_set_customer(chat_id, cust_name, phone=cust_phone)
    elif cust_phone and not view.customer_phone:
        view = repo.cart_set_customer(chat_id, view.customer_name or "Walk-in Customer", phone=cust_phone)

    pay_mode = arg(args, "payment_mode")
    pay_ref = arg(args, "payment_ref")
    if pay_mode:
        view = repo.cart_set_payment(chat_id, pay_mode, ref=pay_ref)
    elif not view.payment_mode:
        default_pay = repo.get_preference("default_payment") or "cash"
        view = repo.cart_set_payment(chat_id, default_pay, ref=pay_ref)

    idempotency_key = f"bill-{view.bill_id}"
    bill = repo.finalize_bill(
        chat_id,
        idempotency_key,
        confirm_below_cost=bool(arg(args, "confirm_below_cost", False)),
    )

    today_str = today_iso()
    ref_display = bill.reference_display(today_str)
    total = money.format_inr(bill.total_paise)
    biller_note = f" (Cashier: {bill.biller_name})" if bill.biller_name else ""
    cust_note = f" for {bill.customer_name}" if bill.customer_name else ""
    if bill.customer_phone and bill.customer_name:
        cust_note += f" (+91 {bill.customer_phone})"

    if bill.was_retry:
        summary = (
            f"{ref_display} (Invoice {bill.invoice_no}) was already finalized — "
            f"{total}, paid by {bill.payment_mode}{cust_note}{biller_note}."
        )
    else:
        summary = (
            f"{ref_display} saved. Invoice No: {bill.invoice_no}. Total {total}, paid by "
            f"{bill.payment_mode}{cust_note}{biller_note}. Stock updated."
        )
    return ok(
        summary,
        bill_id=bill.id,  # use this for generate_invoice_pdf
        invoice_no=bill.invoice_no,
        daily_seq=bill.daily_seq,
        bill_reference=ref_display,
        grand_total=total,
        payment_mode=bill.payment_mode,
        customer=bill.customer_name,
        customer_phone=bill.customer_phone,
        cashier=bill.biller_name,
        was_retry=bill.was_retry,
    )


@tool(
    "lookup_bill",
    "Find and inspect a finalized bill by daily bill number and date, invoice number, "
    "or bill ID. Use when the owner asks to see or check a bill, e.g. 'show bill 2 of today', "
    "'bill 2 of September 3', 'check invoice INV-20260905-0001', or 'bill 2'.",
    {
        "type": "object",
        "properties": {
            "bill_no": {"type": "integer", "description": "Daily bill sequence number (e.g. 1, 2, 3)"},
            "date": {"type": "string", "description": "Date of the bill: 'today', 'yesterday', or YYYY-MM-DD (e.g. '2026-09-03')"},
            "invoice_no": {"type": "string", "description": "Exact invoice number like 'INV-20260905-0001'"},
            "bill_id": {"type": "integer", "description": "Internal database bill ID"},
        },
    },
)
@guard
async def lookup_bill(args):
    repo = get_repo()
    bill_no = arg(args, "bill_no")
    date_arg = arg(args, "date")
    invoice_no = arg(args, "invoice_no")
    bill_id = arg(args, "bill_id")

    bd = None
    today = today_iso()

    # 1. By invoice_no
    if invoice_no:
        bd = repo.get_bill_by_invoice(invoice_no)
        if not bd:
            return err(f"No bill found with invoice number '{invoice_no}'.")

    # 2. By bill_id
    elif bill_id is not None:
        try:
            bd = repo.get_bill(int(bill_id))
        except Exception:
            return err(f"No bill found with ID {bill_id}.")

    # 3. By bill_no and date
    elif bill_no is not None:
        target_date = today
        if date_arg:
            clean_date = str(date_arg).strip().lower()
            if clean_date in ("today", "aaj", "indru"):
                target_date = today
            elif clean_date in ("yesterday", "kal", "netru"):
                from datetime import date, timedelta
                target_date = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
            else:
                try:
                    from datetime import date
                    target_date = date.fromisoformat(clean_date).isoformat()
                except Exception:
                    import re
                    m = re.search(r"(\d{1,2})\s+([a-zA-Z]+)", clean_date)
                    if m:
                        import datetime
                        try:
                            cur_year = today[:4]
                            dt = datetime.datetime.strptime(f"{m.group(1)} {m.group(2)} {cur_year}", "%d %B %Y").date()
                            target_date = dt.isoformat()
                        except Exception:
                            pass

        if date_arg:
            bd = repo.get_bill_by_date_and_seq(target_date, int(bill_no))
            if not bd:
                return err(f"No Bill #{bill_no} found for date {target_date}.")
        else:
            bd = repo.get_bill_by_date_and_seq(today, int(bill_no))
            if not bd:
                matches = repo.find_bills_by_seq(int(bill_no))
                if not matches:
                    return err(f"No Bill #{bill_no} found on today ({today}) or any past date.")
                if len(matches) == 1:
                    bd = matches[0]
                else:
                    candidates = [f"• {m.bill.reference_display(today)} — {money.format_inr(m.bill.total_paise)}" for m in matches[:5]]
                    cand_text = "\n".join(candidates)
                    return ok(
                        f"Found multiple bills with number #{bill_no} on different dates:\n{cand_text}\n\nWhich date would you like to view?",
                        candidates=[{"date": m.bill.date_str, "invoice_no": m.bill.invoice_no} for m in matches[:5]],
                    )

    if bd is None:
        latest_id = repo.latest_finalized_bill_id()
        if not latest_id:
            return err("No finalized bills found.")
        bd = repo.get_bill(latest_id)

    b = bd.bill
    ref = b.reference_display(today)
    lines_desc = [f"• {ln.name} — {ln.qty_display} — {money.format_inr(ln.line_total_paise)}" for ln in bd.lines]
    cust_disp = b.customer_name or "Walk-in"
    if b.customer_phone:
        cust_disp += f" (+91 {b.customer_phone})"
    cashier_line = f"• **Cashier:** {b.biller_name}\n" if b.biller_name else ""
    summary = (
        f"🧾 **{ref}**\n"
        f"• **Invoice No:** {b.invoice_no}\n"
        f"• **Date:** {b.date_str}\n"
        f"• **Customer:** {cust_disp}\n"
        f"{cashier_line}"
        f"• **Payment:** {(b.payment_mode or 'cash').upper()}\n\n"
        f"**Items:**\n" + "\n".join(lines_desc) + "\n\n"
        f"**Total:** {money.format_inr(b.total_paise)} "
        f"(Taxable {money.format_inr(b.subtotal_paise)} + GST {money.format_inr(b.cgst_paise + b.sgst_paise)})"
    )
    return ok(
        summary,
        bill_id=b.id,
        invoice_no=b.invoice_no,
        daily_seq=b.daily_seq,
        date=b.date_str,
        reference=ref,
        grand_total=money.format_inr(b.total_paise),
        payment_mode=b.payment_mode,
        customer=b.customer_name,
        customer_phone=b.customer_phone,
        cashier=b.biller_name,
    )


@tool(
    "cart_cancel",
    "Abandon the current unfinalized bill and start fresh. Use when the owner says "
    "'cancel', 'start over', or wants to drop a draft (e.g. after an out-of-stock "
    "refusal). Does nothing to finalized invoices or stock.",
    {"type": "object", "properties": {}},
)
@guard
async def cart_cancel(args):
    repo = get_repo()
    chat_id = current_chat.get() or 0
    repo.cart_clear(chat_id)
    return ok("Cleared the current bill. Ready for a new one.")


@tool(
    "void_bill",
    "Void or cancel a previously finalized bill/invoice. Automatically restores deducted inventory stock, "
    "restores batches, and reverses credit if billed to khata. Use when the owner says 'void bill #X', "
    "'cancel bill #X', 'void invoice INV-...', or wants to reverse an accidental sale.",
    {
        "type": "object",
        "properties": {
            "bill_id": {"type": "integer", "description": "Internal database bill ID (optional)."},
            "bill_no": {"type": "integer", "description": "Daily sequence number of the bill (e.g. 5, 6, 7)."},
            "invoice_no": {"type": "string", "description": "Invoice number like 'INV-20260906-0006' (optional)."},
        },
    },
)
@guard
async def void_bill(args):
    repo = get_repo()
    bill_id = arg(args, "bill_id")
    bill_no = arg(args, "bill_no")
    invoice_no = arg(args, "invoice_no")

    target_id = None
    if bill_id is not None:
        target_id = int(bill_id)
    elif invoice_no:
        bd = repo.get_bill_by_invoice(invoice_no)
        if not bd:
            return err(f"No bill found with invoice number '{invoice_no}'.")
        target_id = bd.bill.id
    elif bill_no is not None:
        today = today_iso()
        bd = repo.get_bill_by_date_and_seq(today, int(bill_no))
        if not bd:
            matches = repo.find_bills_by_seq(int(bill_no))
            if len(matches) == 1:
                target_id = matches[0].bill.id
            elif len(matches) > 1:
                return err(f"Multiple bills match sequence #{bill_no}. Please specify invoice number or date.")
            else:
                return err(f"No bill found with number #{bill_no}.")
        else:
            target_id = bd.bill.id
    else:
        chat_id = current_chat.get() or 0
        target_id = repo.latest_finalized_bill_id(chat_id)
        if not target_id:
            return err("Please specify the bill number or invoice number to void.")

    voided = repo.void_bill(target_id)
    ref = voided.reference_display(today_iso())
    return ok(
        f"✅ {ref} (Invoice {voided.invoice_no}) has been voided. All product stock has been restored to inventory.",
        bill_id=voided.id,
        invoice_no=voided.invoice_no,
        daily_seq=voided.daily_seq,
        status="void",
    )


BILLING_TOOLS = [
    cart_add_item,
    cart_add_batch,
    cart_update_item,
    cart_remove_item,
    cart_view,
    cart_set_biller,
    cart_set_payment,
    cart_set_customer,
    cart_finalize,
    lookup_bill,
    cart_cancel,
    void_bill,
]
