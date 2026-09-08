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


async def test_lookup_bill_and_date_scoped_pdf(_wire):
    p = _wire.add_product(name="Parle-G", unit="packet", gst_rate=18, mrp_paise=1000,
                          sell_paise=1000, cost_paise=800, opening_stock_base=50 * 1000)
    await call("cart_add_item", product_id=p.id, quantity=5)
    await call("cart_set_payment", mode="upi", reference="UPI-TEST-123")
    fin = json.loads(_text(await call("cart_finalize")).split("\n", 1)[1])
    assert fin["daily_seq"] == 1
    assert "INV-" in fin["invoice_no"]

    # lookup_bill by daily bill number and date
    looked = await call("lookup_bill", bill_no=1, date="today")
    assert not _is_error(looked)
    assert "Bill #1 of Today" in _text(looked)
    assert fin["invoice_no"] in _text(looked)

    # generate_invoice_pdf by daily bill number and date
    pdf_res = await call("generate_invoice_pdf", bill_no=1, date="today")
    assert not _is_error(pdf_res)
    assert "Invoice PDF is ready" in _text(pdf_res)


async def test_list_inventory_tool(_wire):
    _wire.add_product(name="Tata Salt 1kg", unit="packet", gst_rate=0, mrp_paise=2800,
                      sell_paise=2800, cost_paise=2000, opening_stock_base=25 * 1000)
    _wire.add_product(name="Loose Rice", unit="kg", gst_rate=0, mrp_paise=6000,
                      sell_paise=5500, cost_paise=4000, opening_stock_base=40 * 1000, is_loose=True)

    res = await call("list_inventory")
    assert not _is_error(res)
    text = _text(res)
    assert "The store inventory is healthy:" in text or "Store inventory" in text
    assert "• Total SKUs: 2" in text
    assert "• Out of stock: 0" in text
    assert "• Low stock: 0" in text
    assert "• Total Cost Value:" in text
    assert "• Total Retail Value:" in text
    assert "Here is the list of products currently in your store:" in text
    assert "• Tata Salt 1kg (25 packet) - ₹28.00" in text
    assert "• Loose Rice (40 kg) - ₹55.00/kg" in text

    # Also test get_inventory alias
    res_alias = await call("get_inventory")
    assert not _is_error(res_alias)
    assert "Tata Salt 1kg (25 packet) - ₹28.00" in _text(res_alias)


async def test_cart_finalize_with_payment_mode_and_fallback(_wire):
    p = _wire.add_product(name="Cadbury Dairy Milk", unit="packet", gst_rate=18, mrp_paise=4000,
                          sell_paise=4000, cost_paise=3000, opening_stock_base=20 * 1000)
    await call("cart_add_item", product_id=p.id, quantity=2)

    # 1. Finalize with payment_mode directly in cart_finalize call
    res = await call("cart_finalize", payment_mode="upi", payment_ref="UPI-DIRECT-1")
    assert not _is_error(res)
    assert "paid by upi" in _text(res)
    assert _wire.get_product(p.id).stock_base == 18 * 1000

    # 2. Finalize without prior payment_mode -> defaults to cash
    await call("cart_add_item", product_id=p.id, quantity=3)
    res_default = await call("cart_finalize")
    assert not _is_error(res_default)
    assert "paid by cash" in _text(res_default)
    assert _wire.get_product(p.id).stock_base == 15 * 1000


async def test_build_system_prompt_with_draft_cart(_wire):
    from kirana.agent.system_prompt import build_system_prompt

    # Empty cart
    prompt_empty = build_system_prompt(_wire, chat_id=1)
    assert "Active draft bill: None (cart is empty)" in prompt_empty

    # Add item to cart via tool
    p = _wire.add_product(name="Maggi 70g", unit="packet", gst_rate=18, mrp_paise=1400,
                          sell_paise=1400, cost_paise=1200, opening_stock_base=50 * 1000)
    await call("cart_add_item", product_id=p.id, quantity=2)

    prompt_active = build_system_prompt(_wire, chat_id=1)
    assert "Active draft bill for this chat:" in prompt_active
    assert "Maggi 70g: 2 packet" in prompt_active
    assert "STATUS: DRAFT (Pending payment confirmation)." in prompt_active
    assert "MANDATORY BEHAVIOR: If the owner's message asks another question or skips the payment mode" in prompt_active


async def test_receive_stock_normal_quantity_succeeds(_wire):
    p = _wire.add_product(name="Amul Milk", unit="packet", gst_rate=0, mrp_paise=3000,
                          sell_paise=3000, cost_paise=2600, opening_stock_base=50 * 1000)
    # Receiving 1000 packets (not above 1000) does not require confirmation
    res = await call("receive_stock", product_id=p.id, quantity=1000)
    assert not _is_error(res)
    assert _wire.get_product(p.id).stock_base == 1050 * 1000


async def test_receive_stock_bulk_quantity_requires_confirmation(_wire):
    p = _wire.add_product(name="Loose Rice", unit="kg", gst_rate=0, mrp_paise=6000,
                          sell_paise=6000, cost_paise=5000, opening_stock_base=100 * 1000)
    # Attempting to receive 1500 kg (> 1000) without confirm_bulk
    res = await call("receive_stock", product_id=p.id, quantity=1500)
    assert _is_error(res)
    text = _text(res)
    assert "The order seems to be bulk, is this the right quantity?" in text
    assert "No changes were made" in text
    # Verify stock was NOT changed
    assert _wire.get_product(p.id).stock_base == 100 * 1000

    # With confirm_bulk=True, stock update succeeds
    res_confirmed = await call("receive_stock", product_id=p.id, quantity=1500, confirm_bulk=True)
    assert not _is_error(res_confirmed)
    assert _wire.get_product(p.id).stock_base == 1600 * 1000


async def test_receive_stock_sub_unit_grams_not_flagged_as_bulk(_wire):
    p = _wire.add_product(name="Loose Sugar", unit="kg", gst_rate=0, mrp_paise=4500,
                          sell_paise=4500, cost_paise=4000, opening_stock_base=10 * 1000)
    # 1500 g is only 1.5 kg, which should not be flagged as bulk
    res = await call("receive_stock", product_id=p.id, quantity=1500, unit="g")
    assert not _is_error(res)
    assert _wire.get_product(p.id).stock_base == 11500


async def test_cart_add_fractional_packet_rejected(_wire):
    p = _wire.add_product(name="Aashirvaad Atta 5kg", unit="packet", gst_rate=5, mrp_paise=27500,
                          sell_paise=27000, cost_paise=25000, opening_stock_base=20 * 1000)
    # 1.5 packets of a packaged item must be rejected
    res = await call("cart_add_item", product_id=p.id, quantity=1.5)
    assert _is_error(res)
    assert "Packets are discrete sealed units" in _text(res)


async def test_void_bill_tool(_wire):
    p = _wire.add_product(name="Cadbury 40g", unit="packet", gst_rate=18, mrp_paise=4000,
                          sell_paise=4000, cost_paise=3500, opening_stock_base=10 * 1000)
    await call("cart_add_item", product_id=p.id, quantity=2)
    fin = await call("cart_finalize", payment_mode="cash")
    assert not _is_error(fin)
    assert _wire.get_product(p.id).stock_base == 8 * 1000

    # Void the bill using void_bill tool
    void_res = await call("void_bill", bill_no=1)
    assert not _is_error(void_res)
    assert "has been voided" in _text(void_res)
    assert _wire.get_product(p.id).stock_base == 10 * 1000


async def test_update_product_tool(_wire):
    p = _wire.add_product(name="Fortune Sunflower Oil 1L", unit="litre", gst_rate=5,
                          mrp_paise=15000, sell_paise=14500, cost_paise=13000, opening_stock_base=20 * 1000)
    res = await call(
        "update_product",
        product_id=p.id,
        sell_rupees=140.0,
        mrp_rupees=155.0,
        cost_rupees=125.0,
        reorder_qty=5,
    )
    assert not _is_error(res)
    updated = _wire.get_product(p.id)
    assert updated.sell_paise == 14000
    assert updated.mrp_paise == 15500
    assert updated.cost_paise == 12500
    assert updated.reorder_base == 5000


async def test_adjust_stock_tool_reduce_damage(_wire):
    p = _wire.add_product(name="Britannia Bread 400g", unit="packet", gst_rate=0,
                          mrp_paise=4000, sell_paise=4000, cost_paise=3200, opening_stock_base=15 * 1000)
    # Deduct 3 packets due to mold/spoilage
    res = await call(
        "adjust_stock",
        product_id=p.id,
        quantity=3,
        mode="reduce",
        reason="spoilage",
        note="mold detected",
    )
    assert not _is_error(res)
    assert "reduced by 3 packet" in _text(res)
    assert _wire.get_product(p.id).stock_base == 12 * 1000


async def test_adjust_stock_tool_set_physical_count(_wire):
    p = _wire.add_product(name="Basmati Rice 1kg", unit="packet", gst_rate=5,
                          mrp_paise=12000, sell_paise=11000, cost_paise=9500, opening_stock_base=50 * 1000)
    # Physical audit count showed only 42 packets on shelf
    res = await call(
        "adjust_stock",
        product_id=p.id,
        quantity=42,
        mode="set",
        reason="audit_correction",
    )
    assert not _is_error(res)
    assert _wire.get_product(p.id).stock_base == 42 * 1000


async def test_adjust_stock_tool_excessive_reduction_rejected(_wire):
    p = _wire.add_product(name="Amul Ghee 500ml", unit="packet", gst_rate=12,
                          mrp_paise=35000, sell_paise=34000, cost_paise=30000, opening_stock_base=5 * 1000)
    res = await call("adjust_stock", product_id=p.id, quantity=10, mode="reduce")
    assert _is_error(res)
    assert "Cannot reduce stock" in _text(res)
    # Stock preserved
    assert _wire.get_product(p.id).stock_base == 5 * 1000


async def test_receive_stock_strictly_positive(_wire):
    p = _wire.add_product(name="Colgate 100g", unit="packet", gst_rate=18,
                          mrp_paise=6000, sell_paise=5500, cost_paise=4800, opening_stock_base=10 * 1000)
    res_zero = await call("receive_stock", product_id=p.id, quantity=0)
    assert _is_error(res_zero)
    assert "must be strictly positive" in _text(res_zero)

    res_neg = await call("receive_stock", product_id=p.id, quantity=-5)
    assert _is_error(res_neg)
    assert "must be strictly positive" in _text(res_neg)


async def test_cart_add_item_blocks_generic_query_when_siblings_exist(_wire):
    p1 = _wire.add_product(name="Maggi 70g", unit="packet", gst_rate=18, mrp_paise=1400,
                           sell_paise=1400, cost_paise=1200, opening_stock_base=50 * 1000)
    _wire.add_product(name="Maggi 10g", unit="packet", gst_rate=18, mrp_paise=500,
                      sell_paise=500, cost_paise=400, opening_stock_base=100 * 1000)

    # Generic user query without weight specification
    token = ctx.current_user_message.set("If you have sugar in inventory add maggi 1 packet else add butter")
    try:
        res = await call("cart_add_item", product_id=p1.id, quantity=1)
        assert _is_error(res)
        assert "multiple weight/pack variants" in _text(res)
        assert "Maggi 70g" in _text(res) and "Maggi 10g" in _text(res)
        assert "Which Maggi would you like to add?" in _text(res)
    finally:
        ctx.current_user_message.reset(token)


async def test_cart_add_item_allows_explicit_variant_in_query(_wire):
    p1 = _wire.add_product(name="Maggi 70g", unit="packet", gst_rate=18, mrp_paise=1400,
                           sell_paise=1400, cost_paise=1200, opening_stock_base=50 * 1000)
    _wire.add_product(name="Maggi 10g", unit="packet", gst_rate=18, mrp_paise=500,
                      sell_paise=500, cost_paise=400, opening_stock_base=100 * 1000)

    # User explicitly specifies 70g
    token = ctx.current_user_message.set("Order 1 packet Maggi 70g")
    try:
        res = await call("cart_add_item", product_id=p1.id, quantity=1)
        assert not _is_error(res)
        assert "Maggi 70g" in _text(res)
    finally:
        ctx.current_user_message.reset(token)


async def test_cart_add_item_allows_with_confirm_variant(_wire):
    p1 = _wire.add_product(name="Maggi 70g", unit="packet", gst_rate=18, mrp_paise=1400,
                           sell_paise=1400, cost_paise=1200, opening_stock_base=50 * 1000)
    _wire.add_product(name="Maggi 10g", unit="packet", gst_rate=18, mrp_paise=500,
                      sell_paise=500, cost_paise=400, opening_stock_base=100 * 1000)

    # User confirms choice in second turn
    token = ctx.current_user_message.set("add maggi")
    try:
        res = await call("cart_add_item", product_id=p1.id, quantity=1, confirm_variant=True)
        assert not _is_error(res)
        assert "Maggi 70g" in _text(res)
    finally:
        ctx.current_user_message.reset(token)


async def test_cashier_and_customer_purchase_history_tools(_wire):
    p = _wire.add_product(name="Cadbury Dairy Milk", unit="packet", gst_rate=18, mrp_paise=4000,
                          sell_paise=4000, cost_paise=3000, opening_stock_base=20 * 1000)

    # 1. Set biller
    res_biller = await call("cart_set_biller", employee_name="Ramesh")
    assert not _is_error(res_biller)
    assert "Cashier set to Ramesh" in _text(res_biller)

    # 2. Add item and set customer with phone
    res_add = await call("cart_add_item", product_id=p.id, quantity=2)
    assert "Cashier: Ramesh" in _text(res_add)
    res_cust = await call("cart_set_customer", name="Anita", phone="+91 98450 12345")
    assert not _is_error(res_cust)
    assert "Customer set to Anita (+91 9845012345)" in _text(res_cust)

    # 3. Finalize bill
    res_fin = await call("cart_finalize", payment_mode="upi")
    assert not _is_error(res_fin)
    assert "Cashier: Ramesh" in _text(res_fin)
    assert "Anita (+91 9845012345)" in _text(res_fin)

    # 4. Query purchase history via customer_purchase_history tool
    res_hist = await call("customer_purchase_history", phone="9845012345")
    assert not _is_error(res_hist)
    hist_text = _text(res_hist)
    assert "Customer Profile: Anita" in hist_text
    assert "9845012345" in hist_text
    assert "Total Visits:" in hist_text
    assert "1 bill(s)" in hist_text
    assert "Cadbury Dairy Milk" in hist_text
    assert "Billed by Ramesh" in hist_text

    # 5. Second bill for same customer should display returning customer badge
    await call("cart_add_item", product_id=p.id, quantity=1)
    res_cust2 = await call("cart_set_customer", name="Anita", phone="9845012345")
    assert "Returning customer: 1 past visit" in _text(res_cust2)


@pytest.mark.asyncio
async def test_multi_item_order_and_cashier_default(repo):
    from tests.conftest import add

    # Set default cashier preference from store onboarding
    repo.set_preference("last_biller", "Kokki kumar")

    # Add standard items
    add(repo, "Maggi 70g", "packet", 18, 14, 12, stock=80)
    add(repo, "Loose Toor Dal", "kg", 0, 130, 110, stock=30, is_loose=True)
    add(repo, "Loose Sugar", "kg", 5, 45, 40, stock=50, is_loose=True)
    add(repo, "Loose Atta", "kg", 0, 44, 38, stock=40, is_loose=True)
    add(repo, "Aashirvaad Atta 5kg", "packet", 5, 270, 250, stock=20)
    add(repo, "Amul Butter 100g", "packet", 12, 62, 55, stock=24)

    # Multi-item batch billing (as transcribed from user audio)
    res = await call(
        "cart_add_batch",
        items=[
            {"product_name": "Maggi", "quantity": 3, "unit": "packet"},
            {"product_name": "Toor Dal", "quantity": 2, "unit": "kg"},
            {"product_name": "Sugar", "quantity": 5, "unit": "kg"},
            {"product_name": "Atta", "quantity": 4, "unit": "kg"},
            {"product_name": "Butter", "quantity": 2, "unit": "packet"},
        ],
    )
    text = _text(res)
    assert "Maggi 70g" in text
    assert "Loose Toor Dal" in text
    assert "Loose Sugar" in text
    assert "Loose Atta" in text  # Unit-aware: resolved 4 kg to Loose Atta!
    assert "Amul Butter 100g" in text  # Single-match: resolved Butter to Amul Butter 100g!
    assert "Kokki kumar" in text  # Auto-defaulted cashier!
    assert not _is_error(res)
    view = repo.cart_view(1)
    assert len(view.lines) == 5
    assert view.biller_name == "Kokki kumar"


def test_build_system_prompt_language_enforcement(repo):
    from kirana.agent.system_prompt import build_system_prompt

    repo.set_preference("language", "en")
    prompt = build_system_prompt(repo, chat_id=1)
    assert "Configured Store Language: en" in prompt
    assert "MANDATORY LANGUAGE RULE: The store owner's configured language is English." in prompt
    assert "You MUST respond ONLY in English." in prompt

    repo.set_preference("language", "ta")
    prompt_ta = build_system_prompt(repo, chat_id=1)
    assert "Configured Store Language: ta" in prompt_ta
    assert "MANDATORY LANGUAGE RULE: The store owner's configured language is Tamil." in prompt_ta


@pytest.mark.asyncio
async def test_cart_prompts_for_store_employee_name_when_unconfirmed(repo):
    from tests.conftest import add
    from kirana.agent.system_prompt import build_system_prompt

    repo.set_preference("last_biller", "Karthik")
    p = add(repo, "Sunfeast Dark Fantasy 75g", "packet", 18, 40, 30, stock=50)

    # 1. Add item without setting biller
    res = await call("cart_add_item", product_id=p.id, quantity=1)
    assert not _is_error(res)
    text = _text(res)
    assert "Cashier: Karthik (unconfirmed)" in text
    assert "Store Employee / Cashier name" in text
    assert "Customer name & 10-digit mobile number" in text

    # Check system prompt context instructs to ask for store employee
    prompt = build_system_prompt(repo, chat_id=1)
    assert "Cashier / Billed By: Karthik (DEFAULT / UNCONFIRMED - MUST ASK STORE EMPLOYEE NAME)" in prompt
    assert "Store employee / cashier name who is billing this order" in prompt

    # 2. Confirm cashier explicitly
    res_biller = await call("cart_set_biller", employee_name="Suresh")
    assert not _is_error(res_biller)
    biller_text = _text(res_biller)
    assert "Cashier: Suresh" in biller_text
    assert "(unconfirmed)" not in biller_text
    assert "Store Employee / Cashier name" not in biller_text

    # Prompt now shows confirmed
    prompt_confirmed = build_system_prompt(repo, chat_id=1)
    assert "Cashier / Billed By: Suresh (Confirmed for this bill)" in prompt_confirmed


@pytest.mark.asyncio
async def test_employee_management_and_password_authorization(repo):
    ctx.init_context(repo)
    ctx.current_chat.set(101)

    # 1. Initially no employees
    res_list0 = await call("list_employees")
    assert not _is_error(res_list0)
    assert "No employees registered" in _text(res_list0)

    # 2. Add employees
    res_add1 = await call("add_employee", name="Ramesh Kumar", role="cashier")
    assert not _is_error(res_add1)
    assert "EMP-001" in _text(res_add1)
    assert "Ramesh Kumar" in _text(res_add1)

    res_add2 = await call("add_employee", name="Priya Sundaram", role="manager")
    assert not _is_error(res_add2)
    assert "EMP-002" in _text(res_add2)

    # 3. List employees
    res_list = await call("list_employees")
    assert not _is_error(res_list)
    assert "• **EMP-001**: Ramesh Kumar (Cashier)" in _text(res_list)
    assert "• **EMP-002**: Priya Sundaram (Manager)" in _text(res_list)

    # 4. Set owner password
    repo.set_owner_password("Admin#2026")
    assert repo.has_owner_password() is True

    # 5. Remove employee without confirmation or password -> returns warning
    res_rem_warn = await call("remove_employee", code_or_name="EMP-001")
    assert not _is_error(res_rem_warn)
    text_warn = _text(res_rem_warn)
    assert "CONFIRMATION & AUTHORIZATION REQUIRED" in text_warn
    assert "EMP-001" in text_warn
    assert "Ramesh Kumar" in text_warn

    # 6. Remove employee with wrong password -> rejected
    res_rem_wrong = await call("remove_employee", code_or_name="EMP-001", password="wrongpassword", confirm=True)
    assert _is_error(res_rem_wrong)
    assert "Invalid store owner security password/PIN" in _text(res_rem_wrong)

    # 7. Remove employee with correct password -> successfully deactivated
    res_rem_ok = await call("remove_employee", code_or_name="EMP-001", password="Admin#2026", confirm=True)
    assert not _is_error(res_rem_ok)
    assert "successfully deactivated" in _text(res_rem_ok)

    # Verify active list now only has EMP-002
    active_emps = repo.list_employees(include_inactive=False)
    assert len(active_emps) == 1
    assert active_emps[0].emp_code == "EMP-002"

    # Inactive list includes EMP-001
    all_emps = repo.list_employees(include_inactive=True)
    assert len(all_emps) == 2
    assert any(e.emp_code == "EMP-001" and not e.is_active for e in all_emps)

    # 8. Test change_owner_password
    res_chg_bad = await call("change_owner_password", old_password="bad", new_password="NewSecretPass99")
    assert _is_error(res_chg_bad)

    res_chg_ok = await call("change_owner_password", old_password="Admin#2026", new_password="NewSecretPass99")
    assert not _is_error(res_chg_ok)
    assert "updated successfully" in _text(res_chg_ok)
    assert repo.verify_owner_password("NewSecretPass99") is True
    assert repo.verify_owner_password("Admin#2026") is False











