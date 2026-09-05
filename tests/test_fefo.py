"""Expiry / batch tracking with FEFO. Batches are additive: products.stock_base
stays the oversell guard, and SUM(batches.qty_base) must always equal it.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from kirana.db.repository import ValidationError
from tests.conftest import add

CHAT = 1


def _base(n):
    return n * 1000  # packets


def test_stock_in_creates_batches(repo):
    p = add(repo, "Milk", "packet", 0, 30, 26, stock=10)
    batches = repo.product_batches(p.id)
    assert len(batches) == 1 and batches[0].qty_base == _base(10)
    repo.receive_stock(p.id, _base(5))
    assert sum(b.qty_base for b in repo.product_batches(p.id)) == _base(15)


def test_fefo_consumes_earliest_expiry_first(repo):
    soon = (date.today() + timedelta(days=3)).isoformat()
    late = (date.today() + timedelta(days=90)).isoformat()
    m = repo.add_product(name="Curd", unit="packet", gst_rate=0, mrp_paise=3000,
                         sell_paise=3000, cost_paise=2600,
                         opening_stock_base=_base(10), expiry=soon)
    repo.receive_stock(m.id, _base(10), expiry=late)

    repo.cart_add_item(CHAT, m.id, 12)
    repo.cart_set_payment(CHAT, "cash")
    repo.finalize_bill(CHAT, "k-fefo")

    batches = repo.product_batches(m.id)
    assert len(batches) == 1
    assert batches[0].expiry == late and batches[0].qty_base == _base(8)  # soon fully drained
    assert sum(b.qty_base for b in batches) == repo.get_product(m.id).stock_base


def test_invariant_holds_after_ops(seeded):
    repo, ids = seeded
    repo.receive_stock(ids["maggi"], _base(20))
    repo.cart_add_item(CHAT, ids["maggi"], 7)
    repo.cart_set_payment(CHAT, "upi")
    repo.finalize_bill(CHAT, "k-inv")
    for pid in ids.values():
        p = repo.get_product(pid)
        assert sum(b.qty_base for b in repo.product_batches(pid)) == p.stock_base


def test_expiring_soon(repo):
    soon = (date.today() + timedelta(days=2)).isoformat()
    expired = (date.today() - timedelta(days=1)).isoformat()
    far = (date.today() + timedelta(days=60)).isoformat()
    repo.add_product(name="Bread", unit="packet", gst_rate=5, mrp_paise=4000,
                     sell_paise=4000, opening_stock_base=_base(5), expiry=soon)
    repo.add_product(name="Paneer", unit="packet", gst_rate=5, mrp_paise=8000,
                     sell_paise=8000, opening_stock_base=_base(3), expiry=expired)
    repo.add_product(name="Ghee", unit="packet", gst_rate=12, mrp_paise=60000,
                     sell_paise=60000, opening_stock_base=_base(4), expiry=far)

    names = {e.name for e in repo.expiring_soon(7)}
    assert "Bread" in names and "Paneer" in names  # soon + already-expired
    assert "Ghee" not in names                       # 60 days out
    paneer = next(e for e in repo.expiring_soon(7) if e.name == "Paneer")
    assert paneer.days_left < 0                       # flagged as expired


def test_legacy_stock_backfilled(repo):
    # Simulate pre-FEFO data: stock_base set with no batch rows, then re-init.
    with repo.db.transaction() as c:
        c.execute(
            "INSERT INTO products (name,unit,gst_rate,cost_paise,mrp_paise,sell_paise,"
            "stock_base,reorder_base,active,created_at,updated_at) "
            "VALUES ('Legacy','kg',0,100,200,200,?,0,1,'t','t')", (_base(5),))
    repo.init()
    leg = repo.search_products("Legacy")[0]
    assert sum(b.qty_base for b in repo.product_batches(leg.id)) == leg.stock_base


def test_void_restores_a_batch(seeded):
    repo, ids = seeded
    before = repo.get_product(ids["maggi"]).stock_base
    repo.cart_add_item(CHAT, ids["maggi"], 5)
    repo.cart_set_payment(CHAT, "cash")
    b = repo.finalize_bill(CHAT, "k-void")
    repo.void_bill(b.id)
    p = repo.get_product(ids["maggi"])
    assert p.stock_base == before
    assert sum(bt.qty_base for bt in repo.product_batches(ids["maggi"])) == p.stock_base


def test_invalid_expiry_rejected(repo):
    with pytest.raises(ValidationError):
        repo.add_product(name="X", unit="packet", gst_rate=0, mrp_paise=100,
                         sell_paise=100, opening_stock_base=_base(1), expiry="31-12-2026")
