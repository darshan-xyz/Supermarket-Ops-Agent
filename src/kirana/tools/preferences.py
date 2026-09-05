"""Durable cross-session memory tools: user preferences and shop profile.

Preferences are arbitrary key/value strings the owner stores so the agent
remembers standing choices (e.g. default_payment=upi, language=hinglish).
The shop profile supplies the header printed on every invoice.
"""

from __future__ import annotations

from .base import tool

from ._helpers import guard, ok
from .context import get_repo


@tool(
    "set_preference",
    "Save a standing preference so the agent remembers it across sessions. "
    "Examples: default_payment=upi, default_brand_atta='Aashirvaad Atta 5kg', language=hinglish.",
    {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Preference key, e.g. 'default_payment'"},
            "value": {"type": "string", "description": "Preference value, e.g. 'upi'"},
        },
        "required": ["key", "value"],
    },
)
@guard
async def set_preference(args: dict) -> dict:
    key: str = args["key"]
    value: str = args["value"]
    repo = get_repo()
    repo.set_preference(key, value)
    return ok(f"Preference saved: {key} = {value}.", key=key, value=value)


@tool(
    "get_preferences",
    "Retrieve all stored preferences. Use to recall standing owner choices before acting.",
    {
        "type": "object",
        "properties": {},
        "required": [],
    },
)
@guard
async def get_preferences(args: dict) -> dict:
    repo = get_repo()
    prefs = repo.get_preferences()
    if not prefs:
        return ok("No preferences saved yet.", preferences={})
    return ok(f"{len(prefs)} preference(s) loaded.", preferences=prefs)


@tool(
    "set_shop_profile",
    "Update the shop profile that appears on invoices. Supply only the fields to change.",
    {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Shop name"},
            "gstin": {"type": "string", "description": "GST Identification Number (15 chars)"},
            "address": {"type": "string", "description": "Shop address"},
            "phone": {"type": "string", "description": "Contact phone number"},
            "state_code": {"type": "string", "description": "2-digit state code for GST (e.g. '27' for Maharashtra)"},
            "invoice_prefix": {"type": "string", "description": "Invoice number prefix, e.g. 'INV' or 'SHOP'"},
        },
        "required": [],
    },
)
@guard
async def set_shop_profile(args: dict) -> dict:
    fields = {
        k: args[k]
        for k in ("name", "gstin", "address", "phone", "state_code", "invoice_prefix")
        if k in args and args[k] is not None
    }
    if not fields:
        return ok("Nothing to update — supply at least one field.")
    repo = get_repo()
    profile = repo.set_shop_profile(**fields)
    return ok(
        "Shop profile updated.",
        name=profile.name,
        gstin=profile.gstin,
        address=profile.address,
        phone=profile.phone,
        state_code=profile.state_code,
        invoice_prefix=profile.invoice_prefix,
    )


@tool(
    "get_shop_profile",
    "Retrieve the current shop profile (name, GSTIN, address, phone, invoice prefix). "
    "Use before generating an invoice or when the owner asks what's on file.",
    {
        "type": "object",
        "properties": {},
        "required": [],
    },
)
@guard
async def get_shop_profile(args: dict) -> dict:
    repo = get_repo()
    profile = repo.get_shop_profile()
    return ok(
        "Shop profile retrieved.",
        name=profile.name,
        gstin=profile.gstin,
        address=profile.address,
        phone=profile.phone,
        state_code=profile.state_code,
        invoice_prefix=profile.invoice_prefix,
    )


MEMORY_TOOLS = [set_preference, get_preferences, set_shop_profile, get_shop_profile]
