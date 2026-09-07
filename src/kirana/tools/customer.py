"""Customer tools: purchase history, visit analytics, top products, and khata records.

Anchored primarily on customer's 10-digit mobile phone number.
"""

from __future__ import annotations

from .base import tool
from ..db.repository import CustomerNotFound
from ..domain import money
from .context import get_repo
from ._helpers import arg, err, guard, ok


@tool(
    "customer_purchase_history",
    "Look up a customer's purchase history, lifetime spend, visit count, favorite items, "
    "and past bills by mobile number or customer name. Use when the owner asks about a customer's "
    "history (e.g. 'what has 9845012345 bought?', 'customer history of Ramesh', 'past bills for Anita').",
    {
        "type": "object",
        "properties": {
            "phone": {
                "type": "string",
                "description": "Customer's 10-digit mobile phone number (primary identifier).",
            },
            "name": {
                "type": "string",
                "description": "Optional customer name if phone is unknown.",
            },
        },
    },
)
@guard
async def customer_purchase_history(args):
    repo = get_repo()
    phone = arg(args, "phone")
    name = arg(args, "name")

    query = phone or name
    if not query:
        return err("Please provide either a customer mobile phone number or name.")

    try:
        hist = repo.get_customer_history(query)
    except CustomerNotFound as exc:
        return err(str(exc))

    cust = hist.customer
    phone_display = f"+91 {hist.phone}" if hist.phone else "Not registered"
    total_spend = money.format_inr(hist.total_spend_paise)
    avg_bill = money.format_inr(hist.average_bill_paise)
    khata_bal = money.format_inr(hist.khata_balance_paise)

    lines = [
        f"👤 **Customer Profile: {cust.name}**",
        f"• **Mobile:** {phone_display}",
        f"• **Total Visits:** {hist.total_visits} bill(s)",
        f"• **Lifetime Spend:** {total_spend} (Avg: {avg_bill}/visit)",
    ]
    if hist.first_visit_date and hist.last_visit_date:
        lines.append(f"• **Active Period:** {hist.first_visit_date} to {hist.last_visit_date}")
    if hist.khata_balance_paise > 0:
        lines.append(f"• **Khata Balance:** {khata_bal} (owes shop)")

    if hist.top_products:
        lines.append("")
        lines.append("🛍️ **Frequently Purchased Products:**")
        for p in hist.top_products[:5]:
            spend = money.format_inr(p.total_spend_paise)
            lines.append(f"• {p.name} — {p.total_qty_display} ({p.order_count} orders, {spend})")

    if hist.recent_bills:
        lines.append("")
        lines.append("🧾 **Recent Bills:**")
        for b in hist.recent_bills[:5]:
            amt = money.format_inr(b["total_paise"])
            biller_str = f" · Billed by {b['billed_by']}" if b["billed_by"] else ""
            lines.append(f"• #{b['daily_seq']} ({b['date']}) — {b['invoice_no']} — {amt} [{(b['payment_mode'] or 'cash').upper()}]{biller_str}")

    summary = "\n".join(lines)
    return ok(
        summary,
        customer_name=cust.name,
        phone=hist.phone,
        total_visits=hist.total_visits,
        lifetime_spend=total_spend,
        average_bill=avg_bill,
        khata_balance=khata_bal,
        top_products=[
            {"name": p.name, "qty": p.total_qty_display, "spend": money.format_inr(p.total_spend_paise)}
            for p in hist.top_products
        ],
        recent_bills=hist.recent_bills,
    )


CUSTOMER_TOOLS = [
    customer_purchase_history,
]
