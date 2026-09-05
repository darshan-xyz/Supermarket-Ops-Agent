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
