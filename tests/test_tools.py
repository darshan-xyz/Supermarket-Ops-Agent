"""Drive the actual @tool handlers (the agent-facing surface). Verifies the
tool -> repository -> response path, error surfacing (is_error), and that
ambiguous lookups return candidates for the model to disambiguate.
"""

from __future__ import annotations

import json

import pytest

from kirana.tools import context as ctx
from kirana.tools.registry import ALL_TOOLS

TOOLS = {t.name: t for t in ALL_TOOLS}


def _text(res: dict) -> str:
    return res["content"][0]["text"]


def _is_error(res: dict) -> bool:
    return bool(res.get("is_error"))


async def call(tool_name: str, **args) -> dict:
    return await TOOLS[tool_name].handler(args)


@pytest.fixture(autouse=True)
def _wire(repo, monkeypatch):
    """Point the tool context at the test repo and a fresh chat."""
    ctx.init_context(repo)
    token = ctx.current_chat.set(1)
    yield repo
    ctx.current_chat.reset(token)


async def test_add_product_and_stock(_wire):
    res = await call("add_product", name="Maggi 70g", unit="packet", gst_rate=18,
                     mrp_rupees=14, sell_rupees=14, cost_rupees=12, brand="Nestle",
                     hsn_code="1902", opening_stock_qty=50, reorder_qty=10,
                     aliases="maggi,noodles")
    assert not _is_error(res)
    hits = _wire.search_products("maggi")
    assert len(hits) == 1


async def test_search_returns_candidates_for_disambiguation(_wire):
    _wire.add_product(name="Aashirvaad Atta 5kg", unit="packet", gst_rate=5,
                      mrp_paise=27500, sell_paise=27000, aliases="atta,aata")
    _wire.add_product(name="Loose Atta", unit="kg", gst_rate=0, mrp_paise=4500,
                      sell_paise=4400, is_loose=True, aliases="atta,aata")
    res = await call("search_products", query="atta")
    assert not _is_error(res)
    assert "Aashirvaad" in _text(res) and "Loose Atta" in _text(res)  # both offered


async def test_billing_flow_via_tools(_wire):
    p = _wire.add_product(name="Maggi 70g", unit="packet", gst_rate=18,
                          mrp_paise=1400, sell_paise=1400, cost_paise=1200,
                          opening_stock_base=50 * 1000)
    assert not _is_error(await call("cart_add_item", product_id=p.id, quantity=4))
    assert not _is_error(await call("cart_set_payment", mode="upi", reference="X1"))
    res = await call("cart_finalize")
    assert not _is_error(res)
    assert "INV-" in _text(res)
    assert _wire.get_product(p.id).stock_base == 46 * 1000


async def test_oversell_surfaces_is_error(_wire):
    p = _wire.add_product(name="Atta", unit="packet", gst_rate=5, mrp_paise=27000,
                          sell_paise=27000, cost_paise=25000, opening_stock_base=6 * 1000)
    await call("cart_add_item", product_id=p.id, quantity=100)
    await call("cart_set_payment", mode="cash")
    res = await call("cart_finalize")
    assert _is_error(res)
    assert "insufficient_stock" in _text(res)


async def test_khata_unknown_customer_is_error(_wire):
    res = await call("khata_record_payment", customer_name="Ghost", amount_rupees=100)
    assert _is_error(res)
    assert "customer_not_found" in _text(res)


async def test_cart_cancel_clears_draft(_wire):
    p = _wire.add_product(name="Maggi", unit="packet", gst_rate=18, mrp_paise=1400,
                          sell_paise=1400, opening_stock_base=50 * 1000)
    await call("cart_add_item", product_id=p.id, quantity=3)
    assert len(_wire.cart_view(1).lines) == 1
    res = await call("cart_cancel")
    assert not _is_error(res)
    assert _wire.cart_view(1).lines == []


async def test_generate_pdf_by_invoice_no_and_latest(_wire):
    p = _wire.add_product(name="Maggi", unit="packet", gst_rate=18, mrp_paise=1400,
                          sell_paise=1400, cost_paise=1200, opening_stock_base=50 * 1000)
    await call("cart_add_item", product_id=p.id, quantity=2)
    await call("cart_set_payment", mode="cash")
    fin = json.loads(_text(await call("cart_finalize")).split("\n", 1)[1])
    inv = fin["invoice_no"]
    # by explicit invoice number
    r1 = await call("generate_invoice_pdf", invoice_no=inv)
    assert not _is_error(r1) and r1["content"][0]["text"]
    # by "that bill" (latest, no args)
    r2 = await call("generate_invoice_pdf")
    assert not _is_error(r2)
    # unknown invoice -> is_error
    r3 = await call("generate_invoice_pdf", invoice_no="INV-99999")
    assert _is_error(r3)


async def test_add_with_expiry_then_list_expiring(_wire):
    from datetime import date, timedelta
    soon = (date.today() + timedelta(days=3)).isoformat()
    res = await call("add_product", name="Amul Milk 500ml", unit="packet", gst_rate=0,
                     mrp_rupees=30, sell_rupees=30, opening_stock_qty=10, expiry=soon)
    assert not _is_error(res)
    listed = await call("list_expiring", within_days=7)
    assert not _is_error(listed)
    assert "Amul Milk 500ml" in _text(listed)


async def test_preferences_persist(_wire):
    await call("set_preference", key="default_payment", value="upi")
    res = await call("get_preferences")
    assert "default_payment" in _text(res)
    prof = await call("set_shop_profile", name="Sharma Kirana", gstin="29ABCDE1234F1Z5")
    assert not _is_error(prof)
    assert _wire.get_shop_profile().name == "Sharma Kirana"
