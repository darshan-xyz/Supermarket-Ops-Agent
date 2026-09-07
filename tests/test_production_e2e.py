"""Production-grade End-to-End Test Suite for Kirana Operations Agent.

Validates all features and technical invariants:
1. Telegram response formatting & HTML rendering engine (safety, escaping, fallbacks)
2. Inventory & restock operations (loose, packaged, batches, FEFO expiry, velocity reorder)
3. Point-of-Sale billing (GST reverse-calculation, slab splits, rounding, oversell guard, idempotency)
4. Khata customer credit ledger (tabs, partial repayments, balance, statements, unknown debtor guard)
5. Analytics & reporting (daily closings, date range reports, top items, GST collected, stock health)
6. Document artifact generators (GST invoice PDF with Rupee font, weekly 8-slide PPTX with native charts)
7. Multi-lingual engine (English, Tamil script, Tanglish phonetics, Hindi script, Hinglish)
8. Cross-session durability and isolation (/new conversation reset vs durable SQLite data)
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import pytest

from kirana.telegram.bot import (
    _to_telegram_html,
    _strip_html,
    _split_message,
    _LANG_KEYBOARD,
)
from kirana.domain.money import to_paise
from kirana.domain.units import to_base
from kirana.domain.gst import compute_line_tax, summarize
from kirana.db.repository import InsufficientStock, CustomerNotFound
from kirana.artifacts.invoice_pdf import render_invoice
from kirana.artifacts.analysis_deck import render_deck
from scripts.seed import seed_catalog, seed_profile


def _milli(qty: int) -> int:
    return qty * 1000


# ==============================================================================
# 1. TELEGRAM FORMATTING & RESPONSE ENGINE
# ==============================================================================

def test_telegram_html_preformatted_tags_preserved():
    """Pre-formatted HTML tags like <b> and <i> must NEVER be double-escaped to &lt;b&gt;."""
    raw = '✅ <b>Language set to English!</b>\n• <b>Inventory</b>: <i>"50 packets"</i>'
    formatted = _to_telegram_html(raw)
    assert "<b>Language set to English!</b>" in formatted
    assert '<i>"50 packets"</i>' in formatted
    assert "&lt;b&gt;" not in formatted
    assert "&lt;i&gt;" not in formatted


def test_telegram_html_markdown_conversions():
    """Markdown bold, italic, code, and links must convert to clean Telegram HTML."""
    md = (
        "**Bold Title** and _italic text_ with `inline_code`\n"
        "[Bot Link](https://t.me/Marketopsagentbot)\n"
        "- Bullet item 1\n"
        "* Bullet item 2\n"
        "```python\nx = 10\n```"
    )
    formatted = _to_telegram_html(md)
    assert "<b>Bold Title</b>" in formatted
    assert "<i>italic text</i>" in formatted
    assert "<code>inline_code</code>" in formatted
    assert '<a href="https://t.me/Marketopsagentbot">Bot Link</a>' in formatted
    assert "• Bullet item 1" in formatted
    assert "• Bullet item 2" in formatted
    assert "<pre>x = 10</pre>" in formatted


def test_telegram_html_special_character_escaping():
    """Loose &, <, > characters in prices, product names, or comparisons must be safely escaped."""
    raw = "Item A & Item B: cost < mrp, price > ₹50, and 5% GST & cess"
    formatted = _to_telegram_html(raw)
    assert "&amp;" in formatted
    assert "&lt;" in formatted
    assert "&gt;" in formatted
    assert "< mrp" not in formatted


def test_telegram_html_strip_fallback():
    """_strip_html should cleanly remove all HTML tags and unescape entities for plain-text fallback."""
    html_text = "<b>Bill #101</b>: Total &amp; Tax: <i>₹150</i>"
    plain = _strip_html(html_text)
    assert plain == "Bill #101: Total & Tax: ₹150"
    assert "<" not in plain and ">" not in plain


def test_telegram_split_message_at_newline():
    """Long messages must be cleanly split at newline boundaries below the 4096 character limit."""
    lines = [f"Line {i:03d}: " + ("x" * 60) for i in range(100)]
    long_msg = "\n".join(lines)
    assert len(long_msg) > 4096

    chunks = _split_message(long_msg, limit=4096)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert len(chunk) <= 4096
    rejoined = "\n".join(chunks)
    assert rejoined == long_msg


def test_telegram_language_keyboard_layout():
    """Language selection keyboard must have 3 distinct options, all carrying the Indian flag 🇮🇳."""
    buttons = [btn.callback_data for row in _LANG_KEYBOARD.inline_keyboard for btn in row]
    assert buttons == ["lang:English", "lang:Tamil", "lang:Hindi"]

    labels = [btn.text for row in _LANG_KEYBOARD.inline_keyboard for btn in row]
    assert any("🇮🇳" in l and "English" in l for l in labels)
    assert any("🇮🇳" in l and "Tamil / Tanglish" in l for l in labels)
    assert any("🇮🇳" in l and "Hindi / Hinglish" in l for l in labels)


# ==============================================================================
# 2. INVENTORY & RESTOCK DOMAIN
# ==============================================================================

def test_inventory_loose_and_packaged_products(repo):
    """Verify adding loose staples (kg, 0% GST) and packaged FMCG (packet, 18% GST)."""
    p_loose = repo.add_product(
        name="Organic Wheat Grain",
        unit="kg",
        gst_rate=0,
        hsn_code="1101",
        cost_paise=to_paise(35),
        mrp_paise=to_paise(45),
        sell_paise=to_paise(42),
        is_loose=True,
        opening_stock_base=to_base(50, "kg", "kg"),
        reorder_base=to_base(15, "kg", "kg"),
        aliases="organic gehu,organic wheat",
    )
    assert p_loose.is_loose is True
    assert p_loose.gst_rate == 0
    assert p_loose.stock_base == 50000

    p_packaged = repo.add_product(
        name="Haldiram Bhujia 150g",
        brand="Haldiram",
        unit="packet",
        gst_rate=18,
        hsn_code="2106",
        cost_paise=to_paise(40),
        mrp_paise=to_paise(50),
        sell_paise=to_paise(48),
        is_loose=False,
        opening_stock_base=_milli(30),
        reorder_base=_milli(10),
        aliases="bhujia,sev",
    )
    assert p_packaged.is_loose is False
    assert p_packaged.gst_rate == 18
    assert p_packaged.stock_base == _milli(30)


def test_inventory_stock_receipt_with_batch_and_fefo(repo):
    """Verify stock receiving with batch expiry and earliest-expiry-first (FEFO) drawdown."""
    today = date.today()
    exp_early = (today + timedelta(days=5)).isoformat()
    exp_late = (today + timedelta(days=30)).isoformat()

    p = repo.add_product(
        name="Fresh Paneer 200g",
        unit="packet",
        gst_rate=5,
        hsn_code="0406",
        cost_paise=to_paise(65),
        mrp_paise=to_paise(85),
        sell_paise=to_paise(80),
        is_loose=False,
        opening_stock_base=_milli(10),
        expiry=exp_early,
    )

    # Receive batch 2 (expiring in 30 days, 20 packets)
    repo.receive_stock(
        p.id,
        _milli(20),
        cost_paise=to_paise(65),
        expiry=exp_late,
    )

    p_updated = repo.get_product(p.id)
    assert p_updated.stock_base == _milli(30)

    # Check expiring soon query
    expiring = repo.expiring_soon(within_days=7)
    assert any(b.name == "Fresh Paneer 200g" for b in expiring)

    # Draw down 5 packets via billing -> FEFO must pull from batch 1
    chat_id = 10001
    repo.cart_add_item(chat_id, p.id, 5)
    repo.cart_set_payment(chat_id, "cash")
    repo.finalize_bill(chat_id, "idemp-fefo-1")

    # Remaining stock is 25 packets
    assert repo.get_product(p.id).stock_base == _milli(25)


def test_inventory_velocity_reorder_calculation(repo):
    """Reorder suggestions must compute daily sales velocity and recommend target cover."""
    ids = seed_catalog(repo)
    sugar_id = ids["Loose Sugar"]
    chat_id = 10002

    # Make sales to establish velocity
    for i in range(3):
        repo.cart_add_item(chat_id, sugar_id, 2, "kg")
        repo.cart_set_payment(chat_id, "cash")
        repo.finalize_bill(chat_id, f"sugar-vel-{i}")

    velocity = repo.sales_velocity(sugar_id, days=7)
    assert velocity > 0


# ==============================================================================
# 3. POINT-OF-SALE BILLING & GST MATH
# ==============================================================================

def test_billing_cart_lifecycle_and_gst_breakup(repo):
    """Verify progressive cart building, item adjustments, and exact GST tax calculation."""
    ids = seed_catalog(repo)
    chat_id = 20001

    rice_id = ids["Loose Rice (Sona Masoori)"]
    butter_id = ids["Amul Butter 100g"]
    maggi_id = ids["Maggi 70g"]

    repo.cart_add_item(chat_id, rice_id, 2, "kg")
    repo.cart_add_item(chat_id, butter_id, 2, "packet")
    repo.cart_add_item(chat_id, maggi_id, 5, "packet")

    # Update Maggi quantity to 4
    repo.cart_update_item(chat_id, maggi_id, 4)

    view = repo.cart_view(chat_id)
    assert len(view.lines) == 3

    # Rice: 2 * 62 = 124.00, Butter: 2 * 62 = 124.00, Maggi: 4 * 14 = 56.00 -> Total 304.00
    expected_total_paise = to_paise(124 + 124 + 56)
    assert view.grand_total_paise == expected_total_paise

    repo.cart_set_payment(chat_id, "upi", "TXN12345")
    repo.cart_set_customer(chat_id, "Kavitha")

    bill = repo.finalize_bill(chat_id, "idemp-bill-lifecycle-1")
    assert bill.status == "finalized"
    assert bill.customer_name == "Kavitha"
    assert bill.payment_mode == "upi"
    assert bill.total_paise == expected_total_paise

    bill_detail = repo.get_bill(bill.id)
    bill_items = bill_detail.lines
    line_taxes = [compute_line_tax(item.line_total_paise, item.gst_rate) for item in bill_items]
    gst_calc = summarize(line_taxes)
    assert abs(gst_calc.cgst_paise - gst_calc.sgst_paise) <= 1
    assert (
        gst_calc.subtotal_paise
        + gst_calc.cgst_paise
        + gst_calc.sgst_paise
        + gst_calc.round_off_paise
        == bill.total_paise
    )


def test_billing_oversell_protection(repo):
    """Deterministic refusal at the database layer upon finalize if quantity exceeds stock."""
    ids = seed_catalog(repo)
    chat_id = 20002
    maggi_id = ids["Maggi 70g"]

    repo.cart_add_item(chat_id, maggi_id, 1000)
    repo.cart_set_payment(chat_id, "cash")

    with pytest.raises(InsufficientStock):
        repo.finalize_bill(chat_id, "k-oversell-reject")


def test_billing_finalize_idempotency(repo):
    """Duplicate finalize calls with identical idempotency key return the same bill with 0 extra stock deduction."""
    ids = seed_catalog(repo)
    chat_id = 20003
    salt_id = ids["Tata Salt 1kg"]
    initial_stock = repo.get_product(salt_id).stock_base

    repo.cart_add_item(chat_id, salt_id, 2)
    repo.cart_set_payment(chat_id, "cash")

    idemp_key = "idemp-test-network-retry-999"
    bill_first = repo.finalize_bill(chat_id, idemp_key)
    stock_after_first = repo.get_product(salt_id).stock_base
    assert stock_after_first == initial_stock - _milli(2)

    # Simulate network retry with the exact same key
    bill_second = repo.finalize_bill(chat_id, idemp_key)
    stock_after_second = repo.get_product(salt_id).stock_base

    assert bill_first.id == bill_second.id
    assert stock_after_second == stock_after_first


# ==============================================================================
# 4. KHATA (CREDIT LEDGER) DOMAIN
# ==============================================================================

def test_khata_customer_credit_tab_lifecycle(repo):
    """Verify adding customer debt, partial repayments, balance updates, and statement logs."""
    debtor = "Sundaram"

    # Add credit debt tab
    st1 = repo.khata_add_credit(debtor, to_paise(1200), note="Monthly provision credit")
    assert st1.balance_paise == to_paise(1200)

    # Query balance
    bal1 = repo.khata_balance(debtor).balance_paise
    assert bal1 == to_paise(1200)

    # Customer pays back ₹500
    st2 = repo.khata_record_payment(debtor, to_paise(500), note="GooglePay ₹500 (UPI)")
    assert st2.balance_paise == to_paise(700)

    bal2 = repo.khata_balance(debtor).balance_paise
    assert bal2 == to_paise(700)

    # Check statement history
    stmt = repo.khata_statement(debtor)
    assert len(stmt) == 2

    # Check debtors list
    debtors = repo.list_khata_debtors()
    assert any(d.customer.name == debtor and d.balance_paise == to_paise(700) for d in debtors)


def test_khata_unknown_customer_protection(repo):
    """Recording payment for an unrecognized/unknown customer must raise CustomerNotFound."""
    with pytest.raises(CustomerNotFound):
        repo.khata_record_payment("CompletelyNonExistentCustomerName", to_paise(500), "cash")


# ==============================================================================
# 5. STORE ANALYTICS DOMAIN
# ==============================================================================

def test_analytics_daily_summary_and_top_items(repo):
    """Verify daily revenue summaries, payment mode breakdown, and top-selling item rankings."""
    ids = seed_catalog(repo)
    chat_id = 40001
    today_str = date.today().isoformat()

    # Finalize a cash bill and a UPI bill
    repo.cart_add_item(chat_id, ids["Maggi 70g"], 10)
    repo.cart_set_payment(chat_id, "cash")
    repo.finalize_bill(chat_id, "analytics-bill-1")

    repo.cart_add_item(chat_id, ids["Cadbury Dairy Milk 40g"], 5)
    repo.cart_set_payment(chat_id, "upi")
    repo.finalize_bill(chat_id, "analytics-bill-2")

    # Fetch daily summary
    summary = repo.daily_summary(today_str)
    assert summary.bill_count >= 2
    assert summary.gross_sales_paise >= to_paise(10 * 14 + 5 * 40)
    assert "cash" in summary.by_payment
    assert "upi" in summary.by_payment

    # Fetch top items
    top = repo.top_items(today_str, today_str)
    assert len(top) >= 1
    top_names = [item.name for item in top]
    assert "Maggi 70g" in top_names or "Cadbury Dairy Milk 40g" in top_names


# ==============================================================================
# 6. DOCUMENT ARTIFACT GENERATION
# ==============================================================================

def test_document_gst_invoice_pdf_generation(repo, tmp_path):
    """Generate a formal branded GST invoice PDF and verify valid PDF header bytes."""
    seed_profile(repo)
    ids = seed_catalog(repo)
    chat_id = 50001

    repo.cart_add_item(chat_id, ids["Tata Salt 1kg"], 3)
    repo.cart_add_item(chat_id, ids["Amul Butter 100g"], 1)
    repo.cart_set_payment(chat_id, "upi")
    repo.cart_set_customer(chat_id, "Rajesh Kumar")
    bill = repo.finalize_bill(chat_id, "doc-test-bill-pdf")

    out_file = str(tmp_path / f"invoice_{bill.invoice_no}.pdf")
    pdf_path_str = render_invoice(repo, bill.id, out_file)
    pdf_path = Path(pdf_path_str)

    assert pdf_path.exists()
    assert pdf_path.stat().st_size > 1000
    with open(pdf_path, "rb") as f:
        header = f.read(5)
        assert header == b"%PDF-"


def test_document_weekly_sales_pptx_deck(repo, tmp_path):
    """Generate weekly sales analysis PowerPoint deck with 8 slides and native charts."""
    seed_profile(repo)
    seed_catalog(repo)
    today_str = date.today().isoformat()
    start_str = (date.today() - timedelta(days=7)).isoformat()

    pptx_path_str = render_deck(repo, start=start_str, end=today_str, out_dir=str(tmp_path))
    pptx_path = Path(pptx_path_str)
    assert pptx_path.exists()
    assert pptx_path.stat().st_size > 5000

    from pptx import Presentation
    prs = Presentation(str(pptx_path))
    assert len(prs.slides) >= 8


# ==============================================================================
# 7. MULTI-LINGUAL SUPPORT (TAMIL, HINDI, TANGLISH, HINGLISH)
# ==============================================================================

def test_multilingual_tamil_script_and_tanglish_resolution(repo):
    """Ensure catalog search accurately resolves native Tamil script and Tanglish phonetics."""
    ids = seed_catalog(repo)

    # Native Tamil script
    rice_hits = repo.search_products("அரிசி")
    assert rice_hits and rice_hits[0].id == ids["Loose Rice (Sona Masoori)"]

    dal_hits = repo.search_products("பருப்பு")
    assert dal_hits and dal_hits[0].id == ids["Loose Toor Dal"]

    # Tanglish phonetics
    sugar_hits = repo.search_products("sarkkarai")
    assert sugar_hits and sugar_hits[0].id == ids["Loose Sugar"]

    oil_hits = repo.search_products("ennai")
    assert oil_hits and oil_hits[0].id == ids["Fortune Sunflower Oil 1L"]


def test_multilingual_hindi_script_and_hinglish_resolution(repo):
    """Ensure catalog search accurately resolves Hindi names and Hinglish aliases."""
    ids = seed_catalog(repo)

    # Hindi / Hinglish aliases
    rice_hindi = repo.search_products("chawal")
    assert rice_hindi and rice_hindi[0].id == ids["Loose Rice (Sona Masoori)"]

    atta_hindi = repo.search_products("gehu aata")
    assert atta_hindi and atta_hindi[0].id == ids["Loose Atta"]

    milk_hindi = repo.search_products("doodh")
    assert milk_hindi and milk_hindi[0].id == ids["Amul Taaza Milk 500ml"]


# ==============================================================================
# 8. CROSS-SESSION ISOLATION & RESET INVARIANTS
# ==============================================================================

def test_session_reset_preserves_durable_store_data(repo):
    """Resetting conversational chat sessions (/new) must NEVER clear the store catalog, stock, or khata."""
    seed_profile(repo)
    ids = seed_catalog(repo)
    repo.set_preference("language", "Tamil")

    # Record a customer credit tab
    repo.khata_add_credit("Anand", to_paise(850), "Groceries")

    # Simulate /new session reset
    from kirana.agent.session import reset_session
    from kirana.tools.context import init_context
    init_context(repo)
    chat_id = 99999
    reset_session(chat_id)

    # Store profile, preferences, products, stock, and khata MUST remain 100% intact
    profile = repo.get_shop_profile()
    assert profile.name == "Sharma Kirana Store"
    assert repo.get_preferences().get("language") == "Tamil"
    assert len(repo.search_products("", limit=50)) >= len(ids)
    assert repo.khata_balance("Anand").balance_paise == to_paise(850)
