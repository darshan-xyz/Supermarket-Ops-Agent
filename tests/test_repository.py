import pytest

from kirana.db.connection import today_iso
from kirana.db.repository import (
    BelowCostSale,
    CustomerNotFound,
    EmptyCart,
    InsufficientStock,
    NeedsConfirmation,
    ValidationError,
)
from tests.conftest import add

CHAT = 1


# ---------------- inventory ----------------
def test_add_and_search_by_alias(repo):
    add(repo, "Aashirvaad Atta 5kg", "packet", 5, 270, 250, stock=6, aliases="atta,aata,gehu")
    hits = repo.search_products("aata")
    assert len(hits) == 1 and hits[0].name == "Aashirvaad Atta 5kg"
    assert repo.search_products("nonexistent") == []


def test_receive_stock_increments(repo):
    p = add(repo, "Maggi 70g", "packet", 18, 14, 12, stock=10)
    repo.receive_stock(p.id, 50 * 1000, cost_paise=1300)
    assert repo.get_product(p.id).stock_base == 60 * 1000


def test_low_stock_and_velocity(seeded):
    repo, ids = seeded
    # sell atta down to/below reorder to make it low
    repo.cart_add_item(CHAT, ids["atta"], 5)
    repo.cart_set_payment(CHAT, "cash")
    repo.finalize_bill(CHAT, "k-low")
    low = repo.list_low_stock()
    assert any(p.id == ids["atta"] for p in low)
    assert repo.sales_velocity(ids["atta"], 14) > 0


# ---------------- billing: build / edit / GST ----------------
def test_multi_turn_bill_with_edit(seeded):
    repo, ids = seeded
    repo.cart_add_item(CHAT, ids["sugar"], 2, "kg")
    repo.cart_add_item(CHAT, ids["atta"], 1)
    repo.cart_add_item(CHAT, ids["maggi"], 4)
    repo.cart_add_item(CHAT, ids["butter"], 1)
    v = repo.cart_view(CHAT)
    assert len(v.lines) == 4
    # edit: drop butter, make maggi 6
    repo.cart_remove_item(CHAT, ids["butter"])
    repo.cart_update_item(CHAT, ids["maggi"], 6)
    v = repo.cart_view(CHAT)
    names = {l.name for l in v.lines}
    assert "Amul Butter 100g" not in names
    maggi_line = next(l for l in v.lines if l.product_id == ids["maggi"])
    assert maggi_line.qty_display == "6 packet"
    # GST breakup present and consistent
    assert v.cgst_paise == v.sgst_paise or abs(v.cgst_paise - v.sgst_paise) <= len(v.lines)
    assert v.grand_total_paise % 100 == 0


def test_stock_decrements_only_on_finalize(seeded):
    repo, ids = seeded
    before = repo.get_product(ids["maggi"]).stock_base
    repo.cart_add_item(CHAT, ids["maggi"], 4)
    assert repo.get_product(ids["maggi"]).stock_base == before  # not yet!
    repo.cart_set_payment(CHAT, "upi")
    repo.finalize_bill(CHAT, "k-fin")
    assert repo.get_product(ids["maggi"]).stock_base == before - 4 * 1000


# ---------------- guardrails ----------------
def test_oversell_refused_at_finalize(seeded):
    repo, ids = seeded
    before = repo.get_product(ids["atta"]).stock_base  # 6 packets
    repo.cart_add_item(CHAT, ids["atta"], 100)          # allowed to build
    repo.cart_set_payment(CHAT, "cash")
    with pytest.raises(InsufficientStock):
        repo.finalize_bill(CHAT, "k-over")
    assert repo.get_product(ids["atta"]).stock_base == before  # unchanged


def test_finalize_requires_payment(seeded):
    repo, ids = seeded
    repo.cart_add_item(CHAT, ids["maggi"], 1)
    with pytest.raises(ValidationError):
        repo.finalize_bill(CHAT, "k-nopay")


def test_below_cost_guard(repo):
    p = add(repo, "Cheap Rice", "kg", 0, 50, 60, stock=10)  # sell < cost
    repo.cart_add_item(CHAT, p.id, 1, "kg")
    repo.cart_set_payment(CHAT, "cash")
    with pytest.raises(BelowCostSale):
        repo.finalize_bill(CHAT, "k-bc")
    # explicit confirm proceeds
    bill = repo.finalize_bill(CHAT, "k-bc2", confirm_below_cost=True)
    assert bill.invoice_no is not None


def test_empty_cart_finalize(repo):
    with pytest.raises(EmptyCart):
        repo.finalize_bill(CHAT, "k-empty")


def test_idempotent_finalize(seeded):
    repo, ids = seeded
    before = repo.get_product(ids["maggi"]).stock_base
    repo.cart_add_item(CHAT, ids["maggi"], 3)
    repo.cart_set_payment(CHAT, "upi")
    b1 = repo.finalize_bill(CHAT, "same-key")
    b2 = repo.finalize_bill(CHAT, "same-key")   # retry
    assert b1.invoice_no == b2.invoice_no
    assert b2.was_retry is True
    assert repo.get_product(ids["maggi"]).stock_base == before - 3 * 1000  # decremented ONCE


def test_void_restores_stock(seeded):
    repo, ids = seeded
    before = repo.get_product(ids["maggi"]).stock_base
    repo.cart_add_item(CHAT, ids["maggi"], 5)
    repo.cart_set_payment(CHAT, "cash")
    b = repo.finalize_bill(CHAT, "k-void")
    repo.void_bill(b.id)
    assert repo.get_product(ids["maggi"]).stock_base == before


def test_daily_bill_numbering_and_unique_invoices(seeded):
    repo, ids = seeded
    # Bill 1 today
    repo.cart_add_item(CHAT, ids["maggi"], 2)
    repo.cart_set_payment(CHAT, "cash")
    b1 = repo.finalize_bill(CHAT, "k-bill1")
    assert b1.daily_seq == 1
    today_slug = b1.date_str.replace("-", "")
    assert b1.invoice_no == f"INV-{today_slug}-0001"
    assert "Bill #1 of Today" in b1.reference_display(b1.date_str)

    # Bill 2 today
    repo.cart_add_item(CHAT, ids["sugar"], 1)
    repo.cart_set_payment(CHAT, "upi")
    b2 = repo.finalize_bill(CHAT, "k-bill2")
    assert b2.daily_seq == 2
    assert b2.invoice_no == f"INV-{today_slug}-0002"
    assert "Bill #2 of Today" in b2.reference_display(b2.date_str)

    # Lookup by date and sequence
    fetched = repo.get_bill_by_date_and_seq(b2.date_str, 2)
    assert fetched is not None
    assert fetched.bill.id == b2.id
    assert fetched.bill.invoice_no == b2.invoice_no

    # Different date bill gets sequence 1
    past_date = "2026-09-03"
    with repo.db.transaction() as c:
        c.execute(
            """INSERT INTO bills (chat_id, status, invoice_no, daily_seq, subtotal_paise,
               total_paise, created_at, finalized_at)
               VALUES (?, 'finalized', 'INV-20260903-0001', 1, 1000, 1000, ?, ?)""",
            (CHAT, f"{past_date}T10:00:00+05:30", f"{past_date}T10:00:00+05:30")
        )
    b_past = repo.get_bill_by_date_and_seq(past_date, 1)
    assert b_past is not None
    assert b_past.bill.daily_seq == 1
    assert "3 Sep 2026" in b_past.bill.reference_display(b1.date_str)

    # find_bills_by_seq(1) returns both bills across dates
    seq1_bills = repo.find_bills_by_seq(1)
    assert len(seq1_bills) >= 2


# ---------------- khata ----------------
def test_khata_cycle(repo):
    repo.khata_add_credit("Ramesh", 50000)          # ₹500
    assert repo.khata_balance("Ramesh").balance_paise == 50000
    repo.khata_record_payment("Ramesh", 30000)      # ₹300
    assert repo.khata_balance("Ramesh").balance_paise == 20000
    assert any(s.customer.name == "Ramesh" for s in repo.list_khata_debtors())


def test_khata_unknown_customer_payment(repo):
    with pytest.raises(CustomerNotFound):
        repo.khata_record_payment("Ghost", 10000)


def test_khata_overpay_needs_confirmation(repo):
    repo.khata_add_credit("Suresh", 10000)
    with pytest.raises(NeedsConfirmation):
        repo.khata_record_payment("Suresh", 15000)
    st = repo.khata_record_payment("Suresh", 15000, allow_overpay=True)
    assert st.balance_paise == -5000  # advance


def test_khata_sale_posts_debit(seeded):
    repo, ids = seeded
    repo.cart_add_item(CHAT, ids["maggi"], 2)
    repo.cart_set_customer(CHAT, "Ramesh")
    repo.cart_set_payment(CHAT, "khata")
    b = repo.finalize_bill(CHAT, "k-khata")
    assert repo.khata_balance("Ramesh").balance_paise == b.total_paise


def test_khata_sale_needs_customer(seeded):
    repo, ids = seeded
    repo.cart_add_item(CHAT, ids["maggi"], 1)
    repo.cart_set_payment(CHAT, "khata")
    with pytest.raises(ValidationError):
        repo.finalize_bill(CHAT, "k-khata-nocust")


# ---------------- analytics ----------------
def test_daily_summary_and_gst(seeded):
    repo, ids = seeded
    repo.cart_add_item(CHAT, ids["maggi"], 4)      # 18%
    repo.cart_add_item(CHAT, ids["atta"], 1)       # 5%
    repo.cart_set_payment(CHAT, "upi")
    b = repo.finalize_bill(CHAT, "k-day")
    rep = repo.daily_summary(today_iso())
    assert rep.bill_count == 1
    assert rep.gross_sales_paise == b.total_paise
    assert rep.by_payment.get("upi") == b.total_paise
    assert rep.total_tax_paise > 0
    g = repo.gst_collected(today_iso(), today_iso())
    assert {s["rate"] for s in g.by_slab} == {5, 18}


def test_stock_health(seeded):
    repo, ids = seeded
    sh = repo.stock_health()
    assert sh.total_skus == 4
    assert sh.inventory_retail_value_paise > 0


def test_list_inventory(seeded):
    repo, ids = seeded
    items = repo.list_inventory()
    assert len(items) == 4
    names = [p.name for p in items]
    assert names == sorted(names)
    assert any(p.name == "Loose Sugar" and p.stock_display == "10 kg" for p in items)
    assert any(p.name == "Maggi 70g" and p.stock_display == "50 packet" for p in items)


def test_update_product(repo):
    p = add(repo, "Parle-G 250g", "packet", 18, 25, 20, stock=20)
    # Update sell, mrp, cost, reorder
    updated = repo.update_product(
        p.id,
        sell_paise=2800,
        mrp_paise=3000,
        cost_paise=2200,
        reorder_base=5 * 1000,
        aliases="parle,parle-g,biscuit",
    )
    assert updated.sell_paise == 2800
    assert updated.mrp_paise == 3000
    assert updated.cost_paise == 2200
    assert updated.reorder_base == 5000
    assert "biscuit" in updated.aliases

    # Legal Metrology violation: sell > mrp
    with pytest.raises(ValidationError, match="Legal Metrology"):
        repo.update_product(p.id, sell_paise=3500, mrp_paise=3000)


def test_adjust_stock_damage_reductions(repo):
    p = add(repo, "Good Day 100g", "packet", 18, 30, 25, stock=20)
    assert p.stock_base == 20000

    # Reduce 3 packets for damage
    adj = repo.adjust_stock(p.id, -3000, reason="damage", note="crushed packets")
    assert adj.stock_base == 17000

    # Verify batch was consumed
    batches = repo.product_batches(p.id)
    assert sum(b.qty_base for b in batches) == 17000

    # Cannot reduce below 0
    with pytest.raises(InsufficientStock):
        repo.adjust_stock(p.id, -18000, reason="damage")


def test_adjust_stock_audit_increase(repo):
    p = add(repo, "Tata Salt 1kg", "packet", 0, 28, 24, stock=10)
    adj = repo.adjust_stock(p.id, 5000, reason="audit_correction", note="surplus found on shelf")
    assert adj.stock_base == 15000
    batches = repo.product_batches(p.id)
    assert sum(b.qty_base for b in batches) == 15000


def test_add_product_rejects_duplicate_active_name(repo):
    add(repo, "Maggi 10g", "packet", 18, 5, 4, stock=100)

    # Attempting to add an identical active product should fail
    with pytest.raises(ValidationError, match="already exists in your store catalogue"):
        add(repo, "maggi 10g", "packet", 18, 5, 4, stock=50)

    with pytest.raises(ValidationError, match="already exists in your store catalogue"):
        add(repo, "  Maggi 10g  ", "packet", 18, 5, 4, stock=50)


def test_biller_attribution_and_preference(repo):
    p = add(repo, "Atta 1kg", "packet", 5, 60, 50, stock=20)
    # Set biller on cart
    cart = repo.cart_add_item(CHAT, p.id, 2)
    cart = repo.cart_set_biller(CHAT, "Ramesh")
    assert cart.biller_name == "Ramesh"
    assert repo.get_preference("active_cashier") == "Ramesh"

    # Set customer and payment, finalize
    repo.cart_set_payment(CHAT, "cash")
    bill = repo.finalize_bill(CHAT, "biller-test-1")
    assert bill.biller_name == "Ramesh"

    # Next bill inherits Ramesh from active_cashier preference
    cart2 = repo.cart_add_item(CHAT, p.id, 1)
    assert cart2.biller_name == "Ramesh"
    repo.cart_set_payment(CHAT, "cash")
    bill2 = repo.finalize_bill(CHAT, "biller-test-2")
    assert bill2.biller_name == "Ramesh"


def test_customer_resolution_and_purchase_history(repo):
    p1 = add(repo, "Sunfeast Dark Fantasy", "packet", 18, 40, 30, stock=50)
    p2 = add(repo, "Tata Tea 250g", "packet", 5, 120, 100, stock=30)

    # First checkout for customer Anita with phone
    repo.cart_add_item(CHAT, p1.id, 2)
    repo.cart_set_customer(CHAT, "Anita", phone="+91 98450 12345")
    repo.cart_set_biller(CHAT, "Priya")
    repo.cart_set_payment(CHAT, "upi")
    bill1 = repo.finalize_bill(CHAT, "cust-hist-1")
    assert bill1.customer_name == "Anita"
    assert bill1.customer_phone == "9845012345"
    assert bill1.biller_name == "Priya"

    # Second checkout for same customer phone with different product
    repo.cart_add_item(CHAT, p2.id, 1)
    repo.cart_set_customer(CHAT, "Anita", phone="9845012345")
    repo.cart_set_payment(CHAT, "cash")
    bill2 = repo.finalize_bill(CHAT, "cust-hist-2")
    assert bill2.customer_phone == "9845012345"

    # Query customer purchase history by phone
    hist = repo.get_customer_history("9845012345")
    assert hist.customer.name == "Anita"
    assert hist.phone == "9845012345"
    assert hist.total_visits == 2
    assert hist.total_spend_paise == bill1.total_paise + bill2.total_paise
    assert len(hist.recent_bills) == 2
    assert len(hist.top_products) == 2

    # Query by name works as well
    hist_name = repo.get_customer_history("Anita")
    assert hist_name.customer.id == hist.customer.id



