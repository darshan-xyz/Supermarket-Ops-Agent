"""Billing tools: multi-turn cart building, edits, GST breakup, and an
idempotent finalize. These are thin adapters over the repository — every
invariant (oversell, below-cost, idempotency, khata integrity) lives there.

The cart is keyed by ``chat_id`` (a draft bill per chat). We never expose
``chat_id`` to the model; it is read from the ContextVar set by the session
runner.
"""

from __future__ import annotations

from .base import tool

from ..domain import money
from ..domain.models import CartView
from .context import current_chat, get_repo
from ._helpers import arg, guard, ok


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

    rows = [f"🧾 **Bill #{view.bill_id}**"]
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
        extras.append(view.customer_name)
    if extras:
        total_line += "  ·  " + " · ".join(extras)
    rows.append(total_line)
    for w in view.warnings:
        rows.append(f"⚠️ {w}")

    summary = "\n".join(rows)
    if note:
        summary = f"{note}\n\n{summary}"

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
        warnings=view.warnings or None,
    )


@tool(
    "cart_add_item",
    "Add a product to the current bill (or increase its quantity). Use when the "
    "owner is billing a customer and names an item + how many. quantity is the "
    "number; unit is optional (defaults to the product's own unit, e.g. '500' with "
    "unit 'g' for a kg-priced item).",
    {
        "type": "object",
        "properties": {
            "product_id": {"type": "integer"},
            "quantity": {"type": "number"},
            "unit": {"type": "string"},
        },
        "required": ["product_id", "quantity"],
    },
)
@guard
async def cart_add_item(args):
    repo = get_repo()
    chat_id = current_chat.get() or 0
    view = repo.cart_add_item(
        chat_id, args["product_id"], args["quantity"], arg(args, "unit")
    )
    return _format_cart(view)


@tool(
    "cart_update_item",
    "Set an item's quantity on the current bill to an absolute value (not a delta). "
    "Use to correct a quantity the owner already added.",
    {
        "type": "object",
        "properties": {
            "product_id": {"type": "integer"},
            "quantity": {"type": "number"},
            "unit": {"type": "string"},
        },
        "required": ["product_id", "quantity"],
    },
)
@guard
async def cart_update_item(args):
    repo = get_repo()
    chat_id = current_chat.get() or 0
    view = repo.cart_update_item(
        chat_id, args["product_id"], args["quantity"], arg(args, "unit")
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
    "cart_set_customer",
    "Attach a customer name to the current bill — required for khata (credit) "
    "sales and useful for named invoices.",
    {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    },
)
@guard
async def cart_set_customer(args):
    repo = get_repo()
    chat_id = current_chat.get() or 0
    view = repo.cart_set_customer(chat_id, args["name"])
    note = f"Customer set to {view.customer_name}."
    return _format_cart(view, note=note)


@tool(
    "cart_finalize",
    "Finalize the current bill: decrement stock, assign an invoice number, and "
    "save the sale. Set confirm_below_cost=true only after the owner confirms a "
    "loss-making price. Idempotent — a retry returns the same invoice.",
    {
        "type": "object",
        "properties": {"confirm_below_cost": {"type": "boolean"}},
    },
)
@guard
async def cart_finalize(args):
    repo = get_repo()
    chat_id = current_chat.get() or 0
    view = repo.cart_view(chat_id)
    if not view.lines:
        return ok("Cart is empty — add items before finalizing.", bill_id=view.bill_id)

    idempotency_key = f"bill-{view.bill_id}"
    bill = repo.finalize_bill(
        chat_id,
        idempotency_key,
        confirm_below_cost=bool(arg(args, "confirm_below_cost", False)),
    )

    total = money.format_inr(bill.total_paise)
    if bill.was_retry:
        summary = (
            f"Bill {bill.invoice_no} was already finalized (no double charge) — "
            f"{total}, paid by {bill.payment_mode}."
        )
    else:
        summary = (
            f"Invoice {bill.invoice_no} saved. Total {total}, paid by "
            f"{bill.payment_mode}. Stock updated."
        )
    return ok(
        summary,
        bill_id=bill.id,  # use this for generate_invoice_pdf
        invoice_no=bill.invoice_no,
        grand_total=total,
        payment_mode=bill.payment_mode,
        customer=bill.customer_name,
        was_retry=bill.was_retry,
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


BILLING_TOOLS = [
    cart_add_item,
    cart_update_item,
    cart_remove_item,
    cart_view,
    cart_set_payment,
    cart_set_customer,
    cart_finalize,
    cart_cancel,
]
