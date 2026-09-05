"""Khata (credit ledger) tools — thin adapters over Repository.khata_* methods.

Business invariants (CustomerNotFound, NeedsConfirmation) are raised by the
repository and surfaced via @guard so the model can relay them / ask the owner
for confirmation without any tool-level branching.
"""

from __future__ import annotations

from .base import tool

from ..domain import money
from .context import get_repo
from ._helpers import arg, guard, ok


@tool(
    "khata_add_credit",
    "Add a credit (debit entry) to a customer's khata account — use when a customer "
    "takes goods on credit or you want to record an amount they owe. Creates the "
    "customer automatically if they don't exist yet.",
    {
        "type": "object",
        "properties": {
            "customer_name": {"type": "string", "description": "Customer's name"},
            "amount_rupees": {"type": "number", "description": "Amount in rupees to credit"},
            "note": {"type": "string", "description": "Optional note / reason"},
        },
        "required": ["customer_name", "amount_rupees"],
    },
)
@guard
async def khata_add_credit(args: dict) -> dict:
    repo = get_repo()
    name: str = args["customer_name"]
    amount_paise = money.to_paise(args["amount_rupees"])
    note: str | None = arg(args, "note")

    state = repo.khata_add_credit(name, amount_paise, note=note)
    bal = money.format_inr(state.balance_paise)
    return ok(
        f"Added {money.format_inr(amount_paise)} to {state.customer.name}'s khata. "
        f"New balance: {bal} (owes shop).",
        customer=state.customer.name,
        balance=bal,
    )


@tool(
    "khata_record_payment",
    "Record a payment received from a khata customer — use when a customer pays "
    "back some or all of what they owe. If the payment exceeds the outstanding "
    "balance, pass confirm_overpay=true to record an advance.",
    {
        "type": "object",
        "properties": {
            "customer_name": {"type": "string", "description": "Customer's name"},
            "amount_rupees": {"type": "number", "description": "Amount paid in rupees"},
            "note": {"type": "string", "description": "Optional note"},
            "confirm_overpay": {
                "type": "boolean",
                "description": "Set true to allow payment greater than outstanding balance",
            },
        },
        "required": ["customer_name", "amount_rupees"],
    },
)
@guard
async def khata_record_payment(args: dict) -> dict:
    repo = get_repo()
    name: str = args["customer_name"]
    amount_paise = money.to_paise(args["amount_rupees"])
    note: str | None = arg(args, "note")
    confirm_overpay: bool = bool(arg(args, "confirm_overpay", False))

    state = repo.khata_record_payment(
        name, amount_paise, note=note, allow_overpay=confirm_overpay
    )
    bal = money.format_inr(state.balance_paise)
    bal_msg = f"{bal} (owes shop)" if state.balance_paise > 0 else (
        f"{money.format_inr(-state.balance_paise)} (advance)" if state.balance_paise < 0
        else "₹0.00 (settled)"
    )
    return ok(
        f"Payment of {money.format_inr(amount_paise)} recorded for {state.customer.name}. "
        f"New balance: {bal_msg}.",
        customer=state.customer.name,
        balance=bal,
    )


@tool(
    "khata_balance",
    "Check how much a customer owes on khata. Returns current outstanding balance.",
    {
        "type": "object",
        "properties": {
            "customer_name": {"type": "string", "description": "Customer's name"},
        },
        "required": ["customer_name"],
    },
)
@guard
async def khata_balance(args: dict) -> dict:
    repo = get_repo()
    name: str = args["customer_name"]
    state = repo.khata_balance(name)
    bal = money.format_inr(state.balance_paise)
    return ok(
        f"{state.customer.name} owes {bal}." if state.balance_paise > 0
        else f"{state.customer.name} has no outstanding balance.",
        customer=state.customer.name,
        balance=bal,
        balance_paise=state.balance_paise,
    )


@tool(
    "khata_statement",
    "Show recent khata ledger entries for a customer — credits extended and payments made.",
    {
        "type": "object",
        "properties": {
            "customer_name": {"type": "string", "description": "Customer's name"},
            "limit": {
                "type": "integer",
                "description": "Maximum number of entries to return (default 20)",
            },
        },
        "required": ["customer_name"],
    },
)
@guard
async def khata_statement(args: dict) -> dict:
    repo = get_repo()
    name: str = args["customer_name"]
    limit: int = int(arg(args, "limit", 20))

    entries = repo.khata_statement(name, limit=limit)
    if not entries:
        return ok(f"No khata entries found for {name}.", customer=name, entries=[])

    rows = [
        {
            "type": e.entry_type,
            "amount": money.format_inr(e.amount_paise),
            "note": e.note or "",
            "date": e.created_at[:10],
        }
        for e in entries
    ]
    summary = f"{len(rows)} recent entries for {name}."
    return ok(summary, customer=name, entries=rows)


@tool(
    "list_khata_debtors",
    "List all customers who currently owe money on khata, sorted by outstanding balance.",
    {
        "type": "object",
        "properties": {},
    },
)
@guard
async def list_khata_debtors(args: dict) -> dict:  # noqa: ARG001
    repo = get_repo()
    debtors = repo.list_khata_debtors()
    if not debtors:
        return ok("No outstanding credit — all khata accounts are clear.", debtors=[])

    rows = [
        {"name": s.customer.name, "balance": money.format_inr(s.balance_paise)}
        for s in debtors
    ]
    total = money.format_inr(sum(s.balance_paise for s in debtors))
    return ok(
        f"{len(rows)} customer(s) owe a total of {total}.",
        debtors=rows,
        total=total,
    )


KHATA_TOOLS = [
    khata_add_credit,
    khata_record_payment,
    khata_balance,
    khata_statement,
    list_khata_debtors,
]
